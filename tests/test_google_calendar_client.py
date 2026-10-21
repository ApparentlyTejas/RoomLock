"""
Direct unit tests for app/google_calendar.py (the OAuth/Calendar-API
client itself) — not the routes in app/routers/google_calendar.py, which
have their own test file. Every Google-facing call is mocked (requests.get
/ requests.post / requests.delete), matching the pattern test_assistant.py
uses for Groq: no real network calls.
"""

from datetime import UTC, datetime

import pytest
import requests
from jose import JWTError, jwt

from app import google_calendar as gcal
from app.config import settings


class _FakeResponse:
    def __init__(self, json_body=None, status_code=200):
        self._json_body = json_body or {}
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._json_body


def test_is_configured_requires_both_id_and_secret(monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "")
    monkeypatch.setattr(settings, "google_client_secret", "")
    assert gcal.is_configured() is False

    monkeypatch.setattr(settings, "google_client_id", "client-id")
    monkeypatch.setattr(settings, "google_client_secret", "")
    assert gcal.is_configured() is False

    monkeypatch.setattr(settings, "google_client_secret", "client-secret")
    assert gcal.is_configured() is True


def test_state_token_round_trip_connect_flow():
    token = gcal.create_state_token(flow="connect", user_id=42)
    payload = gcal.verify_state_token(token)
    assert payload["flow"] == "connect"
    assert payload["sub"] == "42"
    assert payload["aud"] == gcal.STATE_AUDIENCE


def test_state_token_round_trip_login_flow_has_no_sub():
    token = gcal.create_state_token(flow="login")
    payload = gcal.verify_state_token(token)
    assert payload["flow"] == "login"
    assert "sub" not in payload


def test_verify_state_token_rejects_wrong_audience():
    # A token signed for something else entirely shouldn't be accepted as
    # Google-OAuth state, even with the same secret/algorithm.
    bad_token = jwt.encode(
        {"aud": "something-else", "flow": "connect"},
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(JWTError):
        gcal.verify_state_token(bad_token)


def test_verify_state_token_rejects_tampered_token():
    token = gcal.create_state_token(flow="connect", user_id=1)
    with pytest.raises(JWTError):
        gcal.verify_state_token(token + "tampered")


def test_get_authorize_url_contains_expected_params(monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "my-client-id")
    monkeypatch.setattr(settings, "google_redirect_uri", "http://localhost:8000/auth/google/callback")
    url = gcal.get_authorize_url("some-state", gcal.CALENDAR_SCOPE)
    assert url.startswith(gcal.GOOGLE_AUTH_URL)
    assert "client_id=my-client-id" in url
    assert "state=some-state" in url
    assert "access_type=offline" in url
    assert "prompt=consent" in url


def test_exchange_code_posts_to_token_url_and_returns_json(monkeypatch):
    captured = {}

    def fake_post(url, data, timeout):
        captured["url"] = url
        captured["data"] = data
        return _FakeResponse({"access_token": "a", "refresh_token": "r"})

    monkeypatch.setattr(gcal.requests, "post", fake_post)
    body = gcal.exchange_code("auth-code-123")
    assert body == {"access_token": "a", "refresh_token": "r"}
    assert captured["url"] == gcal.GOOGLE_TOKEN_URL
    assert captured["data"]["code"] == "auth-code-123"
    assert captured["data"]["grant_type"] == "authorization_code"


def test_exchange_code_raises_on_error_response(monkeypatch):
    monkeypatch.setattr(gcal.requests, "post", lambda *a, **k: _FakeResponse({}, status_code=400))
    with pytest.raises(requests.exceptions.HTTPError):
        gcal.exchange_code("bad-code")


def test_get_user_info_sends_bearer_token(monkeypatch):
    captured = {}

    def fake_get(url, headers, timeout):
        captured["headers"] = headers
        return _FakeResponse({"email": "x@example.com", "email_verified": True})

    monkeypatch.setattr(gcal.requests, "get", fake_get)
    info = gcal.get_user_info("my-access-token")
    assert info["email"] == "x@example.com"
    assert captured["headers"]["Authorization"] == "Bearer my-access-token"


def test_get_access_token_posts_refresh_grant(monkeypatch):
    def fake_post(url, data, timeout):
        assert data["grant_type"] == "refresh_token"
        assert data["refresh_token"] == "my-refresh"
        return _FakeResponse({"access_token": "fresh-access-token"})

    monkeypatch.setattr(gcal.requests, "post", fake_post)
    token = gcal._get_access_token("my-refresh")
    assert token == "fresh-access-token"


def test_create_event_returns_google_event_id(monkeypatch):
    monkeypatch.setattr(gcal, "_get_access_token", lambda refresh_token: "access-token")
    captured = {}

    def fake_post(url, headers, json, timeout):
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse({"id": "google-event-id-123"})

    monkeypatch.setattr(gcal.requests, "post", fake_post)
    event_id = gcal.create_event(
        "refresh-token",
        summary="Room booking",
        description="Booked via RoomLock",
        start=datetime(2027, 1, 1, 10, 0, tzinfo=UTC),
        end=datetime(2027, 1, 1, 11, 0, tzinfo=UTC),
    )
    assert event_id == "google-event-id-123"
    assert captured["url"] == gcal.GOOGLE_CALENDAR_EVENTS_URL
    assert captured["json"]["summary"] == "Room booking"


def test_create_event_raises_when_google_api_fails(monkeypatch):
    # This is the failure mode booking creation must catch and swallow —
    # covered end-to-end in test_bookings_google_sync.py. Here we just
    # confirm the client itself does raise rather than failing silently
    # (the caller needs a real exception to catch).
    monkeypatch.setattr(gcal, "_get_access_token", lambda refresh_token: "access-token")
    monkeypatch.setattr(gcal.requests, "post", lambda *a, **k: _FakeResponse({}, status_code=503))
    with pytest.raises(requests.exceptions.HTTPError):
        gcal.create_event(
            "refresh-token",
            summary="x",
            description="y",
            start=datetime(2027, 1, 1, 10, 0, tzinfo=UTC),
            end=datetime(2027, 1, 1, 11, 0, tzinfo=UTC),
        )


def test_update_event_patches_existing_event(monkeypatch):
    monkeypatch.setattr(gcal, "_get_access_token", lambda refresh_token: "access-token")
    captured = {}

    def fake_patch(url, headers, json, timeout):
        captured["url"] = url
        return _FakeResponse({})

    monkeypatch.setattr(gcal.requests, "patch", fake_patch)
    gcal.update_event(
        "refresh-token", "event-id-1", datetime(2027, 1, 1, 10, 0, tzinfo=UTC), datetime(2027, 1, 1, 11, 0, tzinfo=UTC)
    )
    assert captured["url"] == f"{gcal.GOOGLE_CALENDAR_EVENTS_URL}/event-id-1"


def test_delete_event_treats_204_as_success(monkeypatch):
    monkeypatch.setattr(gcal, "_get_access_token", lambda refresh_token: "access-token")
    monkeypatch.setattr(gcal.requests, "delete", lambda *a, **k: _FakeResponse({}, status_code=204))
    gcal.delete_event("refresh-token", "event-id-1")  # must not raise


def test_delete_event_treats_410_gone_as_success(monkeypatch):
    # Event already deleted directly in Google Calendar by the user — not
    # an error for us, see the comment in app/google_calendar.py.
    monkeypatch.setattr(gcal, "_get_access_token", lambda refresh_token: "access-token")
    monkeypatch.setattr(gcal.requests, "delete", lambda *a, **k: _FakeResponse({}, status_code=410))
    gcal.delete_event("refresh-token", "event-id-1")  # must not raise


def test_delete_event_raises_on_other_error_codes(monkeypatch):
    monkeypatch.setattr(gcal, "_get_access_token", lambda refresh_token: "access-token")
    monkeypatch.setattr(gcal.requests, "delete", lambda *a, **k: _FakeResponse({}, status_code=500))
    with pytest.raises(requests.exceptions.HTTPError):
        gcal.delete_event("refresh-token", "event-id-1")
