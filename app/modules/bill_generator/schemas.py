"""Request/response models for `/api/v1/bill_generator`. Money is Decimal (serialised as a string, never float)."""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

AgeUnit = Literal["Y", "M", "D"]
Gender = Literal["Male", "Female", "Other"]
Resolution = Literal["existing", "incoming", "both"]
Delivery = Literal["preview", "download", "email"]
Money = Annotated[Decimal, Field(ge=0, le=Decimal("10000000"), max_digits=10, decimal_places=2)]


class LabOut(BaseModel):
    key: str
    name: str
    mode: Literal["generate", "edit_pdf"]


class BillDataIn(BaseModel):
    """What the bill page saves on the entry (PRD 10.2). Send null to clear a field."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    tests: list[str] | None = Field(None, max_length=60)
    test_amounts: list[Money] | None = Field(None, max_length=60)
    discount_amount: Money | None = None
    lab_cost: Money | None = None
    client_name: str | None = Field(None, max_length=120)


class BillDataPatchIn(BillDataIn):
    expected_version: int | None = None


class ManualEntryIn(BillDataIn):
    """New entry from the bill page (Spectrum): the duplicate check runs first."""

    partner_key: str
    patient_name: str = Field(min_length=1, max_length=120)
    registration_date: date
    age: int | None = Field(None, ge=0, le=130)
    age_unit: AgeUnit | None = None
    gender: Gender | None = None
    lab_pid: str | None = Field(None, max_length=40)
    referred_by: str | None = Field(None, max_length=120)
    phone: str | None = Field(None, max_length=30)
    email: str | None = Field(None, max_length=120)
    target_entry_id: str | None = None
    force_new: bool = False
    resolutions: dict[str, Resolution] = Field(default_factory=dict)


class EntryBillOut(BaseModel):
    id: str
    bill_no: str | None
    bill_no_original: str | None = None
    status: str
    version: int
    partner_key: str
    partner_name: str
    patient_name: str
    age: int | None = None
    age_unit: AgeUnit | None = None
    gender: Gender | None = None
    lab_pid: str | None = None
    referred_by: str | None = None
    registration_date: date
    tests: list[str] = []
    test_amounts: list[Decimal] = []
    gross_amount: Decimal | None = None
    discount_amount: Decimal | None = None
    net_amount: Decimal | None = None
    lab_cost: Decimal | None = None
    margin: Decimal | None = Field(None, description="Admin only; null for everyone else")
    client_name: str | None = None
    updated_at: datetime


class SaveOut(BaseModel):
    outcome: Literal["created", "merged", "unchanged"]
    entry: EntryBillOut
    filled_fields: list[str] = []


class SentOut(BaseModel):
    sent: bool = True


class ParsedOut(BaseModel):
    """A Spectrum report read in memory. Nothing is stored; use the fields to start a bill."""

    partner_key: str
    fields: dict[str, Any]
    duplicate_kind: Literal["exact", "possible"] | None = None
    duplicate_entry_ids: list[str] = []


class SpectrumRenderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_id: str
    include_client: bool = False
    delivery: Delivery = "preview"


class LabRowOut(BaseModel):
    name: str
    printed_amount: Decimal
    amount: Decimal = Field(description="Prefilled from the entry where names match, else the printed amount")


class MatchOut(BaseModel):
    kind: Literal["exact", "possible", "none"]
    entries: list[dict[str, Any]] = []


class LabInspectOut(BaseModel):
    lab_bill_no: str
    lab_pid: str
    bill_date: date
    patient_name: str
    age: int | None = None
    age_unit: AgeUnit | None = None
    gender: Gender | None = None
    referred_by: str | None = None
    rows: list[LabRowOut]
    printed_gross: Decimal
    printed_discount: Decimal
    printed_grand: Decimal
    entry_id: str | None = None
    entry_gross: Decimal | None = None
    entry_discount: Decimal | None = None
    entry_lab_cost: Decimal | None = None
    match: MatchOut
    bill_no_taken: bool = False