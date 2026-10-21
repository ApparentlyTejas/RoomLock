# RoomLock

![CI](https://github.com/ApparentlyTejas/RoomLock/actions/workflows/ci.yml/badge.svg)

Concurrency-safe resource booking API, themed for booking study rooms and
seminar spaces around FAU Erlangen-Nürnberg: found a double-booking race
under load, fixed it two different ways (row lock, then a Postgres
exclusion constraint), load-tested both to compare correctness and
throughput, and then kept building — room metadata, recurring bookings,
admin analytics, and a natural-language booking assistant on top of it.
The name is literal: a Postgres exclusion constraint locks a booked slot
so nothing else can land on top of it.

**Independent student project — not an official FAU service, and not
affiliated with or endorsed by the university.** The FAU-blue color
scheme (`#004A9F` / `#04316A`, sourced from FAU's own published design
docs) and sample room names are cosmetic branding for this portfolio
demo, applied on top of a generic booking backend that isn't tied to any
one institution.

**Live demo:** **https://bookingapi-i4cg.onrender.com**
(free-tier hosting — if it's been quiet for a while, the first request can
take 30-50s to wake up. Everything after that is normal speed.)

![Demo: asking the natural-language assistant to book a room, confirming, then toggling dark mode](docs/demo.gif)

## The core story: a real race condition, fixed twice, measured both times

Two requests can both `SELECT` "is this slot free?", both get "yes," and
both `INSERT` — nothing ties the check to the write:

```mermaid
sequenceDiagram
    participant A as Request A
    participant B as Request B
    participant DB as Postgres

    A->>DB: SELECT — is 10:00-11:00 free?
    DB-->>A: yes
    B->>DB: SELECT — is 10:00-11:00 free?
    DB-->>B: yes
    A->>DB: INSERT booking A
    DB-->>A: 201 Created
    B->>DB: INSERT booking B
    DB-->>B: 201 Created
    Note over DB: both committed —<br/>two overlapping bookings, same slot
```

The fix isn't a bigger check — it's removing the gap between checking and
writing entirely, by giving Postgres itself a constraint it enforces at
commit time regardless of what any `SELECT` saw beforehand:

```mermaid
sequenceDiagram
    participant A as Request A
    participant B as Request B
    participant DB as Postgres (EXCLUDE constraint)

    A->>DB: SELECT — fast-path check (optimistic, not the guarantee)
    DB-->>A: yes, looks free
    B->>DB: SELECT — fast-path check
    DB-->>B: yes, looks free
    A->>DB: INSERT booking A
    DB-->>A: 201 Created
    B->>DB: INSERT booking B
    DB--xB: rejected — violates bookings_no_overlap
    Note over B: caught as IntegrityError -> 409
```

Racing 30 concurrent users at the exact same slot for 10 seconds, against
each version:

| Version | Requests | Succeeded | Rejected (409) | Overlapping bookings in DB | Throughput |
|---|---|---|---|---|---|
| Naive check-then-insert | — | multiple | — | **4 bookings, 6 overlapping pairs** | — |
| Fix 1: `SELECT ... FOR UPDATE` row lock | 4,128 | 1 | 4,127 | 0 | 420 req/s |
| Fix 2: Postgres `EXCLUDE` constraint | 4,448 | 1 | 4,447 | 0 | 452-570 req/s |

Fix 1 works, but the lock is on the *resource row*, not the *timeslot* — it
serializes every booking attempt on that resource, including ones that
don't overlap at all. Fix 2 (`db/002_add_exclusion_constraint.sql`) lets
Postgres reject only genuinely overlapping rows at commit time via
`EXCLUDE USING gist (resource_id WITH =, tstzrange(start_time, end_time)
WITH &&)`, so non-overlapping bookings on the same resource commit
concurrently — same correctness, higher throughput, no app-level lock.
`tests/test_concurrency.py` automates the same check (20 concurrent
identical requests, asserts exactly one wins) so it's proven on every test
run, not just the one manual load test.

## Proving it at scale, not just in a toy test

Everything above holds at 20-30 concurrent requests against one process.
The natural follow-up question is whether it still holds under real
concurrency, across multiple app instances, and when things fail
mid-request — so `docker-compose.scale-demo.yml` stands up a second,
throwaway stack (3 independent app containers behind nginx, sharing one
Postgres) specifically to answer that with real numbers instead of
assertions.

```mermaid
graph LR
    LG[Load generator] --> NG[nginx<br/>round-robin, no session affinity]
    NG --> A1[api1]
    NG --> A2[api2]
    NG --> A3[api3]
    A1 --> PG[(Postgres<br/>bookings_no_overlap<br/>EXCLUDE constraint)]
    A2 --> PG
    A3 --> PG
```

**Same-slot correctness under maximum contention** — every request racing
the exact same 1-hour slot, spread across all 3 replicas:

| Concurrent requests | Succeeded | Rejected (409) | Overlapping bookings | Wall time | Errors |
|---|---|---|---|---|---|
| 300 | 1 | 299 | 0 | 1.8s | 0 |
| 1,000 | 1 | 999 | 0 | 1.8s | 0 |
| 2,000 | 1 | 1,999 | 0 | 3.0s | 0 |
| **10,000** | **1** | **9,999** | **0** | 16.1s | 0 |

Requests split ~evenly across replicas every run (e.g. 3,334 / 3,335 /
3,331 at 10k) — confirmed via an `X-Replica-Id` response header, not
assumed. Zero overlaps was verified by direct SQL query after every run,
the same verification query from `db/001_schema.sql`, not just by trusting
the HTTP status codes.

**Realistic mixed load** — this is the scenario that answers "what's the
success rate and latency," which the same-slot test structurally can't:
racing one slot means ~100% of requests *should* fail, so success rate
there is meaningless. 10,000 requests instead target 9,800 distinct
resource/time slots (should all succeed) plus 200 requests deliberately
aimed at an already-taken slot (should all correctly fail), mixed and
shuffled:

| | Count | % |
|---|---|---|
| Succeeded (201) | 9,800 | 98.00% |
| Correctly rejected (409) | 200 | 2.00% |
| Unexpected errors | **0** | **0.00%** |

Every single non-success was one of the 200 deliberately-conflicting
requests — the *unexpected* failure rate, which is the number that
actually matters, is 0%. Sustained **321 req/s** across the 3-replica
stack. Client-observed latency (round trip including network + load
generator overhead — see the caveat below): **p50 55.8ms, p95 320ms, p99
2,120ms, max 3,871ms**. The p99/max gap points at real queueing under a
300-concurrent sustained burst, not a fluke — see "what actually broke"
below.

**Failure injection** (`tests/test_failure_injection.py`) — proving the
retry/idempotency machinery survives the failures it was built for, not
just the happy path: a transient deadlock recovers and still returns 201;
exhausted retries return 503 (not a 500, not a hang) and leave zero
partial rows; an idempotent resubmission still returns the *same* booking
after retrying through a simulated deadlock; a hard-killed connection
mid-transaction (simulating a crashed process) leaves no partial row,
proven by Postgres's own atomicity guarantee, independent of any app code.

**What actually broke building this** — reported because a load test that
succeeds on the first try either got lucky or wasn't pushed hard enough.
Every one of these was a real failure this project hit, not a
hypothetical:

| Symptom | Root cause | Fix |
|---|---|---|
| Connections reset under load | nginx defaults to HTTP/1.0 with no keepalive to the upstream — a fresh TCP connection per request | `proxy_http_version 1.1` + `proxy_set_header Connection ""` + upstream `keepalive` |
| Client-side timeouts waiting for a response | SQLAlchemy's unconfigured default pool (5 + 10 overflow = 15 connections/process) undersized for concurrent load | Explicit, documented `pool_size`/`max_overflow`, sized against Postgres's `max_connections` |
| `"no live upstreams"` — nginx briefly refused everything | nginx's default passive health check (`max_fails=1`) pulled all 3 replicas out of rotation after one transient blip each | `max_fails=5 fail_timeout=3s` — tolerate a few failures before pulling a replica |
| 62% of a realistic-load run came back `502`/`500` | FastAPI dispatches sync `def` routes through a bounded thread pool (~40 threads) per process — one process per container wasn't enough concurrency | `WEB_CONCURRENCY` (uvicorn `--workers`), tunable per-deployment via env var so the real single-replica deployment isn't affected |

None of these were Postgres or the booking logic failing — every one was
infrastructure capacity/configuration between the load generator and the
database, which is exactly what you'd want a load test to actually find.

**Observability**: `/metrics` (Prometheus format, via
`prometheus-fastapi-instrumentator`) and structured JSON access logs on
every request (`app/observability.py`) — not wired up to a hosted
dashboard nobody would watch, but real enough that the numbers above could
have been computed from it. (They weren't, in the end — see the
methodology note below.)

**Methodology, honestly**: everything here — nginx, 3 app containers,
Postgres, and the load generator itself — ran on one laptop, sharing the
same CPU cores. That's a real constraint on the *latency* numbers (a
properly isolated benchmark, load generator on separate hardware from the
system under test, would show lower and less noisy tail latency) but not
on the *correctness* numbers (0 overlapping bookings doesn't get better or
worse based on what else the CPU was doing). Latency was measured
client-side rather than scraped from `/metrics`, because with multiple
worker processes across multiple replicas, a single `/metrics` scrape only
sees whichever one process happened to serve that request — real
multi-process/multi-instance Prometheus setups solve this by scraping
every instance separately and aggregating at query time, which is more
infrastructure than a load-test footnote justifies here.

Reproduce it yourself: `./scripts/run_scale_benchmark.sh` brings up
`docker-compose.scale-demo.yml`, applies migrations, runs every table
above via `scripts/scale_benchmark.py` (same-slot race at each concurrency
level, the mixed-load run, replica distribution, and the overlap
verification query — all driven by one script instead of hand-run and
transcribed), writes the results to `benchmark_results/`, and tears the
stack down again. `docker-compose.scale-demo.yml`'s header comment still
has the manual step-by-step if you'd rather drive it yourself.

## Everything else, in the order it was built

- **Auth**: JWT (python-jose) + bcrypt password hashing, plus optional
  "Sign in with Google" / calendar sync.
- **Idempotency keys**: resubmitting a booking with the same key returns
  the original instead of erroring or duplicating — `pg_advisory_xact_lock`
  scoped to `(user_id, idempotency_key)` handles true duplicate submissions
  racing each other, separately from the overlap-check machinery above.
- **Role-based admin**: `is_admin` gate on room management and user roles,
  bootstrapped via direct SQL (no self-promotion endpoint, by design).
- **Automated tests**: pytest against a real throwaway Postgres database
  (not SQLite — the whole point is Postgres-specific guarantees a mock
  can't exercise), including the concurrency test above.
- **Booking editing**: change the time on an existing booking; Postgres
  checks the exclusion constraint on `UPDATE` the same way it does on
  `INSERT`.
- **Room metadata**: capacity, amenities.
- **CSV bulk-import**: admins add many rooms at once instead of one-by-one.
- **Admin analytics**: usage stats, busiest rooms/hours.
- **Recurring bookings**: weekly series, partial success if some weeks are
  taken, cancel-one or cancel-the-series.
- **Dark mode + installable PWA**: persists across pages, offline-capable
  app shell.
- **Natural-language booking assistant**: "book Meeting Room A tomorrow at
  3pm" gets parsed by an LLM (GPT OSS 120B via Groq's free tier) into a
  structured draft — but the model never books anything directly. Every
  field is re-validated against the real resource list and clock, and the
  draft still goes through the exact same `POST /bookings` overlap-check +
  exclusion-constraint path as any other request. Two real bugs were found
  building this: the model got DST-aware UTC conversion wrong by an hour,
  and separately left a default duration unset — both fixed by doing that
  arithmetic in code instead of trusting the model with it (see
  `app/groq_client.py`).
- **iCal subscription feed**: a per-user secret-token URL any calendar app
  can subscribe to once (`GET /calendar/feed/{token}.ics`) and it stays
  live from then on — no OAuth, unlike the Google Calendar integration, so
  it works regardless of calendar provider.
- **Observability**: `/metrics` (Prometheus format) and structured JSON
  access logs on every request — see "Proving it at scale" above for what
  they were actually used for.
- **Failure injection tests**: deliberately-triggered deadlocks, exhausted
  retries, and a hard-killed mid-transaction connection, proving the
  resilience code recovers correctly instead of only ever exercising the
  happy path — see "Proving it at scale" above.
- **Horizontal scaling, proven not asserted**: a 3-replica stack
  (`docker-compose.scale-demo.yml`) behind nginx, re-running the
  same-slot concurrency test across independent app processes — see
  "Proving it at scale" above.
- **Room capacity search**: filter the resource picker by minimum
  capacity ("need seats for at least...") so booking a large lecture hall
  doesn't mean guessing which room name is big enough.
- **Password strength requirements**: 8+ characters, upper/lowercase, a
  number, with a live checklist that ticks off each rule as you type
  rather than only telling you what's wrong after you submit.
- **Small interaction polish**: Enter submits login/register/the
  assistant/admin forms instead of requiring a mouse click on the button,
  and the email field autofocuses when the login screen appears.

## Setup

```bash
docker compose up --build
```

Then apply the schema manually (not auto-run via `Base.metadata.create_all()`):

```bash
for f in db/*.sql; do
  echo "Applying $f..."
  docker compose exec db psql -U app -d bookingapi -f "/db/$(basename "$f")"
done
```

Before first run, generate real secrets for `.env` (the checked-in placeholders are dev-only and must not be used anywhere reachable by anyone else):

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"        # -> JWT_SECRET_KEY
python3 -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"  # -> TOKEN_ENCRYPTION_KEY
```

Check it's up:

```bash
curl http://localhost:8000/health
open http://localhost:8000/docs
```

Note: Postgres is exposed on host port **5434** (not 5432), since a native
Postgres was already using 5432 (and 5433) on this machine. `docker compose
exec` commands above go straight into the container and are unaffected.

## Calendar UI

A calendar app is served at `http://localhost:8000/ui/` (`/ui/admin.html`
for admins) — register/log in, pick a resource and date, and click an
hourly slot to book it, or just type what you want into the "Or just
describe it" box. A "Resend last booking" button re-submits the same
request (same idempotency key) to demonstrate that a retry returns the
original booking instead of erroring or double-booking.

## Auth flow

```bash
# Register
curl -X POST http://localhost:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "test@example.com", "password": "test-password-123"}'

# Login (OAuth2 password flow — form-encoded, not JSON)
curl -X POST http://localhost:8000/auth/login \
  -d "username=test@example.com&password=test-password-123"
# -> {"access_token": "...", "token_type": "bearer"}
```

## Admin access

Creating/deleting rooms (`POST`/`DELETE /resources`) and promoting other
users requires `is_admin`, which defaults to `false` for everyone — nobody
is an admin until someone makes them one. Bootstrap the first admin
directly in the database (there's no other way in, by design — an API
endpoint that could self-promote would defeat the point):

```bash
docker compose exec db psql -U app -d bookingapi \
  -c "UPDATE users SET is_admin = true WHERE email = 'you@example.com';"
```

From there, that account can promote/demote anyone else, manage rooms
(including CSV bulk-import), and view usage analytics from
`/ui/admin.html` (linked in the header once logged in as an admin) — no
more manual SQL needed. The API refuses to demote the last remaining admin.

`docs/fau-sample-rooms.csv` has a few sample rooms named after real FAU
Erlangen campus locations (Kollegienhaus, the University Library, Roter
Platz, Südmensa) — import it via the admin page's "Import CSV" to populate
the demo with something more concrete than "Meeting Room A."

## Create a resource and a booking

```bash
TOKEN="<access_token from an admin account>"

curl -X POST http://localhost:8000/resources \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "Meeting Room A", "capacity": 8, "amenities": ["Projector", "Whiteboard"]}'
# -> {"id": 1, ...}

TOKEN="<access_token from any account>"
curl -X POST http://localhost:8000/bookings \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"resource_id": 1, "start_time": "2026-09-01T10:00:00Z", "end_time": "2026-09-01T11:00:00Z", "idempotency_key": "<client-generated-uuid>"}'
# idempotency_key is optional. Resubmitting the same key returns the
# original booking instead of erroring or creating a duplicate.

curl "http://localhost:8000/bookings?resource_id=1&date=2026-09-01" \
  -H "Authorization: Bearer $TOKEN"
```

Or skip the manual JSON entirely:

```bash
curl -X POST http://localhost:8000/assistant/parse \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "book Meeting Room A next Tuesday at 2pm"}'
# -> a draft {resource_id, start_time, end_time} for the frontend to
#    confirm before it hits POST /bookings above
```

## Recurring bookings

```bash
curl -X POST http://localhost:8000/bookings/recurring \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"resource_id": 1, "start_time": "2026-09-01T10:00:00Z", "end_time": "2026-09-01T11:00:00Z", "occurrences": 6}'
# books 6 weekly occurrences starting from start_time; a slot that's
# already taken in a given week is reported back as "skipped" rather than
# aborting the whole series

curl -X DELETE "http://localhost:8000/bookings/series/<series_id>" \
  -H "Authorization: Bearer $TOKEN"
# cancels this and all future occurrences in the series
```

## Running tests

```bash
docker compose exec api pytest -v
```

Runs against a throwaway `bookingapi_test` database in the same Postgres
container (dropped and recreated each run, with all `db/*.sql` migrations
applied) — not SQLite, since the whole point of `test_concurrency.py` is to
prove the actual `EXCLUDE` constraint and advisory-lock behavior work, which
a non-Postgres test double couldn't exercise. That file races 20 concurrent
identical requests at the same slot and asserts exactly one wins, zero
overlaps land in the database — the automated version of the manual Locust
load test below. 158 tests total, covering auth, resources, bookings,
booking edits, recurring bookings, admin, the booking assistant (with the
Groq call mocked so tests don't depend on network/API cost), Google
Calendar OAuth/sync (with Google's API mocked), and encryption at rest.

### Lint, type-check, coverage (CI)

`.github/workflows/ci.yml` runs this on every push/PR: `ruff check .`,
`mypy app`, and the pytest suite above with coverage (97%) against a real
Postgres 16 service container — same image as `docker-compose.yml`, with
`tests/conftest.py` creating/migrating/dropping `bookingapi_test` itself.
Coverage (XML + HTML) is uploaded as a workflow artifact. Locally, with a
Postgres reachable via `DATABASE_URL`:

```bash
pip install -r requirements-dev.txt
ruff check .
mypy app
pytest --cov=app --cov-report=term-missing --cov-report=xml
```

## Load test (reproducing the race condition)

The top table's three rows (naive check-then-insert, Fix 1: row lock,
Fix 2: exclusion constraint) come from three different points in this
repo's history — `app/routers/bookings.py` only has the current, active
fix in the working tree. `scripts/fix_comparison_benchmark.py` reproduces
all three numbers in one run: it checks out each commit
(`c94703f`/`7675679`/`HEAD`) into its own disposable `git worktree`,
builds and runs it in isolation, races 30 concurrent requests at the same
slot with Locust, and verifies overlapping bookings directly against
Postgres — then tears the worktree down before moving to the next one.

```bash
pip install locust  # not a project dependency — only this manual benchmark needs it
python3 scripts/fix_comparison_benchmark.py
```

To run a single version by hand instead (e.g. against whatever's
currently checked out):

```bash
TEST_EMAIL=test@example.com TEST_PASSWORD=test-password-123 RESOURCE_ID=1 \
  locust -f loadtest/locustfile.py --headless -u 30 -r 30 -t 10s \
  --host http://localhost:8000
```

Then check for overlapping bookings — the verification query lives at the
bottom of `db/001_schema.sql`:

```bash
docker compose exec db psql -U app -d bookingapi
```

<details>
<summary><strong>Google Calendar sync</strong> (optional — click to expand setup)</summary>

When connected, every booking is silently synced to the user's Google
Calendar (and un-synced on cancel), so Google's own reminder system
handles notifying them — no email infra of our own. It's entirely
best-effort: any Google API failure is caught and logged nowhere, on
purpose, since it must never affect whether a booking succeeds/cancels.

Two ways to connect, same underlying OAuth setup:
- **"Sign in with Google"** on the login screen — for a new visitor, this
  authenticates them (creating an account if their Google email isn't
  registered yet, password field left empty) *and* connects calendar sync
  in one consent screen.
- **"Connect Google Calendar"** in the app sidebar — for a user who's
  already logged in (password or Google) and just wants to add calendar
  sync without changing how they log in.

To enable either:

1. In [Google Cloud Console](https://console.cloud.google.com/), create a
   project (or use an existing one).
2. **APIs & Services > Library** — enable the **Google Calendar API**.
3. **APIs & Services > OAuth consent screen** — choose **External**,
   fill in the required fields (app name, your email). While in "Testing"
   mode you'll need to add your own Google account under **Test users** —
   only those accounts can complete the consent flow until the app is
   published/verified, which isn't needed for personal/small-group use.
4. **APIs & Services > Credentials > Create Credentials > OAuth client ID**
   — type **Web application**. Add an authorized redirect URI matching
   `GOOGLE_REDIRECT_URI` below exactly (`http://localhost:8000/auth/google/callback`
   for local dev; update this — and re-add it here — when you deploy).
5. Copy the generated **Client ID** and **Client secret** into `.env`:
   ```bash
   GOOGLE_CLIENT_ID=...
   GOOGLE_CLIENT_SECRET=...
   ```
6. Restart the API (`docker compose up --build -d api`) — both "Sign in
   with Google" and "Connect Google Calendar" work from there.

Leaving `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` blank disables the
integration cleanly — `/auth/google/login` and `/auth/google/connect` both
return 503, surfaced in the UI as a toast rather than a broken redirect.

</details>

<details>
<summary><strong>Natural-language booking assistant</strong> (optional — click to expand setup)</summary>

Free tier, no credit card required:

1. Go to [console.groq.com/keys](https://console.groq.com/keys), sign in,
   create an API key.
2. Add it to `.env`:
   ```bash
   GROQ_API_KEY=...
   ```
3. Restart the API. The "Or just describe it" box appears on the booking
   page automatically once a key is configured (`GET /assistant/status`
   drives that check) — leaving it blank disables the feature cleanly,
   same pattern as the Google integration above.

The model (`openai/gpt-oss-120b`, open-weight, served by Groq — originally
Llama 3.3 70B until Groq decommissioned it) is only ever asked to extract
*local wall-clock* date/time and match a resource name from free text.
Timezone conversion and default-duration math are done in Python
(`app/groq_client.py`, `app/routers/assistant.py`), not trusted to the
model — see the comments there for the two real bugs that motivated that
split. Every field is re-validated server-side before it's ever shown to
the user as a draft, and the draft still has to go through the same
`POST /bookings` guarantee as any other booking:

```mermaid
flowchart LR
    A["'book Meeting Room A<br/>tomorrow at 3pm'"] --> B[Groq LLM:<br/>extract room + local date/time]
    B --> C{Server-side validation:<br/>real resource id?<br/>valid date/time?<br/>end after start?<br/>not in the past?}
    C -- invalid or unsure --> D[needs_clarification<br/>— nothing booked]
    C -- valid --> E[Draft shown to user]
    E -- user confirms --> F["POST /bookings<br/>— same overlap-check + EXCLUDE<br/>constraint as any other request"]
```

The model never has a path to write a booking directly — confirming a
draft is just a normal `POST /bookings` call, indistinguishable from one
typed in by hand.

</details>

## Deployment

Live at **https://bookingapi-i4cg.onrender.com** — free tier throughout:
- **App**: [Render](https://render.com), Docker web service built straight
  from this repo's `Dockerfile`.
- **Database**: [Neon](https://neon.tech), serverless Postgres (`btree_gist`
  extension enabled for the exclusion constraint).

```mermaid
graph LR
    User[Browser] -->|HTTPS| Render[Render<br/>FastAPI in Docker]
    Render -->|SQL, incl. the<br/>EXCLUDE constraint| Neon[(Neon<br/>Postgres)]
    Render -.optional.-> Groq[Groq API<br/>booking assistant]
    Render -.optional.-> Google[Google Calendar API<br/>event sync]
```

Free-tier tradeoff: Render's free web service sleeps after 15 minutes
idle, so the first request after a quiet period takes 30-50s to wake up —
fine for sharing with a small group, not for anything latency-sensitive.

## Security notes

- **Rate limited**: `/auth/login` (10/min) and `/auth/register` (5/min) per
  IP, via `slowapi` — see `app/rate_limit.py`. The booking assistant is
  separately rate-limited (10/min) since each call costs a real API request.
- **Passwords**: bcrypt-hashed (never stored/logged in plaintext), minimum
  8 characters enforced at the schema level (`app/schemas.py`).
- **Google refresh tokens**: encrypted at rest (Fernet, `app/crypto.py`) —
  a database leak alone doesn't hand out live Calendar access. Requires
  `TOKEN_ENCRYPTION_KEY` to be set; if it changes, previously-connected
  users' tokens become undecryptable and they'll need to reconnect.
- **JWT secret**: must be a real random value in any environment other
  users can reach.
- **LLM output is never trusted directly**: every field the booking
  assistant returns (resource id, date, time) is re-validated against the
  real database and clock before being shown to the user, and the actual
  booking write is unreachable from the model — it can only produce a
  draft the user must explicitly confirm through the normal endpoint.
- Known gaps, if this goes further: no email verification on registration
  (anyone can register with any email address without proving they own it),
  and auth tokens live in `localStorage` rather than an httpOnly cookie
  (standard XSS-vs-CSRF tradeoff for a Bearer-token API; fine as long as
  there's no way to inject arbitrary script, which the app avoids by never
  rendering user-supplied strings via `innerHTML`).
