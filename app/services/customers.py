"""Customer intelligence (PRD §10.5). Only sales with a named customer can be analysed."""

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.models.enums import Settlement, TransactionKind
from app.services.analytics import Confidence, LedgerView, money, pct, previous_period

K = TransactionKind

DORMANT_MIN_DAYS = 45
DORMANT_LOOKBACK_DAYS = 365


@dataclass
class _Cust:
    first: date | None = None
    last: date | None = None
    dates: list[date] = field(default_factory=list)
    lifetime_minor: Decimal = Decimal(0)
    period_minor: Decimal = Decimal(0)
    period_count: int = 0
    prev_minor: Decimal = Decimal(0)
    owing_minor: Decimal = Decimal(0)


def customers_from(view: LedgerView, start: date, end: date, top_n: int = 10) -> dict:
    prev_start, prev_end = previous_period(start, end)
    custs: dict[uuid.UUID, _Cust] = defaultdict(_Cust)
    total_revenue = Decimal(0)
    named_revenue = Decimal(0)

    for t in view.txns:
        if t.occurred_on > end:
            continue
        if t.kind == K.SALE:
            in_period = start <= t.occurred_on <= end
            if in_period:
                total_revenue += t.amount_minor
            if t.counterparty_id is None:
                continue
            c = custs[t.counterparty_id]
            c.first = c.first or t.occurred_on
            c.last = t.occurred_on
            c.dates.append(t.occurred_on)
            c.lifetime_minor += t.amount_minor
            if t.settlement == Settlement.CREDIT:
                c.owing_minor += t.amount_minor
            if in_period:
                named_revenue += t.amount_minor
                c.period_minor += t.amount_minor
                c.period_count += 1
            elif prev_start <= t.occurred_on <= prev_end:
                c.prev_minor += t.amount_minor
        elif t.kind == K.CUSTOMER_PAYMENT and t.counterparty_id:
            custs[t.counterparty_id].owing_minor -= t.amount_minor

    def name(cid: uuid.UUID) -> str:
        cp = view.counterparties.get(cid)
        return cp.name if cp else "Unknown"

    active = {cid: c for cid, c in custs.items() if c.period_count > 0}
    new = [cid for cid, c in active.items() if c.first and c.first >= start]
    returning = [cid for cid in active if cid not in new]

    ranked = sorted(active.items(), key=lambda kv: kv[1].period_minor, reverse=True)
    top = [
        {
            "customer_id": cid,
            "name": name(cid),
            "revenue": str(money(c.period_minor)),
            "share_pct": pct(c.period_minor, total_revenue),
            "purchases": c.period_count,
            "owing": str(money(max(c.owing_minor, Decimal(0)))),
            "is_new": cid in new,
        }
        for cid, c in ranked[:top_n]
    ]

    dormant = []
    for cid, c in custs.items():
        if c.period_count or len(c.dates) < 2 or c.last is None:
            continue
        days_since = (end - c.last).days
        if days_since > DORMANT_LOOKBACK_DAYS:
            continue
        span = (c.dates[-1] - c.dates[0]).days
        avg_gap = Decimal(span) / (len(c.dates) - 1)
        # Dormant = silent for clearly longer than their usual gap between purchases.
        if days_since >= max(DORMANT_MIN_DAYS, int(avg_gap * 2)):
            dormant.append(
                {
                    "customer_id": cid,
                    "name": name(cid),
                    "last_purchase": c.last,
                    "days_since_last_purchase": days_since,
                    "usual_days_between_purchases": str(avg_gap.quantize(Decimal("0.1"))),
                    "lifetime_revenue": str(money(c.lifetime_minor)),
                    "_lifetime_minor": c.lifetime_minor,
                }
            )
    dormant.sort(key=lambda d: d["_lifetime_minor"], reverse=True)
    for d in dormant:
        d.pop("_lifetime_minor")

    owing = sorted(
        (
            {"customer_id": cid, "name": name(cid), "owing": str(money(c.owing_minor))}
            for cid, c in custs.items()
            if c.owing_minor > 0
        ),
        key=lambda d: Decimal(d["owing"]),
        reverse=True,
    )

    conf = Confidence()
    coverage = named_revenue / total_revenue if total_revenue else Decimal(0)
    if total_revenue == 0:
        conf.lower("low", "No sales in this period.")
    elif coverage < Decimal("0.5"):
        conf.lower(
            "low",
            f"Only {pct(named_revenue, total_revenue)}% of sales have a customer name. "
            "Record who you sell to for better customer insight.",
        )
    elif coverage < Decimal("0.8"):
        conf.lower("medium", f"{pct(named_revenue, total_revenue)}% of sales have a customer name.")

    n_active = len(active)
    top1 = ranked[0][1].period_minor if ranked else Decimal(0)
    top3 = sum((c.period_minor for _, c in ranked[:3]), Decimal(0))
    period_sales = sum(c.period_count for c in active.values())
    return {
        "period": {"start": start, "end": end},
        "summary": {
            "active_customers": n_active,
            "new_customers": len(new),
            "returning_customers": len(returning),
            "returning_share_pct": pct(Decimal(len(returning)), Decimal(n_active)),
            "average_spend": str(money(named_revenue / n_active)) if n_active else None,
            "purchases_per_customer": (
                str((Decimal(period_sales) / n_active).quantize(Decimal("0.1")))
                if n_active
                else None
            ),
            "top_customer_share_pct": pct(top1, total_revenue),
            "top_3_share_pct": pct(top3, total_revenue),
            "dormant_customers": len(dormant),
            "total_owed_by_customers": str(
                money(sum((max(c.owing_minor, Decimal(0)) for c in custs.values()), Decimal(0)))
            ),
            "sales_with_customer_pct": pct(named_revenue, total_revenue),
        },
        "top_customers": top,
        "dormant_customers": dormant,
        "customers_owing": owing,
        "confidence": conf.as_dict(),
    }
