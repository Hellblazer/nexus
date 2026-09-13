# SPDX-License-Identifier: AGPL-3.0-or-later
"""``.beads/PRIME.md`` overrides the beads plugin's bundled ``bd prime``
output for this repo (nexus-cnzei.2 item 4).

Installed ``bd`` (Homebrew 1.0.5) resolves ``.beads/PRIME.md`` relative to
cwd BEFORE any global default, and REPLACES the whole ``bd prime`` output.
So a worktree or a fresh clone only gets this override if the file is
actually committed, not merely present in one developer's checkout
(``.beads/.gitignore`` does not exclude it). Without this file, the
plugin's bundled prime text tells the model "Do NOT use MEMORY.md files",
shows a bare ``git add . && git commit`` example, and defaults to a
conservative no-commit/no-push posture that contradicts this repo's
AGENTS.md workflow and the T2/auto-memory split Sam ruled on 2026-09-13
(memory_get project=nexus title=bd-prime-analysis-2026-09-13).

This is a static-content lint, not a `bd` behavior test: it does not shell
out to `bd prime` (that would pin a specific installed `bd` version's
lookup semantics, which have already drifted once between 1.0.5 and
1.2.x, see the T2 record above). It only asserts the FILE ITSELF is
present, tracked, small, and free of the specific wrong content the
plugin default carries.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PRIME_MD = PROJECT_ROOT / ".beads" / "PRIME.md"

#: Well under the plugin's own bundled default (6.5 KB) and small enough
#: that nobody is tempted to restate a workflow instead of pointing at it.
_MAX_BYTES = 1024


def test_prime_md_exists() -> None:
    assert PRIME_MD.exists(), (
        ".beads/PRIME.md is missing -- without it, every session in this "
        "repo gets the beads plugin's bundled bd prime text instead "
        "(nexus-cnzei.2 item 4)"
    )


def test_prime_md_is_git_tracked() -> None:
    """A committed file, not one developer's untracked convenience. A
    fresh clone or an isolation worktree resolves ``.beads/PRIME.md``
    relative to ITS OWN cwd, so an untracked file helps only the checkout
    that created it."""
    result = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "ls-files", "--error-unmatch", ".beads/PRIME.md"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f".beads/PRIME.md exists but is not git-tracked: {result.stderr}"
    )


def test_prime_md_stays_under_byte_budget() -> None:
    size = PRIME_MD.stat().st_size
    assert size < _MAX_BYTES, (
        f".beads/PRIME.md is {size} bytes, budget is {_MAX_BYTES} -- this "
        "file replaces the plugin's ENTIRE bd prime output every session; "
        "keep it a pointer, not a restated workflow"
    )


def test_prime_md_never_restates_git_commands() -> None:
    """Git workflow is AGENTS.md's job (hot rule: never `git add -A`/`git
    add .`; push only through scripts/git-push-develop.sh). The plugin
    default's own wrong example was a bare ``git add . && git commit``.
    This file must point at AGENTS.md instead of re-deriving git steps
    that can drift out of sync with it."""
    text = PRIME_MD.read_text(encoding="utf-8")
    for forbidden in ("git add .", "git add -A", "git commit", "git push"):
        assert forbidden not in text, (
            f".beads/PRIME.md restates a git command ({forbidden!r}) instead "
            "of pointing at AGENTS.md"
        )
    assert "AGENTS.md" in text


def test_prime_md_does_not_repeat_the_plugin_defaults_wrong_claims() -> None:
    """The specific wrong claims the plugin's bundled default makes,
    per the 2026-09-13 analysis: no MEMORY.md prohibition, no bd remember
    endorsement, no emoji."""
    text = PRIME_MD.read_text(encoding="utf-8")
    assert "Do NOT use MEMORY.md" not in text
    assert "bd remember" not in text or "not used" in text.lower()
    assert text.isascii(), (
        ".beads/PRIME.md contains a non-ASCII character (emoji included)"
    )
