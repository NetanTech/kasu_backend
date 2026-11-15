from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import BusinessContext, require
from app.core.db import get_db
from app.models.enums import Permission
from app.services import analytics
from app.services.customers import customers_from
from app.services.data_quality import data_quality
from app.services.intelligence import build_snapshot, health_from, insights_from
from app.services.inventory import inventory_from
from app.services.reports import dashboard, public, weekly_brief

router = APIRouter(prefix="/businesses/{business_id}/analytics", tags=["analytics"])
intel_router = APIRouter(prefix="/businesses/{business_id}", tags=["intelligence"])

MAX_RANGE_DAYS = 366 * 3


def _range(start: date | None, end: date | None) -> tuple[date, date]:
    """Default: the current calendar month to date."""
    end = end or date.today()
    start = start or end.replace(day=1)
    if start > end:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "start must be before end")
    if end - start > timedelta(days=MAX_RANGE_DAYS):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Date range is too long")
    return start, end


@router.get("/overview")
def overview(
    start: date | None = None,
    end: date | None = None,
    include_provisional: bool = False,
    ctx: BusinessContext = Depends(require(Permission.VIEW_FINANCIALS)),
    db: Session = Depends(get_db),
) -> dict:
    """Revenue, profit, cash flow and business-vs-personal for a period, with confidence."""
    s, e = _range(start, end)
    return {
        "currency": ctx.business.currency,
        **analytics.overview(db, ctx.id, s, e, include_provisional),
    }


@router.get("/trends")
def trends(
    start: date | None = None,
    end: date | None = None,
    granularity: Literal["day", "week", "month"] = "month",
    include_provisional: bool = False,
    ctx: BusinessContext = Depends(require(Permission.VIEW_FINANCIALS)),
    db: Session = Depends(get_db),
) -> dict:
    if start is None:
        end = end or date.today()
        start = (end.replace(day=1) - timedelta(days=150)).replace(day=1)  # ~6 months
    s, e = _range(start, end)
    if granularity == "day" and (e - s).days > 120:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Use week or month for long ranges"
        )
    return {
        "currency": ctx.business.currency,
        **analytics.trends(db, ctx.id, s, e, granularity, include_provisional),
    }


@router.get("/products")
def products(
    start: date | None = None,
    end: date | None = None,
    include_provisional: bool = False,
    ctx: BusinessContext = Depends(require(Permission.VIEW_FINANCIALS)),
    db: Session = Depends(get_db),
) -> dict:
    s, e = _range(start, end)
    return {
        "currency": ctx.business.currency,
        **analytics.product_performance(db, ctx.id, s, e, include_provisional),
    }


@router.get("/expenses")
def expenses(
    start: date | None = None,
    end: date | None = None,
    include_provisional: bool = Query(default=False),
    ctx: BusinessContext = Depends(require(Permission.VIEW_FINANCIALS)),
    db: Session = Depends(get_db),
) -> dict:
    s, e = _range(start, end)
    return {
        "currency": ctx.business.currency,
        **analytics.expense_breakdown(db, ctx.id, s, e, include_provisional),
    }


@router.get("/inventory")
def inventory(
    as_of: date | None = None,
    window_days: int = Query(default=30, ge=7, le=180),
    ctx: BusinessContext = Depends(require(Permission.VIEW_FINANCIALS)),
    db: Session = Depends(get_db),
) -> dict:
    """Estimated stock, days of stock left, fast/slow/dead stock and reorder suggestions."""
    as_of = as_of or date.today()
    view = analytics.LedgerView(db, ctx.id, as_of)
    return {"currency": ctx.business.currency, **public(inventory_from(view, as_of, window_days))}


@router.get("/customers")
def customers(
    start: date | None = None,
    end: date | None = None,
    ctx: BusinessContext = Depends(require(Permission.VIEW_FINANCIALS)),
    db: Session = Depends(get_db),
) -> dict:
    """New vs returning, top customers, dormant customers, concentration and who owes you."""
    s, e = _range(start, end)
    view = analytics.LedgerView(db, ctx.id, e)
    return {"currency": ctx.business.currency, **customers_from(view, s, e)}


@router.get("/data-quality")
def data_quality_view(
    start: date | None = None,
    end: date | None = None,
    ctx: BusinessContext = Depends(require(Permission.VIEW_FINANCIALS)),
    db: Session = Depends(get_db),
) -> dict:
    """How clean this business's records are: unresolved and unconfirmed records, and how
    often critical fields had to be corrected. Default: the last 90 days."""
    end = end or date.today()
    s, e = _range(start or end - timedelta(days=89), end)
    return data_quality(db, ctx.id, s, e)


# ------------------------------------------------------------------ intelligence


@intel_router.get("/insights")
def insights(
    start: date | None = None,
    end: date | None = None,
    include_provisional: bool = False,
    ctx: BusinessContext = Depends(require(Permission.VIEW_FINANCIALS)),
    db: Session = Depends(get_db),
) -> dict:
    """Alerts and recommendations: what happened, why, evidence, what to consider."""
    s, e = _range(start, end)
    snap = build_snapshot(analytics.LedgerView(db, ctx.id, e, include_provisional), s, e)
    return {
        "period": {"start": s, "end": e},
        "insights": [i.as_dict() for i in insights_from(snap)],
    }


@intel_router.get("/health-score")
def health_score(
    start: date | None = None,
    end: date | None = None,
    include_provisional: bool = False,
    ctx: BusinessContext = Depends(require(Permission.VIEW_FINANCIALS)),
    db: Session = Depends(get_db),
) -> dict:
    s, e = _range(start, end)
    snap = build_snapshot(analytics.LedgerView(db, ctx.id, e, include_provisional), s, e)
    return health_from(snap)


@intel_router.get("/brief")
def brief(
    week_ending: date | None = None,
    include_provisional: bool = False,
    ctx: BusinessContext = Depends(require(Permission.VIEW_FINANCIALS)),
    db: Session = Depends(get_db),
) -> dict:
    """Weekly Business Brief: the 7 days ending `week_ending` (default today)."""
    return {
        "currency": ctx.business.currency,
        **weekly_brief(
            db, ctx.id, ctx.business.name, week_ending or date.today(), include_provisional
        ),
    }


@intel_router.get("/dashboard")
def dashboard_view(
    start: date | None = None,
    end: date | None = None,
    include_provisional: bool = False,
    ctx: BusinessContext = Depends(require(Permission.VIEW_FINANCIALS)),
    db: Session = Depends(get_db),
) -> dict:
    """Everything the main dashboard needs in one call (PRD §21)."""
    s, e = _range(start, end)
    return {"currency": ctx.business.currency, **dashboard(db, ctx.id, s, e, include_provisional)}
