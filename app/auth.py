"""
Auth module — JWT-based authentication & role-based access control (v2).

Provides:
  hash_password()         — bcrypt password hashing
  verify_password()       — bcrypt password verification
  create_access_token()   — JWT token generation
  decode_access_token()   — JWT token decoding
  get_current_user()      — FastAPI dependency returning AuthContext
  require_role()          — dependency factory for role-gated endpoints
  require_any_role()      — dependency factory accepting multiple roles
  require_admin()         — dependency for ADMIN-only endpoints

Role Hierarchy:
  ADMIN > DIRECTOR > GM > SALES

Higher roles automatically have access to lower-role endpoints.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import IntEnum
from typing import Optional

import bcrypt
import jwt
from fastapi import Request, HTTPException, Depends
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import User, Organization


# ─────────────────────────────────────────────────────────────────────────────
# Role Hierarchy
# ─────────────────────────────────────────────────────────────────────────────

class Role(IntEnum):
    """
    Role hierarchy — higher value = more authority.
    Numeric ordering enables `role >= Role.GM` checks.
    """
    SALES = 10
    GM = 20
    DIRECTOR = 30
    ADMIN = 40


# Friendly lookup: "GM" → Role.GM
ROLE_MAP: dict[str, Role] = {r.name: r for r in Role}

# Valid role names for error messages
VALID_ROLES = sorted(ROLE_MAP.keys())

# Roles that ADMIN can create via API (never ADMIN itself)
CREATABLE_ROLES = ["GM", "DIRECTOR", "SALES"]


def parse_role(role_str: str) -> Role:
    """Parse a role string, case-insensitive. Raises ValueError on invalid."""
    normalized = role_str.strip().upper()
    role = ROLE_MAP.get(normalized)
    if role is None:
        raise ValueError(
            f"Invalid role '{role_str}'. Must be one of: {VALID_ROLES}"
        )
    return role


# ─────────────────────────────────────────────────────────────────────────────
# Password Hashing (bcrypt)
# ─────────────────────────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    """Hash a plaintext password using bcrypt. Returns UTF-8 string."""
    salt = bcrypt.gensalt(rounds=12)
    hashed = bcrypt.hashpw(plain.encode("utf-8"), salt)
    return hashed.decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a plaintext password against a bcrypt hash."""
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# JWT Token Management
# ─────────────────────────────────────────────────────────────────────────────

def create_access_token(user_id: str, org_id: str, role: str) -> str:
    """
    Create a JWT access token with user identity and role.

    Claims:
      sub   → user ID
      org   → organization ID
      role  → user role string
      exp   → expiration timestamp
    """
    payload = {
        "sub": user_id,
        "org": org_id,
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(hours=settings.JWT_EXPIRY_HOURS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """
    Decode and validate a JWT access token.
    Returns the payload dict or raises HTTPException on failure.
    """
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


# ─────────────────────────────────────────────────────────────────────────────
# Auth Context
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AuthContext:
    """
    The result of authentication — available in every endpoint via Depends().

    Attributes:
        user_id    User ID from JWT
        org_id     Organization ID from JWT
        role       Parsed Role enum
        role_name  Original role string (uppercase)
        email      User email from database
    """
    user_id: str
    org_id: str
    role: Role
    role_name: str
    email: str

    def has_role(self, minimum: Role) -> bool:
        """Check if user's role meets or exceeds the minimum."""
        return self.role >= minimum

    def is_admin(self) -> bool:
        return self.role >= Role.ADMIN

    def is_director(self) -> bool:
        return self.role >= Role.DIRECTOR

    def is_gm(self) -> bool:
        return self.role >= Role.GM


# ─────────────────────────────────────────────────────────────────────────────
# Core Dependency: get_current_user (JWT-based)
# ─────────────────────────────────────────────────────────────────────────────

async def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> AuthContext:
    """
    FastAPI dependency — extracts JWT from the HTTP-only cookie and returns AuthContext.

    Token is read exclusively from the `access_token` cookie (set at login).
    JS code cannot access this cookie — eliminates XSS token theft.

    Validates:
      1. access_token cookie is present
      2. JWT is valid and not expired
      3. User exists in database and is active
      4. Organization exists

    Usage:
        @router.get("/endpoint")
        def my_endpoint(auth: AuthContext = Depends(get_current_user)):
            # auth.user_id, auth.org_id, auth.role, auth.role_name
    """
    # Web: read from HTTP-only cookie (set by login endpoint)
    token = request.cookies.get("access_token")

    # Native/mobile (Capacitor): read from Authorization: Bearer <token>
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]

    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Decode JWT
    payload = decode_access_token(token)

    user_id = payload.get("sub")
    org_id = payload.get("org")
    role_str = payload.get("role")

    if not user_id or not org_id or not role_str:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    # Validate user exists and is active
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if not user.is_active:
        raise HTTPException(status_code=401, detail="User account is deactivated")

    # Validate org exists
    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        raise HTTPException(status_code=403, detail="Organization not found")

    # Validate role
    try:
        role = parse_role(role_str)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return AuthContext(
        user_id=user_id,
        org_id=org_id,
        role=role,
        role_name=role.name,
        email=user.email,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Role Gate Dependencies
# ─────────────────────────────────────────────────────────────────────────────

def require_role(minimum_role: Role):
    """
    Dependency factory — returns a dependency that enforces minimum role.

    Usage:
        @router.post("/approve-gm")
        async def approve(auth: AuthContext = Depends(require_role(Role.GM))):
            ...
    """
    async def _check(auth: AuthContext = Depends(get_current_user)) -> AuthContext:
        if not auth.has_role(minimum_role):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Insufficient permissions. "
                    f"Required: {minimum_role.name}, your role: {auth.role_name}"
                ),
            )
        return auth
    return _check


def require_any_role(*roles: Role):
    """
    Dependency factory — returns a dependency that accepts any of the listed roles.
    Does NOT use hierarchy — only exact matches.

    Usage:
        @router.get("/tasks/gm")
        def gm_tasks(auth: AuthContext = Depends(require_any_role(Role.GM, Role.ADMIN))):
            ...
    """
    async def _check(auth: AuthContext = Depends(get_current_user)) -> AuthContext:
        if auth.role not in roles:
            role_names = [r.name for r in roles]
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Access denied. "
                    f"Allowed roles: {role_names}, your role: {auth.role_name}"
                ),
            )
        return auth
    return _check


def require_admin():
    """
    Dependency — enforces ADMIN role only.
    Used for user creation and management endpoints.
    """
    async def _check(auth: AuthContext = Depends(get_current_user)) -> AuthContext:
        if not auth.is_admin():
            raise HTTPException(
                status_code=403,
                detail="Admin access required",
            )
        return auth
    return _check
