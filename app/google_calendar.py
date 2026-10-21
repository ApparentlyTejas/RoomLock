"""
Google Calendar OAuth client. Two entry points share this module:

1. "Connect" — an already-logged-in user (however they logged in) links
   their Google Calendar. Requests only the calendar.events scope.
2. "Sign in with Google" — an anonymous visitor authenticates AND connects
   calendar in one consent screen. Additionally requests openid/email/
   profile scope, so the callback can identify (or create) the account via
   Google's userinfo endpoint.

Both funnel through the same redirect URI; the signed `state` JWT (Google
redirects the browser back with no Authorization header available, so this
is how the callback knows what's going on) carries a `flow` claim
distinguishing them, and a `sub` (user id) only for the connect flow, where
the user is already known.

Every call here that touches an existing booking (create_event,
delete_event) is used as a best-effort side effect — callers must catch
broadly, since a Google API hiccup (network, expired/revoked token, quota)
must never break the actual booking. That's enforced at the call site, not
here.
"""

from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import requests
from jose import jwt

from app.config import settings

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
GOOGLE_CALENDAR_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
STATE_AUDIENCE = "google-oauth-state"

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"
LOGIN_SCOPE = f"openid email profile {CALENDAR_SCOPE}"


def is_configured() -> bool:
    return bool(settings.google_client_id and settings.google_client_secret)


def create_state_token(flow: str, user_id: int | None = None) -> str:
    payload = {
        "aud": STATE_AUDIENCE,
        "flow": flow,
        "exp": datetime.now(UTC) + timedelta(minutes=5),
    }
    if user_id is not None:
        payload["sub"] = str(user_id)
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def verify_state_token(token: str) -> dict:
    return jwt.decode(
        token,
        settings.jwt_secret_key,
        algorithms=[settings.jwt_algorithm],
        audience=STATE_AUDIENCE,
    )


def get_authorize_url(state: str, scope: str) -> str:
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": scope,
        # access_type=offline + prompt=consent: forces Google to return a
        # refresh_token every time, not just on first-ever consent.
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str) -> dict:
    resp = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": settings.google_redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def get_user_info(access_token: str) -> dict:
    resp = requests.get(
        GOOGLE_USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _get_access_token(refresh_token: str) -> str:
    resp = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "refresh_token": refresh_token,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "grant_type": "refresh_token",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def create_event(
    refresh_token: str,
    summary: str,
    description: str,
    start: datetime,
    end: datetime,
) -> str:
    access_token = _get_access_token(refresh_token)
    resp = requests.post(
        GOOGLE_CALENDAR_EVENTS_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        json={
            "summary": summary,
            "description": description,
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def update_event(refresh_token: str, event_id: str, start: datetime, end: datetime) -> None:
    access_token = _get_access_token(refresh_token)
    resp = requests.patch(
        f"{GOOGLE_CALENDAR_EVENTS_URL}/{event_id}",
        headers={"Authorization": f"Bearer {access_token}"},
        json={
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
        },
        timeout=10,
    )
    resp.raise_for_status()


def delete_event(refresh_token: str, event_id: str) -> None:
    access_token = _get_access_token(refresh_token)
    resp = requests.delete(
        f"{GOOGLE_CALENDAR_EVENTS_URL}/{event_id}",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    # 410 Gone means the event was already deleted (e.g. by the user,
    # directly in Google Calendar) — treat that as success, not an error.
    if resp.status_code not in (204, 410):
        resp.raise_for_status()
