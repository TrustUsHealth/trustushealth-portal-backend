"""Configuration: `.env` for secrets (Settings) and `config/*.yaml` for content (PortalConfig).

Everything is validated at startup and in CI. Errors are written in plain language,
for example: "partners/spectrum.yaml: header -> lab_pid: unknown standard field".
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
Role = Literal["admin", "staff", "engineer", "customer"]
LOGIN_ROLES: tuple[str, ...] = ("admin", "staff", "engineer")  # customer has no UI yet
# Header keys a partner file may map that are not standard fields themselves.
COMPOSITE_HEADER_KEYS = {"age_gender": ("age", "age_unit", "gender")}


class ConfigError(Exception):
    """One or more problems in config/, each already phrased for a non-technical editor."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("\n".join(problems))


# ───────────────────────────── .env (secrets & environment) ─────────────────────────────
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    environment: Literal["local", "production", "test"] = "production"  # safe default: Secure cookies, no /docs
    mongodb_uri: str
    mongodb_db: str = "trustushealth"
    jwt_secret: str = Field(min_length=32)
    frontend_origin: str = "http://localhost:3000"
    bootstrap_admin_email: EmailStr | None = None
    config_dir: Path = Path("config")

    mail_transport: Literal["smtp", "gmail"] = "smtp"
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    gmail_address: EmailStr | None = None
    gmail_app_password: SecretStr | None = None

    job_secret: str | None = Field(default=None, min_length=24)
    admin_report_emails: str | None = None
    sentry_dsn: str | None = None

    @property
    def cookie_secure(self) -> bool:
        return self.environment == "production"  # plain http is only allowed for local/test

    @model_validator(mode="after")
    def _gmail_needs_credentials(self) -> Settings:
        if self.mail_transport == "gmail":
            if not self.gmail_address:
                raise ValueError("GMAIL_ADDRESS is required when MAIL_TRANSPORT=gmail")
            if not self.gmail_app_password or not self.gmail_app_password.get_secret_value().strip():
                raise ValueError("GMAIL_APP_PASSWORD is required when MAIL_TRANSPORT=gmail")
        return self


# ───────────────────────────── YAML models ─────────────────────────────
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Brand(_Strict):
    name: str
    logo_full: str
    logo_mark: str
    favicon: str


class SessionCfg(_Strict):
    access_token_minutes: int = Field(15, ge=1, le=60)
    max_session_hours: int = Field(12, ge=1, le=72)


class UploadCfg(_Strict):
    max_mb: int = Field(10, ge=1, le=50)
    allowed: list[Literal["pdf", "docx"]]


class BillNoCfg(_Strict):
    digits: int = Field(6, ge=1, le=12)
    start: int = Field(1, ge=1)


class OtpCfg(_Strict):
    digits: int = Field(6, ge=6, le=8)
    expires_minutes: int = Field(5, ge=1, le=15)
    max_attempts: int = Field(5, ge=1, le=10)
    resend_cooldown_seconds: int = Field(60, ge=0, le=600)
    max_requests_per_hour: int = Field(5, ge=1, le=20)


class OtpEmailCfg(_Strict):
    subject: str
    body: str


class AuthCfg(_Strict):
    otp: OtpCfg = OtpCfg()
    email: OtpEmailCfg


class AppCfg(_Strict):
    brand: Brand
    timezone: str
    currency: str = "INR"
    session: SessionCfg = SessionCfg()
    upload: UploadCfg
    bill_no: BillNoCfg = BillNoCfg()
    auth: AuthCfg

    @model_validator(mode="after")
    def _tz(self) -> AppCfg:
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone '{self.timezone}' (example: Asia/Kolkata)") from exc
        return self


class SubmoduleDef(_Strict):
    key: str
    name: str


class ModuleDef(_Strict):
    key: str
    name: str
    icon: str
    tile: bool = True
    visible_to_staff: bool
    roles: list[Role]
    lab_aware: bool = False
    submodules: list[SubmoduleDef] = []


class ModulesCfg(_Strict):
    modules: list[ModuleDef]


FieldType = Literal["text", "number", "enum", "date", "datetime", "text_list", "money"]


class FieldDef(_Strict):
    key: str
    label: str
    type: FieldType
    options: list[str] | None = None
    required_in: list[str] = []
    default: int | float | str | None = None
    auto: bool = False
    computed: bool = False
    internal: bool = False
    admin_only: bool = False


class FieldsCfg(_Strict):
    fields: list[FieldDef]


class DedupeCfg(_Strict):
    age_tolerance_years: int = Field(ge=0, le=10)
    name_similarity_threshold: float = Field(ge=0.5, le=1.0)


class ExcelColumn(_Strict):
    field: str
    header: str


class ExcelSend(_Strict):
    day: int = Field(1, ge=1, le=28)
    time: str = Field("06:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


class ExcelCfg(_Strict):
    workbook_name: str
    tests_separator: str = "; "
    recipients_env: str | None = None
    recipients: list[EmailStr] = []
    send: ExcelSend = ExcelSend()
    columns: list[ExcelColumn] = Field(min_length=1)

    @model_validator(mode="after")
    def _workbook(self) -> ExcelCfg:
        if "{year}" not in self.workbook_name:
            raise ValueError("workbook_name must contain {year}")
        if not (self.recipients or self.recipients_env):
            raise ValueError("give recipients or recipients_env")
        return self


class EmailMsg(_Strict):
    subject: str
    body: str


class WhatsappMsg(_Strict):
    template_name: str
    language: str
    share_text: str


class DeliveryCfg(_Strict):
    email: EmailMsg
    whatsapp: WhatsappMsg


class HeaderField(_Strict):
    label: str | None = None
    from_: str | None = Field(default=None, alias="from")
    type: Literal["text", "date", "datetime"] = "text"
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @model_validator(mode="after")
    def _one_source(self) -> HeaderField:
        if bool(self.label) == bool(self.from_):
            raise ValueError("give exactly one of 'label' or 'from'")
        return self


class Detect(_Strict):
    sender_domains: list[str] = []
    text_contains_all: list[str] = []
    filename_hint: str | None = None

    @model_validator(mode="after")
    def _regex(self) -> Detect:
        if self.filename_hint:
            try:
                re.compile(self.filename_hint)
            except re.error as exc:
                raise ValueError(f"filename_hint is not a valid pattern ({exc})") from exc
        return self


class ExtractTests(_Strict):
    source: Literal["page_heading", "investigation_column"]
    repeat_pages: Literal["merge"] | None = None


class BillLayout(_Strict):
    header_lines: list[str]
    sections: list[Literal["bill_no", "patient_details", "tests_table", "amount_summary"]]


class PartnerCfg(_Strict):
    key: str
    name: str
    enabled_in: list[str]
    parser: Literal["generic", "manual"] = "generic"
    detect: Detect | None = None
    datetime_formats: list[str] = []
    header: dict[str, HeaderField] = {}
    extra_labels: list[str] = []
    tests: ExtractTests | None = None
    bill: BillLayout | None = None

    @model_validator(mode="after")
    def _generic_needs_rules(self) -> PartnerCfg:
        if self.parser == "generic":
            if not self.header:
                raise ValueError("a generic parser needs a 'header' section")
            if not self.tests:
                raise ValueError("a generic parser needs a 'tests' section")
            if not self.datetime_formats:
                raise ValueError("a generic parser needs 'datetime_formats'")
        return self


class PortalConfig(BaseModel):
    app: AppCfg
    modules: list[ModuleDef]
    fields: list[FieldDef]
    partners: dict[str, PartnerCfg]
    excel: ExcelCfg
    dedupe: DedupeCfg
    delivery: DeliveryCfg

    def module(self, key: str) -> ModuleDef | None:
        return next((m for m in self.modules if m.key == key), None)

    def labs_for(self, module_key: str) -> list[PartnerCfg]:
        """Labs act as submodules automatically (PRD 4.1): derived from partner YAML."""
        mod = self.module(module_key)
        if not mod or not mod.lab_aware:
            return []
        return [p for p in self.partners.values() if module_key in p.enabled_in]

    def submodule_keys(self, module_key: str) -> list[str]:
        mod = self.module(module_key)
        if not mod:
            return []
        return [s.key for s in mod.submodules] + [p.key for p in self.labs_for(module_key)]


# ───────────────────────────── loading & validation ─────────────────────────────
def _read_yaml(path: Path, label: str, problems: list[str]) -> dict | None:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        problems.append(f"{label}: file is missing")
        return None
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark else ""
        problems.append(f"{label}: not valid YAML{where}. Check indentation and quotes.")
        return None
    if not isinstance(data, dict):
        problems.append(f"{label}: expected a list of settings, found {type(data).__name__}")
        return None
    return data


def _validate[T: BaseModel](model: type[T], data: dict | None, label: str, problems: list[str]):
    if data is None:
        return None
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        for err in exc.errors():
            where = " -> ".join(str(p) for p in err["loc"])
            msg = err["msg"].removeprefix("Value error, ")
            if err["type"] == "extra_forbidden":
                msg = "this setting is not recognised (typo?)"
            elif err["type"] == "missing":
                msg = "this setting is required but missing"
            problems.append(f"{label}: {where}: {msg}" if where else f"{label}: {msg}")
        return None


def _cross_check(cfg: PortalConfig) -> list[str]:
    p: list[str] = []
    mod_keys = [m.key for m in cfg.modules]
    for k in {k for k in mod_keys if mod_keys.count(k) > 1}:
        p.append(f"modules.yaml: module '{k}' is listed more than once")
    for m in cfg.modules:
        if not KEY_RE.match(m.key):
            p.append(f"modules.yaml: key '{m.key}' must be snake_case (letters, digits, _)")
        sub_keys = [s.key for s in m.submodules]
        for s in sub_keys:
            if not KEY_RE.match(s) or sub_keys.count(s) > 1:
                p.append(f"modules.yaml: module '{m.key}' has a bad or repeated submodule '{s}'")
        if "customer" in m.roles:
            p.append(f"modules.yaml: module '{m.key}': the customer role has no UI yet")

    field_keys = [f.key for f in cfg.fields]
    for k in {k for k in field_keys if field_keys.count(k) > 1}:
        p.append(f"fields.yaml: field '{k}' is listed more than once")
    for f in cfg.fields:
        if f.type == "enum" and not f.options:
            p.append(f"fields.yaml: field '{f.key}' is an enum and needs 'options'")
        for mk in f.required_in:
            if mk not in mod_keys:
                p.append(f"fields.yaml: field '{f.key}' is required_in unknown module '{mk}'")
    fset = set(field_keys)

    for col in cfg.excel.columns:
        if col.field not in fset:
            p.append(f"excel.yaml: column '{col.header}' uses unknown field '{col.field}'")

    for key, partner in cfg.partners.items():
        f = f"partners/{key}.yaml"
        for mk in partner.enabled_in:
            m = cfg.module(mk)
            if m is None:
                p.append(f"{f}: enabled_in lists unknown module '{mk}'")
            elif not m.lab_aware:
                p.append(f"{f}: module '{mk}' is not lab_aware in modules.yaml")
        if "bill_generator" in partner.enabled_in and not partner.bill:
            p.append(f"{f}: enabled in bill_generator but has no 'bill:' layout section")
        for hk, hf in partner.header.items():
            if hk not in fset and hk not in COMPOSITE_HEADER_KEYS:
                p.append(f"{f}: header -> {hk}: not a standard field (see fields.yaml)")
            if hf.from_ and hf.from_ not in partner.header:
                p.append(f"{f}: header -> {hk}: 'from: {hf.from_}' is not another header entry")
        for std in ("patient_name", "registration_date"):
            if partner.parser == "generic" and std not in partner.header:
                p.append(f"{f}: header must map '{std}' (needed to save an entry)")
    return p


def load_config(config_dir: Path | str = "config") -> PortalConfig:
    """Load and validate every YAML file. Raises ConfigError listing all problems at once."""
    root = Path(config_dir)
    problems: list[str] = []

    app = _validate(AppCfg, _read_yaml(root / "app.yaml", "app.yaml", problems), "app.yaml", problems)
    modules = _validate(
        ModulesCfg, _read_yaml(root / "modules.yaml", "modules.yaml", problems), "modules.yaml", problems
    )
    fields = _validate(FieldsCfg, _read_yaml(root / "fields.yaml", "fields.yaml", problems), "fields.yaml", problems)
    excel = _validate(ExcelCfg, _read_yaml(root / "excel.yaml", "excel.yaml", problems), "excel.yaml", problems)
    dedupe = _validate(DedupeCfg, _read_yaml(root / "dedupe.yaml", "dedupe.yaml", problems), "dedupe.yaml", problems)
    delivery = _validate(
        DeliveryCfg, _read_yaml(root / "delivery.yaml", "delivery.yaml", problems), "delivery.yaml", problems
    )

    partners: dict[str, PartnerCfg] = {}
    pdir = root / "partners"
    files = sorted(pdir.glob("*.yaml")) if pdir.is_dir() else []
    if not files:
        problems.append("partners/: no lab files found (one <key>.yaml per lab)")
    for path in files:
        label = f"partners/{path.name}"
        partner = _validate(PartnerCfg, _read_yaml(path, label, problems), label, problems)
        if partner is None:
            continue
        if partner.key != path.stem:
            problems.append(f"{label}: key '{partner.key}' must match the file name '{path.stem}'")
            continue
        partners[partner.key] = partner

    if problems or not all([app, modules, fields, excel, dedupe, delivery]):
        raise ConfigError(problems)

    cfg = PortalConfig(
        app=app,
        modules=modules.modules,
        fields=fields.fields,
        partners=partners,
        excel=excel,
        dedupe=dedupe,
        delivery=delivery,
    )
    problems = _cross_check(cfg)
    if problems:
        raise ConfigError(problems)
    return cfg
