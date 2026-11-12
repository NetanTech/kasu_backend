import os

# Point the app at the test database *before* anything imports app settings.
os.environ["KASU_DATABASE_URL"] = os.environ.get(
    "KASU_TEST_DATABASE_URL", "postgresql+psycopg://kasu:kasu@127.0.0.1:5432/kasu_test"
)

import tempfile  # noqa: E402
import uuid  # noqa: E402
from pathlib import Path  # noqa: E402

os.environ["KASU_UPLOAD_DIR"] = tempfile.mkdtemp(prefix="kasu-test-uploads-")

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session", autouse=True)
def _schema():
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["database_url"] = os.environ["KASU_DATABASE_URL"]
    command.upgrade(cfg, "head")
    yield


@pytest.fixture
def _clean_tables():
    """Wipe data after any test that touched the database. DELETE (children first) is much
    faster than TRUNCATE on tiny tables, which pays a fixed cost per table plus lock waits."""
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture
def client(_clean_tables):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db(_clean_tables):
    session = SessionLocal()
    yield session
    session.close()


def register(client: TestClient, email: str | None = None) -> dict[str, str]:
    email = email or f"user-{uuid.uuid4().hex[:8]}@example.com"
    res = client.post(
        "/v1/auth/register",
        json={"email": email, "password": "correct-horse-1", "full_name": "Ada Obi"},
    )
    assert res.status_code == 201, res.text
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


@pytest.fixture
def auth(client) -> dict[str, str]:
    return register(client)


@pytest.fixture
def business(client, auth) -> str:
    res = client.post("/v1/businesses", json={"name": "Mama Nkechi Provisions"}, headers=auth)
    assert res.status_code == 201, res.text
    return res.json()["id"]


@pytest.fixture
def api(client, auth, business):
    """Small helper: api.post('/transactions', json=...) scoped to the test business."""

    class Api:
        base = f"/v1/businesses/{business}"
        headers = auth

        def get(self, path, **kw):
            return client.get(self.base + path, headers=auth, **kw)

        def post(self, path, **kw):
            return client.post(self.base + path, headers=auth, **kw)

        def patch(self, path, **kw):
            return client.patch(self.base + path, headers=auth, **kw)

        def put(self, path, **kw):
            return client.put(self.base + path, headers=auth, **kw)

    return Api()
