# SPDX-License-Identifier: AGPL-3.0-or-later
"""Salience extraction + retrieval boost (RDR-109 Phase 5).

The production version of the Phase-4 prototype that previously lived
under ``scripts/rdr_109_salience.py``. Two stages:

1. :func:`extract_salient_sentences` runs at index time (or via the
   ``attention-guided-v1`` extractor): for a document, score every
   sentence against the content-type's seed queries via the Phase 3
   cross-encoder substrate, retain the top-N as salience candidates.
   Results land in ``DocumentAspects.salient_sentences``.
2. :func:`token_overlap_boost` runs at search time: compute fractional
   token overlap between the user query and the stored salient
   sentences for a candidate chunk; apply ``weight * overlap_fraction``
   as an additive boost.

Both stages are pure functions; the I/O wiring (extractor registration
in ``aspect_extractor.py``, retrieval boost in ``search_engine.py``)
lives at the callsites.

Per Phase 4b measurement outcome (recorded in RDR-109's revisions
section, run date 2026-05-11), the recommended default weight when
enabled is ``0.025`` with the mechanism gated behind
``.nexus.yml``'s ``attention_guided_v1.enabled`` flag (default
False).
"""
from __future__ import annotations

import re
from typing import Iterable, Protocol

# Sentence segmentation: regex on sentence-terminal punctuation. The
# boost doesn't need perfect boundaries, just stable candidate text
# spans, so a defensive cap on per-sentence chars keeps long run-ons
# from dominating the cross-encoder cost.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[`*_])")
_TOKEN_RE = re.compile(r"[a-z0-9_]+")


class _ScorerProtocol(Protocol):
    def score(self, query: str, documents: list[str]) -> list[float]: ...


def split_sentences(text: str, *, max_sentence_chars: int = 600) -> list[str]:
    """Split *text* into sentence-shaped spans.

    Drops empty pieces and truncates over-long spans (defensive cap;
    a 5000-char run-on doesn't get a meaningful single CE score).
    """
    raw = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    return [s[:max_sentence_chars] for s in raw if s]


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def extract_salient_sentences(
    text: str,
    seed_queries: list[str],
    *,
    top_n: int = 3,
    cross_encoder: _ScorerProtocol | None = None,
) -> list[str]:
    """Return the top-N salient sentences from *text*.

    For each sentence the score is the max over seed queries (max-pool).
    Sort sentences by score descending, keep top-N, restore original
    order so the output reads naturally.

    *cross_encoder* defaults to :func:`nexus.cross_encoder.get_local_cross_encoder`
    which is imported lazily so the module is cheap to load.
    """
    sentences = split_sentences(text)
    if not sentences:
        return []
    if cross_encoder is None:
        from nexus.cross_encoder import get_local_cross_encoder  # noqa: PLC0415
        cross_encoder = get_local_cross_encoder()

    max_scores = [float("-inf")] * len(sentences)
    for query in seed_queries:
        scores = cross_encoder.score(query, sentences)
        for i, s in enumerate(scores):
            if s > max_scores[i]:
                max_scores[i] = s

    indexed = list(enumerate(max_scores))
    indexed.sort(key=lambda p: (-p[1], p[0]))
    keep = sorted(idx for idx, _ in indexed[:top_n])
    return [sentences[i] for i in keep]


def token_overlap_boost(
    query: str,
    salient_sentences: Iterable[str],
    *,
    weight: float,
) -> float:
    """Return the additive boost for a chunk given its salient
    sentences.

    Score = ``weight * |Q & S| / max(1, |Q|)`` where Q is the query
    token set and S is the union of salient-sentence tokens. Returns
    0.0 on empty inputs or zero weight (cheap short-circuit).
    """
    if weight == 0:
        return 0.0
    qt = _tokens(query)
    if not qt:
        return 0.0
    salient_union: set[str] = set()
    for s in salient_sentences:
        salient_union |= _tokens(s)
    if not salient_union:
        return 0.0
    overlap = len(qt & salient_union)
    return weight * (overlap / max(1, len(qt)))


__all__ = [
    "extract_salient_sentences",
    "split_sentences",
    "token_overlap_boost",
]
