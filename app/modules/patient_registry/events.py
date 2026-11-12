"""Reacts to `entry.saved` (published by any module that saves an entry): refresh the Excel register rows."""

import logging
from typing import Any

from app.core.events import EventBus
from app.domain import excel_register

log = logging.getLogger(__name__)


async def _on_entry_saved(payload: dict[str, Any]) -> None:
    months = payload.get("months") or []
    if months:
        await excel_register.rebuild_months(payload["db"], payload["cfg"], months)


def register(bus: EventBus) -> None:
    bus.subscribe("entry.saved", _on_entry_saved)