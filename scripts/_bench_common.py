"""Shared helpers for scripts/scale_benchmark.py and
scripts/fix_comparison_benchmark.py."""

import psycopg2


def percentile(values, pct):
    if not values:
        return 0.0
    values = sorted(values)
    k = (len(values) - 1) * (pct / 100)
    f, c = int(k), min(int(k) + 1, len(values) - 1)
    if f == c:
        return values[f]
    return values[f] + (values[c] - values[f]) * (k - f)


def count_overlaps(db_url: str) -> int:
    """
    Same query as the comment at the bottom of db/001_schema.sql: counts
    pairs of bookings on the same resource whose time ranges overlap.
    Works against every commit benchmarked here since `bookings
    (resource_id, start_time, end_time)` has been stable since the schema
    was first introduced.
    """
    conn = psycopg2.connect(db_url)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT count(*)
        FROM bookings a
        JOIN bookings b ON a.resource_id = b.resource_id AND a.id < b.id
        WHERE a.start_time < b.end_time AND b.start_time < a.end_time
        """
    )
    n = cur.fetchone()[0]
    cur.close()
    conn.close()
    return n
