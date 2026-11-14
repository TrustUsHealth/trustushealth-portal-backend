"""Move MongoDB report file bytes to private Google Drive storage."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import re
from dataclasses import dataclass, field

from app.core.config import Settings
from app.core.db import create_client, get_database
from app.core.filestore import DriveFileStore

_REPORT_COLLECTION_RE = re.compile(r"^reports_(\d{4})_(0[1-9]|1[0-2])$")


@dataclass
class MigrationSummary:
    selected: int = 0
    migrated: int = 0
    skipped: int = 0
    failed: int = 0
    failures: list[str] = field(default_factory=list)


async def migrate_collection(
    db,
    store: DriveFileStore,
    collection: str,
    *,
    dry_run: bool = False,
    limit: int | None = None,
) -> MigrationSummary:
    match = _REPORT_COLLECTION_RE.fullmatch(collection)
    if not match:
        raise ValueError(f"Not a monthly report collection: {collection}")
    month_key = f"{match.group(1)}-{match.group(2)}"
    summary = MigrationSummary()
    query = {"file.data": {"$exists": True}, "file.drive_id": {"$exists": False}}
    cursor = db[collection].find(query, {"file": 1})
    async for doc in cursor:
        if limit is not None and summary.selected >= limit:
            break
        summary.selected += 1
        if dry_run:
            continue
        file_doc = doc["file"]
        data = bytes(file_doc["data"])
        drive_id = None
        try:
            drive_id = await store.upload_file(file_doc["name"], file_doc["content_type"], data, month_key)
            downloaded = await store.download_file(drive_id)
            content_hash = hashlib.sha256(data).hexdigest()
            if len(downloaded) != int(file_doc["size"]) or hashlib.sha256(downloaded).hexdigest() != content_hash:
                raise ValueError("Drive verification did not match source bytes")
        except Exception as exc:
            summary.failed += 1
            summary.failures.append(f"{collection}/{doc['_id']}: {type(exc).__name__}")
            if drive_id:
                try:
                    await store.delete_file(drive_id)
                except Exception:
                    summary.failures.append(f"{collection}/{doc['_id']}: cleanup failed")
            continue
        try:
            result = await db[collection].update_one(
                {
                    "_id": doc["_id"],
                    "file.data": {"$exists": True},
                    "file.drive_id": {"$exists": False},
                },
                {
                    "$set": {
                        "file.storage": "drive",
                        "file.drive_id": drive_id,
                        "file.content_sha256": content_hash,
                    },
                    "$unset": {"file.data": ""},
                },
            )
        except Exception as exc:
            summary.failed += 1
            summary.failures.append(f"{collection}/{doc['_id']}: {type(exc).__name__}")
            continue
        if result.matched_count:
            summary.migrated += 1
        else:
            try:
                await store.delete_file(drive_id)
                summary.skipped += 1
            except Exception as exc:
                summary.failed += 1
                summary.failures.append(f"{collection}/{doc['_id']}: {type(exc).__name__}")
    return summary


async def run_migration(
    db,
    store: DriveFileStore,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    collection: str | None = None,
) -> MigrationSummary:
    names = [collection] if collection else sorted(await db.list_collection_names())
    names = [name for name in names if _REPORT_COLLECTION_RE.fullmatch(name)]
    total = MigrationSummary()
    remaining = limit
    for name in names:
        summary = await migrate_collection(db, store, name, dry_run=dry_run, limit=remaining)
        total.selected += summary.selected
        total.migrated += summary.migrated
        total.skipped += summary.skipped
        total.failed += summary.failed
        total.failures.extend(summary.failures)
        if remaining is not None:
            remaining -= summary.selected
            if remaining <= 0:
                break
    return total


async def _main(args: argparse.Namespace) -> int:
    settings = Settings()
    if not all(
        [
            settings.gdrive_client_id,
            settings.gdrive_client_secret,
            settings.gdrive_refresh_token,
            settings.gdrive_root_folder_id,
        ]
    ):
        raise RuntimeError("All GDRIVE_* variables are required to run this migration.")
    client = create_client(settings)
    try:
        summary = await run_migration(
            get_database(client, settings),
            DriveFileStore.from_settings(settings),
            dry_run=args.dry_run,
            limit=args.limit,
            collection=args.collection,
        )
    finally:
        await client.close()
    action = "would migrate" if args.dry_run else "migrated"
    print(
        f"Selected: {summary.selected}; {action}: "
        f"{summary.selected if args.dry_run else summary.migrated}; "
        f"skipped: {summary.skipped}; failed: {summary.failed}"
    )
    for failure in summary.failures:
        print(f"FAILED {failure}")
    return 1 if summary.failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Count eligible documents without uploading")
    parser.add_argument("--limit", type=int, help="Maximum number of eligible documents to process")
    parser.add_argument("--collection", help="Only process one reports_YYYY_MM collection")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be greater than zero")
    if args.collection and not _REPORT_COLLECTION_RE.fullmatch(args.collection):
        parser.error("--collection must use the reports_YYYY_MM format")
    raise SystemExit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
