"""Request/response models for `/api/v1/patient_registry`."""

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

AgeUnit = Literal["Y", "M", "D"]
Gender = Literal["Male", "Female", "Other"]
Resolution = Literal["existing", "incoming", "both"]


class _EntryInBase(BaseModel):
    """Editable standard fields only (PRD section 8). bill_no / net_amount / margin are never accepted."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    patient_name: str | None = Field(None, max_length=120)
    registration_date: date | None = None
    age: int | None = Field(None, ge=0, le=130)
    age_unit: AgeUnit | None = None
    gender: Gender | None = None
    lab_pid: str | None = Field(None, max_length=40)
    referred_by: str | None = Field(None, max_length=120)
    collection_time: datetime | None = None
    reporting_time: datetime | None = None
    tests: list[str] | None = Field(None, max_length=60)
    phone: str | None = Field(None, max_length=30)
    email: str | None = Field(None, max_length=120)
    lab_cost: float | None = Field(None, ge=0)
    gross_amount: float | None = Field(None, ge=0)
    discount_amount: float | None = Field(None, ge=0)


class EntryCreateIn(_EntryInBase):
    partner_key: str
    patient_name: str = Field(min_length=1, max_length=120)
    registration_date: date
    report_id: str | None = Field(None, description="Report stored by /entries/parse, to link to the entry")
    target_entry_id: str | None = Field(None, description="Entry chosen from a possible-match list")
    force_new: bool = Field(False, description="'This is a different patient'")
    resolutions: dict[str, Resolution] = Field(default_factory=dict, description="Per-field choice on conflicts")


class EntryPatchIn(_EntryInBase):
    expected_version: int | None = None


class FieldOut(BaseModel):
    key: str
    label: str
    type: str
    options: list[str] | None = None
    required: bool = False
    default: int | float | str | None = None
    auto: bool = False
    computed: bool = False
    internal: bool = False


class EntrySummaryOut(BaseModel):
    id: str
    bill_no: str | None
    status: str
    partner_key: str
    patient_name: str
    age: int | None = None
    age_unit: AgeUnit | None = None
    gender: Gender | None = None
    lab_pid: str | None = None
    referred_by: str | None = None
    registration_date: date
    tests: list[str] = []
    net_amount: float | None = None


class EntryOut(EntrySummaryOut):
    version: int
    partner_name: str
    collection_time: datetime | None = None
    reporting_time: datetime | None = None
    phone: str | None = None
    email: str | None = None
    lab_cost: float | None = None
    gross_amount: float | None = None
    discount_amount: float | None = None
    margin: float | None = Field(None, description="Admin only; null for everyone else")
    customer_id: str | None = None
    created_at: datetime
    updated_at: datetime


class ReportRefOut(BaseModel):
    id: str
    filename: str
    content_type: str
    size: int
    status: str
    created_at: datetime


class EntryDetailOut(EntryOut):
    reports: list[ReportRefOut] = []


class EntryListOut(BaseModel):
    items: list[EntrySummaryOut]
    total: int
    page: int
    page_size: int


class SaveOut(BaseModel):
    outcome: Literal["created", "merged", "unchanged"]
    entry: EntryOut
    filled_fields: list[str] = []
    report_link: Literal["linked", "already_linked", "missing", "none"] = "none"


class FieldConflictOut(BaseModel):
    field: str
    label: str
    existing: Any
    incoming: Any


class DuplicateDecisionOut(BaseModel):
    """409 body: staff must choose before anything is saved (PRD 6.2)."""

    outcome: Literal["possible_match", "conflict"]
    message: str
    match: EntrySummaryOut | None = None
    possible_matches: list[EntrySummaryOut] = []
    conflicts: list[FieldConflictOut] = []
    can_force_new: bool = True


class ParsedFieldsOut(BaseModel):
    patient_name: str | None = None
    age: int | None = None
    age_unit: AgeUnit | None = None
    gender: Gender | None = None
    lab_pid: str | None = None
    referred_by: str | None = None
    registration_date: date | None = None
    collection_time: datetime | None = None
    reporting_time: datetime | None = None
    tests: list[str] = []
    phone: str | None = None
    email: str | None = None


class DuplicateHintOut(BaseModel):
    kind: Literal["exact", "possible"]
    entries: list[EntrySummaryOut]


class ParseOut(BaseModel):
    report_id: str
    filename: str
    partner_key: str
    file_duplicate: bool = Field(False, description="This exact file was uploaded before")
    existing_entry_id: str | None = None
    fields: ParsedFieldsOut
    confidence: float
    needs_verify: list[str] = Field([], description="Fields to mark with the amber 'Please verify' icon")
    missing_fields: list[str] = []
    flags: list[str] = []
    warnings: list[str] = []
    duplicate: DuplicateHintOut | None = None


class CustomerOut(BaseModel):
    id: str
    name: str
    age: int | None = None
    age_unit: AgeUnit | None = None
    gender: Gender | None = None
    phone: str | None = None
    email: str | None = None
    created_at: datetime


class CustomerListOut(BaseModel):
    items: list[CustomerOut]
    total: int
    page: int
    page_size: int


class CustomerDetailOut(CustomerOut):
    entries: list[EntrySummaryOut] = []


class WorkbookOut(BaseModel):
    year: int
    filename: str
    version: int
    size: int
    last_sent_month: str | None = None
    updated_at: datetime | None = None


class SendOut(BaseModel):
    sent: int


class JobOut(BaseModel):
    status: str
    month: str
    sent: int = 0
    failed: int = 0