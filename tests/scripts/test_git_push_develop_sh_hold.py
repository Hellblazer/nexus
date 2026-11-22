# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/git-push-develop.sh as the first caller of ``nx ci record-push`` and ``nx ci check-hold`` (RDR-252, nexus-igea9).

The 2026-11-22 post-close audit found (S3) that a skipped or failed push record was one stderr line nobody read (2 of 9
pushes in an evening), and (hold) that "Hold your pushes" was display only. The logic now lives in nx, so any repo's push
script can call it (tests/test_ci_record_push.py and tests/test_ci_push_gate.py test the commands); this script passes the
board base (``nexus-develop``), reports the status line of the record in its push summary, and holds a push the way
``nx ci check-hold`` says: PUSH_REFUSED_DEVELOP_RED, exit 13, unless ``--red-fix '<reason>'``. A push that overrode the
hold hands its reason to ``nx ci record-push``, which writes it into the record body.

Driven like test_git_push_develop_sh_record.py: a real bare origin and clone, an ``nx`` stand-in on PATH that logs argv and
prints what the real commands print.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REC_NAME = "_git_push_develop_record_helpers"
if _REC_NAME not in sys.modules:
    _spec = importlib.util.spec_from_file_location(_REC_NAME, Path(__file__).with_name("test_git_push_develop_sh_record.py"))
    assert _spec and _spec.loader
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[_REC_NAME] = _mod
    _spec.loader.exec_module(_mod)
_rec = sys.modules[_REC_NAME]

repos = _rec.repos  # fixtures, re-exposed
fake_nx = _rec.fake_nx
_git, _commit, _env, _run, _calls, _records, _opt = (
    _rec._git, _rec._commit, _rec._env, _rec._run, _rec._calls, _rec._records, _rec._opt)

RED = "PUSH_REFUSED_DEVELOP_RED"
RED_TIP = "a" * 40
REFUSAL = (f"{RED} nexus-develop is red at {RED_TIP} and owned by i9 (linda); this push (cccccccccccc) is from neither the owner "
           "nor the pusher of one of its suspects.\nHold your pushes until the red clears, or hand the fix to the owner. "
           "A push that is the fix: --red-fix '<reason>', on the record.")
OVERRIDE = f"PUSH_RED_FIX reason=owner away (nexus-develop is red at {RED_TIP}, owned by i9 (linda); the hold was overridden)"
NOT_NEEDED = "PUSH_RED_FIX reason=owner away (not needed: no open red)"


def _gate_calls(log: Path) -> list[list[str]]:
    return [c for c in _calls(log) if c[:2] == ["ci", "check-hold"]]


def _origin_develop(origin: Path) -> str:
    return _git("rev-parse", "refs/heads/develop", cwd=origin)


# ── S3: a skipped or failed record is in the stdout push summary ─────────────────────────────────

def test_a_record_that_fails_is_reported_on_stdout_and_stderr_and_the_push_stands(repos, fake_nx) -> None:
    origin, work = repos
    nx_dir, log = fake_nx
    a = _commit(work, "a")
    proc = _run(work, a, nx_dir=nx_dir, log=log, env={"FAKE_NX_RECORD_EXIT": "1", "FAKE_NX_RECORD_OUT": "Error: record write failed"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _origin_develop(origin) == a
    out = proc.stdout.splitlines()
    assert out[0] == f"PUSH_OK n=1 tip={a}"
    failed = [ln for ln in out if ln.startswith("PUSH_RECORD_FAILED")]
    assert len(failed) == 1 and "record write failed" in failed[0], proc.stdout
    assert any(ln.startswith("PUSH_RECORD_FAILED") for ln in proc.stderr.splitlines()), proc.stderr


def test_an_nx_with_no_record_push_command_is_a_failed_record_not_a_failed_push(repos, fake_nx) -> None:
    """An installed nx that predates the command exits 2 with a usage error: reported, and the push stands."""
    origin, work = repos
    nx_dir, log = fake_nx
    a = _commit(work, "a")
    proc = _run(work, a, nx_dir=nx_dir, log=log, env={"FAKE_NX_RECORD_EXIT": "2", "FAKE_NX_RECORD_OUT": "Error: No such command 'record-push'."})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _origin_develop(origin) == a
    assert any(ln.startswith("PUSH_RECORD_FAILED exit 2") and "No such command" in ln for ln in proc.stdout.splitlines()), proc.stdout


def test_a_fallback_status_from_the_command_is_in_the_summary(repos, fake_nx) -> None:
    _origin, work = repos
    nx_dir, log = fake_nx
    status = "PUSH_RECORD_FALLBACK the stamped write failed (ConnectionError: refused); an unstamped record with base, tip and from=i5 was written"
    proc = _run(work, _commit(work, "a"), nx_dir=nx_dir, log=log, env={"FAKE_NX_RECORD_STATUS": status})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert status in proc.stdout.splitlines() and status in proc.stderr.splitlines()


def test_a_failed_status_from_the_command_is_in_the_summary_even_at_exit_0(repos, fake_nx) -> None:
    """The command exits 0 whatever it wrote (the push has landed); its status line carries the news."""
    _origin, work = repos
    nx_dir, log = fake_nx
    status = "PUSH_RECORD_FAILED stamped refused; the minimal unstamped retry failed too (the push stands, no record was written)"
    proc = _run(work, _commit(work, "a"), nx_dir=nx_dir, log=log, env={"FAKE_NX_RECORD_STATUS": status})
    assert proc.returncode == 0 and status in proc.stdout.splitlines(), proc.stdout


def test_a_record_skipped_for_want_of_an_installed_nx_is_reported_on_stdout(repos, tmp_path) -> None:
    origin, work = repos
    log = tmp_path / "nx-calls.log"
    a = _commit(work, "a")
    proc = _run(work, "--skip-lock", "hold test", a, nx_dir=None, log=log)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert any(ln.startswith("PUSH_RECORD_SKIPPED") for ln in proc.stdout.splitlines()), proc.stdout
    assert any(ln.startswith("PUSH_RECORD_SKIPPED") for ln in proc.stderr.splitlines()), proc.stderr
    assert _origin_develop(origin) == a


def test_a_clean_push_has_exactly_the_push_ok_line_on_stdout(repos, fake_nx) -> None:
    _origin, work = repos
    nx_dir, log = fake_nx
    a = _commit(work, "a")
    proc = _run(work, a, nx_dir=nx_dir, log=log)
    assert proc.stdout == f"PUSH_OK n=1 tip={a}\n", proc.stdout


def test_the_record_command_gets_the_nexus_base_and_the_push_shas(repos, fake_nx) -> None:
    _origin, work = repos
    nx_dir, log = fake_nx
    before = _git("rev-parse", "HEAD", cwd=work)
    a, b = _commit(work, "a"), _commit(work, "b")
    assert _run(work, a, b, nx_dir=nx_dir, log=log).returncode == 0
    (record,) = _records(log)
    assert _opt(record, "--base") == ["nexus-develop"] and _opt(record, "--from-sha") == [before]
    assert _opt(record, "--to-sha") == [b] and _opt(record, "--count") == ["2"] and "--red-fix" not in record


# ── A2: hold your pushes ─────────────────────────────────────────────────────────────────────────

def test_a_push_while_develop_is_red_and_not_yours_is_refused_with_the_commands_lines(repos, fake_nx) -> None:
    origin, work = repos
    nx_dir, log = fake_nx
    before = _origin_develop(origin)
    proc = _run(work, _commit(work, "a"), nx_dir=nx_dir, log=log, env={"FAKE_NX_GATE_OUT": REFUSAL, "FAKE_NX_GATE_EXIT": "13"})
    assert proc.returncode == 13, proc.stdout + proc.stderr
    refusal = [ln for ln in proc.stdout.splitlines() if ln.startswith(RED)]
    assert len(refusal) == 1 and "i9 (linda)" in refusal[0] and RED_TIP in refusal[0], proc.stdout
    assert "--red-fix" in proc.stdout and "PUSH_OK" not in proc.stdout
    assert _origin_develop(origin) == before, "the refused push must not land"
    assert _records(log) == [] and ["tuple", "in"] not in [c[:2] for c in _calls(log)], "refused before the lock and the record"


def test_an_allowed_push_goes_through_and_the_hold_is_asked_once_with_the_base_and_tip(repos, fake_nx) -> None:
    origin, work = repos
    nx_dir, log = fake_nx
    a = _commit(work, "a")
    proc = _run(work, a, nx_dir=nx_dir, log=log)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _origin_develop(origin) == a
    (gate,) = _gate_calls(log)
    assert _opt(gate, "--base") == ["nexus-develop"] and _opt(gate, "--tip") == [a] and "--red-fix" not in gate
    assert RED not in proc.stdout and "PUSH_GATE_UNCHECKED" not in proc.stderr


def test_the_hold_is_asked_after_the_scope_audit_and_before_the_lock(repos, fake_nx) -> None:
    _origin, work = repos
    nx_dir, log = fake_nx
    proc = _run(work, _commit(work, "a"), nx_dir=nx_dir, log=log)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    kinds = [c[:2] for c in _calls(log)]
    assert kinds.index(["ci", "check-hold"]) < kinds.index(["tuple", "in"]) < kinds.index(["ci", "record-push"])


def test_red_fix_is_handed_to_the_command_and_an_override_puts_its_reason_in_the_record(repos, fake_nx) -> None:
    origin, work = repos
    nx_dir, log = fake_nx
    a = _commit(work, "a")
    proc = _run(work, "--red-fix", "owner away", a, nx_dir=nx_dir, log=log, env={"FAKE_NX_GATE_OUT": OVERRIDE})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _origin_develop(origin) == a
    lines = proc.stdout.splitlines()
    assert lines[0] == OVERRIDE and f"PUSH_OK n=1 tip={a}" in lines
    (gate,) = _gate_calls(log)
    assert _opt(gate, "--red-fix") == ["owner away"]
    (record,) = _records(log)
    assert _opt(record, "--red-fix") == ["owner away"], "every escape is in the push record body"


def test_a_red_fix_that_was_not_needed_is_not_written_into_the_record(repos, fake_nx) -> None:
    _origin, work = repos
    nx_dir, log = fake_nx
    proc = _run(work, "--red-fix", "owner away", _commit(work, "a"), nx_dir=nx_dir, log=log, env={"FAKE_NX_GATE_OUT": NOT_NEEDED})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert NOT_NEEDED in proc.stdout.splitlines()
    (record,) = _records(log)
    assert "--red-fix" not in record


def test_a_refusal_even_with_red_fix_is_the_commands_call(repos, fake_nx) -> None:
    """The script does not decide: a command that still refuses (exit 13) holds the push, whatever flag was passed."""
    origin, work = repos
    nx_dir, log = fake_nx
    before = _origin_develop(origin)
    proc = _run(work, "--red-fix", "x", _commit(work, "a"), nx_dir=nx_dir, log=log, env={"FAKE_NX_GATE_OUT": REFUSAL, "FAKE_NX_GATE_EXIT": "13"})
    assert proc.returncode == 13 and _origin_develop(origin) == before


def test_red_fix_without_a_reason_is_a_usage_error(repos, fake_nx) -> None:
    origin, work = repos
    nx_dir, log = fake_nx
    before = _origin_develop(origin)
    proc = _run(work, _commit(work, "a"), "--red-fix", nx_dir=nx_dir, log=log)
    assert proc.returncode == 64 and "PUSH_REFUSED_USAGE" in proc.stdout
    assert _origin_develop(origin) == before


def test_an_unchecked_warning_from_the_command_goes_to_stderr_and_the_push_goes_on(repos, fake_nx) -> None:
    origin, work = repos
    nx_dir, log = fake_nx
    a = _commit(work, "a")
    warn = "PUSH_GATE_UNCHECKED could not read board/ci/nexus-develop--owner: ConnectionError: engine down"
    proc = _run(work, a, nx_dir=nx_dir, log=log, env={"FAKE_NX_GATE_OUT": warn})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _origin_develop(origin) == a
    assert warn in proc.stderr.splitlines() and warn not in proc.stdout


@pytest.mark.parametrize(("env", "needle"), [
    ({"FAKE_NX_GATE_EXIT": "1", "FAKE_NX_GATE_OUT": "Traceback: boom"}, "exit 1"),
    ({"FAKE_NX_GATE_EXIT": "2", "FAKE_NX_GATE_OUT": "Error: No such command 'check-hold'."}, "No such command"),
    ({"FAKE_NX_GATE_SLEEP": "30", "NX_PUSH_GATE_TIMEOUT_S": "1"}, "ran past 1s"),
], ids=["crash", "old-nx", "hung"])
def test_a_check_that_cannot_run_warns_on_one_line_and_does_not_refuse(repos, fake_nx, env, needle) -> None:
    origin, work = repos
    nx_dir, log = fake_nx
    a = _commit(work, "a")
    proc = _run(work, a, nx_dir=nx_dir, log=log, env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _origin_develop(origin) == a
    warn = [ln for ln in proc.stderr.splitlines() if ln.startswith("PUSH_GATE_UNCHECKED")]
    assert len(warn) == 1 and needle in warn[0], proc.stderr
    assert RED not in proc.stdout


def test_no_installed_nx_warns_and_pushes_without_the_check(repos, tmp_path) -> None:
    origin, work = repos
    log = tmp_path / "nx-calls.log"
    a = _commit(work, "a")
    proc = _run(work, "--skip-lock", "hold test", a, nx_dir=None, log=log)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert any(ln.startswith("PUSH_GATE_UNCHECKED nx is not installed") for ln in proc.stderr.splitlines()), proc.stderr
    assert _origin_develop(origin) == a


def test_the_hold_applies_on_the_skip_lock_path_too(repos, fake_nx) -> None:
    origin, work = repos
    nx_dir, log = fake_nx
    before = _origin_develop(origin)
    proc = _run(work, "--skip-lock", "hold test", _commit(work, "a"), nx_dir=nx_dir, log=log,
                env={"FAKE_NX_GATE_OUT": REFUSAL, "FAKE_NX_GATE_EXIT": "13"})
    assert proc.returncode == 13 and RED in proc.stdout
    assert _origin_develop(origin) == before


def test_a_push_to_another_branch_is_never_held_and_writes_no_record(repos, fake_nx) -> None:
    origin, work = repos
    nx_dir, log = fake_nx
    _git("push", "-q", "origin", "develop:refs/heads/staging", cwd=work)
    _git("checkout", "-q", "-b", "staging", cwd=work)
    a = _commit(work, "a")
    proc = _run(work, a, nx_dir=nx_dir, log=log, env={"NX_PUSH_BRANCH": "staging", "FAKE_NX_GATE_OUT": REFUSAL, "FAKE_NX_GATE_EXIT": "13"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _gate_calls(log) == [] and _records(log) == []
    assert _git("rev-parse", "refs/heads/staging", cwd=origin) == a


def test_the_hold_and_record_temp_files_do_not_outlive_a_refusal(repos, fake_nx, tmp_path: Path) -> None:
    _origin, work = repos
    nx_dir, log = fake_nx
    tmpdir = tmp_path / "tmpdir"
    tmpdir.mkdir()
    from tests._tmp_root import child_env_with_tmpdir  # noqa: PLC0415
    a = _commit(work, "a")
    env = child_env_with_tmpdir(_env(work, nx_dir, log, {"FAKE_NX_GATE_OUT": REFUSAL, "FAKE_NX_GATE_EXIT": "13"}), tmpdir)
    proc = subprocess.run([str(_rec.SCRIPT), a], cwd=work, env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 13
    assert [p.name for p in tmpdir.iterdir() if p.name.startswith(("push-gate.", "push-record."))] == []


def test_the_pasteable_step_two_carries_red_fix(repos, fake_nx) -> None:
    """--red-fix is echoed with the other flags that change which push this is, so step two of the two-step push keeps it."""
    _origin, work = repos
    nx_dir, log = fake_nx
    a = _commit(work, "a")
    env = _env(work, nx_dir, log)
    env.pop("NX_PUSH_SKIP_SCOPE_AUDIT")
    proc = subprocess.run([str(_rec.SCRIPT), "--red-fix", "because", "--allowed-paths-from-range", a], cwd=work, env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 7, proc.stdout + proc.stderr
    assert "--red-fix because" in proc.stdout


def test_a_hung_record_command_is_stopped_and_the_push_stands(repos, fake_nx) -> None:
    origin, work = repos
    nx_dir, log = fake_nx
    a = _commit(work, "a")
    started = time.monotonic()
    proc = _run(work, a, nx_dir=nx_dir, log=log, env={"FAKE_NX_RECORD_SLEEP": "30", "NX_PUSH_RECORD_TIMEOUT_S": "1"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert time.monotonic() - started < 15
    assert _origin_develop(origin) == a
    assert any("ran past 1s" in ln for ln in proc.stdout.splitlines() if ln.startswith("PUSH_RECORD_FAILED")), proc.stdout
