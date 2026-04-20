"""
SQLAlchemy ORM models for the AEGIBIT Flow database.

Tables
------
organizations       Multi-tenant org container
org_config           Per-org approval thresholds
car_models           Pricing data ingested from Excel
deals                Core deal records with margin / risk / decision
tasks                Approval tasks routed to GM / Director
notifications        Real-time event notifications
deal_events          Immutable audit log for every deal action
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    String,
    Float,
    DateTime,
    Boolean,
    ForeignKey,
    Text,
    JSON,
    Index,
    Integer,
    UniqueConstraint,
)
# Using generic types for SQLite compatibility (no PostgreSQL dialect needed)
from sqlalchemy.orm import relationship

from app.database import Base


def _uuid():
    return str(uuid.uuid4())


def _utcnow():
    return datetime.now(timezone.utc)


# ── Users ────────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        Index("ix_user_org_role", "organization_id", "role"),
        Index("ix_user_email", "email", unique=True),
    )

    id = Column(String(36), primary_key=True, default=_uuid)
    email = Column(String(255), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), nullable=False)  # ADMIN | GM | DIRECTOR | SALES
    organization_id = Column(
        String(36), ForeignKey("organizations.id"), nullable=False
    )
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=_utcnow)

    organization = relationship("Organization", backref="users")


# ── Organizations ────────────────────────────────────────────────────────────

class Organization(Base):
    __tablename__ = "organizations"

    id = Column(String(36), primary_key=True, default=_uuid)
    name = Column(String(255), unique=True, nullable=False)
    created_at = Column(DateTime, default=_utcnow)

    config = relationship("OrgConfig", back_populates="organization", uselist=False)
    deals = relationship("Deal", back_populates="organization")
    car_models = relationship("CarModel", back_populates="organization")
    notifications = relationship("Notification", back_populates="organization")


# ── Org Config ───────────────────────────────────────────────────────────────

class OrgConfig(Base):
    __tablename__ = "org_config"

    id = Column(String(36), primary_key=True, default=_uuid)
    organization_id = Column(
        String(36), ForeignKey("organizations.id"), unique=True, nullable=False
    )
    gm_discount_limit = Column(Float, nullable=False, default=5000)
    director_discount_limit = Column(Float, nullable=False, default=15000)
    min_margin_threshold = Column(Float, nullable=False, default=3.0)  # percent

    organization = relationship("Organization", back_populates="config")


# ── Car Models (Pricing) ────────────────────────────────────────────────────

class CarModel(Base):
    __tablename__ = "car_models"
    __table_args__ = (
        Index("ix_car_org_variant_active", "organization_id", "variant", "is_active"),
        Index("ix_car_org_batch", "organization_id", "upload_batch"),
    )

    id = Column(String(36), primary_key=True, default=_uuid)
    organization_id = Column(
        String(36), ForeignKey("organizations.id"), nullable=False
    )
    variant = Column(String(512), nullable=False)
    model_name = Column(String(255), nullable=True)  # derived from variant prefix
    ex_showroom_price = Column(Float, nullable=False)
    total_5yr = Column(Float, nullable=True)
    total_15yr = Column(Float, nullable=True)
    total_bh = Column(Float, nullable=True)
    upload_batch = Column(String(36), nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=_utcnow)

    organization = relationship("Organization", back_populates="car_models")


# ── Deals ────────────────────────────────────────────────────────────────────

class Deal(Base):
    __tablename__ = "deals"
    __table_args__ = (
        Index("ix_deal_org_status", "organization_id", "status"),
        Index("ix_deal_org_created", "organization_id", "created_at"),
        Index("ix_deal_customer", "customer_id"),
        Index("ix_deal_is_deleted", "is_deleted"),
        UniqueConstraint("customer_phone", name="uq_deal_customer_phone"),
    )

    id = Column(String(36), primary_key=True, default=_uuid)
    organization_id = Column(
        String(36), ForeignKey("organizations.id"), nullable=False
    )
    customer_id = Column(
        String(36), ForeignKey("customers.id"), nullable=True
    )
    # salesperson_id tracks which user created the deal
    salesperson_id = Column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    customer_name = Column(String(255), nullable=False)
    customer_phone = Column(String(15), nullable=True)  # UNIQUE - customer's phone (nullable for legacy)
    phone_verified = Column(Boolean, default=False)  # Email OTP verification status
    customer_email = Column(String(255), nullable=True)  # For OTP delivery
    phone = Column(String(20), nullable=True)  # mobile/phone number (legacy)
    mobile = Column(String(20), nullable=True)  # alias for phone
    father_name = Column(String(255), nullable=True)
    address = Column(Text, nullable=True)
    aadhaar = Column(String(12), nullable=True)
    pan = Column(String(10), nullable=True)
    voter_id = Column(String(20), nullable=True)
    rse_name = Column(String(255), nullable=True)       # Retail Sales Executive
    sm_name = Column(String(255), nullable=True)         # Sales Manager

    # Deal/CRM fields
    delivery_date = Column(DateTime, nullable=True)
    crm_date = Column(DateTime, nullable=True)
    crm_invoice_no = Column(String(100), nullable=True)
    crm_esp = Column(Float, nullable=True)

    variant = Column(String(512), nullable=False)

    # Vehicle fields
    model = Column(String(255), nullable=False)  # required field
    colour = Column(String(100), nullable=True)
    chassis_no = Column(String(50), nullable=True)
    engine_no = Column(String(50), nullable=True)

    # Finance fields
    sale_type = Column(String(20), nullable=False, default="CASH")  # CASH / FINANCE
    financer_name = Column(String(255), nullable=True)
    financer_branch = Column(String(255), nullable=True)
    inhouse_finance = Column(String(3), nullable=False, default="NO")  # YES / NO

    registration_type = Column(String(50), nullable=False, default="INDIVIDUAL")
    base_price = Column(Float, nullable=False)
    discount = Column(Float, nullable=False, default=0)
    final_price = Column(Float, nullable=False)
    margin = Column(Float, nullable=False)
    margin_percent = Column(Float, nullable=False)
    status = Column(String(20), nullable=False, default="PENDING")       # PENDING | APPROVED | REJECTED
    approval_stage = Column(String(20), nullable=True)                    # GM | DIRECTOR | DONE
    risk_level = Column(String(10), nullable=False, default="LOW")       # LOW | MEDIUM | HIGH
    decision = Column(String(30), nullable=False)                         # AUTO_APPROVE | GM_APPROVAL | DIRECTOR_APPROVAL
    reason = Column(Text, nullable=True)
    pricing_breakdown = Column(JSON, nullable=True)  # detailed price line items
    created_at = Column(DateTime, default=_utcnow)

    # ── Soft Delete Fields ────────────────────────────────────────────────────
    is_deleted = Column(Boolean, default=False, nullable=False)
    deleted_at = Column(DateTime, nullable=True)
    deleted_by = Column(String(36), ForeignKey("users.id"), nullable=True)

    organization = relationship("Organization", back_populates="deals")
    customer = relationship("Customer", back_populates="deals")
    tasks = relationship("Task", back_populates="deal")
    events = relationship("DealEvent", back_populates="deal", order_by="DealEvent.created_at")


# ── Tasks ────────────────────────────────────────────────────────────────────

class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_task_deal_role_status", "deal_id", "assigned_to_role", "status"),
    )

    id = Column(String(36), primary_key=True, default=_uuid)
    deal_id = Column(String(36), ForeignKey("deals.id"), nullable=False)
    assigned_to_role = Column(String(20), nullable=False)  # GM | DIRECTOR
    status = Column(String(20), nullable=False, default="PENDING")  # PENDING | COMPLETED
    created_at = Column(DateTime, default=_utcnow)
    completed_at = Column(DateTime, nullable=True)

    deal = relationship("Deal", back_populates="tasks")


# ── Notifications ────────────────────────────────────────────────────────────

class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notif_org_status", "organization_id", "status"),
    )

    id = Column(String(36), primary_key=True, default=_uuid)
    organization_id = Column(
        String(36), ForeignKey("organizations.id"), nullable=False
    )
    event_type = Column(String(50), nullable=False)
    payload = Column(JSON, nullable=True)
    status = Column(String(20), nullable=False, default="UNREAD")  # UNREAD | READ
    created_at = Column(DateTime, default=_utcnow)

    organization = relationship("Organization", back_populates="notifications")


# ── Deal Events (Audit Log) ─────────────────────────────────────────────────

class DealEvent(Base):
    __tablename__ = "deal_events"
    __table_args__ = (
        Index("ix_event_deal", "deal_id"),
    )

    id = Column(String(36), primary_key=True, default=_uuid)
    deal_id = Column(String(36), ForeignKey("deals.id"), nullable=False)
    event_type = Column(String(50), nullable=False)
    actor_role = Column(String(20), nullable=True)
    metadata_ = Column("metadata", JSON, nullable=True)
    created_at = Column(DateTime, default=_utcnow)

    deal = relationship("Deal", back_populates="events")


# ── Audit Log (org-wide, all entities) ──────────────────────────────────────

class AuditLog(Base):
    """
    Immutable org-wide audit log.
    Every CREATE / UPDATE / DELETE / RESTORE / APPROVE / REJECT action is written
    here — independently of DealEvent — so nothing can be silently erased.
    """
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_org_created",   "organization_id", "created_at"),
        Index("ix_audit_entity",        "entity_type", "entity_id"),
        Index("ix_audit_performed_by",  "performed_by"),
        Index("ix_audit_action",        "action_type"),
    )

    id                  = Column(String(36), primary_key=True, default=_uuid)
    organization_id     = Column(String(36), ForeignKey("organizations.id"), nullable=False)
    action_type         = Column(String(50), nullable=False)   # CREATE DELETE RESTORE APPROVE REJECT UPDATE
    entity_type         = Column(String(50), nullable=False)   # deal pricing user
    entity_id           = Column(String(36), nullable=False)
    performed_by        = Column(String(36), ForeignKey("users.id"), nullable=True)
    performed_by_email  = Column(String(255), nullable=True)   # denormalized — survives user deletion
    ip_address          = Column(String(45), nullable=True)    # IPv6-safe
    previous_data       = Column(JSON, nullable=True)
    new_data            = Column(JSON, nullable=True)
    note                = Column(Text, nullable=True)
    created_at          = Column(DateTime, default=_utcnow)


# ── Customers ───────────────────────────────────────────────────────────────

class Customer(Base):
    """Verified customers — tied to phone numbers for fraud prevention."""
    __tablename__ = "customers"
    __table_args__ = (
        Index("ix_customer_phone", "phone_number", unique=True),
        Index("ix_customer_org", "organization_id"),
    )

    id = Column(String(36), primary_key=True, default=_uuid)
    organization_id = Column(
        String(36), ForeignKey("organizations.id"), nullable=False
    )
    phone_number = Column(String(15), nullable=False)  # Normalized: 10 digits
    name = Column(String(255), nullable=True)
    is_verified = Column(Boolean, default=False)
    verification_count = Column(Integer, default=0)  # Times verified
    created_at = Column(DateTime, default=_utcnow)
    verified_at = Column(DateTime, nullable=True)

    organization = relationship("Organization", backref="customers")
    deals = relationship("Deal", back_populates="customer")


# ── Phone Verifications ─────────────────────────────────────────────────────

class PhoneVerification(Base):
    """OTP records for phone verification with rate limiting."""
    __tablename__ = "phone_verifications"
    __table_args__ = (
        Index("ix_phone_ver_phone", "phone_number"),
        Index("ix_phone_ver_expiry", "expires_at"),
    )

    id = Column(String(36), primary_key=True, default=_uuid)
    phone_number = Column(String(15), nullable=False)
    otp = Column(String(6), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    attempts = Column(Integer, default=0)
    max_attempts = Column(Integer, default=3)
    verified = Column(Boolean, default=False)
    created_at = Column(DateTime, default=_utcnow)


# ── Fraud Audit Log ──────────────────────────────────────────────────────────

class FraudAuditLog(Base):
    """Immutable audit log for fraud detection events."""
    __tablename__ = "fraud_audit_logs"
    __table_args__ = (
        Index("ix_fraud_org", "organization_id"),
        Index("ix_fraud_phone", "phone_number"),
        Index("ix_fraud_salesperson", "salesperson_id"),
        Index("ix_fraud_created", "created_at"),
    )

    id = Column(String(36), primary_key=True, default=_uuid)
    organization_id = Column(
        String(36), ForeignKey("organizations.id"), nullable=False
    )
    phone_number = Column(String(15), nullable=False)
    salesperson_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    deal_id = Column(String(36), ForeignKey("deals.id"), nullable=True)
    event_type = Column(String(50), nullable=False)  # OTP_SENT, OTP_VERIFIED, FRAUD_BLOCKED, etc.
    details = Column(JSON, nullable=True)
    ip_address = Column(String(45), nullable=True)  # IPv6 compatible
    created_at = Column(DateTime, default=_utcnow)


# ── Email OTP Verifications ───────────────────────────────────────────────────

class OtpVerification(Base):
    """Email-based OTP for phone verification — prevents duplicate deals."""
    __tablename__ = "otp_verifications"
    __table_args__ = (
        Index("ix_otp_email", "email"),
        Index("ix_otp_phone", "phone"),
        Index("ix_otp_expires", "expires_at"),
    )

    id = Column(String(36), primary_key=True, default=_uuid)
    email = Column(String(255), nullable=False)      # Where OTP is sent
    phone = Column(String(15), nullable=False)       # Phone being verified
    otp_hash = Column(String(255), nullable=False)   # Bcrypt hashed OTP
    expires_at = Column(DateTime, nullable=False)   # 5 minutes expiry
    verified = Column(Boolean, default=False)       # True = used/consumed (includes invalidated ones)
    legitimately_verified = Column(Boolean, default=False)  # True ONLY when user entered the correct OTP
    attempts = Column(Integer, default=0)             # Failed attempts
    max_attempts = Column(Integer, default=3)         # Max allowed attempts
    created_at = Column(DateTime, default=_utcnow)
    verified_at = Column(DateTime, nullable=True)
