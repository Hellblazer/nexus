# SPDX-License-Identifier: AGPL-3.0-or-later
"""Exercise every tool registered in the nexus MCP server (core.py).

Each tool call is run against the sandbox (T2 sqlite + local ChromaDB).
Stream per-tool pass/fail with latency so observability stays live.

Coverage — 26 tools:
  search, query, store_put, store_get, store_list, store_get_many,
  memory_put, memory_get, memory_delete, memory_search, memory_consolidate,
  scratch, scratch_manage, collection_list,
  plan_save, plan_search,
  traverse,
  operator_extract, operator_rank, operator_compare, operator_summarize,
  operator_generate,
  nx_answer, nx_tidy, nx_enrich_beads, nx_plan_audit.

Tools that require real LLM egress (operator_*) are skipped unless
NX_VALIDATE_WITH_LLM=1 is set.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import traceback
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch


# ── Observability ────────────────────────────────────────────────────────────

_pass: int = 0
_fail: int = 0
_failures: list[tuple[str, str]] = []


def ts() -> str:
    return time.strftime("%H:%M:%S")


def info(msg: str) -> None:
    print(f"[{ts()}]    {msg}", flush=True)


def step(msg: str) -> None:
    print(f"\n[{ts()}] ─── {msg} ───", flush=True)


@contextmanager
def case(name: str):
    """Exercise one tool; stream latency + verdict."""
    global _pass, _fail
    start = time.monotonic()
    try:
        yield
        dur = int((time.monotonic() - start) * 1000)
        print(f"[{ts()}]  ✓ {name}  ({dur} ms)", flush=True)
        _pass += 1
    except AssertionError as exc:
        dur = int((time.monotonic() - start) * 1000)
        print(f"[{ts()}]  ✗ {name}  ({dur} ms) — assertion: {exc}", flush=True)
        _fail += 1
        _failures.append((name, f"assertion: {exc}"))
    except Exception as exc:
        dur = int((time.monotonic() - start) * 1000)
        short = f"{type(exc).__name__}: {exc}"
        print(f"[{ts()}]  ✗ {name}  ({dur} ms) — {short}", flush=True)
        if os.environ.get("NX_VALIDATE_VERBOSE"):
            traceback.print_exc()
        _fail += 1
        _failures.append((name, short))


def skip(name: str, reason: str) -> None:
    global _pass
    print(f"[{ts()}]  ⊖ {name}  — skipped: {reason}", flush=True)


# ── Suite ────────────────────────────────────────────────────────────────────

def run_suite() -> None:
    _seed_catalog_and_plans()
    _exercise_scratch()
    _exercise_memory()
    _exercise_store()
    _exercise_store_get_many()
    _exercise_collection_list()
    _exercise_plan_save_search()
    _exercise_traverse()
    _exercise_search_query()
    _exercise_operators()
    _exercise_nx_answer()
    _exercise_nx_tidy()
    _exercise_nx_enrich_beads()
    _exercise_nx_plan_audit()


def _seed_catalog_and_plans() -> None:
    """Run `nx catalog setup` so the sandbox has the 14 seeded plans."""
    step("Seed catalog + plans")
    import subprocess
    with case("nx catalog setup (14 plans seeded)"):
        r = subprocess.run(
            ["uv", "run", "nx", "catalog", "setup"],
            capture_output=True, text=True, timeout=60,
        )
        assert r.returncode == 0, r.stderr
        info(f"  output tail: {r.stdout.strip().splitlines()[-1][:80]}")


# ── scratch / scratch_manage ─────────────────────────────────────────────────

def _exercise_scratch() -> None:
    step("scratch / scratch_manage")
    from nexus.mcp.core import scratch, scratch_manage

    with case("scratch put"):
        out = scratch(action="put", content="validation-test-note", tags="validate")
        assert isinstance(out, str)

    with case("scratch list"):
        out = scratch(action="list", limit=5)
        assert "validation-test-note" in out or "validate" in out.lower(), out[:200]

    # scratch_manage requires an entry_id; use the ID reported by put above.
    # Re-put and capture an ID to delete.
    with case("scratch_manage delete"):
        put_out = scratch(action="put", content="scratch-to-delete", tags="validate")
        # `put` returns a line containing the stored entry ID (UUID).
        import re
        m = re.search(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}", put_out)
        assert m, f"couldn't parse entry_id from: {put_out!r}"
        entry_id = m.group(0)
        out = scratch_manage(action="delete", entry_id=entry_id)
        assert isinstance(out, str)


# ── memory_* ────────────────────────────────────────────────────────────────

def _exercise_memory() -> None:
    step("memory_put / get / search / delete / consolidate")
    from nexus.mcp.core import (
        memory_put, memory_get, memory_search, memory_delete, memory_consolidate,
    )

    with case("memory_put"):
        out = memory_put(
            content="RDR-078 added plan_match and traverse.",
            project="validate",
            title="fact-001",
            tags="validate,rdr-078",
        )
        assert "fact-001" in out or "stored" in out.lower(), out[:200]

    with case("memory_get by title"):
        out = memory_get(project="validate", title="fact-001")
        assert "plan_match" in out, out[:200]

    with case("memory_search"):
        out = memory_search(query="plan_match", project="validate")
        assert "fact-001" in out or "plan_match" in out, out[:200]

    with case("memory_consolidate propose (no-op on fresh db)"):
        out = memory_consolidate(action="propose", project="validate", dry_run=True)
        assert isinstance(out, str)

    with case("memory_delete"):
        out = memory_delete(project="validate", title="fact-001")
        assert isinstance(out, str)


# ── store_put / get / list ───────────────────────────────────────────────────

def _exercise_store() -> None:
    step("store_put / store_get / store_list")
    from nexus.mcp.core import store_put, store_get, store_list

    stored_id = [None]  # closure workaround

    with case("store_put"):
        out = store_put(
            content="The retrieval layer was consolidated in RDR-080 via nx_answer.",
            collection="knowledge__validate",
            title="doc-001",
            tags="validate,rdr-080",
        )
        assert isinstance(out, str) and len(out) > 0, out[:200]
        # store_put returns the actual doc ID used — capture it for the get test.
        import re
        m = re.search(r"([a-f0-9]{16,})", out)
        if m:
            stored_id[0] = m.group(1)
        info(f"  stored_id: {stored_id[0]}")

    with case("store_get by id"):
        doc_id = stored_id[0] or "doc-001"
        out = store_get(doc_id=doc_id, collection="knowledge__validate")
        assert isinstance(out, str) and len(out) > 0, out[:200]

    with case("store_list"):
        out = store_list(collection="knowledge__validate", limit=5)
        assert isinstance(out, str)
        # At minimum mentions the collection or has content
        assert len(out) > 10, out[:200]


# ── store_get_many (at 301-ID quota boundary) ────────────────────────────────

def _exercise_store_get_many() -> None:
    step("store_get_many")
    from nexus.mcp.core import store_get_many

    with case("store_get_many 301 IDs (mocked T3)"):
        fake = {f"doc-{i:04d}": {"content": f"body-{i}"} for i in range(301)}
        mock_t3 = MagicMock()
        mock_t3.get_by_id = lambda col, doc_id: fake.get(doc_id)
        with patch("nexus.mcp.core._get_t3", return_value=mock_t3):
            result = store_get_many(
                ids=[f"doc-{i:04d}" for i in range(301)],
                collections="knowledge__validate",
                structured=True,
            )
        assert len(result["contents"]) == 301, len(result["contents"])


# ── collection_list ──────────────────────────────────────────────────────────

def _exercise_collection_list() -> None:
    step("collection_list")
    from nexus.mcp.core import collection_list

    with case("collection_list"):
        out = collection_list()
        assert isinstance(out, str)
        # Should include the collection we just wrote to
        info(f"  collections: {out.splitlines()[0][:80] if out else '(empty)'}")


# ── plan_save / plan_search ──────────────────────────────────────────────────

def _exercise_plan_save_search() -> None:
    step("plan_save / plan_search")
    from nexus.mcp.core import plan_save, plan_search

    with case("plan_save"):
        out = plan_save(
            query="walk the catalog to find implementations",
            plan_json='{"steps":[{"tool":"traverse","args":{"seeds":["1.1"],"link_types":["implements"]}}]}',
            tags="validate,test",
        )
        assert isinstance(out, str)

    with case("plan_search"):
        out = plan_search(query="walk catalog", limit=5)
        assert isinstance(out, str)
        # Seeded 14 plans + 1 we just saved should surface SOMETHING
        assert "walk" in out.lower() or "catalog" in out.lower() or len(out) > 50, out[:200]


# ── traverse ─────────────────────────────────────────────────────────────────

def _exercise_traverse() -> None:
    step("traverse")
    from nexus.mcp.core import traverse

    with case("traverse malformed seeds"):
        r = traverse(seeds=["not-a-tumbler"], link_types=["cites"])
        assert r.get("tumblers") == []

    with case("traverse SC-16 mutual exclusion"):
        r = traverse(seeds=["1.1"], link_types=["a"], purpose="find-implementations")
        assert "error" in r

    with case("traverse empty seed"):
        r = traverse(seeds=[], link_types=["cites"])
        assert r == {"tumblers": [], "ids": [], "collections": []}


# ── search / query (empty corpus is expected) ────────────────────────────────

def _exercise_search_query() -> None:
    step("search / query (sandbox has no corpus; smoke only)")
    from nexus.mcp.core import search, query

    with case("search (empty corpus → graceful)"):
        out = search(query="anything", limit=3)
        assert isinstance(out, str)

    with case("query (empty corpus → graceful)"):
        out = query(question="anything", limit=3)
        assert isinstance(out, str)


# ── operators (LLM-backed; skipped by default) ───────────────────────────────

def _exercise_operators() -> None:
    step("operator_* (LLM-backed)")
    if os.environ.get("NX_VALIDATE_WITH_LLM") != "1":
        for n in ("operator_extract", "operator_rank", "operator_compare",
                  "operator_summarize", "operator_generate"):
            skip(n, "LLM off (set NX_VALIDATE_WITH_LLM=1 to enable)")
        return

    from nexus.mcp.core import (
        operator_extract, operator_rank, operator_compare,
        operator_summarize, operator_generate,
    )
    inputs = ["The retrieval layer was simplified.", "Plans now carry dimensions."]

    with case("operator_extract"):
        out = operator_extract(inputs=inputs, fields="rdr,component")
        assert isinstance(out, (str, dict))
    with case("operator_rank"):
        out = operator_rank(inputs=inputs, criterion="relevance to RDR-080")
        assert isinstance(out, (str, dict))
    with case("operator_compare"):
        out = operator_compare(inputs=inputs, criterion="scope")
        assert isinstance(out, (str, dict))
    with case("operator_summarize"):
        out = operator_summarize(inputs=inputs)
        assert isinstance(out, (str, dict))
    with case("operator_generate"):
        out = operator_generate(outline="one-line summary of retrieval layer", inputs=inputs)
        assert isinstance(out, (str, dict))


# ── nx_answer (orchestration trunk, plan_run mocked) ─────────────────────────

def _exercise_nx_answer() -> None:
    step("nx_answer — orchestration trunk")
    from nexus.plans.runner import PlanResult

    async def _run():
        import nexus.plans.runner as _runner
        with patch.object(
            _runner, "plan_run",
            AsyncMock(return_value=PlanResult(steps=[{"text": "mocked trunk answer"}]))
        ):
            from nexus.mcp.core import nx_answer
            r = await nx_answer("design planning corpus", scope="global")
        assert isinstance(r, str)
        assert len(r) > 0

    with case("nx_answer trunk (real plan_match + mocked plan_run)"):
        asyncio.run(_run())


# ── Stub replacements (all 3 are LLM-backed via claude -p subprocess) ────────

def _exercise_nx_tidy() -> None:
    step("nx_tidy (replaces knowledge-tidier agent; LLM-backed)")
    if os.environ.get("NX_VALIDATE_WITH_LLM") != "1":
        skip("nx_tidy", "spawns claude -p subprocess (set NX_VALIDATE_WITH_LLM=1)")
        return
    from nexus.mcp.core import nx_tidy

    async def _run():
        out = await nx_tidy(topic="chromadb quotas", timeout=60.0)
        assert isinstance(out, str) and len(out) > 0
        info(f"  result head: {out.splitlines()[0][:100] if out else '(empty)'}")

    with case("nx_tidy (claude -p subprocess)"):
        asyncio.run(_run())


def _exercise_nx_enrich_beads() -> None:
    step("nx_enrich_beads (replaces plan-enricher agent; LLM-backed)")
    if os.environ.get("NX_VALIDATE_WITH_LLM") != "1":
        skip("nx_enrich_beads", "spawns claude -p subprocess (set NX_VALIDATE_WITH_LLM=1)")
        return
    from nexus.mcp.core import nx_enrich_beads

    async def _run():
        out = await nx_enrich_beads(
            bead_description="Add CLI flag --foo to nx search that filters by tag",
            context="",
            timeout=60.0,
        )
        assert isinstance(out, str) and len(out) > 0
        info(f"  result head: {out.splitlines()[0][:100] if out else '(empty)'}")

    with case("nx_enrich_beads (claude -p subprocess)"):
        asyncio.run(_run())


def _exercise_nx_plan_audit() -> None:
    step("nx_plan_audit (replaces plan-auditor agent; LLM-backed)")
    if os.environ.get("NX_VALIDATE_WITH_LLM") != "1":
        skip("nx_plan_audit", "spawns claude -p subprocess (set NX_VALIDATE_WITH_LLM=1)")
        return
    from nexus.mcp.core import nx_plan_audit

    async def _run():
        plan = '{"steps":[{"tool":"search","args":{"query":"$topic"}}]}'
        out = await nx_plan_audit(plan_json=plan, context="", timeout=60.0)
        assert isinstance(out, str) and len(out) > 0
        info(f"  result head: {out.splitlines()[0][:100] if out else '(empty)'}")

    with case("nx_plan_audit (claude -p subprocess)"):
        asyncio.run(_run())


# ── entrypoint ───────────────────────────────────────────────────────────────

def main() -> int:
    print(f"[{ts()}] MCP core tools live exercise — sandbox={os.environ.get('HOME')}")
    try:
        run_suite()
    finally:
        print(f"\n[{ts()}] ── mcp-core: {_pass} pass, {_fail} fail ──")
        for name, err in _failures:
            print(f"       - {name}: {err}")
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
