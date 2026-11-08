from datetime import UTC, datetime, timedelta

from app import groq_client


def _future_local_date_and_hour(hours_from_now: int, hour: int = 15):
    future = datetime.now(UTC) + timedelta(hours=hours_from_now)
    return future.strftime("%Y-%m-%d"), f"{hour:02d}:00"


def _mock_parse(monkeypatch, result: dict):
    monkeypatch.setattr(groq_client, "parse_assistant_message", lambda message, resources, bookings, history, now_utc: result)


def _book_result(resource_id, date_str, start="15:00", end="16:00"):
    return {
        "intent": "book",
        "resource_id": resource_id,
        "date": date_str,
        "start_time": start,
        "end_time": end,
        "booking_id": None,
        "reply": None,
        "clarification": None,
    }


def test_status_reports_unconfigured(client, user_headers, monkeypatch):
    monkeypatch.setattr(groq_client, "is_configured", lambda: False)
    resp = client.get("/assistant/status")
    assert resp.status_code == 200
    assert resp.json() == {"configured": False}


def test_chat_503s_when_not_configured(client, user_headers, monkeypatch):
    monkeypatch.setattr(groq_client, "is_configured", lambda: False)
    resp = client.post("/assistant/chat", json={"message": "book something"}, headers=user_headers)
    assert resp.status_code == 503


def test_chat_requires_auth(client):
    resp = client.post("/assistant/chat", json={"message": "book something"})
    assert resp.status_code == 401


def test_book_intent_returns_draft_for_valid_model_output(client, user_headers, resource_id, monkeypatch):
    monkeypatch.setattr(groq_client, "is_configured", lambda: True)
    date_str, _ = _future_local_date_and_hour(48)
    _mock_parse(monkeypatch, _book_result(resource_id, date_str))
    resp = client.post(
        "/assistant/chat", json={"message": "book Test Room tomorrow at 3pm"}, headers=user_headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["intent"] == "book"
    assert body["needs_clarification"] is False
    assert body["resource_id"] == resource_id
    assert body["resource_name"] == "Test Room"


def test_local_time_converts_to_correct_utc_offset(client, user_headers, resource_id, monkeypatch):
    # The whole reason date/time is kept local and converted server-side
    # (see groq_client.combine_local_datetime) instead of trusting the
    # model with UTC math: verify a summer (CEST, UTC+2) local time lands
    # on the correct UTC hour.
    monkeypatch.setattr(groq_client, "is_configured", lambda: True)
    _mock_parse(monkeypatch, _book_result(resource_id, "2027-07-15"))  # July: always CEST (UTC+2)
    resp = client.post("/assistant/chat", json={"message": "book Test Room"}, headers=user_headers)
    body = resp.json()
    assert body["needs_clarification"] is False
    assert body["start_time"].startswith("2027-07-15T13:00:00")  # 15:00 CEST == 13:00 UTC
    assert body["end_time"].startswith("2027-07-15T14:00:00")


def test_missing_end_time_defaults_to_one_hour(client, user_headers, resource_id, monkeypatch):
    monkeypatch.setattr(groq_client, "is_configured", lambda: True)
    _mock_parse(monkeypatch, _book_result(resource_id, "2027-07-15", end=None))
    resp = client.post("/assistant/chat", json={"message": "book Test Room at 3pm"}, headers=user_headers)
    body = resp.json()
    assert body["needs_clarification"] is False
    assert body["start_time"].startswith("2027-07-15T13:00:00")
    assert body["end_time"].startswith("2027-07-15T14:00:00")


def test_book_draft_can_be_confirmed(client, user_headers, resource_id, monkeypatch):
    monkeypatch.setattr(groq_client, "is_configured", lambda: True)
    date_str, _ = _future_local_date_and_hour(48)
    _mock_parse(monkeypatch, _book_result(resource_id, date_str))
    draft = client.post(
        "/assistant/chat", json={"message": "book Test Room tomorrow"}, headers=user_headers
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


def test_model_clarification_is_passed_through(client, user_headers, resource_id, monkeypatch):
    monkeypatch.setattr(groq_client, "is_configured", lambda: True)
    _mock_parse(
        monkeypatch,
        {
            "intent": "book",
            "resource_id": None,
            "date": None,
            "start_time": None,
            "end_time": None,
            "booking_id": None,
            "reply": None,
            "clarification": "Which room did you mean?",
        },
    )
    resp = client.post("/assistant/chat", json={"message": "book a room"}, headers=user_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["needs_clarification"] is True
    assert body["reply"] == "Which room did you mean?"


def test_book_rejects_hallucinated_resource_id(client, user_headers, resource_id, monkeypatch):
    monkeypatch.setattr(groq_client, "is_configured", lambda: True)
    date_str, _ = _future_local_date_and_hour(48)
    _mock_parse(monkeypatch, _book_result(resource_id + 999999, date_str))
    resp = client.post("/assistant/chat", json={"message": "book the moon base"}, headers=user_headers)
    assert resp.status_code == 200
    assert resp.json()["needs_clarification"] is True


def test_book_rejects_past_time(client, user_headers, resource_id, monkeypatch):
    monkeypatch.setattr(groq_client, "is_configured", lambda: True)
    _mock_parse(monkeypatch, _book_result(resource_id, "2020-01-01", start="10:00", end="11:00"))
    resp = client.post("/assistant/chat", json={"message": "book Test Room yesterday"}, headers=user_headers)
    assert resp.status_code == 200
    assert resp.json()["needs_clarification"] is True


def test_book_rejects_end_before_start(client, user_headers, resource_id, monkeypatch):
    monkeypatch.setattr(groq_client, "is_configured", lambda: True)
    date_str, _ = _future_local_date_and_hour(48)
    _mock_parse(monkeypatch, _book_result(resource_id, date_str, start="16:00", end="15:00"))
    resp = client.post("/assistant/chat", json={"message": "book Test Room"}, headers=user_headers)
    assert resp.status_code == 200
    assert resp.json()["needs_clarification"] is True


def test_book_rejects_malformed_time_strings(client, user_headers, resource_id, monkeypatch):
    monkeypatch.setattr(groq_client, "is_configured", lambda: True)
    _mock_parse(monkeypatch, _book_result(resource_id, "not-a-date", start="3pm-ish", end="16:00"))
    resp = client.post("/assistant/chat", json={"message": "book Test Room whenever"}, headers=user_headers)
    assert resp.status_code == 200
    assert resp.json()["needs_clarification"] is True


def test_chat_502s_when_groq_call_fails(client, user_headers, resource_id, monkeypatch):
    monkeypatch.setattr(groq_client, "is_configured", lambda: True)

    def _boom(message, resources, bookings, history, now_utc):
        raise RuntimeError("network error")

    monkeypatch.setattr(groq_client, "parse_assistant_message", _boom)
    resp = client.post("/assistant/chat", json={"message": "book Test Room"}, headers=user_headers)
    assert resp.status_code == 502


def test_book_with_no_resources_asks_for_clarification(client, user_headers, monkeypatch):
    monkeypatch.setattr(groq_client, "is_configured", lambda: True)
    _mock_parse(monkeypatch, _book_result(1, "2027-07-15"))
    resp = client.post("/assistant/chat", json={"message": "book something"}, headers=user_headers)
    assert resp.status_code == 200
    assert resp.json()["needs_clarification"] is True


def test_history_is_forwarded_to_the_model(client, user_headers, resource_id, monkeypatch):
    captured = {}

    def fake_parse(message, resources, bookings, history, now_utc):
        captured["history"] = history
        captured["message"] = message
        return {
            "intent": "book",
            "resource_id": None,
            "date": None,
            "start_time": None,
            "end_time": None,
            "booking_id": None,
            "reply": None,
            "clarification": "Which room?",
        }

    monkeypatch.setattr(groq_client, "is_configured", lambda: True)
    monkeypatch.setattr(groq_client, "parse_assistant_message", fake_parse)
    resp = client.post(
        "/assistant/chat",
        json={
            "message": "3pm",
            "history": [
                {"role": "user", "content": "book a room tomorrow"},
                {"role": "assistant", "content": "Which room?"},
            ],
        },
        headers=user_headers,
    )
    assert resp.status_code == 200
    assert captured["message"] == "3pm"
    assert captured["history"] == [
        {"role": "user", "content": "book a room tomorrow"},
        {"role": "assistant", "content": "Which room?"},
    ]


def test_list_bookings_intent_returns_real_upcoming_bookings(client, user_headers, resource_id, monkeypatch):
    monkeypatch.setattr(groq_client, "is_configured", lambda: True)
    date_str, _ = _future_local_date_and_hour(48)
    client.post(
        "/bookings",
        json={"resource_id": resource_id, "start_time": f"{date_str}T15:00:00Z", "end_time": f"{date_str}T16:00:00Z"},
        headers=user_headers,
    )
    _mock_parse(
        monkeypatch,
        {
            "intent": "list_bookings",
            "resource_id": None,
            "date": None,
            "start_time": None,
            "end_time": None,
            "booking_id": None,
            "reply": None,
            "clarification": None,
        },
    )
    resp = client.post("/assistant/chat", json={"message": "what have I booked"}, headers=user_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["intent"] == "list_bookings"
    assert len(body["bookings"]) == 1
    assert body["bookings"][0]["resource_name"] == "Test Room"


def test_list_bookings_intent_with_none_reports_empty(client, user_headers, monkeypatch):
    monkeypatch.setattr(groq_client, "is_configured", lambda: True)
    _mock_parse(
        monkeypatch,
        {
            "intent": "list_bookings",
            "resource_id": None,
            "date": None,
            "start_time": None,
            "end_time": None,
            "booking_id": None,
            "reply": None,
            "clarification": None,
        },
    )
    resp = client.post("/assistant/chat", json={"message": "what have I booked"}, headers=user_headers)
    assert resp.status_code == 200
    assert resp.json()["bookings"] == []


def test_availability_intent_reports_free_room(client, user_headers, resource_id, monkeypatch):
    monkeypatch.setattr(groq_client, "is_configured", lambda: True)
    date_str, _ = _future_local_date_and_hour(48)
    _mock_parse(
        monkeypatch,
        {
            "intent": "availability",
            "resource_id": resource_id,
            "date": date_str,
            "start_time": None,
            "end_time": None,
            "booking_id": None,
            "reply": None,
            "clarification": None,
        },
    )
    resp = client.post("/assistant/chat", json={"message": "is Test Room free tomorrow"}, headers=user_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["intent"] == "availability"
    assert body["bookings"] == []
    assert "free" in body["reply"]


def test_availability_intent_reports_existing_bookings(client, user_headers, resource_id, monkeypatch):
    monkeypatch.setattr(groq_client, "is_configured", lambda: True)
    date_str, _ = _future_local_date_and_hour(48)
    client.post(
        "/bookings",
        json={"resource_id": resource_id, "start_time": f"{date_str}T15:00:00Z", "end_time": f"{date_str}T16:00:00Z"},
        headers=user_headers,
    )
    _mock_parse(
        monkeypatch,
        {
            "intent": "availability",
            "resource_id": resource_id,
            "date": date_str,
            "start_time": None,
            "end_time": None,
            "booking_id": None,
            "reply": None,
            "clarification": None,
        },
    )
    resp = client.post("/assistant/chat", json={"message": "is Test Room free tomorrow"}, headers=user_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["bookings"]) == 1


def test_availability_intent_rejects_hallucinated_resource(client, user_headers, resource_id, monkeypatch):
    monkeypatch.setattr(groq_client, "is_configured", lambda: True)
    _mock_parse(
        monkeypatch,
        {
            "intent": "availability",
            "resource_id": resource_id + 999999,
            "date": "2027-07-15",
            "start_time": None,
            "end_time": None,
            "booking_id": None,
            "reply": None,
            "clarification": None,
        },
    )
    resp = client.post("/assistant/chat", json={"message": "is the moon base free"}, headers=user_headers)
    assert resp.status_code == 200
    assert resp.json()["needs_clarification"] is True


def test_cancel_intent_returns_draft_for_own_booking(client, user_headers, resource_id, monkeypatch):
    monkeypatch.setattr(groq_client, "is_configured", lambda: True)
    date_str, _ = _future_local_date_and_hour(48)
    created = client.post(
        "/bookings",
        json={"resource_id": resource_id, "start_time": f"{date_str}T15:00:00Z", "end_time": f"{date_str}T16:00:00Z"},
        headers=user_headers,
    ).json()
    _mock_parse(
        monkeypatch,
        {
            "intent": "cancel",
            "resource_id": None,
            "date": None,
            "start_time": None,
            "end_time": None,
            "booking_id": created["id"],
            "reply": None,
            "clarification": None,
        },
    )
    resp = client.post("/assistant/chat", json={"message": "cancel my Test Room booking"}, headers=user_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["intent"] == "cancel"
    assert body["cancel_booking_id"] == created["id"]


def test_cancel_intent_rejects_booking_that_is_not_the_users(client, user_headers, admin_headers, resource_id, monkeypatch):
    monkeypatch.setattr(groq_client, "is_configured", lambda: True)
    date_str, _ = _future_local_date_and_hour(48)
    other_users_booking = client.post(
        "/bookings",
        json={"resource_id": resource_id, "start_time": f"{date_str}T15:00:00Z", "end_time": f"{date_str}T16:00:00Z"},
        headers=admin_headers,
    ).json()
    _mock_parse(
        monkeypatch,
        {
            "intent": "cancel",
            "resource_id": None,
            "date": None,
            "start_time": None,
            "end_time": None,
            "booking_id": other_users_booking["id"],
            "reply": None,
            "clarification": None,
        },
    )
    resp = client.post("/assistant/chat", json={"message": "cancel that booking"}, headers=user_headers)
    assert resp.status_code == 200
    assert resp.json()["needs_clarification"] is True


def test_chat_intent_passes_through_model_reply(client, user_headers, monkeypatch):
    monkeypatch.setattr(groq_client, "is_configured", lambda: True)
    _mock_parse(
        monkeypatch,
        {
            "intent": "chat",
            "resource_id": None,
            "date": None,
            "start_time": None,
            "end_time": None,
            "booking_id": None,
            "reply": "Hi! Ask me to book, check availability, list, or cancel a booking.",
            "clarification": None,
        },
    )
    resp = client.post("/assistant/chat", json={"message": "hello"}, headers=user_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["intent"] == "chat"
    assert "book" in body["reply"]
