#!/usr/bin/env python3
"""
Reproduces the README's top table — "a real race condition, fixed twice,
measured both times" — from one command, instead of it being three manual
runs transcribed by hand. For each of the three historical states (naive
check-then-insert, Fix 1: row lock, Fix 2: exclusion constraint) it checks
out that commit into a disposable git worktree, builds and runs it in
isolation via its own docker-compose.yml, races 30 concurrent requests at
one slot with Locust, verifies overlapping bookings directly against
Postgres, then tears the worktree down before moving to the next one.

Needs: docker, git, and `pip install locust` (not a project dependency —
only this manual benchmark uses it). Takes a few minutes total (three
image builds). Run from the repo root:

    python3 scripts/fix_comparison_benchmark.py

By default "Fix 2" benchmarks the current checked-out commit (HEAD) rather
than the historical 0108c14 commit, since Fix 2 is the live implementation
main has kept building on — pass --fix2-ref to pin it to that commit
instead.
"""

import argparse
import csv
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

import httpx

from _bench_common import count_overlaps

NAIVE_REF = "c94703f"  # naive check-then-insert + the Locust test confirming the race
FIX1_REF = "7675679"  # SELECT ... FOR UPDATE row lock
FIX2_REF = "HEAD"  # Postgres EXCLUDE constraint (what main has kept building on)

TEST_EMAIL = "fix-bench@example.com"
TEST_PASSWORD = "fix-bench-password-123"
RACE_USERS = 30
RACE_DURATION = "10s"


def run(cmd, cwd=None, check=True, capture=False):
    print(f"$ {' '.join(shlex.quote(c) for c in cmd)}" + (f"  (cwd={cwd})" if cwd else ""))
    return subprocess.run(
        cmd, cwd=cwd, check=check,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        text=True,
    )


def resolve_ref(repo_root, ref) -> str:
    return run(["git", "rev-parse", ref], cwd=repo_root, capture=True).stdout.strip()


def write_env(worktree, jwt_secret):
    example_path = os.path.join(worktree, ".env.example")
    with open(example_path) as f:
        content = f.read()
    content = re.sub(r"JWT_SECRET_KEY=.*", f"JWT_SECRET_KEY={jwt_secret}", content)
    with open(os.path.join(worktree, ".env"), "w") as f:
        f.write(content)


def wait_for(predicate, timeout_s=90, interval_s=2, what="service"):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    raise TimeoutError(f"Timed out waiting for {what}")


def setup_stack(worktree, project):
    compose = ["docker", "compose", "-p", project, "--project-directory", worktree,
               "-f", os.path.join(worktree, "docker-compose.yml")]
    run(compose + ["up", "--build", "-d"], cwd=worktree)

    def db_ready():
        r = run(compose + ["exec", "-T", "db", "pg_isready", "-U", "app", "-d", "bookingapi"],
                cwd=worktree, check=False, capture=True)
        return r.returncode == 0

    wait_for(db_ready, what="Postgres")

    for name in sorted(os.listdir(os.path.join(worktree, "db"))):
        if name.endswith(".sql"):
            run(compose + ["exec", "-T", "db", "psql", "-U", "app", "-d", "bookingapi",
                            "-f", f"/db/{name}"], cwd=worktree)

    def api_ready():
        try:
            return httpx.get("http://localhost:8000/health", timeout=3).status_code == 200
        except httpx.HTTPError:
            return False

    wait_for(api_ready, what="API")
    return compose


def teardown_stack(compose, worktree):
    run(compose + ["down", "-v"], cwd=worktree, check=False)


def setup_user_and_resource(db_url) -> int:
    with httpx.Client(base_url="http://localhost:8000", timeout=15) as client:
        resp = client.post("/auth/register", json={"email": TEST_EMAIL, "password": TEST_PASSWORD})
        if resp.status_code not in (201, 400):
            resp.raise_for_status()
        try:
            import psycopg2
            conn = psycopg2.connect(db_url)
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("UPDATE users SET is_admin = true WHERE email = %s", (TEST_EMAIL,))
            cur.close()
            conn.close()
        except Exception:
            pass  # older commits predate role-based admin — resource creation is unrestricted there

        resp = client.post("/auth/login", data={"username": TEST_EMAIL, "password": TEST_PASSWORD})
        resp.raise_for_status()
        token = resp.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        resp = client.post("/resources", json={"name": "fix-bench-room"}, headers=headers)
        resp.raise_for_status()
        return resp.json()["id"], token


def run_locust(worktree, resource_id, token, csv_prefix) -> dict:
    env = {
        **os.environ,
        "TEST_EMAIL": TEST_EMAIL,
        "TEST_PASSWORD": TEST_PASSWORD,
        "RESOURCE_ID": str(resource_id),
        "TOKEN": token,
    }
    subprocess.run(
        ["locust", "-f", os.path.join(worktree, "loadtest", "locustfile.py"),
         "--headless", "-u", str(RACE_USERS), "-r", str(RACE_USERS), "-t", RACE_DURATION,
         "--host", "http://localhost:8000", "--csv", csv_prefix],
        env=env, check=True,
    )
    stats_file = f"{csv_prefix}_stats.csv"
    with open(stats_file, newline="") as f:
        rows = list(csv.reader(f))
    header, *data = rows
    agg = next(r for r in data if r[header.index("Name")] == "Aggregated")

    def col(name):
        return agg[header.index(name)]

    return {
        "requests": int(col("Request Count")),
        "rps": float(col("Requests/s")),
    }


def bench_one(repo_root, label, ref) -> dict:
    sha = resolve_ref(repo_root, ref)
    worktree = tempfile.mkdtemp(prefix=f"roomlock-bench-{label}-")
    shutil.rmtree(worktree)  # git worktree add requires the path not exist
    project = f"roomlock-bench-{label}"
    print(f"\n=== {label} ({ref} -> {sha[:12]}) ===")
    run(["git", "worktree", "add", "--detach", worktree, sha], cwd=repo_root)
    compose = None
    try:
        write_env(worktree, jwt_secret=f"bench-only-secret-{label}-do-not-use-in-prod")
        compose = setup_stack(worktree, project)
        resource_id, token = setup_user_and_resource(
            "postgresql://app:app_dev_password@localhost:5434/bookingapi"
        )
        csv_prefix = os.path.join(tempfile.gettempdir(), f"roomlock-locust-{label}")
        stats = run_locust(worktree, resource_id, token, csv_prefix)
        overlaps = count_overlaps("postgresql://app:app_dev_password@localhost:5434/bookingapi")
        return {
            "label": label,
            "ref": ref,
            "commit": sha[:12],
            "requests": stats["requests"],
            "rps": stats["rps"],
            "overlapping_bookings": overlaps,
        }
    finally:
        if compose:
            teardown_stack(compose, worktree)
        run(["git", "worktree", "remove", "--force", worktree], cwd=repo_root, check=False)


def render_markdown(results) -> str:
    lines = [
        "# Fix-comparison benchmark results",
        "",
        "Generated by `scripts/fix_comparison_benchmark.py` — each row is a "
        f"fresh build of that commit, raced with {RACE_USERS} concurrent users "
        f"for {RACE_DURATION} against the same booking slot.",
        "",
        "| Version | Commit | Requests | Throughput | Overlapping bookings in DB |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['label']} | `{r['commit']}` | {r['requests']:,} "
            f"| {r['rps']:.0f} req/s | {r['overlapping_bookings']} |"
        )
    return "\n".join(lines) + "\n"


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--naive-ref", default=NAIVE_REF)
    p.add_argument("--fix1-ref", default=FIX1_REF)
    p.add_argument("--fix2-ref", default=FIX2_REF)
    p.add_argument("--out", default="benchmark_results/fix_comparison.md")
    p.add_argument("--json-out", default="benchmark_results/fix_comparison.json")
    args = p.parse_args()

    if shutil.which("locust") is None:
        sys.exit("locust not found on PATH — install it with `pip install locust` first")

    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = run(["git", "rev-parse", "--show-toplevel"], cwd=script_dir, capture=True).stdout.strip()

    results = []
    for slug, label, ref in [
        ("naive", "Naive check-then-insert", args.naive_ref),
        ("fix1", "Fix 1: SELECT ... FOR UPDATE row lock", args.fix1_ref),
        ("fix2", "Fix 2: Postgres EXCLUDE constraint", args.fix2_ref),
    ]:
        result = bench_one(repo_root, slug, ref)
        result["label"] = label
        results.append(result)

    report = render_markdown(results)
    print("\n" + report)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        f.write(report)
    with open(args.json_out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Wrote {args.out} and {args.json_out}")


if __name__ == "__main__":
    main()
