"""
Shared pytest fixtures for the AEGIBIT Flow test suite.

Provides:
  - In-memory SQLite database (isolated per test)
  - Pre-seeded organization + config
  - Pre-seeded car model pricing
  - FastAPI test client with auth headers
"""

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.models import Organization, OrgConfig, CarModel
from app.main import app


# ─────────────────────────────────────────────────────────────────────────────
# Database fixtures
# ─────────────────────────────────────────────────────────────────────────────

TEST_ENGINE = create_engine(
    "sqlite:///./test_aegibit.db",
    connect_args={"check_same_thread": False},
)
TestSession = sessionmaker(bind=TEST_ENGINE)


@pytest.fixture(autouse=True)
def setup_db():
    """Create all tables before each test, drop after."""
    Base.metadata.create_all(bind=TEST_ENGINE)
    yield
    Base.metadata.drop_all(bind=TEST_ENGINE)


@pytest.fixture()
def db_session():
    """Yield a fresh DB session per test."""
    session = TestSession()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


# ─────────────────────────────────────────────────────────────────────────────
# Seed data fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def org(db_session):
    """Create a test organization with config."""
    o = Organization(id="test-org-001", name="Test Motors")
    db_session.add(o)
    db_session.flush()

    config = OrgConfig(
        organization_id=o.id,
        gm_discount_limit=5000,
        director_discount_limit=15000,
        min_margin_threshold=3.0,
    )
    db_session.add(config)
    db_session.commit()
    return o


@pytest.fixture()
def org_config(db_session, org):
    """Return the OrgConfig for the test org."""
    return db_session.query(OrgConfig).filter(
        OrgConfig.organization_id == org.id
    ).first()


@pytest.fixture()
def car_model(db_session, org):
    """Create a test car model with pricing."""
    car = CarModel(
        organization_id=org.id,
        variant="pulsar ns200",
        ex_showroom_price=150000,
        total_5yr=170000,
        total_15yr=175000,
        total_bh=180000,
        upload_batch="test-batch-001",
        is_active=True,
    )
    db_session.add(car)
    db_session.commit()
    return car


@pytest.fixture()
def second_car(db_session, org):
    """Create a second car model for variant tests."""
    car = CarModel(
        organization_id=org.id,
        variant="dominar 400",
        ex_showroom_price=220000,
        total_5yr=250000,
        total_15yr=260000,
        total_bh=270000,
        upload_batch="test-batch-001",
        is_active=True,
    )
    db_session.add(car)
    db_session.commit()
    return car


# ─────────────────────────────────────────────────────────────────────────────
# API client fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def client(db_session, org, car_model):
    """
    FastAPI test client with DB override and test org seeded.
    Uses ADMIN role by default for maximum access.
    """
    from fastapi.testclient import TestClient

    def _override_db():
        session = TestSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_db
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()


def make_headers(org_id: str = "test-org-001", role: str = "ADMIN"):
    """Build auth headers for API requests."""
    return {
        "X-Org-Id": org_id,
        "X-Role": role,
    }
