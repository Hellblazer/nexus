#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-210 P4.3 (nexus-t189m) ranking measurement: entry point.

Run through ``run.sh``, which provisions a fenced scratch engine and sets the
environment this needs. ``--plan`` needs no engine and calls nothing.

    p43_run.py --plan [--keyless] [--cap-tokens N]
    p43_run.py [--keyless] [--only A,B,C] ...        (inside run.sh)
    p43_run.py --seed-register                        (run.sh, Voyage-profile boot)
    p43_run.py --write-corpus-list                    (regenerate corpus-files.json)
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import p43_budget as budget  # noqa: E402
import p43_corpus as corp  # noqa: E402
import p43_engine as eng  # noqa: E402
import p43_ingest as ing  # noqa: E402
import p43_measure_a as ma  # noqa: E402
import p43_measure_b as mb  # noqa: E402
import p43_measure_c as mc  # noqa: E402
import p43_plan as plan_mod  # noqa: E402
import p43_queries as qs  # noqa: E402
import p43_results as res  # noqa: E402

DEFAULT_REPO_ROOT = HERE.parent.parent.parent


def emit(msg: str = "", *, err: bool = False) -> None:
    """Line-buffered output (the harness is read through a pipe or a log file)."""
    stream = sys.stderr if err else sys.stdout
    stream.write(msg + "\n")
    stream.flush()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", action="store_true", help="print the corpora, query counts, collections and the Voyage token estimate; call nothing")
    ap.add_argument("--keyless", action="store_true", help="measurement B only, on a bge-only engine (no Voyage anywhere)")
    ap.add_argument("--cap-tokens", type=int, default=budget.DEFAULT_CAP_TOKENS, help="refuse to run, and stop mid-run, over this many estimated Voyage tokens (default 1,500,000)")
    ap.add_argument("--assume-voyage-rerank", action="store_true", help="count rerank tokens against the cap in --plan (a Voyage-profile engine)")
    ap.add_argument("--only", default="", help="comma list of measurements to run: A,B,C (default: B when --keyless, else A,B,C)")
    ap.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT)
    ap.add_argument("--workdir", type=Path, default=None, help="persistent state and results directory (default $P43_WORKDIR)")
    ap.add_argument("--resamples", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=210)
    ap.add_argument("--depths", default="10,20", help="list depths for the inversion count in C (default 10,20)")
    ap.add_argument("--no-shuffled-collection", action="store_true", help="B: skip the shuffled-order pool collection (noise floor = the repeated request)")
    ap.add_argument("--limit-queries", type=int, default=0, help="smoke test: first N queries per set; the results are marked partial")
    ap.add_argument("--seed-needed", action="store_true", help="exit 10 when a Voyage collection is not registered yet (run.sh then does the Voyage-profile seed boot), else 0; calls nothing")
    ap.add_argument("--seed-register", action="store_true", help="register the Voyage collections (Voyage-profile boot), then exit")
    ap.add_argument("--write-corpus-list", action="store_true", help="regenerate corpus-files.json from the repo and exit")
    return ap.parse_args(argv)


def load_world(repo_root: Path, limit: int = 0):
    pinned = corp.load_pinned(HERE / "corpus-files.json")
    corpora = {k: corp.build_corpus(repo_root, k, pinned) for k in corp.KINDS}
    queries = qs.load_queries(HERE / "queries.json")
    qs.validate(queries, corpora)
    if limit:
        queries = [q for k in corp.KINDS for q in qs.of_set(queries, k)[:limit]]
    return pinned, corpora, queries


def want(only: str, keyless: bool) -> set[str]:
    chosen = {s.strip().upper() for s in only.split(",") if s.strip()}
    if not chosen:
        chosen = {"B"} if keyless else {"A", "B", "C"}
    if keyless and chosen != {"B"}:
        raise SystemExit("--keyless runs measurement B only")
    bad = chosen - {"A", "B", "C"}
    if bad:
        raise SystemExit(f"unknown measurement(s): {sorted(bad)}")
    return chosen


def collection_specs(corpora: dict[str, corp.Corpus], keyless: bool) -> dict[tuple[str, str], ing.CollectionSpec]:
    out = {}
    for kind, c in corpora.items():
        out[(kind, "bge")] = ing.CollectionSpec.for_corpus(c, eng.BGE_MODEL)
        if not keyless:
            vm = eng.CONTEXT_MODEL if kind == "prose" else eng.CODE_MODEL
            out[(kind, "voyage")] = ing.CollectionSpec.for_corpus(c, vm)
    return out


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = args.repo_root.resolve()
    if args.write_corpus_list:
        corp.write_pinned_list(repo_root, HERE / "corpus-files.json")
        emit(f"wrote {HERE / 'corpus-files.json'}")
        return 0

    pinned, corpora, queries = load_world(repo_root, args.limit_queries)
    for kind, c in corpora.items():
        if c.drifted:
            emit(f"[p43] WARNING: {len(c.drifted)} {kind} file(s) changed since corpus-files.json was pinned "
                 f"(new corpus hash {c.short_hash}): {', '.join(c.drifted[:5])}", err=True)
    chosen = want(args.only, args.keyless)

    if args.seed_needed:
        workdir = resolve_workdir(args)
        state = ing.State(workdir / "p43-state.json")
        needed = [s.name for (_, fam), s in collection_specs(corpora, args.keyless).items()
                  if fam == "voyage" and not state.collection(s.name).get("registered")]
        return 10 if needed else 0

    p = plan_mod.make_plan(corpora, queries, keyless=args.keyless, cap=args.cap_tokens,
                           assume_voyage_rerank=args.assume_voyage_rerank)
    if args.plan:
        emit(plan_mod.format_plan(p))
        return 3 if p.refused else 0
    if p.refused:
        emit(plan_mod.format_plan(p), err=True)
        return 3

    workdir = resolve_workdir(args)
    tokens = budget.TokenBudget(cap=args.cap_tokens)
    engine = eng.Engine.from_env(tokens)
    state = ing.State(workdir / "p43-state.json")
    specs = collection_specs(corpora, args.keyless)

    if args.seed_register:
        for (kind, family), spec in specs.items():
            if family == "voyage":
                ing.register(engine, state, spec)
                emit(f"[p43] registered {spec.name}")
        return 0

    version = engine.version()
    check_engine(version, args.keyless)
    log = emit

    # Corpus collections (idempotent; Voyage ones are charged to the budget before each write).
    built = {}
    for (kind, family), spec in specs.items():
        built[spec.name] = ing.ingest(engine, state, spec, corpora[kind].chunks_by_file, log=log)
        log(f"[p43] {spec.name}: {'reused' if built[spec.name]['reused'] else 'written'} "
            f"({built[spec.name]['count']} chunks)")
    names = {key: s.name for key, s in specs.items()}

    doc: dict = {
        "meta": {
            "bead": "nexus-t189m", "rdr": "RDR-210 P4.3",
            "mode": "keyless" if args.keyless else "full", "measurements": sorted(chosen),
            "partial": bool(args.limit_queries), "limit_queries": args.limit_queries,
            "started_utc": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "seed": args.seed, "resamples": args.resamples,
            "queries_sha256": hashlib.sha256((HERE / "queries.json").read_bytes()).hexdigest(),
            "corpora": {k: {"files": len(c.files), "chunks": c.n_chunks, "corpus_hash": c.corpus_hash,
                            "file_list": c.files} for k, c in corpora.items()},
            "collections": built,
            "engine": {k: version.get(k) for k in (
                "embedding_mode", "embedding_models", "embedding_profile", "reranker",
                "reranker_available", "release_version", "build_ref")},
        },
    }

    if "A" in chosen:
        def search_a(q: str, coll: str, n: int) -> list[dict]:
            return engine.search(q, [coll], n)[0]
        raw = ma.collect_a(search_a, queries, corpora, names, log=log)
        analysis = ma.analyze_a(raw, resamples=args.resamples, seed=args.seed)
        analysis["raw"] = raw
        doc["measurement_a"] = analysis
        write_partial(workdir, doc, tokens)
    if "B" in chosen:
        bge_names = {k: names[(k, "bge")] for k in corp.KINDS}

        def spec_for(kind: str, owner_id: str) -> ing.CollectionSpec:
            return ing.CollectionSpec(
                eng.collection_name(corpora[kind].content_type, owner_id, eng.BGE_MODEL),
                corpora[kind].content_type, owner_id, ing.owner_prefix_for(owner_id), eng.BGE_MODEL)

        def ingest_fn(spec: ing.CollectionSpec, chunks_by_file: dict) -> dict:
            return ing.ingest(engine, state, spec, chunks_by_file, log=lambda *_: None)

        doc["measurement_b"] = mb.run_b(
            engine, ingest_fn, spec_for, queries, corpora, bge_names, seed=args.seed,
            shuffled_collection=not args.no_shuffled_collection, log=log)
        write_partial(workdir, doc, tokens)
    if "C" in chosen:
        reranks_voyage = engine.reranker_is_voyage()

        def query_tokens(q: str, n_collections: int) -> int:
            return budget.chars_to_tokens(len(q)) * max(1, n_collections // 2)

        def rerank_tokens(q: str, n: int) -> int:
            return n * mc_rerank_mult() * budget.chars_to_tokens(len(q) + 1500) if reranks_voyage else 0

        live = mc.LiveSurfaces(charge=tokens.charge, query_tokens=query_tokens, rerank_tokens=rerank_tokens)
        depths = tuple(int(x) for x in args.depths.split(",") if x.strip())
        doc["measurement_c"] = mc.run_c(live, queries, corpora, names, depths=depths, log=log)

    write_partial(workdir, doc, tokens, final=True)
    emit()
    emit(res.summary_text(doc))
    return 0


def resolve_workdir(args: argparse.Namespace) -> Path:
    workdir = args.workdir or (Path(os.environ["P43_WORKDIR"]) if os.environ.get("P43_WORKDIR") else None)
    if workdir is None:
        raise SystemExit("--workdir (or P43_WORKDIR) is required: the persistent directory that holds state and results")
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir


def mc_rerank_mult() -> int:
    return plan_mod.C_RERANK_MULTIPLIER


def check_engine(version: dict, keyless: bool) -> None:
    mode = version.get("embedding_mode")
    models = set(version.get("embedding_models", []))
    if keyless:
        if mode != "onnx-local":
            raise SystemExit(f"--keyless needs a bge-only engine (embedding_mode onnx-local); this one reports {mode!r}")
        return
    if mode != "dual" or not {eng.BGE_MODEL, eng.CONTEXT_MODEL, eng.CODE_MODEL} <= models:
        raise SystemExit(
            f"the full run needs a dual engine serving {eng.BGE_MODEL}, {eng.CONTEXT_MODEL} and {eng.CODE_MODEL}; "
            f"this one reports mode={mode!r} models={sorted(models)}")


def write_partial(workdir: Path, doc: dict, tokens: budget.TokenBudget, *, final: bool = False) -> None:
    doc["tokens"] = tokens.snapshot()
    out = workdir / "results"
    out.mkdir(exist_ok=True)
    res.write_results(out / "results.json", doc)
    if final:
        (out / "summary.txt").write_text(res.summary_text(doc) + "\n", encoding="utf-8")
        emit(f"[p43] wrote {out / 'results.json'} and summary.txt")


if __name__ == "__main__":
    sys.exit(main())
