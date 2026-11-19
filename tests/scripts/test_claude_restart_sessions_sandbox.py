# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""claude-restart-sessions' sandbox-pass gate (nexus-8zdbu), advisory since Sam's 2026-11-19 ruling (T2
nexus/sam-ruling-restart-sandbox-gate-2026-11-19, A1): a missing or stale pass, another script, tmux,
harness or Claude Code is a WARN line naming the problem, the last pass and the weekly run's result,
and the restart goes on. Moved out of test_claude_restart_sessions.py, which is over its size ceiling."""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests._module_seam import setattr_in

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "claude-restart-sessions"


@pytest.fixture(scope="module")
def crs():
    loader = importlib.machinery.SourceFileLoader("claude_restart_sessions", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _no_sent_asks(crs, monkeypatch, tmp_path_factory):
    """Each test starts with no mailed ask on record, so none is withdrawn through a real nx, and no
    unwithdrawn ask either; and the script's TERM/HUP handlers (exit_on_stop_signals) are never
    installed in the pytest process: a test that checks them patches signal.signal again itself."""
    monkeypatch.setattr(crs, "SENT_ASKS", {})
    monkeypatch.setattr(crs, "SENT_MARKERS", {})
    monkeypatch.setattr(crs, "MADE_MARKERS", set())
    monkeypatch.setattr(crs, "UNWITHDRAWN", [])
    monkeypatch.setattr(crs, "ASK_MARKERS", tmp_path_factory.mktemp("ask-markers") / "restart-asks")
    # Never this machine's managed settings, user settings or flags (the channel-swap gate reads them).
    settings = tmp_path_factory.mktemp("claude-settings")
    monkeypatch.setattr(crs, "MANAGED_SETTINGS", (settings / "managed" / "managed-settings.json",))
    monkeypatch.setattr(crs, "USER_SETTINGS", settings / "user-settings.json")
    monkeypatch.setattr(crs, "MARKETPLACES", settings / "marketplaces")
    monkeypatch.setattr(crs, "KNOWN_MARKETPLACES", settings / "known_marketplaces.json")
    monkeypatch.setattr(crs, "INSTALLED", settings / "installed_plugins.json")
    monkeypatch.setattr(crs, "KEEP_DEV_CHANNEL", False)
    # The exit-and-resume path; /restart has its own tests (test_claude_restart_sessions_slash.py).
    monkeypatch.setattr(crs, "NO_SLASH", True)
    # A roll under test must not prune this machine's real /tmp (nexus-2v7da); that prune is pinned in
    # test_claude_restart_sessions_prune.py.
    monkeypatch.setattr(crs, "_prune_handoffs_at_write", lambda: None)
    setattr_in(monkeypatch, crs, "signal.signal", lambda *_a: None)


# ── the sandbox-pass gate (nexus-8zdbu) ──────────────────────────────────────────────────

SHA = "a" * 64


@pytest.fixture
def passfile(crs, monkeypatch, tmp_path) -> Path:
    path = tmp_path / "state" / "sandbox-pass.json"
    monkeypatch.setattr(crs, "SANDBOX_PASS", path)
    monkeypatch.setattr(crs, "LOG", tmp_path / "restart.log")
    monkeypatch.setattr(crs, "tmux_version", lambda: "tmux 3.6a")
    monkeypatch.setattr(crs, "harness_sha256", lambda *_a: "h" * 64)
    return path


def test_a_recorded_pass_covers_its_script_and_claude_version(crs, passfile) -> None:
    crs.record_sandbox_pass(passfile, SHA, "2.1.316", ["roll", "variant", "abort", "gate"], 756.4)
    assert crs.sandbox_pass_check(SHA, "2.1.316") == ""
    rec = json.loads(passfile.read_text(encoding="utf-8"))
    assert rec["seconds"] == 756 and rec["scenarios"] == ["roll", "variant", "abort", "gate"]


@pytest.mark.parametrize(("script", "claude", "says"), [
    ("b" * 64, "2.1.316", "another copy of this script"),
    (SHA, "2.1.317", "Claude Code 2.1.316, not 2.1.317"),
    (SHA, "", "could not read Claude Code's version"),
], ids=["script-changed", "claude-changed", "claude-unreadable"])
def test_a_pass_on_anything_else_is_a_warning(crs, passfile, script: str, claude: str, says: str) -> None:
    crs.record_sandbox_pass(passfile, SHA, "2.1.316", ["roll"], 1)
    assert says in crs.sandbox_pass_check(script, claude)


@pytest.mark.parametrize(("content", "says"), [
    (None, "no sandbox pass recorded"),
    ("{not json", "unreadable"),
    ("[1]", "not an object"),
], ids=["absent", "garbled", "list"])
def test_a_missing_or_bad_record_is_a_warning(crs, passfile, content: str | None, says: str) -> None:
    if content is not None:
        passfile.parent.mkdir(parents=True)
        passfile.write_text(content, encoding="utf-8")
    assert says in crs.sandbox_pass_check(SHA, "2.1.316")


def test_script_sha256_is_the_hash_of_the_scripts_bytes_through_a_symlink(crs, tmp_path) -> None:
    expected = hashlib.sha256(SCRIPT.read_bytes()).hexdigest()
    link = tmp_path / "claude-restart-sessions"
    link.symlink_to(SCRIPT)
    assert crs.script_sha256(link) == expected
    assert crs.script_sha256() == expected  # with no path: the module's own file
    changed = tmp_path / "changed"
    changed.write_bytes(SCRIPT.read_bytes() + b"\n")
    assert crs.script_sha256(changed) != expected


def _main(crs, monkeypatch, *argv: str, converge=None) -> tuple[int, list[list[str]]]:
    """Run main with one idle session; return its exit code and the commands it ran."""
    ran: list[list[str]] = []
    monkeypatch.setattr(crs, "sessions", lambda: [{"pid": 1, "pane": "%1", "name": "s", "sessionId": "sid", "status": "idle", "argv": "claude", "cwd": "/w"}])
    monkeypatch.setattr(crs, "claude_version", lambda *_: "2.1.316")
    monkeypatch.setattr(crs, "script_sha256", lambda *_: SHA)
    monkeypatch.setattr(crs, "converge", converge or (lambda *_a, **_k: pytest.fail("converge ran past a refused gate")))
    monkeypatch.setattr(crs, "LAST_RUN", crs.SANDBOX_PASS.parent / "restart-last.json")  # never the operator's
    setattr_in(monkeypatch, crs, "subprocess.run", lambda cmd, *_a, **_k: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    # The ready round has its own tests; here it is skipped so only the sandbox gate decides.
    monkeypatch.setattr(sys, "argv", ["claude-restart-sessions", "--no-ready-check", *argv])
    return crs.main(), ran


def test_a_restart_with_no_pass_warns_and_goes_on(crs, monkeypatch, passfile, capsys) -> None:
    """A1: no pass record is a WARN line (log and stdout) that names the problem, the last pass and the weekly
    run's status; the restart goes on to the next gate (here the engine gate, which refuses its own way)."""
    code, ran = _main(crs, monkeypatch, "--nx", "7.93.1", converge=lambda *_a, **_k: False)
    assert code == 2  # converge's refusal, not the sandbox's exit 3
    log = crs.LOG.read_text(encoding="utf-8")
    assert "WARN: sandbox pass: no sandbox pass recorded" in log and "ABORT: sandbox pass" not in log
    assert "no weekly sandbox run has happened here" in log and "restarting anyway" in log
    out = capsys.readouterr().out
    assert "WARN: sandbox pass: no sandbox pass recorded" in out and "refused" not in out


def test_a_restart_with_a_pass_goes_on(crs, monkeypatch, passfile) -> None:
    crs.record_sandbox_pass(passfile, SHA, "2.1.316", ["roll"], 1)
    code, ran = _main(crs, monkeypatch)
    assert code == 0 and ran and ran[0][:3] == ["tmux", "run-shell", "-b"]


def test_skip_sandbox_check_is_accepted_and_only_logs_its_reason(crs, monkeypatch, passfile) -> None:
    code, ran = _main(crs, monkeypatch, "--skip-sandbox-check", "comment-only edit, rerun tonight")
    assert code == 0 and ran
    assert ("WARN: sandbox pass: no sandbox pass recorded" in (log := crs.LOG.read_text(encoding="utf-8"))
            and "(--skip-sandbox-check: comment-only edit, rerun tonight)" in log)
    # the reason rides into the detached roll's argv, which checks the gate itself
    assert "comment-only edit, rerun tonight" in ran[0][-1]


@pytest.mark.parametrize("argv", [["--skip-sandbox-check", " "], ["--skip-sandbox-check"], ["--skip-sandbox-check=  "],
                                  ["--skip-sandbox", " "], ["--skip-sandbox-ch=\t"], ["--skip-sandbox-check", "\u200b\x1b"],
                                  ["--revive", "--skip-sandbox-check", " "]],
                         ids=["blank", "bare", "equals-blank", "abbrev-blank", "abbrev-equals-tab", "non-printing",
                              "revive-blank"])
def test_skip_sandbox_check_needs_a_reason(crs, monkeypatch, passfile, argv) -> None:
    with pytest.raises(SystemExit) as exc:
        _main(crs, monkeypatch, *argv)
    assert exc.value.code == 2


def test_a_reason_cannot_forge_a_log_line(crs, monkeypatch, passfile) -> None:
    code, _ = _main(crs, monkeypatch, "--skip-sandbox-check", "tired\n12:00:00 RESTART SANDBOX PASSED")
    assert code == 0
    log = crs.LOG.read_text(encoding="utf-8")
    assert "(--skip-sandbox-check: tired 12:00:00 RESTART SANDBOX PASSED)" in log
    assert not any(ln.startswith("12:00:00 RESTART") for ln in log.splitlines())


def test_a_detached_roll_given_a_reason_logs_it_and_rolls(crs, monkeypatch, passfile) -> None:
    rolled: list = []
    monkeypatch.setattr(crs, "_roll", lambda *a, **k: rolled.append(1) or 0)
    monkeypatch.setattr(crs, "footprint_report", lambda *a, **k: [])
    code, _ = _main(crs, monkeypatch, "--detached", "--resume", "--no-update", "--skip-sandbox-check", "direct call")
    assert code == 0 and rolled == [1]
    assert "restarting anyway (detached, --skip-sandbox-check: direct call)" in crs.LOG.read_text(encoding="utf-8")


def test_a_copy_with_no_harness_says_so(crs, monkeypatch, passfile, tmp_path) -> None:
    crs.record_sandbox_pass(passfile, SHA, "2.1.316", ["roll"], 1)
    monkeypatch.setattr(crs, "harness_sha256", lambda *_a: "")
    assert "has no sandbox harness beside it" in crs.sandbox_pass_check(SHA, "2.1.316")


def test_harness_sha256_with_no_argument_reads_harness(crs, monkeypatch, tmp_path) -> None:
    import hashlib  # noqa: PLC0415 -- test-local
    fake = tmp_path / "run.py"
    fake.write_bytes(b"harness bytes")
    monkeypatch.setattr(crs, "HARNESS", fake)
    assert crs.harness_sha256() == hashlib.sha256(b"harness bytes").hexdigest()
    monkeypatch.setattr(crs, "HARNESS", tmp_path / "absent.py")
    assert crs.harness_sha256() == ""


def test_the_record_keeps_the_tmux_and_harness_it_is_given(crs, passfile) -> None:
    crs.record_sandbox_pass(passfile, SHA, "2.1.316", ["roll"], 1, tmux="tmux 9.9", harness="z" * 64)
    rec = json.loads(passfile.read_text(encoding="utf-8"))
    assert rec["tmux_version"] == "tmux 9.9" and rec["harness_sha256"] == "z" * 64


def test_the_detached_roll_warns_with_no_pass_and_rolls(crs, monkeypatch, passfile) -> None:
    """A1: the detached roll re-checks the pass for the log's sake, and a missing one does not stop it."""
    rolled: list = []
    monkeypatch.setattr(crs, "_roll", lambda *a, **k: rolled.append(1) or 0)
    monkeypatch.setattr(crs, "footprint_report", lambda *a, **k: [])
    code, _ = _main(crs, monkeypatch, "--detached", "--resume", "--no-update")
    assert code == 0 and rolled == [1]
    log = crs.LOG.read_text(encoding="utf-8")
    assert "WARN: sandbox pass: no sandbox pass recorded" in log and "(detached)" in log and "ABORT" not in log


@pytest.mark.parametrize(("patch", "says"), [
    ("tmux_version", "was on tmux 3.6a, not tmux 3.7"),
    ("harness_sha256", "another copy of the sandbox harness"),
])
def test_a_pass_on_another_tmux_or_harness_is_a_warning(crs, monkeypatch, passfile, patch, says) -> None:
    crs.record_sandbox_pass(passfile, SHA, "2.1.316", ["roll"], 1)
    assert crs.sandbox_pass_check(SHA, "2.1.316") == ""
    monkeypatch.setattr(crs, patch, (lambda: "tmux 3.7") if patch == "tmux_version" else (lambda *_a: "x" * 64))
    assert says in crs.sandbox_pass_check(SHA, "2.1.316")


def test_harness_sha256_is_the_run_py_beside_the_script(crs) -> None:
    import hashlib  # noqa: PLC0415 -- test-local
    assert crs.HARNESS == SCRIPT.parents[1] / "tests" / "e2e" / "restart-sessions" / "run.py"
    assert crs.harness_sha256(crs.HARNESS) == hashlib.sha256(crs.HARNESS.read_bytes()).hexdigest()


def test_a_dry_run_reports_the_gate_and_refuses_nothing(crs, monkeypatch, passfile, capsys) -> None:
    code, ran = _main(crs, monkeypatch, "--dry-run")
    assert code == 0 and ran == []
    assert "sandbox pass: warning, no sandbox pass recorded" in capsys.readouterr().out


def test_revive_is_not_gated(crs, monkeypatch, passfile) -> None:
    monkeypatch.setattr(crs, "revive", lambda _a: 0)
    code, _ = _main(crs, monkeypatch, "--revive")
    assert code == 0


# ── every cause warns, none refuses (Sam, 2026-11-18 for Claude Code; 2026-11-19 for the rest) ────

def test_a_pass_on_another_claude_code_is_a_warning_naming_both(crs, passfile) -> None:
    crs.record_sandbox_pass(passfile, SHA, "2.1.316", ["roll"], 1)
    warning = crs.sandbox_pass_check(SHA, "2.1.317")
    assert "Claude Code 2.1.316, not 2.1.317" in warning
    assert "no weekly sandbox run has happened here (restart-sandbox-weekly.sh --install sets one up)" in warning


@pytest.mark.parametrize(("record", "says"), [
    ({"status": "passed", "ended": "2026-11-22T03:47:00Z"}, "the last weekly sandbox run (2026-11-22T03:47:00Z) passed"),
    ({"status": "failed", "ended": "E", "reason": "harness exited 1", "claude_version": "2.1.317"},
     "(E on Claude Code 2.1.317) did not record a pass: failed, harness exited 1"),
    ({"status": "timeout", "ended": "E", "reason": "ran past 5400 s"}, "did not record a pass: timeout, ran past 5400 s"),
    ({"status": "not-run", "ended": "E", "reason": "TMPDIR missing"}, "did not record a pass: not-run, TMPDIR missing"),
], ids=["passed", "failed-with-version", "timeout", "not-run"])
def test_the_warning_names_the_weekly_runs_last_result(crs, passfile, record, says) -> None:
    crs.record_sandbox_pass(passfile, SHA, "2.1.316", ["roll"], 1)
    (passfile.parent / "sandbox-weekly-last.json").write_text(json.dumps(record), encoding="utf-8")
    assert says in crs.sandbox_pass_check(SHA, "2.1.317")


@pytest.mark.parametrize("value", [None, "", 2116], ids=["missing", "empty", "not-a-string"])
def test_a_record_that_names_no_claude_code_warns(crs, passfile, value) -> None:
    crs.record_sandbox_pass(passfile, SHA, "2.1.316", ["roll"], 1)
    rec = json.loads(passfile.read_text(encoding="utf-8"))
    if value is None:
        del rec["claude_version"]
    else:
        rec["claude_version"] = value
    passfile.write_text(json.dumps(rec), encoding="utf-8")
    assert "names no Claude Code version" in crs.sandbox_pass_check(SHA, "2.1.316")


@pytest.mark.parametrize(("script", "patch", "says"), [
    ("b" * 64, None, "another copy of this script"),
    (SHA, "tmux_version", "not tmux 3.7"),
    (SHA, "harness_sha256", "another copy of the sandbox harness"),
], ids=["script", "tmux", "harness"])
def test_another_script_tmux_or_harness_is_named_ahead_of_a_claude_code_change(crs, monkeypatch, passfile, script, patch, says) -> None:
    """With Claude Code AND something else changed, the warning names the something else."""
    crs.record_sandbox_pass(passfile, SHA, "2.1.316", ["roll"], 1)
    if patch:
        monkeypatch.setattr(crs, patch, (lambda: "tmux 3.7") if patch == "tmux_version" else (lambda *_a: "x" * 64))
    assert says in crs.sandbox_pass_check(script, "2.1.317")


@pytest.mark.parametrize("content", [None, "{not json"], ids=["absent", "garbled"])
def test_no_readable_pass_is_a_warning_with_the_weekly_status(crs, passfile, content) -> None:
    if content is not None:
        passfile.parent.mkdir(parents=True)
        passfile.write_text(content, encoding="utf-8")
    warning = crs.sandbox_pass_check(SHA, "2.1.316")
    assert warning and "weekly sandbox run" in warning


def test_a_restart_on_a_newer_claude_code_goes_on_with_a_warning(crs, monkeypatch, passfile, capsys) -> None:
    crs.record_sandbox_pass(passfile, SHA, "2.1.315", ["roll"], 1)  # _main runs Claude Code 2.1.316
    code, ran = _main(crs, monkeypatch)
    assert code == 0 and ran and ran[0][:3] == ["tmux", "run-shell", "-b"]
    log = crs.LOG.read_text(encoding="utf-8")
    assert "WARN: sandbox pass: the last sandbox pass" in log and "Claude Code 2.1.315, not 2.1.316" in log
    assert "ABORT" not in log
    out = capsys.readouterr().out
    assert out.count("WARN: sandbox pass:") == 1  # printed once, by log()


def test_a_detached_roll_on_a_newer_claude_code_warns_and_rolls(crs, monkeypatch, passfile) -> None:
    crs.record_sandbox_pass(passfile, SHA, "2.1.315", ["roll"], 1)
    rolled: list = []
    monkeypatch.setattr(crs, "_roll", lambda *a, **k: rolled.append(1) or 0)
    monkeypatch.setattr(crs, "footprint_report", lambda *a, **k: [])
    code, _ = _main(crs, monkeypatch, "--detached", "--resume", "--no-update")
    assert code == 0 and rolled == [1]
    assert "Claude Code 2.1.315, not 2.1.316" in crs.LOG.read_text(encoding="utf-8")


def test_a_dry_run_on_a_newer_claude_code_reports_the_warning(crs, monkeypatch, passfile, capsys) -> None:
    crs.record_sandbox_pass(passfile, SHA, "2.1.315", ["roll"], 1)
    code, ran = _main(crs, monkeypatch, "--dry-run")
    assert code == 0 and ran == []
    assert "sandbox pass: warning, the last sandbox pass" in capsys.readouterr().out


def test_skip_sandbox_check_is_still_accepted_on_a_newer_claude_code(crs, monkeypatch, passfile) -> None:
    crs.record_sandbox_pass(passfile, SHA, "2.1.315", ["roll"], 1)
    code, ran = _main(crs, monkeypatch, "--skip-sandbox-check", "habit")
    assert code == 0 and ran


def test_the_weekly_run_named_in_the_warning_is_beside_the_script(crs) -> None:
    assert crs.WEEKLY_RUN == SCRIPT.parent / "restart-sandbox-weekly.sh" and crs.WEEKLY_RUN.is_file()


def test_the_sandbox_warning_is_logged_before_the_ready_round_is_read(crs, monkeypatch, passfile, capsys) -> None:
    """No pass: the WARN line is in the log before the ready-round check runs, and the check still runs."""
    seen: list[str] = []
    monkeypatch.setattr(crs, "go_gate", lambda *_a, **_k: seen.append(crs.LOG.read_text(encoding="utf-8")) or 3)
    monkeypatch.setattr(crs, "sessions", lambda: [{"pid": 1, "pane": "%1", "name": "s", "sessionId": "sid", "status": "idle", "argv": "claude", "cwd": "/w"}])
    monkeypatch.setattr(crs, "claude_version", lambda *_: "2.1.316")
    monkeypatch.setattr(crs, "script_sha256", lambda *_: SHA)
    monkeypatch.setattr(crs, "LAST_RUN", passfile.parent / "restart-last.json")
    monkeypatch.setattr(sys, "argv", ["claude-restart-sessions", "--resume", "--no-update"])
    assert crs.main() == 3  # go_gate's own refusal
    assert len(seen) == 1 and "WARN: sandbox pass: no sandbox pass recorded" in seen[0]


def test_the_header_and_help_say_the_gate_is_advisory(crs, monkeypatch, capsys) -> None:
    doc = crs.__doc__ or ""
    assert "advisory" in doc and "restart-sandbox-weekly.sh" in doc and "tests/e2e/restart-sessions/run.sh" in doc
    assert "refuses with exit 3, before" not in doc.replace("\n", " ")
    monkeypatch.setattr(sys, "argv", ["claude-restart-sessions", "--help"])
    with pytest.raises(SystemExit):
        crs.main()
    help_text = " ".join(capsys.readouterr().out.split())
    assert "--skip-sandbox-check" in help_text and "only logs" in help_text
