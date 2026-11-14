"""File storage for report attachments.

MongoDB documents remain the source of metadata. File bytes may live in MongoDB or private Google Drive storage.
List and search queries MUST exclude Mongo bytes: pass ``NO_FILE_DATA`` as the projection.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol

from bson import Binary
from fastapi import Request
from pymongo.asynchronous.database import AsyncDatabase

NO_FILE_DATA: dict[str, int] = {"file.data": 0}
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


@dataclass(frozen=True)
class StoredFile:
    name: str
    content_type: str
    size: int
    sha256: str
    data: bytes


class FileStoreError(RuntimeError):
    """A storage backend operation failed or returned unverifiable data."""


class FileStore(Protocol):
    async def pack(
        self,
        *,
        name: str,
        content_type: str,
        data: bytes,
        original: bytes | None = None,
        month_key: str | None = None,
    ) -> dict[str, Any]:
        """Store bytes and return the `file` sub-document for the owning record."""
        ...

    async def read(self, db: AsyncDatabase, collection: str, doc_id: Any) -> StoredFile | None: ...

    async def cleanup(self, file_doc: dict[str, Any]) -> None: ...


class MongoFileStore:
    async def pack(
        self,
        *,
        name: str,
        content_type: str,
        data: bytes,
        original: bytes | None = None,
        month_key: str | None = None,
    ) -> dict[str, Any]:
        source = original if original is not None else data
        return {
            "storage": "mongo",
            "name": name,
            "content_type": content_type,
            "size": len(data),
            "original_size": len(source),
            "sha256": hashlib.sha256(source).hexdigest(),
            "data": Binary(data),
        }

    @staticmethod
    def read_metadata(f: dict[str, Any]) -> StoredFile:
        return StoredFile(
            name=f["name"],
            content_type=f["content_type"],
            size=int(f["size"]),
            sha256=f["sha256"],
            data=bytes(f["data"]),
        )

    async def read(self, db: AsyncDatabase, collection: str, doc_id: Any) -> StoredFile | None:
        doc = await db[collection].find_one({"_id": doc_id}, {"file": 1})
        f = (doc or {}).get("file")
        return self.read_metadata(f) if f and f.get("data") is not None else None

    async def cleanup(self, file_doc: dict[str, Any]) -> None:
        return None


class DriveFileStore:
    """Private Drive storage. Google API operations run in worker threads."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        root_folder_id: str,
        *,
        client: Any | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._root_folder_id = root_folder_id
        self._api_client = client
        self._client_lock = threading.Lock()
        self._folder_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._folder_ids: dict[str, str] = {}

    @classmethod
    def from_settings(cls, settings: Any) -> DriveFileStore:
        return cls(
            settings.gdrive_client_id,
            settings.gdrive_client_secret.get_secret_value(),
            settings.gdrive_refresh_token.get_secret_value(),
            settings.gdrive_root_folder_id,
        )

    def _client(self) -> Any:
        if self._api_client is None:
            with self._client_lock:
                if self._api_client is None:
                    from google.oauth2.credentials import Credentials
                    from googleapiclient.discovery import build

                    credentials = Credentials(
                        token=None,
                        refresh_token=self._refresh_token,
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=self._client_id,
                        client_secret=self._client_secret,
                        scopes=[DRIVE_SCOPE],
                    )
                    self._api_client = build("drive", "v3", credentials=credentials, cache_discovery=False)
        return self._api_client

    @staticmethod
    def _retryable_http_error(error: Exception) -> tuple[bool, int | None]:
        from googleapiclient.errors import HttpError

        if not isinstance(error, HttpError):
            return False, None
        status = getattr(error.resp, "status", None)
        if status == 429 or (isinstance(status, int) and status >= 500):
            return True, status
        if status == 403:
            try:
                payload = json.loads(error.content.decode("utf-8"))
                reasons = {
                    item.get("reason") for item in payload.get("error", {}).get("errors", []) if isinstance(item, dict)
                }
            except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
                reasons = set()
            return bool(reasons & {"rateLimitExceeded", "userRateLimitExceeded"}), status
        return False, status if isinstance(status, int) else None

    def _execute(self, operation: str, request_factory) -> Any:
        for attempt in range(3):
            try:
                with self._request_lock:
                    return request_factory().execute()
            except Exception as exc:
                retryable, status = self._retryable_http_error(exc)
                if retryable and attempt < 2:
                    time.sleep(0.5 * (2**attempt))
                    continue
                suffix = f" (HTTP {status})" if status is not None else ""
                raise FileStoreError(f"Google Drive {operation} failed{suffix}.") from exc
        raise AssertionError("unreachable")

    def _month_folder(self, month_key: str) -> str:
        if not _MONTH_RE.fullmatch(month_key):
            raise ValueError("month_key must use YYYY-MM format")
        with self._folder_lock:
            if month_key in self._folder_ids:
                return self._folder_ids[month_key]
            files = self._client().files()
            query = (
                "name='"
                + month_key
                + "' and mimeType='application/vnd.google-apps.folder' and '"
                + self._root_folder_id
                + "' in parents and trashed=false"
            )
            result = self._execute(
                "folder lookup",
                lambda: files.list(q=query, spaces="drive", pageSize=1, fields="files(id,name)"),
            )
            matches = result.get("files", [])
            if matches:
                folder_id = matches[0]["id"]
            else:
                result = self._execute(
                    "folder creation",
                    lambda: files.create(
                        body={
                            "name": month_key,
                            "mimeType": "application/vnd.google-apps.folder",
                            "parents": [self._root_folder_id],
                        },
                        fields="id",
                    ),
                )
                folder_id = result["id"]
            self._folder_ids[month_key] = folder_id
            return folder_id

    def _upload_sync(self, name: str, content_type: str, data: bytes, month_key: str) -> str:
        from googleapiclient.http import MediaIoBaseUpload

        parent_id = self._month_folder(month_key)
        files = self._client().files()
        result = self._execute(
            "upload",
            lambda: files.create(
                body={"name": name, "mimeType": content_type, "parents": [parent_id]},
                media_body=MediaIoBaseUpload(io.BytesIO(data), mimetype=content_type, resumable=False),
                fields="id",
            ),
        )
        return result["id"]

    async def upload_file(self, name: str, content_type: str, data: bytes, month_key: str) -> str:
        return await asyncio.to_thread(self._upload_sync, name, content_type, data, month_key)

    def _download_sync(self, drive_id: str) -> bytes:
        files = self._client().files()
        data = self._execute("download", lambda: files.get_media(fileId=drive_id))
        return bytes(data)

    async def download_file(self, drive_id: str) -> bytes:
        return await asyncio.to_thread(self._download_sync, drive_id)

    def _delete_sync(self, drive_id: str) -> None:
        files = self._client().files()
        self._execute("delete", lambda: files.delete(fileId=drive_id))

    async def delete_file(self, drive_id: str) -> None:
        await asyncio.to_thread(self._delete_sync, drive_id)

    async def pack(
        self,
        *,
        name: str,
        content_type: str,
        data: bytes,
        original: bytes | None = None,
        month_key: str | None = None,
    ) -> dict[str, Any]:
        if month_key is None:
            raise ValueError("month_key is required for Google Drive storage")
        source = original if original is not None else data
        drive_id = await self.upload_file(name, content_type, data, month_key)
        return {
            "storage": "drive",
            "drive_id": drive_id,
            "name": name,
            "content_type": content_type,
            "size": len(data),
            "original_size": len(source),
            "sha256": hashlib.sha256(source).hexdigest(),
            "content_sha256": hashlib.sha256(data).hexdigest(),
        }

    async def read_metadata(self, f: dict[str, Any]) -> StoredFile:
        data = await self.download_file(f["drive_id"])
        if len(data) != int(f["size"]):
            raise FileStoreError("Google Drive download size did not match stored metadata.")
        expected_hash = f.get("content_sha256")
        if expected_hash is None and int(f["size"]) == int(f["original_size"]):
            expected_hash = f["sha256"]
        if expected_hash is None or hashlib.sha256(data).hexdigest() != expected_hash:
            raise FileStoreError("Google Drive download SHA-256 did not match stored metadata.")
        return StoredFile(
            name=f["name"],
            content_type=f["content_type"],
            size=int(f["size"]),
            sha256=f["sha256"],
            data=data,
        )

    async def cleanup(self, file_doc: dict[str, Any]) -> None:
        drive_id = file_doc.get("drive_id")
        if drive_id:
            await self.delete_file(drive_id)


class RoutingFileStore:
    """Writes according to configuration and reads according to each document's storage metadata."""

    def __init__(self, write_store: MongoFileStore | DriveFileStore, drive_store: DriveFileStore | None) -> None:
        self._write_store = write_store
        self._mongo_store = MongoFileStore()
        self._drive_store = drive_store

    async def pack(
        self,
        *,
        name: str,
        content_type: str,
        data: bytes,
        original: bytes | None = None,
        month_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._write_store.pack(
            name=name,
            content_type=content_type,
            data=data,
            original=original,
            month_key=month_key,
        )

    async def read(self, db: AsyncDatabase, collection: str, doc_id: Any) -> StoredFile | None:
        doc = await db[collection].find_one({"_id": doc_id}, {"file": 1})
        f = (doc or {}).get("file")
        if not f:
            return None
        if f.get("data") is not None:
            return self._mongo_store.read_metadata(f)
        if f.get("drive_id"):
            if self._drive_store is None:
                raise FileStoreError("The document is stored in Google Drive, but Drive storage is not configured.")
            return await self._drive_store.read_metadata(f)
        return None

    async def cleanup(self, file_doc: dict[str, Any]) -> None:
        if file_doc.get("drive_id") and self._drive_store is not None:
            await self._drive_store.cleanup(file_doc)


def create_filestore(settings: Any) -> RoutingFileStore:
    drive_credentials = (
        settings.gdrive_client_id,
        settings.gdrive_client_secret.get_secret_value() if settings.gdrive_client_secret else None,
        settings.gdrive_refresh_token.get_secret_value() if settings.gdrive_refresh_token else None,
        settings.gdrive_root_folder_id,
    )
    drive_store = (
        DriveFileStore.from_settings(settings) if all(value and value.strip() for value in drive_credentials) else None
    )
    write_store = drive_store if settings.file_store == "drive" else MongoFileStore()
    return RoutingFileStore(write_store, drive_store)


def get_filestore(request: Request) -> FileStore:
    store = getattr(request.app.state, "filestore", None)
    if store is None:
        store = create_filestore(request.app.state.settings)
        request.app.state.filestore = store
    return store
