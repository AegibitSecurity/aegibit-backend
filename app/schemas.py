"""
Pydantic schemas for request / response validation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, List, Dict

from pydantic import BaseModel, validator, model_validator, EmailStr


# ── Auth ──────────────────────────────────────────────────────────────────────

class UserResponse(BaseModel):
    id: str
    email: str
    role: str
    organization_id: str
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class LoginRequest(BaseModel):
    email: EmailStr
    password: str

    @validator('password')
    def password_not_empty(cls, v):
        if not v or len(v.strip()) == 0:
            raise ValueError('Password is required')
        return v


class LoginResponse(BaseModel):
    user: UserResponse
    access_token: Optional[str] = None  # native/mobile clients only — web uses cookie


class CreateUserRequest(BaseModel):
    email: EmailStr
    password: str
    role: str  # GM | DIRECTOR | SALES (never ADMIN)
    organization_id: str

    @validator('role')
    def validate_role(cls, v):
        allowed = ["GM", "DIRECTOR", "SALES"]
        normalized = v.strip().upper()
        if normalized not in allowed:
            raise ValueError(f"Role must be one of {allowed}. ADMIN cannot be created via API.")
        return normalized

    @validator('password')
    def validate_password(cls, v):
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        if len(v) > 128:
            raise ValueError("Password must be 128 characters or fewer")
        return v


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

    @validator('new_password')
    def validate_new_password(cls, v):
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


# ── Deal Analyze (pre-submission preview) ────────────────────────────────────

class DealAnalyzeRequest(BaseModel):
    variant: str
    registration_type: str = "INDIVIDUAL"
    discount: float = 0


class DealAnalyzeResponse(BaseModel):
    variant: str
    base_price: float
    discount: float
    final_price: float
    margin: float
    margin_percent: float
    risk_level: str
    decision: str
    reason: str


# ── Deal Create ──────────────────────────────────────────────────────────────

class DealCreateRequest(BaseModel):
    # CUSTOMER DETAILS - MANDATORY
    customer_name: str
    mobile: str  # mobile/phone number - MANDATORY
    customer_email: EmailStr  # MANDATORY - for OTP verification
    phone: Optional[str] = None  # alternative phone number
    father_name: str  # MANDATORY
    address: str  # MANDATORY
    aadhaar: str  # MANDATORY
    voter_id: str  # MANDATORY
    pan: str  # MANDATORY

    # VEHICLE DETAILS - MANDATORY
    model: str  # vehicle model - MANDATORY
    colour: str  # MANDATORY
    chassis_no: str  # MANDATORY
    engine_no: str  # MANDATORY

    # DELIVERY / CRM DETAILS - MANDATORY
    delivery_date: datetime  # MANDATORY
    crm_date: datetime  # MANDATORY
    crm_invoice_no: str  # MANDATORY
    crm_esp: float  # MANDATORY
    rse_name: str  # Retail Sales Executive - MANDATORY
    sm_name: str  # Sales Manager - MANDATORY

    # FINANCE DETAILS - MANDATORY
    sale_type: str  # CASH / FINANCE - MANDATORY
    financer_name: Optional[str] = None  # Required if FINANCE
    financer_branch: Optional[str] = None  # Required if FINANCE
    inhouse_finance: str  # YES / NO - MANDATORY

    # META - MANDATORY
    rto_code: str  # MANDATORY - RTO code (e.g., WB-09)
    rto_name: str  # MANDATORY - RTO name (e.g., Berhampore)
    rto_district: str  # MANDATORY - RTO district (e.g., Murshidabad)
    branch: str  # MANDATORY

    # Deal fields
    variant: str  # MANDATORY
    registration_type: str = "INDIVIDUAL"
    discount: float = 0

    # Pricing engine fields (optional - backward compatible)
    road_tax: Optional[float] = None
    insurance: Optional[float] = None
    corporate_discount: Optional[float] = None
    festival_discount: Optional[float] = None
    adjustments: Optional[float] = None

    @validator('mobile')
    def validate_mobile(cls, v):
        if not v or len(v) != 10 or not v[0] in '6789':
            raise ValueError('Invalid Indian mobile number format')
        return v

    @validator('aadhaar')
    def validate_aadhaar(cls, v):
        if not v or len(v) != 12 or not v.isdigit():
            raise ValueError('Aadhaar must be exactly 12 digits')
        return v

    @validator('pan')
    def validate_pan(cls, v):
        if not v or len(v) != 10:
            raise ValueError('PAN must be exactly 10 characters')
        if not (v[:5].isalpha() and v[5:9].isdigit() and v[9].isalpha()):
            raise ValueError('Invalid PAN format')
        return v.upper()

    @validator('chassis_no')
    def validate_chassis_no(cls, v):
        if not v or len(v.strip()) < 5:
            raise ValueError('Chassis number too short')
        return v.strip().upper()

    @validator('engine_no')
    def validate_engine_no(cls, v):
        if not v or len(v.strip()) == 0:
            raise ValueError('Engine number is required')
        return v.strip().upper()

    @validator('sale_type')
    def validate_sale_type(cls, v):
        if v not in ['CASH', 'FINANCE']:
            raise ValueError('Sale type must be CASH or FINANCE')
        return v

    @validator('inhouse_finance')
    def validate_inhouse_finance(cls, v):
        if v not in ['YES', 'NO']:
            raise ValueError('Inhouse finance must be YES or NO')
        return v

    @validator('financer_name')
    def validate_financer_name(cls, v, values):
        if values.get('sale_type') == 'FINANCE' and (not v or len(v.strip()) == 0):
            raise ValueError('Financer name is required for FINANCE deals')
        return v.strip() if v else v

    @validator('financer_branch')
    def validate_financer_branch(cls, v, values):
        if values.get('sale_type') == 'FINANCE' and (not v or len(v.strip()) == 0):
            raise ValueError('Financer branch is required for FINANCE deals')
        return v.strip() if v else v


class DealResponse(BaseModel):
    id: str
    organization_id: str
    customer_id: Optional[str] = None
    salesperson_id: Optional[str] = None
    customer_name: str
    customer_phone: Optional[str] = None  # UNIQUE - customer's verified phone (nullable for legacy)
    customer_email: Optional[str] = None  # Email used for OTP
    phone_verified: bool = False  # Email OTP verification status
    phone: Optional[str] = None
    mobile: Optional[str] = None  # mobile/phone number
    father_name: Optional[str] = None
    address: Optional[str] = None
    aadhaar: Optional[str] = None
    pan: Optional[str] = None
    voter_id: Optional[str] = None
    rse_name: Optional[str] = None       # Retail Sales Executive
    sm_name: Optional[str] = None        # Sales Manager
    model: Optional[str] = None  # vehicle model
    variant: str
    colour: Optional[str] = None
    chassis_no: Optional[str] = None
    engine_no: Optional[str] = None
    registration_type: str
    sale_type: Optional[str] = None  # CASH / FINANCE
    financer_name: Optional[str] = None
    financer_branch: Optional[str] = None
    inhouse_finance: Optional[str] = None  # YES / NO
    delivery_date: Optional[datetime] = None
    crm_date: Optional[datetime] = None
    crm_invoice_no: Optional[str] = None
    crm_esp: Optional[float] = None
    base_price: float
    discount: float
    final_price: float
    margin: float
    margin_percent: float
    status: str
    approval_stage: Optional[str] = None
    risk_level: str
    decision: str
    reason: Optional[str] = None
    pricing_breakdown: Optional[dict] = None
    created_at: datetime

    # ── Soft Delete Fields ──────────────────────────────────────────────────
    is_deleted: bool = False
    deleted_at: Optional[datetime] = None
    deleted_by: Optional[str] = None

    @validator('pricing_breakdown', pre=True)
    def validate_pricing_breakdown(cls, v):
        # Handle database storing 'null' as string instead of proper null
        if v == 'null' or v == 'None' or v is None:
            return None
        return v

    class Config:
        from_attributes = True


# ── Approval ─────────────────────────────────────────────────────────────────

class ApprovalRequest(BaseModel):
    deal_id: str


# ── Tasks ────────────────────────────────────────────────────────────────────

class TaskResponse(BaseModel):
    id: str
    deal_id: str
    assigned_to_role: str
    status: str
    created_at: datetime
    completed_at: Optional[datetime] = None
    deal: Optional[DealResponse] = None

    class Config:
        from_attributes = True


# ── Notifications ────────────────────────────────────────────────────────────

class NotificationResponse(BaseModel):
    id: str
    organization_id: str
    event_type: str
    payload: Optional[dict] = None
    status: str
    created_at: datetime

    class Config:
        from_attributes = True


# ── Dashboard ────────────────────────────────────────────────────────────────

class DashboardSummary(BaseModel):
    total_deals: int
    approved: int
    pending: int
    rejected: int
    avg_margin: float
    high_risk: int
    revenue_today: float
    pending_gm_tasks: int
    pending_director_tasks: int


# ── Upload ───────────────────────────────────────────────────────────────────

class ValidationErrorResponse(BaseModel):
    error: str
    missing_fields: List[str] = []
    invalid_fields: Dict[str, str] = {}


class UploadResponse(BaseModel):
    rows_inserted: int
    rows_skipped: int
    errors: list[str]
    upload_batch: str


# ── Variants ─────────────────────────────────────────────────────────────────

class VariantOption(BaseModel):
    variant: str
    ex_showroom_price: float
    total_5yr: Optional[float] = None
    total_15yr: Optional[float] = None
    total_bh: Optional[float] = None


# ── Audit Log ────────────────────────────────────────────────────────────────

class AuditChainVerifyResponse(BaseModel):
    ok: bool
    total: int
    verified: int
    tampered: List[dict] = []
    checked_at: str


class AuditLogResponse(BaseModel):
    id: str
    organization_id: str
    action_type: str
    entity_type: str
    entity_id: str
    performed_by: Optional[str] = None
    performed_by_email: Optional[str] = None
    ip_address: Optional[str] = None
    previous_data: Optional[dict] = None
    new_data: Optional[dict] = None
    note: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


# ── Deal Events (Audit) ───────────────────────────────────────────────────────

class DealEventResponse(BaseModel):
    id: str
    deal_id: str
    event_type: str
    actor_role: Optional[str] = None
    metadata_: Optional[dict] = None
    created_at: datetime

    class Config:
        from_attributes = True


class DealDetailResponse(DealResponse):
    """Full deal with embedded audit trail."""
    events: list[DealEventResponse] = []


# ── Phone Verification ───────────────────────────────────────────────────────

class SendOtpRequest(BaseModel):
    phone_number: str

    @validator('phone_number')
    def validate_phone(cls, v):
        # Remove all non-numeric characters
        cleaned = ''.join(c for c in v if c.isdigit())
        # Remove country code if present (keep last 10 digits)
        if len(cleaned) > 10:
            cleaned = cleaned[-10:]
        if len(cleaned) != 10:
            raise ValueError('Phone number must be exactly 10 digits')
        if cleaned[0] not in '6789':
            raise ValueError('Invalid Indian mobile number format')
        return cleaned


class SendOtpResponse(BaseModel):
    message: str
    expires_in_seconds: int = 300
    masked_phone: str


class VerifyOtpRequest(BaseModel):
    phone_number: str
    otp: str

    @validator('phone_number')
    def validate_phone(cls, v):
        cleaned = ''.join(c for c in v if c.isdigit())
        if len(cleaned) > 10:
            cleaned = cleaned[-10:]
        return cleaned

    @validator('otp')
    def validate_otp(cls, v):
        if not v or len(v) != 6 or not v.isdigit():
            raise ValueError('OTP must be exactly 6 digits')
        return v


class VerifyOtpResponse(BaseModel):
    verified: bool
    message: str
    customer_id: Optional[str] = None
    is_new_customer: bool = False


# ── Customer Check ───────────────────────────────────────────────────────────

class RecentDealSummary(BaseModel):
    id: str
    status: str
    final_price: float
    created_at: datetime
    variant: str


class CheckCustomerResponse(BaseModel):
    exists: bool
    customer_id: Optional[str] = None
    is_verified: bool = False
    name: Optional[str] = None
    deal_count: int = 0
    recent_deals: list[RecentDealSummary] = []
    warning_message: Optional[str] = None


# ── Fraud Detection ──────────────────────────────────────────────────────────

class FraudCheckResult(BaseModel):
    allowed: bool
    reason: Optional[str] = None
    daily_deals_for_phone: int = 0
    hourly_deals_for_salesperson: int = 0
    max_daily_deals_per_phone: int = 3
    max_hourly_deals_per_salesperson: int = 10


class FraudAuditEntry(BaseModel):
    id: str
    event_type: str
    phone_number: str
    details: Optional[dict] = None
    created_at: datetime

    class Config:
        from_attributes = True


# ── Email OTP Verification ───────────────────────────────────────────────────

class SendEmailOtpRequest(BaseModel):
    email: str
    phone: str

    @validator('email')
    def validate_email(cls, v):
        if not v or '@' not in v:
            raise ValueError('Invalid email format')
        return v.lower().strip()

    @validator('phone')
    def validate_phone(cls, v):
        if not v or len(v) != 10 or not v.isdigit() or v[0] not in '6789':
            raise ValueError('Invalid Indian mobile number format')
        return v


class SendEmailOtpResponse(BaseModel):
    success: bool
    message: str
    email_masked: str  # user@example.com → u***@example.com
    expires_in_seconds: int = 300  # 5 minutes
    rate_limit_seconds: Optional[int] = None  # If rate limited


class VerifyEmailOtpRequest(BaseModel):
    email: str
    phone: str
    otp: str

    @validator('otp')
    def validate_otp(cls, v):
        if not v or len(v) != 6 or not v.isdigit():
            raise ValueError('OTP must be 6 digits')
        return v


class VerifyEmailOtpResponse(BaseModel):
    success: bool
    verified: bool
    message: str
    phone: str
    remaining_attempts: Optional[int] = None


class CheckPhoneResponse(BaseModel):
    phone: str
    exists: bool
    has_deal: bool  # True if phone already has a deal
    verified: bool  # If there's a verified OTP entry
    message: Optional[str] = None
