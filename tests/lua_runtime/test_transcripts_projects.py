# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""transcripts.sessions project "repo": which Claude Code project directories hold a repository's sessions.

A repository's sessions sit in one project directory per working tree: the
main checkout, each sibling worktree and Claude's own isolation worktrees
(98 of 178 nexus main sessions were outside the main checkout's directory,
T2 nexus_rdr/238-research-rederived-2026-11-08). Directory names are lossy
encodings of paths, so membership is decided from the ``cwd`` each session
transcript records, and must hold for worktrees that no longer exist.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from nexus.lua_runtime.transcripts_projects import repo_project_dirs
from nexus.lua_runtime.verb_types import VerbTimeout


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    main = tmp_path / "git" / "proj"
    main.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=main)
    _git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "init",
        cwd=main,
    )
    return main


def _session(
    projects: Path, name: str, cwd: Path | str | None, sid: str = "s1"
) -> None:
    d = projects / name
    d.mkdir(parents=True, exist_ok=True)
    lines = [{"type": "summary", "summary": "x"}]
    if cwd is not None:
        lines.append(
            {"type": "user", "cwd": str(cwd), "timestamp": "2026-11-01T00:00:00Z"}
        )
    (d / f"{sid}.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))


def _dirs(projects: Path, repo: Path, **kw: float) -> list[str]:
    return repo_project_dirs(
        projects, repo, deadline=kw.get("deadline", time.monotonic() + 30)
    )


def test_the_main_checkout_and_paths_under_it_belong(tmp_path: Path) -> None:
    main = _repo(tmp_path)
    projects = tmp_path / "projects"
    _session(projects, "main-dir", main)
    _session(
        projects, "agent-wt", main / ".claude" / "worktrees" / "agent-1"
    )  # never existed
    _session(projects, "subdir", main / "src")
    assert _dirs(projects, main) == ["agent-wt", "main-dir", "subdir"]


def test_a_removed_sibling_worktree_still_belongs(tmp_path: Path) -> None:
    main = _repo(tmp_path)
    projects = tmp_path / "projects"
    _session(
        projects, "gone", main.parent / "proj-wt" / "nexus_ee7-rrkm7"
    )  # removed, name has _
    assert _dirs(projects, main) == ["gone"]


def test_a_live_worktree_outside_the_layout_belongs(tmp_path: Path) -> None:
    main = _repo(tmp_path)
    elsewhere = tmp_path / "far" / "wt"
    _git("worktree", "add", "-q", "--detach", str(elsewhere), cwd=main)
    projects = tmp_path / "projects"
    _session(projects, "far-wt", elsewhere)
    _session(projects, "far-wt-sub", elsewhere / "docs")
    assert _dirs(projects, main) == ["far-wt", "far-wt-sub"]


def test_a_moved_worktree_found_only_by_its_common_dir_belongs(tmp_path: Path) -> None:
    # Rule 4: an existing cwd that git places in this repository, though no
    # other rule names its path (the worktree list is stale for it).
    main = _repo(tmp_path)
    wt = tmp_path / "far" / "wt"
    _git("worktree", "add", "-q", "--detach", str(wt), cwd=main)
    moved = tmp_path / "moved" / "wt"
    moved.parent.mkdir(parents=True)
    shutil.move(
        str(wt), str(moved)
    )  # moved by hand, so git's list still names the old path
    projects = tmp_path / "projects"
    _session(projects, "moved-wt", moved)
    assert _dirs(projects, main) == ["moved-wt"]


def test_a_worktree_caller_gets_the_same_set_as_the_main_checkout(
    tmp_path: Path,
) -> None:
    main = _repo(tmp_path)
    wt = main.parent / "proj-wt" / "a"
    _git("worktree", "add", "-q", "--detach", str(wt), cwd=main)
    projects = tmp_path / "projects"
    _session(projects, "main-dir", main)
    _session(projects, "wt-a", wt)
    assert _dirs(projects, wt) == _dirs(projects, main) == ["main-dir", "wt-a"]


@pytest.mark.parametrize(
    "cwd",
    [
        "{root}/git/proj2",  # a path prefix that is not a path component
        "{root}/git/proj-wtx/a",  # not the -wt/ sibling root
        "{root}/git/other",  # another directory
        "relative/path",  # not absolute
    ],
)
def test_a_session_elsewhere_does_not_belong(tmp_path: Path, cwd: str) -> None:
    main = _repo(tmp_path)
    projects = tmp_path / "projects"
    _session(projects, "elsewhere", cwd.format(root=tmp_path))
    assert _dirs(projects, main) == []


def test_a_clone_of_the_same_repository_does_not_belong(tmp_path: Path) -> None:
    main = _repo(tmp_path)
    clone = tmp_path / "clone"
    _git("clone", "-q", str(main), str(clone), cwd=tmp_path)
    projects = tmp_path / "projects"
    _session(projects, "clone", clone)
    assert _dirs(projects, main) == []


def test_a_directory_with_no_recorded_cwd_does_not_belong(tmp_path: Path) -> None:
    main = _repo(tmp_path)
    projects = tmp_path / "projects"
    _session(projects, "blank", None)
    (projects / "empty").mkdir()
    (projects / "junk").mkdir()
    (projects / "junk" / "s.jsonl").write_text("not json\n")
    assert _dirs(projects, main) == []


def test_any_session_in_a_directory_decides_it(tmp_path: Path) -> None:
    main = _repo(tmp_path)
    projects = tmp_path / "projects"
    _session(projects, "mixed", None, sid="a")
    _session(projects, "mixed", main, sid="b")
    assert _dirs(projects, main) == ["mixed"]


def test_a_passed_deadline_is_a_verb_timeout(tmp_path: Path) -> None:
    main = _repo(tmp_path)
    projects = tmp_path / "projects"
    _session(projects, "main-dir", main)
    with pytest.raises(VerbTimeout):
        _dirs(projects, main, deadline=time.monotonic() - 1)


def test_a_repo_root_that_is_not_a_repository_is_refused(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    projects.mkdir()
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ValueError, match="not a git repository"):
        _dirs(projects, plain)
