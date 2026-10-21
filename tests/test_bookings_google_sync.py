"""
Covers the best-effort Google Calendar sync embedded in
app/routers/bookings.py (create/recurring-create/update/cancel/cancel-
series) plus a few retry-path and validation edge cases the other test
files don't reach. The core behavioral guarantee under test throughout:
a Google API failure must be swallowed and must never turn a successful
booking operation into a failed one — only the booking's own DB
constraints get to decide success/failure. gcal.* calls are monkeypatched
so nothing here touches the real Google API.
"""

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session as SASession

from app import crypto
from app import google_calendar as gcal


def _connect_google(email: str) -> None:
    from app.database import SessionLocal
    from app.models import User

    session = SessionLocal()
    user = session.query(User).filter(User.email == email).first()
    user.google_refresh_token = crypto.encrypt("fake-refresh-token")
    session.commit()
    session.close()


def _book(client, headers, resource_id, start="2027-02-01T10:00:00Z", end="2027-02-01T11:00:00Z"):
    return client.post(
        "/bookings", json={"resource_id": resource_id, "start_time": start, "end_time": end}, headers=headers
    )


# --- create_booking -------------------------------------------------------


def test_create_booking_syncs_to_google_when_connected(client, user_headers, resource_id, monkeypatch):
    _connect_google("user@example.com")
    captured = {}

    def fake_create_event(refresh_token, summary, description, start, end):
        captured["refresh_token"] = refresh_token
        captured["summary"] = summary
        return "google-event-abc"

    monkeypatch.setattr(gcal, "create_event", fake_create_event)
    resp = _book(client, user_headers, resource_id)
    assert resp.status_code == 201
    assert captured["refresh_token"] == "fake-refresh-token"  # decrypted before use
    assert "Test Room" in captured["summary"]


def test_create_booking_succeeds_even_when_google_sync_raises(client, user_headers, resource_id, monkeypatch):
    _connect_google("user@example.com")

    def _boom(*a, **k):
        raise RuntimeError("Google API is down")

    monkeypatch.setattr(gcal, "create_event", _boom)
    resp = _book(client, user_headers, resource_id)
    assert resp.status_code == 201  # booking must still succeed
    assert resp.json()["resource_id"] == resource_id


def test_create_booking_skips_sync_entirely_when_not_connected(client, user_headers, resource_id, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("create_event should never be called for a user with no google_refresh_token")

    monkeypatch.setattr(gcal, "create_event", _boom)
    resp = _book(client, user_headers, resource_id)
    assert resp.status_code == 201


# --- update_booking ---------------------------------------------------


def test_update_nonexistent_booking_404s(client, user_headers):
    resp = client.patch(
        "/bookings/999999",
        json={"start_time": "2027-02-01T15:00:00Z", "end_time": "2027-02-01T16:00:00Z"},
        headers=user_headers,
    )
    assert resp.status_code == 404


def test_update_booking_exhausted_integrity_retries_returns_409(client, user_headers, resource_id, monkeypatch):
    booking = _book(client, user_headers, resource_id).json()

    def always_conflict(self):
        raise IntegrityError("simulated exclusion violation", {}, Exception("conflicting key value"))

    monkeypatch.setattr(SASession, "commit", always_conflict)
    resp = client.patch(
        f"/bookings/{booking['id']}",
        json={"start_time": "2027-02-01T15:00:00Z", "end_time": "2027-02-01T16:00:00Z"},
        headers=user_headers,
    )
    assert resp.status_code == 409


def test_update_booking_exhausted_operational_retries_returns_503(client, user_headers, resource_id, monkeypatch):
    booking = _book(client, user_headers, resource_id).json()

    def always_deadlock(self):
        raise OperationalError("simulated deadlock", {}, Exception("deadlock detected"))

    monkeypatch.setattr(SASession, "commit", always_deadlock)
    resp = client.patch(
        f"/bookings/{booking['id']}",
        json={"start_time": "2027-02-01T15:00:00Z", "end_time": "2027-02-01T16:00:00Z"},
        headers=user_headers,
    )
    assert resp.status_code == 503


def test_update_booking_end_before_start_rejected(client, user_headers, resource_id):
    booking = _book(client, user_headers, resource_id).json()
    resp = client.patch(
        f"/bookings/{booking['id']}",
        json={"start_time": "2027-02-01T15:00:00Z", "end_time": "2027-02-01T14:00:00Z"},
        headers=user_headers,
    )
    assert resp.status_code == 422


def test_update_booking_syncs_to_google_when_it_has_an_event_id(client, user_headers, resource_id, monkeypatch):
    _connect_google("user@example.com")
    monkeypatch.setattr(gcal, "create_event", lambda *a, **k: "google-event-xyz")
    booking = _book(client, user_headers, resource_id).json()
    assert booking["id"]  # created with a google_event_id behind the scenes

    captured = {}

    def fake_update_event(refresh_token, event_id, start, end):
        captured["event_id"] = event_id

    monkeypatch.setattr(gcal, "update_event", fake_update_event)
    resp = client.patch(
        f"/bookings/{booking['id']}",
        json={"start_time": "2027-02-01T16:00:00Z", "end_time": "2027-02-01T17:00:00Z"},
        headers=user_headers,
    )
    assert resp.status_code == 200
    assert captured["event_id"] == "google-event-xyz"


def test_update_booking_succeeds_even_when_google_sync_raises(client, user_headers, resource_id, monkeypatch):
    _connect_google("user@example.com")
    monkeypatch.setattr(gcal, "create_event", lambda *a, **k: "google-event-xyz")
    booking = _book(client, user_headers, resource_id).json()

    def _boom(*a, **k):
        raise RuntimeError("Google API is down")

    monkeypatch.setattr(gcal, "update_event", _boom)
    resp = client.patch(
        f"/bookings/{booking['id']}",
        json={"start_time": "2027-02-01T16:00:00Z", "end_time": "2027-02-01T17:00:00Z"},
        headers=user_headers,
    )
    assert resp.status_code == 200  # update must still succeed


# --- cancel_booking / cancel_booking_series -----------------------------


def test_cancel_booking_syncs_delete_to_google(client, user_headers, resource_id, monkeypatch):
    _connect_google("user@example.com")
    monkeypatch.setattr(gcal, "create_event", lambda *a, **k: "google-event-to-delete")
    booking = _book(client, user_headers, resource_id).json()

    captured = {}
    monkeypatch.setattr(gcal, "delete_event", lambda refresh_token, event_id: captured.update(event_id=event_id))
    resp = client.delete(f"/bookings/{booking['id']}", headers=user_headers)
    assert resp.status_code == 204
    assert captured["event_id"] == "google-event-to-delete"


def test_cancel_booking_succeeds_even_when_google_sync_raises(client, user_headers, resource_id, monkeypatch):
    _connect_google("user@example.com")
    monkeypatch.setattr(gcal, "create_event", lambda *a, **k: "google-event-to-delete")
    booking = _book(client, user_headers, resource_id).json()

    def _boom(*a, **k):
        raise RuntimeError("Google API is down")

    monkeypatch.setattr(gcal, "delete_event", _boom)
    resp = client.delete(f"/bookings/{booking['id']}", headers=user_headers)
    assert resp.status_code == 204  # cancellation must still succeed


def test_cancel_series_syncs_delete_for_each_future_occurrence(client, user_headers, resource_id, monkeypatch):
    _connect_google("user@example.com")
    event_ids = iter(["ev-1", "ev-2", "ev-3"])
    monkeypatch.setattr(gcal, "create_event", lambda *a, **k: next(event_ids))
    created = client.post(
        "/bookings/recurring",
        json={
            "resource_id": resource_id,
            "start_time": "2027-02-01T10:00:00Z",
            "end_time": "2027-02-01T11:00:00Z",
            "occurrences": 3,
        },
        headers=user_headers,
    ).json()

    deleted = []
    monkeypatch.setattr(gcal, "delete_event", lambda refresh_token, event_id: deleted.append(event_id))
    resp = client.delete(f"/bookings/series/{created['series_id']}", headers=user_headers)
    assert resp.status_code == 204
    assert sorted(deleted) == ["ev-1", "ev-2", "ev-3"]


def test_cancel_series_succeeds_even_when_google_sync_raises(client, user_headers, resource_id, monkeypatch):
    _connect_google("user@example.com")
    monkeypatch.setattr(gcal, "create_event", lambda *a, **k: "ev-1")
    created = client.post(
        "/bookings/recurring",
        json={
            "resource_id": resource_id,
            "start_time": "2027-02-01T10:00:00Z",
            "end_time": "2027-02-01T11:00:00Z",
            "occurrences": 2,
        },
        headers=user_headers,
    ).json()

    def _boom(*a, **k):
        raise RuntimeError("Google API is down")

    monkeypatch.setattr(gcal, "delete_event", _boom)
    resp = client.delete(f"/bookings/series/{created['series_id']}", headers=user_headers)
    assert resp.status_code == 204  # cancellation must still succeed for every occurrence

    remaining = client.get("/bookings/me", headers=user_headers).json()
    assert remaining == []


def test_cancel_series_leaves_past_occurrences_untouched(client, user_headers, resource_id):
    # One occurrence already in the past, one in the future: only the
    # future one should be cancelled (series endpoint's `continue` branch
    # for already-passed bookings).
    past = client.post(
        "/bookings/recurring",
        json={
            "resource_id": resource_id,
            "start_time": "2020-01-01T10:00:00Z",
            "end_time": "2020-01-01T11:00:00Z",
            "occurrences": 2,
        },
        headers=user_headers,
    ).json()
    series_id = past["series_id"]
    assert len(past["created"]) == 2

    resp = client.delete(f"/bookings/series/{series_id}", headers=user_headers)
    assert resp.status_code == 204
    remaining = client.get("/bookings/me", headers=user_headers).json()
    # both occurrences were in the past (2020), so both are left alone
    assert len(remaining) == 2


# --- recurring booking: contention retry paths --------------------------


def test_recurring_booking_sync_to_google_for_each_created_occurrence(client, user_headers, resource_id, monkeypatch):
    _connect_google("user@example.com")
    calls = []
    monkeypatch.setattr(gcal, "create_event", lambda *a, **k: calls.append(1) or f"ev-{len(calls)}")
    resp = client.post(
        "/bookings/recurring",
        json={
            "resource_id": resource_id,
            "start_time": "2027-02-01T10:00:00Z",
            "end_time": "2027-02-01T11:00:00Z",
            "occurrences": 3,
        },
        headers=user_headers,
    )
    assert resp.status_code == 201
    assert len(calls) == 3


def test_recurring_booking_create_succeeds_even_when_google_sync_raises(client, user_headers, resource_id, monkeypatch):
    _connect_google("user@example.com")

    def _boom(*a, **k):
        raise RuntimeError("Google API is down")

    monkeypatch.setattr(gcal, "create_event", _boom)
    resp = client.post(
        "/bookings/recurring",
        json={
            "resource_id": resource_id,
            "start_time": "2027-02-01T10:00:00Z",
            "end_time": "2027-02-01T11:00:00Z",
            "occurrences": 3,
        },
        headers=user_headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert len(body["created"]) == 3
    assert body["skipped"] == []


def test_recurring_booking_exhausted_integrity_retries_is_reported_as_skipped(client, user_headers, resource_id, monkeypatch):
    # Deterministic stand-in for the real race described in bookings.py's
    # module docstring: every commit attempt for this occurrence hits the
    # EXCLUDE constraint (e.g. a concurrent writer keeps winning), so after
    # _MAX_ATTEMPTS the occurrence is reported as skipped rather than the
    # whole series failing.
    real_commit = SASession.commit
    state = {"fail_next": 0}

    def wrapped(self):
        if state["fail_next"] > 0:
            state["fail_next"] -= 1
            raise IntegrityError("simulated exclusion violation", {}, Exception("conflicting key value"))
        return real_commit(self)

    monkeypatch.setattr(SASession, "commit", wrapped)
    state["fail_next"] = 5  # >= _MAX_ATTEMPTS: every attempt for the first occurrence fails

    resp = client.post(
        "/bookings/recurring",
        json={
            "resource_id": resource_id,
            "start_time": "2027-02-01T10:00:00Z",
            "end_time": "2027-02-01T11:00:00Z",
            "occurrences": 2,
        },
        headers=user_headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert len(body["skipped"]) == 1
    assert body["skipped"][0]["reason"] == "Resource already booked for this time"
    assert len(body["created"]) == 1  # second occurrence's commit succeeds normally


def test_recurring_booking_exhausted_operational_retries_is_reported_as_high_contention(
    client, user_headers, resource_id, monkeypatch
):
    real_commit = SASession.commit
    state = {"fail_next": 0}

    def wrapped(self):
        if state["fail_next"] > 0:
            state["fail_next"] -= 1
            raise OperationalError("simulated deadlock", {}, Exception("deadlock detected"))
        return real_commit(self)

    monkeypatch.setattr(SASession, "commit", wrapped)
    state["fail_next"] = 5

    resp = client.post(
        "/bookings/recurring",
        json={
            "resource_id": resource_id,
            "start_time": "2027-02-01T10:00:00Z",
            "end_time": "2027-02-01T11:00:00Z",
            "occurrences": 2,
        },
        headers=user_headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert len(body["skipped"]) == 1
    assert "High contention" in body["skipped"][0]["reason"]
    assert len(body["created"]) == 1


def test_recurring_booking_recovers_from_transient_conflict_below_max_attempts(client, user_headers, resource_id, monkeypatch):
    real_commit = SASession.commit
    state = {"fail_next": 0}

    def wrapped(self):
        if state["fail_next"] > 0:
            state["fail_next"] -= 1
            raise IntegrityError("simulated", {}, Exception("transient"))
        return real_commit(self)

    monkeypatch.setattr(SASession, "commit", wrapped)
    state["fail_next"] = 2  # fewer than _MAX_ATTEMPTS (5) -> should still succeed

    resp = client.post(
        "/bookings/recurring",
        json={
            "resource_id": resource_id,
            "start_time": "2027-02-01T10:00:00Z",
            "end_time": "2027-02-01T11:00:00Z",
            "occurrences": 2,
        },
        headers=user_headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert len(body["created"]) == 2
    assert body["skipped"] == []


# --- create_booking: idempotent-create exhausted-retry edge case -------


def test_create_booking_with_idempotency_key_exhausts_retries_returns_409(client, user_headers, resource_id, monkeypatch):
    # Exercises the idempotency-key branch of the exhausted-retries except
    # block: every commit attempt fails and no row with this key was ever
    # actually committed, so there's nothing to fall back to and it's a
    # genuine 409, not a silent idempotent "success".
    def always_conflict(self):
        raise IntegrityError("simulated exclusion violation", {}, Exception("conflicting key value"))

    monkeypatch.setattr(SASession, "commit", always_conflict)
    resp = client.post(
        "/bookings",
        json={
            "resource_id": resource_id,
            "start_time": "2027-02-05T10:00:00Z",
            "end_time": "2027-02-05T11:00:00Z",
            "idempotency_key": "exhausted-key-1",
        },
        headers=user_headers,
    )
    assert resp.status_code == 409
