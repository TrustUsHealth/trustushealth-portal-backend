"""Request/response models for the Spectrum sync endpoints."""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class SyncRunIn(BaseModel):
    """Give a `month` (YYYY-MM) OR `date_from` + `date_to`. Neither = the daily window (today and the lookback days)."""

    model_config = ConfigDict(extra="forbid")

    month: str | None = Field(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$", examples=["2026-10"])
    date_from: date | None = None
    date_to: date | None = None
    dry_run: bool = Field(False, description="Only count what would be processed; nothing is saved")
    retry_failed: bool = Field(False, description="Also retry rows that already used all their attempts")


class SyncRunStartedOut(BaseModel):
    run_id: str
    status: str = "started"
    date_from: date
    date_to: date
    dry_run: bool = False


class SyncStatsOut(BaseModel):
    windows: int = 0
    rows_seen: int = 0
    already_done: int = 0
    exhausted: int = 0
    skipped_existing_entry: int = 0
    would_process: int = 0
    attempted: int = 0
    created: int = 0
    merged: int = 0
    unchanged: int = 0
    existing_conflict: int = 0
    needs_review: int = 0
    failed: int = 0


class SyncRunOut(BaseModel):
    id: str
    trigger: str
    status: str
    dry_run: bool = False
    date_from: date
    date_to: date
    started_at: datetime
    finished_at: datetime | None = None
    message: str | None = None
    stats: SyncStatsOut


class SyncItemOut(BaseModel):
    source_key: str
    sid_no: str
    sid_date: date
    status: str
    outcome: str | None = None
    attempts: int = 0
    entry_id: str | None = None
    report_ref: str | None = None
    last_error: str | None = None
    warnings: list[str] = []
    updated_at: datetime | None = None


class SyncItemListOut(BaseModel):
    items: list[SyncItemOut]
    total: int