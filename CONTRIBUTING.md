# Contributing

This started as a solo project, but issues and pull requests are welcome.

## Setup

```bash
docker compose up --build
```

Apply the schema (run manually so each migration is explicit — nothing auto-runs on startup):

```bash
for f in db/*.sql; do
  echo "Applying $f..."
  docker compose exec db psql -U app -d bookingapi -f "/db/$(basename "$f")"
done
```

Generate real secrets (the checked-in `.env` placeholders are dev-only):

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"        # JWT_SECRET_KEY
python3 -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"  # TOKEN_ENCRYPTION_KEY
```

## Running the test suite

```bash
docker compose exec api pytest -v
```

Tests run against a real, throwaway Postgres instance in the same container, recreated on each run with every `db/*.sql` migration applied — not SQLite. The whole point of `tests/test_concurrency.py` is to prove the actual `EXCLUDE` constraint and advisory-lock behavior from [ADR 1](docs/adr/0001-postgres-exclusion-constraint-for-booking-overlaps.md) and [ADR 2](docs/adr/0002-advisory-lock-for-idempotency-key-duplicates.md), which a non-Postgres test double can't exercise.

Before opening a PR:

```bash
pip install -r requirements-dev.txt
ruff check .
mypy app
pytest --cov=app --cov-report=term-missing --cov-report=xml
```

CI runs the same three checks against a real Postgres 16 service container on every push and PR — if it's green locally, it should be green there too.

## Making a change to the booking/overlap logic specifically

If you touch `app/routers/bookings.py`'s concurrency handling, read [ADR 1](docs/adr/0001-postgres-exclusion-constraint-for-booking-overlaps.md) and [ADR 2](docs/adr/0002-advisory-lock-for-idempotency-key-duplicates.md) first — they explain why the code is structured the way it is (which lock/constraint is responsible for which failure mode) so a change doesn't silently remove a guarantee the tests aren't specifically checking for. If you change which concurrency primitive handles what, update the relevant ADR or add a new one rather than leaving the old reasoning to go stale.

`scripts/fix_comparison_benchmark.py` and `scripts/scale_benchmark.py` reproduce the load-test numbers in the README — rerun them if you change anything on the booking write path, since a correctness fix that regresses throughput is worth knowing about before it ships.

## Code style

- `ruff` for linting/formatting, `mypy` for type checking — both run in CI, no separate style debate needed.
- Match the existing docstring convention in files like `app/routers/bookings.py`: explain *why* the code is structured this way, especially for anything concurrency-related, not just what it does.

## Opening an issue

A clear repro matters more than a proposed fix. If it's a concurrency bug, a failing test (even a flaky one that fails sometimes under load) is worth more than a description of what you think happens.
