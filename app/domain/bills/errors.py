"""One error type for every bill problem, carrying a stable `code` the frontend can act on."""

from __future__ import annotations

from typing import Any


class BillError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra

    @property
    def detail(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.extra}