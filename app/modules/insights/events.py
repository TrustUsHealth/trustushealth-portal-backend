"""Insights takes only one thing from other modules: a signal that entries changed, so cached numbers are dropped."""

from typing import Any

from app.modules.insights.service import CACHE


async def _entries_changed(_payload: dict[str, Any]) -> None:
    CACHE.clear()


def register(bus) -> None:
    bus.subscribe("entry.saved", _entries_changed)
    bus.subscribe("entry.deleted", _entries_changed)