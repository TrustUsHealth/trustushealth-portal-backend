import hashlib
import re

import pytest
from bson import Binary
from googleapiclient.errors import HttpError
from httplib2 import Response
from mongomock_motor import AsyncMongoMockClient

from app.core.config import Settings
from app.core.filestore import (
    DRIVE_SCOPE,
    DriveFileStore,
    FileStoreError,
    MongoFileStore,
    RoutingFileStore,
)
from app.domain.reports.service import store_report
from scripts.migrate_files_to_drive import migrate_collection, run_migration


class FakeRequest:
    def __init__(self, callback):
        self.callback = callback

    def execute(self):
        return self.callback()


class FakeDrive:
    def __init__(self):
        self.items = {}
        self.next_id = 0
        self.calls = []
        self.failures = []
        self.media_uploads = []

    def files(self):
        return self

    def _request(self, operation, callback):
        def execute():
            self.calls.append(operation)
            if self.failures and self.failures[0][0] == operation:
                _, error = self.failures.pop(0)
                raise error
            return callback()

        return FakeRequest(execute)

    def list(self, *, q, **kwargs):
        name = re.search(r"name='([^']+)'", q).group(1)
        root = re.search(r"'([^']+)' in parents", q).group(1)

        def result():
            found = [
                {"id": item["id"], "name": item["name"]}
                for item in self.items.values()
                if item.get("name") == name
                and item.get("mimeType") == "application/vnd.google-apps.folder"
                and item.get("parents") == [root]
            ]
            return {"files": found[:1]}

        return self._request("list", result)

    def create(self, *, body, media_body=None, **kwargs):
        def result():
            self.next_id += 1
            drive_id = f"drive-{self.next_id}"
            if media_body:
                self.media_uploads.append(media_body)
            data = media_body._fd.getvalue() if media_body else b""
            self.items[drive_id] = {**body, "id": drive_id, "data": data}
            return {"id": drive_id}

        return self._request("create", result)

    def get_media(self, *, fileId):
        return self._request("get_media", lambda: self.items[fileId]["data"])

    def delete(self, *, fileId):
        return self._request("delete", lambda: self.items.pop(fileId, None))


def drive_store(client=None):
    return DriveFileStore("client-id", "client-secret", "refresh-token", "root-id", client=client or FakeDrive())


@pytest.mark.asyncio
async def test_mongo_store_reads_legacy_document_without_storage_marker():
    db = AsyncMongoMockClient()["test"]
    data = b"legacy bytes"
    result = await db["reports_2026_01"].insert_one(
        {
            "file": {
                "name": "report.pdf",
                "content_type": "application/pdf",
                "size": len(data),
                "original_size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "data": Binary(data),
            }
        }
    )

    stored = await MongoFileStore().read(db, "reports_2026_01", result.inserted_id)

    assert stored is not None
    assert stored.data == data
    assert stored.sha256 == hashlib.sha256(data).hexdigest()


@pytest.mark.asyncio
async def test_drive_store_uploads_and_reads_verified_private_file():
    api = FakeDrive()
    store = drive_store(api)
    data = b"new report bytes" * 150_000
    metadata = await store.pack(
        name="report.pdf", content_type="application/pdf", data=data, original=data, month_key="2026-01"
    )

    stored = await store.read_metadata(metadata)

    assert metadata["storage"] == "drive"
    assert "data" not in metadata
    assert api.items[metadata["drive_id"]]["parents"] == ["drive-1"]
    assert api.items[metadata["drive_id"]]["mimeType"] == "application/pdf"
    assert api.media_uploads[0].resumable() is True
    assert api.media_uploads[0].chunksize() == 1024 * 1024
    assert api.calls.count("list") == 1
    assert stored.data == data
    assert stored.size == len(data)


@pytest.mark.asyncio
async def test_router_reads_each_document_from_its_recorded_backend():
    db = AsyncMongoMockClient()["test"]
    api = FakeDrive()
    backend = drive_store(api)
    mongo_data = b"mongo report"
    drive_data = b"drive report"
    drive_meta = await backend.pack(
        name="drive.pdf", content_type="application/pdf", data=drive_data, month_key="2026-02"
    )
    mongo_id = (
        await db["reports_2026_02"].insert_one(
            {
                "file": {
                    "name": "mongo.pdf",
                    "content_type": "application/pdf",
                    "size": len(mongo_data),
                    "original_size": len(mongo_data),
                    "sha256": hashlib.sha256(mongo_data).hexdigest(),
                    "data": Binary(mongo_data),
                }
            }
        )
    ).inserted_id
    drive_id = (await db["reports_2026_02"].insert_one({"file": drive_meta})).inserted_id
    store = RoutingFileStore(MongoFileStore(), backend)

    assert (await store.read(db, "reports_2026_02", mongo_id)).data == mongo_data
    assert (await store.read(db, "reports_2026_02", drive_id)).data == drive_data


@pytest.mark.asyncio
async def test_insert_failure_deletes_uploaded_drive_file(monkeypatch):
    db = AsyncMongoMockClient()["test"]
    api = FakeDrive()
    backend = drive_store(api)
    store = RoutingFileStore(backend, backend)

    async def fail_insert(*args, **kwargs):
        raise RuntimeError("insert failed")

    collection_type = type(db["reports_2026_03"])
    original_insert = collection_type.insert_one

    async def fail_report_insert(collection, *args, **kwargs):
        if collection.name == "reports_2026_03":
            await fail_insert(*args, **kwargs)
        return await original_insert(collection, *args, **kwargs)

    monkeypatch.setattr(collection_type, "insert_one", fail_report_insert)
    with pytest.raises(RuntimeError, match="insert failed"):
        await store_report(
            db,
            store,
            partner_key="spectrum",
            filename="report.pdf",
            content_type="application/pdf",
            data=b"report",
            parse=None,
            actor_id=None,
            source="upload",
            month_key="2026-03",
        )

    assert not [item for item in api.items.values() if item.get("mimeType") != "application/vnd.google-apps.folder"]
    assert "delete" in api.calls


@pytest.mark.asyncio
async def test_duplicate_sha256_is_checked_before_drive_upload():
    db = AsyncMongoMockClient()["test"]
    data = b"existing report"
    existing_id = (
        await db["reports_2026_04"].insert_one(
            {
                "file": {
                    "name": "existing.pdf",
                    "content_type": "application/pdf",
                    "size": len(data),
                    "original_size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "data": Binary(data),
                }
            }
        )
    ).inserted_id
    api = FakeDrive()
    backend = drive_store(api)

    result = await store_report(
        db,
        RoutingFileStore(backend, backend),
        partner_key="spectrum",
        filename="duplicate.pdf",
        content_type="application/pdf",
        data=data,
        parse=None,
        actor_id=None,
        source="upload",
        month_key="2026-04",
    )

    assert result.created is False
    assert result.doc["_id"] == existing_id
    assert "create" not in api.calls


@pytest.mark.asyncio
async def test_migration_is_idempotent_and_preserves_data_on_hash_mismatch():
    db = AsyncMongoMockClient()["test"]
    data = b"migration source"
    await db["reports_2026_05"].insert_one(
        {
            "file": {
                "name": "report.pdf",
                "content_type": "application/pdf",
                "size": len(data),
                "original_size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "data": Binary(data),
            }
        }
    )
    api = FakeDrive()
    backend = drive_store(api)
    dry_run = await run_migration(db, backend, dry_run=True)
    assert dry_run.selected == 1
    assert dry_run.migrated == 0
    assert not api.calls
    first = await run_migration(db, backend)
    second = await run_migration(db, backend)

    assert first.migrated == 1
    assert second.selected == 0
    migrated = await db["reports_2026_05"].find_one({})
    assert migrated["file"]["storage"] == "drive"
    assert "data" not in migrated["file"]

    class MismatchStore(DriveFileStore):
        async def download_file(self, drive_id):
            return b"mismatch"

    db2 = AsyncMongoMockClient()["test"]
    await db2["reports_2026_05"].insert_one(
        {
            "file": {
                "name": "report.pdf",
                "content_type": "application/pdf",
                "size": len(data),
                "original_size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "data": Binary(data),
            }
        }
    )
    failed = await migrate_collection(
        db2, MismatchStore("id", "secret", "token", "root", client=FakeDrive()), "reports_2026_05"
    )

    assert failed.failed == 1
    unchanged = await db2["reports_2026_05"].find_one({})
    assert bytes(unchanged["file"]["data"]) == data
    assert "drive_id" not in unchanged["file"]


def test_drive_retries_transient_errors_with_exponential_backoff(monkeypatch):
    api = drive_store()
    waits = []
    monkeypatch.setattr("app.core.filestore.time.sleep", waits.append)
    error = HttpError(Response({"status": "500"}), b"server error")
    attempts = 0

    class Request:
        def execute(self):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise error
            return "success"

    assert api._execute("test", Request) == "success"
    assert waits == [0.5, 1.0]
    assert attempts == 3


def test_drive_retries_rate_limited_403_and_raises_clear_final_error(monkeypatch):
    api = drive_store()
    waits = []
    monkeypatch.setattr("app.core.filestore.time.sleep", waits.append)
    limited = HttpError(
        Response({"status": "403"}),
        b'{"error":{"errors":[{"reason":"userRateLimitExceeded"}]}}',
    )
    with pytest.raises(FileStoreError, match="Google Drive test failed \\(HTTP 403\\)"):
        api._execute("test", lambda: FakeRequest(lambda: (_ for _ in ()).throw(limited)))
    assert waits == [0.5, 1.0]


def test_drive_reports_expired_or_revoked_refresh_token():
    from google.auth.exceptions import RefreshError

    api = drive_store()
    def request():
        return FakeRequest(lambda: (_ for _ in ()).throw(RefreshError("invalid_grant")))

    with pytest.raises(FileStoreError, match="authentication expired or was revoked"):
        api._execute("test", request)


def test_drive_settings_are_required_only_when_drive_is_selected():
    base = {
        "_env_file": None,
        "mongodb_uri": "mongodb://unused",
        "jwt_secret": "s" * 40,
    }

    assert Settings(**base).file_store == "mongo"
    with pytest.raises(ValueError, match="GDRIVE_CLIENT_ID.*FILE_STORE=drive"):
        Settings(**base, file_store="drive")
    configured = Settings(
        **base,
        file_store="drive",
        gdrive_client_id="id",
        gdrive_client_secret="secret",
        gdrive_refresh_token="refresh",
        gdrive_root_folder_id="folder",
    )
    assert configured.gdrive_client_secret.get_secret_value() == "secret"
    assert DRIVE_SCOPE == "https://www.googleapis.com/auth/drive.file"
