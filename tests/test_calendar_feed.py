def test_feed_token_is_generated_and_stable(client, user_headers):
    first = client.get("/calendar/feed-token", headers=user_headers).json()["token"]
    second = client.get("/calendar/feed-token", headers=user_headers).json()["token"]
    assert first == second
    assert len(first) > 20


def test_feed_token_requires_auth(client):
    resp = client.get("/calendar/feed-token")
    assert resp.status_code == 401


def test_regenerate_changes_token_and_invalidates_old_one(client, user_headers):
    old_token = client.get("/calendar/feed-token", headers=user_headers).json()["token"]
    new_token = client.post("/calendar/feed-token/regenerate", headers=user_headers).json()["token"]
    assert new_token != old_token
    assert client.get(f"/calendar/feed/{old_token}.ics").status_code == 404
    assert client.get(f"/calendar/feed/{new_token}.ics").status_code == 200


def test_unknown_feed_token_404s(client):
    resp = client.get("/calendar/feed/not-a-real-token.ics")
    assert resp.status_code == 404


def test_feed_requires_no_auth_header(client, user_headers):
    token = client.get("/calendar/feed-token", headers=user_headers).json()["token"]
    # deliberately no Authorization header — calendar apps can't send one
    resp = client.get(f"/calendar/feed/{token}.ics")
    assert resp.status_code == 200


def test_feed_contains_upcoming_booking(client, user_headers, resource_id):
    token = client.get("/calendar/feed-token", headers=user_headers).json()["token"]
    client.post(
        "/bookings",
        json={
            "resource_id": resource_id,
            "start_time": "2027-01-05T10:00:00Z",
            "end_time": "2027-01-05T11:00:00Z",
        },
        headers=user_headers,
    )
    resp = client.get(f"/calendar/feed/{token}.ics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/calendar")
    body = resp.text
    assert "BEGIN:VCALENDAR" in body
    assert "END:VCALENDAR" in body
    assert "DTSTART:20270105T100000Z" in body
    assert "DTEND:20270105T110000Z" in body
    assert "Test Room booking" in body


def test_feed_excludes_past_bookings(client, user_headers, resource_id):
    token = client.get("/calendar/feed-token", headers=user_headers).json()["token"]
    client.post(
        "/bookings",
        json={
            "resource_id": resource_id,
            "start_time": "2020-01-01T10:00:00Z",
            "end_time": "2020-01-01T11:00:00Z",
        },
        headers=user_headers,
    )
    resp = client.get(f"/calendar/feed/{token}.ics")
    assert "BEGIN:VEVENT" not in resp.text


def test_feed_only_shows_own_bookings(client, user_headers, admin_headers, resource_id):
    client.post(
        "/bookings",
        json={
            "resource_id": resource_id,
            "start_time": "2027-01-05T10:00:00Z",
            "end_time": "2027-01-05T11:00:00Z",
        },
        headers=admin_headers,
    )
    token = client.get("/calendar/feed-token", headers=user_headers).json()["token"]
    resp = client.get(f"/calendar/feed/{token}.ics")
    assert "BEGIN:VEVENT" not in resp.text


def test_recurring_booking_skip_dates_excludes_those_occurrences(client, user_headers, resource_id):
    resp = client.post(
        "/bookings/recurring",
        json={
            "resource_id": resource_id,
            "start_time": "2027-02-02T10:00:00Z",
            "end_time": "2027-02-02T11:00:00Z",
            "occurrences": 4,
            "skip_dates": ["2027-02-16"],
        },
        headers=user_headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert len(body["created"]) == 3
    created_dates = {b["start_time"][:10] for b in body["created"]}
    assert "2027-02-16" not in created_dates
    assert len(body["skipped"]) == 1
    assert body["skipped"][0]["reason"] == "Skipped by request"


def test_recurring_booking_without_skip_dates_still_works(client, user_headers, resource_id):
    resp = client.post(
        "/bookings/recurring",
        json={
            "resource_id": resource_id,
            "start_time": "2027-03-02T10:00:00Z",
            "end_time": "2027-03-02T11:00:00Z",
            "occurrences": 3,
        },
        headers=user_headers,
    )
    assert resp.status_code == 201
    assert len(resp.json()["created"]) == 3
