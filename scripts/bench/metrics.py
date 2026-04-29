# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scoring metrics for the RDR-090 retrieval bench.

These were inline in ``scripts/spikes/spike_rdr090_5q.py``. Moving them
into a module so the harness, the spike, and the test suite all agree on
the arithmetic. The spike is left untouched so its on-disk numbers stay
reproducible without an import chain into the moving target.

Conventions:

  * ``ground_truth`` maps an RDR-file basename substring (e.g.
    ``"rdr-049-"``) to a relevance grade in {0, 1, 2, 3}, where 3 is
    most relevant. A retrieved chunk's ``source_path`` is matched by
    substring; the highest grade wins when multiple keys match.
  * ``dedupe_by_doc`` collapses chunks to first-rank-per-document so a
    single highly-relevant doc with many chunks can't inflate DCG.
  * ``ndcg_at_k`` returns 0.0 when IDCG is empty (no relevant docs in
    GT) — the conventional handling.
  * ``multi_hop_precision`` is defined for compositional queries only:
    fraction of *required* (grade >= 2) GT keys that have at least one
    matching retrieved path. Returns ``None`` when GT has no required
    keys (the metric is undefined for that query).
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

# Multi-hop precision considers a GT key "required" when its grade is at
# or above this threshold. Adjacent (grade=1) keys are background, not
# part of the chain we need to reconstruct.
MULTI_HOP_REQUIRED_GRADE = 2


def dedupe_by_doc(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse chunks to first-rank-per-document.

    Keep the highest-ranked (first-encountered) chunk per
    ``source_path`` and drop the rest. Empty source_path is treated as
    a single bucket — multiple unknown-source chunks collapse into one.
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for c in chunks:
        sp = c.get("source_path", "")
        if sp in seen:
            continue
        seen.add(sp)
        out.append(c)
    return out


def grade_for_path(source_path: str, gt: dict[str, int]) -> int:
    """Return the highest GT grade matching ``source_path``'s basename, else 0."""
    if not source_path:
        return 0
    name = Path(source_path).name
    best = 0
    for key, grade in gt.items():
        if key in name and grade > best:
            best = grade
    return best


def ndcg_at_k(grades: list[int], gt: dict[str, int], k: int = 3) -> float:
    """NDCG@k.

    ``grades`` is the ranked list of relevance grades for the top-k
    retrieved (deduped) items; ``gt`` provides the per-doc GT used to
    compute IDCG. Empty IDCG returns 0.0 by convention.
    """
    grades = grades[:k]
    dcg = sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(grades))
    ideal = sorted(gt.values(), reverse=True)[:k]
    idcg = sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(ideal))
    if idcg == 0:
        return 0.0
    return dcg / idcg


def multi_hop_precision(
    retrieved_paths: list[str],
    gt: dict[str, int],
    *,
    required_grade: int = MULTI_HOP_REQUIRED_GRADE,
) -> float | None:
    """Fraction of required GT keys hit by at least one retrieved path.

    "Required" means grade >= ``required_grade`` (default 2). For
    compositional queries this captures whether the retriever
    reconstructs the multi-document chain — partial chains score below
    1.0 even when an individual hop is highly ranked.

    Returns ``None`` when GT has no required keys (the metric is
    undefined for queries that aren't compositional).
    """
    required_keys = [k for k, g in gt.items() if g >= required_grade]
    if not required_keys:
        return None
    basenames = [Path(p).name for p in retrieved_paths if p]
    hits = sum(
        1 for key in required_keys
        if any(key in name for name in basenames)
    )
    return hits / len(required_keys)
