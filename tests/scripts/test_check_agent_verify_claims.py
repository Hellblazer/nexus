# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``scripts/check_agent_verify_claims.py`` (bead nexus-cnzei.6
item 3): the orchestrator-side check that a report row's VERIFY claims
(commit / t2_ref / verify, filled by ``tuple_ledger_project.py``'s item-2
extension) are actually TRUE, not merely present.

Pure-function tests over ``check()`` with planted ``TupleRow``-shaped rows
(no live tuple-space call — ``_make_store``/``_declared_ledger_dims``/
``_make_memory_store`` are monkeypatched), plus real ``git`` fixtures for
the commit-existence/touched-paths/merge-commit checks (a tmp-path repo,
never this checkout). ``scripts/`` is on pythonpath via
``[tool.pytest.ini_options]`` in ``pyproject.toml``.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import check_agent_verify_claims as checker

#: Captured before the autouse fixture below patches ``checker._declared_
#: ledger_dims`` for every ``main()``-level test in this file -- the two
#: direct unit tests of the REAL function at the bottom call this, not
#: the (by then monkeypatched) module attribute.
_REAL_DECLARED_LEDGER_DIMS = checker._declared_ledger_dims


def _row(agent_id: str, dims: dict[str, str]) -> SimpleNamespace:
    """A ``TupleRow``-shaped stand-in: ``check()`` reads only ``.keys``
    and ``.dims``."""
    return SimpleNamespace(keys={"agent_id": agent_id, "kind": "report"}, dims=dims)


class _FakeMemoryStore:
    """A ``HttpMemoryStore``-shaped stand-in: ``check()``/``_t2_entry_exists``
    call only ``resolve_title(project=..., title=...) -> (entry, candidates)``."""

    def __init__(self, existing: set[str] = frozenset(), ambiguous: set[str] = frozenset()):
        self.existing = existing
        self.ambiguous = ambiguous
        self.calls: list[tuple[str, str]] = []

    def resolve_title(self, *, project: str, title: str):
        self.calls.append((project, title))
        ref = f"{project}/{title}"
        if ref in self.existing:
            return {"id": 1, "project": project, "title": title}, []
        if ref in self.ambiguous:
            return None, [{"id": 1, "title": title}, {"id": 2, "title": title + "-2"}]
        return None, []


@pytest.fixture(autouse=True)
def _stub_store_and_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this file that calls ``main()`` needs a store/engine
    stand-in — default to "engine declares all three dims" (the common
    case a floor-crossed engine reaches) so item-1's UNVERIFIABLE branch
    is opt-in per test, not the ambient default that would silently mask
    every other test's assertions."""
    monkeypatch.setattr(checker, "_make_store", lambda: object())
    monkeypatch.setattr(checker, "_declared_ledger_dims", lambda store: set(checker._REQUIRED_VERIFY_DIMS))
    monkeypatch.setattr(checker, "_make_memory_store", lambda: _FakeMemoryStore())


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


def _merge_commit_touching(repo: Path, feature_path: str, content: str) -> str:
    """Build a genuine ``--no-ff`` merge whose ONLY new content relative
    to its first parent is *feature_path* — the exact shape CRE finding 1
    measured plain ``diff-tree`` as blind to."""
    base_branch = _git(repo, "branch", "--show-current").stdout.strip()
    _git(repo, "checkout", "-q", "-b", "feature-branch")
    (repo / feature_path).parent.mkdir(parents=True, exist_ok=True)
    (repo / feature_path).write_text(content)
    _git(repo, "add", feature_path)
    _git(repo, "commit", "-q", "-m", f"add {feature_path}")
    _git(repo, "checkout", "-q", base_branch)
    _git(repo, "merge", "--no-ff", "-q", "-m", "merge feature-branch", "feature-branch")
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


# ── check(): merge commits (fix round 1, CRE finding 1) ──────────────────


def test_merge_commit_touching_a_named_path_is_clean(tmp_repo: Path) -> None:
    """A genuine ``--no-ff`` merge whose only new content is the named
    path must be recognised as touching it -- plain ``git diff-tree``
    with no ``-m``/first-parent handling returns EMPTY for a merge
    commit, which would wrongly convict this as a finding."""
    sha = _merge_commit_touching(tmp_repo, "src/merged.py", "m = 1\n")
    result = checker.check(
        [_row("a1", {"verify": "present", "commit": sha})],
        repo=tmp_repo, expected_paths=["src/merged.py"],
    )
    assert result.clean


def test_merge_commit_touching_none_of_the_named_paths_is_still_a_finding(tmp_repo: Path) -> None:
    """The merge fix must not become a blanket pass for merges -- a merge
    that genuinely does not touch the named path is still a finding."""
    sha = _merge_commit_touching(tmp_repo, "src/merged.py", "m = 1\n")
    result = checker.check(
        [_row("a1", {"verify": "present", "commit": sha})],
        repo=tmp_repo, expected_paths=["src/somewhere-else.py"],
    )
    assert not result.clean
    assert any("touches none of" in f.reason for f in result.findings)


def test_commit_touched_paths_returns_first_parent_diff_for_a_merge(tmp_repo: Path) -> None:
    sha = _merge_commit_touching(tmp_repo, "src/merged.py", "m = 1\n")
    touched = checker._commit_touched_paths(tmp_repo, sha)
    assert touched == {"src/merged.py"}


# ── check(): t2_ref existence (fix round 1, critic Critical 1) ───────────


def test_t2_ref_that_exists_is_clean(tmp_repo: Path) -> None:
    mem = _FakeMemoryStore(existing={"nexus/impl-notes"})
    result = checker.check(
        [_row("a1", {"verify": "present", "t2_ref": "nexus/impl-notes"})],
        repo=tmp_repo, expected_paths=[], memory_store=mem,
    )
    assert result.clean
    assert mem.calls == [("nexus", "impl-notes")]


def test_t2_ref_that_does_not_exist_is_a_finding(tmp_repo: Path) -> None:
    mem = _FakeMemoryStore(existing=set())
    result = checker.check(
        [_row("a1", {"verify": "present", "t2_ref": "nexus/does-not-exist"})],
        repo=tmp_repo, expected_paths=[], memory_store=mem,
    )
    assert not result.clean
    assert any("not found in T2" in f.reason for f in result.findings)


def test_t2_ref_ambiguous_prefix_is_a_finding(tmp_repo: Path) -> None:
    """An ambiguous prefix match does not confirm the ONE entry claimed."""
    mem = _FakeMemoryStore(ambiguous={"nexus/impl"})
    result = checker.check(
        [_row("a1", {"verify": "present", "t2_ref": "nexus/impl"})],
        repo=tmp_repo, expected_paths=[], memory_store=mem,
    )
    assert not result.clean


def test_malformed_t2_ref_with_no_slash_is_a_finding(tmp_repo: Path) -> None:
    mem = _FakeMemoryStore()
    result = checker.check(
        [_row("a1", {"verify": "present", "t2_ref": "not-a-project-slash-title"})],
        repo=tmp_repo, expected_paths=[], memory_store=mem,
    )
    assert not result.clean
    assert mem.calls == []


def test_t2_ref_with_no_memory_store_is_skipped_silently(tmp_repo: Path) -> None:
    """``check()`` is still callable with ``memory_store=None`` (its
    default) for a caller that only wants the commit/verify checks."""
    result = checker.check(
        [_row("a1", {"verify": "present", "t2_ref": "nexus/whatever"})],
        repo=tmp_repo, expected_paths=[],
    )
    assert result.clean


# ── main(): non-vacuity, exit codes, --json ──────────────────────────────


def test_main_examined_zero_is_non_vacuity_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checker, "_fetch_report_rows", lambda store, session_id: [])
    rc = checker.main(["check_agent_verify_claims.py", "sess-empty"])
    assert rc == checker.EXIT_NON_VACUITY


def test_main_exit_zero_on_clean(monkeypatch: pytest.MonkeyPatch, tmp_repo: Path) -> None:
    sha = _commit_touching(tmp_repo, "src/foo.py", "x = 5\n")
    monkeypatch.setattr(
        checker, "_fetch_report_rows",
        lambda store, session_id: [_row("a1", {"verify": "present", "commit": sha})],
    )
    rc = checker.main([
        "check_agent_verify_claims.py", "sess-clean",
        "--repo", str(tmp_repo), "--path", "src/foo.py",
    ])
    assert rc == checker.EXIT_CLEAN


def test_main_exit_one_on_findings(monkeypatch: pytest.MonkeyPatch, tmp_repo: Path) -> None:
    monkeypatch.setattr(
        checker, "_fetch_report_rows",
        lambda store, session_id: [_row("a1", {"verify": "absent"})],
    )
    rc = checker.main(["check_agent_verify_claims.py", "sess-findings", "--repo", str(tmp_repo)])
    assert rc == checker.EXIT_FINDINGS


def test_main_json_output_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_repo: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        checker, "_fetch_report_rows",
        lambda store, session_id: [_row("a1", {"verify": "absent"})],
    )
    rc = checker.main([
        "check_agent_verify_claims.py", "sess-json", "--repo", str(tmp_repo), "--json",
    ])
    assert rc == checker.EXIT_FINDINGS
    out = json.loads(capsys.readouterr().out)
    assert out["session_id"] == "sess-json"
    assert out["examined"] == 1
    assert out["findings"] == [{"agent_id": "a1", "reason": "verify=absent"}]


def test_main_json_non_vacuity_shape(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(checker, "_fetch_report_rows", lambda store, session_id: [])
    rc = checker.main(["check_agent_verify_claims.py", "sess-empty", "--json"])
    assert rc == checker.EXIT_NON_VACUITY
    out = json.loads(capsys.readouterr().out)
    assert out["examined"] == 0
    assert "error" in out


# ── main(): CLEAN message names the path scope (fix round 1, critic Significant b) ─


def test_main_clean_message_names_existence_only_scope_with_no_path(
    monkeypatch: pytest.MonkeyPatch, tmp_repo: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    sha = _commit_touching(tmp_repo, "src/foo.py", "x = 5\n")
    monkeypatch.setattr(
        checker, "_fetch_report_rows",
        lambda store, session_id: [_row("a1", {"verify": "present", "commit": sha})],
    )
    rc = checker.main(["check_agent_verify_claims.py", "sess-noscope", "--repo", str(tmp_repo)])
    assert rc == checker.EXIT_CLEAN
    out = capsys.readouterr().out
    assert "existence checked only" in out
    assert "no --path given" in out


def test_main_clean_message_confirms_relevance_scope_with_path(
    monkeypatch: pytest.MonkeyPatch, tmp_repo: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    sha = _commit_touching(tmp_repo, "src/foo.py", "x = 5\n")
    monkeypatch.setattr(
        checker, "_fetch_report_rows",
        lambda store, session_id: [_row("a1", {"verify": "present", "commit": sha})],
    )
    rc = checker.main([
        "check_agent_verify_claims.py", "sess-withscope",
        "--repo", str(tmp_repo), "--path", "src/foo.py",
    ])
    assert rc == checker.EXIT_CLEAN
    out = capsys.readouterr().out
    assert "existence checked only" not in out
    assert "checkable, verified claim" in out


def test_main_json_names_path_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_repo: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    sha = _commit_touching(tmp_repo, "src/foo.py", "x = 5\n")
    monkeypatch.setattr(
        checker, "_fetch_report_rows",
        lambda store, session_id: [_row("a1", {"verify": "present", "commit": sha})],
    )
    rc = checker.main([
        "check_agent_verify_claims.py", "sess-jsonscope", "--repo", str(tmp_repo), "--json",
    ])
    assert rc == checker.EXIT_CLEAN
    out = json.loads(capsys.readouterr().out)
    assert out["path_scope"] == "existence-only"


# ── main(): UNVERIFIABLE engine (fix round 1, critic Critical 2) ─────────


def test_main_unverifiable_when_engine_declares_no_new_dims(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A below-floor engine (pre engine-service-v0.1.118) declares none
    of commit/t2_ref/verify -- UNVERIFIABLE, distinct exit code, no rows
    fetched, no findings reported."""
    called_fetch = []
    monkeypatch.setattr(checker, "_declared_ledger_dims", lambda store: set())
    monkeypatch.setattr(
        checker, "_fetch_report_rows",
        lambda store, session_id: called_fetch.append(1) or [],
    )
    rc = checker.main(["check_agent_verify_claims.py", "sess-old-engine"])
    assert rc == checker.EXIT_UNVERIFIABLE
    assert called_fetch == [], "must never fetch rows once the engine is confirmed below floor"
    err = capsys.readouterr().err
    assert "UNVERIFIABLE" in err
    assert "commit" in err and "t2_ref" in err and "verify" in err


def test_main_unverifiable_json_shape(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(checker, "_declared_ledger_dims", lambda store: set())
    rc = checker.main(["check_agent_verify_claims.py", "sess-old-engine", "--json"])
    assert rc == checker.EXIT_UNVERIFIABLE
    out = json.loads(capsys.readouterr().out)
    assert out["unverifiable"] is True
    assert "reason" in out


def test_main_proceeds_normally_when_engine_declares_all_three_dims(
    monkeypatch: pytest.MonkeyPatch, tmp_repo: Path,
) -> None:
    """The other branch: an at-or-above-floor engine declares all three
    -- checking proceeds exactly as before this fix round."""
    monkeypatch.setattr(checker, "_declared_ledger_dims", lambda store: set(checker._REQUIRED_VERIFY_DIMS))
    monkeypatch.setattr(
        checker, "_fetch_report_rows",
        lambda store, session_id: [_row("a1", {"verify": "present"})],
    )
    rc = checker.main(["check_agent_verify_claims.py", "sess-new-engine", "--repo", str(tmp_repo)])
    assert rc == checker.EXIT_CLEAN


def test_declared_ledger_dims_finds_the_ledger_template_by_name() -> None:
    store = SimpleNamespace(registry=lambda: {
        "digest": "x", "sources": ["resources"],
        "templates": [
            {"name": "mailbox/<address>", "dimensions": {"from": {}}},
            {"name": "ledger/<session_id>", "dimensions": {"agent_type": {}, "commit": {}, "t2_ref": {}, "verify": {}}},
            {"name": "directory/<name>", "dimensions": {"session_id": {}}},
        ],
    })
    assert _REAL_DECLARED_LEDGER_DIMS(store) == {"agent_type", "commit", "t2_ref", "verify"}


def test_declared_ledger_dims_empty_when_template_absent() -> None:
    store = SimpleNamespace(registry=lambda: {"digest": "x", "sources": [], "templates": []})
    assert _REAL_DECLARED_LEDGER_DIMS(store) == set()
