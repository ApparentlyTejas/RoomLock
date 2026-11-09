-- Applied manually (not via SQLAlchemy create_all()):
--   docker compose exec api alembic upgrade head
--
-- Closes the same gap for POST /bookings/recurring that
-- 003_add_idempotency_key.sql closed for POST /bookings: a double-click or
-- network retry on "create series" previously had no way to be recognized
-- as a replay, so it could create a second, fully duplicate series.
--
-- Unlike the per-booking idempotency_key (set on every row), this key is
-- stamped on only the first occurrence a series produces — the unique
-- constraint is what lets a replay find that row again and return the
-- existing series instead of creating a new one, the same "app check as
-- fast-path, DB constraint as guarantee" pattern as everywhere else in this
-- file. NULL is allowed and not enforced for uniqueness (Postgres treats
-- NULLs as distinct), so a series created without a key is unaffected.

ALTER TABLE bookings ADD COLUMN series_idempotency_key TEXT;

ALTER TABLE bookings
    ADD CONSTRAINT bookings_user_series_idempotency_key_unique
    UNIQUE (user_id, series_idempotency_key);
