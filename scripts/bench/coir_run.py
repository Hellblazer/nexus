# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-244 Track A harness: load the pinned CoIR sample, run ``nx search``, score, write ``results.json``.

The loader writes through ``put_note`` and fires the note chains with an EMPTY ``HookRegistry``, so no
aspect, taxonomy or document hook runs (RDR-244 Track A ingestion; Step 0 item 1 measured zero). Each
document's title is ``<dataset>:<coir-id>``, its identity. The query run calls the venv's own ``nx``
(Step 0: ``uv run nx`` costs about 1.75 s of start-up per call) with the pinned flags only.

Scoring is ``coir_score`` over duplicate-text classes (Sam, ask rdr244-duplicate-text, 2026-11-06).
Every metric, interval and latency percentile is reported per dataset (nexus-2k66t.2.11); the pooled
latency figure is labelled secondary. One discarded warm-up query runs per dataset collection.

``run`` takes the storage environment as given (``NX_SERVICE_URL``/``NX_SERVICE_TOKEN`` in
``os.environ``) and never sets the dev-checkout write opt-in itself. ``main`` boots its own engine and
mints a tenant, as the Step 0 spike did; whether it may write there is the operator's
``NX_ALLOW_PROD_WRITE``, an owner decision before Step 2.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib.metadata import version as _dist_version
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.bench.coir_sample import CAP, QUERIES, SEED, Sample, draw  # noqa: E402
from scripts.bench.coir_score import (  # noqa: E402
    K,
    EmptyKeyError,
    Row,
    bootstrap_interval,
    collapse,
    collapse_qrels,
    mean,
    percentile,
    rank_documents,
    score_query,
)

RESAMPLES: int = 10_000
MAX_RESULTS: int = 100
SEARCH_FLAGS: tuple[str, ...] = ()  # no --hybrid, --lexical or --no-rerank (RDR-244 query run)
# Per-dataset wall-clock ingest cap. Sam, 2026-11-03, on nexus-2k66t.2.7: "cap as proposed" (30 min,
# an over-cap dataset dropped and named).
CAP_SECONDS: float = 30 * 60
SEARCH_TIMEOUT_S: float = 300.0
EMBEDDING_TOKEN: str = "bge-base-en-v15-768"  # local mode, $0 (Decisions, 2026-11-03)
LICENCE_SOURCE: str = ("CoIR paper licence table and the upstream sources (Apache-2.0); the Hugging Face "
                       "cards state no licence (T2 nexus_rdr/244-research-15; Sam, 2026-11-06)")


@dataclass(frozen=True)
class Dataset:
    name: str
    subject: str
    licence: str | None
    revisions: dict[str, str]


def _rev(queries_corpus: str, qrels: str) -> dict[str, str]:
    return {"queries-corpus": queries_corpus, "qrels": qrels}


# Licence: see LICENCE_SOURCE. Revisions: the Hugging Face commits Step 0 read (T2 nexus_rdr/244-research-15).
POOL: dict[str, Dataset] = {d.name: d for d in (
    Dataset("codetrans-dl", "deep-learning-code-translation", "Apache-2.0", _rev("328b6b6870fce952a3d5d49a8672518a0e6f6f58", "3c26def7e3903a50c219584fedc85d3ad77fac43")),
    Dataset("codetrans-contest", "contest-code-translation", "Apache-2.0", _rev("60a64fc853989c538f1d7495514bdd428b641371", "981ff059af50f8d336dc17a0e46dc6e4b7bd1ca5")),
    Dataset("codefeedback-st", "single-turn-code-feedback", "Apache-2.0", _rev("9ea30d040642f9a77df65e0911c3d5e04bc18ddb", "a5f9ffe7f0c6f60fda0e3f06043e1e43df40d4f6")),
    Dataset("codefeedback-mt", "multi-turn-code-feedback", "Apache-2.0", _rev("ef78e2c8858103fccc401e61e2b5864f3f1a3875", "a81bd47c248cd842a985a4750ba07f4d1b785097")),
    Dataset("synthetic-text2sql", "text-to-sql", "Apache-2.0", _rev("9a918a5cc55035350d4525fea38e4a88c6c283f7", "14c01767126acab79d2bd80272ba1f908de639e0")),
)}


class HarnessRefusal(RuntimeError):
    """A precondition the run will not proceed past."""


class OverCap(RuntimeError):
    """A dataset's ingest passed its wall-clock cap; the dataset is dropped and named."""


def check_licences(datasets: Sequence[Dataset]) -> None:
    missing = [d.name for d in datasets if not d.licence]
    if missing:
        raise HarnessRefusal(f"no recorded licence for {', '.join(missing)}; the run does not start")


# ── data ──────────────────────────────────────────────────────────────────────


def read_parquet(path: str) -> list[dict]:
    import pyarrow.parquet as pq  # noqa: PLC0415 — bench-only dependency (uv run --with pyarrow)
    return pq.read_table(path).to_pylist()


def read_dataset(data: Path, name: str) -> tuple[list[dict], list[dict], list[dict]]:
    """Corpus, queries and TEST-split qrels of one pinned dataset (nexus-2k66t.2.12)."""
    qc = data / f"{name}-queries-corpus"
    one = lambda pattern: read_parquet(_single(glob.glob(str(pattern))))  # noqa: E731
    return (one(qc / "corpus-*.parquet"), one(qc / "queries-*.parquet"),
            one(data / f"{name}-qrels" / "test-*.parquet"))


def _single(paths: list[str]) -> str:
    if len(paths) != 1:
        raise HarnessRefusal(f"expected one parquet file, found {paths}")
    return paths[0]


# ── load ──────────────────────────────────────────────────────────────────────


def put_document(collection: str, title: str, text: str) -> None:
    from nexus.catalog.note_write import STORED, fire_note_chains, put_note, stamp_note  # noqa: PLC0415
    from nexus.hook_registry import HookRegistry  # noqa: PLC0415

    outcome = put_note(content=text, collection=collection, title=title)
    if outcome.status != STORED:
        raise HarnessRefusal(f"{title}: put_note returned {outcome.status!r}")
    fire_note_chains(outcome, text, hooks=HookRegistry())
    stamp_note(outcome)


def ensure_empty(t3, collection: str) -> None:
    """nexus-2k66t.2.9: the harness is the only writer, so its collection starts empty."""
    if t3.collection_exists(collection) and t3.count(collection) > 0:
        raise HarnessRefusal(f"{collection} already holds content; the harness writes only to an empty collection")


def load(sample: Sample, text_of: dict[str, str], collection: str, *, cap_s: float,
         put: Callable[[str, str, str], None] = put_document,
         clock: Callable[[], float] = time.monotonic) -> dict:
    start = clock()
    for cid in sample.corpus_ids:
        if clock() - start > cap_s:
            raise OverCap(f"{sample.dataset}: ingest passed {cap_s:.0f} s")
        put(collection, f"{sample.dataset}:{cid}", text_of[cid])
    return {"documents": len(sample.corpus_ids), "seconds": clock() - start}


# ── query ─────────────────────────────────────────────────────────────────────


def nx_binary() -> Path:
    """The venv's own ``nx``, beside this interpreter."""
    path = Path(sys.executable).parent / "nx"
    if not path.exists():
        raise HarnessRefusal(f"no nx beside {sys.executable}")
    return path


def search(nx: Path, text: str, collection: str) -> tuple[list[dict], float]:
    """One pinned ``nx search``. Options precede ``--`` so a query that starts with ``-`` stays the query."""
    argv = [str(nx), "search", "--corpus", collection, "-m", str(MAX_RESULTS), "--json", *SEARCH_FLAGS, "--", text]
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", env=dict(os.environ),
                              timeout=SEARCH_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise HarnessRefusal(f"nx search passed {SEARCH_TIMEOUT_S:.0f} s on {collection}") from exc
    elapsed = time.perf_counter() - t0
    if proc.returncode != 0:
        raise HarnessRefusal(f"nx search exited {proc.returncode}: {proc.stderr[-1000:]}")
    said = [ln for ln in proc.stderr.splitlines() if ln.startswith(("Warning:", "note:"))]
    if said:  # nx's own stderr notices (degraded rerank, excluded collections); library warnings pass
        raise HarnessRefusal(f"nx search warned on {collection}: {said}")
    rows = json.loads(proc.stdout or "[]")
    if not rows:
        raise HarnessRefusal(f"nx search returned no rows from loaded collection {collection}")
    return rows, elapsed


class KeyMap:
    """chash -> titles of the documents in THIS collection that hold it (nexus-2k66t.2.5), cached."""

    def __init__(self, catalog, collection: str) -> None:
        self._cat, self._col = catalog, collection
        self._docs: dict[str, list[str]] = {}
        self._title: dict[str, str | None] = {}

    def prime(self, chashes: Sequence[str]) -> None:
        new = [c for c in chashes if c not in self._docs]
        if new:
            got = self._cat.docs_for_chashes(new)
            for c in new:
                self._docs[c] = list(got.get(c, []))

    def __call__(self, chash: str) -> list[str]:
        out = []
        for d in self._docs.get(chash, []):
            if d not in self._title:
                e = self._cat.by_doc_id(d)
                self._title[d] = e.title if e is not None and e.physical_collection == self._col else None
            if self._title[d]:
                out.append(self._title[d])
        return out


def run_dataset(sample: Sample, query_text: dict[str, str], collection: str, *, nx: Path, catalog,
                taxonomy=None, warmup: str | None = None) -> dict:
    """Query and score one loaded dataset. ``warmup`` is an unscored query text (nexus-2k66t.2.11)."""
    prefix = f"{sample.dataset}:"

    def class_of(title: str) -> str:
        return prefix + sample.class_of(title.removeprefix(prefix))

    keys = KeyMap(catalog, collection)
    keys_for = collapse(keys, class_of)
    search(nx, warmup if warmup is not None else query_text[sample.query_ids[0]], collection)  # discarded
    per_query, chashes_seen = [], set()
    for qid in sample.query_ids:
        rows, elapsed = search(nx, query_text[qid], collection)
        chashes = [r["id"] for r in rows]
        chashes_seen.update(chashes)
        keys.prime(chashes)
        try:
            ranked = rank_documents([Row(r["id"], float(r["distance"])) for r in rows], keys_for, k=K)
        except EmptyKeyError as exc:
            raise HarnessRefusal(f"{sample.dataset} query {qid}: {exc}") from exc
        qrels = collapse_qrels({prefix + c: g for c, g in sample.qrels[qid].items()}, class_of)
        s = score_query(ranked.keys, qrels, k=K)
        signals = sorted({k for r in rows for k, v in r.items()
                          if (k.startswith(("rerank", "bib_")) or "boost" in k) and v not in (None, 0)})
        if signals:
            raise HarnessRefusal(f"{sample.dataset} query {qid}: rows carry rank signals {signals}; the "
                                 "pinned run is vector order only")
        per_query.append({
            "query_id": qid, "ranked": [[k, qrels.get(k, 0)] for k in ranked.keys], "short_by": ranked.short_by,
            "shared_rows": ranked.shared_rows, "rows": len(rows), "ndcg@10": s.ndcg, "recall@10": s.recall,
            "rr": s.rr, "latency_s": elapsed,
        })
    assigned = None
    if taxonomy is not None:
        assigned = len(taxonomy.get_assignments_for_docs(sorted(chashes_seen)))  # keyed by chunk hash
        if assigned:
            raise HarnessRefusal(f"{sample.dataset}: {assigned} taxonomy assignments on bench chunks; the "
                                 "empty registry should leave none (nexus-2k66t.2.1)")
    return {"per_query": per_query, "taxonomy_assignments": assigned}


# ── summary ───────────────────────────────────────────────────────────────────


def summarise(per_query: Sequence[dict], *, resamples: int = RESAMPLES, seed: int = SEED) -> dict:
    out: dict = {"n": len(per_query)}
    for metric in ("ndcg@10", "recall@10", "rr"):
        vals = [q[metric] for q in per_query]
        lo, hi = bootstrap_interval(vals, resamples=resamples, seed=seed)
        out["mrr" if metric == "rr" else metric] = {"value": mean(vals), "ci95": [lo, hi]}
    lat = [q["latency_s"] for q in per_query]
    for p in (50, 95):
        lo, hi = bootstrap_interval(lat, resamples=resamples, seed=seed, stat=lambda v, p=p: percentile(v, p))
        out[f"latency_p{p}_s"] = {"value": percentile(lat, p), "ci95": [lo, hi]}
    out["short_queries"] = sum(1 for q in per_query if q["short_by"])
    out["shared_rows"] = sum(q["shared_rows"] for q in per_query)
    return out


def engine_info() -> dict:
    """``GET /version`` from the service this process writes to: release, embedder family and profile."""
    import httpx  # noqa: PLC0415

    url = os.environ.get("NX_SERVICE_URL", "").rstrip("/")
    headers = {"Authorization": f"Bearer {os.environ['NX_SERVICE_TOKEN']}"} if os.environ.get("NX_SERVICE_TOKEN") else {}
    body = httpx.get(f"{url}/version", headers=headers, timeout=30).json()
    return {k: body.get(k) for k in ("release_version", "version", "embedding_mode", "embedding_profile")}


ENGINE_FLOOR: tuple[int, int, int] = (0, 1, 175)  # RDR-244 Technical Environment


def check_engine_floor(engine: dict) -> None:
    raw = str(engine.get("release_version") or "")
    try:
        got = tuple(int(x) for x in raw.lstrip("v").split("-")[0].split("."))
    except ValueError:
        got = ()
    if len(got) != 3 or got < ENGINE_FLOOR:
        raise HarnessRefusal(f"engine release {raw!r} is below the floor {'.'.join(map(str, ENGINE_FLOOR))} "
                             "or unstamped; the published run needs a stamped engine at or above it")


def check_embedding(collection: str, engine: dict) -> None:
    """Refuse anything but the local bge posture the pre-registration names (and $0 spend)."""
    if EMBEDDING_TOKEN not in collection:
        raise HarnessRefusal(f"{collection} is not a {EMBEDDING_TOKEN} collection; the run is local bge only")
    if engine.get("embedding_mode") != "onnx-local" or engine.get("embedding_profile") != "bge":
        raise HarnessRefusal(f"engine embeds as {engine}; the run is local bge only")


def pins(*, hybrid_default, engine: dict) -> dict:
    import platform  # noqa: PLC0415

    def git(*a: str) -> str:
        return subprocess.run(["git", "-C", str(REPO), *a], capture_output=True, text=True,
                              encoding="utf-8").stdout.strip()

    try:
        pyarrow = _dist_version("pyarrow")
    except Exception:  # noqa: BLE001 — absent when the readers are injected (tests)
        pyarrow = None
    return {"conexus": _dist_version("conexus"), "git_sha": git("rev-parse", "HEAD"),
            "git_dirty": bool(git("status", "--porcelain")), "python": platform.python_version(),
            "platform": platform.platform(), "pyarrow": pyarrow, "engine": engine,
            "embedding_model": EMBEDDING_TOKEN, "max_results": MAX_RESULTS, "search_flags": list(SEARCH_FLAGS),
            "k": K, "seed": SEED, "queries_per_dataset": QUERIES, "corpus_cap": CAP, "resamples": RESAMPLES,
            "ingest_cap_s": None, "hybrid_default": hybrid_default, "gain": "2^g-1 (grades checked to be 1)",
            "duplicate_text": "equivalence classes (ask rdr244-duplicate-text)",
            "query_text": "passed whole to nx search, length unchecked; the embedder's handling past its window is unmeasured",
            "rank_order": "nx search output order; key order within equal-distance runs",
            "distance_threshold": "none: a bge collection gets no threshold (search_engine."
                                  "_threshold_for_collection_model)",
            "retrieval_unit": "chunks from put_note's chunker, deduplicated to documents after retrieval",
            "frecency": "frecency_score is on every row and inert: hybrid off scores 1.0 * vector_norm"}


def run(datasets: Sequence[Dataset], data: Path, out: Path, *, cap_s: float = CAP_SECONDS,
        nx: Path | None = None, resamples: int = RESAMPLES, queries: int = QUERIES, cap: int = CAP,
        readers: Callable[[Path, str], tuple[list[dict], list[dict], list[dict]]] = read_dataset,
        enforce_engine_floor: bool = True) -> dict:
    """Run Track A over ``datasets``; write ``out`` (``results.json``) after each dataset, and each
    dataset's sample manifest beside it as ``<dataset>.manifest.json``. Returns the results."""
    check_licences(datasets)
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415
    from nexus.config import load_config  # noqa: PLC0415
    from nexus.corpus import t3_collection_name  # noqa: PLC0415
    from nexus.db import make_t3  # noqa: PLC0415

    hybrid = (load_config().get("search") or {}).get("hybrid_default")
    if hybrid:
        raise HarnessRefusal("search.hybrid_default is true; the pinned run needs it false (nexus-2k66t.2.1)")
    nx = nx or nx_binary()
    t3, cat = make_t3(), make_catalog_reader()
    taxonomy = _taxonomy()
    engine = engine_info()
    if enforce_engine_floor:
        check_engine_floor(engine)
    results: dict = {"track": "A", "pins": pins(hybrid_default=hybrid, engine=engine), "licence_source": LICENCE_SOURCE,
                     "datasets": {}, "dropped": {}}
    results["pins"]["ingest_cap_s"] = cap_s
    for ds in datasets:
        corpus, qrows, qrels = readers(data, ds.name)
        qtext = {str(q["_id"]): q["text"] for q in qrows}
        sample = draw(ds.name, corpus, qrels, queries=queries, cap=cap, query_text=qtext)
        if not sample.query_ids:
            results["dropped"][ds.name] = "every drawn query was excluded"
            _write(out, results)
            continue
        if sample.grades != [1]:  # checked before ingest; the pre-registered gain statement needs grade 1
            results["dropped"][ds.name] = f"qrels grades {sample.grades}, not [1]"
            _write(out, results)
            continue
        collection = t3_collection_name(ds.subject, t3=t3, for_write=True)
        check_embedding(collection, engine)
        ensure_empty(t3, collection)
        text_of = {str(d["_id"]): d["text"] for d in corpus}
        try:
            ingest = load(sample, text_of, collection, cap_s=cap_s)
        except OverCap as exc:
            results["dropped"][ds.name] = str(exc)
            _write(out, results)
            continue
        drawn = set(sample.query_ids) | set(sample.excluded_queries)
        unscored = [q for q in sorted({str(r["query_id"]) for r in qrels}) if q not in drawn and q in qtext]
        body = run_dataset(sample, qtext, collection, nx=nx, catalog=cat, taxonomy=taxonomy,
                           warmup=qtext[unscored[0]] if unscored else None)
        manifest = sample.manifest()
        (out.parent / f"{ds.name}.manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True))
        results["datasets"][ds.name] = {
            "collection": collection, "licence": ds.licence, "revisions": ds.revisions,
            "query_set_sha256": manifest["sha256"], "ingest": ingest,
            "corpus_total": len(corpus), "corpus_loaded": len(sample.corpus_ids),
            "corpus_fraction": len(sample.corpus_ids) / len(corpus),
            "loaded_whole": len(sample.corpus_ids) + len(sample.excluded_docs) == len(corpus),
            "warmup": "unscored test query" if unscored else "first scored query (no unscored one left)",
            "excluded_documents": len(sample.excluded_docs), "excluded_queries": sample.excluded_queries,
            "duplicate_classes": len(sample.duplicate_classes),
            "documents_merged": sum(len(m) - 1 for m in sample.duplicate_classes.values()),
            "taxonomy_assignments": body["taxonomy_assignments"],
            "summary": summarise(body["per_query"], resamples=resamples),
            "per_query": body["per_query"],
        }
        _pool_latency(results)
        _write(out, results)
    _write(out, results)
    return results


def _pool_latency(results: dict) -> None:
    lat = [q["latency_s"] for d in results["datasets"].values() for q in d["per_query"]]
    if lat:
        results["pooled_latency_secondary"] = {"n": len(lat), "p50_s": percentile(lat, 50), "p95_s": percentile(lat, 95)}


def _write(out: Path, results: dict) -> None:
    out.write_text(json.dumps(results, indent=1, sort_keys=True))


def _taxonomy():
    from nexus.commands._helpers import default_db_path  # noqa: PLC0415
    from nexus.db.t2 import T2Database  # noqa: PLC0415

    return T2Database(default_db_path()).taxonomy


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="RDR-244 Track A harness")
    ap.add_argument("--data", type=Path, required=True, help="pinned CoIR parquet tree")
    ap.add_argument("--out", type=Path, required=True, help="results.json path")
    ap.add_argument("--datasets", nargs="*", default=list(POOL))
    args = ap.parse_args(argv)

    if os.environ.get("NX_VOYAGE_API_KEY", "").strip():
        raise SystemExit("refusing: NX_VOYAGE_API_KEY is set, so the engine would embed with Voyage; "
                         "the pre-registered run is local bge at $0")
    if subprocess.run(["git", "-C", str(REPO), "status", "--porcelain"], capture_output=True, text=True,
                      encoding="utf-8").stdout.strip():
        raise SystemExit("refusing: the checkout has uncommitted changes, so git_sha would not name the code run")
    os.environ["NEXUS_CONFIG_DIR"] = tempfile.mkdtemp(prefix="rdr244-cfg-")
    from urllib.parse import urlparse  # noqa: PLC0415

    from tests._engine_substrate import ensure_engine, mint_test_tenant  # noqa: PLC0415

    state = ensure_engine()
    if urlparse(state["base_url"]).hostname not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit(f"refusing: the booted engine is not on loopback ({state['base_url']})")
    _, token = mint_test_tenant(state)
    os.environ.update({"NX_STORAGE_BACKEND": "service", "NX_SERVICE_URL": state["base_url"],
                       "NX_SERVICE_TOKEN": token, "NX_LOCAL": "1"})
    run([POOL[n] for n in args.datasets], args.data, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
