# 1. Postgres exclusion constraint for booking overlaps

## Status

Accepted (2026-08-07).

## Context

The first version of `create_booking()` checked whether a slot was free with a `SELECT`, then `INSERT`ed the new booking as a separate statement, with no lock between the two. A Locust load test firing 30 concurrent users at the same resource/timeslot confirmed this wasn't theoretical: the naive endpoint produced 4 overlapping bookings (6 overlapping pairs), verified directly against Postgres, not by trusting the HTTP responses.

Something has to guarantee that two overlapping bookings for the same resource can never both commit, under real concurrent load.

## Alternatives considered

**`SELECT ... FOR UPDATE` row lock on the resource.** Locks the resource row before the overlap check, so a second transaction has to wait for the first to finish before it can even read. The same load test confirmed this closes the race: 0 overlapping pairs (down from 6), 4,127 of 4,128 racing requests correctly rejected, 420 req/s sustained.

This works, but the lock is scoped to the whole resource row, not the specific timeslot — it serializes *every* booking attempt on that resource, including two requests for completely non-overlapping times. Correctness lives entirely in application code; if any other code path ever inserted a booking without taking this lock, the guarantee silently disappears.

## Decision

Drop the application-side lock entirely. Add a Postgres `EXCLUDE` constraint on `(resource_id, tstzrange(start_time, end_time))` using a GiST index (`db/002_add_exclusion_constraint.sql`, requires the `btree_gist` extension for the equality operator on `resource_id` to coexist with the range overlap operator). The existing `SELECT` becomes an optimistic fast-path only — cheap, saves a round trip in the common non-racing case — not the actual guarantee. If two concurrent requests both pass the `SELECT` and both `INSERT`, Postgres rejects the second insert at commit time; the app catches that as `IntegrityError` and returns 409.

The same load test against this version: still 0 overlapping pairs, 4,447 of 4,448 racing requests correctly rejected, 452-570 req/s — and, unlike the row lock, two non-overlapping bookings on the same resource now commit concurrently instead of queuing behind each other. `tests/test_concurrency.py` automates the same check (20 concurrent identical requests, asserts exactly one wins) so it runs on every CI build, not just as a one-off manual benchmark.

## Consequences

- Correctness no longer depends on every write path remembering to take a lock — it's enforced by the database schema itself, including against any future code path that inserts into `bookings`.
- Higher throughput and better concurrency than the row lock, because only genuine overlaps serialize.
- Postgres-specific: `EXCLUDE USING gist` with `btree_gist` isn't portable to MySQL or SQLite as-is. Acceptable here since the project is Postgres-only end to end (Neon in production).
- `UPDATE`s to a booking's time range hit the same constraint, so editing a booking into an overlap fails the same way a conflicting `INSERT` would — this had to be handled explicitly in the booking-edit path, not assumed for free.

## When this would change

If the project ever needed to support a non-Postgres database, or needed overlap rules more complex than "no two bookings for the same resource at overlapping times" (e.g. partial-capacity rooms, multiple simultaneous bookings up to a seat count), the constraint approach would need to be replaced or supplemented with application-level logic, since a GiST exclusion constraint can't express capacity-aware overlap.
