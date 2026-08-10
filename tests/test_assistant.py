from datetime import datetime, timedelta, timezone

from app import groq_client


def _future_local_date_and_hour(hours_from_now: int, hour: int = 15):
    future = datetime.now(timezone.utc) + timedelta(hours=hours_from_now)
    return future.strftime("%Y-%m-%d"), f"{hour:02d}:00"


def test_status_reports_unconfigured(client, user_headers, monkeypatch):
    monkeypatch.setattr(groq_client, "is_configured", lambda: False)
    resp = client.get("/assistant/status")
    assert resp.status_code == 200
    assert resp.json() == {"configured": False}


def test_parse_503s_when_not_configured(client, user_headers, monkeypatch):
    monkeypatch.setattr(groq_client, "is_configured", lambda: False)
    resp = client.post("/assistant/parse", json={"message": "book something"}, headers=user_headers)
    assert resp.status_code == 503


def test_parse_requires_auth(client):
    resp = client.post("/assistant/parse", json={"message": "book something"})
    assert resp.status_code == 401


def test_parse_returns_draft_for_valid_model_output(client, user_headers, resource_id, monkeypatch):
    monkeypatch.setattr(groq_client, "is_configured", lambda: True)
    date_str, _ = _future_local_date_and_hour(48)
    monkeypatch.setattr(
        groq_client,
        "parse_booking_request",
        lambda message, resources, now_utc: {
            "resource_id": resource_id,
            "date": date_str,
            "start_time": "15:00",
            "end_time": "16:00",
            "clarification": None,
        },
    )
    resp = client.post(
        "/assistant/parse", json={"message": "book Test Room tomorrow at 3pm"}, headers=user_headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["needs_clarification"] is False
    assert body["resource_id"] == resource_id
    assert body["resource_name"] == "Test Room"


def test_local_time_converts_to_correct_utc_offset(client, user_headers, resource_id, monkeypatch):
    # The whole reason date/time is kept local and converted server-side
    # (see groq_client.combine_local_datetime) instead of trusting the
    # model with UTC math: verify a summer (CEST, UTC+2) local time lands
    # on the correct UTC hour.
    monkeypatch.setattr(groq_client, "is_configured", lambda: True)
    monkeypatch.setattr(
        groq_client,
        "parse_booking_request",
        lambda message, resources, now_utc: {
            "resource_id": resource_id,
            "date": "2027-07-15",  # July: always CEST (UTC+2), no DST-boundary ambiguity
            "start_time": "15:00",
            "end_time": "16:00",
            "clarification": None,
        },
    )
    resp = client.post("/assistant/parse", json={"message": "book Test Room"}, headers=user_headers)
    body = resp.json()
    assert body["needs_clarification"] is False
    assert body["start_time"].startswith("2027-07-15T13:00:00")  # 15:00 CEST == 13:00 UTC
    assert body["end_time"].startswith("2027-07-15T14:00:00")


def test_missing_end_time_defaults_to_one_hour(client, user_headers, resource_id, monkeypatch):
    monkeypatch.setattr(groq_client, "is_configured", lambda: True)
    monkeypatch.setattr(
        groq_client,
        "parse_booking_request",
        lambda message, resources, now_utc: {
            "resource_id": resource_id,
            "date": "2027-07-15",
            "start_time": "15:00",
            "end_time": None,
            "clarification": None,
        },
    )
    resp = client.post("/assistant/parse", json={"message": "book Test Room at 3pm"}, headers=user_headers)
    body = resp.json()
    assert body["needs_clarification"] is False
    assert body["start_time"].startswith("2027-07-15T13:00:00")
    assert body["end_time"].startswith("2027-07-15T14:00:00")


def test_parse_confirmed_draft_can_be_booked(client, user_headers, resource_id, monkeypatch):
    monkeypatch.setattr(groq_client, "is_configured", lambda: True)
    date_str, _ = _future_local_date_and_hour(48)
    monkeypatch.setattr(
        groq_client,
        "parse_booking_request",
        lambda message, resources, now_utc: {
            "resource_id": resource_id,
            "date": date_str,
            "start_time": "15:00",
            "end_time": "16:00",
            "clarification": None,
        },
    )
    draft = client.post(
        "/assistant/parse", json={"message": "book Test Room tomorrow"}, headers=user_headers
    ).json()
    resp = client.post(
        "/bookings",
        json={
            "resource_id": draft["resource_id"],
            "start_time": draft["start_time"],
            "end_time": draft["end_time"],
        },
        headers=user_headers,
    )
    assert resp.status_code == 201


def test_parse_passes_through_model_clarification(client, user_headers, resource_id, monkeypatch):
    monkeypatch.setattr(groq_client, "is_configured", lambda: True)
    monkeypatch.setattr(
        groq_client,
        "parse_booking_request",
        lambda message, resources, now_utc: {
            "resource_id": None,
            "date": None,
            "start_time": None,
            "end_time": None,
            "clarification": "Which room did you mean?",
        },
    )
    resp = client.post("/assistant/parse", json={"message": "book a room"}, headers=user_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["needs_clarification"] is True
    assert body["clarification"] == "Which room did you mean?"


def test_parse_rejects_hallucinated_resource_id(client, user_headers, resource_id, monkeypatch):
    monkeypatch.setattr(groq_client, "is_configured", lambda: True)
    date_str, _ = _future_local_date_and_hour(48)
    monkeypatch.setattr(
        groq_client,
        "parse_booking_request",
        lambda message, resources, now_utc: {
            "resource_id": resource_id + 999999,
            "date": date_str,
            "start_time": "15:00",
            "end_time": "16:00",
            "clarification": None,
        },
    )
    resp = client.post(
        "/assistant/parse", json={"message": "book the moon base"}, headers=user_headers
    )
    assert resp.status_code == 200
    assert resp.json()["needs_clarification"] is True


def test_parse_rejects_past_time(client, user_headers, resource_id, monkeypatch):
    monkeypatch.setattr(groq_client, "is_configured", lambda: True)
    monkeypatch.setattr(
        groq_client,
        "parse_booking_request",
        lambda message, resources, now_utc: {
            "resource_id": resource_id,
            "date": "2020-01-01",
            "start_time": "10:00",
            "end_time": "11:00",
            "clarification": None,
        },
    )
    resp = client.post(
        "/assistant/parse", json={"message": "book Test Room yesterday"}, headers=user_headers
    )
    assert resp.status_code == 200
    assert resp.json()["needs_clarification"] is True


def test_parse_rejects_end_before_start(client, user_headers, resource_id, monkeypatch):
    monkeypatch.setattr(groq_client, "is_configured", lambda: True)
    date_str, _ = _future_local_date_and_hour(48)
    monkeypatch.setattr(
        groq_client,
        "parse_booking_request",
        lambda message, resources, now_utc: {
            "resource_id": resource_id,
            "date": date_str,
            "start_time": "16:00",
            "end_time": "15:00",
            "clarification": None,
        },
    )
    resp = client.post("/assistant/parse", json={"message": "book Test Room"}, headers=user_headers)
    assert resp.status_code == 200
    assert resp.json()["needs_clarification"] is True


def test_parse_rejects_malformed_time_strings(client, user_headers, resource_id, monkeypatch):
    monkeypatch.setattr(groq_client, "is_configured", lambda: True)
    monkeypatch.setattr(
        groq_client,
        "parse_booking_request",
        lambda message, resources, now_utc: {
            "resource_id": resource_id,
            "date": "not-a-date",
            "start_time": "3pm-ish",
            "end_time": "16:00",
            "clarification": None,
        },
    )
    resp = client.post("/assistant/parse", json={"message": "book Test Room whenever"}, headers=user_headers)
    assert resp.status_code == 200
    assert resp.json()["needs_clarification"] is True


def test_parse_502s_when_groq_call_fails(client, user_headers, resource_id, monkeypatch):
    monkeypatch.setattr(groq_client, "is_configured", lambda: True)

    def _boom(message, resources, now_utc):
        raise RuntimeError("network error")

    monkeypatch.setattr(groq_client, "parse_booking_request", _boom)
    resp = client.post("/assistant/parse", json={"message": "book Test Room"}, headers=user_headers)
    assert resp.status_code == 502


def test_parse_with_no_resources_asks_for_clarification(client, user_headers, monkeypatch):
    monkeypatch.setattr(groq_client, "is_configured", lambda: True)
    resp = client.post("/assistant/parse", json={"message": "book something"}, headers=user_headers)
    assert resp.status_code == 200
    assert resp.json()["needs_clarification"] is True
