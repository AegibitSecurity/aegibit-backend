"""
Auth routes — login, user management, and profile endpoints.

Endpoints:
  POST /auth/login        → Authenticate and get JWT
  POST /users             → Create user (ADMIN only)
  GET  /users             → List users in org (ADMIN only)
  GET  /auth/me           → Get current user profile
  PATCH /users/{id}/toggle → Activate/deactivate user (ADMIN only)
  POST /auth/change-password → Change own password
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
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
def login(body: LoginRequest, db: Session = Depends(get_db)):
    """
    Authenticate user with email and password.
    Returns JWT access token on success.
    """
    # Find user by email
    user = db.query(User).filter(User.email == body.email.lower().strip()).first()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # Check if active
    if not user.is_active:
        raise HTTPException(status_code=401, detail="Account is deactivated. Contact admin.")

    # Verify password
    if not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # Generate JWT
    token = create_access_token(user.id, user.organization_id, user.role)

    return LoginResponse(
        access_token=token,
        user=UserResponse.model_validate(user),
    )


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
      - User is assigned to specified organization
      - Email must be unique across the system
    """
    # Double-check role is not ADMIN (defense in depth — schema also validates)
    if body.role.upper() == "ADMIN":
        raise HTTPException(
            status_code=403,
            detail="Cannot create ADMIN users via API. Use seed script instead.",
        )

    # Validate organization exists
    org = db.query(Organization).filter(Organization.id == body.organization_id).first()
    if not org:
        raise HTTPException(status_code=400, detail="Organization not found")

    # Check for duplicate email
    existing = db.query(User).filter(User.email == body.email).first()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"User with email '{body.email}' already exists",
        )

    # Create user with hashed password
    user = User(
        email=body.email,
        password_hash=hash_password(body.password),
        role=body.role,
        organization_id=body.organization_id,
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
    """
    List all users in the admin's organization.
    ADMIN only.
    """
    users = (
        db.query(User)
        .filter(User.organization_id == auth.org_id)
        .order_by(User.created_at.desc())
        .all()
    )
    return users


# ── Toggle User Active Status (ADMIN only) ───────────────────────────────────

@router.patch("/users/{user_id}/toggle", response_model=UserResponse)
def toggle_user_status(
    user_id: str,
    auth: AuthContext = Depends(require_admin()),
    db: Session = Depends(get_db),
):
    """
    Activate or deactivate a user. ADMIN only.
    Cannot deactivate yourself.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Prevent self-deactivation
    if user.id == auth.user_id:
        raise HTTPException(status_code=400, detail="Cannot deactivate your own account")

    # Ensure same org
    if user.organization_id != auth.org_id:
        raise HTTPException(status_code=403, detail="Cannot modify users from other organizations")

    user.is_active = not user.is_active
    db.commit()
    db.refresh(user)

    return user


# ── Change Password ──────────────────────────────────────────────────────────

@router.post("/auth/change-password")
def change_password(
    body: ChangePasswordRequest,
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Change the authenticated user's password."""
    user = db.query(User).filter(User.id == auth.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Verify current password
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    # Update password
    user.password_hash = hash_password(body.new_password)
    db.commit()

    return {"message": "Password updated successfully"}


# ── Get creatable roles (for frontend dropdown) ──────────────────────────────

@router.get("/users/creatable-roles")
def get_creatable_roles(auth: AuthContext = Depends(require_admin())):
    """Return the list of roles that can be created via API. ADMIN only."""
    return {"roles": CREATABLE_ROLES}
