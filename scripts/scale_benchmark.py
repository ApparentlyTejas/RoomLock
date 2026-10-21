#!/usr/bin/env python3
"""
Regenerates the "Proving it at scale" numbers in README.md against the
docker-compose.scale-demo.yml stack, instead of them being hand-run once
and transcribed. Run scripts/run_scale_benchmark.sh for the one-command
version (brings the stack up, applies migrations, runs this, tears it
down); run this script directly if the stack is already up.

Needs the stack reachable at --host (default http://localhost:8001, nginx
in front of the 3 app replicas) and its Postgres reachable at --db-url
(default matches docker-compose.scale-demo.yml's exposed host port 5435) —
used only to bootstrap the first admin and to run the overlap-verification
query, the same one at the bottom of db/001_schema.sql, directly against
the database rather than trusting HTTP status codes alone.

    python3 scripts/scale_benchmark.py --out benchmark_results/scale_demo.md
"""

import argparse
import asyncio
import json
import os
import random
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

import httpx
import psycopg2

from _bench_common import count_overlaps, percentile

DEFAULT_HOST = "http://localhost:8001"
DEFAULT_DB_URL = "postgresql://app:app_dev_password@localhost:5435/bookingapi"
DEFAULT_LEVELS = [300, 1000, 2000, 10000]
TEST_EMAIL = "scale-bench@example.com"
TEST_PASSWORD = "scale-bench-password-123"
MIXED_LOAD_TOTAL = 10000
MIXED_LOAD_CONFLICTS = 200


def make_admin(db_url: str, email: str) -> None:
    conn = psycopg2.connect(db_url)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("UPDATE users SET is_admin = true WHERE email = %s", (email,))
    cur.close()
    conn.close()


async def ensure_test_user(client: httpx.AsyncClient) -> str:
    resp = await client.post(
        "/auth/register", json={"email": TEST_EMAIL, "password": TEST_PASSWORD}
    )
    if resp.status_code not in (201, 400):
        resp.raise_for_status()
    resp = await client.post(
        "/auth/login", data={"username": TEST_EMAIL, "password": TEST_PASSWORD}
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


async def ensure_resources(client: httpx.AsyncClient, token: str, count: int) -> list:
    headers = {"Authorization": f"Bearer {token}"}
    existing = (await client.get("/resources", headers=headers)).json()
    by_name = {r["name"]: r["id"] for r in existing}
    ids = []
    for i in range(count):
        name = f"scale-bench-room-{i}"
        if name in by_name:
            ids.append(by_name[name])
            continue
        resp = await client.post(
            "/resources",
            json={"name": name, "capacity": 10, "amenities": []},
            headers=headers,
        )
        resp.raise_for_status()
        ids.append(resp.json()["id"])
    return ids


async def race_same_slot(host, token, resource_id, concurrency, run_index) -> dict:
    # Each concurrency level and each benchmark run gets its own slot, far
    # in the future, so repeat runs never collide with bookings a previous
    # run left behind.
    start = datetime(2030, 1, 1, tzinfo=timezone.utc) + timedelta(
        hours=run_index * 20 + concurrency % 20
    )
    end = start + timedelta(hours=1)
    headers = {"Authorization": f"Bearer {token}"}
    body = {
        "resource_id": resource_id,
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
    }

    async with httpx.AsyncClient(base_url=host, timeout=60.0) as client:
        t0 = time.perf_counter()
        results = await asyncio.gather(
            *[
                client.post(
                    "/bookings",
                    json={**body, "idempotency_key": str(uuid.uuid4())},
                    headers=headers,
                )
                for _ in range(concurrency)
            ],
            return_exceptions=True,
        )
        wall = time.perf_counter() - t0

    statuses = Counter()
    replicas = Counter()
    errors = 0
    for r in results:
        if isinstance(r, Exception):
            errors += 1
            continue
        statuses[r.status_code] += 1
        rid = r.headers.get("x-replica-id")
        if rid:
            replicas[rid] += 1

    return {
        "concurrency": concurrency,
        "succeeded": statuses.get(201, 0),
        "rejected_409": statuses.get(409, 0),
        "other_statuses": {k: v for k, v in statuses.items() if k not in (201, 409)},
        "errors": errors,
        "wall_seconds": round(wall, 1),
        "replica_distribution": dict(replicas),
    }


def _build_mixed_requests(resource_ids, total, conflicts):
    """
    `total - conflicts` requests each target a distinct (resource, hour)
    slot that should all succeed; `conflicts` requests deliberately
    re-target slots already claimed by the first batch, so they should
    all come back 409. Returns a shuffled list of (body, should_succeed).
    """
    base = datetime(2031, 1, 1, tzinfo=timezone.utc)
    distinct = total - conflicts
    slots = []
    i = 0
    while len(slots) < distinct:
        resource_id = resource_ids[i % len(resource_ids)]
        start = base + timedelta(hours=i)
        slots.append((resource_id, start))
        i += 1

    requests = [
        (
            {
                "resource_id": rid,
                "start_time": start.isoformat(),
                "end_time": (start + timedelta(hours=1)).isoformat(),
            },
            True,
        )
        for rid, start in slots
    ]

    conflict_targets = random.sample(slots, conflicts)
    requests += [
        (
            {
                "resource_id": rid,
                "start_time": start.isoformat(),
                "end_time": (start + timedelta(hours=1)).isoformat(),
            },
            False,
        )
        for rid, start in conflict_targets
    ]

    random.shuffle(requests)
    return requests


async def mixed_load(host, token, resource_ids, total, conflicts) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    requests = _build_mixed_requests(resource_ids, total, conflicts)
    latencies = []
    replicas = Counter()
    succeeded = 0
    correctly_rejected = 0
    unexpected = 0

    sem = asyncio.Semaphore(300)  # bounded concurrency, not 10k sockets at once
    limits = httpx.Limits(max_connections=300, max_keepalive_connections=300)

    async def send(client, body):
        async with sem:
            t0 = time.perf_counter()
            try:
                r = await client.post(
                    "/bookings",
                    json={**body, "idempotency_key": str(uuid.uuid4())},
                    headers=headers,
                )
            except httpx.HTTPError:
                return "error", None, time.perf_counter() - t0
            return r.status_code, r.headers.get("x-replica-id"), time.perf_counter() - t0

    t_start = time.perf_counter()
    async with httpx.AsyncClient(base_url=host, timeout=60.0, limits=limits) as client:
        outcomes = await asyncio.gather(*[send(client, body) for body, _ in requests])
    wall = time.perf_counter() - t_start

    for (body, should_succeed), (status, rid, latency) in zip(requests, outcomes):
        latencies.append(latency)
        if rid:
            replicas[rid] += 1
        if status == 201:
            succeeded += 1
            if not should_succeed:
                unexpected += 1  # a deliberately-conflicting request shouldn't have landed
        elif status == 409:
            if should_succeed:
                unexpected += 1  # a distinct slot was wrongly rejected
            else:
                correctly_rejected += 1
        else:
            unexpected += 1

    ms = [v * 1000 for v in latencies]
    return {
        "total": total,
        "conflicts": conflicts,
        "succeeded": succeeded,
        "correctly_rejected": correctly_rejected,
        "unexpected": unexpected,
        "wall_seconds": round(wall, 1),
        "throughput_rps": round(total / wall, 1) if wall else 0.0,
        "latency_ms": {
            "p50": round(percentile(ms, 50), 1),
            "p95": round(percentile(ms, 95), 1),
            "p99": round(percentile(ms, 99), 1),
            "max": round(max(ms), 1) if ms else 0.0,
        },
        "replica_distribution": dict(replicas),
    }


def render_markdown(same_slot_results, mixed_result) -> str:
    lines = [
        f"# Scale-demo benchmark results",
        "",
        f"Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} by "
        "`scripts/scale_benchmark.py` against `docker-compose.scale-demo.yml` "
        "(3 app replicas behind nginx, one shared Postgres).",
        "",
        "## Same-slot correctness under maximum contention",
        "",
        "| Concurrent requests | Succeeded | Rejected (409) | Overlapping bookings | Wall time | Errors |",
        "|---|---|---|---|---|---|",
    ]
    for r in same_slot_results:
        lines.append(
            f"| {r['concurrency']:,} | {r['succeeded']} | {r['rejected_409']:,} "
            f"| {r['overlaps_in_db']} | {r['wall_seconds']}s | {r['errors']} |"
        )

    lines += [
        "",
        "Replica distribution per run (via `X-Replica-Id`):",
        "",
    ]
    for r in same_slot_results:
        lines.append(f"- {r['concurrency']:,} requests: `{r['replica_distribution']}`")

    if mixed_result:
        m = mixed_result
        lines += [
            "",
            "## Realistic mixed load",
            "",
            f"{m['total'] - m['conflicts']:,} requests targeted distinct resource/time "
            f"slots (should all succeed); {m['conflicts']:,} deliberately re-targeted an "
            f"already-taken slot (should all fail).",
            "",
            "| | Count | % |",
            "|---|---|---|",
            f"| Succeeded (201) | {m['succeeded']:,} | {m['succeeded'] / m['total'] * 100:.2f}% |",
            f"| Correctly rejected (409) | {m['correctly_rejected']:,} | "
            f"{m['correctly_rejected'] / m['total'] * 100:.2f}% |",
            f"| Unexpected outcomes | **{m['unexpected']}** | "
            f"**{m['unexpected'] / m['total'] * 100:.2f}%** |",
            "",
            f"Sustained **{m['throughput_rps']} req/s**. Client-observed latency: "
            f"p50 {m['latency_ms']['p50']}ms, p95 {m['latency_ms']['p95']}ms, "
            f"p99 {m['latency_ms']['p99']}ms, max {m['latency_ms']['max']}ms.",
            "",
            f"Replica distribution: `{m['replica_distribution']}`",
        ]

    return "\n".join(lines) + "\n"


async def main_async(args):
    async with httpx.AsyncClient(base_url=args.host, timeout=30.0) as setup_client:
        token = await ensure_test_user(setup_client)
        make_admin(args.db_url, TEST_EMAIL)
        # Re-login: require_admin reads is_admin off the DB per-request via
        # the token's subject, not off the token itself, so no fresh token
        # is strictly required — but this keeps setup order obviously safe
        # if that ever changes.
        token = await ensure_test_user(setup_client)
        resource_ids = await ensure_resources(setup_client, token, args.resource_count)

    same_slot_results = []
    if not args.skip_same_slot:
        for i, level in enumerate(args.levels):
            print(f"[same-slot] racing {level} concurrent requests for one slot...")
            result = await race_same_slot(args.host, token, resource_ids[0], level, i)
            result["overlaps_in_db"] = count_overlaps(args.db_url)
            same_slot_results.append(result)
            print(f"  -> {json.dumps(result)}")

    mixed_result = None
    if not args.skip_mixed:
        print(f"[mixed-load] sending {args.mixed_total} requests "
              f"({args.mixed_conflicts} deliberately conflicting)...")
        mixed_result = await mixed_load(
            args.host, token, resource_ids, args.mixed_total, args.mixed_conflicts
        )
        print(f"  -> {json.dumps(mixed_result)}")

    report = render_markdown(same_slot_results, mixed_result)
    print("\n" + report)

    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w") as f:
            f.write(report)
        print(f"Wrote {args.out}")
    if args.json_out:
        os.makedirs(os.path.dirname(args.json_out) or ".", exist_ok=True)
        with open(args.json_out, "w") as f:
            json.dump(
                {"same_slot": same_slot_results, "mixed_load": mixed_result},
                f,
                indent=2,
            )
        print(f"Wrote {args.json_out}")


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--db-url", default=DEFAULT_DB_URL)
    p.add_argument(
        "--levels",
        default=",".join(str(n) for n in DEFAULT_LEVELS),
        help="comma-separated concurrency levels for the same-slot test",
    )
    p.add_argument("--resource-count", type=int, default=5)
    p.add_argument("--mixed-total", type=int, default=MIXED_LOAD_TOTAL)
    p.add_argument("--mixed-conflicts", type=int, default=MIXED_LOAD_CONFLICTS)
    p.add_argument("--skip-same-slot", action="store_true")
    p.add_argument("--skip-mixed", action="store_true")
    p.add_argument("--out", default="benchmark_results/scale_demo.md")
    p.add_argument("--json-out", default="benchmark_results/scale_demo.json")
    args = p.parse_args()
    args.levels = [int(x) for x in args.levels.split(",") if x.strip()]
    return args


def main():
    args = parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
