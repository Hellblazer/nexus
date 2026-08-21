# SPDX-License-Identifier: AGPL-3.0-or-later
"""Three retrieval-path handlers for the bench harness.

  * **Path A** — ``nx search --json`` CLI restricted to a corpus,
    over-fetched 3x so dedupe-by-doc still leaves K unique docs.
  * **Path B** — ``nx_answer(scope=…, structured=True)``: the
    plan-library-routed path. Captures cross-project leakage when
    ``scope`` is a bare prefix (e.g. ``"rdr"``).
  * **Path C** — ``nx_answer(force_dynamic=True)``: forces plan-match
    miss so the inline LLM planner runs. Requires ``force_dynamic``
    (RDR-090 P1.1, PR #346); falls back to ``scope=corpus`` (the
    spike's preview semantics) when the kwarg isn't recognized — that
    fallback is preserved so the harness runs against pre-#346
    branches without changing behavior.

Chunk -> path resolution (nexus-hmu02): catalog-aware indexing no longer
writes ``source_path`` into chunk metadata (RDR-108 P3). Path A reads the
catalog-resolved ``_display_path`` that ``nx search --json`` attaches
(``search_engine._attach_display_paths``), falling back to the legacy
keys. Path B/C resolve the structured envelope's ``chash`` through the
catalog manifest (``docs_for_chashes`` -> ``resolve_many`` ->
``entry.file_path``), the same chain ``nx search`` uses; the handlers take
the catalog reader as an explicit dependency. A result set whose every
chunk resolves to an empty path is reported as a ``vacuous`` ERROR, never
scored 0.0 (AGENTS.md non-vacuity rule; the pre-fix bench scored 0.0 on
every query for exactly this reason).
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import time
from typing import Any

from bench.metrics import dedupe_by_doc, grade_for_path, ndcg_at_k
from bench.schema import Query

K = 3


_PATH_KEYS = ("_display_path", "source_path", "file_path")
_VACUOUS = "vacuous: every retrieved chunk resolved to an empty path"


def _chunk_path(chunk: dict[str, Any]) -> str:
    """Catalog-resolved ``_display_path`` first, legacy keys after."""
    for key in _PATH_KEYS:
        val = chunk.get(key)
        if val:
            return str(val)
    return ""


def _resolve_paths_via_catalog(
    catalog: Any | None, chashes: list[str],
) -> dict[str, str]:
    """Map each chash to its document's file path via the catalog manifest.

    Two batched round-trips regardless of input size. Unknown chashes (or
    no catalog) map to ``""`` so the caller's non-vacuity guard can fire.
    """
    wanted = sorted({c for c in chashes if c})
    out = {c: "" for c in wanted}
    if catalog is None or not wanted:
        return out
    chash_to_docs = catalog.docs_for_chashes(wanted) or {}
    doc_ids = sorted({d for docs in chash_to_docs.values() for d in docs})
    entries = catalog.resolve_many(doc_ids) if doc_ids else {}
    for chash, docs in chash_to_docs.items():
        # Sorted: a chash shared by several docs (identical chunk text
        # collapses to one T3 row) must resolve the same way every run.
        for doc_id in sorted(docs):
            entry = entries.get(doc_id)
            if entry is not None and getattr(entry, "file_path", ""):
                out[chash] = entry.file_path
                break
    return out


def _grade(raw: list[dict[str, Any]], query: Query) -> dict[str, Any]:
    """Dedupe, grade, score; flag the all-empty-path case as vacuous.

    A partially unresolved set still scores (an unresolved chunk grades 0
    and occupies one of the K slots, the conservative reading), but the
    count is reported as ``unresolved_count`` so a depressed NDCG can be
    told apart from a genuinely poor retrieval.
    """
    unresolved = sum(1 for c in raw if not c["source_path"])
    if raw and unresolved == len(raw):
        return {"chunks": [], "grades": [], "ndcg_at_3": 0.0,
                "unresolved_count": unresolved, "error": _VACUOUS}
    deduped = dedupe_by_doc(raw)[:K]
    grades = [grade_for_path(c["source_path"], query.ground_truth) for c in deduped]
    return {"chunks": deduped, "grades": grades,
            "ndcg_at_3": ndcg_at_k(grades, query.ground_truth, k=K),
            "unresolved_count": unresolved, "error": None}


def run_path_a(query: Query, *, corpus: str) -> dict[str, Any]:
    """Path A — ``nx search`` CLI restricted to a single corpus."""
    t0 = time.monotonic()
    proc = subprocess.run(
        ["nx", "search", query.text, "--corpus", corpus,
         "-m", str(K * 3), "--json"],
        capture_output=True, text=True, timeout=60,
    )
    elapsed = time.monotonic() - t0
    if proc.returncode != 0:
        return {
            "path": "A", "qid": query.qid, "elapsed_s": elapsed,
            "error": proc.stderr.strip()[:500],
            "chunks": [], "grades": [], "ndcg_at_3": 0.0,
        }
    try:
        chunks = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        return {
            "path": "A", "qid": query.qid, "elapsed_s": elapsed,
            "error": f"json decode: {e}",
            "chunks": [], "grades": [], "ndcg_at_3": 0.0,
        }
    raw = [
        {
            "id": c.get("id", ""),
            "source_path": _chunk_path(c),
            "content_hash": c.get("chunk_text_hash", "") or c.get("content_hash", ""),
            "distance": c.get("distance", 0.0),
            "section_title": c.get("section_title", ""),
        }
        for c in chunks
    ]
    return {
        "path": "A", "qid": query.qid, "elapsed_s": elapsed,
        "raw_chunk_count": len(raw), **_grade(raw, query),
    }


def _run_nx_answer(
    query: Query,
    catalog: Any | None,
    *,
    path_label: str,
    answer_kwargs: dict[str, Any],
    answer_fn: Any = None,
) -> dict[str, Any]:
    """Run ``nx_answer(structured=True)`` and grade the returned chunks.

    ``answer_fn`` is an injection seam for tests; it defaults to the real
    ``nexus.mcp.core.nx_answer`` (imported lazily — it is heavy).
    """
    if answer_fn is None:
        from nexus.mcp.core import nx_answer as answer_fn  # noqa: PLC0415

    t0 = time.monotonic()
    try:
        envelope = asyncio.run(
            answer_fn(question=query.text, structured=True, trace=False, **answer_kwargs)
        )
    except TypeError as e:
        # Unrecognized kwarg (likely force_dynamic on a pre-#346 branch).
        # Caller-supplied fallback handler can convert this; we surface
        # a typed error so the runner knows it's recoverable.
        return {
            "path": path_label, "qid": query.qid,
            "scope": answer_kwargs.get("scope", ""),
            "elapsed_s": time.monotonic() - t0,
            "error": f"unsupported_kwarg: {e}",
            "chunks": [], "grades": [], "ndcg_at_3": 0.0,
            "plan_id": None, "step_count": None,
        }
    except Exception as e:
        return {
            "path": path_label, "qid": query.qid,
            "scope": answer_kwargs.get("scope", ""),
            "elapsed_s": time.monotonic() - t0,
            "error": f"{type(e).__name__}: {e}",
            "chunks": [], "grades": [], "ndcg_at_3": 0.0,
            "plan_id": None, "step_count": None,
        }
    elapsed = time.monotonic() - t0
    if not isinstance(envelope, dict):
        return {
            "path": path_label, "qid": query.qid,
            "scope": answer_kwargs.get("scope", ""),
            "elapsed_s": elapsed,
            "error": f"non-envelope response (type={type(envelope).__name__})",
            "chunks": [], "grades": [], "ndcg_at_3": 0.0,
            "plan_id": None, "step_count": None,
        }
    raw_chunks = envelope.get("chunks") or []
    try:
        paths = _resolve_paths_via_catalog(
            catalog, [c.get("chash", "") for c in raw_chunks],
        )
    except Exception as e:  # noqa: BLE001 — a catalog outage is a typed bench error, not a crash
        return {
            "path": path_label, "qid": query.qid,
            "scope": answer_kwargs.get("scope", ""),
            "elapsed_s": elapsed,
            "error": f"catalog_resolve: {type(e).__name__}: {e}",
            "chunks": [], "grades": [], "ndcg_at_3": 0.0,
            "raw_chunk_count": len(raw_chunks),
            "plan_id": envelope.get("plan_id"),
            "step_count": envelope.get("step_count"),
        }
    raw = [
        {
            "id": c.get("id", ""),
            "source_path": paths.get(c.get("chash", ""), ""),
            "content_hash": c.get("chash", ""),
            "collection": c.get("collection", ""),
            "distance": c.get("distance", 0.0),
        }
        for c in raw_chunks
    ]
    return {
        "path": path_label, "qid": query.qid,
        "scope": answer_kwargs.get("scope", ""),
        "elapsed_s": elapsed, "raw_chunk_count": len(raw),
        **_grade(raw, query),
        "plan_id": envelope.get("plan_id"),
        "step_count": envelope.get("step_count"),
    }


def run_path_b(query: Query, catalog: Any | None, *, scope: str) -> dict[str, Any]:
    """Path B — plan-library-routed via ``scope``."""
    return _run_nx_answer(
        query, catalog, path_label="B", answer_kwargs={"scope": scope},
    )


def run_path_c(query: Query, catalog: Any | None, *, corpus: str) -> dict[str, Any]:
    """Path C — ``force_dynamic=True`` (post-#346) with a fallback to
    ``scope=corpus`` for the spike-preview semantics on older branches.
    """
    res = _run_nx_answer(
        query, catalog, path_label="C",
        answer_kwargs={"force_dynamic": True, "scope": corpus},
    )
    if (res.get("error") or "").startswith("unsupported_kwarg"):
        # #346 not present — fall back to the spike's path-C preview:
        # collection-scoped scope forces a plan-match miss.
        res = _run_nx_answer(
            query, catalog, path_label="C",
            answer_kwargs={"scope": corpus},
        )
        if res.get("error") is None:
            res["fallback"] = "scope-as-corpus"
    return res
