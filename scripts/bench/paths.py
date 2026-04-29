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

The path-B/C handlers need a ``T3Database`` to resolve
``chunk_text_hash`` → ``source_path`` for the structured envelope's
chunks. The handlers accept it as an explicit dependency rather than
re-creating one per query.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from bench.metrics import dedupe_by_doc, grade_for_path, ndcg_at_k
from bench.schema import Query

K = 3


def _resolve_source_path(t3: Any, collection: str, chash: str) -> str:
    """Look up a chunk's ``source_path`` by ``chunk_text_hash`` via raw chroma.

    Returns ``""`` on miss. ``T3Database`` doesn't expose
    ``get_collection``; the raw chromadb client is at ``t3._client``.
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
    except Exception:
        return ""
    return ""


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
        "path": "A", "qid": query.qid, "elapsed_s": elapsed, "error": None,
        "chunks": deduped, "raw_chunk_count": len(raw),
        "grades": grades,
        "ndcg_at_3": ndcg_at_k(grades, query.ground_truth, k=K),
    }


def _run_nx_answer(
    query: Query,
    t3: Any,
    *,
    path_label: str,
    answer_kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Run ``nx_answer(structured=True)`` and grade the returned chunks."""
    from nexus.mcp.core import nx_answer  # noqa: PLC0415

    t0 = time.monotonic()
    try:
        envelope = asyncio.run(
            nx_answer(question=query.text, structured=True, trace=False, **answer_kwargs)
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
    raw = []
    for c in raw_chunks:
        chash = c.get("chash", "")
        coll = c.get("collection", "")
        sp = _resolve_source_path(t3, coll, chash) if (chash and coll) else ""
        raw.append({
            "id": c.get("id", ""),
            "source_path": sp,
            "content_hash": chash,
            "collection": coll,
            "distance": c.get("distance", 0.0),
        })
    deduped = dedupe_by_doc(raw)[:K]
    grades = [grade_for_path(c["source_path"], query.ground_truth) for c in deduped]
    return {
        "path": path_label, "qid": query.qid,
        "scope": answer_kwargs.get("scope", ""),
        "elapsed_s": elapsed, "error": None,
        "chunks": deduped, "raw_chunk_count": len(raw),
        "grades": grades,
        "ndcg_at_3": ndcg_at_k(grades, query.ground_truth, k=K),
        "plan_id": envelope.get("plan_id"),
        "step_count": envelope.get("step_count"),
    }


def run_path_b(query: Query, t3: Any, *, scope: str) -> dict[str, Any]:
    """Path B — plan-library-routed via ``scope``."""
    return _run_nx_answer(
        query, t3, path_label="B", answer_kwargs={"scope": scope},
    )


def run_path_c(query: Query, t3: Any, *, corpus: str) -> dict[str, Any]:
    """Path C — ``force_dynamic=True`` (post-#346) with a fallback to
    ``scope=corpus`` for the spike-preview semantics on older branches.
    """
    res = _run_nx_answer(
        query, t3, path_label="C",
        answer_kwargs={"force_dynamic": True, "scope": corpus},
    )
    if res.get("error", "").startswith("unsupported_kwarg"):
        # #346 not present — fall back to the spike's path-C preview:
        # collection-scoped scope forces a plan-match miss.
        res = _run_nx_answer(
            query, t3, path_label="C",
            answer_kwargs={"scope": corpus},
        )
        if res.get("error") is None:
            res["fallback"] = "scope-as-corpus"
    return res
