"""Weekly brief generation, delivery and open tracking (KASU-030).

Once a week (see app.jobs.weekly_briefs, run from cron on Monday morning) every active business
gets a brief for the Monday–Sunday week that just ended:

* enough data      -> a "ready" brief: highlights, alerts, recommendations, confidence
* not enough data  -> an "insufficient_data" brief that only says what to record
                      (never a misleading brief built on a handful of transactions)

Each brief is stored as generated and delivered as an in-app notification to everyone on the
team who may see financials. Opening the brief (or its notification) stamps `opened_at`, which
is what the open rate is measured from. Generation is idempotent per business per week, so
re-running the job, or two runners racing, never sends a brief twice.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from fastapi.encoders import jsonable_encoder
from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Business, BusinessMember, Notification, Transaction, WeeklyBrief
from app.models.base import utcnow
from app.models.enums import (
    ROLE_PERMISSIONS,
    BriefStatus,
    NotificationKind,
    Permission,
    TransactionStatus,
)
from app.services.reports import weekly_brief

log = logging.getLogger("kasu.briefs")


def local_today() -> date:
    return datetime.now(ZoneInfo(get_settings().brief_timezone)).date()


def last_completed_week_end(today: date) -> date:
    """The most recent Sunday strictly before `today` (on a Sunday the week isn't over yet)."""
    return today - timedelta(days=today.isoweekday())


def active_business_ids(db: Session, week_end: date) -> list[uuid.UUID]:
    """Businesses that existed by the end of the week and have shown signs of life recently:
    created, or with a transaction dated, within `brief_active_days`. Long-dormant businesses
    aren't nagged every week."""
    since = week_end - timedelta(days=get_settings().brief_active_days)
    week_over = datetime.combine(week_end + timedelta(days=1), time.min, UTC)
    recent_activity = exists().where(
        Transaction.business_id == Business.id,
        Transaction.status != TransactionStatus.VOIDED,
        Transaction.occurred_on > since,
        Transaction.occurred_on <= week_end,
    )
    created_recently = Business.created_at >= datetime.combine(since, time.min, UTC)
    return list(
        db.scalars(
            select(Business.id)
            .where(Business.created_at < week_over, or_(recent_activity, created_recently))
            .order_by(Business.created_at)
        )
    )


def _recipients(db: Session, business_id: uuid.UUID) -> list[uuid.UUID]:
    rows = db.execute(
        select(BusinessMember.user_id, BusinessMember.role).where(
            BusinessMember.business_id == business_id
        )
    ).all()
    return [uid for uid, role in rows if Permission.VIEW_FINANCIALS in ROLE_PERMISSIONS[role]]


def _notification_text(business: Business, content: dict) -> tuple[str, str]:
    if content["status"] == BriefStatus.INSUFFICIENT_DATA:
        return "Add more records to get your weekly brief", content["prompt"]
    body = " ".join(line["text"] for line in content["highlights"][:2])
    return f"Your week at {business.name}"[:200], body or "Your weekly brief is ready."


def generate_brief(db: Session, business: Business, week_end: date) -> WeeklyBrief | None:
    """Generate, store and deliver one business's brief. None if it already exists."""
    already = db.scalar(
        select(WeeklyBrief.id).where(
            WeeklyBrief.business_id == business.id, WeeklyBrief.week_end == week_end
        )
    )
    if already:
        return None
    content = jsonable_encoder(weekly_brief(db, business.id, business.name, week_end))
    brief = WeeklyBrief(
        business_id=business.id,
        week_start=week_end - timedelta(days=6),
        week_end=week_end,
        status=BriefStatus(content["status"]),
        content=content,
    )
    db.add(brief)
    db.flush()
    title, body = _notification_text(business, content)
    for user_id in _recipients(db, business.id):
        db.add(
            Notification(
                business_id=business.id,
                user_id=user_id,
                kind=NotificationKind.WEEKLY_BRIEF,
                title=title,
                body=body,
                brief_id=brief.id,
            )
        )
    db.flush()
    return brief


@dataclass
class RunSummary:
    week_end: date
    businesses: int = 0
    ready: int = 0
    insufficient_data: int = 0
    already_generated: int = 0
    failed: list[str] = field(default_factory=list)


def run_weekly_briefs(db: Session, today: date | None = None) -> RunSummary:
    """Generate last week's brief for every active business. Each business is committed on
    its own, so one failure doesn't stop the rest."""
    week_end = last_completed_week_end(today or local_today())
    summary = RunSummary(week_end=week_end)
    for business_id in active_business_ids(db, week_end):
        summary.businesses += 1
        try:
            brief = generate_brief(db, db.get(Business, business_id), week_end)
            db.commit()
        except IntegrityError:
            db.rollback()  # another runner got there first
            brief = None
        except Exception:
            db.rollback()
            log.exception("weekly brief failed for business %s", business_id)
            summary.failed.append(str(business_id))
            continue
        if brief is None:
            summary.already_generated += 1
        elif brief.status == BriefStatus.READY:
            summary.ready += 1
        else:
            summary.insufficient_data += 1
    log.info("weekly briefs for week ending %s: %s", week_end, summary)
    return summary


def mark_brief_opened(db: Session, brief_id: uuid.UUID, user_id: uuid.UUID) -> None:
    db.execute(
        update(Notification)
        .where(
            Notification.brief_id == brief_id,
            Notification.user_id == user_id,
            Notification.opened_at.is_(None),
        )
        .values(opened_at=utcnow())
    )


def open_rate(db: Session, business_id: uuid.UUID | None = None, since: date | None = None) -> dict:
    """Share of delivered brief notifications that were opened. Platform-wide when
    `business_id` is None."""
    stmt = (
        select(
            WeeklyBrief.status,
            func.count(Notification.id),
            func.count(Notification.opened_at),
        )
        .join(WeeklyBrief, WeeklyBrief.id == Notification.brief_id)
        .where(Notification.kind == NotificationKind.WEEKLY_BRIEF)
        .group_by(WeeklyBrief.status)
    )
    if business_id is not None:
        stmt = stmt.where(Notification.business_id == business_id)
    if since is not None:
        stmt = stmt.where(WeeklyBrief.week_end >= since)

    def rate(opened: int, sent: int) -> Decimal | None:
        if not sent:
            return None
        return (Decimal(opened) / sent * 100).quantize(Decimal("0.1"), ROUND_HALF_UP)

    by_status = {s.value: {"delivered": 0, "opened": 0} for s in BriefStatus}
    for status, sent, opened in db.execute(stmt):
        by_status[status.value] = {"delivered": sent, "opened": opened}
    for row in by_status.values():
        row["open_rate_pct"] = rate(row["opened"], row["delivered"])
    sent = sum(r["delivered"] for r in by_status.values())
    opened = sum(r["opened"] for r in by_status.values())
    return {
        "since": since,
        "delivered": sent,
        "opened": opened,
        "open_rate_pct": rate(opened, sent),
        "by_status": by_status,
    }
