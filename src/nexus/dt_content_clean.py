# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Boilerplate stripping for DT-extracted web content (nexus-mok9x).

``nx dt index --dt-content`` used to write DEVONthink's AI-extracted text
VERBATIM into the indexed cache. For web archives that text carries the whole
cookie-consent wall and inline ``data:`` image payloads. Measured on tumbler
1.12.102 (CACM "Formal Reasoning Meets LLMs"): 59% of the 80,670 indexed
chars were Cookiebot output and base64 image data; 32 of 70 chunks matched a
cookie-tracking query, and the junk was MORE retrievable than the article
(cookie query at distance 0.275 vs the paper's own subject at 0.394). The
noise also aggregates corpus-wide: the 2026-07-27 taxonomy rebuild grew ~3
spurious topics (~15% of the collection) out of markup/boilerplate residue.

Two passes, both pure and independently testable:

1. **Inline data URIs** — the base64 payload of any ``data:...;base64,...``
   token is replaced with a short placeholder. Payloads embed no searchable
   content and each one burns embedding spend.
2. **Cookie-consent runs** — Cookiebot's output is highly regular ("Maximum
   Storage Duration" / "Type: HTTP Cookie" line pairs, per-vendor cookie
   tables). Lines matching a small signature vocabulary are marked, and
   CONTIGUOUS RUNS (small gaps allowed) containing at least
   :data:`_MIN_RUN_SIGNATURES` signature lines are dropped whole. The run
   threshold is what keeps an article that merely DISCUSSES cookies intact —
   prose mentions do not form dense multi-signature runs.

The caller (``_index_dt_content_record``) logs the stripped ratio on every
clean and warns loudly past :data:`BOILERPLATE_WARN_RATIO` — silent quality
loss is the part of the incident that let 59% junk ship unnoticed.

KNOWN RESIDUALS (both reviewers, 2026-08-31):

- A scholarly work that REPRODUCES an actual cookie table (not isolated
  mentions — a quoted, signature-dense block) has that block stripped like
  the real thing, and if the block is small the whole-document warn ratio
  stays quiet. Accepted: this module is scoped to the DT web-archive path,
  where signature-dense runs are overwhelmingly CMP furniture.
- The vocabulary covers Cookiebot (measured) plus the OneTrust/Quantcast/
  Didomi names and OneTrust's category labels. A CMP outside that set
  passes through UNDETECTED AND UNWARNED — ``stripped_ratio`` reads 0,
  indistinguishable from clean. There is no general-purpose boilerplate
  oracle here by design; extend ``_SIGNATURES`` when a new wall is
  measured (the run mechanism generalizes, only the vocabulary is vendor-
  specific).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: Stripped-fraction threshold above which ingest should warn loudly.
BOILERPLATE_WARN_RATIO: float = 0.30

#: Minimum signature lines a contiguous run must contain to be dropped.
#: Three is deliberately conservative: Cookiebot tables produce dozens per
#: screenful, while an article discussing cookie consent in prose produces
#: isolated matches that never cluster.
_MIN_RUN_SIGNATURES: int = 3

#: Non-matching lines tolerated INSIDE a run before it is considered ended
#: (cookie tables interleave names/values with signature lines).
_MAX_RUN_GAP: int = 3

#: Case-insensitive substrings that mark a line as cookie-consent furniture.
#: Sourced from the measured Cookiebot output plus its standard category
#: labels; deliberately specific multi-word phrases, never bare "cookie".
_SIGNATURES: tuple[str, ...] = (
    "maximum storage duration",
    "type: http cookie",
    "type: html local storage",
    "type: pixel tracker",
    "type: ifab registry",
    "cookiebot",
    "cookie declaration",
    "we use cookies",
    "use of cookies",
    "necessary cookies",
    "preference cookies",
    "statistics cookies",
    "marketing cookies",
    "unclassified cookies",
    "consent selection",
    "withdraw your consent",
    "cookie policy",
    # Other common CMPs (critic fold 2026-08-31 — the Cookiebot-only
    # vocabulary left OneTrust/Quantcast/Didomi walls both undetected and
    # unwarned): vendor names plus OneTrust's standard category labels.
    "onetrust",
    "quantcast",
    "didomi",
    "we and our partners",
    "strictly necessary cookies",
    "performance cookies",
    "functional cookies",
    "targeting cookies",
    "manage consent preferences",
    "your privacy choices",
)

_DATA_URI = re.compile(
    r"data:[\w/+.-]+;base64,[A-Za-z0-9+/=]{64,}",
)

_PLACEHOLDER = "[inline data removed]"


@dataclass(frozen=True, slots=True)
class CleanResult:
    text: str
    original_chars: int
    stripped_chars: int
    data_uris_removed: int
    consent_runs_removed: int

    @property
    def stripped_ratio(self) -> float:
        if self.original_chars == 0:
            return 0.0
        return self.stripped_chars / self.original_chars


def _is_signature_line(line: str) -> bool:
    low = line.lower()
    return any(sig in low for sig in _SIGNATURES)


def _drop_consent_runs(lines: list[str]) -> tuple[list[str], int]:
    """Remove contiguous signature-dense runs; returns (kept, runs_removed)."""
    n = len(lines)
    drop = [False] * n
    runs_removed = 0
    i = 0
    while i < n:
        if not _is_signature_line(lines[i]):
            i += 1
            continue
        # Grow a run from i: include subsequent lines while the gap between
        # signature lines stays within _MAX_RUN_GAP.
        run_start = i
        last_sig = i
        signatures = 1
        j = i + 1
        while j < n and (j - last_sig) <= _MAX_RUN_GAP:
            if _is_signature_line(lines[j]):
                last_sig = j
                signatures += 1
            j += 1
        if signatures >= _MIN_RUN_SIGNATURES:
            for k in range(run_start, last_sig + 1):
                drop[k] = True
            runs_removed += 1
        i = last_sig + 1
    kept = [line for keep_i, line in enumerate(lines) if not drop[keep_i]]
    return kept, runs_removed


def clean_dt_content(text: str) -> CleanResult:
    """Strip inline data-URI payloads and cookie-consent runs from *text*."""
    original_chars = len(text)

    data_uris_removed = 0

    def _sub(match: re.Match[str]) -> str:
        nonlocal data_uris_removed
        data_uris_removed += 1
        return _PLACEHOLDER

    without_uris = _DATA_URI.sub(_sub, text)

    lines = without_uris.split("\n")
    kept, consent_runs_removed = _drop_consent_runs(lines)
    cleaned = "\n".join(kept)

    return CleanResult(
        text=cleaned,
        original_chars=original_chars,
        stripped_chars=original_chars - len(cleaned),
        data_uris_removed=data_uris_removed,
        consent_runs_removed=consent_runs_removed,
    )
