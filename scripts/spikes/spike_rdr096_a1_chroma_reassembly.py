# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-096 spike A1: chroma reassembly extractability for scholarly-paper-v1.

Pre-registered acceptance (per nexus_rdr/096-research-1, id=1008):

- A1 PASS: >=4 of 5 documents produce an AspectRecord with at least
  problem_formulation OR proposed_method populated (non-null), AND
  operator_verify agreement >=0.7 on those documents.
- A1 FAIL: <=2 of 5 populate either field, OR operator_verify
  agreement <0.5 on the populated documents.
- A1 INCONCLUSIVE (3 of 5).

Empirical adaptation noted in research-4: knowledge__knowledge
chunks identify documents by metadata field ``title`` (slug), NOT
``source_path`` (which doesn't exist on these chunks). The existing
``_source_content_from_t3`` queries ``where={"source_path": ...}``
and returns empty; that's the actual root cause behind issue #333,
deeper than the issue's reported symptom. This spike reassembles via
``where={"title": ...}`` for knowledge__knowledge specifically.

Procedure:

1. Pick 5 documents from knowledge__knowledge (per pre-reg).
2. Reassemble text via chroma chunk-pagination (single-chunk
   degenerate for these short notes, but the contract still applies).
3. Pass to extract_aspects(content=reassembled, ...,
   collection="knowledge__knowledge").
4. For each non-null aspect field, formulate as a verify claim and
   run operator_verify against the reassembled text.
5. Per-document agreement = verified_count / total_claim_count.

operator_verify returns a binary verified=bool; per-document
agreement is the fraction of claims marked verified — a continuous
score in [0, 1] aligned with the pre-reg's >=0.7 / <0.5 thresholds.

Outputs:
- spike_rdr096_a1_results.jsonl
- spike_rdr096_a1_summary.json

Run with:  uv run python scripts/spikes/spike_rdr096_a1_chroma_reassembly.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

OUT_DIR = Path(__file__).parent
RESULTS_PATH = OUT_DIR / "spike_rdr096_a1_results.jsonl"
SUMMARY_PATH = OUT_DIR / "spike_rdr096_a1_summary.json"

# Five hand-picked documents from knowledge__knowledge — diverse
# content shapes (synthesis, research, analysis, technical, gap
# analysis) to surface variation in scholarly-paper-v1's response.
DOCUMENTS = [
    {
        "title": "bfdb-project-synthesis-2026-03-08",
        "category": "project synthesis",
    },
    {
        "title": "research-bfdb-steel-thread-synthesis",
        "category": "research synthesis",
    },
    {
        "title": "analysis-deep-nexus-arcaneum-pipeline-divergence-2026-03-02",
        "category": "deep analysis",
    },
    {
        "title": "kramer-java25-technical-solutions-v1.0",
        "category": "technical solutions",
    },
    {
        "title": "kramer-gap-analysis-data-model",
        "category": "gap analysis",
    },
]
COLLECTION = "knowledge__knowledge"


def reassemble_via_title(coll, title: str) -> tuple[str, int]:
    """Return (reassembled_text, chunk_count) for a document by its title."""
    chunks: list[tuple[int, str]] = []
    offset = 0
    while True:
        res = coll.get(
            where={"title": title},
            limit=300,
            offset=offset,
            include=["documents", "metadatas"],
        )
        docs = res.get("documents") or []
        mds = res.get("metadatas") or []
        if not docs:
            break
        for md, doc in zip(mds, docs):
            if not doc:
                continue
            ci = md.get("chunk_index", 0) if isinstance(md, dict) else 0
            chunks.append((int(ci), doc))
        if len(docs) < 300:
            break
        offset += 300
    chunks.sort(key=lambda x: x[0])
    text = "\n\n".join(doc for _, doc in chunks)
    return (text, len(chunks))


def aspect_to_claims(aspects: dict) -> list[str]:
    """Materialize verifiable claims from a populated AspectRecord-shaped dict."""
    claims: list[str] = []
    if aspects.get("problem_formulation"):
        claims.append(
            f"The document's problem formulation is: {aspects['problem_formulation']}"
        )
    if aspects.get("proposed_method"):
        claims.append(
            f"The document's proposed method is: {aspects['proposed_method']}"
        )
    if aspects.get("experimental_results"):
        claims.append(
            f"The document's reported experimental results are: "
            f"{aspects['experimental_results']}"
        )
    eds = aspects.get("experimental_datasets")
    if eds and eds != [] and eds != "[]":
        claims.append(f"The document references datasets: {eds}")
    ebs = aspects.get("experimental_baselines")
    if ebs and ebs != [] and ebs != "[]":
        claims.append(f"The document references baselines: {ebs}")
    return claims


async def verify_claims(claims: list[str], evidence: str) -> tuple[int, int]:
    """Run operator_verify on each claim. Return (verified_count, total)."""
    if not claims:
        return (0, 0)
    from nexus.mcp.core import operator_verify

    verified = 0
    for claim in claims:
        try:
            result = await operator_verify(claim=claim, evidence=evidence)
            if isinstance(result, dict) and result.get("verified"):
                verified += 1
        except Exception as e:
            print(f"    [warn] operator_verify failed: {type(e).__name__}: {e}")
    return (verified, len(claims))


def has_signal(aspects: dict) -> bool:
    """Pre-reg condition: problem_formulation OR proposed_method populated."""
    pf = aspects.get("problem_formulation")
    pm = aspects.get("proposed_method")
    pf_ok = pf is not None and (pf if isinstance(pf, str) else "").strip() not in ("", "null")
    pm_ok = pm is not None and (pm if isinstance(pm, str) else "").strip() not in ("", "null")
    return pf_ok or pm_ok


def main() -> int:
    from nexus.aspect_extractor import extract_aspects
    from nexus.mcp_infra import get_t3

    t3 = get_t3()
    coll = t3._client.get_collection(COLLECTION)

    rows: list[dict] = []
    docs_with_signal = 0
    verify_scores_on_signal = []

    for i, doc in enumerate(DOCUMENTS, 1):
        title = doc["title"]
        category = doc["category"]
        print(f"\n[{i}/{len(DOCUMENTS)}] {title} ({category})")

        text, chunk_count = reassemble_via_title(coll, title)
        if not text:
            print(f"  reassembly empty (no chunks for title={title})")
            rows.append({
                "title": title,
                "category": category,
                "chunk_count": 0,
                "reassembled_bytes": 0,
                "aspects": None,
                "has_signal": False,
                "verify_score": None,
                "skipped": True,
                "skip_reason": "no_chunks",
            })
            continue

        print(f"  reassembled {chunk_count} chunks → {len(text)} bytes")

        record = extract_aspects(
            content=text,
            source_path=title,
            collection=COLLECTION,
        )
        if record is None:
            print("  extract_aspects returned None (no extractor for collection)")
            rows.append({
                "title": title,
                "category": category,
                "chunk_count": chunk_count,
                "reassembled_bytes": len(text),
                "aspects": None,
                "has_signal": False,
                "verify_score": None,
                "skipped": True,
                "skip_reason": "no_extractor",
            })
            continue

        aspects = {
            "problem_formulation": record.problem_formulation,
            "proposed_method": record.proposed_method,
            "experimental_datasets": record.experimental_datasets,
            "experimental_baselines": record.experimental_baselines,
            "experimental_results": record.experimental_results,
            "extras": record.extras,
            "confidence": record.confidence,
        }
        signal = has_signal(aspects)
        print(f"  has_signal: {signal}  confidence: {aspects['confidence']}")
        for k in ("problem_formulation", "proposed_method"):
            v = aspects.get(k)
            if v:
                print(f"    {k}: {str(v)[:100]}")

        verify_score = None
        if signal:
            docs_with_signal += 1
            claims = aspect_to_claims(aspects)
            print(f"  running operator_verify on {len(claims)} claims...")
            verified, total = asyncio.run(verify_claims(claims, evidence=text))
            verify_score = (verified / total) if total else 0.0
            verify_scores_on_signal.append(verify_score)
            print(f"  operator_verify: {verified}/{total} = {verify_score:.2f}")

        rows.append({
            "title": title,
            "category": category,
            "chunk_count": chunk_count,
            "reassembled_bytes": len(text),
            "aspects": aspects,
            "has_signal": signal,
            "verify_score": verify_score,
            "skipped": False,
        })

    n = len(DOCUMENTS)
    mean_verify_on_signal = (
        sum(verify_scores_on_signal) / len(verify_scores_on_signal)
        if verify_scores_on_signal else 0.0
    )

    print("\n=== Pre-registered A1 verdict ===")
    print(f"  docs with signal: {docs_with_signal}/{n}")
    print(f"  mean verify on signal: {mean_verify_on_signal:.2f}")

    if docs_with_signal >= 4 and mean_verify_on_signal >= 0.7:
        verdict = "PASS"
    elif docs_with_signal <= 2 or mean_verify_on_signal < 0.5:
        verdict = "FAIL"
    else:
        verdict = "INCONCLUSIVE"
    print(f"  verdict: {verdict}")

    with RESULTS_PATH.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, default=str) + "\n")
    summary = {
        "collection": COLLECTION,
        "documents_tested": n,
        "docs_with_signal": docs_with_signal,
        "verify_scores_on_signal": verify_scores_on_signal,
        "mean_verify_on_signal": mean_verify_on_signal,
        "verdict": verdict,
        "notes": [
            "knowledge__knowledge identifies documents by metadata 'title' "
            "field, not 'source_path'; existing _source_content_from_t3 "
            "querying where={'source_path':...} returns empty for these "
            "collections (root cause behind issue #333).",
            "Documents in this collection are mostly single-chunk short "
            "notes; multi-chunk reassembly contract is exercised by paper "
            "collections (knowledge__art / knowledge__delos), not tested "
            "here.",
        ],
    }
    with SUMMARY_PATH.open("w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"\n  Wrote {RESULTS_PATH.name}, {SUMMARY_PATH.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
