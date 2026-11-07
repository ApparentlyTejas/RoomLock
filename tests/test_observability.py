"""X-Request-ID: generated when absent, echoed back when the client supplies one."""


def test_generates_request_id_when_absent(client):
    response = client.get("/health")
    assert "X-Request-ID" in response.headers
    assert len(response.headers["X-Request-ID"]) > 0


def test_echoes_client_supplied_request_id(client):
    response = client.get("/health", headers={"X-Request-ID": "test-fixed-id-123"})
    assert response.headers["X-Request-ID"] == "test-fixed-id-123"
