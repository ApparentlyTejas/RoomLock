"""
Load test for the naive check-then-insert /bookings endpoint.

All simulated users log in as the same test account and POST the *same*
resource_id + start_time/end_time window, so requests race for the exact
same slot. Run with a high spawn rate so requests actually overlap in-flight:

    locust -f loadtest/locustfile.py --headless -u 30 -r 30 -t 10s \
        --host http://localhost:8000

Configure the target via env vars (defaults match the test user/resource
created during setup):
    TEST_EMAIL, TEST_PASSWORD, RESOURCE_ID, START_TIME, END_TIME

Pass a pre-fetched TOKEN env var to skip per-user login entirely — this
matters because each user logging in independently (bcrypt hashing takes
hundreds of ms) staggers their first /bookings request enough that they
may not actually land concurrently. Get one with:

    curl -s -X POST http://localhost:8000/auth/login \
      -d "username=$TEST_EMAIL&password=$TEST_PASSWORD" | jq -r .access_token
"""

import os

from locust import HttpUser, between, task

TEST_EMAIL = os.environ.get("TEST_EMAIL", "loadtest@example.com")
TEST_PASSWORD = os.environ.get("TEST_PASSWORD", "loadtest-password")
RESOURCE_ID = int(os.environ.get("RESOURCE_ID", "1"))
START_TIME = os.environ.get("START_TIME", "2026-09-01T10:00:00Z")
END_TIME = os.environ.get("END_TIME", "2026-09-01T11:00:00Z")
PRESET_TOKEN = os.environ.get("TOKEN")


class BookingRaceUser(HttpUser):
    wait_time = between(0, 0)

    def on_start(self):
        if PRESET_TOKEN:
            token = PRESET_TOKEN
        else:
            resp = self.client.post(
                "/auth/login",
                data={"username": TEST_EMAIL, "password": TEST_PASSWORD},
            )
            resp.raise_for_status()
            token = resp.json()["access_token"]
        self.client.headers.update({"Authorization": f"Bearer {token}"})

    @task
    def book_same_slot(self):
        self.client.post(
            "/bookings",
            json={
                "resource_id": RESOURCE_ID,
                "start_time": START_TIME,
                "end_time": END_TIME,
            },
            name="/bookings (racing same slot)",
        )
