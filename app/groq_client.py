"""
Groq client for the natural-language booking assistant.

This module has exactly one job: turn a free-text message, plus whatever
chat history came before it, into structured JSON describing what the
user wants (book a room, check availability, list or cancel a booking, or
just chat) and any fields needed to act on it. It never writes to the
database and never talks to /bookings directly — the caller
(app/routers/assistant.py) re-validates every field against real data
(the actual resource list, the actual clock, the user's actual bookings)
before anything is shown to the user as actionable. Creating a booking
still goes through the normal, concurrency-safe POST /bookings endpoint,
and cancelling still goes through DELETE /bookings/{id} — the model is a
fuzzy front end bolted onto an unchanged, strict backend, not a shortcut
around it.

GPT OSS 120B via Groq: free tier, OpenAI-compatible REST API, native JSON
mode (response_format={"type": "json_object"}) is enough for this — no
function-calling or agent framework needed for a single structured
extraction. (Originally built against Llama 3.3 70B; switched after Groq
announced its decommission on 2026-08-16 — same open-weight-model-via-Groq
approach, just the model id changed.)
"""

import json
from datetime import UTC, datetime

import requests

from app.config import settings

GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "openai/gpt-oss-120b"
DISPLAY_TZ = "Europe/Berlin"


def is_configured() -> bool:
    return bool(settings.groq_api_key)


def _system_prompt(resources: list[dict], bookings: list[dict], now_local: datetime) -> str:
    resource_lines = "\n".join(f"- id={r['id']}: {r['name']}" for r in resources)
    if bookings:
        booking_lines = "\n".join(
            f"- booking_id={b['id']}: {b['resource_name']} on "
            f"{b['start_time'].strftime('%A %Y-%m-%d %H:%M')}-{b['end_time'].strftime('%H:%M')}"
            for b in bookings
        )
    else:
        booking_lines = "(the user has no upcoming bookings)"
    return f"""You classify a short message from a room-booking app into one intent and extract its fields. You do not book, cancel, or look anything up yourself — you only propose; a human (or the real backend) confirms.

Current date/time: {now_local.strftime("%A, %Y-%m-%d %H:%M")} local time (interpret relative times like "tomorrow" or "3pm" against this — already local, do not convert timezones yourself).

Available resources (you MUST use one of these ids for "book" or "availability", never invent one):
{resource_lines}

The user's own upcoming bookings (use these ids for "cancel", never invent one):
{booking_lines}

Pick exactly one intent:
- "book": wants to create a new booking.
- "availability": wants to know if a room is free, or what's already booked on it.
- "list_bookings": wants to see their own upcoming bookings.
- "cancel": wants to cancel one of their own bookings listed above.
- "chat": anything else (greeting, question about the app, unclear) — put a short, direct, helpful answer in "reply".

Bookings default to a 1-hour duration unless the user gives an explicit end time or duration.

Respond with ONLY a JSON object, no other text, matching exactly this shape:
{{
  "intent": "book" | "availability" | "list_bookings" | "cancel" | "chat",
  "resource_id": <int or null, for "book"/"availability">,
  "date": "<YYYY-MM-DD, local calendar date, or null>",
  "start_time": "<HH:MM in 24-hour local time, e.g. 15:00, or null>",
  "end_time": "<HH:MM in 24-hour local time, or null>",
  "booking_id": <int or null, for "cancel" — must be one of the booking_id values above>,
  "reply": "<short natural-language reply, used for \\"chat\\" intent, else null>",
  "clarification": "<a short question to ask if something required is missing or ambiguous, else null>"
}}

Give date/start_time/end_time as plain local wall-clock values exactly as a person would say them — do NOT convert to UTC or apply any timezone offset, that's handled separately. If a "book" request doesn't clearly name one of the resources above, or gives no usable time, or a "cancel" request doesn't clearly match one of the bookings above, set clarification instead of guessing."""


def combine_local_datetime(date_str: str, time_str: str) -> datetime:
    """
    Interprets a YYYY-MM-DD date and an HH:MM time as wall-clock local time
    in DISPLAY_TZ and returns the equivalent UTC datetime. Doing this
    conversion here (with zoneinfo, which knows the real CET/CEST
    transition dates) rather than asking the model to do it itself is
    deliberate — DST-aware offset arithmetic is exactly the kind of thing
    LLMs get wrong (observed: off by an hour on a real Groq response during
    testing), so the model's job is narrowed to extracting plain local
    values a human would say, never a UTC conversion.
    """
    from zoneinfo import ZoneInfo

    naive = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    local = naive.replace(tzinfo=ZoneInfo(DISPLAY_TZ))
    return local.astimezone(UTC)


def parse_assistant_message(
    message: str,
    resources: list[dict],
    bookings: list[dict],
    history: list[dict],
    now_utc: datetime,
) -> dict:
    """
    Returns the model's raw parsed dict (intent, resource_id, date,
    start_time, end_time, booking_id, reply, clarification) exactly as it
    came back — the caller is responsible for validating every field
    before trusting it. `history` is prior {role, content} turns, replayed
    as real chat messages so a follow-up answering a clarifying question
    has the original request as context.
    """
    from zoneinfo import ZoneInfo

    try:
        now_local = now_utc.astimezone(ZoneInfo(DISPLAY_TZ))
    except Exception:
        now_local = now_utc

    resp = requests.post(
        GROQ_CHAT_URL,
        headers={"Authorization": f"Bearer {settings.groq_api_key}"},
        json={
            "model": GROQ_MODEL,
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": 300,
            "messages": [
                {"role": "system", "content": _system_prompt(resources, bookings, now_local)},
                *history,
                {"role": "user", "content": message},
            ],
        },
        timeout=15,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    return json.loads(content)
