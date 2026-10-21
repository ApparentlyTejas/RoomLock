"""
Groq client for the natural-language booking assistant.

This module has exactly one job: turn a free-text sentence like "book
Meeting Room A tomorrow at 3pm" into a structured {resource_id, start_time,
end_time} draft. It never writes to the database and never talks to
/bookings directly — the caller (app/routers/assistant.py) validates the
model's output against the real resource list and re-checks every
constraint (valid resource, end after start, not in the past) before
handing it back to the frontend as a draft the user must still confirm.
The actual booking is still created through the normal, concurrency-safe
POST /bookings endpoint like any other request — the model is a fuzzy
front end bolted onto an unchanged, strict backend, not a shortcut around
it.

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


def _system_prompt(resources: list[dict], now_local: datetime) -> str:
    resource_lines = "\n".join(f"- id={r['id']}: {r['name']}" for r in resources)
    return f"""You turn a short booking request into structured JSON. You do not book anything yourself — you only extract fields for a human to confirm.

Current date/time: {now_local.strftime("%A, %Y-%m-%d %H:%M")} local time (interpret all relative times like "tomorrow" or "3pm" against this — this is already the user's local time, do not convert timezones yourself).

Available resources (you MUST use one of these ids, never invent one):
{resource_lines}

Bookings default to a 1-hour duration unless the user gives an explicit end time or duration.

Respond with ONLY a JSON object, no other text, matching exactly this shape:
{{
  "resource_id": <int or null>,
  "date": "<YYYY-MM-DD, local calendar date, or null>",
  "start_time": "<HH:MM in 24-hour local time, e.g. 15:00, or null>",
  "end_time": "<HH:MM in 24-hour local time, or null>",
  "clarification": "<a short question to ask the user if resource/time is missing or ambiguous, else null>"
}}

Give date/start_time/end_time as plain local wall-clock values exactly as a person would say them — do NOT convert to UTC or apply any timezone offset, that's handled separately. If the request doesn't clearly name one of the resources above, or gives no usable time, set clarification instead of guessing."""


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


def parse_booking_request(message: str, resources: list[dict], now_utc: datetime) -> dict:
    """
    Returns the model's raw parsed dict (resource_id, date, start_time,
    end_time, clarification) exactly as it came back — the caller is
    responsible for validating every field before trusting it.
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
                {"role": "system", "content": _system_prompt(resources, now_local)},
                {"role": "user", "content": message},
            ],
        },
        timeout=15,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    return json.loads(content)
