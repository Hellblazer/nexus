# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.

"""The release battery refuses a tree a peer can move underneath it (nexus-57cvk).

AGENTS.md § Worktrees rule 4 says the battery runs in the release worktree
and never the primary. Rule 9 obliges whoever pushes to ``develop`` to
fast-forward the primary in the same breath. Both were followed on
2026-09-22 and 7.57.0's battery died on its twelfth leg with a tree-identity
mismatch: eleven legs green, nothing wrong with the code, ~70 minutes gone.

The rule was amended in ``f00b12bbd``. nexus-57cvk's own closing question
was whether the battery should REFUSE or merely prefer, observing that "the
first is enforceable and the second is advice, and this rule set's own
history says advice decays". This tests the enforceable half.

THE GUARD IS EXTRACTED FROM THE REAL SCRIPT, never retyped here. That is
this repo's existing pattern for shape checks over shell
(``extract_release_native_build_argv`` in
``scripts/check_release_workflow_shape.py``, pinned by its own test) and it
exists because a retyped copy passes happily while the original drifts. If
either marker comment disappears, :func:`_extract_guard` fails loudly
rather than silently testing nothing.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = pathlib.Path(__file__).parent.parent
BATTERY = REPO_ROOT / "tests" / "e2e" / "release-battery.sh"

_BEGIN = ">>> BEGIN moving-tree guard (nexus-57cvk)"
_END = "<<< END moving-tree guard (nexus-57cvk)"


def _extract_guard() -> str:
    """The guard block, verbatim, from the script that actually runs it."""
    text = BATTERY.read_text()
    start = text.find(_BEGIN)
    end = text.find(_END)
    assert start != -1, (
        f"marker {_BEGIN!r} is gone from {BATTERY}; the guard moved or was deleted"
    )
    assert end > start, (
        f"marker {_END!r} is gone from {BATTERY}; the guard moved or was deleted"
    )
    block = text[text.index("\n", start) + 1 : end]
    block = block.rsplit("\n", 1)[0]  # drop the trailing comment-opening line
    assert "BATTERY REFUSED" in block, (
        "extracted block does not contain the refusal; extraction is wrong"
    )
    return block


def _git(repo: pathlib.Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, timeout=60
    )


@pytest.fixture
def repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """A real git repo on ``develop`` with one commit."""
    root = tmp_path / "primary"
    root.mkdir()
    _git(root, "init", "-q", "-b", "develop")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    (root / "f").write_text("x")
    _git(root, "add", "f")
    _git(root, "commit", "-q", "-m", "c")
    return root


def _run_guard(
    repo: pathlib.Path, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    script = "set -uo pipefail\n" + _extract_guard() + '\necho "GUARD ALLOWED"\n'
    return subprocess.run(
        ["bash", "-c", script],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", **(env_extra or {})},
    )


def test_refuses_on_develop_when_a_peer_worktree_exists(
    repo: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """The measured 7.57.0 condition: develop, plus another worktree."""
    _git(repo, "worktree", "add", "-q", "-b", "peer", str(tmp_path / "peer"))

    result = _run_guard(repo)

    assert result.returncode == 2, (
        f"expected refusal, got rc={result.returncode}: {result.stdout}{result.stderr}"
    )
    assert "BATTERY REFUSED" in result.stderr
    assert "GUARD ALLOWED" not in result.stdout
    # The message has to name the remedy, not just the refusal.
    assert "release worktree" in result.stderr.lower()


def test_allows_a_lone_checkout_on_develop(repo: pathlib.Path) -> None:
    """No peers means no rule-9 pusher, so nothing can move the tree.

    This is the false-positive case Sam flagged on the bead: a refusal here
    would block a release on a box where the hazard cannot occur.
    """
    result = _run_guard(repo)

    assert result.returncode == 0, f"lone checkout was refused: {result.stderr}"
    assert "GUARD ALLOWED" in result.stdout


def test_allows_a_release_branch_even_with_peers(
    repo: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """The sanctioned shape: a release worktree, which develop cannot move."""
    _git(repo, "worktree", "add", "-q", "-b", "peer", str(tmp_path / "peer"))
    _git(repo, "checkout", "-q", "-b", "release/v9.9.9")

    result = _run_guard(repo)

    assert result.returncode == 0, f"release branch was refused: {result.stderr}"
    assert "GUARD ALLOWED" in result.stdout


def test_override_is_honoured(repo: pathlib.Path, tmp_path: pathlib.Path) -> None:
    """A deliberate non-release sweep can still run, loudly opted in."""
    _git(repo, "worktree", "add", "-q", "-b", "peer", str(tmp_path / "peer"))

    result = _run_guard(repo, {"NX_BATTERY_ALLOW_DEVELOP": "1"})

    assert result.returncode == 0, f"override did not work: {result.stderr}"
    assert "GUARD ALLOWED" in result.stdout


def test_guard_is_wired_into_the_battery_before_any_work() -> None:
    """Position matters: refusing after leg 0 would have paid for artifacts.

    The guard must sit ahead of the ``WORK``/``LOGS`` setup, which is the
    first thing that creates state.
    """
    text = BATTERY.read_text()
    assert text.index(_BEGIN) < text.index('WORK="/tmp/nxb-'), (
        "the moving-tree guard runs after the battery starts building state; "
        "it has to refuse before anything is paid for"
    )
