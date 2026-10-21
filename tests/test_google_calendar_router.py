"""
Tests for app/routers/google_calendar.py — the OAuth connect/login/callback/
status/disconnect HTTP routes. The real Google endpoints (token exchange,
userinfo) are mocked via app.google_calendar (gcal), same pattern as
test_google_calendar_client.py; these tests only exercise routing,
state-token handling, and the DB side effects (encrypting/storing/clearing
google_refresh_token).
"""

from urllib.parse import parse_qs, urlparse

from app import google_calendar as gcal
from app.config import settings
from tests.conftest import register_and_login


def _set_user_refresh_token(email: str, encrypted_value) -> None:
    from app.database import SessionLocal
    from app.models import User

    session = SessionLocal()
    user = session.query(User).filter(User.email == email).first()
    user.google_refresh_token = encrypted_value
    session.commit()
    session.close()


def test_login_503s_when_google_not_configured(client, monkeypatch):
    monkeypatch.setattr(gcal, "is_configured", lambda: False)
    resp = client.get("/auth/google/login")
    assert resp.status_code == 503


def test_login_returns_authorize_url_when_configured(client, monkeypatch):
    monkeypatch.setattr(gcal, "is_configured", lambda: True)
    monkeypatch.setattr(settings, "google_client_id", "client-id")
    resp = client.get("/auth/google/login")
    assert resp.status_code == 200
    assert resp.json()["authorize_url"].startswith(gcal.GOOGLE_AUTH_URL)


def test_connect_requires_auth(client, monkeypatch):
    monkeypatch.setattr(gcal, "is_configured", lambda: True)
    resp = client.get("/auth/google/connect")
    assert resp.status_code == 401


def test_connect_503s_when_google_not_configured(client, user_headers, monkeypatch):
    monkeypatch.setattr(gcal, "is_configured", lambda: False)
    resp = client.get("/auth/google/connect", headers=user_headers)
    assert resp.status_code == 503


def test_connect_returns_authorize_url_with_calendar_scope(client, user_headers, monkeypatch):
    monkeypatch.setattr(gcal, "is_configured", lambda: True)
    monkeypatch.setattr(settings, "google_client_id", "client-id")
    resp = client.get("/auth/google/connect", headers=user_headers)
    assert resp.status_code == 200
    query = parse_qs(urlparse(resp.json()["authorize_url"]).query)
    assert query["scope"] == [gcal.CALENDAR_SCOPE]


def test_status_reports_disconnected_by_default(client, user_headers):
    resp = client.get("/auth/google/status", headers=user_headers)
    assert resp.status_code == 200
    assert resp.json() == {"connected": False}


def test_status_reports_connected_when_token_stored(client, user_headers):
    _set_user_refresh_token("user@example.com", "some-encrypted-value")
    resp = client.get("/auth/google/status", headers=user_headers)
    assert resp.json() == {"connected": True}


def test_disconnect_clears_token(client, user_headers):
    _set_user_refresh_token("user@example.com", "some-encrypted-value")
    resp = client.post("/auth/google/disconnect", headers=user_headers)
    assert resp.status_code == 200
    assert resp.json() == {"connected": False}
    assert client.get("/auth/google/status", headers=user_headers).json() == {"connected": False}


def test_callback_redirects_with_failure_on_google_error_param(client):
    resp = client.get("/auth/google/callback?error=access_denied", follow_redirects=False)
    assert resp.status_code == 307
    assert "google_connected=0" in resp.headers["location"]


def test_callback_redirects_with_failure_when_code_missing(client):
    resp = client.get("/auth/google/callback?state=abc", follow_redirects=False)
    assert resp.status_code == 307
    assert "google_connected=0" in resp.headers["location"]


def test_callback_redirects_with_failure_on_invalid_state_token(client):
    resp = client.get("/auth/google/callback?code=abc&state=not-a-real-jwt", follow_redirects=False)
    assert resp.status_code == 307
    assert "google_connected=0" in resp.headers["location"]


def test_callback_redirects_with_failure_when_exchange_code_fails(client, monkeypatch):
    state = gcal.create_state_token(flow="connect", user_id=1)

    def _boom(code):
        raise RuntimeError("Google token endpoint unreachable")

    monkeypatch.setattr(gcal, "exchange_code", _boom)
    resp = client.get(f"/auth/google/callback?code=abc&state={state}", follow_redirects=False)
    assert resp.status_code == 307
    assert "google_connected=0" in resp.headers["location"]


def test_callback_redirects_with_failure_when_no_refresh_token_returned(client, monkeypatch):
    state = gcal.create_state_token(flow="connect", user_id=1)
    monkeypatch.setattr(gcal, "exchange_code", lambda code: {"access_token": "a"})  # no refresh_token
    resp = client.get(f"/auth/google/callback?code=abc&state={state}", follow_redirects=False)
    assert resp.status_code == 307
    assert "google_connected=0" in resp.headers["location"]


def test_callback_redirects_with_failure_for_unknown_flow(client, monkeypatch):
    state = gcal.create_state_token(flow="something-unexpected")
    monkeypatch.setattr(gcal, "exchange_code", lambda code: {"refresh_token": "r"})
    resp = client.get(f"/auth/google/callback?code=abc&state={state}", follow_redirects=False)
    assert resp.status_code == 307
    assert "google_connected=0" in resp.headers["location"]


# --- connect flow -----------------------------------------------------


def test_callback_connect_flow_success_stores_encrypted_token(client, user_headers, monkeypatch):
    from app.database import SessionLocal
    from app.models import User

    whoami = client.get("/bookings/me", headers=user_headers)
    assert whoami.status_code == 200
    session = SessionLocal()
    user = session.query(User).filter(User.email == "user@example.com").first()
    user_id = user.id
    session.close()

    state = gcal.create_state_token(flow="connect", user_id=user_id)
    monkeypatch.setattr(gcal, "exchange_code", lambda code: {"refresh_token": "raw-refresh-token-value"})
    resp = client.get(f"/auth/google/callback?code=abc&state={state}", follow_redirects=False)
    assert resp.status_code == 307
    assert "google_connected=1" in resp.headers["location"]

    session = SessionLocal()
    stored = session.query(User).filter(User.id == user_id).first().google_refresh_token
    session.close()
    assert stored is not None
    assert stored != "raw-refresh-token-value"  # encrypted, not plaintext

    from app import crypto

    assert crypto.decrypt(stored) == "raw-refresh-token-value"


def test_callback_connect_flow_fails_for_unknown_user(client, monkeypatch):
    state = gcal.create_state_token(flow="connect", user_id=999999)
    monkeypatch.setattr(gcal, "exchange_code", lambda code: {"refresh_token": "r"})
    resp = client.get(f"/auth/google/callback?code=abc&state={state}", follow_redirects=False)
    assert resp.status_code == 307
    assert "google_connected=0" in resp.headers["location"]


def test_callback_connect_flow_swallows_missing_encryption_key(client, monkeypatch):
    from app import crypto
    from app.database import SessionLocal
    from app.models import User

    register_and_login(client, "connect-noenc@example.com")
    session = SessionLocal()
    user = session.query(User).filter(User.email == "connect-noenc@example.com").first()
    user_id = user.id
    session.close()

    state = gcal.create_state_token(flow="connect", user_id=user_id)
    monkeypatch.setattr(gcal, "exchange_code", lambda code: {"refresh_token": "r"})
    monkeypatch.setattr(crypto, "_fernet", None)  # simulate TOKEN_ENCRYPTION_KEY unset
    resp = client.get(f"/auth/google/callback?code=abc&state={state}", follow_redirects=False)
    assert resp.status_code == 307
    assert "google_connected=0" in resp.headers["location"]


# --- login flow ---------------------------------------------------------


def test_callback_login_flow_fails_when_userinfo_lookup_fails(client, monkeypatch):
    state = gcal.create_state_token(flow="login")
    monkeypatch.setattr(gcal, "exchange_code", lambda code: {"refresh_token": "r", "access_token": "a"})

    def _boom(access_token):
        raise RuntimeError("userinfo unreachable")

    monkeypatch.setattr(gcal, "get_user_info", _boom)
    resp = client.get(f"/auth/google/callback?code=abc&state={state}", follow_redirects=False)
    assert resp.status_code == 307
    assert "google_connected=0" in resp.headers["location"]


def test_callback_login_flow_fails_when_email_not_verified(client, monkeypatch):
    state = gcal.create_state_token(flow="login")
    monkeypatch.setattr(gcal, "exchange_code", lambda code: {"refresh_token": "r", "access_token": "a"})
    monkeypatch.setattr(gcal, "get_user_info", lambda access_token: {"email": "x@example.com", "email_verified": False})
    resp = client.get(f"/auth/google/callback?code=abc&state={state}", follow_redirects=False)
    assert resp.status_code == 307
    assert "google_connected=0" in resp.headers["location"]


def test_callback_login_flow_creates_new_user_and_logs_in(client, monkeypatch):
    state = gcal.create_state_token(flow="login")
    monkeypatch.setattr(gcal, "exchange_code", lambda code: {"refresh_token": "r", "access_token": "a"})
    monkeypatch.setattr(
        gcal, "get_user_info", lambda access_token: {"email": "brand-new@example.com", "email_verified": True}
    )
    resp = client.get(f"/auth/google/callback?code=abc&state={state}", follow_redirects=False)
    assert resp.status_code == 307
    location = resp.headers["location"]
    assert "google_connected=1" in location
    assert "login_token=" in location
    assert "email=brand-new%40example.com" in location

    from app.database import SessionLocal
    from app.models import User

    session = SessionLocal()
    user = session.query(User).filter(User.email == "brand-new@example.com").first()
    assert user is not None
    assert user.hashed_password is None  # Google-only account, nothing to hash
    assert user.google_refresh_token is not None
    session.close()


def test_callback_login_flow_logs_in_existing_user(client, monkeypatch):
    register_and_login(client, "already-exists@example.com")
    state = gcal.create_state_token(flow="login")
    monkeypatch.setattr(gcal, "exchange_code", lambda code: {"refresh_token": "r", "access_token": "a"})
    monkeypatch.setattr(
        gcal, "get_user_info", lambda access_token: {"email": "already-exists@example.com", "email_verified": True}
    )
    resp = client.get(f"/auth/google/callback?code=abc&state={state}", follow_redirects=False)
    assert resp.status_code == 307
    assert "login_token=" in resp.headers["location"]

    # the returned login_token must actually work
    import re

    token = re.search(r"login_token=([^&]+)", resp.headers["location"]).group(1)
    me = client.get("/bookings/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200


def test_callback_login_flow_swallows_missing_encryption_key(client, monkeypatch):
    from app import crypto

    state = gcal.create_state_token(flow="login")
    monkeypatch.setattr(gcal, "exchange_code", lambda code: {"refresh_token": "r", "access_token": "a"})
    monkeypatch.setattr(
        gcal, "get_user_info", lambda access_token: {"email": "no-enc-login@example.com", "email_verified": True}
    )
    monkeypatch.setattr(crypto, "_fernet", None)  # simulate TOKEN_ENCRYPTION_KEY unset
    resp = client.get(f"/auth/google/callback?code=abc&state={state}", follow_redirects=False)
    # Login still succeeds even though the token couldn't be stored —
    # identity/auth is independent of calendar sync.
    assert resp.status_code == 307
    assert "login_token=" in resp.headers["location"]

    from app.database import SessionLocal
    from app.models import User

    session = SessionLocal()
    user = session.query(User).filter(User.email == "no-enc-login@example.com").first()
    assert user is not None
    assert user.google_refresh_token is None
    session.close()
