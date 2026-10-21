"""
Google Calendar connect + "Sign in with Google" flows. Routes only — the
OAuth/API client lives in app/google_calendar.py.

Two ways in, one shared callback:
- /login: anonymous entry point. Requests identity (openid/email/profile)
  scope alongside calendar scope in one consent screen, so a new visitor
  ends up registered/logged-in AND calendar-connected from a single click.
  Since this is a plain browser redirect (no Authorization header
  available for the frontend to attach), the resulting JWT comes back as a
  query param on the redirect to /ui/, not a JSON response — the frontend
  picks it up and stores it exactly like a normal login.
- /connect: authenticated entry point for a user who's already logged in
  (password or Google) and just wants to add calendar sync. Calendar-only
  scope, no identity request needed since we already know who they are.

Both funnel through /callback, which tells them apart via the `flow` claim
on the signed `state` token created above.
"""

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import crypto
from app import google_calendar as gcal
from app.auth import create_access_token, get_current_user
from app.database import get_db
from app.models import User

router = APIRouter(prefix="/auth/google", tags=["google-calendar"])


@router.get("/login")
def login():
    if not gcal.is_configured():
        raise HTTPException(status_code=503, detail="Google Calendar integration not configured")
    state = gcal.create_state_token(flow="login")
    return {"authorize_url": gcal.get_authorize_url(state, gcal.LOGIN_SCOPE)}


@router.get("/connect")
def connect(current_user: User = Depends(get_current_user)):
    if not gcal.is_configured():
        raise HTTPException(status_code=503, detail="Google Calendar integration not configured")
    state = gcal.create_state_token(flow="connect", user_id=current_user.id)
    return {"authorize_url": gcal.get_authorize_url(state, gcal.CALENDAR_SCOPE)}


@router.get("/callback")
def callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    if error or not code or not state:
        return RedirectResponse(url="/ui/?google_connected=0")

    try:
        state_payload = gcal.verify_state_token(state)
        token_body = gcal.exchange_code(code)
        refresh_token = token_body.get("refresh_token")
        if not refresh_token:
            raise ValueError("Google did not return a refresh token")
    except Exception:
        return RedirectResponse(url="/ui/?google_connected=0")

    flow = state_payload.get("flow")

    if flow == "connect":
        return _finish_connect(db, state_payload.get("sub"), refresh_token)
    if flow == "login":
        return _finish_login(db, token_body, refresh_token)
    return RedirectResponse(url="/ui/?google_connected=0")


def _finish_connect(db: Session, user_id_claim: str | None, refresh_token: str) -> RedirectResponse:
    user = db.query(User).filter(User.id == int(user_id_claim)).first() if user_id_claim else None
    if not user:
        return RedirectResponse(url="/ui/?google_connected=0")
    try:
        user.google_refresh_token = crypto.encrypt(refresh_token)
    except RuntimeError:
        return RedirectResponse(url="/ui/?google_connected=0")
    db.commit()
    return RedirectResponse(url="/ui/?google_connected=1")


def _finish_login(db: Session, token_body: dict, refresh_token: str) -> RedirectResponse:
    try:
        info = gcal.get_user_info(token_body["access_token"])
    except Exception:
        return RedirectResponse(url="/ui/?google_connected=0")

    email = info.get("email")
    if not email or not info.get("email_verified"):
        return RedirectResponse(url="/ui/?google_connected=0")

    user = db.query(User).filter(User.email == email).first()
    if not user:
        # Google-only account: nothing to hash, hashed_password stays NULL
        # (see db/005_google_login.sql). A concurrent double-click could
        # race two inserts for the same brand-new email — same "app check
        # as fast path, DB constraint as guarantee" shape as booking
        # creation: catch the unique-email violation and fall back to
        # whichever request actually won.
        user = User(email=email, hashed_password=None)
        db.add(user)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            user = db.query(User).filter(User.email == email).first()
            if not user:
                return RedirectResponse(url="/ui/?google_connected=0")

    try:
        user.google_refresh_token = crypto.encrypt(refresh_token)
        db.commit()
    except RuntimeError:
        db.rollback()
    db.refresh(user)

    # See the matching comment in routers/auth.py: email is non-nullable in
    # the schema, but the sqlalchemy mypy plugin widens legacy Column()
    # attributes to Optional regardless.
    access_token = create_access_token(subject=user.email)  # type: ignore[arg-type]
    query = urlencode({"login_token": access_token, "email": user.email, "google_connected": "1"})
    return RedirectResponse(url=f"/ui/?{query}")


@router.get("/status")
def status(current_user: User = Depends(get_current_user)):
    return {"connected": bool(current_user.google_refresh_token)}


@router.post("/disconnect")
def disconnect(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    current_user.google_refresh_token = None
    db.commit()
    return {"connected": False}
