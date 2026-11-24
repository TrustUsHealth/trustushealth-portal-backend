"""Run the Spectrum sync from the command line (same code as the API and the scheduler).

    python -m scripts.spectrum_sync --month 2026-10            # a whole month
    python -m scripts.spectrum_sync --from 2026-10-01 --to 2026-10-15
    python -m scripts.spectrum_sync --month 2026-10 --dry-run  # count only, save nothing
    python -m scripts.spectrum_sync                            # the daily window
    add --retry-failed to retry rows that already used all their attempts
"""

import argparse
import asyncio
import sys
from datetime import date

from app.core.config import ConfigError, Settings, load_config
from app.core.db import create_client, get_database
from app.core.events import EventBus
from app.core.filestore import create_filestore
from app.modules.patient_registry import events as pr_events
from app.modules.patient_registry import spectrum_sync
from app.partners.spectrum.sync_config import PortalSecrets, load_sync_config


async def _main(args: argparse.Namespace) -> int:
    settings = Settings()
    cfg = load_config(settings.config_dir)
    sync_cfg = load_sync_config(settings.config_dir)
    client = create_client(settings)
    try:
        db = get_database(client, settings)
        bus = EventBus()
        pr_events.register(bus)  # keeps the Excel register in step, as the API does
        ctx = spectrum_sync.SyncContext(
            db=db,
            cfg=cfg,
            sync_cfg=sync_cfg,
            secrets=PortalSecrets(),
            store=create_filestore(settings),
            bus=bus,
        )
        await spectrum_sync.ensure_indexes(db)
        d_from, d_to = spectrum_sync.resolve_window(
            cfg,
            sync_cfg,
            month=args.month,
            date_from=date.fromisoformat(args.date_from) if args.date_from else None,
            date_to=date.fromisoformat(args.date_to) if args.date_to else None,
        )
        print(f"Spectrum sync {d_from} to {d_to}{' (dry run)' if args.dry_run else ''} ...")
        run_id = await spectrum_sync.launch(
            ctx,
            spectrum_sync.RunParams(d_from, d_to, "cli", dry_run=args.dry_run, retry_failed=args.retry_failed),
            wait=True,
        )
        run = await db[spectrum_sync.RUNS].find_one({"_id": __import__("bson").ObjectId(run_id)})
        print(f"status: {run['status']}")
        if run.get("message"):
            print(f"message: {run['message']}")
        for key, value in run["stats"].items():
            print(f"  {key}: {value}")
        return 0 if run["status"] in ("completed", "completed_with_errors", "partial") else 1
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Spectrum portal visits into the patient registry.")
    parser.add_argument("--month", help="YYYY-MM, for example 2026-10")
    parser.add_argument("--from", dest="date_from", help="YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", help="YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args()
    try:
        sys.exit(asyncio.run(_main(args)))
    except (ConfigError, spectrum_sync.SyncInputError, spectrum_sync.SyncUnavailable, spectrum_sync.SyncBusy) as exc:
        sys.exit(f"Cannot run: {exc}")


if __name__ == "__main__":
    main()