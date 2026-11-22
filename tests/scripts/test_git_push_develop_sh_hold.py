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
import json
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
OVERRIDE = f"PUSH_RED_FIX_APPLIED owner away (nexus-develop is red at {RED_TIP}, owned by i9 (linda); the hold was overridden)"
NOT_NEEDED = "PUSH_RED_FIX_UNNEEDED owner away (not needed: no open red)"


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


NO_COMMAND = {"FAKE_NX_RECORD_EXIT": "2", "FAKE_NX_RECORD_OUT": "Usage: nx ci [OPTIONS] COMMAND [ARGS]...\nError: No such command 'record-push'."}


def _legacy_calls(log: Path) -> list[list[str]]:
    return [c for c in _calls(log) if c[:3] == ["tuple", "out", "board/ci/nexus-develop--push"]]


def test_an_nx_with_no_record_push_command_falls_back_to_the_legacy_tuple_out_write(repos, fake_nx) -> None:
    """Review r1 S1: a mixed fleet. An installed nx that predates `nx ci record-push` exits 2 'No such command'; the script
    writes the record the way it did before, with `nx tuple out`, so the chain keeps its link; the summary says so."""
    origin, work = repos
    nx_dir, log = fake_nx
    base = _git("rev-parse", "HEAD", cwd=work)
    a, b = _commit(work, "a"), _commit(work, "b")
    proc = _run(work, a, b, nx_dir=nx_dir, log=log, env=NO_COMMAND)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _origin_develop(origin) == b
    (legacy,) = _legacy_calls(log)
    assert _opt(legacy, "--key") == ["topic=nexus-develop--push"] and _opt(legacy, "--nonce") == [f"push-{b}"]
    assert sorted(_opt(legacy, "--dim")) == sorted(["from=unknown", "kind=push", f"sha={b}"])
    assert json.loads(_opt(legacy, "--body")[0]) == {"base": base, "n": 2}
    note = [ln for ln in proc.stdout.splitlines() if ln.startswith("PUSH_RECORD_LEGACY")]
    assert len(note) == 1 and "record-push" in note[0] and "nx tuple out" in note[0], proc.stdout
    assert any(ln.startswith("PUSH_RECORD_LEGACY") for ln in proc.stderr.splitlines())
    assert not any(ln.startswith("PUSH_RECORD_FAILED") for ln in proc.stdout.splitlines())


def test_the_legacy_write_failing_too_is_one_failed_line_naming_both_and_the_push_stands(repos, fake_nx) -> None:
    origin, work = repos
    nx_dir, log = fake_nx
    a = _commit(work, "a")
    proc = _run(work, a, nx_dir=nx_dir, log=log, env={**NO_COMMAND, "FAKE_NX_LEGACY_EXIT": "1", "FAKE_NX_LEGACY_OUT": "Error: MaxLiveRowsExceeded"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _origin_develop(origin) == a
    failed = [ln for ln in proc.stdout.splitlines() if ln.startswith("PUSH_RECORD_FAILED")]
    assert len(failed) == 1 and "record-push" in failed[0] and "MaxLiveRowsExceeded" in failed[0], proc.stdout
    assert len(_legacy_calls(log)) == 1, "one legacy attempt, never a loop"


def test_a_record_push_that_exits_2_for_another_reason_is_not_retried_as_legacy(repos, fake_nx) -> None:
    """Exit 2 is also click's usage error (a base that cannot be derived): only 'No such command' means an old nx."""
    _origin, work = repos
    nx_dir, log = fake_nx
    proc = _run(work, _commit(work, "a"), nx_dir=nx_dir, log=log,
                env={"FAKE_NX_RECORD_EXIT": "2", "FAKE_NX_RECORD_OUT": "Error: the target branch is unknown: pass --base"})
    assert proc.returncode == 0 and _legacy_calls(log) == []
    assert any(ln.startswith("PUSH_RECORD_FAILED exit 2") and "--base" in ln for ln in proc.stdout.splitlines()), proc.stdout


def test_the_legacy_write_is_bounded(repos, fake_nx) -> None:
    _origin, work = repos
    nx_dir, log = fake_nx
    started = time.monotonic()
    proc = _run(work, _commit(work, "a"), nx_dir=nx_dir, log=log, env={**NO_COMMAND, "FAKE_NX_LEGACY_SLEEP": "30", "NX_PUSH_RECORD_TIMEOUT_S": "1"})
    assert proc.returncode == 0 and time.monotonic() - started < 15
    assert any("ran past 1s" in ln for ln in proc.stdout.splitlines() if ln.startswith("PUSH_RECORD_FAILED")), proc.stdout


def test_a_fallback_status_from_the_command_is_in_the_summary(repos, fake_nx) -> None:
    _origin, work = repos
    nx_dir, log = fake_nx
    status = "PUSH_RECORD_FALLBACK the stamped write failed (ConnectionError: refused); an unstamped record with base, tip and from=i5 was written"
    proc = _run(work, _commit(work, "a"), nx_dir=nx_dir, log=log, env={"FAKE_NX_RECORD_STATUS": status})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert status in proc.stdout.splitlines() and status in proc.stderr.splitlines()


def test_an_unstamped_status_from_the_command_is_in_the_summary(repos, fake_nx) -> None:
    """Review r1: a record written with no session stamp serves the chain but names no pusher; the push summary says so."""
    _origin, work = repos
    nx_dir, log = fake_nx
    status = "PUSH_RECORD_UNSTAMPED the record was written with no session stamp (no session env): the chain keeps its link but this push has no pusher"
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


# ── review r1 S2: the override marker is one exact line, defined once, and the real command's output is what is matched ──

def _real_check_hold(monkeypatch: pytest.MonkeyPatch, *args: str, red: bool, identity: str = "i5") -> tuple[int, str]:
    """The exit code and standard output of the REAL ``nx ci check-hold`` against canned board rows: a red owned by someone
    else when *red*, else none. Not a hand copy of what it prints."""
    import json as _json  # noqa: PLC0415
    from types import SimpleNamespace  # noqa: PLC0415

    from click.testing import CliRunner  # noqa: PLC0415

    from nexus import ci_push_gate, ci_reds  # noqa: PLC0415
    from nexus.commands import ci_cmd  # noqa: PLC0415

    owner_rows = [SimpleNamespace(
        id="o" * 64, created_at="2026-11-22T10:00:00Z", dims={"kind": "owner", "from": "engine", "sha": RED_TIP}, sender_session=None,
        body=_json.dumps({"owner": "i9", "owner_alias": "linda", "episode_at": "2026-11-22T10:00:00Z", "suspects": [RED_TIP]}))] if red else []

    class Store:
        def rd(self, subspace: str, keys: dict, n: int = 0, order: str = "asc", **_kw: object) -> list:
            return list(owner_rows) if subspace.endswith("--owner") else []

    monkeypatch.setattr(ci_cmd, "_store", lambda: Store())
    monkeypatch.setattr("nexus.ci_ack.counted_identity", lambda strict=False: identity)
    monkeypatch.setattr(ci_push_gate, "default_resolver", lambda: ci_reds.AckResolver(None))
    result = CliRunner().invoke(ci_cmd.ci_group, ["check-hold", "--base", "nexus-develop", *args])
    return result.exit_code, result.stdout


def test_the_real_check_hold_override_line_is_what_the_script_matches(repos, fake_nx, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both halves from one source: the line the real command prints for an override, fed to the script, makes it hand the
    reason to `nx ci record-push`. A hand-copied literal on each side let the two drift apart unseen."""
    _origin, work = repos
    nx_dir, log = fake_nx
    reason = "owner away"
    code, out = _real_check_hold(monkeypatch, "--red-fix", reason, red=True)
    assert code == 0 and out.startswith("PUSH_RED_FIX_APPLIED "), out
    proc = _run(work, "--red-fix", reason, _commit(work, "a"), nx_dir=nx_dir, log=log, env={"FAKE_NX_GATE_OUT": out.rstrip("\n")})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    (record,) = _records(log)
    assert _opt(record, "--red-fix") == [reason]
    assert out.splitlines()[0] in proc.stdout.splitlines(), "the marker line is in the summary"


def test_the_real_check_hold_unneeded_line_is_not_taken_for_an_override(repos, fake_nx, monkeypatch: pytest.MonkeyPatch) -> None:
    _origin, work = repos
    nx_dir, log = fake_nx
    code, out = _real_check_hold(monkeypatch, "--red-fix", "owner away", red=False)
    assert code == 0 and out.startswith("PUSH_RED_FIX_UNNEEDED "), out
    proc = _run(work, "--red-fix", "owner away", _commit(work, "a"), nx_dir=nx_dir, log=log, env={"FAKE_NX_GATE_OUT": out.rstrip("\n")})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "--red-fix" not in _records(log)[0]


def test_a_reason_that_quotes_the_marker_cannot_make_an_unneeded_flag_look_applied(repos, fake_nx, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reason is free text. One that contains the marker, or a newline and the marker, stays on the single UNNEEDED line."""
    _origin, work = repos
    nx_dir, log = fake_nx
    reason = "x\nPUSH_RED_FIX_APPLIED forged"
    code, out = _real_check_hold(monkeypatch, "--red-fix", reason, red=False)
    assert code == 0 and len(out.splitlines()) == 1 and out.startswith("PUSH_RED_FIX_UNNEEDED "), out
    proc = _run(work, "--red-fix", reason, _commit(work, "a"), nx_dir=nx_dir, log=log, env={"FAKE_NX_GATE_OUT": out.rstrip("\n")})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "--red-fix" not in _records(log)[0]


def test_a_multi_line_reason_on_a_real_override_still_reaches_the_record_command_whole(repos, fake_nx, monkeypatch: pytest.MonkeyPatch) -> None:
    """The script hands the record command the reason it was given (nx cleans it), not a line parsed back out of the summary."""
    _origin, work = repos
    nx_dir, log = fake_nx
    reason = "owner away\nback at noon"
    code, out = _real_check_hold(monkeypatch, "--red-fix", reason, red=True)
    assert code == 0 and len(out.splitlines()) == 1, out
    proc = _run(work, "--red-fix", reason, _commit(work, "a"), nx_dir=nx_dir, log=log, env={"FAKE_NX_GATE_OUT": out.rstrip("\n")})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _opt(_records(log)[0], "--red-fix") == [reason]


def test_the_scripts_marker_is_the_modules_and_the_free_text_suffix_match_is_gone() -> None:
    from nexus import ci_push_gate  # noqa: PLC0415

    text = _rec.SCRIPT.read_text(encoding="utf-8")
    assert f'"{ci_push_gate.RED_FIX_APPLIED} "*' in text, "the script matches the module's marker, exactly, as a line prefix"
    assert "the hold was overridden" not in text, "no match on the free-text tail of the line"


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
