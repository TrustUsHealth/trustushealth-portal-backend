"""Finds a lab's bill code (`app/partners/<key>/bill.py`) by name, so modules never import `partners` directly."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from types import ModuleType

from app.core.config import PartnerCfg
from app.domain.bills.errors import BillError

APP_DIR = Path(__file__).resolve().parents[2]


def asset_path(relative: str) -> Path:
    """'assets/bill/signature.png' -> <app>/assets/bill/signature.png"""
    return APP_DIR / relative


def bill_module(partner: PartnerCfg) -> ModuleType:
    if partner.bill is None:
        raise BillError(404, "bill_unsupported", f"Bills are not supported for {partner.name}.")
    path = f"app.partners.{partner.key}.bill"
    try:
        return import_module(path)
    except ModuleNotFoundError as exc:
        if exc.name == path or path.startswith((exc.name or "\0") + "."):
            raise BillError(404, "bill_unsupported", f"Bills are not supported for {partner.name}.") from None
        raise