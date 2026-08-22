"""
Read-only iCal subscription feed: GET /calendar/feed/{token}.ics returns a
live .ics feed of a user's upcoming bookings that Outlook, Apple Calendar,
or Google Calendar ("From URL") can subscribe to once and refresh on their
own from then on — no OAuth, unlike the Google Calendar auto-sync
integration, so it works regardless of which calendar provider someone
uses.

The token is a capability secret embedded in the URL, not a session/JWT —
a calendar app polling a URL can't attach an Authorization header, so
knowledge of the token IS the auth. Treated like a credential: high
entropy (32 random bytes via secrets.token_urlsafe), 404 (not 401/403) on
an unrecognized token so a guess can't be distinguished from "this
endpoint doesn't check tokens," and revocable via /feed-token/regenerate
if a URL ever leaks.
"""

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.models import Booking, Resource, User
from app.schemas import CalendarFeedTokenOut

router = APIRouter(prefix="/calendar", tags=["calendar-feed"])


def _ics_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _ics_datetime(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@router.get("/feed-token", response_model=CalendarFeedTokenOut)
def get_feed_token(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if not current_user.calendar_feed_token:
        current_user.calendar_feed_token = secrets.token_urlsafe(32)
        db.commit()
    return CalendarFeedTokenOut(token=current_user.calendar_feed_token)


@router.post("/feed-token/regenerate", response_model=CalendarFeedTokenOut)
def regenerate_feed_token(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    current_user.calendar_feed_token = secrets.token_urlsafe(32)
    db.commit()
    return CalendarFeedTokenOut(token=current_user.calendar_feed_token)


@router.get("/feed/{token}.ics")
def calendar_feed(token: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.calendar_feed_token == token).first()
    if not user:
        raise HTTPException(status_code=404, detail="Not found")

    now = datetime.now(timezone.utc)
    rows = (
        db.query(Booking, Resource.name)
        .join(Resource, Resource.id == Booking.resource_id)
        .filter(Booking.user_id == user.id, Booking.end_time > now)
        .order_by(Booking.start_time)
        .all()
    )

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//RoomLock//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:RoomLock bookings",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        "X-PUBLISHED-TTL:PT1H",
    ]
    dtstamp = _ics_datetime(now)
    for booking, resource_name in rows:
        lines += [
            "BEGIN:VEVENT",
            f"UID:booking-{booking.id}@roomlock",
            f"DTSTAMP:{dtstamp}",
            f"DTSTART:{_ics_datetime(booking.start_time)}",
            f"DTEND:{_ics_datetime(booking.end_time)}",
            f"SUMMARY:{_ics_escape(resource_name + ' booking')}",
            f"DESCRIPTION:{_ics_escape('Booked via RoomLock')}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    ics_content = "\r\n".join(lines) + "\r\n"

    return Response(content=ics_content, media_type="text/calendar; charset=utf-8")
