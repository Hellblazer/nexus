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


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, check=True, capture_output=True, text=True, encoding="utf-8",
    )


def _rdr(path: Path, name: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(_FM.format(t=name), encoding="utf-8")


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
    # A file in a subdirectory is not an RDR number of this directory.
    _rdr(root / "docs/rdr/joint", "JDR-050-joint.md")
    _git(root, "add", "docs")
    _git(root, "commit", "-q", "-m", "joint")
    monkeypatch.chdir(root)
    monkeypatch.setattr(rdr_mod, "_preamble_get_rdrs_from_t2", lambda repo_name, rdr_dir: [])
    return root


def _next_id(output: str) -> str:
    line = next(ln for ln in output.splitlines() if ln.startswith("**Next ID:**"))
    return line.split("`")[1]


def test_next_id_skips_branch_and_worktree_holders(repo: Path) -> None:
    result = CliRunner().invoke(rdr, ["preamble", "rdr-create"])
    assert result.exit_code == 0, result.output
    assert _next_id(result.output) == "RDR-008"
    assert "RDR-005" in result.output and "feature/held" in result.output
    assert "RDR-007" in result.output and "wt-other" in result.output


def test_next_id_skips_a_t2_record(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        rdr_mod, "_preamble_get_rdrs_from_t2",
        lambda repo_name, rdr_dir: [{"id": "011", "file_path": "docs/rdr/rdr-011-x.md"}],
    )
    result = CliRunner().invoke(rdr, ["preamble", "rdr-create"])
    assert result.exit_code == 0, result.output
    assert _next_id(result.output) == "RDR-012"
    assert "RDR-011" in result.output and "T2" in result.output


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
    monkeypatch.setattr(rdr_mod, "_preamble_get_rdrs_from_t2", lambda repo_name, rdr_dir: [])
    result = CliRunner().invoke(rdr, ["preamble", "rdr-create"])
    assert result.exit_code == 0, result.output
    assert _next_id(result.output) == "RDR-004"
    assert "held elsewhere" not in result.output
