"""
Auth routes — login, logout, user management, and profile endpoints.

Endpoints:
  POST /auth/login            → Authenticate, set HTTP-only cookie
  POST /auth/logout           → Clear auth cookie
  GET  /auth/me               → Get current user profile
  POST /auth/change-password  → Change own password
  POST /users                 → Create user (ADMIN only)
  GET  /users                 → List users in org (ADMIN only)
  PATCH /users/{id}/toggle    → Activate/deactivate user (ADMIN only)
  GET  /users/creatable-roles → Roles that ADMIN can create
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.limiter import limiter
from app.models import User, Organization
from app.auth import (
    AuthContext,
    Role,
    hash_password,
    verify_password,
    create_access_token,
    get_current_user,
    require_admin,
    CREATABLE_ROLES,
)
from app.config import settings
from app.schemas import (
    LoginRequest,
    LoginResponse,
    CreateUserRequest,
    UserResponse,
    ChangePasswordRequest,
)

router = APIRouter(tags=["auth"])


# ── Login ─────────────────────────────────────────────────────────────────────

@router.post("/auth/login", response_model=LoginResponse)
@limiter.limit("10/minute")
def login(
    request: Request,      # required by slowapi rate limiter
    response: Response,
    body: LoginRequest,
    db: Session = Depends(get_db),
):
    """
    Authenticate user. On success:
      - Sets an HTTP-only, Secure cookie named `access_token`
      - Returns the user object (no token in JSON)

    The cookie is inaccessible to JavaScript — eliminates XSS token theft.
    """
    user = db.query(User).filter(User.email == body.email.lower().strip()).first()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not user.is_active:
        raise HTTPException(status_code=401, detail="Account is deactivated. Contact admin.")

    if not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = create_access_token(user.id, user.organization_id, user.role)

    # SameSite=none requires Secure=true (enforced by browsers).
    # When COOKIE_SAMESITE=none in production, ensure COOKIE_SECURE=true as well.
    samesite = settings.COOKIE_SAMESITE
    secure = settings.COOKIE_SECURE
    if samesite.lower() == "none" and not secure:
        secure = True  # browsers reject SameSite=None without Secure

    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        secure=secure,
        samesite=samesite,
        max_age=settings.JWT_EXPIRY_HOURS * 3600,
        path="/",
    )

    # Include token in response body for native/mobile clients (Capacitor).
    # Web clients use the cookie above and ignore this field.
    return LoginResponse(user=UserResponse.model_validate(user), access_token=token)


# ── Logout ────────────────────────────────────────────────────────────────────

@router.post("/auth/logout")
def logout(response: Response):
    """
    Clear the auth cookie. Frontend should also clear its local user state.
    Returns 200 even if no cookie was present (idempotent).
    """
    response.delete_cookie(
        key="access_token",
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite=settings.COOKIE_SAMESITE,
        path="/",
    )
    return {"message": "Logged out"}


# ── Get Current User ─────────────────────────────────────────────────────────

@router.get("/auth/me", response_model=UserResponse)
def get_me(auth: AuthContext = Depends(get_current_user), db: Session = Depends(get_db)):
    """Return the authenticated user's profile."""
    user = db.query(User).filter(User.id == auth.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


# ── Create User (ADMIN only) ─────────────────────────────────────────────────

@router.post("/users", response_model=UserResponse, status_code=201)
def create_user(
    body: CreateUserRequest,
    auth: AuthContext = Depends(require_admin()),
    db: Session = Depends(get_db),
):
    """
    Create a new user. ADMIN only.

    Restrictions:
      - Only GM, DIRECTOR, SALES roles can be created
      - ADMIN cannot be created via API (prevents privilege escalation)
      - Email must be unique across the system
    """
    if body.role.upper() == "ADMIN":
        raise HTTPException(
            status_code=403,
            detail="Cannot create ADMIN users via API. Use seed script instead.",
        )

    # SECURITY: always use the admin's own org — never trust org_id from the request body.
    # This prevents an ADMIN from Org-A from creating users inside Org-B.
    target_org_id = auth.org_id

    existing = db.query(User).filter(User.email == body.email).first()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"User with email '{body.email}' already exists",
        )

    user = User(
        email=body.email,
        password_hash=hash_password(body.password),
        role=body.role,
        organization_id=target_org_id,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


# ── List Users (ADMIN only) ──────────────────────────────────────────────────

@router.get("/users", response_model=list[UserResponse])
def list_users(
    auth: AuthContext = Depends(require_admin()),
    db: Session = Depends(get_db),
):
    """List all users in the admin's organization. ADMIN only."""
    return (
        db.query(User)
        .filter(User.organization_id == auth.org_id)
        .order_by(User.created_at.desc())
        .all()
    )


# ── Toggle User Active Status (ADMIN only) ───────────────────────────────────

@router.patch("/users/{user_id}/toggle", response_model=UserResponse)
def toggle_user_status(
    user_id: str,
    auth: AuthContext = Depends(require_admin()),
    db: Session = Depends(get_db),
):
    """Activate or deactivate a user. ADMIN only. Cannot deactivate yourself."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.id == auth.user_id:
        raise HTTPException(status_code=400, detail="Cannot deactivate your own account")

    if user.organization_id != auth.org_id:
        raise HTTPException(status_code=403, detail="Cannot modify users from other organizations")

    user.is_active = not user.is_active
    db.commit()
    db.refresh(user)
    return user


# ── Change Password ──────────────────────────────────────────────────────────

@router.post("/auth/change-password")
@limiter.limit("5/minute")
def change_password(
    request: Request,
    body: ChangePasswordRequest,
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Change the authenticated user's password."""
    user = db.query(User).filter(User.id == auth.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    user.password_hash = hash_password(body.new_password)
    db.commit()

    return {"message": "Password updated successfully"}


# ── Switch Organization (ADMIN only — issues a new JWT) ─────────────────────

@router.post("/auth/switch-org", response_model=LoginResponse)
def switch_org(
    response: Response,
    body: dict,
    auth: AuthContext = Depends(require_admin()),
    db: Session = Depends(get_db),
):
    """
    Re-scope the session to a different organization.
    ADMIN only.  Issues a new signed JWT bound to the target org.
    The old JWT (and its cookie) is replaced immediately.

    Body: { "org_id": "<uuid>" }
    """
    import logging
    _log = logging.getLogger(__name__)

    target_org_id = body.get("org_id", "").strip()
    if not target_org_id:
        raise HTTPException(status_code=400, detail="org_id is required")

    org = db.query(Organization).filter(Organization.id == target_org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    _log.info("SWITCH_ORG admin=%s from=%s to=%s", auth.user_id, auth.org_id, target_org_id)

    new_token = create_access_token(auth.user_id, target_org_id, auth.role_name)

    samesite = settings.COOKIE_SAMESITE
    secure = settings.COOKIE_SECURE
    if samesite.lower() == "none" and not secure:
        secure = True

    response.set_cookie(
        key="access_token",
        value=new_token,
        httponly=True,
        secure=secure,
        samesite=samesite,
        max_age=settings.JWT_EXPIRY_HOURS * 3600,
        path="/",
    )

    user = db.query(User).filter(User.id == auth.user_id).first()
    # Return updated user profile with the target org embedded so the frontend
    # can update its stored user object and reflect the org change instantly.
    user_data = UserResponse.model_validate(user)
    # Override org in the response so frontend localStorage matches new JWT
    user_dict = user_data.model_dump()
    user_dict["organization_id"] = target_org_id
    from app.schemas import UserResponse as UR
    patched_user = UR(**user_dict)
    return LoginResponse(user=patched_user, access_token=new_token)


# ── Get creatable roles (for frontend dropdown) ──────────────────────────────

@router.get("/users/creatable-roles")
def get_creatable_roles(auth: AuthContext = Depends(require_admin())):
    """Return the list of roles that can be created via API. ADMIN only."""
    return {"roles": CREATABLE_ROLES}
