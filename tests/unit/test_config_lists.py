"""Regression tests for JSON-array env var parsing in config.Settings.

A blank pipeline variable (defined but left empty) used to crash startup because
pydantic-settings JSON-decodes list fields in the settings source, where
``json.loads("")`` raises before any validator runs. The JsonStrList type treats
blank as []; invalid JSON must still fail fast.
"""

import pytest
from pydantic import ValidationError

from config import Settings

# Importing app/Chainlit calls load_dotenv(), so a developer's local .env lands in
# os.environ. Clear the JSON-array keys we assert on (unless a test sets one) so the
# environment, not the .env, decides each case. _env_file=None disables the file read.
_LIST_KEYS = ("ADMIN_USER_IDENTIFIERS", "ALLOWED_LOGINS", "ALLOWED_EMAILS", "ALLOWED_EMAIL_DOMAINS", "ACTION_BUTTONS")


def _settings(monkeypatch, **env) -> Settings:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    for k in _LIST_KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)


def test_empty_string_parses_as_empty_list(monkeypatch):
    s = _settings(monkeypatch, ADMIN_USER_IDENTIFIERS="")
    assert s.admin_user_identifiers == []


def test_whitespace_only_parses_as_empty_list(monkeypatch):
    s = _settings(monkeypatch, ALLOWED_LOGINS="   ")
    assert s.allowed_logins == []


def test_valid_json_array_parses(monkeypatch):
    s = _settings(monkeypatch, ALLOWED_LOGINS='["jeffhoek","alice"]')
    assert s.allowed_logins == ["jeffhoek", "alice"]


def test_unset_uses_default_empty_list(monkeypatch):
    s = _settings(monkeypatch)
    assert s.admin_user_identifiers == []


def test_invalid_json_still_fails_fast(monkeypatch):
    with pytest.raises(ValidationError):
        _settings(monkeypatch, ADMIN_USER_IDENTIFIERS="not-json")
