# SPDX-License-Identifier: AGPL-3.0-or-later
"""Heredoc pipe-budget tripwire (bead nexus-2gcqk).

Bash 5.3 feeds a heredoc/herestring to its command through an anonymous
PIPE, writing the whole body before the reader can drain it. On macOS the
kernel degrades fresh pipes to a 512-byte buffer once system-wide pipe
memory is under pressure (long-uptime boxes accumulate open pipes), at
which point ANY heredoc body larger than 512 bytes deadlocks forever:
bash blocks in heredoc_write() -> write() and the script never runs.

Measured on macOS 26.5.1 / Homebrew bash 5.3.9 (2026-08-01): a 512-byte
body (including the trailing newline) executes; 513 bytes hangs until
killed. Stock /bin/bash 3.2 uses temp files for heredocs and is immune —
but conexus/hooks/hooks.json launches every shell hook as
``bash $CLAUDE_PLUGIN_ROOT/...``, resolving through PATH to the Homebrew
bash, so production hooks hit the broken path. The 16 uniform 30s
timeouts in test_subagent_stop_hook.py were exactly this: the transcript
scan's 1017-byte python heredoc, deadlocked.

This tripwire pins every heredoc body in the hook scripts (and the e2e
shell lib, which requires bash >= 4 and therefore always runs under the
affected bash) at or below the degraded pipe capacity. Large embedded
programs belong in sibling .py files invoked by path, not in heredocs.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

SCANNED_DIRS = [
    REPO_ROOT / "conexus" / "hooks" / "scripts",
    REPO_ROOT / "sn" / "hooks" / "scripts",
    REPO_ROOT / "tests" / "e2e" / "lib",
]

# macOS's PERMANENT POSIX PIPE_BUF (`getconf PIPE_BUF` == 512), the
# guaranteed atomic-write floor a pipe can degrade to — NOT a transient
# artifact of one box's exhausted state, so do not raise this cap after
# a reboot restores 16KB pipes. Bodies are measured INCLUDING the
# trailing newline, matching what bash writes. 513 bytes is the
# empirically measured hang.
MAX_HEREDOC_BODY_BYTES = 512

# A scan that finds fewer heredocs than this is a broken parser
# skip-passing, not a clean repo (gates carry a non-vacuity assert).
MIN_EXPECTED_HEREDOCS = 10

# Tag may be unquoted, 'single'-, "double"-, or backslash-quoted — all four
# open a heredoc; missing any form is a silent false-negative.
_OPENER = re.compile(r"<<-?\s*['\"\\]?([A-Za-z_][A-Za-z0-9_]*)['\"]?")


def _heredocs(path: Path) -> list[tuple[int, str, int]]:
    """Yield (line_number, tag, body_bytes) for each heredoc in *path*."""
    lines = path.read_text(encoding="utf-8").split("\n")
    found: list[tuple[int, str, int]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _OPENER.search(line)
        if m and "<<<" not in line:
            tag = m.group(1)
            j = i + 1
            body: list[str] = []
            while j < len(lines) and lines[j].rstrip() != tag:
                body.append(lines[j])
                j += 1
            if j < len(lines):  # unterminated "heredoc" = a false match, skip
                size = len(("\n".join(body) + "\n").encode("utf-8"))
                found.append((i + 1, tag, size))
                i = j
        i += 1
    return found


def test_no_heredoc_body_exceeds_degraded_pipe_capacity() -> None:
    total = 0
    violations: list[str] = []
    for d in SCANNED_DIRS:
        assert d.is_dir(), f"scanned dir vanished: {d}"
        for sh in sorted(d.glob("*.sh")):
            for lineno, tag, size in _heredocs(sh):
                total += 1
                if size > MAX_HEREDOC_BODY_BYTES:
                    violations.append(
                        f"{sh.relative_to(REPO_ROOT)}:{lineno} "
                        f"<<{tag} body={size}B (cap {MAX_HEREDOC_BODY_BYTES}B)"
                    )
    assert total >= MIN_EXPECTED_HEREDOCS, (
        f"parser found only {total} heredocs across {SCANNED_DIRS} — "
        "scan is vacuous, fix the parser before trusting this gate"
    )
    assert not violations, (
        "heredoc bodies exceed the degraded macOS pipe capacity and will "
        "deadlock under bash 5.3 when the kernel shrinks pipes to 512B "
        "(move the payload to a sibling file invoked by path):\n  "
        + "\n  ".join(violations)
    )
