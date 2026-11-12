"""Core business analytics (PRD §10–12, §19).

Principles:
* Profit and cash are computed separately. Credit sales are revenue but not cash.
* Personal expenses and owner withdrawals never reduce operating profit.
* COGS uses weighted-average cost (WAC) when purchases carry product + quantity. When that
  isn't possible, we fall back to "purchases in the period" and label the result *estimated*.
* Every metric says whether it is calculated or estimated, and the result carries a confidence
  level with plain-language reasons.
* Provisional (unconfirmed) transactions are excluded unless explicitly requested.

Everything works off a `LedgerView`: the business's ledger loaded once per request, walked once
for WAC costing and stock. Other intelligence modules (inventory, customers, insights, health,
brief) reuse the same view, so a dashboard costs one ledger read. That is fine at SME scale; when
it stops being fine, the view is the single place to move into SQL / materialized views.
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from functools import cached_property
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Counterparty, Product, Transaction
from app.models.enums import Settlement, TransactionKind, TransactionStatus
from app.services.normalization import from_minor

K = TransactionKind
Basis = Literal["calculated", "estimated"]
Granularity = Literal["day", "week", "month"]
Level = Literal["high", "medium", "low"]

WAC_COVERAGE_FOR_CALCULATED = Decimal("0.95")


# --------------------------------------------------------------------------- WAC + stock walk


@dataclass
class StockState:
    """What the ledger says about one product's stock, as of the view's end date."""

    qty: Decimal = Decimal(0)  # estimated on hand
    cost_minor: Decimal = Decimal(0)  # total cost of qty on hand (WAC)
    purchased_qty: Decimal = Decimal(0)
    sold_qty: Decimal = Decimal(0)
    unrecorded_qty: Decimal = Decimal(0)  # sold beyond recorded purchases
    first_purchase: date | None = None
    last_purchase: date | None = None
    last_sale: date | None = None

    @property
    def avg_cost_minor(self) -> Decimal | None:
        return self.cost_minor / self.qty if self.qty > 0 else None


@dataclass
class CostedSale:
    txn: Transaction
    cogs_minor: Decimal | None  # None = no cost basis


def walk(txns: list[Transaction]) -> tuple[list[CostedSale], dict[uuid.UUID, StockState]]:
    """Walk the ledger in date order, maintaining weighted-average cost and stock per product."""
    stock: dict[uuid.UUID, StockState] = defaultdict(StockState)
    costed: list[CostedSale] = []
    for t in txns:
        if t.kind == K.PURCHASE and t.product_id and t.quantity and t.quantity > 0:
            s = stock[t.product_id]
            s.qty += t.quantity
            s.cost_minor += Decimal(t.amount_minor)
            s.purchased_qty += t.quantity
            s.first_purchase = s.first_purchase or t.occurred_on
            s.last_purchase = t.occurred_on
        elif t.kind == K.SALE:
            cogs = None
            if t.product_id and t.quantity and t.quantity > 0:
                s = stock[t.product_id]
                s.sold_qty += t.quantity
                s.last_sale = t.occurred_on
                if s.qty >= t.quantity:
                    cogs = s.cost_minor / s.qty * t.quantity
                    s.qty -= t.quantity
                    s.cost_minor -= cogs
                else:
                    # Selling more than we know was bought: cost basis incomplete.
                    s.unrecorded_qty += t.quantity - s.qty
                    s.qty = Decimal(0)
                    s.cost_minor = Decimal(0)
            costed.append(CostedSale(t, cogs))
    return costed, dict(stock)


def cost_sales(txns: list[Transaction]) -> list[CostedSale]:
    return walk(txns)[0]


# --------------------------------------------------------------------------- ledger view


class LedgerView:
    """A business's ledger up to `until`, loaded once and shared across analytics."""

    def __init__(
        self,
        db: Session,
        business_id: uuid.UUID,
        until: date,
        include_provisional: bool = False,
    ):
        self.db = db
        self.business_id = business_id
        self.until = until
        self.include_provisional = include_provisional
        statuses = [TransactionStatus.CONFIRMED]
        if include_provisional:
            statuses.append(TransactionStatus.PROVISIONAL)
        self.txns: list[Transaction] = list(
            db.scalars(
                select(Transaction)
                .where(
                    Transaction.business_id == business_id,
                    Transaction.occurred_on <= until,
                    Transaction.status.in_(statuses),
                )
                .order_by(Transaction.occurred_on, Transaction.created_at)
            )
        )
        self.costed, self.stock = walk(self.txns)

    def between(self, start: date, end: date) -> list[Transaction]:
        return [t for t in self.txns if start <= t.occurred_on <= end]

    def provisional_in(self, start: date, end: date) -> tuple[int, int]:
        """Count and value of provisional transactions this view is *excluding*."""
        if self.include_provisional:
            return 0, 0
        amounts = self.db.scalars(
            select(Transaction.amount_minor).where(
                Transaction.business_id == self.business_id,
                Transaction.status == TransactionStatus.PROVISIONAL,
                Transaction.occurred_on.between(start, end),
            )
        ).all()
        return len(amounts), sum(amounts)

    @cached_property
    def products(self) -> dict[uuid.UUID, Product]:
        return {
            p.id: p
            for p in self.db.scalars(select(Product).where(Product.business_id == self.business_id))
        }

    @cached_property
    def counterparties(self) -> dict[uuid.UUID, Counterparty]:
        return {
            c.id: c
            for c in self.db.scalars(
                select(Counterparty).where(Counterparty.business_id == self.business_id)
            )
        }


# --------------------------------------------------------------------------- helpers


@dataclass
class Metric:
    value: Decimal
    basis: Basis = "calculated"

    def as_dict(self) -> dict:
        return {"value": str(self.value), "basis": self.basis}


def money(minor: Decimal | int) -> Decimal:
    """Minor units (possibly fractional after WAC division) -> naira, 2dp."""
    return from_minor(int(Decimal(minor).quantize(Decimal(1))))


def _m(minor: Decimal | int, basis: Basis = "calculated") -> Metric:
    return Metric(money(minor), basis)


@dataclass
class Confidence:
    level: Level = "high"
    reasons: list[str] = field(default_factory=list)

    def lower(self, to: Literal["medium", "low"], reason: str) -> None:
        order = {"high": 2, "medium": 1, "low": 0}
        if order[to] < order[self.level]:
            self.level = to
        self.reasons.append(reason)

    def as_dict(self) -> dict:
        return {"level": self.level, "reasons": self.reasons}


def pct(part: Decimal, whole: Decimal) -> Decimal | None:
    if whole == 0:
        return None
    return (Decimal(part) / Decimal(whole) * 100).quantize(Decimal("0.1"), ROUND_HALF_UP)


def change(current: Decimal, previous: Decimal) -> Decimal | None:
    if previous == 0:
        return None
    return ((Decimal(current) - previous) / abs(Decimal(previous)) * 100).quantize(
        Decimal("0.1"), ROUND_HALF_UP
    )


def _month_end(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def previous_period(start: date, end: date) -> tuple[date, date]:
    """Whole calendar months compare to the same number of preceding months (Oct -> Sep);
    any other range compares to the same number of days immediately before it."""
    if start.day == 1 and end == _month_end(end):
        months = (end.year - start.year) * 12 + end.month - start.month + 1
        prev_end = start - timedelta(days=1)
        prev_start = prev_end.replace(day=1)
        for _ in range(months - 1):
            prev_start = (prev_start - timedelta(days=1)).replace(day=1)
        return prev_start, prev_end
    length = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    return prev_end - timedelta(days=length - 1), prev_end


# --------------------------------------------------------------------------- period totals


@dataclass
class Totals:
    revenue: Decimal = Decimal(0)
    revenue_with_cost: Decimal = Decimal(0)
    wac_cogs: Decimal = Decimal(0)
    purchases: Decimal = Decimal(0)
    opex: Decimal = Decimal(0)
    other_income: Decimal = Decimal(0)
    personal: Decimal = Decimal(0)
    withdrawals: Decimal = Decimal(0)
    contributions: Decimal = Decimal(0)
    cash_in: Decimal = Decimal(0)
    cash_out: Decimal = Decimal(0)
    count: int = 0


def period_totals(view: LedgerView, start: date, end: date) -> Totals:
    t = Totals()
    cogs_by_id = {c.txn.id: c.cogs_minor for c in view.costed}
    for x in view.between(start, end):
        t.count += 1
        amt = Decimal(x.amount_minor)
        paid = x.settlement == Settlement.PAID
        match x.kind:
            case K.SALE:
                t.revenue += amt
                cogs = cogs_by_id.get(x.id)
                if cogs is not None:
                    t.revenue_with_cost += amt
                    t.wac_cogs += cogs
                if paid:
                    t.cash_in += amt
            case K.PURCHASE:
                t.purchases += amt
                if paid:
                    t.cash_out += amt
            case K.EXPENSE:
                t.opex += amt
                if paid:
                    t.cash_out += amt
            case K.OTHER_INCOME:
                t.other_income += amt
                if paid:
                    t.cash_in += amt
            case K.PERSONAL_EXPENSE:
                t.personal += amt
                t.cash_out += amt
            case K.OWNER_WITHDRAWAL:
                t.withdrawals += amt
                t.cash_out += amt
            case K.OWNER_CONTRIBUTION:
                t.contributions += amt
                t.cash_in += amt
            case K.CUSTOMER_PAYMENT:
                t.cash_in += amt
            case K.SUPPLIER_PAYMENT:
                t.cash_out += amt
    return t


@dataclass
class Profit:
    cogs: Decimal
    gross: Decimal
    net: Decimal
    method: str
    basis: Basis
    coverage: Decimal


def profit(t: Totals, conf: Confidence | None = None) -> Profit:
    """Pick the COGS method and compute profit lines, recording why if estimated."""
    coverage = t.revenue_with_cost / t.revenue if t.revenue else Decimal(1)
    basis: Basis
    if t.revenue == 0:
        cogs, method, basis = Decimal(0), "none", "calculated"
    elif coverage >= WAC_COVERAGE_FOR_CALCULATED:
        # Scale up the small uncovered remainder at the same cost ratio.
        cogs = t.wac_cogs / coverage
        method = "weighted_average_cost"
        basis = "calculated" if coverage == 1 else "estimated"
    elif t.purchases > 0:
        cogs, method, basis = t.purchases, "purchases_in_period", "estimated"
        if conf:
            conf.lower(
                "medium",
                "Cost of goods sold is estimated from purchases made in this period, because "
                "not all sales can be matched to purchase costs. Recording product and quantity "
                "on sales and purchases makes profit exact.",
            )
    else:
        cogs, method, basis = Decimal(0), "unknown", "estimated"
        if conf:
            conf.lower(
                "low",
                "No purchase costs were recorded, so profit may be overstated. Add your stock "
                "purchases to see real profit.",
            )
    gross = t.revenue - cogs
    return Profit(cogs, gross, gross + t.other_income - t.opex, method, basis, coverage)


def receivables_payables(view: LedgerView, end: date) -> tuple[Decimal, Decimal]:
    recv = pay = Decimal(0)
    for x in view.txns:
        if x.occurred_on > end:
            continue
        amt = Decimal(x.amount_minor)
        if x.settlement == Settlement.CREDIT and x.kind == K.SALE:
            recv += amt
        elif x.kind == K.CUSTOMER_PAYMENT:
            recv -= amt
        elif x.settlement == Settlement.CREDIT and x.kind in (K.PURCHASE, K.EXPENSE):
            pay += amt
        elif x.kind == K.SUPPLIER_PAYMENT:
            pay -= amt
    return max(recv, Decimal(0)), max(pay, Decimal(0))


# --------------------------------------------------------------------------- overview


def overview_from(view: LedgerView, start: date, end: date) -> dict:
    prev_start, prev_end = previous_period(start, end)
    conf = Confidence()

    cur = period_totals(view, start, end)
    prev = period_totals(view, prev_start, prev_end)
    p = profit(cur, conf)
    pp = profit(prev)
    recv, pay = receivables_payables(view, end)

    if cur.count == 0:
        conf.lower("low", "No transactions recorded in this period.")
    elif cur.count < 10:
        conf.lower("medium", f"Only {cur.count} transactions in this period.")

    prov_count, prov_amount = view.provisional_in(start, end)
    if prov_count:
        activity = cur.revenue + cur.opex + cur.purchases
        share = Decimal(prov_amount) / activity if activity else Decimal(1)
        conf.lower(
            "low" if share > Decimal("0.25") else "medium",
            f"{prov_count} unconfirmed transaction(s) worth "
            f"₦{from_minor(prov_amount):,} are not included. Confirm them to include them.",
        )

    withdrawn = cur.personal + cur.withdrawals
    return {
        "period": {"start": start, "end": end},
        "previous_period": {"start": prev_start, "end": prev_end},
        "revenue": _m(cur.revenue).as_dict(),
        "cost_of_goods_sold": {**_m(p.cogs, p.basis).as_dict(), "method": p.method},
        "gross_profit": _m(p.gross, p.basis).as_dict(),
        "gross_margin_pct": pct(p.gross, cur.revenue),
        "operating_expenses": _m(cur.opex).as_dict(),
        "other_income": _m(cur.other_income).as_dict(),
        "net_profit": _m(p.net, p.basis).as_dict(),
        "cash_in": _m(cur.cash_in).as_dict(),
        "cash_out": _m(cur.cash_out).as_dict(),
        "net_cash_flow": _m(cur.cash_in - cur.cash_out).as_dict(),
        "receivables": _m(recv).as_dict(),
        "payables": _m(pay).as_dict(),
        "personal_expenses": _m(cur.personal).as_dict(),
        "owner_withdrawals": _m(cur.withdrawals).as_dict(),
        "owner_contributions": _m(cur.contributions).as_dict(),
        # PRD §12: profit generated vs. money taken out for personal use.
        "retained_in_business": _m(p.net - withdrawn, p.basis).as_dict(),
        "previous": {
            "revenue": str(money(prev.revenue)),
            "operating_expenses": str(money(prev.opex)),
            "net_profit": str(money(pp.net)),
            "gross_margin_pct": pct(pp.gross, prev.revenue),
            "gross_margin_basis": pp.basis,
        },
        "change_vs_previous_pct": {
            "revenue": change(cur.revenue, prev.revenue),
            "operating_expenses": change(cur.opex, prev.opex),
            "net_profit": change(p.net, pp.net),
            "personal_and_withdrawals": change(withdrawn, prev.personal + prev.withdrawals),
        },
        "confidence": conf.as_dict(),
        "data": {
            "transactions": cur.count,
            "provisional_excluded": prov_count,
            "provisional_amount": str(from_minor(prov_amount)),
            "cost_coverage_pct": pct(p.coverage, Decimal(1)),
        },
    }


# --------------------------------------------------------------------------- trends


def _bucket(d: date, g: Granularity) -> date:
    if g == "day":
        return d
    if g == "week":
        return d - timedelta(days=d.weekday())
    return d.replace(day=1)


def trends_from(view: LedgerView, start: date, end: date, granularity: Granularity) -> dict:
    buckets: dict[date, defaultdict] = defaultdict(lambda: defaultdict(Decimal))
    for t in view.between(start, end):
        b = buckets[_bucket(t.occurred_on, granularity)]
        amt = Decimal(t.amount_minor)
        if t.kind == K.SALE:
            b["revenue"] += amt
        elif t.kind == K.EXPENSE:
            b["operating_expenses"] += amt
        elif t.kind == K.PURCHASE:
            b["purchases"] += amt
        elif t.kind in (K.PERSONAL_EXPENSE, K.OWNER_WITHDRAWAL):
            b["personal_and_withdrawals"] += amt
        paid = t.settlement == Settlement.PAID
        if (t.kind in (K.SALE, K.OTHER_INCOME) and paid) or t.kind in (
            K.OWNER_CONTRIBUTION,
            K.CUSTOMER_PAYMENT,
        ):
            b["net_cash_flow"] += amt
        elif (t.kind in (K.PURCHASE, K.EXPENSE) and paid) or t.kind in (
            K.PERSONAL_EXPENSE,
            K.OWNER_WITHDRAWAL,
            K.SUPPLIER_PAYMENT,
        ):
            b["net_cash_flow"] -= amt

    # Emit every bucket in range (including empty ones) so charts don't skip gaps.
    keys = (
        "revenue",
        "operating_expenses",
        "purchases",
        "personal_and_withdrawals",
        "net_cash_flow",
    )
    series = []
    cursor = _bucket(start, granularity)
    while cursor <= end:
        b = buckets.get(cursor, {})
        series.append({"period_start": cursor, **{k: str(money(b.get(k, 0))) for k in keys}})
        if granularity == "day":
            cursor += timedelta(days=1)
        elif granularity == "week":
            cursor += timedelta(days=7)
        else:
            cursor = _month_end(cursor) + timedelta(days=1)
    return {"granularity": granularity, "series": series}


# --------------------------------------------------------------------------- products


def product_performance_from(view: LedgerView, start: date, end: date) -> dict:
    prev_start, prev_end = previous_period(start, end)
    rows: dict[uuid.UUID, dict] = defaultdict(
        lambda: {
            "revenue": Decimal(0),
            "qty": Decimal(0),
            "cogs": Decimal(0),
            "costed_revenue": Decimal(0),
            "prev_revenue": Decimal(0),
            "sales": 0,
        }
    )
    unassigned = Decimal(0)
    for c in view.costed:
        t = c.txn
        if t.product_id is None:
            if start <= t.occurred_on <= end:
                unassigned += t.amount_minor
            continue
        r = rows[t.product_id]
        if prev_start <= t.occurred_on <= prev_end:
            r["prev_revenue"] += t.amount_minor
        if not (start <= t.occurred_on <= end):
            continue
        r["revenue"] += t.amount_minor
        r["sales"] += 1
        r["qty"] += t.quantity or 0
        if c.cogs_minor is not None:
            r["cogs"] += c.cogs_minor
            r["costed_revenue"] += t.amount_minor

    products = []
    for pid, r in rows.items():
        if r["revenue"] == 0 and r["prev_revenue"] == 0:
            continue
        product = view.products.get(pid)
        fully_costed = r["revenue"] > 0 and r["costed_revenue"] == r["revenue"]
        gp = r["revenue"] - r["cogs"] if fully_costed else None
        products.append(
            {
                "product_id": pid,
                "name": product.name if product else "Unknown",
                "unit": product.unit if product else None,
                "revenue": str(money(r["revenue"])),
                "previous_revenue": str(money(r["prev_revenue"])),
                "quantity_sold": str(r["qty"]),
                "sales_count": r["sales"],
                "gross_profit": str(money(gp)) if gp is not None else None,
                "gross_margin_pct": pct(gp, r["revenue"]) if gp is not None else None,
                "cost_basis": "calculated" if fully_costed else "missing",
                "revenue_change_pct": change(r["revenue"], r["prev_revenue"]),
            }
        )
    products.sort(key=lambda x: Decimal(x["revenue"]), reverse=True)
    return {
        "period": {"start": start, "end": end},
        "products": [p for p in products if Decimal(p["revenue"]) > 0],
        # Products that sold last period but not at all this period.
        "stopped_selling": [p for p in products if Decimal(p["revenue"]) == 0],
        "revenue_without_product": str(money(unassigned)),
    }


# --------------------------------------------------------------------------- expenses


def expense_breakdown_from(view: LedgerView, start: date, end: date) -> dict:
    prev_start, prev_end = previous_period(start, end)
    cur: dict[str, Decimal] = defaultdict(Decimal)
    prev: dict[str, Decimal] = defaultdict(Decimal)
    for t in view.txns:
        if t.kind != K.EXPENSE:
            continue
        cat = t.category or "uncategorized"
        if start <= t.occurred_on <= end:
            cur[cat] += t.amount_minor
        elif prev_start <= t.occurred_on <= prev_end:
            prev[cat] += t.amount_minor
    total = sum(cur.values(), Decimal(0))
    categories = [
        {
            "category": cat,
            "amount": str(money(amt)),
            "share_pct": pct(amt, total),
            "previous_amount": str(money(prev.get(cat, 0))),
            "change_pct": change(amt, prev.get(cat, Decimal(0))),
        }
        for cat, amt in sorted(cur.items(), key=lambda kv: kv[1], reverse=True)
    ]
    return {
        "period": {"start": start, "end": end},
        "total": str(money(total)),
        "previous_total": str(money(sum(prev.values(), Decimal(0)))),
        "categories": categories,
    }


# --------------------------------------------------------------------------- db-facing wrappers


def overview(db, business_id, start, end, include_provisional=False) -> dict:
    return overview_from(LedgerView(db, business_id, end, include_provisional), start, end)


def trends(db, business_id, start, end, granularity="month", include_provisional=False) -> dict:
    view = LedgerView(db, business_id, end, include_provisional)
    return trends_from(view, start, end, granularity)


def product_performance(db, business_id, start, end, include_provisional=False) -> dict:
    view = LedgerView(db, business_id, end, include_provisional)
    return product_performance_from(view, start, end)


def expense_breakdown(db, business_id, start, end, include_provisional=False) -> dict:
    view = LedgerView(db, business_id, end, include_provisional)
    return expense_breakdown_from(view, start, end)
