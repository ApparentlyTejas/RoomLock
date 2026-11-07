# 2. Advisory lock for idempotency-key duplicate submissions

## Status

Accepted (2026-08-08).

## Context

Clients can send an `idempotency_key` with a booking request so that a retried or double-clicked submission returns the original booking instead of erroring or creating a duplicate. This was first implemented the same way as the overlap check (ADR 1): an app-side `SELECT` for an existing row with that key as an optimistic fast-path, backed by a `bookings_user_idempotency_key_unique` constraint (`db/003_add_idempotency_key.sql`) as the real guarantee.

Load-testing this under N truly-simultaneous duplicate submissions of the *same* idempotency key (not different, genuinely-conflicting bookings — the exact same logical request sent more than once) surfaced two real bugs, in the order they were found:

1. A single check-then-insert attempt wasn't enough: a losing transaction can fail against a *different* in-flight competing insert than the one that ultimately commits, so an immediate one-shot re-check can run before the real winner's row is visible yet — returning a spurious 409 for what should have been a clean idempotent replay.
2. At the same concurrency level, Postgres occasionally detected an actual deadlock while checking the GiST exclusion constraint against several uncommitted candidate rows at once, aborting one transaction as the deadlock victim. That surfaces as `psycopg2.errors.DeadlockDetected` / SQLAlchemy `OperationalError`, not `IntegrityError` — so it was slipping through as an unhandled 500.

## Alternatives considered

**Bounded retry of the check-then-insert sequence alone.** Fixes bug 1 (a fresh attempt gets a fresh snapshot and almost always resolves cleanly) and, combined with catching the deadlock error, also papers over bug 2. This was tried first and does work. But it treats a structural problem — many copies of the *same* writer being forced through conflict-resolution machinery designed for genuinely *different* writers — as a transient-error-retry problem instead of removing the actual contention.

## Decision

Add `pg_advisory_xact_lock`, scoped to `hash(user_id, idempotency_key)`, taken before the check-then-insert sequence runs. True duplicate submissions (same user, same key) now serialize on a cheap in-memory lock before ever reaching the exclusion constraint, so only one of them inserts; the rest find the existing row via the fast-path check once unblocked. This only affects requests that share the same key — genuinely different bookings for different users or keys are completely unaffected and still go through the lock-free constraint path from ADR 1.

The bounded retry loop stays in place as defense in depth for the no-key / different-key overlap case, which can still hit the same two failure modes under heavy concurrency — just far less often, since real overlaps (not self-duplicates) are the much rarer case in practice. If retries are exhausted there, the response is 503 (ask the client to retry) rather than 409, since a deadlock victim's insert may have been perfectly valid; the server genuinely doesn't know.

## Consequences

- Root cause (duplicate-of-self contention) is fixed at its source for the common case, not just masked by retrying around it.
- The retry loop is still needed, not dead code — it covers a real, distinct failure mode (genuine overlaps under heavy concurrency) that the advisory lock doesn't touch.
- Two different concurrency primitives now coexist for two different problems: an exclusion constraint for "is this slot free," an advisory lock for "is this the same request twice." Documenting which one is responsible for what is necessary to keep future changes from assuming either one subsumes the other.

## When this would change

If idempotency semantics ever needed to span multiple app instances disagreeing about `user_id`/key hashing (e.g. a sharded user ID space with collisions), the `hashtext()`-based lock key would need revisiting — collisions there would only cause extra, harmless serialization, never incorrect behavior, so this is a low-risk assumption but worth re-checking if the user ID scheme ever changes.
