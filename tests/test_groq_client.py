"""
Direct unit tests for app/groq_client.py — the parsing module itself, not
the /assistant/parse endpoint (that's covered via mocked
groq_client.parse_booking_request in test_assistant.py). These tests mock
requests.post (never hit the real Groq API) and focus on the two real bugs
called out in the module's own docstring and comments:

1. combine_local_datetime does DST-aware UTC conversion in code (via
   zoneinfo), specifically so the LLM never has to do timezone arithmetic
   itself (observed wrong on a real Groq response). Verified here across
   both a CEST (summer, UTC+2) and a CET (winter, UTC+1) date, plus right
   at the literal DST transition.
2. The system prompt tells the model "default to 1 hour" but the code
   (app/routers/assistant.py) is what actually enforces that default when
   the model leaves end_time null — groq_client itself doesn't set a
   default, it just has to return the field as None faithfully so the
   caller's fallback kicks in. Covered here by asserting the raw parsed
   dict passes end_time through unchanged.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app import groq_client


class _FakeResponse:
    def __init__(self, json_body, status_code=200):
        self._json_body = json_body
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json_body


def _chat_completion(content: str):
    return _FakeResponse({"choices": [{"message": {"content": content}}]})


def test_is_configured_reflects_settings(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "groq_api_key", "")
    assert groq_client.is_configured() is False
    monkeypatch.setattr(settings, "groq_api_key", "gsk_real_looking_key")
    assert groq_client.is_configured() is True


def test_combine_local_datetime_summer_is_cest_utc_plus_2():
    result = groq_client.combine_local_datetime("2027-07-15", "15:00")
    assert result.tzinfo == UTC
    assert result == datetime(2027, 7, 15, 13, 0, tzinfo=UTC)


def test_combine_local_datetime_winter_is_cet_utc_plus_1():
    result = groq_client.combine_local_datetime("2027-01-15", "15:00")
    assert result == datetime(2027, 1, 15, 14, 0, tzinfo=UTC)


def test_combine_local_datetime_handles_dst_transition_correctly():
    # 2027-03-28 is the last Sunday of March 2027 — Europe/Berlin's actual
    # spring-forward date (02:00 CET -> 03:00 CEST). An hour requested just
    # after the transition must resolve as CEST (UTC+2), proving this goes
    # through zoneinfo's real transition table rather than a fixed offset.
    before = groq_client.combine_local_datetime("2027-03-28", "01:00")  # still CET
    after = groq_client.combine_local_datetime("2027-03-28", "03:00")  # already CEST
    assert before == datetime(2027, 3, 28, 0, 0, tzinfo=UTC)  # 01:00 CET == 00:00 UTC
    assert after == datetime(2027, 3, 28, 1, 0, tzinfo=UTC)  # 03:00 CEST == 01:00 UTC


def test_combine_local_datetime_matches_zoneinfo_directly():
    naive = datetime(2027, 11, 1, 9, 30)
    expected = naive.replace(tzinfo=ZoneInfo("Europe/Berlin")).astimezone(UTC)
    assert groq_client.combine_local_datetime("2027-11-01", "09:30") == expected


def test_parse_booking_request_well_formed_response(monkeypatch):
    captured = {}

    def fake_post(url, headers, json, timeout):
        captured["url"] = url
        captured["json"] = json
        return _chat_completion(
            '{"resource_id": 1, "date": "2027-07-15", "start_time": "15:00", '
            '"end_time": "16:00", "clarification": null}'
        )

    monkeypatch.setattr(groq_client.requests, "post", fake_post)
    resources = [{"id": 1, "name": "Meeting Room A"}]
    result = groq_client.parse_booking_request("book room A tomorrow at 3pm", resources, datetime.now(UTC))

    assert result == {
        "resource_id": 1,
        "date": "2027-07-15",
        "start_time": "15:00",
        "end_time": "16:00",
        "clarification": None,
    }
    assert captured["url"] == groq_client.GROQ_CHAT_URL
    assert captured["json"]["model"] == groq_client.GROQ_MODEL
    # system prompt must actually name the resource so the model can pick it
    assert "Meeting Room A" in captured["json"]["messages"][0]["content"]


def test_parse_booking_request_passes_through_null_end_time(monkeypatch):
    # Real bug #2: the model is told to default to a 1-hour booking, but
    # observed (per the module docstring) to sometimes leave end_time null
    # anyway. groq_client must not paper over that itself — it has to
    # return exactly what the model said, null and all, so the *caller*
    # (app/routers/assistant.py) applies the deterministic +1h fallback.
    monkeypatch.setattr(
        groq_client.requests,
        "post",
        lambda *a, **k: _chat_completion(
            '{"resource_id": 1, "date": "2027-07-15", "start_time": "15:00", '
            '"end_time": null, "clarification": null}'
        ),
    )
    result = groq_client.parse_booking_request("book room A at 3pm", [{"id": 1, "name": "Room A"}], datetime.now(UTC))
    assert result["end_time"] is None
    assert result["start_time"] == "15:00"


def test_parse_booking_request_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(groq_client.requests, "post", lambda *a, **k: _FakeResponse({}, status_code=500))
    try:
        groq_client.parse_booking_request("book something", [], datetime.now(UTC))
        raised = False
    except RuntimeError:
        raised = True
    assert raised  # caller (assistant.py) is responsible for catching this as a 502


def test_parse_booking_request_raises_on_malformed_json(monkeypatch):
    monkeypatch.setattr(groq_client.requests, "post", lambda *a, **k: _chat_completion("not json"))
    try:
        groq_client.parse_booking_request("book something", [], datetime.now(UTC))
        raised = False
    except Exception:
        raised = True
    assert raised


def test_system_prompt_lists_every_resource_id():
    now_local = datetime(2027, 7, 15, 10, 0)
    resources = [{"id": 1, "name": "Room A"}, {"id": 2, "name": "Room B"}]
    prompt = groq_client._system_prompt(resources, now_local)
    assert "id=1: Room A" in prompt
    assert "id=2: Room B" in prompt
    assert "1-hour duration" in prompt
