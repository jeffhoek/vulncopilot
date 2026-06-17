"""Unit tests for the /admin dashboard: HTTP Basic Auth + rendering (PR 3)."""

import base64

from starlette.testclient import TestClient


def _auth_header(password: str) -> dict:
    # Username is ignored; only the password is checked.
    token = base64.b64encode(f":{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _make_client(monkeypatch, rows):
    import admin.dashboard as dashboard
    import app
    from config import settings

    monkeypatch.setattr(settings, "admin_secret", "s3cret")
    monkeypatch.setattr(dashboard, "get_pool", lambda: object())

    async def fake_get_usage_stats(_pool, _in_cost, _out_cost):
        return rows

    monkeypatch.setattr(dashboard, "get_usage_stats", fake_get_usage_stats)
    return TestClient(app.fastapi_app, follow_redirects=False)


def _row(**overrides):
    base = {
        "user_identifier": "github:12345678",
        "queries_today": 3,
        "queries_7d": 10,
        "queries_30d": 42,
        "input_tokens": 1234567,
        "output_tokens": 89012,
        "est_cost": 1.2345,
    }
    base.update(overrides)
    return base


def test_missing_credentials_returns_401(monkeypatch):
    client = _make_client(monkeypatch, [])
    resp = client.get("/admin")

    assert resp.status_code == 401
    assert resp.headers["www-authenticate"].startswith("Basic")


def test_wrong_password_returns_401(monkeypatch):
    client = _make_client(monkeypatch, [_row()])
    resp = client.get("/admin", headers=_auth_header("wrong"))

    assert resp.status_code == 401


def test_correct_password_renders_usage_table(monkeypatch):
    client = _make_client(monkeypatch, [_row()])
    resp = client.get("/admin", headers=_auth_header("s3cret"))

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "github:12345678" in resp.text
    assert "1,234,567" in resp.text  # input tokens, thousands-separated
    assert "$1.2345" in resp.text  # estimated cost


def test_empty_usage_shows_graceful_state(monkeypatch):
    client = _make_client(monkeypatch, [])
    resp = client.get("/admin", headers=_auth_header("s3cret"))

    assert resp.status_code == 200
    assert "No usage recorded yet." in resp.text
    assert "<table" not in resp.text


def test_user_identifier_is_autoescaped(monkeypatch):
    client = _make_client(monkeypatch, [_row(user_identifier="<script>alert(1)</script>")])
    resp = client.get("/admin", headers=_auth_header("s3cret"))

    assert "<script>alert(1)</script>" not in resp.text
    assert "&lt;script&gt;" in resp.text


def test_admin_route_not_shadowed_by_chainlit_catchall(monkeypatch):
    """Regression: /admin must win over Chainlit's "/{full_path:path}" SPA catch-all."""
    client = _make_client(monkeypatch, [])
    resp = client.get("/admin", headers=_auth_header("s3cret"))

    assert resp.status_code == 200
    assert "<title>Usage dashboard</title>" in resp.text
