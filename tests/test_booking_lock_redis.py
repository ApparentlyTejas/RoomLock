"""
Same proof as test_concurrency.py's exclusion-constraint race, but with
settings.booking_lock_strategy flipped to "redis" — proving the Redis lock
path in app/booking_lock.py actually serializes concurrent attempts on the
same resource, against a real Redis instance (see docker-compose.yml /
ci.yml), not a mock. The EXCLUDE constraint is still present underneath
either way (it's a DB schema constraint, not something a setting can turn
off), so this isn't testing "redis alone is correct" — it's testing that
the lock really does what resource_lock() claims, instead of being a no-op
that happens to pass because the constraint would have caught it anyway.
That's checked separately below by asserting the lock was actually taken.
"""

import concurrent.futures

import pytest
from sqlalchemy import text

from app.config import settings
from tests.conftest import register_and_login

N_CONCURRENT = 20


@pytest.fixture(autouse=True)
def _redis_strategy(monkeypatch):
    monkeypatch.setattr(settings, "booking_lock_strategy", "redis")


def test_concurrent_identical_bookings_exactly_one_wins_via_redis_lock(client, admin_headers, resource_id):
    token = register_and_login(client, "redis-racer@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "resource_id": resource_id,
        "start_time": "2027-04-01T10:00:00Z",
        "end_time": "2027-04-01T11:00:00Z",
    }

    def attempt(_):
        return client.post("/bookings", json=payload, headers=headers)

    with concurrent.futures.ThreadPoolExecutor(max_workers=N_CONCURRENT) as pool:
        results = list(pool.map(attempt, range(N_CONCURRENT)))

    statuses = [r.status_code for r in results]
    assert statuses.count(201) == 1, f"expected exactly one booking to succeed, got: {statuses}"
    assert statuses.count(409) == N_CONCURRENT - 1

    from app.database import SessionLocal

    session = SessionLocal()
    overlaps = session.execute(
        text(
            """
            SELECT count(*) FROM bookings a
            JOIN bookings b ON a.resource_id = b.resource_id AND a.id < b.id
            WHERE a.start_time < b.end_time AND b.start_time < a.end_time
            """
        )
    ).scalar()
    session.close()
    assert overlaps == 0


def test_lock_is_actually_held_during_the_critical_section(client, admin_headers, resource_id):
    # Distinguishes "the lock works" from "the constraint would have caught
    # it anyway even if resource_lock() were a no-op": starts a booking
    # request on a background thread, and while it's presumably mid-flight,
    # checks from the main thread that the same resource's Redis key exists
    # and is held. Not watertight against every possible scheduling (the
    # first request could finish before the check runs), so it retries a
    # few times across fresh slots rather than asserting on a single sample.
    from app.booking_lock import _get_client

    token = register_and_login(client, "redis-observer@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    redis_client = _get_client()

    for i in range(5):
        payload = {
            "resource_id": resource_id,
            "start_time": f"2027-05-{i + 1:02d}T10:00:00Z",
            "end_time": f"2027-05-{i + 1:02d}T11:00:00Z",
        }
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(client.post, "/bookings", json=payload, headers=headers)
            seen_locked = False
            for _ in range(200):
                if redis_client.exists(f"roomlock:resource:{resource_id}"):
                    seen_locked = True
                    break
            resp = future.result()
        assert resp.status_code == 201
        if seen_locked:
            return

    pytest.fail("never observed the Redis lock key held during a booking request after 5 attempts")
