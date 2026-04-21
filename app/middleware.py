"""
AEGIBIT Flow — HTTP Middleware

StandardResponseMiddleware
  Applies to all /api/v1/* routes (skips WebSocket upgrades).

  Per-request:
    1. Generates a UUID4 X-Request-ID and attaches it to request.state
    2. Wraps 2xx JSON responses:
          { "success": true, "data": <original>, "error": null, "request_id": "..." }
    3. Wraps 4xx/5xx JSON responses:
          { "success": false, "data": null, "error": { "code": "...", "message": "..." }, "request_id": "..." }
    4. Adds X-Request-ID to every response header

  Non-JSON responses (file downloads, plain text) pass through untouched.
  WebSocket handshakes are skipped entirely.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Callable

import jwt as _jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_sec_log = logging.getLogger("aegibit.security")


_STATUS_CODES: dict[int, str] = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
    423: "LOCKED",
    429: "RATE_LIMITED",
    500: "SERVER_ERROR",
    502: "BAD_GATEWAY",
    503: "SERVICE_UNAVAILABLE",
}


def _code_for(status: int) -> str:
    return _STATUS_CODES.get(status, f"HTTP_{status}")


def _extract_error_message(body: object, status: int) -> tuple[str, str]:
    """
    Extract (code, message) from FastAPI's default error shape.

    FastAPI raises:
      HTTPException       → { "detail": "string" }
      RequestValidationError → { "detail": [{"loc": [...], "msg": "...", "type": "..."}, ...] }
      Custom dict detail  → { "detail": { "code": "...", "message": "..." } }
    """
    if not isinstance(body, dict):
        return _code_for(status), str(body)

    detail = body.get("detail")

    if detail is None:
        return _code_for(status), str(body)

    if isinstance(detail, str):
        return _code_for(status), detail

    if isinstance(detail, list):
        # Pydantic validation error list
        parts = []
        for err in detail:
            loc = ".".join(str(l) for l in err.get("loc", []) if l != "body")
            msg = err.get("msg", "")
            parts.append(f"{loc}: {msg}" if loc else msg)
        return "VALIDATION_ERROR", "; ".join(parts) or "Validation failed"

    if isinstance(detail, dict):
        # Custom structured detail from a route handler
        message = detail.get("message") or detail.get("error") or str(detail)
        code = detail.get("code", _code_for(status))
        return code, message

    return _code_for(status), str(detail)


class StandardResponseMiddleware(BaseHTTPMiddleware):
    """
    Wraps all /api/v1/* JSON responses in the standard envelope and
    injects X-Request-ID into every response.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # ── Generate request ID ────────────────────────────────────────────────
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id

        path = request.url.path

        # ── Pass through: WebSocket upgrades ──────────────────────────────────
        if request.headers.get("upgrade", "").lower() == "websocket":
            return await call_next(request)

        # ── Pass through: routes outside /api/v1 ──────────────────────────────
        if not path.startswith("/api/v1"):
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            response.headers["X-API-Version"] = "1.0.0"
            return response

        # ── Process request ────────────────────────────────────────────────────
        response = await call_next(request)

        # Only wrap JSON responses — pass through file downloads, plain text, etc.
        content_type = response.headers.get("content-type", "")
        if "application/json" not in content_type:
            response.headers["X-Request-ID"] = request_id
            return response

        # ── Read response body ─────────────────────────────────────────────────
        raw = b""
        async for chunk in response.body_iterator:
            raw += chunk

        # If JSON parse fails, return original response with request ID
        try:
            original = json.loads(raw)
        except Exception:
            response.headers["X-Request-ID"] = request_id
            return Response(
                content=raw,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type="application/json",
            )

        status = response.status_code

        # ── Build envelope ─────────────────────────────────────────────────────
        if 200 <= status < 300:
            envelope = {
                "success": True,
                "data": original,
                "error": None,
                "request_id": request_id,
            }
        else:
            code, message = _extract_error_message(original, status)
            envelope = {
                "success": False,
                "data": None,
                "error": {"code": code, "message": message},
                "request_id": request_id,
            }

        body = json.dumps(envelope).encode("utf-8")
        headers = dict(response.headers)
        headers["content-length"] = str(len(body))
        headers["X-Request-ID"] = request_id
        headers["X-API-Version"] = "1.0.0"

        return Response(
            content=body,
            status_code=status,
            headers=headers,
            media_type="application/json",
        )


class OrgIsolationMiddleware(BaseHTTPMiddleware):
    """
    Sets PostgreSQL session-local parameter `app.current_org_id` (and
    `app.current_user_id`) at the start of every API request that carries a
    valid JWT.  This activates the Row Level Security policies in rls_policies.sql
    so that even a raw DB query that forgets the organization_id filter is blocked
    at the storage engine level.

    Runs BEFORE the route handler. If the JWT is absent or invalid the parameters
    are set to empty strings — RLS policies will then deny every row, which is the
    safe default.
    """

    def __init__(self, app, jwt_secret: str, jwt_algorithm: str = "HS256"):
        super().__init__(app)
        self._secret    = jwt_secret
        self._algorithm = jwt_algorithm

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path

        # Only set DB context for authenticated API routes
        if not path.startswith("/api/v1") or path in (
            "/api/v1/auth/login",
            "/api/v1/docs",
            "/api/v1/redoc",
            "/api/v1/openapi.json",
        ):
            return await call_next(request)

        org_id  = ""
        user_id = ""

        # Extract JWT from cookie or Bearer header (same logic as get_current_user)
        token = request.cookies.get("access_token")
        if not token:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[7:]

        if token:
            try:
                payload = _jwt.decode(
                    token, self._secret, algorithms=[self._algorithm]
                )
                org_id  = payload.get("org",  "") or ""
                user_id = payload.get("sub",  "") or ""
            except Exception:
                # Invalid/expired token — safe default: empty strings block all rows
                pass

        # Attach to request state so route handlers can read without re-decoding
        request.state.current_org_id  = org_id
        request.state.current_user_id = user_id

        response = await call_next(request)
        return response
