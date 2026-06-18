"""Unit tests for nvd_get_with_backoff — NVD's transient 403/429/5xx retry logic.

Uses httpx.MockTransport to script responses; asyncio.sleep is patched out so the
backoff waits don't slow the suite, and recorded delays are asserted instead.
"""

import httpx
import pytest

from scripts import nvd_utils
from scripts.nvd_utils import NVD_BACKOFF_CAP, nvd_get_with_backoff


@pytest.fixture
def no_sleep(monkeypatch):
    """Replace asyncio.sleep with a recorder so tests run instantly."""
    delays: list[float] = []

    async def fake_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(nvd_utils.asyncio, "sleep", fake_sleep)
    return delays


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_retries_503_then_returns_success(no_sleep):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    async with _client(handler) as client:
        resp = await nvd_get_with_backoff(client, "https://nvd.test/api", log=lambda *_: None)

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert calls["n"] == 3
    assert len(no_sleep) == 2  # backed off before each of the two retries


async def test_403_and_429_are_retried(no_sleep):
    statuses = iter([403, 429, 200])

    def handler(request):
        return httpx.Response(next(statuses))

    async with _client(handler) as client:
        resp = await nvd_get_with_backoff(client, "https://nvd.test/api", log=lambda *_: None)

    assert resp.status_code == 200
    assert len(no_sleep) == 2


async def test_404_returned_immediately_not_retried(no_sleep):
    def handler(request):
        return httpx.Response(404)

    async with _client(handler) as client:
        resp = await nvd_get_with_backoff(client, "https://nvd.test/api", log=lambda *_: None)

    assert resp.status_code == 404
    assert no_sleep == []  # 404 is not retryable


async def test_returns_last_response_when_retries_exhausted(no_sleep):
    def handler(request):
        return httpx.Response(503)

    async with _client(handler) as client:
        resp = await nvd_get_with_backoff(client, "https://nvd.test/api", max_retries=4, log=lambda *_: None)

    assert resp.status_code == 503
    assert len(no_sleep) == 3  # max_retries - 1 backoffs, then the final response returned


async def test_retry_after_header_honored_and_capped(no_sleep):
    statuses = iter([503, 200])

    def handler(request):
        code = next(statuses)
        if code == 503:
            return httpx.Response(503, headers={"Retry-After": "9999"})
        return httpx.Response(200)

    async with _client(handler) as client:
        resp = await nvd_get_with_backoff(client, "https://nvd.test/api", log=lambda *_: None)

    assert resp.status_code == 200
    assert no_sleep == [NVD_BACKOFF_CAP]  # 9999s clamped to the cap


async def test_transport_error_retried_then_raised(no_sleep):
    def handler(request):
        raise httpx.ConnectError("boom")

    async with _client(handler) as client:
        with pytest.raises(httpx.ConnectError):
            await nvd_get_with_backoff(client, "https://nvd.test/api", max_retries=3, log=lambda *_: None)

    assert len(no_sleep) == 2  # retried twice, raised on the final attempt
