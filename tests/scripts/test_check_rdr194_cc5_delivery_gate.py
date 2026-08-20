# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``scripts/check_rdr194_cc5_delivery_gate.py`` (nexus-tk070.p5a
substantive-critic CRITICAL, T2 [22965]).

``scripts/`` is on ``pythonpath`` via ``[tool.pytest.ini_options]`` in
``pyproject.toml`` (same as the sibling ``check_engine_release_floor`` /
``check_remediation_commits_ride_release`` tests), so the gate module
imports directly with no ``sys.path`` hack.

Layout mirrors ``tests/scripts/test_check_inbound_relay_acks.py``: pure
logic (record validation) is tested directly with injected strings; the IO
boundary (``file_present_at_ref`` / ``fetch_cc5_record``) is exercised via
a throwaway git repo (same idiom as
``tests/scripts/test_check_remediation_commits_ride_release.py``) plus
monkeypatching for the ``nx`` half (no live ``nx``/T2 dependency); the
exit-code decision (``run_gate``) is tested directly since it is pure over
already-fetched IO results.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import check_rdr194_cc5_delivery_gate as gate


# ── Synthetic git repo helper (mirrors test_check_remediation_commits_ride_release.py) ──


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo, capture_output=True, text=True, check=True,
    )


def _init_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    return root


def _commit_file(repo: Path, relpath: str, content: str) -> str:
    p = repo / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    _git(repo, "add", relpath)
    _git(repo, "commit", "-q", "-m", f"add {relpath}")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


# ── validate_measured_record: pure logic, no IO ─────────────────────────────


def _valid_record(**overrides: object) -> str:
    values = {
        "topic_assignments_cross_tenant": 0,
        "topic_links_cross_tenant": 0,
        "topics_parent_cross_tenant": 0,
    }
    values.update(overrides)
    lines = [
        "STATUS: MEASURED",
        "cloud-count-5 (RDR-194 D4, nexus-tk070.cc5): 2026-08-21T00:00:00Z",
    ]
    for k, v in values.items():
        lines.append(f"{k}={v}")
    return "\n".join(lines)


def test_valid_measured_zero_record_passes() -> None:
    ok, problems = gate.validate_measured_record(_valid_record())
    assert ok is True
    assert problems == []


def test_missing_status_line_is_rejected() -> None:
    text = _valid_record().replace("STATUS: MEASURED\n", "")
    ok, problems = gate.validate_measured_record(text)
    assert ok is False
    assert any("STATUS: MEASURED" in p for p in problems)


def test_unmeasured_word_does_not_satisfy_the_status_line() -> None:
    """'unmeasured'/'NOT MEASURED' must not satisfy the status requirement,
    and must ALSO trip the explicit negative-marker rejection -- this is
    the exact vacuous-record shape (a prose note saying cc5 is
    BLOCKED/unmeasured) that must never satisfy the gate."""
    text = "cloud-count-5 is currently unmeasured -- blocked, no BYPASSRLS path.\n" + _valid_record().split("\n", 1)[1]
    ok, problems = gate.validate_measured_record(text)
    assert ok is False
    assert any("STATUS: MEASURED" in p for p in problems)
    assert any("negative/blocked status marker" in p for p in problems)


def test_status_line_must_be_dedicated_not_embedded_in_prose() -> None:
    """'STATUS: MEASURED' appearing mid-sentence (not as its own line) must
    not satisfy the anchor -- only a genuinely dedicated status line does."""
    text = "note: STATUS: MEASURED is the shape to use, once real.\n" + _valid_record().split("\n", 1)[1]
    ok, problems = gate.validate_measured_record(text)
    assert ok is False
    assert any("STATUS: MEASURED" in p for p in problems)


def test_vacuous_blocked_prose_record_is_rejected() -> None:
    """The exact shape of record this phase's own dev-notes already wrote
    to T2 (cc5 unrecorded/blocked, no BYPASSRLS path) must not pass."""
    text = (
        "cloud-count-5 (nexus-tk070.cc5) is still OPEN -- every conexus-"
        "reachable role is NOBYPASSRLS, so the cross-tenant population on "
        "the live cloud store cannot yet be measured."
    )
    ok, problems = gate.validate_measured_record(text)
    assert ok is False
    assert len(problems) >= 1


@pytest.mark.parametrize(
    "key",
    ["topic_assignments_cross_tenant", "topic_links_cross_tenant", "topics_parent_cross_tenant"],
)
def test_missing_one_required_count_key_is_rejected(key: str) -> None:
    lines = _valid_record().splitlines()
    filtered = "\n".join(ln for ln in lines if not ln.startswith(key))
    ok, problems = gate.validate_measured_record(filtered)
    assert ok is False
    assert any(key in p and "missing" in p for p in problems)


def test_nonzero_count_is_rejected_as_corruption_not_a_pass() -> None:
    text = _valid_record(topic_assignments_cross_tenant=3)
    ok, problems = gate.validate_measured_record(text)
    assert ok is False
    assert any("topic_assignments_cross_tenant" in p and "3" in p for p in problems)


def test_all_three_missing_reports_all_three_problems() -> None:
    ok, problems = gate.validate_measured_record("STATUS: MEASURED\nnothing else here")
    assert ok is False
    assert len(problems) == 3
    for key in gate._REQUIRED_COUNT_KEYS:
        assert any(key in p for p in problems)


# ── critic round-2 false-accepts (T2 [22965]): RED reproductions ───────────


def test_not_measured_yet_with_template_counts_is_rejected() -> None:
    """Critic round-2 false-accept (i): a record explicitly saying cc5 is
    NOT yet measured, which happens to quote the three counts as
    illustrative template prose (copying the remedy's own example verbatim
    while still being an UNmeasured status), must be rejected -- a bare
    'MEASURED' substring anywhere in the text (e.g. inside 'NOT MEASURED
    yet') must never satisfy the contract."""
    text = (
        "cloud-count-5 is NOT MEASURED yet -- once it is genuinely "
        "measured, record it in this shape:\n"
        "STATUS: MEASURED\n"
        "topic_assignments_cross_tenant=0\n"
        "topic_links_cross_tenant=0\n"
        "topics_parent_cross_tenant=0\n"
        "(the above is only an EXAMPLE of the target shape -- cc5 has not "
        "actually been measured yet, still blocked on BYPASSRLS access)."
    )
    ok, problems = gate.validate_measured_record(text)
    assert ok is False, (
        "a record that says NOT MEASURED, even while quoting a complete "
        f"STATUS: MEASURED template as illustrative prose, must be "
        f"rejected outright: {problems}"
    )
    assert any("NOT MEASURED" in p or "not measured" in p.lower() for p in problems), problems


def test_stale_zero_then_nonzero_same_key_is_rejected() -> None:
    """Critic round-2 false-accept (ii): the same count key appearing
    TWICE with DIFFERENT values (a stale '=0' left in place, superseded by
    a real '=3' later in the same record, or vice versa) must be rejected
    with a conflicting-counts error -- never silently preferring either
    the first or the last match."""
    text = (
        "STATUS: MEASURED\n"
        "cloud-count-5 (RDR-194 D4, nexus-tk070.cc5): 2026-08-21T00:00:00Z\n"
        "topic_assignments_cross_tenant=0\n"
        "topic_links_cross_tenant=0\n"
        "topics_parent_cross_tenant=0\n"
        "(correction below supersedes the stale topic_assignments_cross_tenant=0 above)\n"
        "topic_assignments_cross_tenant=3\n"
    )
    ok, problems = gate.validate_measured_record(text)
    assert ok is False, (
        f"conflicting distinct values for the same key must never silently "
        f"pass on either value: {problems}"
    )
    assert any(
        "topic_assignments_cross_tenant" in p and ("conflict" in p.lower() or "distinct" in p.lower())
        for p in problems
    ), problems


# ── file_present_at_ref: real git, throwaway repo ───────────────────────────


def test_file_present_at_ref_true_when_committed(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit_file(repo, gate.FK_FILE, "<databaseChangeLog/>")
    assert gate.file_present_at_ref("HEAD", repo_root=repo) is True


def test_file_present_at_ref_false_when_never_committed(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit_file(repo, "README.md", "hello")
    assert gate.file_present_at_ref("HEAD", repo_root=repo) is False


def test_file_present_at_ref_unverifiable_on_bad_ref(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit_file(repo, "README.md", "hello")
    result = gate.file_present_at_ref("this-ref-does-not-exist", repo_root=repo)
    assert result is gate._GIT_UNAVAILABLE


def test_file_present_at_ref_unverifiable_when_git_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a: object, **k: object) -> None:
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(gate.subprocess, "run", _boom)
    result = gate.file_present_at_ref("HEAD", repo_root=tmp_path)
    assert result is gate._GIT_UNAVAILABLE


# ── fetch_cc5_record: IO boundary, monkeypatched (no live nx dependency) ────


def test_fetch_cc5_record_returns_none_on_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Result:
        returncode = 1
        stdout = ""
        stderr = "Error: entry not found — use: nx memory list to see available entries\n"

    monkeypatch.setattr(gate.subprocess, "run", lambda *a, **k: _Result())
    assert gate.fetch_cc5_record() is None


def test_fetch_cc5_record_returns_text_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Result:
        returncode = 0
        stdout = "MEASURED ...\n"
        stderr = ""

    monkeypatch.setattr(gate.subprocess, "run", lambda *a, **k: _Result())
    assert gate.fetch_cc5_record() == "MEASURED ...\n"


def test_fetch_cc5_record_unverifiable_when_nx_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a: object, **k: object) -> None:
        raise FileNotFoundError("nx not found")

    monkeypatch.setattr(gate.subprocess, "run", _boom)
    assert gate.fetch_cc5_record() is gate._NX_UNAVAILABLE


def test_fetch_cc5_record_unverifiable_on_unrecognized_error_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """A nonzero exit that is NOT the specific 'entry not found' shape (e.g.
    an auth failure, or a T2 substrate that is down) must be UNVERIFIABLE,
    never silently folded into 'record absent'."""

    class _Result:
        returncode = 1
        stdout = ""
        stderr = "connection refused\n"

    monkeypatch.setattr(gate.subprocess, "run", lambda *a, **k: _Result())
    assert gate.fetch_cc5_record() is gate._NX_UNAVAILABLE


# ── run_gate: the exit-code decision, pure over injected IO results ────────


def test_run_gate_passes_when_file_absent_regardless_of_record() -> None:
    assert gate.run_gate(False, None) == 0
    assert gate.run_gate(False, gate._NX_UNAVAILABLE) == 0


def test_run_gate_unverifiable_when_git_cannot_resolve_the_ref() -> None:
    assert gate.run_gate(gate._GIT_UNAVAILABLE, None) == 2


def test_run_gate_red_tree_has_file_no_t2_record(capsys: pytest.CaptureFixture[str]) -> None:
    """THE explicit RED case the fix-pass asked for: file present in the
    tree, no T2 record at all -- must exit nonzero, never a silent pass."""
    rc = gate.run_gate(True, None)
    assert rc != 0
    err = capsys.readouterr().err
    assert "BLOCKED" in err
    assert "nexus-tk070.cc5" in err


def test_run_gate_unverifiable_when_nx_unreachable_and_file_present() -> None:
    assert gate.run_gate(True, gate._NX_UNAVAILABLE) == 2


def test_run_gate_red_when_record_exists_but_is_vacuous() -> None:
    vacuous = "cloud-count-5 is still blocked, no BYPASSRLS path."
    rc = gate.run_gate(True, vacuous)
    assert rc == 1


def test_run_gate_green_when_record_is_valid_measured_zero() -> None:
    rc = gate.run_gate(True, _valid_record())
    assert rc == 0


# ── main(): full IO-boundary wiring, monkeypatched (no live git/nx call) ───


def test_main_exit_0_when_file_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "file_present_at_ref", lambda ref, repo_root=None: False)
    monkeypatch.setattr(gate, "fetch_cc5_record", lambda project, title: (_ for _ in ()).throw(
        AssertionError("nx must not be consulted when the file is absent")
    ))
    assert gate.main([]) == 0


def test_main_exit_nonzero_tree_has_file_no_t2_record(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "file_present_at_ref", lambda ref, repo_root=None: True)
    monkeypatch.setattr(gate, "fetch_cc5_record", lambda project, title: None)
    assert gate.main([]) != 0


def test_main_exit_0_when_file_present_and_record_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "file_present_at_ref", lambda ref, repo_root=None: True)
    monkeypatch.setattr(gate, "fetch_cc5_record", lambda project, title: _valid_record())
    assert gate.main([]) == 0
