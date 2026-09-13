# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``scripts/check_agent_verify_claims.py`` (bead nexus-cnzei.6
item 3): the orchestrator-side check that a report row's VERIFY claims
(commit / t2_ref / verify, filled by ``tuple_ledger_project.py``'s item-2
extension) are actually TRUE, not merely present.

Pure-function tests over ``check()`` with planted ``TupleRow``-shaped rows
(no live tuple-space call — ``_fetch_report_rows`` is monkeypatched), plus
real ``git`` fixtures for the commit-existence/touched-paths checks (a
tmp-path repo, never this checkout). ``scripts/`` is on pythonpath via
``[tool.pytest.ini_options]`` in ``pyproject.toml``.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import check_agent_verify_claims as checker


def _row(agent_id: str, dims: dict[str, str]) -> SimpleNamespace:
    """A ``TupleRow``-shaped stand-in: ``check()`` reads only ``.keys``
    and ``.dims``."""
    return SimpleNamespace(keys={"agent_id": agent_id, "kind": "report"}, dims=dims)


# ── git fixture: a real repo, never this checkout ───────────────────────────


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=10, check=True,
    )


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "src").mkdir()
    (repo / "src" / "foo.py").write_text("x = 1\n")
    (repo / "other.py").write_text("y = 2\n")
    _git(repo, "add", "src/foo.py", "other.py")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


def _commit_touching(repo: Path, rel_path: str, content: str) -> str:
    path = repo / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    _git(repo, "add", rel_path)
    _git(repo, "commit", "-q", "-m", f"touch {rel_path}")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


# ── check(): verify-presence ─────────────────────────────────────────────


def test_verify_absent_is_a_finding(tmp_repo: Path) -> None:
    result = checker.check([_row("a1", {"verify": "absent"})], repo=tmp_repo, expected_paths=[])
    assert not result.clean
    assert result.findings == [checker.Finding("a1", "verify=absent")]


def test_verify_missing_entirely_is_a_finding(tmp_repo: Path) -> None:
    """A pre-nexus-cnzei.6 row (or a below-floor engine's fallback row)
    carries no ``verify`` dim at all -- treated identically to an
    explicit ``absent``, never as a silent pass."""
    result = checker.check([_row("a1", {})], repo=tmp_repo, expected_paths=[])
    assert not result.clean
    assert result.findings == [checker.Finding("a1", "verify=absent")]


def test_verify_present_with_no_commit_claim_is_clean(tmp_repo: Path) -> None:
    result = checker.check([_row("a1", {"verify": "present"})], repo=tmp_repo, expected_paths=[])
    assert result.clean


# ── check(): commit existence ────────────────────────────────────────────


def test_nonexistent_commit_is_a_finding(tmp_repo: Path) -> None:
    result = checker.check(
        [_row("a1", {"verify": "present", "commit": "0" * 40})],
        repo=tmp_repo, expected_paths=[],
    )
    assert not result.clean
    assert any("does not exist" in f.reason for f in result.findings)


def test_real_commit_with_no_expected_paths_is_clean(tmp_repo: Path) -> None:
    """No ``--path`` given -- existence alone is checked."""
    sha = _commit_touching(tmp_repo, "src/bar.py", "z = 3\n")
    result = checker.check(
        [_row("a1", {"verify": "present", "commit": sha})],
        repo=tmp_repo, expected_paths=[],
    )
    assert result.clean


# ── check(): commit relevance (touches a named path) ─────────────────────


def test_commit_touching_a_named_path_is_clean(tmp_repo: Path) -> None:
    sha = _commit_touching(tmp_repo, "src/foo.py", "x = 2\n")
    result = checker.check(
        [_row("a1", {"verify": "present", "commit": sha})],
        repo=tmp_repo, expected_paths=["src/foo.py"],
    )
    assert result.clean


def test_commit_touching_none_of_the_named_paths_is_a_finding(tmp_repo: Path) -> None:
    sha = _commit_touching(tmp_repo, "other.py", "y = 3\n")
    result = checker.check(
        [_row("a1", {"verify": "present", "commit": sha})],
        repo=tmp_repo, expected_paths=["src/foo.py"],
    )
    assert not result.clean
    assert any("touches none of" in f.reason for f in result.findings)


def test_commit_touching_a_file_under_a_named_directory_is_clean(tmp_repo: Path) -> None:
    """``--path src`` (a directory) is satisfied by any file under it."""
    sha = _commit_touching(tmp_repo, "src/baz.py", "w = 4\n")
    result = checker.check(
        [_row("a1", {"verify": "present", "commit": sha})],
        repo=tmp_repo, expected_paths=["src"],
    )
    assert result.clean


def test_multiple_rows_accumulate_independent_findings(tmp_repo: Path) -> None:
    sha = _commit_touching(tmp_repo, "src/foo.py", "x = 9\n")
    result = checker.check(
        [
            _row("clean-agent", {"verify": "present", "commit": sha}),
            _row("bad-agent", {"verify": "absent"}),
        ],
        repo=tmp_repo, expected_paths=["src/foo.py"],
    )
    assert result.examined == 2
    assert result.findings == [checker.Finding("bad-agent", "verify=absent")]


# ── main(): non-vacuity, exit codes, --json ──────────────────────────────


def test_main_examined_zero_is_non_vacuity_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checker, "_fetch_report_rows", lambda session_id: [])
    rc = checker.main(["check_agent_verify_claims.py", "sess-empty"])
    assert rc == 2


def test_main_exit_zero_on_clean(monkeypatch: pytest.MonkeyPatch, tmp_repo: Path) -> None:
    sha = _commit_touching(tmp_repo, "src/foo.py", "x = 5\n")
    monkeypatch.setattr(
        checker, "_fetch_report_rows",
        lambda session_id: [_row("a1", {"verify": "present", "commit": sha})],
    )
    rc = checker.main([
        "check_agent_verify_claims.py", "sess-clean",
        "--repo", str(tmp_repo), "--path", "src/foo.py",
    ])
    assert rc == 0


def test_main_exit_one_on_findings(monkeypatch: pytest.MonkeyPatch, tmp_repo: Path) -> None:
    monkeypatch.setattr(
        checker, "_fetch_report_rows",
        lambda session_id: [_row("a1", {"verify": "absent"})],
    )
    rc = checker.main(["check_agent_verify_claims.py", "sess-findings", "--repo", str(tmp_repo)])
    assert rc == 1


def test_main_json_output_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_repo: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        checker, "_fetch_report_rows",
        lambda session_id: [_row("a1", {"verify": "absent"})],
    )
    rc = checker.main([
        "check_agent_verify_claims.py", "sess-json", "--repo", str(tmp_repo), "--json",
    ])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["session_id"] == "sess-json"
    assert out["examined"] == 1
    assert out["findings"] == [{"agent_id": "a1", "reason": "verify=absent"}]


def test_main_json_non_vacuity_shape(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(checker, "_fetch_report_rows", lambda session_id: [])
    rc = checker.main(["check_agent_verify_claims.py", "sess-empty", "--json"])
    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["examined"] == 0
    assert "error" in out
