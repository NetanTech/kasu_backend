"""Stored weekly briefs and the in-app notification inbox (KASU-030).

`GET /businesses/{id}/brief` (in analytics.py) still builds a brief on demand for any week;
the routes here serve the briefs the weekly job generated and delivered.
"""

import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.deps import BusinessContext, get_current_user, require
from app.core.db import get_db
from app.models import BusinessMember, Notification, User, WeeklyBrief
from app.models.base import utcnow
from app.models.enums import ROLE_PERMISSIONS, NotificationKind, Permission
from app.schemas.briefs import BriefSummaryOut, NotificationOut
from app.services import briefs

router = APIRouter(prefix="/businesses/{business_id}/briefs", tags=["intelligence"])
notifications_router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=list[BriefSummaryOut])
def list_briefs(
    limit: int = Query(default=12, ge=1, le=104),
    ctx: BusinessContext = Depends(require(Permission.VIEW_FINANCIALS)),
    db: Session = Depends(get_db),
) -> list[BriefSummaryOut]:
    """Past weekly briefs, newest first."""
    rows = db.execute(
        select(WeeklyBrief, Notification.opened_at)
        .outerjoin(
            Notification,
            (Notification.brief_id == WeeklyBrief.id) & (Notification.user_id == ctx.user.id),
        )
        .where(WeeklyBrief.business_id == ctx.id)
        .order_by(WeeklyBrief.week_end.desc())
        .limit(limit)
    ).all()
    return [
        BriefSummaryOut(
            id=b.id,
            week_start=b.week_start,
            week_end=b.week_end,
            status=b.status,
            created_at=b.created_at,
            opened_by_me=opened_at is not None,
        )
        for b, opened_at in rows
    ]


@router.get("/stats")
def brief_stats(
    since: date | None = None,
    ctx: BusinessContext = Depends(require(Permission.VIEW_FINANCIALS)),
    db: Session = Depends(get_db),
) -> dict:
    """How many delivered briefs were opened (open rate), overall and by brief status."""
    return briefs.open_rate(db, ctx.id, since)


@router.get("/{brief_id}")
def get_brief(
    brief_id: uuid.UUID,
    ctx: BusinessContext = Depends(require(Permission.VIEW_FINANCIALS)),
    db: Session = Depends(get_db),
) -> dict:
    """The brief as it was generated. Opening it counts towards the open rate."""
    brief = db.scalar(
        select(WeeklyBrief).where(WeeklyBrief.id == brief_id, WeeklyBrief.business_id == ctx.id)
    )
    if brief is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Brief not found")
    briefs.mark_brief_opened(db, brief.id, ctx.user.id)
    db.commit()
    return {
        "id": brief.id,
        "generated_at": brief.created_at,
        "currency": ctx.business.currency,
        **brief.content,
    }


# --------------------------------------------------------------------------- notifications


_FINANCIAL_ROLES = [
    r for r, perms in ROLE_PERMISSIONS.items() if Permission.VIEW_FINANCIALS in perms
]


def _visible(user: User):
    """The user's notifications for businesses they're still on, and only kinds their
    current role may see (a brief shows financials)."""
    return (
        select(Notification)
        .join(
            BusinessMember,
            (BusinessMember.business_id == Notification.business_id)
            & (BusinessMember.user_id == Notification.user_id),
        )
        .where(
            Notification.user_id == user.id,
            or_(
                Notification.kind != NotificationKind.WEEKLY_BRIEF,
                BusinessMember.role.in_(_FINANCIAL_ROLES),
            ),
        )
    )


@notifications_router.get("", response_model=list[NotificationOut])
def list_notifications(
    business_id: uuid.UUID | None = None,
    unopened_only: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[Notification]:
    """Your notifications across all your businesses, newest first."""
    stmt = _visible(user)
    if business_id:
        stmt = stmt.where(Notification.business_id == business_id)
    if unopened_only:
        stmt = stmt.where(Notification.opened_at.is_(None))
    return list(db.scalars(stmt.order_by(Notification.created_at.desc()).limit(limit)))


@notifications_router.post("/{notification_id}/open", response_model=NotificationOut)
def open_notification(
    notification_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Notification:
    """Mark a notification opened (e.g. when the user taps it)."""
    n = db.scalar(_visible(user).where(Notification.id == notification_id))
    if n is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notification not found")
    if n.brief_id is not None:
        briefs.mark_brief_opened(db, n.brief_id, user.id)
    elif n.opened_at is None:
        n.opened_at = utcnow()
    db.commit()
    db.refresh(n)
    return n
