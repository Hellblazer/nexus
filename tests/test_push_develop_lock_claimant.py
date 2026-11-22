# SPDX-License-Identifier: AGPL-3.0-or-later
"""The push lock's claimant in scripts/git-push-develop.sh (nexus-agctp; RDR-253 Phase 2, nexus-hczdo.2.1).

The claimant names the session pushing: ``NX_SESSION_ID``, else ``CLAUDE_CODE_SESSION_ID`` (the env half of
``nexus.session.resolve_session_id``; the retired ``current_session`` file is never read), else ``unknown`` with an
``(unresolved)`` suffix. The script runs the real lock path against a stand-in ``nx`` that records the ``--claimant`` it
was handed, so what is asserted is what a peer's ``PUSH_REFUSED_LOCK_HELD`` would name, with no engine needed.
"""
from __future__ import annotations

import os
import re
import shlex
import socket
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "git-push-develop.sh"
_ID = ["-c", "user.email=t@t", "-c", "user.name=t"]

#: A stand-in installed ``nx``: ``tuple in`` answers a claim and logs its ``--claimant``; everything else succeeds.
_NX_STANDIN = """#!/usr/bin/env bash
if [[ "$1" == "tuple" && "$2" == "in" ]]; then
  prev=""
  for a in "$@"; do
    if [[ "$prev" == "--claimant" ]]; then printf '%s\\n' "$a" >> {log}; fi
    prev="$a"
  done
  printf '{{"claim_id": "c-1"}}\\n'
fi
exit 0
"""


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *_ID, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def work(tmp_path: Path) -> Path:
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", "-q", "--bare", "--initial-branch=develop", cwd=origin)
    wt = tmp_path / "work"
    wt.mkdir()
    _git("init", "-q", "--initial-branch=develop", cwd=wt)
    _git("remote", "add", "origin", str(origin), cwd=wt)
    (wt / "base").write_text("base")
    _git("add", "base", cwd=wt)
    _git("commit", "-q", "-m", "base", cwd=wt)
    _git("push", "-q", "-u", "origin", "develop", cwd=wt)
    (wt / "mine").write_text("mine")
    _git("add", "mine", cwd=wt)
    _git("commit", "-q", "-m", "mine", cwd=wt)
    return wt


def _lock_run(work: Path, tmp_path: Path, **session_env: str) -> tuple[subprocess.CompletedProcess, str]:
    """Run the script's lock path with exactly *session_env* as the session variables; return the result and the
    claimant the stand-in ``nx`` was handed."""
    standin = tmp_path / "standin"
    standin.mkdir()
    log = tmp_path / "claimants.log"
    nx = standin / "nx"
    nx.write_text(_NX_STANDIN.format(log=shlex.quote(str(log))))
    nx.chmod(0o755)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("NX_PUSH_") and k not in ("NX_SESSION_ID", "CLAUDE_CODE_SESSION_ID")}
    env["PATH"] = f"{standin}{os.pathsep}{env.get('PATH', '')}"
    env["NX_PUSH_SKIP_SCOPE_AUDIT"] = "claimant test, scope not under test"
    env["NX_PUSH_SKIP_SCRUB"] = "claimant test, scrub not under test"
    env.update(session_env)
    sha = _git("rev-parse", "HEAD", cwd=work)
    proc = subprocess.run([str(SCRIPT), sha], cwd=work, env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert log.exists(), "the stand-in nx never saw a lock claim: " + proc.stderr
    claimants = log.read_text(encoding="utf-8").splitlines()
    assert len(claimants) == 1, claimants
    return proc, claimants[0]


def _claimed_line_claimant(proc: subprocess.CompletedProcess) -> str:
    lines = [ln for ln in proc.stderr.splitlines() if ln.startswith("PUSH_LOCK_CLAIMED")]
    assert len(lines) == 1, proc.stderr
    return lines[0].split("claimant=", 1)[1]


def _host() -> str:
    return socket.gethostname().split(".")[0]


class TestThePushLockClaimant:
    def test_claude_code_session_id_names_the_claimant(self, work, tmp_path) -> None:
        proc, claimant = _lock_run(work, tmp_path, CLAUDE_CODE_SESSION_ID="cc-sess-1")
        assert re.fullmatch(r"cc-sess-1@.+#\d+", claimant), claimant
        assert "(unresolved)" not in claimant
        assert _claimed_line_claimant(proc) == claimant

    def test_nx_session_id_wins_over_claude_code_session_id(self, work, tmp_path) -> None:
        proc, claimant = _lock_run(work, tmp_path, NX_SESSION_ID="nx-sess-2", CLAUDE_CODE_SESSION_ID="cc-sess-1")
        assert re.fullmatch(r"nx-sess-2@.+#\d+", claimant), claimant
        assert "cc-sess-1" not in claimant
        assert _claimed_line_claimant(proc) == claimant

    def test_no_session_in_the_env_is_unknown_and_says_unresolved(self, work, tmp_path) -> None:
        proc, claimant = _lock_run(work, tmp_path)
        assert re.fullmatch(r"unknown@.+#\d+ \(unresolved\)", claimant), claimant
        assert _claimed_line_claimant(proc) == claimant

    def test_the_claimant_names_this_host(self, work, tmp_path) -> None:
        _proc, claimant = _lock_run(work, tmp_path, CLAUDE_CODE_SESSION_ID="cc-sess-1")
        assert f"@{_host()}#" in claimant

    def test_a_retired_current_session_file_names_nothing(self, work, tmp_path) -> None:
        """The machine-wide file the script used to fall back to is never read, whatever it holds."""
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        (cfg / "current_session").write_text("file-session-9", encoding="utf-8")
        proc, claimant = _lock_run(work, tmp_path, NEXUS_CONFIG_DIR=str(cfg))
        assert "file-session-9" not in claimant
        assert claimant.startswith("unknown@")
