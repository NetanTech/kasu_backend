"""Inventory intelligence (PRD §10.6).

Stock is *derived* from recorded purchases and sales (there are no physical stock counts yet), so
every stock figure here is an estimate and is labelled as such.
"""

import math
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from app.models.enums import TransactionKind
from app.services.analytics import Confidence, LedgerView, money, pct

K = TransactionKind

StockStatus = Literal["out_of_stock", "low", "ok", "slow", "dead", "unknown"]
Movement = Literal["fast", "normal", "slow", "none"]

DEFAULT_WINDOW_DAYS = 30
REORDER_WHEN_DAYS_LEFT = 7
REORDER_COVER_DAYS = 14
SLOW_WHEN_DAYS_LEFT = 90
DEAD_AFTER_DAYS = 60
COUNT_FRESH_DAYS = 30


@dataclass
class ProductStock:
    product_id: uuid.UUID
    name: str
    unit: str | None
    on_hand: Decimal
    value_minor: Decimal
    avg_cost_minor: Decimal | None
    last_unit_cost_minor: Decimal | None
    sold_in_window: Decimal
    revenue_in_window: Decimal
    cogs_in_window: Decimal
    daily_rate: Decimal
    days_left: Decimal | None
    status: StockStatus
    movement: Movement
    last_sale: date | None
    last_purchase: date | None
    last_counted: date | None
    unrecorded_qty: Decimal
    reorder_qty: Decimal | None
    reorder_cost_minor: Decimal | None

    as_of: date | None = None

    @property
    def stock_basis(self) -> str:
        fresh = (
            self.last_counted is not None
            and self.as_of is not None
            and (self.as_of - self.last_counted).days <= COUNT_FRESH_DAYS
        )
        return "counted" if fresh and self.unrecorded_qty == 0 else "estimated"

    def as_dict(self) -> dict:
        return {
            "product_id": self.product_id,
            "name": self.name,
            "unit": self.unit,
            "estimated_on_hand": str(self.on_hand),
            "estimated_value": str(money(self.value_minor)),
            "average_cost": str(money(self.avg_cost_minor)) if self.avg_cost_minor else None,
            "last_purchase_unit_cost": (
                str(money(self.last_unit_cost_minor)) if self.last_unit_cost_minor else None
            ),
            "sold_in_window": str(self.sold_in_window),
            "daily_sales_rate": str(self.daily_rate.quantize(Decimal("0.01"))),
            "estimated_days_left": (
                str(self.days_left.quantize(Decimal("0.1"))) if self.days_left is not None else None
            ),
            "status": self.status,
            "movement": self.movement,
            "last_sale": self.last_sale,
            "last_purchase": self.last_purchase,
            "last_counted_on": self.last_counted,
            # "counted" = anchored on a physical count within COUNT_FRESH_DAYS, then adjusted
            # by recorded sales/purchases since; "estimated" = derived from records only.
            "stock_basis": self.stock_basis,
            "sold_beyond_recorded_stock": str(self.unrecorded_qty),
            "reorder": (
                {
                    "suggested_quantity": str(self.reorder_qty),
                    "estimated_cost": (
                        str(money(self.reorder_cost_minor)) if self.reorder_cost_minor else None
                    ),
                    "covers_days": REORDER_COVER_DAYS,
                }
                if self.reorder_qty
                else None
            ),
        }


def _status(
    s_qty: Decimal,
    known: bool,
    days_left: Decimal | None,
    daily: Decimal,
    last_sale: date | None,
    first_purchase: date | None,
    as_of: date,
) -> StockStatus:
    if not known:
        return "unknown"  # only sales recorded; we can't know what's on the shelf
    if s_qty <= 0:
        return "out_of_stock"
    idle_since = last_sale or first_purchase
    if idle_since and (as_of - idle_since).days >= DEAD_AFTER_DAYS:
        return "dead"
    if days_left is not None and days_left <= REORDER_WHEN_DAYS_LEFT:
        return "low"
    if daily == 0 or (days_left is not None and days_left > SLOW_WHEN_DAYS_LEFT):
        return "slow"
    return "ok"


def inventory_from(
    view: LedgerView, as_of: date | None = None, window_days: int = DEFAULT_WINDOW_DAYS
) -> dict:
    as_of = as_of or view.until
    window_start = as_of - timedelta(days=window_days - 1)

    sold: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
    revenue: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
    cogs: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
    for c in view.costed:
        t = c.txn
        if t.product_id and t.quantity and window_start <= t.occurred_on <= as_of:
            sold[t.product_id] += t.quantity
            revenue[t.product_id] += t.amount_minor
            if c.cogs_minor is not None:
                cogs[t.product_id] += c.cogs_minor

    last_unit_cost: dict[uuid.UUID, Decimal] = {}
    for t in view.txns:
        if t.kind == K.PURCHASE and t.product_id and t.quantity and t.quantity > 0:
            last_unit_cost[t.product_id] = Decimal(t.amount_minor) / t.quantity

    items: list[ProductStock] = []
    for pid, s in view.stock.items():
        first_activity = s.first_purchase or s.last_sale
        # A product first seen 10 days ago shouldn't have its rate diluted over 30 days.
        effective_days = window_days
        if first_activity and first_activity > window_start:
            effective_days = max((as_of - first_activity).days + 1, 1)
        daily = sold[pid] / effective_days
        known = s.purchased_qty > 0 or s.last_count is not None
        days_left = s.qty / daily if daily > 0 and known else None
        status = _status(s.qty, known, days_left, daily, s.last_sale, s.first_purchase, as_of)

        reorder_qty = reorder_cost = None
        if status in ("low", "out_of_stock") and daily > 0:
            need = daily * REORDER_COVER_DAYS - max(s.qty, Decimal(0))
            if need > 0:
                reorder_qty = Decimal(math.ceil(need))
                if pid in last_unit_cost:
                    reorder_cost = last_unit_cost[pid] * reorder_qty

        product = view.products.get(pid)
        items.append(
            ProductStock(
                product_id=pid,
                name=product.name if product else "Unknown",
                unit=product.unit if product else None,
                on_hand=s.qty,
                value_minor=s.cost_minor,
                avg_cost_minor=s.avg_cost_minor,
                last_unit_cost_minor=last_unit_cost.get(pid),
                sold_in_window=sold[pid],
                revenue_in_window=revenue[pid],
                cogs_in_window=cogs[pid],
                daily_rate=daily,
                days_left=days_left,
                status=status,
                movement="none",
                last_sale=s.last_sale,
                last_purchase=s.last_purchase,
                last_counted=s.last_count,
                unrecorded_qty=s.unrecorded_since_count,
                reorder_qty=reorder_qty,
                reorder_cost_minor=reorder_cost,
            )
        )

    # Movement: rank products that sold in the window by revenue; top quarter is fast-moving.
    selling = sorted(
        (i for i in items if i.revenue_in_window > 0),
        key=lambda i: i.revenue_in_window,
        reverse=True,
    )
    if selling:
        top_n = max(1, math.ceil(len(selling) / 4))
        for rank, item in enumerate(selling):
            if rank < top_n:
                item.movement = "fast"
            elif len(selling) >= 4 and rank >= len(selling) - top_n:
                item.movement = "slow"
            else:
                item.movement = "normal"

    conf = Confidence()
    for i in items:
        i.as_of = as_of
    uncounted = [i for i in items if i.stock_basis != "counted"]
    if uncounted:
        conf.reasons.append(
            f"Stock for {len(uncounted)} of {len(items)} product(s) is estimated from recorded "
            f"purchases and sales, with no physical count in the last {COUNT_FRESH_DAYS} days."
        )
    oversold = [i.name for i in items if i.unrecorded_qty > 0]
    if oversold:
        conf.lower(
            "medium",
            f"More was sold than recorded as bought for: {', '.join(sorted(oversold)[:5])}. "
            "Some purchases may be missing; a stock count will fix the estimate.",
        )
    unknown = [i for i in items if i.status == "unknown"]
    if unknown:
        conf.lower(
            "medium",
            f"{len(unknown)} product(s) have sales but no recorded purchases or stock count, "
            "so their stock can't be estimated.",
        )
    if not items:
        conf.lower("low", "No product quantities recorded yet.")

    total_value = sum((i.value_minor for i in items if i.on_hand > 0), Decimal(0))
    dead_value = sum((i.value_minor for i in items if i.status == "dead"), Decimal(0))
    window_cogs = sum((i.cogs_in_window for i in items), Decimal(0))
    days_of_inventory = (
        (total_value / (window_cogs / window_days)).quantize(Decimal("0.1"))
        if window_cogs > 0
        else None
    )
    order = {"out_of_stock": 0, "low": 1, "dead": 2, "slow": 3, "unknown": 4, "ok": 5}
    items.sort(key=lambda i: (order[i.status], -i.revenue_in_window))

    counts: dict[str, int] = defaultdict(int)
    for i in items:
        counts[i.status] += 1
    return {
        "as_of": as_of,
        "window_days": window_days,
        "summary": {
            "estimated_inventory_value": str(money(total_value)),
            "dead_stock_value": str(money(dead_value)),
            "dead_stock_share_pct": pct(dead_value, total_value),
            # How many days current stock lasts at the window's cost-of-sales pace.
            "days_of_inventory": str(days_of_inventory) if days_of_inventory else None,
            "products_tracked": len(items),
            "products_counted_recently": sum(1 for i in items if i.stock_basis == "counted"),
            # Fast-moving products without a recent count: the ones most worth counting.
            "count_suggested": [
                i.name for i in items if i.movement == "fast" and i.stock_basis != "counted"
            ],
            "by_status": dict(counts),
            "basis": "counted"
            if items and all(i.stock_basis == "counted" for i in items)
            else "estimated",
        },
        "products": [i.as_dict() for i in items],
        "confidence": conf.as_dict(),
        "_items": items,  # internal: used by insights/brief, stripped from API responses
    }
