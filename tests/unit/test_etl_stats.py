import json
from datetime import UTC, datetime
from decimal import Decimal

from rag.etl_stats import get_recent_runs, render_etl_stats_html


class _FakePool:
    """Captures the query + args asyncpg.Pool.fetch would receive, returns canned rows."""

    def __init__(self, rows):
        self._rows = rows
        self.calls = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self._rows


def _row(run_at, status, total_elapsed, results):
    # Mirror asyncpg: a JSONB column comes back as a JSON *string* unless a codec is set.
    return {
        "run_at": run_at,
        "status": status,
        "total_elapsed": Decimal(str(total_elapsed)),
        "results": json.dumps(results),
    }


# -- get_recent_runs --------------------------------------------------------


async def test_get_recent_runs_orders_newest_first_and_passes_limit():
    pool = _FakePool([])
    await get_recent_runs(pool, limit=25)

    sql, args = pool.calls[0]
    assert "ORDER BY run_at DESC" in sql
    assert args == (25,)


async def test_get_recent_runs_parses_jsonb_and_numeric():
    results = [{"ok": True, "label": "KEV catalog", "summary": "Loaded 1619 KEV records"}]
    pool = _FakePool([_row(datetime(2026, 6, 14, 18, 3, tzinfo=UTC), "SUCCESS", "173.88", results)])

    runs = await get_recent_runs(pool)

    assert runs[0]["results"] == results  # JSON string decoded to a list
    assert runs[0]["total_elapsed"] == 173.88  # Decimal coerced to float


# -- render_etl_stats_html --------------------------------------------------


def _run(results, status="SUCCESS"):
    return {
        "run_at": datetime(2026, 6, 14, 18, 3, tzinfo=UTC),
        "status": status,
        "total_elapsed": 173.88,
        "results": results,
    }


def test_render_empty_history_shows_graceful_state():
    html = render_etl_stats_html([])

    assert "No ETL runs recorded yet." in html
    assert "<table" not in html


def test_render_does_not_leak_raw_error_text():
    """Public-exposure hardening: the raw `error` exception string is never shown."""
    raw_error = "OperationalError: could not connect to /secret/path:5432"
    runs = [
        _run(
            [{"ok": False, "label": "KEV catalog", "summary": "", "error": raw_error}],
            status="FAILED",
        )
    ]

    html = render_etl_stats_html(runs)

    assert raw_error not in html
    assert "/secret/path" not in html
    assert "failed" in html  # generic note shown instead


def test_render_escapes_stored_text():
    """Anything stored in the DB is autoescaped — no HTML injection from a summary."""
    runs = [_run([{"ok": True, "label": "KEV catalog", "summary": "<script>alert(1)</script>"}])]

    html = render_etl_stats_html(runs)

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_render_shows_status_and_loader_summary():
    runs = [_run([{"ok": True, "label": "KEV catalog", "summary": "Loaded 1619 KEV records"}])]

    html = render_etl_stats_html(runs)

    assert "SUCCESS" in html
    assert "KEV catalog" in html
    assert "Loaded 1619 KEV records" in html


# -- route wiring -----------------------------------------------------------


def test_etl_stats_route_is_not_shadowed_by_chainlit_catchall(monkeypatch):
    """Regression: the public route must win over Chainlit's "/{full_path:path}"
    SPA catch-all, otherwise the frontend is served and the client redirects to "/"."""
    from starlette.testclient import TestClient

    import app

    async def fake_init_db():
        return object()

    async def fake_get_recent_runs(_pool, limit=50):
        return []

    monkeypatch.setattr(app, "init_db", fake_init_db)
    monkeypatch.setattr(app, "get_recent_runs", fake_get_recent_runs)

    client = TestClient(app.fastapi_app, follow_redirects=False)
    resp = client.get("/etl-stats")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "<title>ETL run history</title>" in resp.text
