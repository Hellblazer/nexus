# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""scripts/agent-worktree-preflight.sh registers the worktree as its identity's work (RDR-253 Step 6, nexus-hczdo.1.9).

A worktree an agent works in is work its identity owns; ``nx identity work register --kind worktree`` labels it so that
``nx identity may-stop`` keeps another session from removing it. The label is best effort in every part: a
PREFLIGHT_OK stays a PREFLIGHT_OK, with the same single line and exit 0, whether nx is missing, fails, or hangs.
Real git worktrees under tmp_path; ``nx`` is a stand-in script that records its arguments."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "agent-worktree-preflight.sh"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.email=t@example.com", "-c", "user.name=T",
                           "-c", "commit.gpgsign=false", *args], check=True, capture_output=True, text=True).stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "f").write_text("1")
    _git(path, "add", "f")
    _git(path, "commit", "-q", "-m", "c1")
    return path


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    main = _repo(tmp_path / "main")
    _git(main, "branch", "-m", "develop")
    wt = tmp_path / "wt" / "agent"
    _git(main, "worktree", "add", "-q", str(wt), "-b", "feature/x")
    return wt


def fake_nx(tmp_path: Path, body: str) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "nx.log"
    nx = bin_dir / "nx"
    nx.write_text(f'#!/bin/sh\necho "$*" >> "{log}"\n{body}\n')
    nx.chmod(nx.stat().st_mode | stat.S_IXUSR)
    return bin_dir, log


def preflight(wt: Path, path: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(SCRIPT)], cwd=str(wt), capture_output=True, text=True,
                          env={**os.environ, "PATH": path}, timeout=60)


def with_nx(bin_dir: Path) -> str:
    return f"{bin_dir}{os.pathsep}{os.environ['PATH']}"


def test_a_passing_preflight_registers_the_worktree_by_its_real_path(worktree: Path, tmp_path: Path) -> None:
    bin_dir, log = fake_nx(tmp_path, "exit 0")
    done = preflight(worktree, with_nx(bin_dir))
    assert done.returncode == 0 and done.stdout.startswith("PREFLIGHT_OK head="), done
    assert done.stdout.count("\n") == 1 and done.stderr == "", "the label adds no output"
    assert log.read_text().splitlines() == [f"identity work register --kind worktree --ref {worktree.resolve()}"]


@pytest.mark.parametrize("body", ["exit 1", "echo boom >&2; exit 7", "sleep 30"], ids=["fails", "noisy-failure", "hangs"])
def test_a_label_that_fails_or_hangs_never_turns_the_ok_into_a_failure(worktree: Path, tmp_path: Path, body: str) -> None:
    if body.startswith("sleep") and not shutil.which("timeout"):
        pytest.skip("no timeout(1) on this box: the hang bound only exists where timeout does")
    bin_dir, _log = fake_nx(tmp_path, body)
    done = preflight(worktree, with_nx(bin_dir))
    assert done.returncode == 0 and done.stdout.startswith("PREFLIGHT_OK head=") and done.stdout.count("\n") == 1, done
    assert done.stderr == "", "a failing label is silent"


def test_without_nx_the_registration_is_skipped_and_the_ok_stands(worktree: Path, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    done = preflight(worktree, f"{empty}{os.pathsep}/usr/bin{os.pathsep}/bin")        # git and bash are there, nx is not
    assert done.returncode == 0 and done.stdout.startswith("PREFLIGHT_OK"), done


def test_a_refused_preflight_registers_nothing(tmp_path: Path) -> None:
    """The primary checkout is refused (exit 2) before any label: only a passing preflight registers."""
    main = _repo(tmp_path / "main")
    bin_dir, log = fake_nx(tmp_path, "exit 0")
    done = preflight(main, with_nx(bin_dir))
    assert done.returncode == 2 and "PREFLIGHT_FAIL_PRIMARY_CHECKOUT" in done.stdout
    assert not log.exists()
