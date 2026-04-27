# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-090 spike: 5-query benchmark on rdr__nexus corpus, three paths.

Verifies three RDR-090 critical assumptions before /nx:rdr-gate:

1. Human-labeled GT is feasible at 30-query scale (extrapolated from a
   5-query timed labeling exercise).
2. Path A (naive ``search``) vs. path B (``nx_answer`` plan-first) shows
   measurable NDCG@3 deltas on >=50% of queries.
3. Path C preview — ``nx_answer`` with full-collection scope forces
   plan-library miss and falls through to the inline LLM planner. This
   is operationally equivalent to the not-yet-implemented
   ``force_dynamic=True`` flag. Useful as a path C preview for spike;
   replace with the real flag once it lands post-acceptance.

Path-B leakage finding (registered as research-2 alongside results):
``scope="rdr"`` matched a hardcoded ``corpus="rdr__arcaneum-..."`` plan
on a nexus-corpus question (Q1 dry-run, plan #52 at confidence 0.51).
The spike runs both ``scope="rdr"`` (B) and ``scope="rdr__nexus-..."``
(C) so the leakage shows up as a measurable NDCG@3 delta in the data.

Outputs:
- ``spike_rdr090_5q_results.jsonl`` — per-query, per-path rows
- ``spike_rdr090_5q_summary.json`` — aggregated NDCG@3 + timing

Run with ``uv run python scripts/spikes/spike_rdr090_5q.py``.
"""

from __future__ import annotations

import asyncio
import json
import math
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CORPUS = "rdr__nexus-571b8edd"
K = 3  # NDCG@K, top-K retrieval
OUT_DIR = Path(__file__).parent
RESULTS_PATH = OUT_DIR / "spike_rdr090_5q_results.jsonl"
SUMMARY_PATH = OUT_DIR / "spike_rdr090_5q_summary.json"


@dataclass
class Query:
    """Spike query with manually-labeled relevance grades.

    ``ground_truth`` maps an RDR-file basename substring (e.g.
    ``"rdr-049-"``) to a relevance grade in {0, 1, 2, 3}, where 3 is
    most relevant. Any retrieved chunk whose ``source_path`` matches a
    GT key inherits that key's grade; chunks with no match score 0.
    """

    qid: str
    category: str  # factual | comparative | compositional
    text: str
    ground_truth: dict[str, int] = field(default_factory=dict)


# Five hand-authored queries on rdr__nexus-571b8edd corpus.
# GT grades assigned from project memory (CLAUDE.md + RDR titles known
# cold). Time spent labeling tracked in spike notes.
QUERIES: list[Query] = [
    Query(
        qid="Q1-factual-tumblers",
        category="factual",
        text="Which RDR introduced catalog tumblers as hierarchical document addresses?",
        ground_truth={
            "rdr-049-": 3,  # explicitly introduced tumblers + Nelson principles
            "rdr-053-": 1,  # Xanadu fidelity follow-up
        },
    ),
    Query(
        qid="Q2-factual-chash",
        category="factual",
        text="Which RDR added the content-hash chunk index for stable chunk identity?",
        ground_truth={
            "rdr-086-": 3,  # chash-index introduction
            "rdr-061-": 1,  # earlier content-hash chunk discussion
            "rdr-075-": 1,  # collection routing groundwork
        },
    ),
    Query(
        qid="Q3-factual-taxonomy",
        category="factual",
        text="Which RDR proposed the BERTopic-based taxonomy with HDBSCAN clustering?",
        ground_truth={
            "rdr-070-": 3,  # taxonomy with BERTopic + HDBSCAN
            "rdr-067-": 1,  # adjacent observability/audit work
        },
    ),
    Query(
        qid="Q4-comparative-hooks",
        category="comparative",
        text=(
            "Compare the post-store hook chain mechanisms — single-document "
            "vs batch vs document-grain — and identify which RDR introduced each."
        ),
        ground_truth={
            "rdr-070-": 3,  # single-document hook chain origin
            "rdr-095-": 3,  # batch hook contract
            "rdr-089-": 3,  # document-grain chain (aspect extraction)
            "rdr-086-": 1,  # chash dual-write batch hook
        },
    ),
    Query(
        qid="Q5-compositional-retrieval",
        category="compositional",
        text=(
            "Trace the retrieval-layer evolution: how did plan-first dispatch, "
            "operator extraction, and plan match scope-awareness develop across "
            "the RDR-078 through RDR-095 sequence?"
        ),
        ground_truth={
            "rdr-078-": 3,  # paper audit / retrieval critique
            "rdr-080-": 3,  # retrieval layer consolidation
            "rdr-088-": 3,  # operator additions
            "rdr-091-": 3,  # scope-aware plan matching
            "rdr-092-": 2,  # plan match-text from dimensional identity
            "rdr-079-": 1,  # operator dispatch (abandoned, partial relevance)
            "rdr-093-": 1,  # groupby/aggregate operators
        },
    ),
]


def grade_for_path(source_path: str, gt: dict[str, int]) -> int:
    """Return highest GT grade matching ``source_path`` basename, else 0."""
    if not source_path:
        return 0
    name = Path(source_path).name
    best = 0
    for key, grade in gt.items():
        if key in name and grade > best:
            best = grade
    return best


def dedupe_by_doc(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse chunks to first-rank-per-document.

    Path A and path B both return chunks; GT is per-RDR-file. Multiple
    chunks from the same RDR would otherwise let a single relevant doc
    inflate DCG. Keep the highest-ranked (first-encountered) chunk per
    ``source_path`` and drop the rest.
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


def ndcg_at_k(grades: list[int], gt: dict[str, int], k: int = K) -> float:
    """NDCG@k. ``grades`` is the ranked list of relevance grades for the
    top-k retrieved (deduped) items; ``gt`` is the per-doc GT used to
    compute IDCG.

    Empty IDCG (no relevant docs in GT) returns 0.0 by convention.
    """
    grades = grades[:k]
    dcg = sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(grades))
    ideal = sorted(gt.values(), reverse=True)[:k]
    idcg = sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(ideal))
    if idcg == 0:
        return 0.0
    return dcg / idcg


def run_path_a(query: Query) -> dict[str, Any]:
    """Path A — ``nx search`` CLI restricted to the RDR corpus."""
    t0 = time.monotonic()
    # Over-fetch by 3x so dedupe-by-document still leaves K unique docs
    # in most cases. NDCG@K is then computed on the deduped top-K.
    result = subprocess.run(
        ["nx", "search", query.text, "--corpus", CORPUS, "-m", str(K * 3), "--json"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    elapsed = time.monotonic() - t0
    if result.returncode != 0:
        return {
            "path": "A",
            "qid": query.qid,
            "elapsed_s": elapsed,
            "error": result.stderr.strip(),
            "chunks": [],
            "grades": [],
            "ndcg_at_3": 0.0,
        }
    chunks = json.loads(result.stdout)
    raw = [
        {
            "id": c.get("id", ""),
            "source_path": c.get("source_path", ""),
            "content_hash": c.get("content_hash", ""),
            "distance": c.get("distance", 0.0),
            "section_title": c.get("section_title", ""),
        }
        for c in chunks
    ]
    deduped = dedupe_by_doc(raw)[:K]
    grades = [grade_for_path(c["source_path"], query.ground_truth) for c in deduped]
    return {
        "path": "A",
        "qid": query.qid,
        "elapsed_s": elapsed,
        "error": None,
        "chunks": deduped,
        "raw_chunk_count": len(raw),
        "grades": grades,
        "ndcg_at_3": ndcg_at_k(grades, query.ground_truth),
    }


def _resolve_source_path(t3: Any, collection: str, chash: str) -> str:
    """Look up a chunk's ``source_path`` by ``chunk_text_hash``.

    ``nx_answer``'s structured envelope fills ``chash`` from
    ``chunk_text_hash``, not ``content_hash`` (see ``mcp/core.py``).
    Returns "" on miss.

    ``T3Database`` doesn't expose ``get_collection``; the raw chromadb
    client is at ``t3._client``.
    """
    if not chash or not collection:
        return ""
    try:
        coll = t3._client.get_collection(collection)
        res = coll.get(
            where={"chunk_text_hash": chash}, limit=1, include=["metadatas"]
        )
        metas = res.get("metadatas") or []
        if metas:
            return metas[0].get("source_path", "") or ""
    except Exception as e:
        print(f"  [warn] _resolve_source_path({collection},{chash[:8]}): {e}")
        return ""
    return ""


def run_nx_answer(
    query: Query,
    t3: Any,
    *,
    path_label: str,
    scope: str,
) -> dict[str, Any]:
    """Run ``nx_answer(structured=True)`` and grade the returned chunks.

    ``path_label`` is recorded with the result row ("B" for the
    plan-library-routed case, "C" for the collection-scoped case).
    ``scope`` is forwarded to ``nx_answer``.
    """
    from nexus.mcp.core import nx_answer

    t0 = time.monotonic()
    try:
        envelope = asyncio.run(
            nx_answer(
                question=query.text,
                scope=scope,
                structured=True,
                trace=False,
            )
        )
    except Exception as e:
        return {
            "path": path_label,
            "qid": query.qid,
            "scope": scope,
            "elapsed_s": time.monotonic() - t0,
            "error": f"{type(e).__name__}: {e}",
            "chunks": [],
            "grades": [],
            "ndcg_at_3": 0.0,
            "plan_id": None,
            "step_count": None,
        }
    elapsed = time.monotonic() - t0
    if not isinstance(envelope, dict):
        return {
            "path": path_label,
            "qid": query.qid,
            "scope": scope,
            "elapsed_s": elapsed,
            "error": f"non-envelope response (type={type(envelope).__name__})",
            "chunks": [],
            "grades": [],
            "ndcg_at_3": 0.0,
            "plan_id": None,
            "step_count": None,
        }
    raw_chunks = envelope.get("chunks") or []
    raw = []
    for c in raw_chunks:
        chash = c.get("chash", "")
        coll = c.get("collection", "")
        source_path = _resolve_source_path(t3, coll, chash) if (chash and coll) else ""
        raw.append(
            {
                "id": c.get("id", ""),
                "source_path": source_path,
                "content_hash": chash,
                "collection": coll,
                "distance": c.get("distance", 0.0),
            }
        )
    deduped = dedupe_by_doc(raw)[:K]
    grades = [grade_for_path(c["source_path"], query.ground_truth) for c in deduped]
    return {
        "path": path_label,
        "qid": query.qid,
        "scope": scope,
        "elapsed_s": elapsed,
        "error": None,
        "chunks": deduped,
        "raw_chunk_count": len(raw),
        "grades": grades,
        "ndcg_at_3": ndcg_at_k(grades, query.ground_truth),
        "plan_id": envelope.get("plan_id"),
        "step_count": envelope.get("step_count"),
    }


def run_path_b(query: Query, t3: Any) -> dict[str, Any]:
    """Path B — ``scope="rdr"`` plan-library-routed (captures cross-project leakage)."""
    return run_nx_answer(query, t3, path_label="B", scope="rdr")


def run_path_c(query: Query, t3: Any) -> dict[str, Any]:
    """Path C preview — ``scope="rdr__nexus-571b8edd"`` forces plan-match
    miss → inline LLM planner. Operationally equivalent to the
    not-yet-implemented ``force_dynamic=True``."""
    return run_nx_answer(query, t3, path_label="C", scope=CORPUS)


def main() -> None:
    from nexus.mcp_infra import get_t3

    t3 = get_t3()

    rows: list[dict[str, Any]] = []
    print(f"=== RDR-090 spike: {len(QUERIES)} queries × 3 paths on {CORPUS} ===")
    for q in QUERIES:
        print(f"\n[{q.qid}] {q.category}: {q.text[:80]}")

        a = run_path_a(q)
        rows.append(a)
        print(
            f"  A (search):       NDCG@3={a['ndcg_at_3']:.3f} "
            f"grades={a['grades']} t={a['elapsed_s']:.2f}s"
        )

        b = run_path_b(q, t3)
        rows.append(b)
        if b.get("error"):
            print(f"  B (rdr scope):    ERROR — {b['error']}")
        else:
            print(
                f"  B (rdr scope):    NDCG@3={b['ndcg_at_3']:.3f} "
                f"grades={b['grades']} t={b['elapsed_s']:.2f}s "
                f"plan={b.get('plan_id')} steps={b.get('step_count')}"
            )

        c = run_path_c(q, t3)
        rows.append(c)
        if c.get("error"):
            print(f"  C (coll scope):   ERROR — {c['error']}")
        else:
            print(
                f"  C (coll scope):   NDCG@3={c['ndcg_at_3']:.3f} "
                f"grades={c['grades']} t={c['elapsed_s']:.2f}s "
                f"plan={c.get('plan_id')} steps={c.get('step_count')}"
            )

    with RESULTS_PATH.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    by_path: dict[str, list[dict[str, Any]]] = {"A": [], "B": [], "C": []}
    by_category: dict[str, dict[str, list[float]]] = {}
    for r in rows:
        by_path[r["path"]].append(r)
        cat = next((q.category for q in QUERIES if q.qid == r["qid"]), "unknown")
        by_category.setdefault(cat, {"A": [], "B": [], "C": []})
        if r.get("error") is None:
            by_category[cat][r["path"]].append(r["ndcg_at_3"])

    def _mean(xs: list[float]) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    summary = {
        "corpus": CORPUS,
        "queries": len(QUERIES),
        "k": K,
        "by_path": {
            p: {
                "mean_ndcg_at_3": _mean([r["ndcg_at_3"] for r in rs if r.get("error") is None]),
                "mean_elapsed_s": _mean([r["elapsed_s"] for r in rs]),
                "errors": sum(1 for r in rs if r.get("error")),
                "n": len(rs),
            }
            for p, rs in by_path.items()
        },
        "by_category": {
            cat: {p: _mean(vals) for p, vals in cat_data.items()}
            for cat, cat_data in by_category.items()
        },
        "deltas": [
            {
                "qid": q.qid,
                "category": q.category,
                "ndcg_a": next(
                    (r["ndcg_at_3"] for r in rows if r["qid"] == q.qid and r["path"] == "A"),
                    None,
                ),
                "ndcg_b": next(
                    (r["ndcg_at_3"] for r in rows if r["qid"] == q.qid and r["path"] == "B"),
                    None,
                ),
                "ndcg_c": next(
                    (r["ndcg_at_3"] for r in rows if r["qid"] == q.qid and r["path"] == "C"),
                    None,
                ),
            }
            for q in QUERIES
        ],
    }
    with SUMMARY_PATH.open("w") as f:
        json.dump(summary, f, indent=2)

    print("\n=== Summary ===")
    print(f"Path A mean NDCG@3: {summary['by_path']['A']['mean_ndcg_at_3']:.3f}")
    print(f"Path B mean NDCG@3: {summary['by_path']['B']['mean_ndcg_at_3']:.3f}")
    print(f"Path C mean NDCG@3: {summary['by_path']['C']['mean_ndcg_at_3']:.3f}")
    print(f"Wrote {RESULTS_PATH.name}, {SUMMARY_PATH.name}")


if __name__ == "__main__":
    main()
