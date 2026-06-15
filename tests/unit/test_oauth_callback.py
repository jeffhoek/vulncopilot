"""Unit tests for the GitHub OAuth allow-list callback in app.py."""

import app
from config import settings


class _DefaultUser:
    """Stand-in for chainlit.User — the callback only touches .identifier."""

    def __init__(self) -> None:
        self.identifier = "github:placeholder"


def _call(raw_user_data: dict):
    return app.oauth_callback("github", "token", raw_user_data, _DefaultUser())


def _reset(monkeypatch):
    """Lock the allow-list shut so each test opts into exactly one path."""
    monkeypatch.setattr(settings, "open_registration", False)
    monkeypatch.setattr(settings, "allowed_emails", [])
    monkeypatch.setattr(settings, "allowed_email_domains", [])
    monkeypatch.setattr(settings, "allowed_logins", [])


def test_open_registration_allows_anyone(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(settings, "open_registration", True)

    user = _call({"id": 12345678, "login": "randomuser", "email": "nobody@nowhere.io"})

    assert user is not None
    assert user.identifier == "github:12345678"


def test_allowed_email_match(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(settings, "allowed_emails", ["alice@example.com"])

    user = _call({"id": 42, "login": "alice", "email": "alice@example.com"})

    assert user is not None
    assert user.identifier == "github:42"


def test_allowed_email_domain_match(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(settings, "allowed_email_domains", ["mycompany.com"])

    user = _call({"id": 7, "login": "bob", "email": "bob@mycompany.com"})

    assert user is not None
    assert user.identifier == "github:7"


def test_allowed_login_match(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(settings, "allowed_logins", ["jeffhoek"])

    user = _call({"id": 99, "login": "jeffhoek", "email": ""})

    assert user is not None
    # identifier is the stable numeric id, never the (mutable) login.
    assert user.identifier == "github:99"


def test_disallowed_user_denied(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(settings, "allowed_logins", ["jeffhoek"])

    user = _call({"id": 1, "login": "intruder", "email": "intruder@evil.com"})

    assert user is None


def test_empty_email_does_not_match_domain(monkeypatch):
    """A missing email must not be coerced into a domain match."""
    _reset(monkeypatch)
    monkeypatch.setattr(settings, "allowed_email_domains", ["mycompany.com"])

    user = _call({"id": 2, "login": "ghost", "email": ""})

    assert user is None
