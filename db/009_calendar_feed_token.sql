-- Applied manually (not via SQLAlchemy create_all()):
--   docker compose exec db psql -U app -d bookingapi -f /db/009_calendar_feed_token.sql
--
-- Per-user secret token powering the read-only iCal subscription feed
-- (GET /calendar/feed/{token}.ics). Generated lazily on first request,
-- not at registration time, so most users never have one at all.
ALTER TABLE users ADD COLUMN calendar_feed_token TEXT UNIQUE;
