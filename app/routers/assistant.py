"""
Natural-language booking assistant. Routes only — the Groq/LLM call lives
in app/groq_client.py.

The model classifies a message into one of five intents (book,
availability, list_bookings, cancel, chat) and extracts whatever fields
that intent needs. It never acts on anything directly:

- "book" returns a draft (resource_id, start_time, end_time) for the
  frontend to show for confirmation; confirming still goes through the
  normal POST /bookings, with its full overlap check + EXCLUDE constraint
  + retry loop, completely unchanged.
- "availability" and "list_bookings" are answered from a real DB query
  here, not from anything the model claims — the model only has to
  recognize the intent (and, for availability, which resource/date).
- "cancel" returns the one booking_id the model matched against the
  user's real upcoming bookings; the frontend must still call the normal
  DELETE /bookings/{id} to actually cancel it.
- "chat" passes through the model's own short reply, for anything that
  isn't an actionable request.

Every field the model returns is re-checked here against the real
resource list, the real clock, and the real booking table before it's
trusted with anything, exactly like any other untrusted client input. If
the model invents an id, returns an unparseable or past date, or isn't
confident, this returns needs_clarification instead of a draft.
"""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app import groq_client
from app.auth import get_current_user
from app.database import get_db
from app.models import Booking, Resource, User
from app.rate_limit import limiter
from app.schemas import AssistantBookingSummary, AssistantChatRequest, AssistantChatResponse

router = APIRouter(prefix="/assistant", tags=["assistant"])

_GENERIC_CLARIFICATION = "Not sure which room or time you mean — try naming the room and a specific day/time."


def _clarify(message: str) -> AssistantChatResponse:
    return AssistantChatResponse(intent="chat", reply=message, needs_clarification=True)


def _my_upcoming_bookings(db: Session, current_user: User) -> list[dict]:
    rows = (
        db.query(Booking, Resource.name)
        .join(Resource, Resource.id == Booking.resource_id)
        .filter(Booking.user_id == current_user.id, Booking.end_time > datetime.now(UTC))
        .order_by(Booking.start_time)
        .all()
    )
    return [
        {
            "id": booking.id,
            "resource_id": booking.resource_id,
            "resource_name": resource_name,
            "start_time": booking.start_time,
            "end_time": booking.end_time,
        }
        for booking, resource_name in rows
    ]


@router.get("/status")
def status():
    return {"configured": groq_client.is_configured()}


@router.post("/chat", response_model=AssistantChatResponse)
@limiter.limit("10/minute")
def chat(
    request: Request,
    payload: AssistantChatRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not groq_client.is_configured():
        raise HTTPException(status_code=503, detail="The booking assistant isn't configured on this server yet.")

    resources = db.query(Resource).all()
    resource_by_id = {r.id: r for r in resources}
    resource_list = [{"id": r.id, "name": r.name} for r in resources]
    my_bookings = _my_upcoming_bookings(db, current_user)
    my_bookings_by_id = {b["id"]: b for b in my_bookings}

    now = datetime.now(UTC)
    history = [{"role": turn.role, "content": turn.content} for turn in payload.history]
    try:
        parsed = groq_client.parse_assistant_message(payload.message, resource_list, my_bookings, history, now)
    except Exception:
        raise HTTPException(
            status_code=502, detail="Couldn't reach the booking assistant, please try again."
        ) from None

    intent = parsed.get("intent")

    if parsed.get("clarification"):
        return _clarify(parsed["clarification"])

    if intent == "chat":
        return AssistantChatResponse(intent="chat", reply=parsed.get("reply") or _GENERIC_CLARIFICATION)

    if intent == "list_bookings":
        if not my_bookings:
            return AssistantChatResponse(intent="list_bookings", reply="You have no upcoming bookings.", bookings=[])
        return AssistantChatResponse(
            intent="list_bookings",
            reply=f"You have {len(my_bookings)} upcoming booking(s):",
            bookings=[AssistantBookingSummary(**b) for b in my_bookings],
        )

    if intent == "cancel":
        booking = my_bookings_by_id.get(parsed.get("booking_id"))
        if booking is None:
            return _clarify("Which booking did you want to cancel? Say the room and day.")
        return AssistantChatResponse(
            intent="cancel",
            reply=f"Cancel {booking['resource_name']} on {booking['start_time'].strftime('%a %Y-%m-%d %H:%M')}?",
            cancel_booking_id=booking["id"],
        )

    if intent == "availability":
        resource = resource_by_id.get(parsed.get("resource_id"))
        if resource is None:
            return _clarify(_GENERIC_CLARIFICATION)
        date_str = parsed.get("date")
        try:
            day = datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else now.date()
        except ValueError:
            return _clarify(_GENERIC_CLARIFICATION)
        day_start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
        day_end = day_start + timedelta(days=1)
        rows = (
            db.query(Booking, Resource.name)
            .join(Resource, Resource.id == Booking.resource_id)
            .filter(
                Booking.resource_id == resource.id,
                Booking.start_time < day_end,
                Booking.end_time > day_start,
            )
            .order_by(Booking.start_time)
            .all()
        )
        if not rows:
            return AssistantChatResponse(
                intent="availability", reply=f"{resource.name} is free all day on {day.isoformat()}.", bookings=[]
            )
        return AssistantChatResponse(
            intent="availability",
            reply=f"{resource.name} already has {len(rows)} booking(s) on {day.isoformat()}:",
            bookings=[
                AssistantBookingSummary(
                    id=booking.id,
                    resource_id=booking.resource_id,
                    resource_name=resource_name,
                    start_time=booking.start_time,
                    end_time=booking.end_time,
                )
                for booking, resource_name in rows
            ],
        )

    if intent == "book":
        resource = resource_by_id.get(parsed.get("resource_id"))
        if resource is None:
            return _clarify(_GENERIC_CLARIFICATION)

        date_str = parsed.get("date")
        start_str = parsed.get("start_time")
        end_str = parsed.get("end_time")
        if not date_str or not start_str:
            return _clarify(_GENERIC_CLARIFICATION)

        try:
            # Local wall-clock -> UTC conversion happens here, not in the model
            # (see combine_local_datetime's docstring) — the one real timezone
            # bug found during manual testing was exactly this arithmetic done
            # by the LLM itself, so it's deliberately kept out of its hands.
            start_time = groq_client.combine_local_datetime(date_str, start_str)
            # Same reasoning for duration: the model is told "default to 1
            # hour" but isn't reliable at actually computing that end time
            # (observed: it left end_time null on a real response during
            # testing) — so a missing end_time defaults here, deterministically,
            # rather than trusting the model to have done the math.
            end_time = (
                groq_client.combine_local_datetime(date_str, end_str) if end_str else start_time + timedelta(hours=1)
            )
        except (TypeError, ValueError):
            return _clarify(_GENERIC_CLARIFICATION)

        if end_time <= start_time:
            return _clarify(_GENERIC_CLARIFICATION)
        if start_time <= now:
            return _clarify("That time's already passed — try a time in the future.")

        return AssistantChatResponse(
            intent="book",
            reply=f"Book {resource.name} from {start_time.strftime('%a %Y-%m-%d %H:%M')} to "
            f"{end_time.strftime('%H:%M')} UTC?",
            resource_id=resource.id,
            resource_name=resource.name,
            start_time=start_time,
            end_time=end_time,
        )

    return _clarify(_GENERIC_CLARIFICATION)
