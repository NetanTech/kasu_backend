# KASU Backend

API for **KASU — Your AI Business Analyst**: turns messy business records (Excel, CSV, and soon
receipts, voice and text) into a structured Business Memory, then explains revenue, profit, cash
flow and business-vs-personal spending with explicit confidence.

## Stack

Python 3.12+ · FastAPI · PostgreSQL 16 · SQLAlchemy 2 · Alembic · uv

## Getting started

```bash
cp .env.example .env            # then set KASU_JWT_SECRET
docker compose up -d --wait     # Postgres (creates `kasu` and `kasu_test` databases)
uv sync                         # install dependencies into .venv
uv run alembic upgrade head     # create tables
uv run uvicorn app.main:app --reload
```

Interactive API docs: http://localhost:8000/docs

```bash
uv run pytest                   # tests (uses the kasu_test database, wiped per run)
uv run ruff check . && uv run ruff format .
uv run alembic revision --autogenerate -m "describe change"   # after changing models
```

## Layout

```
app/
  core/          config, database session, password hashing + JWT
  models/        SQLAlchemy models (tenancy, ledger, memory, imports) + enums
  schemas/       Pydantic request/response models
  api/           FastAPI dependencies (auth, tenant scoping) and routes under /v1
  services/
    ledger.py          the only code that writes transactions; every write emits an event
    normalization.py   amounts (₦5,000 / 5k / (2,500)), day-first dates, Excel serials, name keys
    classification.py  rule-based kind/category guesses with confidence
    memory.py          product/customer/supplier resolution via aliases; asks when ambiguous
    imports/           read file -> detect headers/columns -> stage rows -> review -> commit
    analytics.py       LedgerView (ledger loaded + WAC-walked once per request); overview,
                       trends, product performance, expense breakdown
    inventory.py       estimated stock, days left, fast/slow/dead stock, reorder suggestions
    customers.py       new vs returning, top/dormant customers, concentration, who owes you
    intelligence.py    rule-based insights (what happened / why / evidence / what to do) and
                       the Business Health Score
    reports.py         Weekly Business Brief (with WhatsApp-ready text) and dashboard payload
migrations/      Alembic migrations
tests/           API-level tests against real Postgres
```

## Key design decisions

| PRD requirement | How it's implemented |
| --- | --- |
| Corrections auditable, never silently overwritten (§20) | `transactions` is the current state; `transaction_events` is an append-only log with a full snapshot + diff per change. A DB trigger blocks `UPDATE` on events. Voiding keeps the row. |
| Tenant isolation (§32) | Every business-owned row has `business_id`. All routes resolve the tenant via `get_business`, which returns 404 for non-members. |
| Ask, don't guess (§7.1, §9) | Imports are staged. Rows carry per-field confidence and issues. Blocking issues → row becomes `unresolved` on commit. Non-blocking doubts → transaction stored as `provisional`. Near-miss product names ("Golden Peny" vs "Golden Penny") raise a question instead of auto-merging. |
| Low-confidence data can't drive high-confidence output (§19) | Analytics exclude provisional transactions by default and report what was left out. Every metric has `basis: calculated \| estimated`; responses include a confidence level with plain-language reasons. |
| Business vs Personal (§6, §12) | `personal_expense` and `owner_withdrawal` are cash out but never reduce operating profit. Overview reports `retained_in_business`. |
| Profit ≠ cash (§11) | `settlement: paid \| credit`. Credit sales are revenue, not cash. Receivables/payables are derived from credit entries and `customer_payment` / `supplier_payment`. |
| WAC costing (§10.3) | COGS uses weighted-average cost per product when purchases and sales carry quantity. Otherwise falls back to purchases-in-period and labels profit **estimated**. |
| Offline sync (§7.4) | `client_ref` is unique per business. Re-sending returns the original transaction (200) instead of duplicating. |
| Explainable intelligence (§15–17) | Insights are deterministic rules with tunable thresholds at the top of `intelligence.py`. Each carries evidence and a confidence. Health Score dimensions without enough data are *skipped*, not scored zero, and the response states what share of the score is backed by data. Inflation isn't faked. |
| Money accuracy | Stored as integer minor units (kobo). The API speaks decimal naira strings. |

## API overview (`/v1`)

- `POST /auth/register`, `POST /auth/login`, `POST /auth/refresh`, `GET /auth/me`
- `POST/GET /businesses`, `GET/PATCH /businesses/{id}`
- `GET /businesses/{id}/products`, `PATCH .../products/{pid}`, `POST .../products/{pid}/aliases`
- Transactions: `POST/GET /businesses/{id}/transactions`, `GET/PATCH .../{tid}`,
  `POST .../{tid}/confirm`, `POST .../{tid}/void`, `GET .../{tid}/history`
- Imports: `POST /businesses/{id}/imports` (multipart file), `GET .../imports/{iid}`,
  `PUT .../{iid}/mapping`, `PATCH .../{iid}/rows/{rid}`, `POST .../{iid}/commit`
- Analytics: `GET /businesses/{id}/analytics/overview|trends|products|expenses|customers?start=&end=`,
  `GET .../analytics/inventory?as_of=&window_days=`
- Intelligence: `GET /businesses/{id}/insights`, `GET .../health-score`, `GET .../dashboard`
  (all `?start=&end=`), `GET .../brief?week_ending=`

## Not built yet

- Physical stock counts / adjustments (stock is currently derived from purchases and sales)
- Inflation intelligence (needs an inflation data source)

- AI layer: LLM extraction for images/receipts/voice/text and the AI Business Analyst
- Background worker for long-running extraction (CSV/Excel parse inline for now)
- S3-compatible object storage (uploads go to local disk via `services/storage.py`)
- Postgres row-level security as a second line of tenant isolation
- Phone/OTP login, member invitations, rate limiting, data export/deletion endpoints
