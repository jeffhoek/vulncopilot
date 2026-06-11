from scripts.run_etl import _fmt_duration, build_email, run_pipeline

STEPS = [("step-a", ["a"]), ("step-b", ["b"]), ("step-c", ["c"])]


def _result(label: str, ok: bool) -> dict:
    return {"label": label, "ok": ok, "returncode": 0 if ok else 1, "elapsed": 1.0, "highlights": []}


def test_run_pipeline_runs_all_steps_when_each_succeeds():
    ran = []

    def runner(label, argv):
        ran.append(label)
        return _result(label, ok=True)

    results = run_pipeline(STEPS, runner=runner)

    assert ran == ["step-a", "step-b", "step-c"]
    assert [r["label"] for r in results] == ["step-a", "step-b", "step-c"]


def test_run_pipeline_runs_every_step_even_when_one_fails():
    """The loaders are independent, so a failure in one must not skip the others."""
    ran = []

    def runner(label, argv):
        ran.append(label)
        return _result(label, ok=(label != "step-b"))

    results = run_pipeline(STEPS, runner=runner)

    assert ran == ["step-a", "step-b", "step-c"]  # nothing is skipped
    assert [r["ok"] for r in results] == [True, False, True]


def test_build_email_success_subject_and_body():
    subject, body = build_email([_result("KEV catalog", ok=True)], total_elapsed=65.0)

    assert subject.startswith("[SUCCESS] NVD/KEV ETL — ")
    assert "ETL run SUCCESS" in body
    assert "[OK  ] KEV catalog (0m01s, exit 0)" in body
    assert "(total 1m05s)" in body


def test_build_email_failed_subject_when_any_step_fails():
    subject, body = build_email([_result("a", ok=True), _result("b", ok=False)], total_elapsed=5.0)

    assert subject.startswith("[FAILED] ")
    assert "[FAIL] b (0m01s, exit 1)" in body


def test_build_email_includes_highlights():
    result = _result("NVD full incremental", ok=True) | {"highlights": ["Done! Synced 42 CVEs"]}
    _, body = build_email([result], total_elapsed=1.0)

    assert "        Done! Synced 42 CVEs" in body


def test_fmt_duration():
    assert _fmt_duration(0) == "0m00s"
    assert _fmt_duration(65) == "1m05s"
    assert _fmt_duration(3661) == "61m01s"
