"""
Tests run against a real, throwaway Postgres database (bookingapi_test),
not SQLite — the whole point of this project is Postgres-specific
guarantees (the GiST exclusion constraint, pg_advisory_xact_lock) that
SQLite doesn't have. A concurrency test against SQLite would prove nothing.

DATABASE_URL is overridden to point at the test database *before* any
app.* module is imported (pydantic-settings reads the environment once, at
Settings() instantiation in app/config.py) — that's why this happens at
the very top of the file, ahead of every other import.

Run with: docker compose exec api pytest
(needs to run inside the container/network so the "db" hostname resolves).
"""

import glob
import os
import re

_ORIGINAL_DB_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg2://app:app_dev_password@db:5432/bookingapi"
)
_TEST_DB_NAME = "bookingapi_test"
_TEST_DB_URL = re.sub(r"/[^/]+$", f"/{_TEST_DB_NAME}", _ORIGINAL_DB_URL)
os.environ["DATABASE_URL"] = _TEST_DB_URL

import psycopg2  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "..", "db")


def _plain_url(url: str) -> str:
    return url.replace("postgresql+psycopg2://", "postgresql://")


@pytest.fixture(scope="session", autouse=True)
def _test_database():
    admin_url = re.sub(r"/[^/]+$", "/postgres", _ORIGINAL_DB_URL)
    conn = psycopg2.connect(_plain_url(admin_url))
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(f"DROP DATABASE IF EXISTS {_TEST_DB_NAME}")
    cur.execute(f"CREATE DATABASE {_TEST_DB_NAME}")
    cur.close()
    conn.close()

    test_conn = psycopg2.connect(_plain_url(_TEST_DB_URL))
    test_conn.autocommit = True
    test_cur = test_conn.cursor()
    for path in sorted(glob.glob(os.path.join(MIGRATIONS_DIR, "*.sql"))):
        with open(path) as f:
            test_cur.execute(f.read())
    test_cur.close()
    test_conn.close()
    yield


@pytest.fixture()
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _clean_tables():
    yield
    from app.database import SessionLocal

    session = SessionLocal()
    session.execute(text("TRUNCATE bookings, resources, users RESTART IDENTITY CASCADE"))
    session.commit()
    session.close()


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    # slowapi's Limiter tracks by client IP, and TestClient requests all
    # appear to come from the same address — without this, tests that
    # register/log in several users (most of them, via the fixtures below)
    # would trip /auth/register's 5/minute limit and start failing for
    # reasons that have nothing to do with what they're testing.
    from app.rate_limit import limiter

    limiter.reset()
    yield


def register_and_login(client, email: str, password: str = "Test-password-123") -> str:
    client.post("/auth/register", json={"email": email, "password": password})
    resp = client.post("/auth/login", data={"username": email, "password": password})
    return resp.json()["access_token"]


def make_admin(email: str) -> None:
    from app.database import SessionLocal
    from app.models import User

    session = SessionLocal()
    user = session.query(User).filter(User.email == email).first()
    user.is_admin = True
    session.commit()
    session.close()


@pytest.fixture()
def admin_headers(client):
    token = register_and_login(client, "admin@example.com")
    make_admin("admin@example.com")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def user_headers(client):
    token = register_and_login(client, "user@example.com")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def resource_id(client, admin_headers):
    resp = client.post("/resources", json={"name": "Test Room"}, headers=admin_headers)
    return resp.json()["id"]
