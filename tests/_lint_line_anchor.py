# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Content-addressed line anchors for line-keyed lint exemption ledgers
(nexus-vkpr3).

THE DEFECT THIS CLOSES. Several repo-wide lint ratchets in this directory
carry an EXEMPTION SET keyed by ``"<path>:<lineno>"`` (or a ``(path,
lineno)`` tuple): a documented, reviewed reason for NOT flagging one
specific violation site. Keying by line number is silently wrong the
moment anything is inserted ABOVE the exempted line -- the number no
longer names the site it was written for, it names whatever code now
happens to sit at that number. Two confirmed incidents on the same file,
same day (nexus-vkpr3, 2026-09-12/13): inserting 11-13 and then 17 more
lines above a block of twelve pipefail-lint exemptions re-pointed every
one of them onto a different line. Both times the sweep's own "still a
live violation" check caught the shift LOUDLY -- but the recovery it
demanded (retarget every entry to its new line) only avoided silently
mis-exempting a new, unreviewed site because the entries were matched
old-to-new BY CONTENT, not by an arithmetic line offset. An offset that
happens to be right for MOST entries is exactly the failure mode this
closes: the ones it gets wrong are silent, and the wrong direction is
the dangerous one -- a real, never-reviewed violation landing under an
exemption whose rationale was written for different code entirely, with
the gate staying green throughout.

THE FIX. Key an exemption on the VIOLATING LINE'S OWN CONTENT -- one or
more consecutive stripped source lines, in file order, ending at the
target line -- instead of its line number. A pure insertion above the
site changes no line's TEXT, so the anchor keeps matching the same
physical code after the shift; `resolve_anchor` below re-derives the
CURRENT line number from the content every time a ledger is checked, so
no stored number is ever load-bearing between runs. An anchor that no
longer occurs anywhere in the file (the line was fixed, deleted, or
rewritten) fails loud as STALE; one that occurs more than once fails
loud as AMBIGUOUS, rather than silently guessing which occurrence the
entry meant.

WHY A MULTI-LINE BLOCK, NOT JUST A LONGER SUBSTRING. A single stripped
line is not always unique within a file on its own (e.g. ``head -1)"``
alone recurs roughly five times in one real migration-rehearsal script)
-- widening to a longer SUBSTRING of that same single line is still
line-scoped and just as capable of coincidentally matching a different,
unrelated occurrence of the identical line elsewhere in the file.
Widening to a CONTIGUOUS BLOCK of consecutive lines (the target line
plus however many immediately preceding lines it takes to become
unique) stays anchored to the surrounding code's own content, which
shifts by the same delta as the target line under any insertion above
it, so the block keeps matching as one unit after the shift. Measured
against every entry converted by this bead: a same-file duplicate
single line was disambiguated by its immediately preceding line in
every real case found (see nexus-vkpr3's commit for the sites).
"""
from __future__ import annotations

from pathlib import Path

_NO_FILE = "no such file: {path}"
_EMPTY = "empty content anchor for {path}"
_STALE = (
    "content no longer found in {path} (STALE exemption -- the line was "
    "fixed, deleted, or rewritten; drop the entry): {content!r}"
)
_AMBIGUOUS = (
    "content occurs {n} times in {path} at lines {lines} (AMBIGUOUS "
    "exemption -- widen the anchor with a leading line of unique "
    "context instead of guessing which occurrence was meant): {content!r}"
)


def resolve_anchor(
    repo_root: Path, rel_path: str, content_lines: tuple[str, ...],
) -> tuple[int | None, str]:
    """Resolve *content_lines* -- one or more consecutive STRIPPED source
    lines, in file order, ending at the exempted line -- to that line's
    CURRENT 1-based line number inside *rel_path* (relative to
    *repo_root*).

    Returns ``(lineno, "")`` on exactly one match. Returns ``(None,
    reason)`` when the file does not exist, the block occurs nowhere in
    the file (STALE), or it occurs more than once (AMBIGUOUS) -- a
    caller must treat either failure as loud, never as "assume the
    first match" or "assume the site is still clean".
    """
    if not content_lines:
        return None, _EMPTY.format(path=rel_path)
    full = repo_root / rel_path
    if not full.is_file():
        return None, _NO_FILE.format(path=rel_path)
    lines = full.read_text(encoding="utf-8", errors="replace").splitlines()
    stripped = [ln.strip() for ln in lines]
    n = len(content_lines)
    matches = [
        i + n  # 1-based lineno of the LAST line in the window
        for i in range(len(stripped) - n + 1)
        if tuple(stripped[i:i + n]) == content_lines
    ]
    if not matches:
        return None, _STALE.format(path=rel_path, content=content_lines)
    if len(matches) > 1:
        return None, _AMBIGUOUS.format(
            path=rel_path, n=len(matches), lines=matches, content=content_lines,
        )
    return matches[0], ""
