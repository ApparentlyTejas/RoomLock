"""
Natural-language booking assistant. Routes only — the Groq/LLM call lives
in app/groq_client.py.

This endpoint NEVER creates a booking. It returns a draft (resource_id,
start_time, end_time) for the frontend to show the user for confirmation;
confirming still goes through the normal POST /bookings, with its full
overlap check + EXCLUDE constraint + retry loop, completely unchanged. The
model only gets to propose — every field it returns is re-validated here
against the real resource list and the real clock before it's trusted with
anything, exactly like any other untrusted client input. If the model
invents a resource_id, returns an unparseable or past date, or just isn't
confident, this returns needs_clarification instead of a draft.
"""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app import groq_client
from app.auth import get_current_user
from app.database import get_db
from app.models import Resource, User
from app.rate_limit import limiter
from app.schemas import AssistantParseRequest, AssistantParseResponse

router = APIRouter(prefix="/assistant", tags=["assistant"])

_GENERIC_CLARIFICATION = "Not sure which room or time you mean — try naming the room and a specific day/time."


@router.get("/status")
def status():
    return {"configured": groq_client.is_configured()}


@router.post("/parse", response_model=AssistantParseResponse)
@limiter.limit("10/minute")
def parse(
    request: Request,
    payload: AssistantParseRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not groq_client.is_configured():
        raise HTTPException(status_code=503, detail="The booking assistant isn't configured on this server yet.")

    resources = db.query(Resource).all()
    if not resources:
        return AssistantParseResponse(needs_clarification=True, clarification="There are no bookable rooms yet.")
    resource_by_id = {r.id: r for r in resources}
    resource_list = [{"id": r.id, "name": r.name} for r in resources]

    now = datetime.now(UTC)
    try:
        parsed = groq_client.parse_booking_request(payload.message, resource_list, now)
    except Exception:
        raise HTTPException(
            status_code=502, detail="Couldn't reach the booking assistant, please try again."
        ) from None

    if parsed.get("clarification"):
        return AssistantParseResponse(needs_clarification=True, clarification=parsed["clarification"])

    resource = resource_by_id.get(parsed.get("resource_id"))
    if resource is None:
        return AssistantParseResponse(needs_clarification=True, clarification=_GENERIC_CLARIFICATION)

    date_str = parsed.get("date")
    start_str = parsed.get("start_time")
    end_str = parsed.get("end_time")
    if not date_str or not start_str:
        return AssistantParseResponse(needs_clarification=True, clarification=_GENERIC_CLARIFICATION)

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
        return AssistantParseResponse(needs_clarification=True, clarification=_GENERIC_CLARIFICATION)

    if end_time <= start_time:
        return AssistantParseResponse(needs_clarification=True, clarification=_GENERIC_CLARIFICATION)
    if start_time <= now:
        return AssistantParseResponse(
            needs_clarification=True, clarification="That time's already passed — try a time in the future."
        )

    return AssistantParseResponse(
        needs_clarification=False,
        resource_id=resource.id,
        resource_name=resource.name,
        start_time=start_time,
        end_time=end_time,
    )
