# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-096 spike A2: source_path → file:// URI backfill round-trip cleanliness.

Verifies the second of three RDR-096 critical assumptions before
/nx:rdr-gate. Pre-registered acceptance:

- A2 PASS if >=99% of rows round-trip cleanly across all collections
  AND every non-round-tripping case has a documented mitigation.
- A2 FAIL if <95% round-trip or any non-round-tripping case has no
  obvious mitigation.
- A2 BORDERLINE (95-99%) ships with a fallback (rows that don't
  round-trip retain source_path; source_uri populated separately
  later).

Procedure (per nexus_rdr/096-research-1, id=1008):

1. Enumerate every unique source_path across:
   a. document_aspects rows (already-extracted documents)
   b. T3 chunk metadata (broader scope — every indexed chunk)
2. For each: compute uri = file:// + os.path.abspath(source_path);
   reverse via urlparse + unquote; compare recovered_path against
   the original.
3. Categorize non-round-trip cases by reason.

Outputs:
- spike_rdr096_a2_results.jsonl — per-row data
- spike_rdr096_a2_summary.json — aggregated counts + per-collection
  + per-reason breakdown

Run with:  uv run python scripts/spikes/spike_rdr096_a2_backfill_roundtrip.py
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlparse

OUT_DIR = Path(__file__).parent
RESULTS_PATH = OUT_DIR / "spike_rdr096_a2_results.jsonl"
SUMMARY_PATH = OUT_DIR / "spike_rdr096_a2_summary.json"

# Sample size cap per collection during the T3 sweep — 600 is enough
# to surface common-case failures without spending hours on 60K-chunk
# code corpora. Long tail can be audited separately at backfill time.
T3_SAMPLE_PER_COLLECTION = 600


def round_trip(source_path: str) -> tuple[str, str, bool, str]:
    """Compute uri + recovered_path + ok + reason for a source_path.

    Returns (uri, recovered_path, ok, reason). ``reason`` is empty
    when ok is True; a short label otherwise.
    """
    if not source_path:
        return ("", "", False, "empty_source_path")

    if not source_path.startswith("/"):
        # Relative path — convert via os.path.abspath. This depends
        # on CWD; backfill should use the catalog's original ingest
        # path. Flag as a known-needs-mitigation case.
        try:
            absolute = os.path.abspath(source_path)
        except Exception as e:
            return ("", "", False, f"abspath_failed:{type(e).__name__}")
    else:
        absolute = source_path

    uri = "file://" + absolute
    try:
        parsed = urlparse(uri)
    except Exception as e:
        return (uri, "", False, f"urlparse_failed:{type(e).__name__}")

    if parsed.scheme != "file":
        return (uri, "", False, f"unexpected_scheme:{parsed.scheme}")

    recovered = unquote(parsed.path)
    if recovered != absolute:
        return (uri, recovered, False, "path_mismatch_after_roundtrip")

    return (uri, recovered, True, "")


def collect_aspect_paths(t2) -> dict[str, set[str]]:
    """Return {collection: set(source_path)} from document_aspects."""
    out: dict[str, set[str]] = {}
    cur = t2.document_aspects.conn.execute(
        "SELECT collection, source_path FROM document_aspects"
    )
    for collection, sp in cur:
        out.setdefault(collection, set()).add(sp or "")
    return out


def collect_t3_paths(
    t3, collection: str, sample_cap: int = T3_SAMPLE_PER_COLLECTION
) -> set[str]:
    """Return unique ``source_path`` values for a T3 collection.

    Uses paginated ``coll.get`` (ChromaDB Cloud caps limit at 300 per
    request — see ``chroma_quotas.MAX_QUERY_RESULTS``). Stops once
    ``sample_cap`` unique source_paths have been observed; the long
    tail is sampled-not-exhaustive, intentionally.
    """
    seen: set[str] = set()
    try:
        coll = t3._client.get_collection(collection)
    except Exception:
        return seen
    offset = 0
    while True:
        try:
            res = coll.get(limit=300, offset=offset, include=["metadatas"])
        except Exception:
            break
        metas = res.get("metadatas") or []
        if not metas:
            break
        for m in metas:
            sp = (m or {}).get("source_path", "") or ""
            seen.add(sp)
            if len(seen) >= sample_cap:
                return seen
        offset += 300
        if len(metas) < 300:
            break
    return seen


def main() -> int:
    from nexus.db.t2 import T2Database
    from nexus.commands._helpers import default_db_path
    from nexus.mcp_infra import get_t3

    t2 = T2Database(default_db_path())
    t3 = get_t3()

    # Stage A: aspect-row source_paths (the immediate backfill target)
    print("=== Stage A: document_aspects source_paths ===")
    aspect_paths = collect_aspect_paths(t2)
    aspect_total = sum(len(s) for s in aspect_paths.values())
    print(f"  {aspect_total} unique source_paths across {len(aspect_paths)} collections")

    # Stage B: T3 chunk metadata sweep across FS-backed collections
    # ("docs__", "rdr__", "code__"). knowledge__ collections are
    # excluded from A2 — those are exactly the case that motivates
    # chroma:// reader (they don't have FS backing). Backfill for
    # knowledge__ uses chroma:// scheme, not file://.
    print("\n=== Stage B: T3 chunk metadata sweep (FS-backed prefixes) ===")
    fs_prefixes = ("docs__", "rdr__", "code__")
    all_collections = t3.list_collections()
    fs_collections = [
        c["name"] for c in all_collections if c["name"].startswith(fs_prefixes)
    ]
    print(
        f"  Sampling up to {T3_SAMPLE_PER_COLLECTION} source_paths from each of "
        f"{len(fs_collections)} collections"
    )

    t3_paths: dict[str, set[str]] = {}
    for i, coll_name in enumerate(fs_collections):
        paths = collect_t3_paths(t3, coll_name)
        t3_paths[coll_name] = paths
        if i < 3 or i % 5 == 0 or i == len(fs_collections) - 1:
            print(f"    [{i + 1}/{len(fs_collections)}] {coll_name}: {len(paths)} paths")

    t3_total = sum(len(s) for s in t3_paths.values())
    print(f"\n  {t3_total} unique source_paths sampled")

    # Round-trip every collected path; combine aspect + T3 sources,
    # de-duped per (collection, source_path) pair so a path that
    # appears in both stages is only tested once.
    print("\n=== Round-trip ===")
    rows: list[dict] = []
    by_collection: dict[str, dict] = {}
    by_reason: Counter = Counter()
    seen: set[tuple[str, str]] = set()

    def _ingest(collection: str, source_path: str, source_stage: str) -> None:
        key = (collection, source_path)
        if key in seen:
            return
        seen.add(key)
        uri, recovered, ok, reason = round_trip(source_path)
        rows.append({
            "collection": collection,
            "source_path": source_path,
            "stage": source_stage,
            "uri": uri,
            "recovered_path": recovered,
            "round_trip_ok": ok,
            "reason": reason,
        })
        info = by_collection.setdefault(collection, {"ok": 0, "fail": 0, "n": 0})
        info["n"] += 1
        if ok:
            info["ok"] += 1
        else:
            info["fail"] += 1
            by_reason[reason] += 1

    for coll, paths in aspect_paths.items():
        for sp in paths:
            _ingest(coll, sp, "aspect")
    for coll, paths in t3_paths.items():
        for sp in paths:
            _ingest(coll, sp, "t3")

    total = len(rows)
    ok_count = sum(1 for r in rows if r["round_trip_ok"])
    fail_count = total - ok_count
    pct_ok = (ok_count / total * 100) if total else 0.0

    print(f"\n  Total round-trips tested: {total}")
    print(f"  OK:    {ok_count} ({pct_ok:.2f}%)")
    print(f"  FAIL:  {fail_count}")
    if by_reason:
        print(f"  Reasons (top):")
        for reason, count in by_reason.most_common(10):
            print(f"    {count:5d}  {reason}")

    # Pre-registered acceptance verdict
    print("\n=== Pre-registered A2 verdict ===")
    if pct_ok >= 99.0:
        verdict = "PASS"
    elif pct_ok < 95.0:
        verdict = "FAIL"
    else:
        verdict = "BORDERLINE"
    print(f"  {pct_ok:.2f}% round-trip → {verdict}")

    # Persist
    with RESULTS_PATH.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    summary = {
        "stage_a_aspect_total": aspect_total,
        "stage_b_t3_total_sampled": t3_total,
        "round_trip_total": total,
        "round_trip_ok": ok_count,
        "round_trip_fail": fail_count,
        "round_trip_pct_ok": pct_ok,
        "verdict": verdict,
        "by_reason": dict(by_reason),
        "by_collection": by_collection,
        "sample_cap_per_collection": T3_SAMPLE_PER_COLLECTION,
    }
    with SUMMARY_PATH.open("w") as f:
        json.dump(summary, f, indent=2)

    print(f"\n  Wrote {RESULTS_PATH.name} ({total} rows)")
    print(f"  Wrote {SUMMARY_PATH.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
