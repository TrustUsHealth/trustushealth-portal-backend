"""Spectrum EMR portal client (ASP.NET WebForms + UpdatePanel / __doPostBack).

Only scraping lives here: no database, no entries. The flow mirrors a browser session:

    login (async postback) -> GET fm_Test_Report.aspx -> View (date range) -> grid rows
    per row:  "Test Wise Status" postback -> tests[]      (table ctl00_cpTechmed_grdTestStatusList)
              tick that row only + Print postback -> hidden ctl00_cpTechmed_hidTemplate_Report_Url
              GET fm_Preview_Report.aspx?ourls=... -> PDF

WebForms rules honoured: every response of an async postback carries the next __VIEWSTATE and
__EVENTVALIDATION (`hiddenField` entries); they are absorbed and re-sent on the next request.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any, TypeVar
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx

log = logging.getLogger(__name__)
T = TypeVar("T")

LOGIN_PAGE = "fm_Login.aspx"
DASHBOARD_PAGE = "fm_Dashboard.aspx"
REPORT_PAGE = "fm_Test_Report.aspx"
PREVIEW_PAGE = "fm_Preview_Report.aspx"

SCRIPT_MANAGER = "ctl00$aspScript"
PANEL_TRIGGER = "ctl00$cpTechmed$upTestHistory"
VIEW_BUTTON = "ctl00$cpTechmed$butView"
PRINT_BUTTON = "ctl00$cpTechmed$butPrint"
FROM_FIELD = "ctl00$cpTechmed$txt_FromDate"
TO_FIELD = "ctl00$cpTechmed$txt_ToDate"
TEMPLATE_URL_FIELD = "ctl00$cpTechmed$hidTemplate_Report_Url"
GRID_ID = "ctl00_cpTechmed_gvTestReport"
GRID_NAME_PART = "$gvTestReport$"
STATUS_GRID_ID = "ctl00_cpTechmed_grdTestStatusList"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

# Client-side (DevExpress / AJAX toolkit) values the browser adds to every postback. They are copied from a real
# request and only used when the page itself did not provide a non-empty value.
_Q = "&quot;"
_PAGE_DEFAULTS: dict[str, str] = {
    "ctl00$cpTechmed$mseFromDate_ClientState": "",
    "ctl00$cpTechmed$meTodate_ClientState": "",
    "ctl00$cpTechmed$txt_PtCode": "",
    "ctl00$cpTechmed$txt_PtName": "",
    "ctl00$cpTechmed$hfPtCodeID": "",
    "ctl00$cpTechmed$pcFilesState": "{" + _Q + "windowsState" + _Q + ":" + _Q + "0:0:-1:0:0:0:-10000:-10000:1:0:0:0" + _Q + "}",
    "ctl00$cpTechmed$pcFiles$ASPxPanel2$fmPatient_Files": (
        "{" + _Q + "filter" + _Q + ":{}," + _Q + "item" + _Q + ":{}," + _Q + "currentPath" + _Q + ":" + _Q + _Q + ","
        + _Q + "currentIdPath" + _Q + ":[" + _Q + "a:" + _Q + "]," + _Q + "virtScrollItemIndex" + _Q + ":0,"
        + _Q + "virtScrollPageItemsCount" + _Q + ":25}"
    ),
    "ctl00$cpTechmed$pcFiles$ASPxPanel2$fmPatient_Files$Splitter": (
        "{" + _Q + "state" + _Q + ":[{" + _Q + "s" + _Q + ":50," + _Q + "st" + _Q + ":" + _Q + "%" + _Q + ","
        + _Q + "c" + _Q + ":0," + _Q + "mp" + _Q + ":-1," + _Q + "spt" + _Q + ":0," + _Q + "spl" + _Q + ":0},{"
        + _Q + "i" + _Q + ":[{" + _Q + "st" + _Q + ":" + _Q + "px" + _Q + "," + _Q + "s" + _Q + ":250},{"
        + _Q + "i" + _Q + ":[{},{" + _Q + "s" + _Q + ":100," + _Q + "st" + _Q + ":" + _Q + "%" + _Q + ","
        + _Q + "c" + _Q + ":0," + _Q + "mp" + _Q + ":-1," + _Q + "spt" + _Q + ":0," + _Q + "spl" + _Q + ":0},{"
        + _Q + "c" + _Q + ":1}]," + _Q + "s" + _Q + ":100," + _Q + "st" + _Q + ":" + _Q + "%" + _Q + ","
        + _Q + "c" + _Q + ":0," + _Q + "mp" + _Q + ":-1}]," + _Q + "s" + _Q + ":50," + _Q + "st" + _Q + ":"
        + _Q + "%" + _Q + "," + _Q + "c" + _Q + ":0," + _Q + "mp" + _Q + ":-1}]}"
    ),
    "ctl00$cpTechmed$pcFiles$ASPxPanel2$fmPatient_Files$Splitter$Toolbar": (
        "{" + _Q + "selectedItemIndexPath" + _Q + ":" + _Q + _Q + "," + _Q + "checkedState" + _Q + ":" + _Q + _Q + "}"
    ),
    "ctl00$cpTechmed$pcFiles$ASPxPanel2$fmPatient_Files$Splitter$Toolbar$ITCNT0$TC$Path$TB": "QR_Gershwin",
    "ctl00$cpTechmed$pcFiles$ASPxPanel2$fmPatient_Files$Splitter$Toolbar$ITCNT10$TC$FC$TB": "",
    "ctl00$cpTechmed$pcFiles$ASPxPanel2$fmPatient_Files$ContextMenu": (
        "{" + _Q + "selectedItemIndexPath" + _Q + ":" + _Q + _Q + "," + _Q + "checkedState" + _Q + ":" + _Q + _Q + "}"
    ),
    "ctl00$cpTechmed$pcFiles$ASPxPanel2$ASPxPopupControlState": (
        "{" + _Q + "windowsState" + _Q + ":" + _Q + "0:0:-1:0:0:0:-10000:-10000:1:0:0:0" + _Q + "}"
    ),
    "ctl00$cpTechmed$txtChange": "",
    "ctl00$cpTechmed$txtFire": "",
    "ctl00$cpTechmed$hfclientScreenHeight": "864",
    "ctl00$cpTechmed$hfclientScreenWidth": "1536",
    "ctl00$cpTechmed$hfWidthForDataList": "600",
    "hiddenInputToUpdateATBuffer_CommonToolkitScripts": "1",
    "DXScript": "1_11,1_12,1_252,1_23,1_64,1_13,1_14,1_15,1_17,1_41,1_24,1_33,1_46,1_213,1_211,1_240,1_47,1_52,1_51,1_183,1_184,1_34",
    "DXCss": "1_74,1_68,1_69,1_73,1_210,1_207,1_209,1_206",
}


# ───────────────────────────── errors & data ─────────────────────────────
class PortalError(RuntimeError):
    """The portal answered in an unexpected way (or not at all)."""


class PortalAuthError(PortalError):
    """The portal refused the user name or password."""


class PortalSessionExpired(PortalError):
    """The portal sent us back to the login page."""


class ReportNotAvailable(PortalError):
    """The portal would not produce a PDF for this row (not ready, due bill, ...)."""


@dataclass(frozen=True)
class PortalSettings:
    base_url: str
    username: str
    password: str = field(repr=False)
    request_timeout_seconds: float = 60.0
    pdf_timeout_seconds: float = 120.0
    pause_between_requests_seconds: float = 0.3
    max_retries: int = 3
    verify_ssl: bool = True
    test_status_page: str | None = None


@dataclass(frozen=True)
class PortalRow:
    """One line of the Test Report grid."""

    ctl: str  # grid row token, e.g. "ctl03"
    branch_name: str
    branch_code: str
    sid_no: str
    sid_date: date
    referrer_type: str
    referrer_name: str
    patient_text: str  # "Mrs. BIJAL SHAH 46Y / F"
    patient_name: str  # "Mrs. BIJAL SHAH"
    age: int | None
    age_unit: str | None  # Y | M | D
    gender: str | None  # Male | Female | Other
    amount: Decimal | None  # what the lab bills us (lab_cost)
    gross: Decimal | None
    net: Decimal | None
    paid: Decimal | None
    due: Decimal | None
    status: str
    status_target: str  # __doPostBack target of the "Test Wise Status" link
    printable: bool  # the row has a Print checkbox

    @property
    def key(self) -> str:
        return f"{self.branch_code or self.branch_name}|{self.sid_no}|{self.sid_date.isoformat()}"


@dataclass(frozen=True)
class PortalReport:
    data: bytes
    filename: str | None


@dataclass
class Delta:
    """A parsed ASP.NET partial-page (UpdatePanel) response."""

    panels: dict[str, str] = field(default_factory=dict)
    hidden: dict[str, str] = field(default_factory=dict)
    scripts: list[str] = field(default_factory=list)
    redirect: str | None = None
    errors: list[tuple[str, str]] = field(default_factory=list)
    text: str = ""


# ───────────────────────────── parsing helpers ─────────────────────────────
def parse_delta(body: str) -> Delta | None:
    """`length|type|id|content|` records. Returns None when the body is not in that format."""
    if not re.match(r"\d+\|", body):
        return None
    out = Delta(text=body)
    pos, n = 0, len(body)
    while pos < n:
        bar = body.find("|", pos)
        if bar == -1:
            break
        try:
            length = int(body[pos:bar])
        except ValueError:
            raise PortalError("The portal sent a malformed partial-page response.") from None
        pos = bar + 1
        bar = body.find("|", pos)
        if bar == -1:
            raise PortalError("The portal sent a malformed partial-page response.")
        kind, pos = body[pos:bar], bar + 1
        bar = body.find("|", pos)
        if bar == -1:
            raise PortalError("The portal sent a malformed partial-page response.")
        ident, pos = body[pos:bar], bar + 1
        content = body[pos : pos + length]
        pos += length
        if body[pos : pos + 1] != "|":
            raise PortalError("The portal sent a malformed partial-page response.")
        pos += 1
        if kind == "updatePanel":
            out.panels[ident] = content
        elif kind == "hiddenField":
            out.hidden[ident] = content
        elif kind in ("scriptBlock", "scriptStartupBlock"):
            out.scripts.append(content)
        elif kind == "pageRedirect":
            out.redirect = unquote(content)
        elif kind == "error":
            out.errors.append((ident, content))
    return out


_SKIPPED_INPUT_TYPES = {"submit", "button", "image", "reset", "file"}


class _FormCollector(HTMLParser):
    """What a browser would submit: named, enabled controls; unchecked boxes are left out."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fields: dict[str, str] = {}
        self._select: str | None = None
        self._first: str | None = None
        self._chosen: str | None = None
        self._opt: dict[str, Any] | None = None
        self._area: str | None = None
        self._area_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "input":
            name = a.get("name")
            if not name or "disabled" in a:
                return
            kind = a.get("type", "text").lower()
            if kind in _SKIPPED_INPUT_TYPES:
                return
            if kind in ("checkbox", "radio"):
                if "checked" in a:
                    self.fields[name] = a.get("value") or "on"
                return
            self.fields[name] = a.get("value", "")
        elif tag == "select":
            if a.get("name") and "disabled" not in a:
                self._select, self._first, self._chosen = a["name"], None, None
        elif tag == "option" and self._select:
            self._opt = {"value": dict(attrs).get("value"), "selected": "selected" in a, "text": []}
        elif tag == "textarea" and a.get("name") and "disabled" not in a:
            self._area, self._area_buf = a["name"], []

    def handle_data(self, data: str) -> None:
        if self._opt is not None:
            self._opt["text"].append(data)
        if self._area is not None:
            self._area_buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "option" and self._opt is not None:
            value = self._opt["value"]
            if value is None:
                value = "".join(self._opt["text"]).strip()
            if self._first is None:
                self._first = value
            if self._opt["selected"]:
                self._chosen = value
            self._opt = None
        elif tag == "select" and self._select:
            value = self._chosen if self._chosen is not None else self._first
            if value is not None:
                self.fields[self._select] = value
            self._select = None
        elif tag == "textarea" and self._area is not None:
            self.fields[self._area] = "".join(self._area_buf)
            self._area = None


def collect_fields(markup: str) -> dict[str, str]:
    parser = _FormCollector()
    parser.feed(markup)
    parser.close()
    return parser.fields


_CELL_RE = re.compile(r"<(?P<tag>span|a)\b(?P<attrs>[^>]*?)>(?P<body>.*?)</(?P=tag)>", re.S | re.I)
_ID_RE = re.compile(r"""\bid\s*=\s*["']([^"']+)["']""", re.I)
_GRID_CELL_RE = re.compile(rf"{GRID_ID}_(ctl\d+)_(\w+)")
_STATUS_CELL_RE = re.compile(rf"{STATUS_GRID_ID}_(ctl\d+)_(\w+)")
_POSTBACK_RE = re.compile(r"__doPostBack\('([^']+)'")
_POPUP_RE = re.compile(r"PopUp\('([^']*)','([^']*)','([^']*)'")
_PATIENT_RE = re.compile(
    r"^(?P<name>.+?)\s+(?P<age>\d{1,3})\s*(?P<unit>[YMDymd])\w*\s*/\s*(?P<gender>[A-Za-z]+)\.?$"
)
_GENDERS = {"m": "Male", "male": "Male", "f": "Female", "female": "Female", "o": "Other", "t": "Other",
            "other": "Other", "transgender": "Other"}


@dataclass(frozen=True)
class _Cell:
    text: str
    attrs: str


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", fragment))).strip()


def _money(value: str) -> Decimal | None:
    try:
        return Decimal(value.replace(",", "").strip())
    except (InvalidOperation, AttributeError):
        return None


def parse_patient(text: str) -> tuple[str, int | None, str | None, str | None]:
    """'Mrs. BIJAL SHAH 46Y / F' -> ('Mrs. BIJAL SHAH', 46, 'Y', 'Female')."""
    m = _PATIENT_RE.match(text.strip())
    if not m:
        return text.strip(), None, None, None
    return (
        m.group("name").strip(),
        int(m.group("age")),
        m.group("unit").upper(),
        _GENDERS.get(m.group("gender").casefold()),
    )


def _cells(text: str, pattern: re.Pattern[str]) -> dict[str, dict[str, _Cell]]:
    grouped: dict[str, dict[str, _Cell]] = {}
    for m in _CELL_RE.finditer(text):
        ident = _ID_RE.search(m.group("attrs"))
        hit = pattern.fullmatch(ident.group(1)) if ident else None
        if hit:
            grouped.setdefault(hit.group(1), {})[hit.group(2)] = _Cell(_text(m.group("body")), html.unescape(m.group("attrs")))
    return grouped


def parse_rows(markup: str) -> list[PortalRow]:
    rows: list[PortalRow] = []
    seen: set[str] = set()
    for ctl, cells in _cells(markup, _GRID_CELL_RE).items():

        def t(name: str, cells: dict[str, _Cell] = cells) -> str:
            c = cells.get(name)
            return c.text if c else ""

        patient, link = cells.get("lnkPat_Name"), cells.get("lbutTest_Status")
        if not (patient and link and link.text and t("lblSid_Date")):
            continue
        try:
            sid_date = datetime.strptime(t("lblSid_Date"), "%d/%m/%Y").date()
        except ValueError:
            log.warning("Spectrum grid row %s has an unreadable date; skipped", ctl)
            continue
        post = _POSTBACK_RE.search(link.attrs)
        pop = _POPUP_RE.search(patient.attrs)
        name, age, unit, gender = parse_patient(patient.text)
        row = PortalRow(
            ctl=ctl,
            branch_name=t("lblBrancn_Name"),
            branch_code=pop.group(2) if pop else "",
            sid_no=link.text,
            sid_date=sid_date,
            referrer_type=t("lblRef_Desc"),
            referrer_name=t("lblRef_Name"),
            patient_text=patient.text,
            patient_name=name,
            age=age,
            age_unit=unit,
            gender=gender,
            amount=_money(t("lblTr_Amount")),
            gross=_money(t("lblTr_Gross")),
            net=_money(t("lblTr_Net")),
            paid=_money(t("lblTr_Paid")),
            due=_money(t("lblTr_Due")),
            status=t("lblSid_Status"),
            status_target=post.group(1) if post else f"ctl00$cpTechmed$gvTestReport${ctl}$lbutTest_Status",
            printable=f"gvTestReport${ctl}$chkSelect" in markup,
        )
        if row.key not in seen:
            seen.add(row.key)
            rows.append(row)
    return rows


def parse_test_list(markup: str) -> list[str]:
    """Test / Profile names from the `grdTestStatusList` table (status column is ignored)."""
    names: list[str] = []
    for cells in _cells(markup, _STATUS_CELL_RE).values():
        cell = cells.get("Label1")
        if cell and cell.text and cell.text.casefold() not in {n.casefold() for n in names}:
            names.append(cell.text)
    return names


def _alerts(texts: list[str]) -> list[str]:
    out: list[str] = []
    for text in texts:
        out += [_text(m) for m in re.findall(r"""alert\(\s*['"](.+?)['"]\s*\)""", text, re.S)]
    return [a for a in out if a]


def _looks_like_login(body: str) -> bool:
    return 'id="txtUserName"' in body and "butLogin" in body


# ───────────────────────────── the client ─────────────────────────────
class SpectrumPortalClient:
    def __init__(self, settings: PortalSettings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._s = settings
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self._form: dict[str, str] = {}
        self._rows: dict[str, PortalRow] = {}
        self._window: tuple[date, date] | None = None
        self._requests = 0

    async def __aenter__(self) -> SpectrumPortalClient:
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(self._s.request_timeout_seconds),
            verify=self._s.verify_ssl,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
            transport=self._transport,
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ── plumbing
    def _url(self, page: str) -> str:
        return f"{self._s.base_url.rstrip('/')}/{page}"

    @property
    def _report_url(self) -> str:
        return self._url(REPORT_PAGE)

    @staticmethod
    def _is_login(resp: httpx.Response) -> bool:
        return LOGIN_PAGE.casefold() in str(resp.url).casefold()

    async def _request(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        assert self._http is not None, "use `async with SpectrumPortalClient(...)`"
        last: Exception | None = None
        for attempt in range(1, self._s.max_retries + 1):
            if self._requests and self._s.pause_between_requests_seconds:
                await asyncio.sleep(self._s.pause_between_requests_seconds)
            self._requests += 1
            try:
                resp = await self._http.request(
                    method, url, data=data, headers=headers, timeout=timeout or self._s.request_timeout_seconds
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = exc
            else:
                if resp.status_code < 500:
                    if resp.status_code >= 400:
                        raise PortalError(f"The portal answered HTTP {resp.status_code}.")
                    return resp
                last = PortalError(f"The portal answered HTTP {resp.status_code}.")
            if attempt < self._s.max_retries:
                await asyncio.sleep(1.5 * attempt)
        raise PortalError(f"The portal could not be reached: {last}") from last

    def _ajax_headers(self, referer: str) -> dict[str, str]:
        parts = urlsplit(self._s.base_url)
        return {
            "X-MicrosoftAjax": "Delta=true",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "*/*",
            "Cache-Control": "no-cache",
            "Origin": f"{parts.scheme}://{parts.netloc}",
            "Referer": referer,
        }

    async def _post_async(self, url: str, data: dict[str, str]) -> Delta:
        resp = await self._request("POST", url, data=data, headers=self._ajax_headers(url))
        if self._is_login(resp):
            raise PortalSessionExpired("The portal session expired.")
        delta = parse_delta(resp.text)
        if delta is None:
            if _looks_like_login(resp.text):
                raise PortalSessionExpired("The portal session expired.")
            raise PortalError("The portal did not answer with a partial-page response.")
        if delta.errors:
            code, message = delta.errors[0]
            raise PortalError(f"The portal reported an error ({code}): {_text(message)[:200]}")
        if delta.redirect and LOGIN_PAGE.casefold() in delta.redirect.casefold():
            raise PortalSessionExpired("The portal session expired.")
        self._absorb(delta)
        return delta

    def _absorb(self, delta: Delta) -> None:
        """Keep the form state in step with the server (ViewState, EventValidation, re-rendered panels)."""
        self._form.update(delta.hidden)
        for panel_id, markup in delta.panels.items():
            if panel_id.endswith("upTestHistory"):  # the grid is re-rendered: forget old row checkboxes
                for key in [k for k in self._form if GRID_NAME_PART in k]:
                    del self._form[key]
            self._form.update(collect_fields(markup))

    def _payload(self, trigger: str) -> dict[str, str]:
        data = dict(self._form)
        for key, value in _PAGE_DEFAULTS.items():
            if not data.get(key):
                data[key] = value
        data.update(
            {
                SCRIPT_MANAGER: trigger,
                "__EVENTTARGET": "",
                "__EVENTARGUMENT": "",
                "__LASTFOCUS": "",
                "__ASYNCPOST": "true",
                TEMPLATE_URL_FIELD: "",
            }
        )
        return data

    # ── session
    async def login(self) -> None:
        url = self._url(LOGIN_PAGE)
        page = await self._request("GET", url)
        fields = collect_fields(page.text)
        if "__VIEWSTATE" not in fields:
            raise PortalError("The portal login page changed (no view state found).")
        data = dict(fields)
        data.update(
            {
                "aspScript": "UpdatePanel1|butLogin",
                "txtUserName": self._s.username,
                "txtPassword": self._s.password,
                "__LASTFOCUS": "",
                "__EVENTTARGET": "",
                "__EVENTARGUMENT": "",
                "__ASYNCPOST": "true",
                "butLogin": "Log in",
            }
        )
        resp = await self._request("POST", url, data=data, headers=self._ajax_headers(url))
        delta = parse_delta(resp.text)
        if delta is not None and delta.errors:
            raise PortalAuthError("The portal rejected the login request.")
        dash = await self._request("GET", self._url(DASHBOARD_PAGE))
        if self._is_login(dash) or _looks_like_login(dash.text):
            raise PortalAuthError("The portal rejected the user name or password.")
        self._form, self._rows = {}, {}

    async def _open_report_page(self) -> None:
        resp = await self._request("GET", self._report_url)
        if self._is_login(resp) or _looks_like_login(resp.text):
            raise PortalSessionExpired("The portal session expired.")
        form = collect_fields(resp.text)
        if "__VIEWSTATE" not in form:
            raise PortalError("The Test Report page changed (no view state found).")
        self._form = form

    async def _with_session(self, op: Callable[[], Awaitable[T]], *, restore_view: bool = True) -> T:
        try:
            return await op()
        except PortalSessionExpired:
            log.warning("Spectrum portal session expired; signing in again")
            await self.login()
            if restore_view and self._window:
                await self._view(*self._window)
            return await op()

    # ── list
    async def open_window(self, date_from: date, date_to: date) -> list[PortalRow]:
        """Run the View button for [date_from, date_to] (dd/mm/yyyy on the wire) and return the grid rows."""
        self._window = (date_from, date_to)
        return await self._with_session(lambda: self._view(date_from, date_to), restore_view=False)

    async def _view(self, date_from: date, date_to: date) -> list[PortalRow]:
        if "__VIEWSTATE" not in self._form:
            await self._open_report_page()
        data = self._payload(f"{PANEL_TRIGGER}|{VIEW_BUTTON}")
        data[FROM_FIELD] = date_from.strftime("%d/%m/%Y")
        data[TO_FIELD] = date_to.strftime("%d/%m/%Y")
        data[VIEW_BUTTON] = "View"
        delta = await self._post_async(self._report_url, data)
        panel = next((m for pid, m in delta.panels.items() if pid.endswith("upTestHistory")), None)
        if panel is None:
            raise PortalError("The portal did not return the test report list.")
        if "Page$" in panel:
            log.warning("Spectrum grid shows a pager; only the first page is read. Use a shorter window_days.")
        rows = parse_rows(panel)
        self._rows = {r.key: r for r in rows}
        if not rows:
            log.info("Spectrum: no records for %s - %s", date_from, date_to)
        return rows

    def _current(self, row: PortalRow) -> PortalRow:
        cur = self._rows.get(row.key)
        if cur is None:
            raise PortalError("This row is no longer in the portal list.")
        return cur

    # ── tests
    async def fetch_tests(self, row: PortalRow) -> list[str]:
        return await self._with_session(lambda: self._tests(row))

    def _candidate_urls(self, delta: Delta) -> list[str]:
        found: list[str] = []
        if delta.redirect:
            found.append(delta.redirect)
        for script in delta.scripts:
            script = script.replace("\\u0026", "&").replace("\\/", "/")
            found += re.findall(r"""(?:window\.open|location\.href\s*=|location\.replace)\(?\s*['"]([^'"]+)['"]""", script)
            found += re.findall(r"""['"]([^'"\s]*\.aspx[^'"\s]*)['"]""", script)
        if self._s.test_status_page:
            found.append(self._s.test_status_page)
        skip = (REPORT_PAGE.casefold(), LOGIN_PAGE.casefold(), DASHBOARD_PAGE.casefold())
        urls: list[str] = []
        for u in found:
            u = html.unescape(u)
            if any(s in u.casefold() for s in skip) and u != self._s.test_status_page:
                continue
            absolute = urljoin(self._report_url, u)
            if absolute not in urls:
                urls.append(absolute)
        return urls

    async def _tests(self, row: PortalRow) -> list[str]:
        cur = self._current(row)
        data = self._payload(f"{PANEL_TRIGGER}|{cur.status_target}")
        data["__EVENTTARGET"] = cur.status_target
        delta = await self._post_async(self._report_url, data)
        tests = parse_test_list("\n".join([*delta.panels.values(), *delta.scripts]))
        if tests:
            return tests
        for url in self._candidate_urls(delta):
            resp = await self._request("GET", url, headers={"Referer": self._report_url})
            if self._is_login(resp):
                raise PortalSessionExpired("The portal session expired.")
            tests = parse_test_list(resp.text)
            if tests:
                return tests
        raise PortalError("The test list for this SID could not be found (see test_status_page in spectrum_sync.yaml).")

    # ── report
    async def fetch_report(self, row: PortalRow) -> PortalReport:
        return await self._with_session(lambda: self._report(row))

    async def _report(self, row: PortalRow) -> PortalReport:
        cur = self._current(row)
        if not cur.printable:
            raise ReportNotAvailable("The portal offers no Print option for this row (for example a due bill).")
        data = self._payload(f"{PANEL_TRIGGER}|{PRINT_BUTTON}")
        data[f"ctl00$cpTechmed$gvTestReport${cur.ctl}$chkSelect"] = "on"  # this row only
        data[PRINT_BUTTON] = "Print"
        delta = await self._post_async(self._report_url, data)
        value = ""
        for markup in delta.panels.values():  # only THIS response: never a value left over from another row
            value = collect_fields(markup).get(TEMPLATE_URL_FIELD, "") or value
        if not value:
            m = re.search(r"""fm_Preview_Report\.aspx\?ourls=([^'"&\s<\\]+)""", delta.text)
            value = m.group(1) if m else ""
        if not value:
            alerts = _alerts([*delta.scripts, delta.text])
            raise ReportNotAvailable(alerts[0] if alerts else "The portal returned no report link for this row.")
        return await self._download_pdf(value)

    async def _download_pdf(self, ourls: str) -> PortalReport:
        value = html.unescape(ourls)
        if "%" not in value:
            value = quote(value, safe="")
        url = f"{self._url(PREVIEW_PAGE)}?ourls={value}"
        headers = {"Referer": self._report_url}
        resp = await self._request("GET", url, headers=headers, timeout=self._s.pdf_timeout_seconds)
        if self._is_login(resp):
            raise PortalSessionExpired("The portal session expired.")
        if resp.content[:1024].find(b"%PDF-") == -1:  # a viewer page that links to the file
            link = re.search(r"""(?:src|href)\s*=\s*["']([^"']+\.pdf[^"']*)["']""", resp.text, re.I)
            if not link:
                raise ReportNotAvailable("The portal did not return a PDF for this row.")
            resp = await self._request(
                "GET", urljoin(url, html.unescape(link.group(1))), headers=headers, timeout=self._s.pdf_timeout_seconds
            )
            if resp.content[:1024].find(b"%PDF-") == -1:
                raise ReportNotAvailable("The portal did not return a PDF for this row.")
        disposition = resp.headers.get("content-disposition", "")
        name = re.search(r"""filename\*?=(?:UTF-8'')?"?([^";]+)""", disposition, re.I)
        return PortalReport(resp.content, unquote(name.group(1)).strip() if name else None)