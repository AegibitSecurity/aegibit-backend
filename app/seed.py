"""
Seed data — bootstraps demo organizations, configs, and the initial ADMIN user.

Run on app startup if no orgs exist.
"""

from sqlalchemy.orm import Session

from app.models import Organization, OrgConfig, User
from app.auth import hash_password


SEED_ORGS = [
    {
        "name": "Nibir Motors",
        "config": {
            "gm_discount_limit": 5000,
            "director_discount_limit": 15000,
            "min_margin_threshold": 3.0,
        },
    },
    {
        "name": "S.S Bajaj",
        "config": {
            "gm_discount_limit": 10000,
            "director_discount_limit": 25000,
            "min_margin_threshold": 1.5,
        },
    },
]

# Initial ADMIN user — created via seed script only, never via API
SEED_ADMIN = {
    "email": "pradip.ss19@gmail.com",
    "password": "Pradip@1998",
    "role": "ADMIN",
    # Will be assigned to the first organization
}


def seed_database(db: Session):
    """Insert seed organizations + configs + admin user if the orgs table is empty."""
    existing = db.query(Organization).count()
    if existing > 0:
        # Still check if admin user needs to be created
        _seed_admin_if_needed(db)
        return

    first_org_id = None

    for entry in SEED_ORGS:
        org = Organization(name=entry["name"])
        db.add(org)
        db.flush()

        if first_org_id is None:
            first_org_id = org.id

        config = OrgConfig(
            organization_id=org.id,
            **entry["config"],
        )
        db.add(config)

    db.commit()
    print(f"Successfully seeded {len(SEED_ORGS)} organizations")

    # Create admin user after orgs are committed
    _seed_admin_if_needed(db, first_org_id)


def _seed_admin_if_needed(db: Session, org_id: str = None):
    """Create the initial ADMIN user if no admin exists."""
    admin_exists = db.query(User).filter(User.role == "ADMIN").first()
    if admin_exists:
        return

    # If no org_id provided, use the first organization
    if org_id is None:
        first_org = db.query(Organization).first()
        if not first_org:
            return
        org_id = first_org.id

    admin = User(
        email=SEED_ADMIN["email"],
        password_hash=hash_password(SEED_ADMIN["password"]),
        role=SEED_ADMIN["role"],
        organization_id=org_id,
        is_active=True,
    )
    db.add(admin)
    db.commit()
    print(f"Successfully seeded ADMIN user: {SEED_ADMIN['email']}")
