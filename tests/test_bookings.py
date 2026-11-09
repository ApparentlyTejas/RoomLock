def _book(client, headers, resource_id, start="2027-01-01T10:00:00Z", end="2027-01-01T11:00:00Z", key=None):
    payload = {"resource_id": resource_id, "start_time": start, "end_time": end}
    if key:
        payload["idempotency_key"] = key
    return client.post("/bookings", json=payload, headers=headers)


def test_create_booking_success(client, user_headers, resource_id):
    resp = _book(client, user_headers, resource_id)
    assert resp.status_code == 201
    body = resp.json()
    assert body["resource_id"] == resource_id


def test_end_before_start_rejected(client, user_headers, resource_id):
    resp = _book(client, user_headers, resource_id, start="2027-01-01T11:00:00Z", end="2027-01-01T10:00:00Z")
    assert resp.status_code == 422


def test_booking_unknown_resource_404s(client, user_headers):
    resp = _book(client, user_headers, resource_id=999999)
    assert resp.status_code == 404


def test_overlapping_booking_rejected(client, user_headers, resource_id):
    first = _book(client, user_headers, resource_id)
    assert first.status_code == 201
    second = _book(client, user_headers, resource_id, start="2027-01-01T10:30:00Z", end="2027-01-01T11:30:00Z")
    assert second.status_code == 409


def test_adjacent_non_overlapping_bookings_both_succeed(client, user_headers, resource_id):
    first = _book(client, user_headers, resource_id, start="2027-01-01T10:00:00Z", end="2027-01-01T11:00:00Z")
    second = _book(client, user_headers, resource_id, start="2027-01-01T11:00:00Z", end="2027-01-01T12:00:00Z")
    assert first.status_code == 201
    assert second.status_code == 201


def test_idempotency_key_replay_returns_same_booking(client, user_headers, resource_id):
    first = _book(client, user_headers, resource_id, key="my-key-1")
    second = _book(client, user_headers, resource_id, key="my-key-1")
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]


def test_list_bookings_for_resource_and_date(client, user_headers, resource_id):
    _book(client, user_headers, resource_id)
    resp = client.get(f"/bookings?resource_id={resource_id}&date=2027-01-01", headers=user_headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_my_bookings_lists_own_bookings_with_resource_name(client, user_headers, resource_id):
    _book(client, user_headers, resource_id)
    resp = client.get("/bookings/me", headers=user_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["resource_name"] == "Test Room"


def test_cancel_own_upcoming_booking(client, user_headers, resource_id):
    booking = _book(client, user_headers, resource_id).json()
    resp = client.delete(f"/bookings/{booking['id']}", headers=user_headers)
    assert resp.status_code == 204
    # slot should be free again
    rebooked = _book(client, user_headers, resource_id)
    assert rebooked.status_code == 201


def test_cannot_cancel_someone_elses_booking(client, user_headers, resource_id):
    booking = _book(client, user_headers, resource_id).json()
    other_token = None
    from tests.conftest import register_and_login

    other_token = register_and_login(client, "other@example.com")
    resp = client.delete(f"/bookings/{booking['id']}", headers={"Authorization": f"Bearer {other_token}"})
    assert resp.status_code == 403


def test_cancel_nonexistent_booking_404s(client, user_headers):
    resp = client.delete("/bookings/999999", headers=user_headers)
    assert resp.status_code == 404


def test_cancel_past_booking_rejected(client, user_headers, resource_id):
    booking = _book(
        client, user_headers, resource_id, start="2020-01-01T10:00:00Z", end="2020-01-01T11:00:00Z"
    ).json()
    resp = client.delete(f"/bookings/{booking['id']}", headers=user_headers)
    assert resp.status_code == 400


def _edit(client, headers, booking_id, start, end):
    return client.patch(
        f"/bookings/{booking_id}",
        json={"start_time": start, "end_time": end},
        headers=headers,
    )


def test_edit_own_booking_to_free_slot(client, user_headers, resource_id):
    booking = _book(client, user_headers, resource_id).json()
    resp = _edit(client, user_headers, booking["id"], "2027-01-01T14:00:00Z", "2027-01-01T15:00:00Z")
    assert resp.status_code == 200
    assert resp.json()["start_time"].startswith("2027-01-01T14:00:00")


def test_edit_into_another_bookings_slot_rejected(client, user_headers, resource_id):
    _book(client, user_headers, resource_id, start="2027-01-01T14:00:00Z", end="2027-01-01T15:00:00Z")
    mover = _book(client, user_headers, resource_id, start="2027-01-01T10:00:00Z", end="2027-01-01T11:00:00Z").json()
    resp = _edit(client, user_headers, mover["id"], "2027-01-01T14:00:00Z", "2027-01-01T15:00:00Z")
    assert resp.status_code == 409


def test_edit_can_keep_same_slot(client, user_headers, resource_id):
    booking = _book(client, user_headers, resource_id, start="2027-01-01T10:00:00Z", end="2027-01-01T11:00:00Z").json()
    resp = _edit(client, user_headers, booking["id"], "2027-01-01T10:00:00Z", "2027-01-01T11:00:00Z")
    assert resp.status_code == 200


def test_cannot_edit_someone_elses_booking(client, user_headers, resource_id):
    from tests.conftest import register_and_login

    booking = _book(client, user_headers, resource_id).json()
    other_token = register_and_login(client, "other@example.com")
    resp = _edit(
        client, {"Authorization": f"Bearer {other_token}"}, booking["id"], "2027-01-01T14:00:00Z", "2027-01-01T15:00:00Z"
    )
    assert resp.status_code == 403


def test_cannot_edit_to_a_past_time(client, user_headers, resource_id):
    booking = _book(client, user_headers, resource_id).json()
    resp = _edit(client, user_headers, booking["id"], "2020-01-01T10:00:00Z", "2020-01-01T11:00:00Z")
    assert resp.status_code == 400


def test_cannot_edit_a_past_booking(client, user_headers, resource_id):
    booking = _book(
        client, user_headers, resource_id, start="2020-01-01T10:00:00Z", end="2020-01-01T11:00:00Z"
    ).json()
    resp = _edit(client, user_headers, booking["id"], "2027-01-01T10:00:00Z", "2027-01-01T11:00:00Z")
    assert resp.status_code == 400


def _book_recurring(
    client, headers, resource_id, occurrences, start="2027-01-01T10:00:00Z", end="2027-01-01T11:00:00Z", key=None
):
    payload = {"resource_id": resource_id, "start_time": start, "end_time": end, "occurrences": occurrences}
    if key:
        payload["idempotency_key"] = key
    return client.post("/bookings/recurring", json=payload, headers=headers)


def test_create_recurring_booking_success(client, user_headers, resource_id):
    resp = _book_recurring(client, user_headers, resource_id, occurrences=4)
    assert resp.status_code == 201
    body = resp.json()
    assert len(body["created"]) == 4
    assert body["skipped"] == []
    series_ids = {b["series_id"] for b in body["created"]}
    assert series_ids == {body["series_id"]}
    starts = sorted(b["start_time"] for b in body["created"])
    assert starts[0].startswith("2027-01-01T10:00:00")
    assert starts[1].startswith("2027-01-08T10:00:00")
    assert starts[3].startswith("2027-01-22T10:00:00")


def test_recurring_booking_skips_conflicting_week(client, user_headers, resource_id):
    # third occurrence (2027-01-15) is already taken by another booking
    _book(client, user_headers, resource_id, start="2027-01-15T10:00:00Z", end="2027-01-15T11:00:00Z")
    resp = _book_recurring(client, user_headers, resource_id, occurrences=4)
    assert resp.status_code == 201
    body = resp.json()
    assert len(body["created"]) == 3
    assert len(body["skipped"]) == 1
    assert body["skipped"][0]["start_time"].startswith("2027-01-15T10:00:00")


def test_recurring_booking_end_before_start_rejected(client, user_headers, resource_id):
    resp = _book_recurring(
        client, user_headers, resource_id, occurrences=3, start="2027-01-01T11:00:00Z", end="2027-01-01T10:00:00Z"
    )
    assert resp.status_code == 422


def test_recurring_booking_unknown_resource_404s(client, user_headers):
    resp = _book_recurring(client, user_headers, resource_id=999999, occurrences=3)
    assert resp.status_code == 404


def test_recurring_booking_occurrences_out_of_range_rejected(client, user_headers, resource_id):
    assert _book_recurring(client, user_headers, resource_id, occurrences=1).status_code == 422
    assert _book_recurring(client, user_headers, resource_id, occurrences=53).status_code == 422


def test_recurring_idempotency_key_replay_returns_same_series(client, user_headers, resource_id):
    first = _book_recurring(client, user_headers, resource_id, occurrences=4, key="series-key-1")
    second = _book_recurring(client, user_headers, resource_id, occurrences=4, key="series-key-1")
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["series_id"] == second.json()["series_id"]
    assert sorted(b["id"] for b in first.json()["created"]) == sorted(b["id"] for b in second.json()["created"])

    mine = client.get("/bookings/me", headers=user_headers).json()
    assert len(mine) == 4  # the replay must not have created a second series


def test_recurring_idempotency_key_does_not_collide_across_users(client, user_headers, resource_id):
    from tests.conftest import register_and_login

    other_token = register_and_login(client, "other@example.com")
    other_headers = {"Authorization": f"Bearer {other_token}"}

    first = _book_recurring(client, user_headers, resource_id, occurrences=2, key="shared-key")
    second = _book_recurring(
        client,
        other_headers,
        resource_id,
        occurrences=2,
        start="2027-02-01T10:00:00Z",
        end="2027-02-01T11:00:00Z",
        key="shared-key",
    )
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["series_id"] != second.json()["series_id"]


def test_recurring_without_idempotency_key_creates_separate_series_each_call(client, user_headers, resource_id):
    first = _book_recurring(client, user_headers, resource_id, occurrences=2)
    second = _book_recurring(
        client, user_headers, resource_id, occurrences=2, start="2027-03-01T10:00:00Z", end="2027-03-01T11:00:00Z"
    )
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["series_id"] != second.json()["series_id"]


def test_cancel_series_removes_all_future_occurrences(client, user_headers, resource_id):
    created = _book_recurring(client, user_headers, resource_id, occurrences=3).json()
    series_id = created["series_id"]
    resp = client.delete(f"/bookings/series/{series_id}", headers=user_headers)
    assert resp.status_code == 204
    remaining = client.get("/bookings/me", headers=user_headers).json()
    assert remaining == []


def test_cannot_cancel_someone_elses_series(client, user_headers, resource_id):
    from tests.conftest import register_and_login

    created = _book_recurring(client, user_headers, resource_id, occurrences=3).json()
    other_token = register_and_login(client, "other@example.com")
    resp = client.delete(
        f"/bookings/series/{created['series_id']}", headers={"Authorization": f"Bearer {other_token}"}
    )
    assert resp.status_code == 404


def test_cancel_nonexistent_series_404s(client, user_headers):
    resp = client.delete("/bookings/series/00000000-0000-0000-0000-000000000000", headers=user_headers)
    assert resp.status_code == 404
