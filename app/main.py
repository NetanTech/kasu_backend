from fastapi import FastAPI
from sqlalchemy import text

from app.api.routes import analytics, auth, businesses, imports, stock, team, transactions
from app.core.db import engine

app = FastAPI(
    title="KASU API",
    version="0.1.0",
    description="AI business intelligence for African businesses.",
)

for module in (auth, businesses, team, transactions, imports, stock, analytics):
    app.include_router(module.router, prefix="/v1")
app.include_router(analytics.intel_router, prefix="/v1")
app.include_router(team.public_router, prefix="/v1")


@app.get("/health", tags=["meta"])
def health() -> dict:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"status": "ok"}
