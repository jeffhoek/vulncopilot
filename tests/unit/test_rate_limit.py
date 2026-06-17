"""Unit tests for the per-user effective rate limit in app.py."""

import app
from config import settings


def test_default_user_gets_standard_limit(monkeypatch):
    monkeypatch.setattr(settings, "daily_query_limit", 20)
    monkeypatch.setattr(settings, "admin_daily_query_limit", 100000)
    monkeypatch.setattr(settings, "admin_user_identifiers", ["github:111"])

    assert app._limit_for("github:999") == 20


def test_admin_identifier_gets_elevated_limit(monkeypatch):
    monkeypatch.setattr(settings, "daily_query_limit", 20)
    monkeypatch.setattr(settings, "admin_daily_query_limit", 100000)
    monkeypatch.setattr(settings, "admin_user_identifiers", ["github:111", "github:222"])

    assert app._limit_for("github:222") == 100000


def test_empty_admin_list_means_everyone_standard(monkeypatch):
    monkeypatch.setattr(settings, "daily_query_limit", 5)
    monkeypatch.setattr(settings, "admin_user_identifiers", [])

    assert app._limit_for("github:111") == 5
