# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Repo-wide lint: no backslash continuation may land on a comment line.

The defect class (shipped in ``376115c1``, tests/e2e/local-service-gate.sh):
a line ending in an odd number of backslashes continues the logical line, and
bash strips backslash-newline BEFORE comment tokenization — so a continued
line whose next physical line is a comment terminates the logical command at
the ``#``. An env-prefix like::

    NX_SERVICE_HOST=127.0.0.1 NX_SERVICE_PORT="$PORT" \\
      # some comment
      uv run pytest ...

silently degrades into plain unexported shell-variable assignments, and the
command on the following line runs env-less. In the gate script that meant
pytest lost NX_SERVICE_* and the NEXUS_CONFIG_DIR scratch pin: service-
resolving tests skipped (nightly skips 21 -> 28 vs budget 25, two red
nightlies 2026-08-01/02) or — worse — could resolve the operator's LIVE
service through the uid-scoped ServiceRegistry lease fallback.

``bash -n`` cannot catch this (it is valid syntax). shellcheck flags it only
obliquely (SC2034 unused-variable fallout). This scan is the direct tripwire:
zero real occurrences across every tracked shell script, enforced with a
detector that is itself tested against the historical defect shape.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _continuation_into_comment(lines: list[str]) -> list[int]:
    """Return 1-based line numbers of comment lines that terminate a live
    backslash continuation.

    A continuation is live when the PREVIOUS line ends in an odd number of
    backslashes and is not itself a comment line (a backslash at the end of a
    comment is inert prose — bash comments end at the newline, so nothing
    continues; see expectations.sh:366 for the benign shape).
    """
    hits: list[int] = []
    for i in range(1, len(lines)):
        prev = lines[i - 1].rstrip("\n")
        stripped_prev = prev.rstrip()
        trailing = len(stripped_prev) - len(stripped_prev.rstrip("\\"))
        if trailing % 2 == 1 and not prev.lstrip().startswith("#") \
                and lines[i].lstrip().startswith("#"):
            hits.append(i + 1)
    return hits


def _tracked_shell_scripts() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "*.sh"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return [REPO_ROOT / p for p in out.splitlines() if p]


def test_detector_catches_the_376115c1_shape() -> None:
    """Falsification control: the detector must flag the exact historical
    defect — env prefix, continued line, comment where the command belongs."""
    bad = (
        'NX_SERVICE_HOST=127.0.0.1 NX_SERVICE_PORT="$SERVICE_PORT" \\\n'
        '  NEXUS_CONFIG_DIR="$SCRATCH" \\\n'
        "  # --color=no: this output is PARSED\n"
        '  uv run pytest -q "$@"\n'
    ).splitlines(keepends=True)
    assert _continuation_into_comment(bad) == [3]


def test_detector_ignores_backslash_inside_comment_prose() -> None:
    """The expectations.sh benign shape: a backslash ending a COMMENT line is
    prose, not a continuation, and must not be flagged."""
    benign = (
        "#   CLASSIFIED reported=N blocked_resolved=N \\\n"
        "#       wouldblock=N no_terminal=N\n"
    ).splitlines(keepends=True)
    assert _continuation_into_comment(benign) == []


def test_detector_ignores_escaped_backslash_at_eol() -> None:
    """An even number of trailing backslashes is an escaped backslash, not a
    continuation."""
    benign = (
        'printf "a\\\\\\\\"\n'
        "# a comment\n"
    ).splitlines(keepends=True)
    assert _continuation_into_comment(benign) == []


def test_no_tracked_shell_script_continues_into_a_comment() -> None:
    scripts = _tracked_shell_scripts()
    # Non-vacuity: the sweep must actually be sweeping something. The repo
    # carries dozens of shell scripts; an empty list means the enumeration
    # broke, not that the tree went shell-free.
    assert len(scripts) >= 10, f"suspicious sweep: only {len(scripts)} scripts enumerated"

    violations: list[str] = []
    for script in scripts:
        lines = script.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        for lineno in _continuation_into_comment(lines):
            violations.append(f"{script.relative_to(REPO_ROOT)}:{lineno}")

    assert not violations, (
        "backslash continuation lands on a comment line (the 376115c1 defect "
        "class — the continued command silently ends at the '#', and any "
        "env-prefix on it degrades to unexported assignments): "
        + ", ".join(violations)
    )
