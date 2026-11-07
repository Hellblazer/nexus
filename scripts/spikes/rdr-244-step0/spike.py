# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-244 Step 0 spike, engine-side items 1 to 5 (nexus-2k66t.2).

Boots its own engine and mints its own tenant (the ``tests._engine_substrate``
machinery, as ``scripts/spikes/rdr-205-mvv/harness.py`` does), so nothing it writes
reaches an operator's service; the tenant dies with the engine at exit.

Usage (from a checkout with the gate jar built)::

    uv run python scripts/spikes/rdr-244-step0/spike.py --data <dir> --out <report.json>

``--data`` holds the pinned CoIR parquet files as ``<dir>/<name>-queries-corpus/*.parquet``
and ``<dir>/<name>-qrels/test-*.parquet``. Reading parquet needs pyarrow, which the project
does not depend on: run with ``uv run --with pyarrow==18.1.0``.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

DATASETS = {
    "codetrans-dl": "deep-learning-code-translation",
    "codetrans-contest": "contest-code-translation",
    "codefeedback-st": "single-turn-code-feedback",
    "codefeedback-mt": "multi-turn-code-feedback",
    "synthetic-text2sql": "text-to-sql",
}
SEED = 2026
WRITE_SAMPLE = 100  # documents timed per non-first dataset (item 5)


def read_parquet(path: str) -> list[dict]:
    import pyarrow.parquet as pq  # noqa: PLC0415 — spike-only dependency
    return pq.read_table(path).to_pylist()


def load_dataset(data: Path, name: str) -> tuple[list[dict], list[dict], list[dict]]:
    qc = data / f"{name}-queries-corpus"
    corpus = read_parquet(glob.glob(str(qc / "corpus-*.parquet"))[0])
    queries = read_parquet(glob.glob(str(qc / "queries-*.parquet"))[0])
    qrels = read_parquet(glob.glob(str(data / f"{name}-qrels" / "test-*.parquet"))[0])
    return corpus, queries, qrels


def claude_pids() -> list[str]:
    out = subprocess.run(["pgrep", "-x", "claude"], capture_output=True, text=True, encoding="utf-8")
    return sorted(out.stdout.split())


def aspect_stats(db) -> dict:
    s = db.tuples.subspace_stats("aspects")
    return {"available": s.available, "claimed": s.claimed, "dead": s.dead, "consumed": s.consumed}


def put_one(col: str, title: str, text: str) -> tuple[float, int, str]:
    from nexus.catalog.note_write import fire_note_chains, put_note, stamp_note  # noqa: PLC0415
    from nexus.hook_registry import HookRegistry  # noqa: PLC0415

    t0 = time.perf_counter()
    outcome = put_note(content=text, collection=col, title=title)
    fire_note_chains(outcome, text, hooks=HookRegistry())
    outcome = stamp_note(outcome)
    return time.perf_counter() - t0, len(outcome.pieces), str(getattr(outcome, "status", ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    cfg = Path(tempfile.mkdtemp(prefix="rdr244-cfg-"))
    os.environ["NEXUS_CONFIG_DIR"] = str(cfg)
    from tests._engine_substrate import ensure_engine, mint_test_tenant  # noqa: PLC0415

    state = ensure_engine()
    tenant, token = mint_test_tenant(state)
    from urllib.parse import urlparse  # noqa: PLC0415

    host = urlparse(state["base_url"]).hostname
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit(f"refusing: the booted engine is not on loopback ({state['base_url']})")
    os.environ.update({
        "NX_STORAGE_BACKEND": "service", "NX_SERVICE_URL": state["base_url"],
        "NX_SERVICE_TOKEN": token, "NX_LOCAL": "1",
        # The dev-checkout write guard presumes the operator's live store. This process booted the
        # engine above, on loopback, and minted the tenant; that is the only target it writes to.
        "NX_ALLOW_PROD_WRITE": f"RDR-244 Step 0 spike: self-provisioned engine {state['base_url']}, minted tenant {tenant}",
    })
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415
    from nexus.config import load_config  # noqa: PLC0415
    from nexus.corpus import t3_collection_name  # noqa: PLC0415
    from nexus.db import make_t3  # noqa: PLC0415
    from nexus.db.t2 import T2Database  # noqa: PLC0415

    report: dict = {"tenant": tenant, "engine": state.get("version_body") or state.get("version")}
    db = T2Database(cfg / "memory.db")
    t3 = make_t3()
    cat = make_catalog_reader()
    report["hybrid_default"] = (load_config().get("search") or {}).get("hybrid_default")

    # Item 1: aspect check on twenty documents of codetrans-dl.
    corpus, queries, qrels = load_dataset(args.data, "codetrans-dl")
    col = t3_collection_name(DATASETS["codetrans-dl"], t3=t3, for_write=True)
    report["collection_dl"] = col
    before, pids0 = aspect_stats(db), claude_pids()
    for doc in corpus[:20]:
        put_one(col, f"codetrans-dl:{doc['_id']}", doc["text"])
    report["item1"] = {"before": before, "after": aspect_stats(db),
                       "claude_before": pids0, "claude_after": claude_pids()}

    # Item 5 (part): load the rest of codetrans-dl, timing every write.
    times, pieces = [], []
    for doc in corpus[20:]:
        dt, n, _ = put_one(col, f"codetrans-dl:{doc['_id']}", doc["text"])
        times.append(dt)
        pieces.append(n)
    report["write_dl"] = {"docs": len(times), "total_s": sum(times),
                          "mean_s": sum(times) / len(times), "max_s": max(times),
                          "pieces_total": sum(pieces) + 20, "multi_piece_docs": sum(1 for p in pieces if p > 1)}
    report["item1"]["after_full_dl"] = aspect_stats(db)

    # Items 2 to 4 on five test queries of codetrans-dl.
    qtext = {q["_id"]: q["text"] for q in queries}
    rel = {r["query_id"]: r["corpus_id"] for r in qrels}
    qids = random.Random(SEED).sample(sorted(rel), 5)
    env = dict(os.environ)
    title_of: dict[str, tuple[str, str]] = {}
    item2 = []
    for qid in qids:
        text = qtext[qid][:256]
        t0 = time.perf_counter()
        proc = subprocess.run(
            ["uv", "run", "--project", str(REPO), "nx", "search", text, "--corpus", col, "-m", "100", "--json"],
            capture_output=True, text=True, encoding="utf-8", env=env, cwd=str(REPO),
        )
        elapsed = time.perf_counter() - t0
        if proc.returncode != 0:
            item2.append({"qid": qid, "error": proc.stderr[-2000:]})
            continue
        rows = json.loads(proc.stdout)
        chashes = [r["id"] for r in rows]
        groups = cat.docs_for_chashes(chashes)
        for ids in groups.values():
            for d in ids:
                if d not in title_of:
                    e = cat.by_doc_id(d)
                    title_of[d] = (e.title, e.physical_collection) if e else ("", "")
        keys_per_row = [[title_of[d] for d in groups.get(c, [])] for c in chashes]
        item2.append({
            "qid": qid, "relevant": f"codetrans-dl:{rel[qid]}", "elapsed_s": elapsed, "rows": len(rows),
            "rows_no_key": sum(1 for k in keys_per_row if not k),
            "rows_shared": sum(1 for k in keys_per_row if len(k) > 1),
            "rows_out_of_collection": sum(1 for k in keys_per_row for _t, c in k if c != col),
            "distinct_keys": len({t for k in keys_per_row for t, _c in k}),
            "first_rows": [{"id": r["id"], "distance": r.get("distance"), "hybrid_score": r.get("hybrid_score"),
                            "boost_fields": {k: v for k, v in r.items() if "boost" in k or k in ("frecency", "quality")},
                            "keys": keys_per_row[i]} for i, r in enumerate(rows[:5])],
            "row_fields": sorted(rows[0]) if rows else [],
        })
    report["item2_4"] = item2

    # Item 5 (rest): per-document write time on a sample of each other dataset.
    report["write_sample"] = {}
    for name, subject in DATASETS.items():
        if name == "codetrans-dl":
            continue
        corpus, _q, _r = load_dataset(args.data, name)
        c = t3_collection_name(subject, t3=t3, for_write=True)
        sample = random.Random(SEED).sample(corpus, WRITE_SAMPLE)
        ts = [put_one(c, f"{name}:{d['_id']}", d["text"])[0] for d in sample if (d["text"] or "").strip()]
        report["write_sample"][name] = {"collection": c, "docs": len(ts), "mean_s": sum(ts) / len(ts), "max_s": max(ts)}
    report["aspect_final"] = aspect_stats(db)
    report["claude_final"] = claude_pids()
    args.out.write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps({k: report[k] for k in ("item1", "write_dl", "write_sample", "aspect_final")}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
