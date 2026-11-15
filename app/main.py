from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exception_handlers import http_exception_handler
from sqlalchemy import text

from app.api.routes import (
    analytics,
    auth,
    briefs,
    businesses,
    imports,
    stock,
    team,
    transactions,
)
from app.core.db import engine
from app.services.security import AccessDenied, record_denial

app = FastAPI(
    title="KASU API",
    version="0.1.0",
    description="AI business intelligence for African businesses.",
)

for module in (auth, businesses, team, transactions, imports, stock, analytics):
    app.include_router(module.router, prefix="/v1")
app.include_router(analytics.intel_router, prefix="/v1")
app.include_router(team.public_router, prefix="/v1")
app.include_router(briefs.router, prefix="/v1")
app.include_router(briefs.notifications_router, prefix="/v1")


@app.exception_handler(AccessDenied)
async def access_denied(request: Request, exc: AccessDenied):
    """Refuse with the usual error body, and keep a record of the attempt (KASU-002)."""
    await run_in_threadpool(record_denial, request, exc)
    return await http_exception_handler(request, exc)


@app.get("/health", tags=["meta"])
def health() -> dict:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"status": "ok"}
