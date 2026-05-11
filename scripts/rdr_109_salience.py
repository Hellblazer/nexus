# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-109 Phase 4 prototype: salient-sentence extraction + token-overlap boost.

Phase 5 will ship the production version (aspect extractor wired into
the indexer pipeline + ``search_cross_corpus`` integration). This
module is the prototype Phase 4 needs to MEASURE the boost effect
against held-out QA before committing the design to production.

Two stages:

1. :func:`extract_salient_sentences` — for a chunk, score every
   sentence against the content-type's seed queries via the Phase 3
   cross-encoder; retain the top-N as salience candidates.
2. :func:`token_overlap_boost` — at search time, compute Jaccard-style
   token overlap between the user query and a chunk's salient
   sentences. Apply ``weight * overlap_fraction`` as an additive
   boost.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

# Sentence segmentation is intentionally simple here: regex on
# sentence-terminal punctuation. Calibration runs on prose / docs /
# rdr / code; the boost mechanism's outcome doesn't depend on
# perfect sentence boundaries, just on having candidate text spans.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[`*_])")
_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def split_sentences(text: str, *, max_sentence_chars: int = 600) -> list[str]:
    """Split *text* into sentence-shaped spans.

    Drops empty pieces and truncates over-long spans (defensive cap;
    a 5000-char run-on doesn't get a meaningful single CE score).
    """
    raw = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    return [s[:max_sentence_chars] for s in raw if s]


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def load_seed_queries(seed_dir: Path, content_type: str) -> list[str]:
    """Load seed queries for *content_type* from
    ``data/calibration/rdr-109/seed_queries_<content_type>.json``."""
    path = seed_dir / f"seed_queries_{content_type}.json"
    payload = json.loads(path.read_text())
    return list(payload["seeds"])


def extract_salient_sentences(
    chunk_text: str,
    seed_queries: list[str],
    *,
    top_n: int = 3,
    cross_encoder=None,
) -> list[str]:
    """Return the top-N salient sentences from *chunk_text* per the
    seed queries.

    For each sentence, take the max CE score across all seed queries
    (max-pool over the seed set). Sort sentences by max-pooled score,
    keep top-N. Stable secondary order is original position.
    """
    sentences = split_sentences(chunk_text)
    if not sentences:
        return []
    if cross_encoder is None:
        from nexus.cross_encoder import get_local_cross_encoder  # noqa: PLC0415
        cross_encoder = get_local_cross_encoder()

    # Per-sentence max-pooled score: max over seed queries.
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
    0.0 when *salient_sentences* is empty or *query* tokenises empty.
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
    "load_seed_queries",
    "split_sentences",
    "token_overlap_boost",
]
