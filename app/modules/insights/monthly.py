"""Optional monthly email of last month's Business Report (`monthly_email`). Called by the 1st-of-month
scheduler after the Excel register job; idempotent through `last_report_month` kept in `module_state`."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.core.db import utcnow
from app.core.mailer import EmailAttachment, EmailDeliveryError, OutboundEmail
from app.core.rbac import STATE
from app.domain.excel_register import recipients
from app.modules.insights import report, service

KEY = "insights"


async def send_last_month(db, cfg, settings, mailer, *, now: datetime | None = None) -> dict[str, Any]:
    if not cfg.insights.report.monthly_email:
        return {"status": "disabled", "sent": 0}
    now = now or datetime.now(ZoneInfo(cfg.app.timezone))
    prev = service.previous_month_range(now.date())
    month_key = f"{prev.start.year}-{prev.start.month:02d}"
    to = recipients(cfg, settings)
    if not to:
        return {"status": "no_recipients", "month": month_key, "sent": 0}
    # `visible_to_staff` is written only because the Access Portal reads it from every module_state document.
    await db[STATE].update_one({"_id": KEY}, {"$setOnInsert": {"visible_to_staff": False}}, upsert=True)
    now_utc = utcnow()
    claim = await db[STATE].update_one(
        {
            "_id": KEY,
            "last_report_month": {"$ne": month_key},
            "$or": [{"report_lease_until": None}, {"report_lease_until": {"$lt": now_utc}}],
        },
        {"$set": {"report_lease_until": now_utc + timedelta(minutes=10)}},
    )
    doc = await db[STATE].find_one({"_id": KEY})
    if claim.matched_count == 0:
        done = doc.get("last_report_month") == month_key
        return {"status": "already_sent" if done else "busy", "month": month_key, "sent": 0}

    params = service.resolve_params(
        cfg, period="last_month", start=None, end=None, labs=None, compare="previous", today=now.date()
    )
    a = await service.analysis(db, cfg, params, refresh=True)
    pdf = await asyncio.to_thread(report.render, a, list(cfg.insights.report.sections), "Scheduled monthly job", cfg.app.timezone)
    already = set(doc.get("report_sent_to", [])) if doc.get("report_sent_month") == month_key else set()
    brand = cfg.app.brand.name
    sent = failed = 0
    for addr in [x for x in to if x not in already]:
        try:
            await mailer.send_email(
                OutboundEmail(
                    to=addr,
                    subject=f"{brand} business report: {params.range.start:%B %Y}",
                    text=f"The Business Report for {params.range.start:%B %Y} is attached.\n\n{brand}",
                    attachments=(EmailAttachment(report.file_name(a), pdf, "application/pdf"),),
                )
            )
        except EmailDeliveryError:
            failed += 1
            continue
        sent += 1
        await db[STATE].update_one({"_id": KEY}, {"$set": {"report_sent_month": month_key}, "$addToSet": {"report_sent_to": addr}})
    if failed:
        await db[STATE].update_one({"_id": KEY}, {"$unset": {"report_lease_until": ""}})
        return {"status": "partial", "month": month_key, "sent": sent, "failed": failed}
    await db[STATE].update_one(
        {"_id": KEY}, {"$set": {"last_report_month": month_key}, "$unset": {"report_lease_until": ""}}
    )
    return {"status": "sent", "month": month_key, "sent": sent}