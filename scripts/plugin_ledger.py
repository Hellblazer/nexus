# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The PENDING_RELEASE drift-ledger parsers.

Moved out of the anchored-tag cut script (nexus-rbmja.2), since retired
(nexus-rbmja.8), so the drift-ledger test does not depend on it. The RDR-228
Phase 2 guard that used to sit beside them was pruned by nexus-rbmja.9: Phase 2
shipped in v7.81.0, so the last client tag (v7.97.0) already carries it and the
guard had nothing left to refuse.

Under the plugin-only fast lane the ledger is the cherry-pick list, and an
entry under "Deferred to the next client release" still means "do not
cherry-pick this one".
"""

from __future__ import annotations

import re

from plugin_channel import is_channel_path

_is_allowlisted = is_channel_path


def _is_continuation(line: str) -> bool:
    return line.startswith("  ") or line.startswith("\t")


def _ledger_blocks(ledger_text: str) -> list[tuple[bool, list[str]]]:
    """Group the ledger into (is_bullet_block, lines) runs.

    A blank line INSIDE a bullet stays with it when the next non-blank
    line is an indented continuation — a CommonMark paragraph break
    within one list item (R2 finding: treating the blank as an
    unconditional boundary split a two-paragraph entry, judged its first
    half covered, erased it, and orphaned the second half's wheel-half
    warning and `bead:` marker).
    """
    lines = ledger_text.splitlines(keepends=True)
    blocks: list[tuple[bool, list[str]]] = []
    i = 0
    total = len(lines)
    while i < total:
        line = lines[i]
        if not line.startswith("- "):
            blocks.append((False, [line]))
            i += 1
            continue
        entry = [line]
        i += 1
        while i < total:
            nxt = lines[i]
            if _is_continuation(nxt):
                entry.append(nxt)
                i += 1
            elif nxt.strip() == "":
                j = i
                while j < total and lines[j].strip() == "":
                    j += 1
                if j < total and _is_continuation(lines[j]):
                    entry.extend(lines[i:j])
                    i = j
                else:
                    break
            else:
                break
        blocks.append((True, entry))
    return blocks


#: The ledger heading that marks the deferral section (nexus-2x3qy).
#: Membership is by the LAST "## " heading seen before an entry -- any
#: other heading, or none yet, means the entry is not deferred. Matched
#: on the heading's own text (after "## "), so a bead-scoped suffix a
#: human appends does not break recognition.
DEFERRED_SECTION_HEADING = "Deferred to the next client release"

_HEADING_RE = re.compile(r"^##\s+(.*\S)\s*$")


def _deferred_entries(ledger_text: str) -> list[str]:
    """Path-carrying entries sitting under :data:`DEFERRED_SECTION_HEADING`.

    A deferred entry is one the releaser does not cherry-pick into a
    plugin-only release, and :func:`deferred_paths` reads that set. Reuses
    ``_ledger_blocks`` so section membership and entry boundaries come
    from the one grouping.
    """
    deferred: list[str] = []
    in_section = False
    for is_bullet, lines in _ledger_blocks(ledger_text):
        if not is_bullet:
            for line in lines:
                heading = _HEADING_RE.match(line.rstrip("\n"))
                if heading:
                    in_section = heading.group(1).strip() == DEFERRED_SECTION_HEADING
            continue
        text = "".join(lines)
        has_path = any("/" in span for span in re.findall(r"`([^`]+)`", text))
        if in_section and has_path:
            deferred.append(text)
    return deferred


def deferred_paths(ledger_text: str) -> set[str]:
    """Channel paths a DEFERRED entry says must not ship in this release.

    Only allowlisted (channel) spans count -- a deferred entry's wheel
    half was never going to ship regardless of deferral, and a bare
    prefix root in prose (``conexus/``) is not a path.
    """
    paths: set[str] = set()
    for entry in _deferred_entries(ledger_text):
        for span in re.findall(r"`([^`]+)`", entry):
            span = span.strip()
            if "/" in span.strip("/") and _is_allowlisted(span):
                paths.add(span)
    return paths
