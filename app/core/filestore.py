"""File storage behind one small interface (PRD 4.2, 9).

Today files live inside the owning MongoDB document as BSON BinData under a `file` sub-document. All other code talks
to `FileStore`, so moving to Cloudflare R2 / S3 later means adding another implementation here.
List and search queries MUST exclude the bytes: pass `NO_FILE_DATA` as the projection.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Protocol

from bson import Binary
from fastapi import Request
from pymongo.asynchronous.database import AsyncDatabase

NO_FILE_DATA: dict[str, int] = {"file.data": 0}


@dataclass(frozen=True)
class StoredFile:
    name: str
    content_type: str
    size: int
    sha256: str
    data: bytes


class FileStore(Protocol):
    def pack(self, *, name: str, content_type: str, data: bytes, original: bytes | None = None) -> dict[str, Any]:
        """Build the `file` sub-document to embed in the owning record."""
        ...

    async def read(self, db: AsyncDatabase, collection: str, doc_id: Any) -> StoredFile | None: ...


class MongoFileStore:
    def pack(self, *, name: str, content_type: str, data: bytes, original: bytes | None = None) -> dict[str, Any]:
        source = original if original is not None else data
        return {
            "name": name,
            "content_type": content_type,
            "size": len(data),
            "original_size": len(source),
            "sha256": hashlib.sha256(source).hexdigest(),  # hash of the ORIGINAL upload (duplicate detection)
            "data": Binary(data),
        }

    async def read(self, db: AsyncDatabase, collection: str, doc_id: Any) -> StoredFile | None:
        doc = await db[collection].find_one({"_id": doc_id}, {"file": 1})
        f = (doc or {}).get("file")
        if not f or f.get("data") is None:
            return None
        return StoredFile(
            name=f["name"],
            content_type=f["content_type"],
            size=int(f["size"]),
            sha256=f["sha256"],
            data=bytes(f["data"]),
        )


_DEFAULT = MongoFileStore()


def get_filestore(request: Request) -> FileStore:
    return getattr(request.app.state, "filestore", None) or _DEFAULT