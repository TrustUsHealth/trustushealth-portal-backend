"""Simple in-process event bus. Modules never import each other; they react to events.

Planned events (PRD 4.2): entry.saved, report.stored, bill.generated, delivery.sent.
A module may define `events.py` with `register(bus)`; discovery calls it at startup.
"""

import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)
Handler = Callable[[dict[str, Any]], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, event: str, handler: Handler) -> None:
        self._handlers[event].append(handler)

    async def publish(self, event: str, payload: dict[str, Any]) -> None:
        """Run all handlers; one failing handler never blocks the publisher or the others."""
        for handler in self._handlers.get(event, []):
            try:
                await handler(payload)
            except Exception:  # noqa: BLE001
                log.exception("event handler failed for %s", event)
