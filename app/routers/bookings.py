"""
ACTIVE: fix attempt 2 (Postgres EXCLUDE constraint) + idempotency key.

create_booking() no longer locks the resource row app-side. The overlap
SELECT below is now just an optimistic fast-path (cheap, saves a round trip
for the common non-racing case) — it is NOT what prevents duplicate
bookings. The actual guarantee is the `bookings_no_overlap` EXCLUDE
constraint added in db/002_add_exclusion_constraint.sql: if two concurrent
requests both pass the SELECT and both INSERT, Postgres rejects the second
INSERT at commit time, and we catch that as IntegrityError -> 409. Unlike
step 4's FOR UPDATE lock, this only blocks genuinely overlapping bookings —
non-overlapping requests on the same resource can commit concurrently.

If the client sends idempotency_key, a second submission with the same key
(double-click, retry after a dropped response) returns the original booking
instead of erroring or creating a duplicate — enforced by the
bookings_user_idempotency_key_unique constraint from
db/003_add_idempotency_key.sql, same "app check as fast-path, DB constraint
as guarantee" pattern as the overlap check.

Load testing this under N truly-simultaneous duplicate submissions (same
idempotency_key) surfaced two real bugs, in order of how they were found:

1. A single check-then-insert attempt wasn't enough: a losing transaction
   can fail against a DIFFERENT in-flight competing insert than the one
   that ultimately commits, so an immediate one-shot re-check can run
   before the real winner's row is visible yet, returning a spurious 409
   for what should be a clean idempotent replay. Fixed with a bounded retry
   of the whole check-then-insert sequence (_MAX_ATTEMPTS) — a fresh
   attempt gets a fresh snapshot and almost always resolves cleanly.
2. At the same concurrency level, Postgres occasionally detected an actual
   deadlock while checking the GiST exclusion constraint against several
   uncommitted candidate rows at once (multiple transactions each waiting
   on a lock another one holds), aborting one as the deadlock victim —
   psycopg2.errors.DeadlockDetected / SQLAlchemy OperationalError, not
   IntegrityError, so it was slipping through as an unhandled 500. Now
   caught and retried the same way; if attempts are exhausted this way we
   return 503 (ask the client to retry) rather than 409, since a deadlock
   victim's insert may have been perfectly valid — we don't actually know.

Root cause of both: duplicate submissions of the *same* logical request
were racing each other through the database's general-purpose conflict
machinery, which is built to resolve conflicts between genuinely different
writers, not to coalesce N copies of one writer. The actual fix is
pg_advisory_xact_lock, scoped to hash(user_id, idempotency_key): true
duplicates now serialize on a cheap in-memory lock before ever reaching the
constraint, so only one of them inserts and the rest find it via the
existing-row check once unblocked. This only affects requests sharing the
same key — genuinely different bookings for different users/keys are
completely unaffected and still go through the lock-free constraint path
from step 5. The retry loop stays as defense in depth for the no-key /
different-key overlap case, which can still hit the same two failure modes
under heavy concurrency, just far less often since real overlaps (not
self-duplicates) are the much rarer case in practice.

create_booking() also best-effort syncs to Google Calendar if the user has
connected one (see app/google_calendar.py, app/routers/google_calendar.py),
and cancel_booking() reverses that. Both treat the Google API as a
convenience layered on top of the booking guarantee, never allowed to affect
whether a booking succeeds/is cancelled.
"""

from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app import crypto
from app import google_calendar as gcal
from app.auth import get_current_user
from app.booking_lock import resource_lock
from app.database import get_db
from app.models import Booking, Resource, User
from app.schemas import (
    BookingCreate,
    BookingOut,
    BookingUpdate,
    MyBookingOut,
    RecurringBookingCreate,
    RecurringBookingResult,
)

router = APIRouter(prefix="/bookings", tags=["bookings"])

_MAX_ATTEMPTS = 5


@router.get("", response_model=list[BookingOut])
def list_bookings(
    resource_id: int,
    date: date,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    day_start = datetime.combine(date, time.min, tzinfo=UTC)
    day_end = day_start + timedelta(days=1)
    return (
        db.query(Booking)
        .filter(
            Booking.resource_id == resource_id,
            Booking.start_time < day_end,
            Booking.end_time > day_start,
        )
        .order_by(Booking.start_time)
        .all()
    )


@router.get("/me", response_model=list[MyBookingOut])
def list_my_bookings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (
        db.query(Booking, Resource.name)
        .join(Resource, Resource.id == Booking.resource_id)
        .filter(Booking.user_id == current_user.id)
        .order_by(Booking.start_time.desc())
        .all()
    )
    return [
        MyBookingOut(
            id=booking.id,
            resource_id=booking.resource_id,
            resource_name=resource_name,
            start_time=booking.start_time,
            end_time=booking.end_time,
            series_id=booking.series_id,
            created_at=booking.created_at,
        )
        for booking, resource_name in rows
    ]


@router.post("", response_model=BookingOut, status_code=201)
def create_booking(
    payload: BookingCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if payload.end_time <= payload.start_time:
        raise HTTPException(status_code=422, detail="end_time must be after start_time")

    resource = db.query(Resource).filter(Resource.id == payload.resource_id).first()
    if not resource:
        raise HTTPException(status_code=404, detail="Resource not found")

    for attempt in range(_MAX_ATTEMPTS):
        if payload.idempotency_key:
            # Serializes only duplicate submissions of this exact (user, key) —
            # transaction-scoped, released automatically on commit/rollback.
            db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
                {"lock_key": f"{current_user.id}:{payload.idempotency_key}"},
            )
            existing = (
                db.query(Booking)
                .filter(
                    Booking.user_id == current_user.id,
                    Booking.idempotency_key == payload.idempotency_key,
                )
                .first()
            )
            if existing:
                return existing

        try:
            with resource_lock(payload.resource_id):
                overlap = (
                    db.query(Booking)
                    .filter(
                        Booking.resource_id == payload.resource_id,
                        Booking.start_time < payload.end_time,
                        Booking.end_time > payload.start_time,
                    )
                    .first()
                )
                if overlap:
                    raise HTTPException(
                        status_code=409, detail="Resource already booked for this time"
                    )

                booking = Booking(
                    resource_id=payload.resource_id,
                    user_id=current_user.id,
                    start_time=payload.start_time,
                    end_time=payload.end_time,
                    idempotency_key=payload.idempotency_key,
                )
                db.add(booking)
                try:
                    db.commit()
                except IntegrityError:
                    db.rollback()
                    if attempt < _MAX_ATTEMPTS - 1:
                        continue
                    if payload.idempotency_key:
                        existing = (
                            db.query(Booking)
                            .filter(
                                Booking.user_id == current_user.id,
                                Booking.idempotency_key == payload.idempotency_key,
                            )
                            .first()
                        )
                        if existing:
                            return existing
                    raise HTTPException(
                        status_code=409, detail="Resource already booked for this time"
                    ) from None
                except OperationalError:
                    db.rollback()
                    if attempt < _MAX_ATTEMPTS - 1:
                        continue
                    raise HTTPException(
                        status_code=503,
                        detail="High contention booking this resource, please retry",
                    ) from None
                db.refresh(booking)
        except TimeoutError:
            if attempt < _MAX_ATTEMPTS - 1:
                continue
            raise HTTPException(
                status_code=503, detail="High contention booking this resource, please retry"
            ) from None

        if current_user.google_refresh_token:
            # Best-effort: Google Calendar sync is a convenience layered on
            # top of the booking guarantee, not part of it. Any failure here
            # (network, revoked/expired token, quota) must never surface as
            # a failed booking — the booking already committed above.
            try:
                # start_time/end_time are `nullable=False` in the schema;
                # the sqlalchemy mypy plugin still widens legacy
                # Column()-style attributes to Optional regardless.
                event_id = gcal.create_event(
                    crypto.decrypt(current_user.google_refresh_token),
                    summary=f"{resource.name} booking",
                    description="Booked via RoomLock",
                    start=booking.start_time,  # type: ignore[arg-type]
                    end=booking.end_time,  # type: ignore[arg-type]
                )
                booking.google_event_id = event_id
                db.commit()
            except Exception:
                pass

        return booking


@router.post("/recurring", response_model=RecurringBookingResult, status_code=201)
def create_recurring_booking(
    payload: RecurringBookingCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Creates `occurrences` weekly bookings starting at payload.start_time, all
    tagged with one series_id. Each occurrence goes through the exact same
    overlap-check + EXCLUDE-constraint + retry loop as a single POST
    /bookings — recurrence has no special claim on a slot. A slot that's
    already taken (someone else booked that week, or a holiday closure) is
    reported back as `skipped` rather than aborting the whole series: partial
    success is the useful behavior for "book my weekly 5pm slot" — the user
    wants the 11 free weeks, not an all-or-nothing failure because week 6 is
    taken.
    """
    if payload.end_time <= payload.start_time:
        raise HTTPException(status_code=422, detail="end_time must be after start_time")

    resource = db.query(Resource).filter(Resource.id == payload.resource_id).first()
    if not resource:
        raise HTTPException(status_code=404, detail="Resource not found")

    series_id = uuid4()
    created: list[Booking] = []
    skipped: list[dict] = []
    skip_dates = set(payload.skip_dates)

    for i in range(payload.occurrences):
        occ_start = payload.start_time + timedelta(weeks=i)
        occ_end = payload.end_time + timedelta(weeks=i)

        if occ_start.date() in skip_dates:
            skipped.append({"start_time": occ_start, "reason": "Skipped by request"})
            continue

        for attempt in range(_MAX_ATTEMPTS):
            overlap = (
                db.query(Booking)
                .filter(
                    Booking.resource_id == payload.resource_id,
                    Booking.start_time < occ_end,
                    Booking.end_time > occ_start,
                )
                .first()
            )
            if overlap:
                skipped.append({"start_time": occ_start, "reason": "Resource already booked for this time"})
                break

            booking = Booking(
                resource_id=payload.resource_id,
                user_id=current_user.id,
                start_time=occ_start,
                end_time=occ_end,
                # series_id is a plain uuid.UUID; the sqlalchemy mypy plugin
                # expects its own dialect-specific UUID return type here —
                # a stub-generics mismatch, not a real type error.
                series_id=series_id,  # type: ignore[arg-type]
            )
            db.add(booking)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                if attempt < _MAX_ATTEMPTS - 1:
                    continue
                skipped.append({"start_time": occ_start, "reason": "Resource already booked for this time"})
                break
            except OperationalError:
                db.rollback()
                if attempt < _MAX_ATTEMPTS - 1:
                    continue
                skipped.append(
                    {"start_time": occ_start, "reason": "High contention, please retry this week separately"}
                )
                break
            db.refresh(booking)
            created.append(booking)

            if current_user.google_refresh_token:
                try:
                    # See the matching comment above: start_time/end_time are
                    # non-nullable, but the sqlalchemy mypy plugin widens
                    # legacy Column()-style attributes to Optional regardless.
                    event_id = gcal.create_event(
                        crypto.decrypt(current_user.google_refresh_token),
                        summary=f"{resource.name} booking",
                        description="Booked via RoomLock",
                        start=booking.start_time,  # type: ignore[arg-type]
                        end=booking.end_time,  # type: ignore[arg-type]
                    )
                    booking.google_event_id = event_id
                    db.commit()
                except Exception:
                    pass

            break

    # `created` holds ORM Booking rows, not BookingOut schemas — FastAPI's
    # response_model serializes them via from_attributes at runtime, which
    # mypy has no way to verify statically.
    return RecurringBookingResult(
        series_id=series_id, created=created, skipped=skipped  # type: ignore[arg-type]
    )


@router.delete("/series/{series_id}", status_code=204)
def cancel_booking_series(
    series_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    bookings = (
        db.query(Booking)
        .filter(Booking.series_id == series_id, Booking.user_id == current_user.id)
        .all()
    )
    if not bookings:
        raise HTTPException(status_code=404, detail="Booking series not found")

    now = datetime.now(UTC)
    for booking in bookings:
        # end_time is `nullable=False` in the schema; the sqlalchemy mypy
        # plugin still widens legacy Column()-style attributes to Optional
        # regardless.
        if booking.end_time <= now:  # type: ignore[operator]
            continue  # leave past occurrences in history, only cancel future ones
        if booking.google_event_id and current_user.google_refresh_token:
            try:
                gcal.delete_event(crypto.decrypt(current_user.google_refresh_token), booking.google_event_id)
            except Exception:
                pass
        db.delete(booking)
    db.commit()


@router.patch("/{booking_id}", response_model=BookingOut)
def update_booking(
    booking_id: int,
    payload: BookingUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if payload.end_time <= payload.start_time:
        raise HTTPException(status_code=422, detail="end_time must be after start_time")
    if payload.end_time <= datetime.now(UTC):
        raise HTTPException(status_code=400, detail="Can't move a booking to a time in the past")

    booking = db.query(Booking).filter(Booking.id == booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="You can only edit your own bookings")
    if booking.end_time <= datetime.now(UTC):  # type: ignore[operator]
        raise HTTPException(status_code=400, detail="Can't edit a booking that has already passed")

    # Same "optimistic SELECT + EXCLUDE constraint as guarantee" pattern as
    # create_booking — Postgres checks the constraint on UPDATE just like
    # INSERT, so moving a booking into another one's slot fails the same
    # way. Excludes its own row from the fast-path check (it will always
    # "overlap" itself under the old time range otherwise).
    for attempt in range(_MAX_ATTEMPTS):
        overlap = (
            db.query(Booking)
            .filter(
                Booking.id != booking_id,
                Booking.resource_id == booking.resource_id,
                Booking.start_time < payload.end_time,
                Booking.end_time > payload.start_time,
            )
            .first()
        )
        if overlap:
            raise HTTPException(status_code=409, detail="Resource already booked for this time")

        booking.start_time = payload.start_time
        booking.end_time = payload.end_time
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            if attempt < _MAX_ATTEMPTS - 1:
                continue
            raise HTTPException(
                status_code=409, detail="Resource already booked for this time"
            ) from None
        except OperationalError:
            db.rollback()
            if attempt < _MAX_ATTEMPTS - 1:
                continue
            raise HTTPException(
                status_code=503, detail="High contention updating this booking, please retry"
            ) from None
        db.refresh(booking)

        if booking.google_event_id and current_user.google_refresh_token:
            try:
                gcal.update_event(
                    crypto.decrypt(current_user.google_refresh_token),
                    booking.google_event_id,
                    booking.start_time,
                    booking.end_time,
                )
            except Exception:
                pass

        return booking


@router.delete("/{booking_id}", status_code=204)
def cancel_booking(
    booking_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    booking = db.query(Booking).filter(Booking.id == booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="You can only cancel your own bookings")
    if booking.end_time <= datetime.now(UTC):  # type: ignore[operator]
        raise HTTPException(status_code=400, detail="Can't cancel a booking that has already passed")

    if booking.google_event_id and current_user.google_refresh_token:
        # Same best-effort contract as sync-on-create: a Google failure here
        # must not block cancelling the actual booking.
        try:
            gcal.delete_event(crypto.decrypt(current_user.google_refresh_token), booking.google_event_id)
        except Exception:
            pass

    db.delete(booking)
    db.commit()
