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


def _signed(pct: Decimal) -> str:
    return f"up {pct}%" if pct >= 0 else f"down {abs(pct)}%"


def _brief_lines(s: Snapshot) -> list[str]:
    o = s.overview
    lines = []

    revenue = Decimal(o["revenue"]["value"])
    rev_chg = o["change_vs_previous_pct"]["revenue"]
    if revenue or rev_chg is not None:
        line = f"Sales were ₦{revenue:,.0f}"
        line += f", {_signed(Decimal(rev_chg))} on the week before." if rev_chg is not None else "."
        lines.append(line)

    margin, prev_margin = o["gross_margin_pct"], o["previous"]["gross_margin_pct"]
    if (
        margin is not None
        and prev_margin is not None
        and o["gross_profit"]["basis"] == "calculated"
        and abs(Decimal(margin) - Decimal(prev_margin)) >= 1
    ):
        verb = "rose" if Decimal(margin) > Decimal(prev_margin) else "fell"
        lines.append(f"Gross margin {verb} from {prev_margin}% to {margin}%.")

    net = Decimal(o["net_profit"]["value"])
    if revenue:
        label = "Estimated profit" if o["net_profit"]["basis"] == "estimated" else "Profit"
        lines.append(f"{label}: {'-' if net < 0 else ''}₦{abs(net):,.0f}.")

    movers = [
        c
        for c in s.expenses["categories"]
        if c["change_pct"] is not None and abs(Decimal(c["change_pct"])) >= 10
    ]
    if movers:
        c = max(movers, key=lambda c: abs(Decimal(c["amount"]) - Decimal(c["previous_amount"])))
        verb = "increased" if Decimal(c["change_pct"]) > 0 else "decreased"
        lines.append(
            f"{c['category'].replace('_', ' ').capitalize()} expenses {verb} "
            f"{abs(Decimal(c['change_pct']))}%."
        )

    fast = [i for i in s.inventory["_items"] if i.movement == "fast" and i.days_left is not None]
    if fast:
        i = max(fast, key=lambda i: i.revenue_in_window)
        lines.append(
            f"{i.name}, your fastest-selling product, has about "
            f"{i.days_left.quantize(Decimal('1'))} days of stock left."
        )

    withdrawn = Decimal(o["personal_expenses"]["value"]) + Decimal(o["owner_withdrawals"]["value"])
    if withdrawn:
        lines.append(f"₦{withdrawn:,.0f} was taken out for personal use.")

    flow = Decimal(o["net_cash_flow"]["value"])
    if flow:
        lines.append(f"Cash {'increased' if flow > 0 else 'decreased'} by ₦{abs(flow):,.0f}.")
    return lines


def weekly_brief(
    db: Session, business_id, business_name: str, week_ending: date, include_provisional=False
) -> dict:
    """The 7 days ending on `week_ending`, compared with the 7 days before."""
    start = week_ending - timedelta(days=6)
    view = LedgerView(db, business_id, week_ending, include_provisional)
    s = build_snapshot(view, start, week_ending)
    insights = insights_from(s)
    health = health_from(s)

    highlights = _brief_lines(s)
    actions = [i.recommendation for i in insights if i.recommendation and i.severity != "positive"]
    recommendations = list(dict.fromkeys(actions))[:3]  # de-duplicate, keep order

    text = [f"*{business_name}: your week ({start:%d %b} – {week_ending:%d %b})*", ""]
    text += [f"• {line}" for line in highlights] or ["• No transactions recorded this week."]
    if recommendations:
        text += ["", "*What to do next*"] + [f"{n}. {r}" for n, r in enumerate(recommendations, 1)]
    if health["score"] is not None:
        text += [
            "",
            f"Business Health Score: {health['score']}/100 ({health['band'].replace('_', ' ')})",
        ]
    if s.overview["confidence"]["level"] != "high":
        text += ["", "_Some numbers are estimates; see the app for details._"]

    return {
        "period": {"start": start, "end": week_ending},
        "highlights": highlights,
        "recommendations": recommendations,
        "alerts": [i.as_dict() for i in insights if i.severity in ("critical", "warning")],
        "health_score": {"score": health["score"], "band": health["band"]},
        "confidence": s.overview["confidence"],
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
