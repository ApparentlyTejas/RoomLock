from tests.conftest import register_and_login


def test_register_creates_user(client):
    resp = client.post("/auth/register", json={"email": "a@example.com", "password": "Goodpassword1"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["email"] == "a@example.com"
    assert body["is_admin"] is False
    assert "password" not in body
    assert "hashed_password" not in body


def test_register_duplicate_email_rejected(client):
    client.post("/auth/register", json={"email": "a@example.com", "password": "Goodpassword1"})
    resp = client.post("/auth/register", json={"email": "a@example.com", "password": "Otherpassword1"})
    assert resp.status_code == 400


def test_register_too_short_password_rejected(client):
    resp = client.post("/auth/register", json={"email": "a@example.com", "password": "Sh0rt"})
    assert resp.status_code == 422


def test_register_password_missing_uppercase_rejected(client):
    resp = client.post("/auth/register", json={"email": "a@example.com", "password": "lowercase123"})
    assert resp.status_code == 422
    assert "uppercase" in resp.json()["detail"][0]["msg"]


def test_register_password_missing_lowercase_rejected(client):
    resp = client.post("/auth/register", json={"email": "a@example.com", "password": "UPPERCASE123"})
    assert resp.status_code == 422
    assert "lowercase" in resp.json()["detail"][0]["msg"]


def test_register_password_missing_number_rejected(client):
    resp = client.post("/auth/register", json={"email": "a@example.com", "password": "NoNumbersHere"})
    assert resp.status_code == 422
    assert "number" in resp.json()["detail"][0]["msg"]


def test_login_success(client):
    client.post("/auth/register", json={"email": "a@example.com", "password": "Goodpassword1"})
    resp = client.post("/auth/login", data={"username": "a@example.com", "password": "Goodpassword1"})
    assert resp.status_code == 200
    assert "access_token" in resp.json()


def test_login_wrong_password(client):
    client.post("/auth/register", json={"email": "a@example.com", "password": "Goodpassword1"})
    resp = client.post("/auth/login", data={"username": "a@example.com", "password": "wrongpassword"})
    assert resp.status_code == 401


def test_login_nonexistent_user(client):
    resp = client.post("/auth/login", data={"username": "nobody@example.com", "password": "whatever123"})
    assert resp.status_code == 401


def test_me_returns_current_user(client):
    token = register_and_login(client, "a@example.com")
    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["email"] == "a@example.com"


def test_me_requires_auth(client):
    resp = client.get("/auth/me")
    assert resp.status_code == 401
