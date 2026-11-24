"""`config/spectrum_sync.yaml` (settings) and the portal credentials (.env).

Validated at startup with plain-language messages, like the other config files. A missing file means "sync off".
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.config import ConfigError
from app.partners.spectrum.portal import PortalSettings

FILE_NAME = "spectrum_sync.yaml"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PortalCfg(_Strict):
    base_url: str = "https://emr.spectrumhealthcare.in/QR_Gershwin/Forms"
    request_timeout_seconds: float = Field(60, ge=5, le=300)
    pdf_timeout_seconds: float = Field(120, ge=10, le=600)
    pause_between_requests_seconds: float = Field(0.3, ge=0, le=10)
    max_retries: int = Field(3, ge=1, le=6)
    verify_ssl: bool = True
    test_status_page: str | None = None  # only if the test list opens at a URL that cannot be detected

    @field_validator("base_url")
    @classmethod
    def _url(cls, v: str) -> str:
        if not v.startswith(("https://", "http://")):
            raise ValueError("base_url must start with https://")
        return v.rstrip("/")


class ScheduleCfg(_Strict):
    enabled: bool = True
    run_time: str = Field("21:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")  # local time (app.yaml timezone)
    catch_up_on_start: bool = True


class DailyRunCfg(_Strict):
    lookback_days: int = Field(7, ge=0, le=90)


class SyncCfg(_Strict):
    enabled: bool = False
    partner_key: str = "spectrum"
    portal: PortalCfg = Field(default_factory=PortalCfg)
    schedule: ScheduleCfg = Field(default_factory=ScheduleCfg)
    daily_run: DailyRunCfg = Field(default_factory=DailyRunCfg)
    window_days: int = Field(1, ge=1, le=31)
    max_attempts: int = Field(8, ge=1, le=50)
    max_entries_per_run: int = Field(400, ge=1, le=5000)
    abort_after_consecutive_failures: int = Field(5, ge=1, le=100)
    skip_if_entry_has_report: bool = True
    create_entry_when_report_unreadable: bool = True
    lock_minutes: int = Field(180, ge=5, le=1440)


class PortalSecrets(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    spectrum_portal_username: str | None = None
    spectrum_portal_password: SecretStr | None = None

    @property
    def configured(self) -> bool:
        return bool(
            self.spectrum_portal_username
            and self.spectrum_portal_password
            and self.spectrum_portal_password.get_secret_value()
        )


def load_sync_config(config_dir: Path | str = "config") -> SyncCfg:
    path = Path(config_dir) / FILE_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return SyncCfg()  # sync stays off
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark else ""
        raise ConfigError([f"{FILE_NAME}: not valid YAML{where}. Check indentation and quotes."]) from None
    if not isinstance(data, dict):
        raise ConfigError([f"{FILE_NAME}: expected a list of settings, found {type(data).__name__}"])
    try:
        return SyncCfg.model_validate(data)
    except ValidationError as exc:
        problems = []
        for err in exc.errors():
            where = " -> ".join(str(p) for p in err["loc"])
            msg = err["msg"].removeprefix("Value error, ")
            if err["type"] == "extra_forbidden":
                msg = "this setting is not recognised (typo?)"
            problems.append(f"{FILE_NAME}: {where}: {msg}" if where else f"{FILE_NAME}: {msg}")
        raise ConfigError(problems) from None


def portal_settings(cfg: SyncCfg, secrets: PortalSecrets) -> PortalSettings:
    if not secrets.configured:
        raise ConfigError(["SPECTRUM_PORTAL_USERNAME and SPECTRUM_PORTAL_PASSWORD must be set in .env"])
    p = cfg.portal
    return PortalSettings(
        base_url=p.base_url,
        username=secrets.spectrum_portal_username or "",
        password=secrets.spectrum_portal_password.get_secret_value() if secrets.spectrum_portal_password else "",
        request_timeout_seconds=p.request_timeout_seconds,
        pdf_timeout_seconds=p.pdf_timeout_seconds,
        pause_between_requests_seconds=p.pause_between_requests_seconds,
        max_retries=p.max_retries,
        verify_ssl=p.verify_ssl,
        test_status_page=p.test_status_page,
    )