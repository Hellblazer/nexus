# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-096 spike P2.0: multi-chunk reassembly verification.

Pre-registered acceptance (per nexus-ocu9.2 bead description):

- PASS: ≥3 of 3 papers produce extractable aspects (problem_formulation
  OR proposed_method populated), AND mean operator_verify ≥0.7 on
  signal documents, AND chunk-ordering verification passes (no
  out-of-order chunks in reassembled text).
- FAIL: <2 of 3 with signal, OR ordering verification fails, OR mean
  verify <0.5.
- INCONCLUSIVE: 2 of 3 with signal.

Falsification:
- Reassembled text malformed (visibly out of order despite
  chunk_index sort) → SPIKE FAIL regardless of extraction outcome.
- All 3 papers extract NULL aspects → infrastructure failure, not
  multi-chunk failure (rerun investigation).

Methodology:

1. Pick 3 multi-chunk scholarly papers from knowledge__delos (the only
   target collection with multiple paper-shaped multi-chunk docs;
   knowledge__art and knowledge__agentic-scholar each have a single
   multi-chunk doc which is insufficient diversity).
2. Reassemble each via :func:`nexus.aspect_readers.read_source` with a
   ``chroma://`` URI — exercises the chunk_index sort + concatenation
   + pagination path (273 / 408 / 209 chunks well past
   ``QUOTAS.MAX_QUERY_RESULTS = 300`` for two of the three).
3. Verify ordering: assemble independently via direct chroma query +
   sort, compare hashes against ``read_source`` output.
4. Truncate to 80KB (matches the prior content cap from
   ``_source_content_from_t3``) and pass to ``extract_aspects``.
5. For each populated aspect, formulate a verify claim and run
   ``operator_verify`` against the (truncated) reassembled text.

Outputs:

- ``scripts/spikes/spike_rdr096_multichunk_results.jsonl``
- ``scripts/spikes/spike_rdr096_multichunk_summary.json``

Run with: ``uv run python scripts/spikes/spike_rdr096_multichunk_reassembly.py``
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

OUT_DIR = Path(__file__).parent
RESULTS_PATH = OUT_DIR / "spike_rdr096_multichunk_results.jsonl"
SUMMARY_PATH = OUT_DIR / "spike_rdr096_multichunk_summary.json"

# Three multi-chunk scholarly papers from knowledge__delos. Hand-picked
# from the survey (≥3 chunks each, formal academic structure):
PAPERS = [
    {
        "collection": "knowledge__delos",
        "source_path": "/Users/hal.hildebrand/Downloads/delos-papers/lightweight-smr.pdf",
        "label": "lightweight-smr",
        "expected_chunk_count": 273,
    },
    {
        "collection": "knowledge__delos",
        "source_path": "/Users/hal.hildebrand/Downloads/delos-papers/aleph-bft.pdf",
        "label": "aleph-bft",
        "expected_chunk_count": 408,
    },
    {
        "collection": "knowledge__delos",
        "source_path": "/Users/hal.hildebrand/Downloads/delos-papers/fireflies-tocs.pdf",
        "label": "fireflies-tocs",
        "expected_chunk_count": 209,
    },
]

# Mirrors aspect_extractor._T3_CONTENT_CAP_BYTES.
CONTENT_CAP_BYTES = 80_000


def _independent_reassembly(coll, source_path: str) -> tuple[str, int, list[int]]:
    """Bypass aspect_readers and assemble directly: paginate the
    full collection, filter to source_path, sort by chunk_index,
    join with '\\n\\n'. The text + chunk_index sequence is the
    ground truth the spike compares against.
    """
    from nexus.db.chroma_quotas import QUOTAS

    chunks: list[tuple[int, int, str]] = []  # (chunk_index, insertion_seq, text)
    seq = 0
    offset = 0
    page_limit = QUOTAS.MAX_QUERY_RESULTS
    while True:
        page = coll.get(
            where={"source_path": source_path},
            limit=page_limit,
            offset=offset,
            include=["documents", "metadatas"],
        )
        docs = page.get("documents") or []
        mds = page.get("metadatas") or []
        if not docs:
            break
        for md, doc in zip(mds, docs):
            if not doc:
                continue
            ci = md.get("chunk_index", 0) if isinstance(md, dict) else 0
            chunks.append((int(ci), seq, doc))
            seq += 1
        if len(docs) < page_limit:
            break
        offset += page_limit
    chunks.sort(key=lambda t: (t[0], t[1]))
    text = "\n\n".join(d for _, _, d in chunks)
    chunk_indexes = [ci for ci, _, _ in chunks]
    return text, len(chunks), chunk_indexes


def _aspects_to_claims(record) -> list[str]:
    """Materialize verifiable claims from a populated AspectRecord."""
    claims: list[str] = []
    if record.problem_formulation:
        claims.append(
            f"The paper's problem formulation is: {record.problem_formulation}"
        )
    if record.proposed_method:
        claims.append(
            f"The paper's proposed method is: {record.proposed_method}"
        )
    if record.experimental_results:
        claims.append(
            f"The paper's reported experimental results are: "
            f"{record.experimental_results}"
        )
    if record.experimental_datasets:
        claims.append(
            f"The paper references datasets: {record.experimental_datasets}"
        )
    if record.experimental_baselines:
        claims.append(
            f"The paper references baselines: {record.experimental_baselines}"
        )
    return claims


async def _verify(claims: list[str], evidence: str) -> tuple[int, int]:
    """Run operator_verify per claim. Returns (verified_count, total)."""
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
            print(f"    [warn] operator_verify: {type(e).__name__}: {e}")
    return (verified, len(claims))


def _has_signal(record) -> bool:
    """Pre-reg condition: problem_formulation OR proposed_method populated."""
    pf = record.problem_formulation
    pm = record.proposed_method
    pf_ok = pf is not None and (pf if isinstance(pf, str) else "").strip() not in ("", "null")
    pm_ok = pm is not None and (pm if isinstance(pm, str) else "").strip() not in ("", "null")
    return pf_ok or pm_ok


def main() -> int:
    from nexus.aspect_extractor import extract_aspects
    from nexus.aspect_readers import ReadFail, ReadOk, read_source
    from nexus.mcp_infra import get_t3
    from urllib.parse import quote

    t3 = get_t3()

    rows: list[dict] = []
    docs_with_signal = 0
    verify_scores: list[float] = []
    ordering_failures = 0

    for i, paper in enumerate(PAPERS, 1):
        label = paper["label"]
        collection = paper["collection"]
        sp = paper["source_path"]
        print(f"\n[{i}/{len(PAPERS)}] {label} ({collection})")

        # Reassemble via aspect_readers (the path under test).
        uri = f"chroma://{collection}/{quote(sp, safe='/')}"
        result = read_source(uri, t3=t3)
        if isinstance(result, ReadFail):
            print(f"  read_source FAIL: reason={result.reason}, detail={result.detail}")
            rows.append({
                "label": label,
                "collection": collection,
                "source_path": sp,
                "read_source_failed": True,
                "read_reason": result.reason,
                "skipped": True,
            })
            continue
        assert isinstance(result, ReadOk)
        text_under_test = result.text
        chunk_count = result.metadata["chunk_count"]
        identity_field = result.metadata["identity_field"]
        print(f"  read_source: {chunk_count} chunks, {len(text_under_test)} bytes, identity_field={identity_field}")

        # Independent reassembly for cross-validation.
        coll = t3.get_collection(collection)
        text_truth, count_truth, idx_seq = _independent_reassembly(coll, sp)
        ordering_correct = (
            text_under_test == text_truth
            and chunk_count == count_truth
            and idx_seq == sorted(idx_seq)
        )
        if not ordering_correct:
            ordering_failures += 1
            print(
                f"  ORDERING VERIFICATION FAILED: "
                f"chunk_count={chunk_count} vs truth={count_truth}; "
                f"text_match={text_under_test == text_truth}; "
                f"idx_sorted={idx_seq == sorted(idx_seq)}"
            )
        else:
            print(
                f"  ordering verification PASS "
                f"(idx range [{min(idx_seq)}, {max(idx_seq)}], "
                f"sha256[:12]={hashlib.sha256(text_truth.encode()).hexdigest()[:12]})"
            )

        # Truncate at byte boundary; matches prior content cap.
        truncated = text_under_test
        if len(truncated.encode("utf-8", errors="replace")) > CONTENT_CAP_BYTES:
            truncated = truncated.encode("utf-8", errors="replace")[:CONTENT_CAP_BYTES]\
                .decode("utf-8", errors="ignore")
            print(f"  truncated to {len(truncated)} chars / {CONTENT_CAP_BYTES} bytes")

        # Run extractor.
        record = extract_aspects(
            content=truncated,
            source_path=sp,
            collection=collection,
        )
        if record is None:
            print(f"  extract_aspects: None (no extractor for collection)")
            rows.append({
                "label": label,
                "collection": collection,
                "source_path": sp,
                "chunk_count": chunk_count,
                "ordering_correct": ordering_correct,
                "extract_returned": "None",
                "skipped": True,
            })
            continue

        from nexus.aspect_extractor import ExtractFail
        if isinstance(record, ExtractFail):
            # Shouldn't happen since we passed content non-empty.
            print(f"  extract_aspects: ExtractFail({record.reason}) — UNEXPECTED")
            rows.append({
                "label": label,
                "collection": collection,
                "source_path": sp,
                "chunk_count": chunk_count,
                "ordering_correct": ordering_correct,
                "extract_returned": "ExtractFail",
                "extract_fail_reason": record.reason,
                "skipped": True,
            })
            continue

        signal = _has_signal(record)
        print(f"  signal: {signal}, confidence: {record.confidence}")
        for k in ("problem_formulation", "proposed_method"):
            v = getattr(record, k)
            if v:
                print(f"    {k}: {str(v)[:120]}")

        verify_score = None
        if signal:
            docs_with_signal += 1
            claims = _aspects_to_claims(record)
            print(f"  running operator_verify on {len(claims)} claims...")
            verified, total = asyncio.run(_verify(claims, evidence=truncated))
            verify_score = (verified / total) if total else 0.0
            verify_scores.append(verify_score)
            print(f"  operator_verify: {verified}/{total} = {verify_score:.2f}")

        rows.append({
            "label": label,
            "collection": collection,
            "source_path": sp,
            "chunk_count": chunk_count,
            "reassembled_bytes": len(text_under_test),
            "truncated_bytes": len(truncated),
            "ordering_correct": ordering_correct,
            "identity_field": identity_field,
            "aspects": {
                "problem_formulation": record.problem_formulation,
                "proposed_method": record.proposed_method,
                "experimental_datasets": record.experimental_datasets,
                "experimental_baselines": record.experimental_baselines,
                "experimental_results": record.experimental_results,
                "extras": record.extras,
                "confidence": record.confidence,
            },
            "has_signal": signal,
            "verify_score": verify_score,
            "skipped": False,
        })

    # Verdict.
    n = len(PAPERS)
    mean_verify = sum(verify_scores) / len(verify_scores) if verify_scores else 0.0
    ordering_passed = ordering_failures == 0

    print("\n=== Pre-registered P2.0 verdict ===")
    print(f"  docs with signal: {docs_with_signal}/{n}")
    print(f"  mean operator_verify on signal: {mean_verify:.2f}")
    print(f"  ordering verification: {'PASS' if ordering_passed else 'FAIL'}")

    if not ordering_passed:
        verdict = "FAIL"
        verdict_reason = "ordering verification failed (falsification condition)"
    elif docs_with_signal == n and mean_verify >= 0.7:
        verdict = "PASS"
        verdict_reason = f"3/3 with signal, mean verify {mean_verify:.2f} ≥ 0.7"
    elif docs_with_signal < 2 or mean_verify < 0.5:
        verdict = "FAIL"
        verdict_reason = (
            f"signal {docs_with_signal}/{n} or mean verify {mean_verify:.2f} "
            f"below FAIL threshold"
        )
    else:
        verdict = "INCONCLUSIVE"
        verdict_reason = f"{docs_with_signal}/{n} with signal — middle band"
    print(f"  verdict: {verdict}")
    print(f"  reason: {verdict_reason}")

    with RESULTS_PATH.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, default=str) + "\n")
    summary = {
        "papers": [p["label"] for p in PAPERS],
        "collection": "knowledge__delos",
        "papers_tested": n,
        "docs_with_signal": docs_with_signal,
        "verify_scores": verify_scores,
        "mean_verify_on_signal": mean_verify,
        "ordering_passed": ordering_passed,
        "ordering_failures": ordering_failures,
        "verdict": verdict,
        "verdict_reason": verdict_reason,
        "notes": [
            "All three papers ingested via nx index pdf — chunks "
            "identify via 'source_path' metadata field, NOT 'title'. "
            "P1.1's CHROMA_IDENTITY_FIELD dispatch table updated to "
            "an ordered tuple ('source_path', 'title') for "
            "knowledge__* during this spike's survey.",
            "Two of the three papers (lightweight-smr at 273 chunks, "
            "aleph-bft at 408 chunks) exceed QUOTAS.MAX_QUERY_RESULTS "
            "= 300, exercising the paginated coll.get loop.",
            "Reassembled text is truncated to CONTENT_CAP_BYTES = "
            "80_000 before extract_aspects (mirrors the prior "
            "_T3_CONTENT_CAP_BYTES from _source_content_from_t3). "
            "P2.0 does NOT validate the post-truncation prompt budget; "
            "that is a separate concern for Phase 2's writer contract.",
        ],
    }
    with SUMMARY_PATH.open("w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"\n  Wrote {RESULTS_PATH.name}, {SUMMARY_PATH.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
