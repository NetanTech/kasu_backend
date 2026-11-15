"""Weekly Business Brief (PRD §18) and the dashboard payload (PRD §21)."""

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.services.analytics import LedgerView, trends_from
from app.services.intelligence import (
    Snapshot,
    build_snapshot,
    health_from,
    insights_from,
)


def public(section: dict) -> dict:
    """Drop internal keys (prefixed with '_') before returning a section from the API."""
    return {k: v for k, v in section.items() if not k.startswith("_")}


# A brief needs at least this much in the week to say anything trustworthy (KASU-030).
MIN_WEEK_TRANSACTIONS = 3


def _signed(pct: Decimal) -> str:
    return f"up {pct}%" if pct >= 0 else f"down {abs(pct)}%"


def _line(
    text: str,
    metric: str,
    confidence: str,
    *,
    change_pct=None,
    basis: str = "calculated",
    **evidence,
) -> dict:
    """One brief insight: what changed, the evidence behind it, and how sure we are."""
    return {
        "text": text,
        "metric": metric,
        "change_pct": change_pct,
        "evidence": {k: str(v) if isinstance(v, Decimal) else v for k, v in evidence.items()},
        "basis": basis,
        "confidence": confidence,
    }


def _at_most(level: str, cap: str) -> str:
    order = ["low", "medium", "high"]
    return order[min(order.index(level), order.index(cap))]


def _brief_lines(s: Snapshot) -> list[dict]:
    o = s.overview
    conf = o["confidence"]["level"]
    lines = []

    revenue = Decimal(o["revenue"]["value"])
    rev_chg = o["change_vs_previous_pct"]["revenue"]
    if revenue or rev_chg is not None:
        text = f"Sales were ₦{revenue:,.0f}"
        text += f", {_signed(Decimal(rev_chg))} on the week before." if rev_chg is not None else "."
        lines.append(
            _line(
                text,
                "revenue",
                conf,
                change_pct=rev_chg,
                revenue=revenue,
                previous_revenue=o["previous"]["revenue"],
            )
        )

    margin, prev_margin = o["gross_margin_pct"], o["previous"]["gross_margin_pct"]
    if (
        margin is not None
        and prev_margin is not None
        and o["gross_profit"]["basis"] == "calculated"
        and abs(Decimal(margin) - Decimal(prev_margin)) >= 1
    ):
        verb = "rose" if Decimal(margin) > Decimal(prev_margin) else "fell"
        lines.append(
            _line(
                f"Gross margin {verb} from {prev_margin}% to {margin}%.",
                "gross_margin_pct",
                conf,
                gross_margin_pct=margin,
                previous_gross_margin_pct=prev_margin,
                change_points=Decimal(margin) - Decimal(prev_margin),
            )
        )

    net = Decimal(o["net_profit"]["value"])
    if revenue:
        estimated = o["net_profit"]["basis"] == "estimated"
        lines.append(
            _line(
                f"{'Estimated profit' if estimated else 'Profit'}: "
                f"{'-' if net < 0 else ''}₦{abs(net):,.0f}.",
                "net_profit",
                # An estimated profit is never reported as high confidence.
                _at_most(conf, "medium") if estimated else conf,
                change_pct=o["change_vs_previous_pct"]["net_profit"],
                basis=o["net_profit"]["basis"],
                net_profit=net,
                previous_net_profit=o["previous"]["net_profit"],
            )
        )

    movers = [
        c
        for c in s.expenses["categories"]
        if c["change_pct"] is not None and abs(Decimal(c["change_pct"])) >= 10
    ]
    if movers:
        c = max(movers, key=lambda c: abs(Decimal(c["amount"]) - Decimal(c["previous_amount"])))
        verb = "increased" if Decimal(c["change_pct"]) > 0 else "decreased"
        lines.append(
            _line(
                f"{c['category'].replace('_', ' ').capitalize()} expenses {verb} "
                f"{abs(Decimal(c['change_pct']))}%.",
                "expense_category",
                conf,
                change_pct=c["change_pct"],
                category=c["category"],
                amount=c["amount"],
                previous_amount=c["previous_amount"],
            )
        )

    fast = [i for i in s.inventory["_items"] if i.movement == "fast" and i.days_left is not None]
    if fast:
        i = max(fast, key=lambda i: i.revenue_in_window)
        counted = i.stock_basis == "counted"
        lines.append(
            _line(
                f"{i.name}, your fastest-selling product, has about "
                f"{i.days_left.quantize(Decimal('1'))} days of stock left.",
                "days_of_stock",
                # Stock worked out from records alone is an estimate until someone counts it.
                conf if counted else _at_most(conf, "medium"),
                basis="calculated" if counted else "estimated",
                product=i.name,
                days_left=i.days_left.quantize(Decimal("0.1")),
                stock_basis=i.stock_basis,
            )
        )

    withdrawn = Decimal(o["personal_expenses"]["value"]) + Decimal(o["owner_withdrawals"]["value"])
    if withdrawn:
        lines.append(
            _line(
                f"₦{withdrawn:,.0f} was taken out for personal use.",
                "personal_and_withdrawals",
                conf,
                change_pct=o["change_vs_previous_pct"]["personal_and_withdrawals"],
                personal_expenses=o["personal_expenses"]["value"],
                owner_withdrawals=o["owner_withdrawals"]["value"],
            )
        )

    flow = Decimal(o["net_cash_flow"]["value"])
    if flow:
        lines.append(
            _line(
                f"Cash {'increased' if flow > 0 else 'decreased'} by ₦{abs(flow):,.0f}.",
                "net_cash_flow",
                conf,
                cash_in=o["cash_in"]["value"],
                cash_out=o["cash_out"]["value"],
                net_cash_flow=flow,
            )
        )
    return lines


def _insufficient_prompt(s: Snapshot) -> str | None:
    """Why this week can't support a brief, phrased as what to do about it; None if it can."""
    o = s.overview
    count = o["data"]["transactions"]
    pending = o["data"]["provisional_excluded"]
    has_sales = Decimal(o["revenue"]["value"]) > 0
    if count >= MIN_WEEK_TRANSACTIONS and has_sales:
        return None
    if count == 0:
        need = "No transactions were recorded this week."
    elif not has_sales:
        need = f"{count} transaction(s) were recorded this week, but no sales."
    else:
        need = f"Only {count} transaction(s) were recorded this week."
    ask = (
        f"Record at least {MIN_WEEK_TRANSACTIONS} transactions a week, including your sales, "
        "and your next brief will show how your business is doing."
    )
    if pending:
        ask += f" You also have {pending} unconfirmed transaction(s); confirm them to count them."
    return f"{need} {ask}"


def weekly_brief(
    db: Session, business_id, business_name: str, week_ending: date, include_provisional=False
) -> dict:
    """The 7 days ending on `week_ending`, compared with the 7 days before.

    `status` is "ready", or "insufficient_data" when the week is too thin to say anything
    reliable. Then there are no highlights or recommendations, only a `prompt` saying what to
    add, so a thin week never produces a misleading brief (KASU-030)."""
    start = week_ending - timedelta(days=6)
    view = LedgerView(db, business_id, week_ending, include_provisional)
    s = build_snapshot(view, start, week_ending)
    title = f"*{business_name}: your week ({start:%d %b} – {week_ending:%d %b})*"

    prompt = _insufficient_prompt(s)
    if prompt:
        return {
            "status": "insufficient_data",
            "period": {"start": start, "end": week_ending},
            "prompt": prompt,
            "highlights": [],
            "recommendations": [],
            "alerts": [],
            "health_score": {"score": None, "band": None},
            "confidence": s.overview["confidence"],
            "data": s.overview["data"],
            "text": "\n".join([title, "", prompt]),
        }

    insights = insights_from(s)
    health = health_from(s)
    highlights = _brief_lines(s)

    # Each recommendation keeps the evidence and confidence of the insight behind it.
    recommendations: list[dict] = []
    for i in insights:
        if not i.recommendation or i.severity == "positive":
            continue
        if any(r["text"] == i.recommendation for r in recommendations):
            continue
        recommendations.append(
            {
                "text": i.recommendation,
                "because": i.title,
                "code": i.code,
                "evidence": i.evidence,
                "basis": "recommended",
                "confidence": i.confidence,
            }
        )
    recommendations = recommendations[:3]

    text = [title, ""]
    text += [f"• {line['text']}" for line in highlights]
    if recommendations:
        text += ["", "*What to do next*"]
        text += [f"{n}. {r['text']}" for n, r in enumerate(recommendations, 1)]
    if health["score"] is not None:
        text += [
            "",
            f"Business Health Score: {health['score']}/100 ({health['band'].replace('_', ' ')})",
        ]
    if s.overview["confidence"]["level"] != "high":
        text += ["", "_Some numbers are estimates; see the app for details._"]

    return {
        "status": "ready",
        "period": {"start": start, "end": week_ending},
        "prompt": None,
        "highlights": highlights,
        "recommendations": recommendations,
        "alerts": [i.as_dict() for i in insights if i.severity in ("critical", "warning")],
        "health_score": {"score": health["score"], "band": health["band"]},
        "confidence": s.overview["confidence"],
        "data": s.overview["data"],
        # Ready to send over WhatsApp/SMS/email (WhatsApp-style *bold* / _italic_).
        "text": "\n".join(text),
    }


def dashboard(db: Session, business_id, start: date, end: date, include_provisional=False) -> dict:
    view = LedgerView(db, business_id, end, include_provisional)
    s = build_snapshot(view, start, end)
    insights = insights_from(s)
    o = s.overview
    trend_start = (end.replace(day=1) - timedelta(days=150)).replace(day=1)  # ~6 months
    return {
        "period": o["period"],
        "previous_period": o["previous_period"],
        "headline": {
            k: o[k]
            for k in (
                "revenue",
                "gross_profit",
                "gross_margin_pct",
                "operating_expenses",
                "net_profit",
                "net_cash_flow",
                "cash_in",
                "cash_out",
                "receivables",
                "payables",
            )
        },
        "change_vs_previous_pct": o["change_vs_previous_pct"],
        "business_vs_personal": {
            "net_profit": o["net_profit"],
            "personal_expenses": o["personal_expenses"],
            "owner_withdrawals": o["owner_withdrawals"],
            "retained_in_business": o["retained_in_business"],
        },
        "health": health_from(s),
        "alerts": [i.as_dict() for i in insights if i.severity in ("critical", "warning")][:5],
        "recommendations": [
            {"code": i.code, "title": i.title, "recommendation": i.recommendation}
            for i in insights
            if i.recommendation
        ][:5],
        "top_products": s.products["products"][:5],
        "top_expenses": s.expenses["categories"][:5],
        "inventory": public(s.inventory)["summary"],
        "customers": s.customers["summary"],
        "trend": trends_from(view, trend_start, end, "month")["series"],
        "inflation": {
            "available": False,
            "note": "Inflation comparison needs a few months of data and an inflation source.",
        },
        "confidence": o["confidence"],
        "data": o["data"],
    }
