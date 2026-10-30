"""
Pluggable concurrency-control strategy for create_booking(), selected by
settings.booking_lock_strategy:

- "exclusion_constraint" (default, what main has always used): this is a
  no-op. The overlap SELECT in create_booking() is just an optimistic fast
  path; the actual guarantee is the Postgres EXCLUDE constraint (see
  db/002_add_exclusion_constraint.sql), enforced at commit time regardless
  of this setting.
- "redis": also acquires a short-lived Redis lock scoped to resource_id
  around the check-then-insert section, serializing concurrent attempts on
  the same resource through an external lock service instead of leaning on
  Postgres alone — the real-world alternative this project's README
  benchmarks it against. The EXCLUDE constraint still backstops this path
  the same as any other strategy; a bug here would surface as an
  unnecessary 409, never an actual overlapping booking.
"""

from collections.abc import Iterator
from contextlib import contextmanager

import redis

from app.config import settings

_redis_client: redis.Redis | None = None


def _get_client() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


@contextmanager
def resource_lock(resource_id: int) -> Iterator[None]:
    if settings.booking_lock_strategy != "redis":
        yield
        return

    lock = _get_client().lock(
        f"roomlock:resource:{resource_id}",
        timeout=10,  # auto-expires if a holder crashes mid-request, instead of jamming the resource forever
        blocking_timeout=5,  # give up rather than hang forever under extreme contention
    )
    if not lock.acquire():
        raise TimeoutError(f"Could not acquire booking lock for resource {resource_id}")
    try:
        yield
    finally:
        try:
            lock.release()
        except redis.exceptions.LockNotOwnedError:
            pass  # lock already expired (timeout) and may have been taken by someone else
