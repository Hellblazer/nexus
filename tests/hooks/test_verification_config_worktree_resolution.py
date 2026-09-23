# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""`.nexus.yml` follows the REPO, not the checkout (bead nexus-634ye).

The file is gitignored by design — ``docs/configuration.md`` says "It is
gitignored by default" — so there is exactly one per repo and it lives in
the primary checkout. The reader (then ``read_verification_config.py``,
now ``nexus.hooks.verification_config``) resolved it from the process cwd, so in a linked worktree it found nothing and returned
DEFAULTS, which for the verification section means ``on_stop`` and
``on_close`` both false. The close gate was therefore silently off in
every worktree, and this project moved every session into one on
2026-09-19.

It survived that move because nothing tested it. The symptom is silence,
and silence is also what a correctly-passing gate produces, so there was
nothing to notice. These tests are built on a REAL git worktree rather
than a monkeypatched path lookup, because the thing that was wrong was
the relationship between two checkouts, and a stub of that relationship
would have been written with the same wrong assumption.

Note what the fix does NOT do: it does not make an unarmed gate loud.
Three separate conditions still disable this gate identically and
without comment — this one, the wrong-tier wiring (bead nexus-17i1n),
and an unreachable T1 (a deliberate fail-open). Only the second is
fixed; this is the first.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

#: The wheel reader, run in a fresh interpreter so the cwd is real. The
#: plugin-resident script it was ported from is deleted (nexus-z9cz2).
_READER_SRC = (
    "import json;"
    "from nexus.hooks.verification_config import read_verification_config;"
    "print(json.dumps(read_verification_config()))"
)

_ARMED_YML = """\
verification:
  on_stop: true
  on_close: true
  test_timeout: 300
"""


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


def _read_config_from(cwd: Path, env: dict[str, str] | None = None) -> dict:
    """Run the reader as a hook would: a separate process, with a cwd."""
    proc = subprocess.run(
        [sys.executable, "-c", _READER_SRC],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, **(env or {})},
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.fixture
def repo_with_worktree(tmp_path: Path):
    """A primary checkout carrying an armed `.nexus.yml`, plus a linked worktree.

    The worktree deliberately does NOT get a copy, because that is the
    real situation: the file is gitignored, so it is never checked out
    into one.
    """
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")

    primary = tmp_path / "primary"
    primary.mkdir()
    _git("init", "-b", "main", ".", cwd=primary)
    _git("config", "user.email", "t@t.invalid", cwd=primary)
    _git("config", "user.name", "T", cwd=primary)
    _git("config", "commit.gpgsign", "false", cwd=primary)
    (primary / ".gitignore").write_text(".nexus.yml\n")
    (primary / "pyproject.toml").write_text("[project]\nname='x'\n")
    _git("add", ".gitignore", "pyproject.toml", cwd=primary)
    _git("commit", "-m", "init", cwd=primary)

    (primary / ".nexus.yml").write_text(_ARMED_YML)

    worktree = tmp_path / "wt"
    _git("worktree", "add", str(worktree), "-b", "feature/x", cwd=primary)
    assert not (worktree / ".nexus.yml").exists(), (
        "the worktree carries a .nexus.yml, so this fixture is not "
        "reproducing the gitignored-file situation it exists to reproduce"
    )
    return primary, worktree


def test_the_primary_reads_its_own_config(repo_with_worktree) -> None:
    """Baseline. If this fails the fixture is wrong, not the resolution."""
    primary, _ = repo_with_worktree
    config = _read_config_from(primary)
    assert config["on_close"] is True
    assert config["test_timeout"] == 300


def test_a_worktree_reads_the_primarys_config(repo_with_worktree) -> None:
    """The defect. Before the fix this returned DEFAULTS, on_close false."""
    _, worktree = repo_with_worktree
    config = _read_config_from(worktree)
    assert config["on_close"] is True, (
        "a worktree fell back to DEFAULTS — the close gate is off in every "
        "worktree, which is where every session now works"
    )
    assert config["on_stop"] is True
    assert config["test_timeout"] == 300


def test_a_repo_with_no_config_still_gets_defaults(tmp_path: Path) -> None:
    """The fix must not invent config where there is none.

    Non-vacuity for the two tests above: if the reader returned an armed
    config unconditionally they would pass and mean nothing.
    """
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    plain = tmp_path / "plain"
    plain.mkdir()
    _git("init", "-b", "main", ".", cwd=plain)
    config = _read_config_from(plain)
    assert config["on_close"] is False
    assert config["on_stop"] is False


def test_a_directory_outside_any_git_repo_still_answers(tmp_path: Path) -> None:
    """No git, no repo, no crash. A hook must never fail on its own lookup."""
    loose = tmp_path / "loose"
    loose.mkdir()
    config = _read_config_from(loose)
    assert config["on_close"] is False


def test_an_explicit_project_dir_still_wins(repo_with_worktree, tmp_path: Path) -> None:
    """CLAUDE_PROJECT_DIR is checked first and keeps its meaning."""
    _, worktree = repo_with_worktree
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / ".nexus.yml").write_text(
        "verification:\n  on_close: false\n  test_timeout: 999\n"
    )
    config = _read_config_from(worktree, env={"CLAUDE_PROJECT_DIR": str(elsewhere)})
    assert config["test_timeout"] == 999, (
        "an explicit CLAUDE_PROJECT_DIR carrying a .nexus.yml was overridden "
        "by the git-common-dir fallback; the fallback is a LAST resort"
    )
    assert config["on_close"] is False


def test_a_project_dir_without_a_config_does_not_shadow_the_repos(
    repo_with_worktree, tmp_path: Path
) -> None:
    """The subtle half: first candidate that HAS one, not first that exists.

    A worktree always exists and never carries the file. Returning the
    first *existing* directory is precisely the bug — so an explicit
    CLAUDE_PROJECT_DIR pointing at a config-less directory must fall
    through too, not shadow the repo that does have one.
    """
    _, worktree = repo_with_worktree
    empty = tmp_path / "empty"
    empty.mkdir()
    config = _read_config_from(worktree, env={"CLAUDE_PROJECT_DIR": str(empty)})
    assert config["on_close"] is True


def test_the_config_is_read_with_no_plugin_and_an_unexpanded_root(monkeypatch) -> None:
    """The nexus-b5ugt defect, under the only condition that shows it.

    Two things had to be true at once and a dev checkout supplies
    neither for free.

    (1) ``conexus/.mcp.json`` declares the MCP servers' env as
    ``{"CLAUDE_PLUGIN_ROOT": "${CLAUDE_PLUGIN_ROOT}"}`` and Claude Code
    does not expand ``${...}`` in an MCP ``env`` block, so every nx-mcp
    process carries that literal. Measured on every nx-mcp on the dev
    box, in three separate repositories.

    (2) The checkout-relative fallback misses. Installed, this module
    lives in site-packages and the anchor lands in the interpreter's lib
    directory; in a checkout it lands on the real ``conexus/`` and
    rescues the lookup.

    The first draft of this test asserted only (1) and PASSED against
    the unfixed code — verified by reverting both files and watching it
    stay green — because (2) is false in the tree the suite runs in. A
    test of a two-condition defect that supplies one condition measures
    the checkout, not the code. ``checkout_plugin_root`` is stubbed to a
    path that does not exist, which is exactly what being installed
    means here.
    """
    from nexus.hooks import _plugin

    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "${CLAUDE_PLUGIN_ROOT}")
    monkeypatch.setattr(
        _plugin, "checkout_plugin_root", lambda: Path("/nonexistent/not-a-checkout")
    )

    from nexus.hooks.stop_verification import _read_config

    config = _read_config()
    assert config, (
        "the verification config came back empty with no reachable plugin — "
        "this is the shape in which on_stop read false and the session-end "
        "gate verified nothing"
    )
    assert "on_stop" in config and "on_close" in config


def test_a_diagnostic_never_lands_on_stdout(tmp_path) -> None:
    """stdout is the hook decision channel, and this module logs.

    Caught by a real failure while writing the port: a module-level
    structlog logger writes to stdout by default, so the
    ``verification_config_absent`` debug line landed ahead of the JSON
    envelope and the decision would not parse. Pinned by value because
    the correct behaviour and the broken one differ only in which
    stream a line went to, which no assertion about the return value
    can see.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json;from nexus._hook_runtime._io import never_fail;"
            "from nexus.hooks import stop_verification;"
            "r=never_fail(lambda: stop_verification.run({'session_id':'x'}), 's');"
            "print(r.stdout)",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(tmp_path)},
    )
    assert proc.returncode == 0, proc.stderr
    # Every line of stdout must be the envelope, nothing else.
    for line in proc.stdout.splitlines():
        if line.strip():
            json.loads(line)
