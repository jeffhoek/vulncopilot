from scripts.run_etl import _fmt_duration, build_email, run_pipeline

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


def test_fmt_duration():
    assert _fmt_duration(0) == "0m00s"
    assert _fmt_duration(65) == "1m05s"
    assert _fmt_duration(3661) == "61m01s"
