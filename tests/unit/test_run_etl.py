import json

from scripts.run_etl import _fmt_duration, build_email, record_run, run_pipeline

STEPS = [("step-a", "m:a"), ("step-b", "m:b"), ("step-c", "m:c")]


def _result(label: str, ok: bool, summary: str = "", error: str | None = None) -> dict:
    return {"label": label, "ok": ok, "elapsed": 1.0, "summary": summary, "metrics": {}, "error": error}


def test_run_pipeline_runs_all_steps_when_each_succeeds():
    ran = []

    def runner(label, target):
        ran.append(label)
        return _result(label, ok=True)

    results = run_pipeline(STEPS, runner=runner)

    assert ran == ["step-a", "step-b", "step-c"]
    assert [r["label"] for r in results] == ["step-a", "step-b", "step-c"]


def test_run_pipeline_runs_every_step_even_when_one_fails():
    """The loaders are independent, so a failure in one must not skip the others."""
    ran = []

    def runner(label, target):
        ran.append(label)
        return _result(label, ok=(label != "step-b"))

    results = run_pipeline(STEPS, runner=runner)

    assert ran == ["step-a", "step-b", "step-c"]  # nothing is skipped
    assert [r["ok"] for r in results] == [True, False, True]


def test_build_email_success_renders_summary():
    subject, body = build_email(
        [_result("KEV catalog", ok=True, summary="Loaded 1617 KEV records")], total_elapsed=65.0
    )

    assert subject.startswith("[SUCCESS] NVD/KEV ETL — ")
    assert "ETL run SUCCESS" in body
    assert "[OK  ] KEV catalog (0m01s) — Loaded 1617 KEV records" in body
    assert "(total 1m05s)" in body


def test_build_email_failed_renders_error():
    subject, body = build_email(
        [_result("a", ok=True, summary="ok"), _result("b", ok=False, error="RuntimeError: boom")],
        total_elapsed=5.0,
    )

    assert subject.startswith("[FAILED] ")
    assert "[FAIL] b (0m01s) — RuntimeError: boom" in body


def test_build_email_failed_without_error_message_falls_back():
    _, body = build_email([_result("b", ok=False)], total_elapsed=1.0)

    assert "[FAIL] b (0m01s) — failed" in body


class _FakeConn:
    """Captures the INSERT and lets tests assert on the args asyncpg received."""

    def __init__(self, calls):
        self._calls = calls

    async def execute(self, sql, *args):
        self._calls.append((sql, args))

    async def close(self):
        pass


def _patch_connect(monkeypatch, conn):
    import asyncpg

    async def fake_connect(*args, **kwargs):
        return conn

    monkeypatch.setattr(asyncpg, "connect", fake_connect)


def test_record_run_inserts_success_status(monkeypatch):
    calls = []
    _patch_connect(monkeypatch, _FakeConn(calls))

    results = [_result("a", ok=True, summary="ok"), _result("b", ok=True, summary="ok")]
    record_run(results, total_elapsed=12.345)

    assert len(calls) == 1
    _sql, args = calls[0]
    status, total_elapsed, results_json = args
    assert status == "SUCCESS"
    assert total_elapsed == 12.35  # rounded to 2 dp
    assert json.loads(results_json) == results


def test_record_run_marks_failed_when_any_loader_fails(monkeypatch):
    calls = []
    _patch_connect(monkeypatch, _FakeConn(calls))

    record_run([_result("a", ok=True), _result("b", ok=False, error="boom")], total_elapsed=1.0)

    assert calls[0][1][0] == "FAILED"


def test_record_run_swallows_db_errors(monkeypatch, capsys):
    """Best-effort contract: a DB error must not propagate or change the exit code."""
    import asyncpg

    async def boom(*args, **kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(asyncpg, "connect", boom)

    record_run([_result("a", ok=True)], total_elapsed=1.0)  # must not raise

    assert "failed to record ETL run" in capsys.readouterr().out


def test_fmt_duration():
    assert _fmt_duration(0) == "0m00s"
    assert _fmt_duration(65) == "1m05s"
    assert _fmt_duration(3661) == "61m01s"
