# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx rdr preamble rdr-create`` skips RDR numbers held anywhere (nexus-3piej).

Next ID used to come from the current tree's docs/rdr only, so a number held
by an unmerged branch, an uncommitted draft in another worktree, or a T2
record was offered again, and the skill's record write then upserted over the
other RDR's T2 row (2026-10-26: RDR-233 and 234).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

import nexus.commands.rdr as rdr_mod
from nexus.commands.rdr import rdr

_FM = "---\ntitle: {t}\nstatus: draft\ntype: Feature\npriority: P2\n---\n\n# {t}\n"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, check=True, capture_output=True, text=True, encoding="utf-8",
    ).stdout.strip()


def _rdr(path: Path, name: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(_FM.format(t=name), encoding="utf-8")


def _t2(monkeypatch: pytest.MonkeyPatch, titles: list[str] | None) -> None:
    monkeypatch.setattr(rdr_mod, "_preamble_t2_rdr_titles", lambda repo_name: titles)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q", "-b", "develop")
    _rdr(root / "docs/rdr", "rdr-001-base.md")
    _rdr(root / "docs/rdr", "README.md")
    _git(root, "add", "docs")
    _git(root, "commit", "-q", "-m", "base")
    # An unmerged branch holds 005.
    _git(root, "switch", "-q", "-c", "feature/held")
    _rdr(root / "docs/rdr", "rdr-005-on-a-branch.md")
    _git(root, "add", "docs")
    _git(root, "commit", "-q", "-m", "held")
    _git(root, "switch", "-q", "develop")
    # Another worktree holds 007 as an uncommitted draft.
    wt = tmp_path / "wt-other"
    _git(root, "worktree", "add", "-q", "-b", "feature/draft", str(wt))
    _rdr(wt / "docs/rdr", "rdr-007-uncommitted-draft.md")
    # Files that are not RDR numbers of this directory: one in a subdirectory
    # whose name would otherwise parse, and a dated note.
    _rdr(root / "docs/rdr/100-archive", "old.md")
    _rdr(root / "docs/rdr/joint", "rdr-060-joint.md")
    _rdr(root / "docs/rdr", "2026-10-26-handoff.md")
    _git(root, "add", "docs")
    _git(root, "commit", "-q", "-m", "joint")
    monkeypatch.chdir(root)
    _t2(monkeypatch, [])
    return root


def _create() -> str:
    result = CliRunner().invoke(rdr, ["preamble", "rdr-create"])
    assert result.exit_code == 0, result.output
    return result.output


def _next_id(output: str) -> str:
    line = next(ln for ln in output.splitlines() if ln.startswith("**Next ID:**"))
    return line.split("`")[1]


def test_next_id_skips_branch_and_worktree_holders(repo: Path) -> None:
    out = _create()
    assert _next_id(out) == "RDR-008"
    assert "RDR-005: branch feature/held" in out
    assert "RDR-007" in out and "wt-other" in out
    assert "RDR-100" not in out and "RDR-060" not in out and "2026" not in _next_id(out)
    assert "incomplete" not in out


def test_next_id_skips_a_remote_only_branch(repo: Path) -> None:
    sha = _git(repo, "rev-parse", "feature/held")
    _git(repo, "update-ref", "refs/remotes/origin/feature/remote-only", sha)
    _git(repo, "branch", "-q", "-D", "feature/held")
    out = _create()
    assert _next_id(out) == "RDR-008"
    assert "RDR-005: branch origin/feature/remote-only" in out


def test_next_id_skips_t2_records_and_research_rows(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _t2(monkeypatch, ["011", "014-research-1", "014-research-2", "notes"])
    out = _create()
    assert _next_id(out) == "RDR-015"
    assert "RDR-011: T2 proj_rdr/011" in out
    assert "RDR-014: T2 proj_rdr/014-research-1" in out


def test_an_unreadable_holder_source_is_said(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _t2(monkeypatch, None)
    out = _create()
    assert _next_id(out) == "RDR-008"
    assert "Holder scan incomplete" in out


def test_git_failure_is_said(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rdr_mod, "_git_lines", lambda repo_root, *args: None)
    out = _create()
    assert _next_id(out) == "RDR-002"
    assert "Holder scan incomplete" in out


def test_bootstrap_skips_held_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "fresh"
    root.mkdir()
    _git(root, "init", "-q", "-b", "develop")
    monkeypatch.chdir(root)
    _t2(monkeypatch, ["001", "002-research-1"])
    out = _create()
    assert "bootstrap required" in out
    assert _next_id(out) == "RDR-003"


def test_no_holders_elsewhere_keeps_the_local_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "solo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "develop")
    _rdr(root / "docs/rdr", "rdr-003-only.md")
    _git(root, "add", "docs")
    _git(root, "commit", "-q", "-m", "only")
    monkeypatch.chdir(root)
    _t2(monkeypatch, [])
    out = _create()
    assert _next_id(out) == "RDR-004"
    assert "held elsewhere" not in out and "incomplete" not in out
