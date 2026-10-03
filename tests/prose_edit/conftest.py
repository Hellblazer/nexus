# SPDX-License-Identifier: AGPL-3.0-or-later
"""Harness for the prose-edit memory.py tests (RDR-221 Step 1.2, nexus-ger02.1).

Every test drives the real script as a subprocess against the real engine
substrate (the tenant `t2_service_env` mints per test). T2 state is verified
in-process through `t2_handle`, independently of memory.py.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest

from nexus.commands._helpers import t2_handle

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".claude" / "skills" / "prose-edit" / "scripts" / "memory.py"
SPY = Path(__file__).with_name("nxspy.py")
PREFIX = "pt_"
REPO_NAME = "proj-x"
USER_PROJECT = f"{PREFIX}prose"
REPO_PROJECT = f"{PREFIX}{REPO_NAME}_prose"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


def make_repo(root: Path) -> Path:
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "docs").mkdir()
    (root / "docs" / "x.md").write_text("one\ntwo\nthree\nfour\nfive\n")
    git(root, "add", "docs/x.md")
    git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / REPO_NAME)


class Prose:
    """Runs memory.py; `calls()` returns every nx argv it issued."""

    def __init__(self, cwd: Path, env: dict[str, str], calls: Path) -> None:
        self.cwd, self.env, self.calls_file = cwd, env, calls

    @property
    def tmp(self) -> Path:
        """This test's own TMPDIR: work directories made by the scripts land here, and nowhere else."""
        return Path(self.env["TMPDIR"])

    def run(self, *args: str, stdin: str | dict | list | None = None,
            cwd: Path | None = None, env: dict[str, str] | None = None
            ) -> subprocess.CompletedProcess[str]:
        data = json.dumps(stdin) if isinstance(stdin, (dict, list)) else stdin
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args], input=data or "",
            capture_output=True, text=True, cwd=cwd or self.cwd,
            env=env or self.env, timeout=180,
        )

    def popen(self, *args: str, stdin: dict | list, cwd: Path,
              env: dict[str, str] | None = None) -> subprocess.Popen[str]:
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPT), *args], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd,
            env=env or self.env,
        )
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(stdin))
        proc.stdin.close()
        proc.stdin = None  # communicate() would otherwise try to flush the closed pipe
        return proc

    def ok(self, *args: str, **kw) -> dict:
        proc = self.run(*args, **kw)
        assert proc.returncode == 0, f"{args}: rc={proc.returncode}\n{proc.stderr}"
        return json.loads(proc.stdout)

    def promote(self, doc: str, n: int, level: str) -> dict:
        """The author's route: a dry run (shown to the author), then the real promote. The script refuses the
        real one without a matching dry run, so a test that is not about that gate goes through here."""
        self.ok("promote", doc, str(n), "--level", level, "--dry-run")
        return self.ok("promote", doc, str(n), "--level", level)

    def calls(self) -> list[list[str]]:
        if not self.calls_file.exists():
            return []
        return [json.loads(line) for line in self.calls_file.read_text().splitlines()]


@pytest.fixture
def prose(repo: Path, tmp_path: Path, t2_service_env: str) -> Prose:
    calls = tmp_path / "nx-calls.jsonl"
    env = os.environ.copy()
    env["NEXUS_CONFIG_DIR"] = str(tmp_path / "cfg")
    # A private temp directory (outside the repo, tmp_path/REPO_NAME): the scripts make their work
    # directories under TMPDIR, so a test that counts them must not share the machine's.
    (tmp_path / "tmp").mkdir()
    env["TMPDIR"] = str(tmp_path / "tmp")
    # The child `nx` is a dev-checkout process, so the production-write guard needs an
    # explicit reason (it is deliberately not inherited from the suite's own opt-in).
    # Grant it only after proving the child points at the hermetic loopback engine
    # with a minted token: t2_service_env supplies both, and this fails loudly if it
    # ever stops, instead of silently authorising writes to a real store.
    host = urlparse(env.get("NX_SERVICE_URL", "")).hostname
    assert host in ("127.0.0.1", "localhost", "::1"), f"child NX_SERVICE_URL is not loopback: {host!r}"
    assert env.get("NX_SERVICE_TOKEN"), "child has no minted tenant token"
    env["NX_ALLOW_PROD_WRITE"] = (
        "prose-edit memory.py tests (nexus-ger02.1): writes to the hermetic "
        "t2_service_env test engine only, never production"
    )
    env["PROSE_EDIT_PROJECT_PREFIX"] = PREFIX
    env["PROSE_EDIT_NX"] = f"{sys.executable} {SPY}"
    env["PROSE_EDIT_NX_CALLS"] = str(calls)
    env["PROSE_EDIT_TEST"] = "1"
    env["PROSE_EDIT_NOW"] = "2026-09-29T17:15:03.482913Z"
    return Prose(repo, env, calls)


def t2_titles(project: str) -> list[str]:
    with t2_handle() as db:
        return sorted(e["title"] for e in db.memory.list_entries(project=project))


def t2_get(project: str, title: str) -> dict | None:
    with t2_handle() as db:
        return db.memory.get(project=project, title=title)


def t2_row(project: str, title: str) -> dict:
    row = t2_get(project, title)
    assert row is not None, f"{project}/{title} not in T2"
    return row


def t2_put(project: str, title: str, content: str) -> None:
    """Write a raw body straight to T2, bypassing memory.py (to plant a malformed record)."""
    with t2_handle() as db:
        db.memory.put(project=project, title=title, content=content, tags="", ttl=None)


def t2_json(project: str, title: str) -> dict:
    row = t2_get(project, title)
    assert row is not None, f"{project}/{title} not in T2"
    return json.loads(row["content"])
