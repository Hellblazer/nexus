# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-jypmj.16 (b): a persistently failing structured extraction is signalled once per process and in `nx doctor`.

A per-request extraction failure (no ``claude`` CLI and no API key, an ``openai_compat`` endpoint that answers
outside the closed schema, a timeout) leaves that request on its first-pass kind and the answer-kind gate keeps
enforcing: per-request fail-closed is the design. The first-pass-only regime is also the one that failed the
held-out lookup measurement, so a process in which the call fails every time was enforcing in it with no sign
anywhere. The process now warns once after a run of failures, and ``nx doctor`` carries an "Answer-kind
extraction" row that reads the same preconditions without a model call.

Tenant-free. The warning is pinned on the tracker and end to end through ``nx_answer``; the row follows
``tests/test_health_llm_backend.py`` (a box with nothing to run it on is not applicable, never red).
"""
from __future__ import annotations

import asyncio
import inspect
import os
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from nexus import health
from nexus.health import _check_answer_kind_extraction
from nexus.plans import eligibility as el
from nexus.plans import request_coverage as rc
from tests._module_seam import setattr_in
from tests.test_rdr232_eligibility import LOOKUP_473, SYNTH_12, _drive

EVENT = "answer_kind_gate_extraction_failing"
KEY = "sk-ant-api03-DOCTORSECRET0123456789abcdef"
FAILED = ("error", "timeout", "invalid")


def _warnings(logs) -> list[dict]:
    return [e for e in logs if e["event"] == EVENT]


def _note(outcomes, *, enforcing: bool = True) -> list[bool]:
    return [rc.note_extraction_outcome(o, gate_enforcing=enforcing) for o in outcomes]


# ── the process-level warning ────────────────────────────────────────────────────────────────────────────────


def test_the_threshold_is_a_run_not_a_single_failure() -> None:
    assert rc.PERSISTENT_FAILURE_THRESHOLD >= 2, "one bad call is per-request fail-closed, not persistence"
    assert set(rc.FAILED_OUTCOMES) == set(FAILED)


@pytest.mark.parametrize("outcome", FAILED)
def test_a_run_of_failures_warns_exactly_once_at_the_threshold(outcome: str) -> None:
    n = rc.PERSISTENT_FAILURE_THRESHOLD
    with capture_logs() as logs:
        fired = _note([outcome] * (n + 5))
    assert fired == [False] * (n - 1) + [True] + [False] * 5, "silent until the run is long enough, then once"
    (event,) = _warnings(logs)
    assert event["log_level"] == "warning" and event["consecutive_failures"] == n and event["last_outcome"] == outcome
    assert "first-pass" in event["consequence"] and "NX_ANSWER_STRUCTURED_EXTRACT" in event["remedy"]
    assert "nx doctor" in event["remedy"]


def test_a_success_ends_the_run() -> None:
    n = rc.PERSISTENT_FAILURE_THRESHOLD
    with capture_logs() as logs:
        for ok in ("ok", "unchanged"):
            rc.reset_extraction_health()
            _note(["error"] * (n - 1) + [ok] + ["error"] * (n - 1))
    assert _warnings(logs) == [], "failures that never reach the threshold in a row do not warn"


@pytest.mark.parametrize("neutral", ["not_needed", "disabled", "skipped_deadline"])
def test_a_request_that_made_no_call_neither_counts_nor_breaks_the_run(neutral: str) -> None:
    n = rc.PERSISTENT_FAILURE_THRESHOLD
    with capture_logs() as logs:
        fired = _note(["error"] * (n - 1) + [neutral, neutral] + ["error"])
    assert fired[-1] is True and len(_warnings(logs)) == 1


def test_a_gate_that_is_not_enforcing_does_not_warn_and_does_not_use_up_the_warning() -> None:
    n = rc.PERSISTENT_FAILURE_THRESHOLD
    with capture_logs() as logs:
        assert not any(_note(["error"] * (n + 2), enforcing=False))
        assert _warnings(logs) == [], "shadow mode loses nothing to a failing extraction"
        assert _note(["error"], enforcing=True) == [True], "and the first enforcing request after the run does warn"
    assert len(_warnings(logs)) == 1


def test_the_warning_is_once_per_streak_and_a_recovery_re_arms_it() -> None:
    """nexus-jypmj.21: fail x3 warns, ok, fail x3 warns again; a streak that never recovers warns only once."""
    n = rc.PERSISTENT_FAILURE_THRESHOLD
    with capture_logs() as logs:
        assert _note(["error"] * n).count(True) == 1
        assert len(_warnings(logs)) == 1
        assert _note(["error"] * 4) == [False] * 4, "the same streak does not warn again"
        assert _note(["ok"]) == [False]
        assert len(_warnings(logs)) == 1, "a recovery logs nothing by itself"
        assert _note(["error"] * (n - 1)) == [False] * (n - 1), "and the new streak counts from zero"
        assert _note(["error"]) == [True]
    assert len(_warnings(logs)) == 2 and all(e["consecutive_failures"] == n for e in _warnings(logs))


@pytest.mark.parametrize("neutral", ["not_needed", "disabled", "skipped_deadline"])
def test_a_request_that_made_no_call_does_not_re_arm_the_warning(neutral: str) -> None:
    n = rc.PERSISTENT_FAILURE_THRESHOLD
    with capture_logs() as logs:
        _note(["error"] * n + [neutral] + ["error"] * n)
    assert len(_warnings(logs)) == 1, "only a call that worked is a recovery"


def test_the_reset_hook_starts_the_process_over() -> None:
    n = rc.PERSISTENT_FAILURE_THRESHOLD
    with capture_logs() as logs:
        _note(["error"] * n)
        rc.reset_extraction_health()
        _note(["error"] * n)
    assert len(_warnings(logs)) == 2


def test_an_unknown_outcome_is_ignored_not_counted() -> None:
    with capture_logs() as logs:
        assert _note(["surprise"] * (rc.PERSISTENT_FAILURE_THRESHOLD + 2)) == [False] * (rc.PERSISTENT_FAILURE_THRESHOLD + 2)
    assert _warnings(logs) == []


def test_nx_answer_warns_once_after_repeated_failed_extractions() -> None:
    """End to end: the suite-wide guard makes every extraction call fail (outcome ``error``), the gate enforces."""
    n = rc.PERSISTENT_FAILURE_THRESHOLD
    with capture_logs() as logs:
        for i in range(n + 2):
            env, _, _ = _drive("Tell me about plan matching.", [LOOKUP_473, SYNTH_12], [(473, 0.1), (12, 0.4)])
            assert env["request_coverage"]["extraction"]["outcome"] == "error"
            assert env["plan_eligibility"]["enforced"] is True
            assert len(_warnings(logs)) == (1 if i + 1 >= n else 0), f"after request {i + 1}"
    (event,) = _warnings(logs)
    assert event["consecutive_failures"] == n


def test_nx_answer_does_not_warn_when_extraction_is_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(rc.EXTRACT_ENV, "0")
    with capture_logs() as logs:
        for _ in range(rc.PERSISTENT_FAILURE_THRESHOLD + 2):
            _drive("Tell me about plan matching.", [LOOKUP_473, SYNTH_12], [(473, 0.1), (12, 0.4)])
    assert _warnings(logs) == []


def test_the_suite_wide_fixture_resets_the_tracker() -> None:
    """A test starts with no failures counted and the warning unspent (tests/conftest.py)."""
    with capture_logs() as logs:
        _note(["error"] * rc.PERSISTENT_FAILURE_THRESHOLD)
    assert len(_warnings(logs)) == 1, "an earlier test's run of failures did not spend this test's warning"


# ── the doctor row ───────────────────────────────────────────────────────────────────────────────────────────


def _write_global_config(text: str) -> None:
    cfg_dir = Path(os.environ["NEXUS_CONFIG_DIR"])
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yml").write_text(text, encoding="utf-8")


@pytest.fixture
def no_claude(monkeypatch):
    setattr_in(monkeypatch, "nexus.operators.llm_backend", "shutil.which", lambda name: None)
    setattr_in(monkeypatch, "nexus.health", "shutil.which", lambda name: None)


@pytest.fixture
def claude(monkeypatch):
    setattr_in(monkeypatch, "nexus.operators.llm_backend", "shutil.which", lambda name: "/usr/bin/claude")
    setattr_in(monkeypatch, "nexus.health", "shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(health, "_claude_cli_problem", lambda: None)  # the path above is not a real binary


def _claude_stub(monkeypatch, tmp_path: Path, script: str) -> Path:
    """A real executable standing in for ``claude`` on PATH; the row's version probe runs it for real."""
    stub = tmp_path / "claude"
    stub.write_text("#!/bin/sh\n" + script, encoding="utf-8")
    stub.chmod(0o755)
    setattr_in(monkeypatch, "nexus.operators.llm_backend", "shutil.which", lambda name: str(stub))
    setattr_in(monkeypatch, "nexus.health", "shutil.which", lambda name: str(stub))
    return stub


LABEL = "Answer-kind extraction"


class TestAnswerKindExtractionRow:
    def test_a_virgin_box_is_not_applicable_not_red(self, no_claude) -> None:
        """No claude CLI and no key: nx_answer's LLM steps are off altogether (the LLM backend row says so);
        the fresh-install bar is zero warnings."""
        (row,) = _check_answer_kind_extraction()
        assert row.label == LABEL and row.ok and not row.warn and not row.fatal
        assert "not applicable" in row.detail and "no LLM backend" in row.detail

    @pytest.mark.parametrize("variable, value, mention", [
        (rc.EXTRACT_ENV, "0", "NX_ANSWER_STRUCTURED_EXTRACT"),
        (rc.EXTRACT_ENV, "off", "NX_ANSWER_STRUCTURED_EXTRACT"),
        (el.ANSWER_KIND_ELIGIBILITY_ENV, "0", "NX_ANSWER_KIND_ELIGIBILITY"),
    ])
    def test_a_switch_that_takes_the_regime_away_is_not_applicable(self, claude, monkeypatch, variable, value, mention) -> None:
        monkeypatch.setenv(variable, value)
        (row,) = _check_answer_kind_extraction()
        assert row.ok and not row.warn and "not applicable" in row.detail and mention in row.detail

    def test_the_cli_runs_the_extraction_on_the_measured_model(self, claude, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
        (row,) = _check_answer_kind_extraction()
        assert row.ok and not row.warn
        assert "cli" in row.detail and rc.DEFAULT_EXTRACT_MODEL in row.detail and KEY not in row.detail

    def test_a_healthy_cli_says_what_the_row_did_not_check(self, claude) -> None:
        """nexus-jypmj.21: green must not claim more than it looked at."""
        (row,) = _check_answer_kind_extraction()
        assert row.ok and "no model call" in row.detail and "answer_kind_gate_extraction_failing" in row.detail

    def test_a_cli_that_starts_reads_green(self, monkeypatch, tmp_path) -> None:
        _claude_stub(monkeypatch, tmp_path, "echo '2.1.0 (Claude Code)'\n")
        (row,) = _check_answer_kind_extraction()
        assert row.ok and not row.warn and "claude --version ran" in row.detail

    @pytest.mark.parametrize("script, mention", [
        ("echo 'node: command not found' >&2\nexit 127\n", "exited 127"),
        ("exit 1\n", "exited 1"),
    ])
    def test_a_present_but_broken_cli_warns(self, monkeypatch, tmp_path, script, mention) -> None:
        """nexus-jypmj.21: claude is on PATH but cannot run; PATH presence alone used to read green."""
        _claude_stub(monkeypatch, tmp_path, script)
        (row,) = _check_answer_kind_extraction()
        assert not row.ok and row.warn and not row.fatal
        assert mention in row.detail and "--version" in row.detail and "first-pass" in row.detail
        assert any("NX_ANSWER_STRUCTURED_EXTRACT" in f for f in row.fix_suggestions)

    def test_a_cli_that_hangs_warns_within_the_bound(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(health, "_CLAUDE_VERSION_TIMEOUT_S", 0.3)
        _claude_stub(monkeypatch, tmp_path, "sleep 30\n")
        (row,) = _check_answer_kind_extraction()
        assert not row.ok and row.warn and "did not finish" in row.detail

    def test_a_cli_file_that_cannot_execute_warns(self, monkeypatch, tmp_path) -> None:
        stub = _claude_stub(monkeypatch, tmp_path, "exit 0\n")
        stub.chmod(0o644)
        (row,) = _check_answer_kind_extraction()
        assert not row.ok and row.warn and "could not start" in row.detail

    def test_the_api_backend_runs_it_too(self, no_claude, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
        (row,) = _check_answer_kind_extraction()
        assert row.ok and not row.warn and row.detail.startswith("runs on anthropic") and KEY not in row.detail

    def test_a_configured_model_is_named(self, claude, monkeypatch) -> None:
        monkeypatch.setenv(rc.EXTRACT_MODEL_ENV, "sonnet")
        (row,) = _check_answer_kind_extraction()
        assert row.ok and "sonnet" in row.detail

    def test_openai_compat_warns_that_the_model_is_not_the_measured_one(self, no_claude, monkeypatch) -> None:
        monkeypatch.setenv("NX_LLM_BACKEND", "openai_compat")
        monkeypatch.setenv("NX_LLM_OPENAI_BASE_URL", "http://gpu-box:8000")
        monkeypatch.setenv("NX_LLM_OPENAI_MODEL", "qwen2.5")
        monkeypatch.setenv("NX_LLM_OPENAI_API_KEY", "oa-secret-key-value")
        (row,) = _check_answer_kind_extraction()
        assert not row.ok and row.warn and not row.fatal
        assert "qwen2.5" in row.detail and "oa-secret-key-value" not in row.detail + "".join(row.fix_suggestions)
        assert "first-pass" in row.detail
        assert any("NX_ANSWER_STRUCTURED_EXTRACT" in f for f in row.fix_suggestions)

    def test_a_backend_setting_that_cannot_serve_warns_with_the_reason(self, claude, monkeypatch) -> None:
        monkeypatch.setenv("NX_LLM_BACKEND", "anthropic")  # no key
        (row,) = _check_answer_kind_extraction()
        assert not row.ok and row.warn and not row.fatal
        assert "anthropic_api_key" in row.detail and "first-pass" in row.detail

    def test_a_broken_probe_is_a_warning_not_a_crash(self, monkeypatch) -> None:
        def boom(*a, **k):
            raise RuntimeError("probe broke")

        monkeypatch.setattr("nexus.operators.llm_backend.backend_status", boom)
        (row,) = _check_answer_kind_extraction()
        assert not row.ok and row.warn and "probe broke" in row.detail

    def test_a_backend_in_the_global_config_counts(self, no_claude) -> None:
        _write_global_config("credentials:\n  anthropic_api_key: " + KEY + "\n")
        (row,) = _check_answer_kind_extraction()
        assert row.ok and not row.warn and KEY not in row.detail


def test_the_row_is_wired_into_run_health_checks() -> None:
    assert "_check_answer_kind_extraction()" in inspect.getsource(health.run_health_checks)


def test_the_row_runs_no_model_call_and_no_dispatch(monkeypatch, claude) -> None:
    async def never(*a, **k):  # pragma: no cover - the failure path
        raise AssertionError("the doctor row must not dispatch")

    monkeypatch.setattr("nexus.operators.dispatch.claude_dispatch", never)
    (row,) = _check_answer_kind_extraction()
    assert row.ok
    assert not asyncio.iscoroutinefunction(_check_answer_kind_extraction)
