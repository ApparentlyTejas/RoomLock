"""
Deliberately injects the specific failure modes create_booking()'s retry
loop was built to survive (see the module docstring in
app/routers/bookings.py) — a transient deadlock during the exclusion-
constraint check, exhausted retries, and a hard-killed connection mid-
transaction — instead of only exercising the happy path. Faults are
injected by monkeypatching Session.commit to raise on demand rather than
racing a real, timing-dependent Postgres deadlock: deterministic and
CI-safe, and it's the exact exception type (OperationalError) and code
path (db.commit() inside the retry loop) that a real deadlock produces,
so it's testing the real recovery logic, not a stand-in for it.
"""

import psycopg2
import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session as SASession

from tests.conftest import _TEST_DB_URL, _plain_url


def _fake_operational_error():
    return OperationalError("simulated deadlock during commit", {}, Exception("deadlock detected"))


@pytest.fixture
def flaky_commit(monkeypatch):
    """
    Patches Session.commit to raise a simulated OperationalError
    `state["remaining_failures"]` more times, then behave normally.
    Only affects commits made after the fixture is requested — fixture
    setup (registering users, creating the test resource) has already
    committed by then, so only the request under test is affected.
    """
    real_commit = SASession.commit
    state = {"remaining_failures": 0, "calls": 0}

    def wrapped(self):
        state["calls"] += 1
        if state["remaining_failures"] > 0:
            state["remaining_failures"] -= 1
            raise _fake_operational_error()
        return real_commit(self)

    monkeypatch.setattr(SASession, "commit", wrapped)
    return state


def test_recovers_from_transient_deadlock_on_create(client, user_headers, resource_id, flaky_commit):
    flaky_commit["remaining_failures"] = 2  # fewer than _MAX_ATTEMPTS (5) — should still succeed
    resp = client.post(
        "/bookings",
        json={
            "resource_id": resource_id,
            "start_time": "2027-01-01T10:00:00Z",
            "end_time": "2027-01-01T11:00:00Z",
        },
        headers=user_headers,
    )
    assert resp.status_code == 201
    assert flaky_commit["remaining_failures"] == 0  # confirms it actually hit and retried past the failures


def test_exhausted_retries_return_503_not_500(client, user_headers, resource_id, flaky_commit):
    flaky_commit["remaining_failures"] = 99  # more than _MAX_ATTEMPTS — every attempt fails
    resp = client.post(
        "/bookings",
        json={
            "resource_id": resource_id,
            "start_time": "2027-01-01T10:00:00Z",
            "end_time": "2027-01-01T11:00:00Z",
        },
        headers=user_headers,
    )
    assert resp.status_code == 503


def test_exhausted_retries_leave_no_partial_booking(client, user_headers, resource_id, flaky_commit):
    flaky_commit["remaining_failures"] = 99
    client.post(
        "/bookings",
        json={
            "resource_id": resource_id,
            "start_time": "2027-01-01T10:00:00Z",
            "end_time": "2027-01-01T11:00:00Z",
        },
        headers=user_headers,
    )
    # a fresh, unpatched request should see the slot as still completely free
    check = client.get(f"/bookings?resource_id={resource_id}&date=2027-01-01", headers=user_headers)
    assert check.json() == []


def test_idempotent_resubmission_survives_a_deadlock_retry(client, user_headers, resource_id, flaky_commit):
    payload = {
        "resource_id": resource_id,
        "start_time": "2027-01-01T10:00:00Z",
        "end_time": "2027-01-01T11:00:00Z",
        "idempotency_key": "flaky-key-1",
    }
    flaky_commit["remaining_failures"] = 2
    first = client.post("/bookings", json=payload, headers=user_headers)
    assert first.status_code == 201

    flaky_commit["remaining_failures"] = 2
    second = client.post("/bookings", json=payload, headers=user_headers)
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]  # same booking, not a duplicate or an error


def test_killed_connection_mid_transaction_leaves_no_partial_row(resource_id):
    """
    Bypasses the app entirely: opens a raw connection, starts a booking
    insert, then kills the connection without committing — simulating a
    server crash or network drop mid-write. Verifies Postgres's own
    atomicity guarantee (not app retry logic) is what actually prevents a
    half-written row, independent of anything this codebase does. (The
    `resource_id` fixture also guarantees at least one user row exists,
    which the raw INSERT below borrows a user_id from.)
    """
    conn = psycopg2.connect(_plain_url(_TEST_DB_URL))
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO bookings (resource_id, user_id, start_time, end_time) "
            "SELECT %s, id, '2027-06-01T10:00:00Z', '2027-06-01T11:00:00Z' FROM users LIMIT 1",
            (resource_id,),
        )
        # deliberately no conn.commit() — simulate the process dying here
    finally:
        conn.close()  # an uncommitted transaction is rolled back on close/disconnect

    verify_conn = psycopg2.connect(_plain_url(_TEST_DB_URL))
    try:
        cur = verify_conn.cursor()
        cur.execute(
            "SELECT count(*) FROM bookings WHERE resource_id = %s AND start_time = '2027-06-01T10:00:00Z'",
            (resource_id,),
        )
        assert cur.fetchone()[0] == 0
    finally:
        verify_conn.close()
