"""Contract tests for McpRouterMiddleware auth enforcement.

Uses httpx.ASGITransport — no live server, no database.
set_mcp_context is never called so _mcp_context stays None throughout.
The FastMCP _mcp_asgi app is replaced with a trivial stub after construction
to avoid FastMCP lifespan side-effects in tests.
"""

import httpx
import pytest

import config
from mcp_server.server import McpRouterMiddleware

_API_KEY = "test-contract-key-abc123"


async def _inner_app(scope, receive, send):
    """Trivial inner app that always returns 200."""
    if scope["type"] == "http":
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"inner"})


async def _stub_mcp_app(scope, receive, send):
    """Stub MCP ASGI app that returns 200 for any HTTP request."""
    if scope["type"] == "http":
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"mcp"})


def _make_middleware(monkeypatch, api_key: str | None) -> McpRouterMiddleware:
    monkeypatch.setattr(config.settings, "mcp_api_key", api_key)
    mw = McpRouterMiddleware(_inner_app)
    mw._mcp_asgi = _stub_mcp_app
    return mw


@pytest.fixture
def middleware_with_key(monkeypatch) -> McpRouterMiddleware:
    return _make_middleware(monkeypatch, _API_KEY)


@pytest.fixture
def middleware_no_key(monkeypatch) -> McpRouterMiddleware:
    return _make_middleware(monkeypatch, None)


async def test_correct_api_key_passes_through(middleware_with_key):
    transport = httpx.ASGITransport(app=middleware_with_key)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/mcp", headers={"X-Api-Key": _API_KEY})
    assert response.status_code != 401


async def test_wrong_api_key_returns_401(middleware_with_key):
    transport = httpx.ASGITransport(app=middleware_with_key)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/mcp", headers={"X-Api-Key": "wrong-key"})
    assert response.status_code == 401


async def test_missing_api_key_returns_401(middleware_with_key):
    transport = httpx.ASGITransport(app=middleware_with_key)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/mcp")
    assert response.status_code == 401


async def test_non_mcp_path_bypasses_auth(middleware_with_key):
    transport = httpx.ASGITransport(app=middleware_with_key)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/other")
    assert response.status_code == 200


async def test_no_api_key_configured_allows_unauthenticated_mcp_access(middleware_no_key):
    transport = httpx.ASGITransport(app=middleware_no_key)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/mcp")
    assert response.status_code != 401
