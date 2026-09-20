# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-217 Phase 1 measurement driver (beads nexus-lqo4p.3 and .4).

Run as a module, never collected by pytest (leading underscore, no test_ prefix)
because it TAKES A NUMBER rather than asserting one:

    uv run python -m tests._rdr217_phase1_measure --route vector
    uv run python -m tests._rdr217_phase1_measure --route hybrid

THE THREE CONDITIONS bead .3 requires, and how each is met here:

1. A FRESHLY BUILT INDEX. A bulk-built HNSW index retrieves measurably better
   than an incrementally grown one, so this seeds a brand-new collection on a
   brand-new tenant in one bulk pass. It never reads the developer's live
   index.
2. ONE SESSION. An autovacuum reclaiming dead tuples once moved a query's
   overlap from 0.818 to 0.333 with no ranking code changing (nexus-4lnn1), so
   the build and every query happen in a single process against a single
   substrate boot. Run both routes in ONE invocation with --route both when
   comparing, or accept that two invocations are two windows and say so.
3. ONE WINDOW, on the side of 1be4146da the artifact records. The artifact
   scopes the corpus to code__ collections, which that commit does not touch,
   so the constraint is satisfied by construction rather than by timing.

THE CORPUS IS COMPLETE BY CONSTRUCTION, which is how this driver sidesteps the
two traps the artifact's completeness_contract names. It does not page the
engine back for an inventory (300-row cap) and does not re-read chunk bodies
(store_get_many's 4000-char truncation): the chunk list it seeded IS the
inventory it scores against, held in memory for the one session. The corpus
fingerprint on the report is what lets a later reader check two measurements
saw the same corpus instead of trusting it.

CHUNK GEOMETRY IS PRODUCTION'S. Files go through nexus.chunker.chunk_file, the
same splitter `nx index repo` uses, so the measurement is not an artifact of a
bespoke chunker invented for it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import nexus.db.http_vector_client as hvc
from nexus.chunker import chunk_file
from nexus.db.limits import QUOTAS, SAFE_CHUNK_BYTES
from tests._engine_substrate import ensure_engine, mint_test_tenant
from tests._rdr217_recall_harness import load_query_set, measure

#: The substrate's real tier-1 embedding token. A guessed width disagrees with
#: the registered row and the upsert 400s.
COLLECTION = "code__rdr217-baseline__bge-base-en-v15-768__v1"


def _say(line: str = "") -> None:
    """Stdout IS this driver's deliverable — it reports a measurement rather
    than asserting one — so printing is the point, not a lapse."""
    print(line, flush=True)  # noqa: T201 — see docstring

#: Both roots, and tests/ is not padding: two of the query set's tokens
#: (jar_freshness_skip_reason, word_similarity_threshold) appear ZERO times
#: under src/, so a src-only corpus would report them unanswerable and measure
#: the corpus rather than the route.
ROOTS = ("src/nexus", "tests")


def build_chunks(repo: Path, roots: tuple[str, ...], limit_files: int | None) -> list[dict]:
    files = sorted(p for r in roots for p in (repo / r).rglob("*.py"))
    if limit_files:
        files = files[:limit_files]
    rows: list[dict] = []
    skipped_oversize = 0
    for f in files:
        try:
            content = f.read_text(errors="replace")
        except OSError:
            continue
        rel = str(f.relative_to(repo))
        for ch in chunk_file(f, content):
            text = ch["text"]
            if len(text.encode()) > SAFE_CHUNK_BYTES:
                skipped_oversize += 1
                continue
            chash = hashlib.sha256(text.encode()).hexdigest()
            rows.append({
                "id": chash,
                "content": text,
                "source_uri": rel,
                "title": f"{rel}:{ch.get('line_start', 0)}-{ch.get('line_end', 0)}",
            })
    # Identical chunk text collapses to one row in T3 by design, so de-duplicate
    # here too or the inventory would over-count the denominator.
    seen: set[str] = set()
    deduped = []
    for r in rows:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        deduped.append(r)
    _say(f"[corpus] files={len(files)} chunks={len(deduped)} "
         f"(deduped {len(rows) - len(deduped)}, oversize skipped {skipped_oversize})")
    return deduped


def seed(db: hvc.HttpVectorClient, corpus: list[dict]) -> float:
    """Bulk-build the index in one pass. Returns elapsed seconds."""
    batch = QUOTAS.MAX_RECORDS_PER_WRITE
    started = time.monotonic()
    for i in range(0, len(corpus), batch):
        part = corpus[i:i + batch]
        db.upsert_chunks_with_embeddings(
            COLLECTION,
            ids=[r["id"] for r in part],
            documents=[r["content"] for r in part],
            embeddings=[],  # server-side embedding, local bge-768, no credentials
            metadatas=[{"chunk_text_hash": r["id"], "title": r["title"],
                        "source_path": r["source_uri"]} for r in part],
        )
        done = min(i + batch, len(corpus))
        elapsed = time.monotonic() - started
        _say(f"[seed] {done}/{len(corpus)} chunks  {elapsed:6.1f}s  "
             f"{done / max(elapsed, 0.001):6.1f} chunks/s")
    return time.monotonic() - started


def report_lines(report, query_set) -> list[str]:
    shapes = sorted({q.shape for q in query_set.queries})
    out = [
        f"route={report.route} k={report.k} corpus={report.corpus_size} chunks "
        f"fingerprint={report.corpus_fingerprint}",
        "",
        "PER SHAPE (precision@k is the primary figure; recall carries a ceiling)",
    ]
    for shape in shapes:
        n = sum(1 for r in report.per_query if r.shape == shape)
        p = report.macro_precision(shape)
        rc = report.macro_recall(shape)
        out.append(
            f"  {shape:<12} n={n}  precision@{report.k}="
            f"{'n/a' if p is None else f'{p:.3f}'}"
            f"  recall@{report.k}={'n/a' if rc is None else f'{rc:.3f}'}"
        )
    out += ["", "PER QUERY"]
    for r in report.per_query:
        out.append(
            f"  {r.query_id:<10} {r.shape:<12} relevant={r.relevant:<5} "
            f"hits={r.hits:<3} precision="
            f"{'n/a' if r.precision is None else f'{r.precision:.3f}'}"
            f"  recall={'n/a' if r.recall is None else f'{r.recall:.3f}'}"
            f"  ceiling={'n/a' if r.recall_ceiling is None else f'{r.recall_ceiling:.3f}'}"
            f"{'  [CAPPED]' if r.recall_is_capped else ''}"
        )
    if report.unanswerable():
        out.append(f"  UNANSWERABLE: {', '.join(report.unanswerable())}")
    if report.capped():
        out.append(f"  CAPPED recall: {', '.join(report.capped())}")
    if report.mislabelled_rare_tokens():
        out.append(f"  MISLABELLED rare_token: {', '.join(report.mislabelled_rare_tokens())}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--route", choices=("vector", "hybrid", "both"), default="vector")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--limit-files", type=int, default=None,
                    help="cap the file set; RECORD IT if used, the number is "
                         "then about that subset")
    ap.add_argument("--json-out", type=Path, default=None)
    args = ap.parse_args(argv)

    # ISOLATE THE AMBIENT ENVIRONMENT FIRST, before anything constructs a
    # client. Under pytest, _isolate_config_dir and _isolate_service_endpoint_env
    # do this; run as a module there is nothing to do it, so the driver inherits
    # the developer's real config.yml and its mint_token, and catalog
    # registration tries to mint a data token for the DEFAULT tenant against the
    # test engine — a 401 that looks like a credential problem and is really an
    # isolation problem. Measured on the first run of this driver.
    sandbox = tempfile.mkdtemp(prefix="rdr217-measure-")
    os.environ["NEXUS_CONFIG_DIR"] = sandbox
    for leaked in ("NX_SERVICE_URL", "NX_SERVICE_TOKEN", "NX_SERVICE_HOST",
                   "NX_SERVICE_PORT", "NX_VOYAGE_API_KEY", "VOYAGE_API_KEY"):
        os.environ.pop(leaked, None)
    # And do not ping the install-ping endpoint from a throwaway environment
    # (bead nexus-6doho: 18 harness install_ids reached production on two
    # release nights). A fresh config dir mints a new install_id, so the
    # opt-out has to be here rather than in the config it just created.
    os.environ["NX_NO_TELEMETRY"] = "1"
    # The nexus-a2qhz production-write guard refuses a write from a dev
    # checkout unless the opt-in NAMES why it is safe — a bare "1" is refused
    # exactly like an unset var, which is the right design. The reason lives
    # here rather than on a command line so it is reviewable with the code that
    # relies on it: every write this driver makes goes to a collection on a
    # tenant minted seconds earlier on a substrate this process booted, and it
    # reads the operator's store never.
    os.environ["NX_ALLOW_PROD_WRITE"] = (
        "RDR-217 Phase 1 measurement driver: writes only to a freshly minted "
        "throwaway tenant on the self-provisioned test engine substrate, in a "
        "sandboxed NEXUS_CONFIG_DIR; never reads or writes the operator's store"
    )

    repo = Path(__file__).resolve().parent.parent
    query_set = load_query_set()
    corpus = build_chunks(repo, ROOTS, args.limit_files)

    state = ensure_engine()
    tenant, token = mint_test_tenant(state)
    os.environ["NX_STORAGE_BACKEND"] = "service"
    os.environ["NX_SERVICE_URL"] = state["base_url"]
    os.environ["NX_SERVICE_TOKEN"] = token
    os.environ["NX_LOCAL"] = "1"
    _say(f"[engine] {state['base_url']} tenant={tenant}")

    db = hvc.HttpVectorClient(tenant=tenant)
    build_seconds = seed(db, corpus)
    _say(f"[seed] index built in {build_seconds:.1f}s")

    routes = ("vector", "hybrid") if args.route == "both" else (args.route,)
    payload: dict = {
        "k": args.k, "collection": COLLECTION, "corpus_chunks": len(corpus),
        "roots": list(ROOTS), "limit_files": args.limit_files,
        "index_build_seconds": round(build_seconds, 1),
        "reports": {},
    }
    for route in routes:
        report = measure(db, query_set, [COLLECTION], corpus, route=route, k=args.k)
        # Refuse to record a report whose averages do not mean what they appear
        # to: mostly-unanswerable, nothing retrieved, or a mislabelled shape.
        report.must_be_interpretable()
        _say()
        for line in report_lines(report, query_set):
            _say(line)
        payload["reports"][route] = {
            "corpus_fingerprint": report.corpus_fingerprint,
            "macro_precision": report.macro_precision(),
            "macro_recall": report.macro_recall(),
            "per_shape": {
                s: {"precision": report.macro_precision(s), "recall": report.macro_recall(s)}
                for s in sorted({q.shape for q in query_set.queries})
            },
            "per_query": [vars(r) | {"recall_is_capped": r.recall_is_capped}
                          for r in report.per_query],
            "unanswerable": list(report.unanswerable()),
            "capped": list(report.capped()),
        }

    if args.json_out:
        args.json_out.write_text(json.dumps(payload, indent=2) + "\n")
        _say(f"\n[out] {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
