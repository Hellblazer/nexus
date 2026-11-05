# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-243 Phase 1 Step 4a (nexus-rfvyy.6.13): the sensitivity scope.

Pins, in order:

* the scope itself (``nexus.sensitivity``): posture lookup order, nesting, reset;
* the three dispatch roots each raise ``DispatchOutsideSensitivityScope`` outside
  the scope, before any side effect, and run inside it;
* the three thread hops (``run_text_sync``'s helper thread, the taxonomy
  labelling pool, the aspect worker thread) dispatch inside the scope;
* every current surface is wrapped, under the registry name of its census row.

The suite as a whole runs inside the scope (``tests/conftest.py``
``_suite_runs_inside_the_sensitivity_scope`` sets ``NX_SENSITIVITY_POSTURE``), so
the many tests that call a root directly keep working. This module undoes that
one fixture to see the roots as production sees them.
"""
from __future__ import annotations

import ast
import asyncio
import contextvars
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from nexus import sensitivity as sens
from nexus.sensitivity import (
    DispatchOutsideSensitivityScope,
    SensitiveDocumentRefused,
    assert_llm_bound,
    bound_to,
    current_posture,
    current_surface,
    llm_bound,
)
from nexus.tables import llm_surfaces

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"


@pytest.fixture(autouse=True)
def outside_the_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production posture: no environment hint, no context posture."""
    monkeypatch.delenv(sens.POSTURE_ENV, raising=False)
    assert current_posture() == "default"


# --------------------------------------------------------------------------- #
# The scope.
# --------------------------------------------------------------------------- #


def test_posture_is_default_outside_and_llm_bound_inside_and_resets() -> None:
    assert (current_posture(), current_surface()) == ("default", None)
    with llm_bound("nx_tidy"):
        assert (current_posture(), current_surface()) == ("llm_bound", "nx_tidy")
    assert (current_posture(), current_surface()) == ("default", None)


def test_scopes_nest_and_the_outer_name_returns() -> None:
    with llm_bound("nx_answer_operators"):
        with llm_bound("nx_answer_plan_helpers"):
            assert current_surface() == "nx_answer_plan_helpers"
        assert current_surface() == "nx_answer_operators"
        assert current_posture() == "llm_bound"
    assert current_posture() == "default"


def test_the_scope_resets_when_the_body_raises() -> None:
    with pytest.raises(RuntimeError):
        with llm_bound("nx_tidy"):
            raise RuntimeError("boom")
    assert (current_posture(), current_surface()) == ("default", None)


def test_the_environment_decides_only_when_the_context_var_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(sens.POSTURE_ENV, "llm_bound")
    assert current_posture() == "llm_bound"
    assert current_surface() is None
    monkeypatch.setenv(sens.POSTURE_ENV, "anything-else")
    assert current_posture() == "default", "only the exact value llm_bound counts"
    monkeypatch.setenv(sens.POSTURE_ENV, "")
    assert current_posture() == "default"


def test_a_thread_that_copies_no_context_does_not_see_the_scope() -> None:
    """The reason the three hops exist: a plain thread starts with an empty context."""
    seen: list[str] = []
    with llm_bound("nx_tidy"):
        t = threading.Thread(target=lambda: seen.append(current_posture()))
        t.start()
        t.join()
        copied: list[str] = []
        ctx = contextvars.copy_context()
        t2 = threading.Thread(target=lambda: copied.append(ctx.run(current_posture)))
        t2.start()
        t2.join()
    assert seen == ["default"] and copied == ["llm_bound"]


def test_assert_llm_bound_names_the_root_and_the_exceptions_carry_their_fields() -> None:
    with pytest.raises(DispatchOutsideSensitivityScope) as err:
        assert_llm_bound("claude_dispatch")
    assert err.value.root == "claude_dispatch" and err.value.surface is None
    assert "claude_dispatch" in str(err.value)
    with llm_bound("nx_tidy"):
        assert_llm_bound("claude_dispatch")
    refused = SensitiveDocumentRefused("1.2.3", surface="enrich_aspects")
    assert (refused.tumbler, refused.surface) == ("1.2.3", "enrich_aspects")


def test_bound_to_wraps_sync_and_async_and_keeps_the_signature() -> None:
    import inspect

    @bound_to("nx_tidy")
    def sync_fn(a: int, b: int = 2) -> tuple[int, str | None]:
        return a + b, current_surface()

    @bound_to("nx_plan_audit")
    async def async_fn(a: int) -> str | None:
        return current_surface()

    assert sync_fn(1) == (3, "nx_tidy")
    assert asyncio.run(async_fn(1)) == "nx_plan_audit"
    assert inspect.iscoroutinefunction(async_fn) and not inspect.iscoroutinefunction(sync_fn)
    assert list(inspect.signature(sync_fn).parameters) == ["a", "b"]
    assert current_posture() == "default"


# --------------------------------------------------------------------------- #
# The three dispatch roots.
# --------------------------------------------------------------------------- #


class _FakeBackend:
    """An LLMBackend that records the posture and thread it was run on."""

    name = "scope-fake"

    def __init__(self) -> None:
        self.runs: list[tuple[str, str | None, int]] = []

    def available(self) -> Any:
        from nexus.operators.llm_backend import BackendStatus  # noqa: PLC0415 - deferred import, as the dispatch module does

        return BackendStatus(name=self.name, available=True)

    async def run(self, req: Any) -> Any:
        from nexus.operators.llm_backend import DispatchResult  # noqa: PLC0415 - as above

        self.runs.append((current_posture(), current_surface(), threading.get_ident()))
        if req.json_schema is None:
            return DispatchResult(text="ok")
        return DispatchResult(structured={})


@pytest.fixture
def fake_backend(monkeypatch: pytest.MonkeyPatch) -> _FakeBackend:
    from nexus.operators import dispatch, llm_backend  # noqa: PLC0415 - deferred import

    backend = _FakeBackend()
    monkeypatch.setattr(dispatch, "_backend_for", lambda req: backend)
    monkeypatch.setattr(llm_backend, "build_api_backend", lambda name: backend)
    return backend


def test_claude_dispatch_refuses_outside_the_scope_before_any_backend_work(fake_backend: _FakeBackend) -> None:
    from nexus.operators.dispatch import claude_dispatch

    with pytest.raises(DispatchOutsideSensitivityScope) as err:
        asyncio.run(claude_dispatch("p", {"type": "object", "properties": {}}))
    assert err.value.root == "claude_dispatch"
    assert fake_backend.runs == [], "the assert runs before the backend is chosen or run"


def test_run_text_sync_refuses_outside_the_scope_before_it_builds_a_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.operators import llm_backend

    def must_not_build(name: str) -> Any:
        raise AssertionError("build_api_backend ran before the scope assert")

    monkeypatch.setattr(llm_backend, "build_api_backend", must_not_build)
    with pytest.raises(DispatchOutsideSensitivityScope) as err:
        llm_backend.run_text_sync("anthropic", "p", model=None, timeout=1.0)
    assert err.value.root == "run_text_sync"


def test_run_claude_isolated_refuses_outside_the_scope_before_it_spawns() -> None:
    from nexus.aspect_extractor import _run_claude_isolated

    with pytest.raises(DispatchOutsideSensitivityScope) as err:
        _run_claude_isolated("p", 5.0, _argv=["/nonexistent/nexus-scope-test-binary"])
    assert err.value.root == "_run_claude_isolated", "a spawn would have raised FileNotFoundError instead"


def test_each_root_runs_inside_the_scope(fake_backend: _FakeBackend) -> None:
    from nexus.aspect_extractor import _run_claude_isolated
    from nexus.operators.dispatch import claude_dispatch
    from nexus.operators.llm_backend import run_text_sync

    with llm_bound("nx_answer_operators"):
        asyncio.run(claude_dispatch("p", {"type": "object", "properties": {}}))
    with llm_bound("aspect_extraction_api"):
        assert run_text_sync("anthropic", "p", model=None, timeout=1.0).text == "ok"
    with llm_bound("aspect_extraction_cli"):
        done = _run_claude_isolated("p", 30.0, _argv=[sys.executable, "-c", "print('ok')"])
    assert done.returncode == 0
    assert [(p, s) for p, s, _ in fake_backend.runs] == [
        ("llm_bound", "nx_answer_operators"), ("llm_bound", "aspect_extraction_api"),
    ]


# --------------------------------------------------------------------------- #
# The three thread hops.
# --------------------------------------------------------------------------- #


def test_hop_1_run_text_sync_helper_thread_sees_the_scope(fake_backend: _FakeBackend) -> None:
    """With a loop already running on the caller's thread, ``run_text_sync`` runs the call on a
    helper thread. The scope reaches it through a copied context."""
    from nexus.operators.llm_backend import run_text_sync

    async def caller() -> str:
        return run_text_sync("anthropic", "p", model=None, timeout=1.0).text or ""

    main_thread = threading.get_ident()
    with llm_bound("aspect_extraction_api"):
        assert asyncio.run(caller()) == "ok"
    ((posture, surface, thread),) = fake_backend.runs
    assert thread != main_thread, "the call ran on the helper thread"
    assert (posture, surface) == ("llm_bound", "aspect_extraction_api")


def test_hop_2_taxonomy_labelling_pool_dispatches_inside_the_scope(
    fake_backend: _FakeBackend, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nexus.mcp_infra as mcp_infra
    from nexus.commands import taxonomy_cmd

    class Taxonomy:
        def get_unreviewed_topics(self, collection: str = "", limit: int = 0) -> list[dict]:
            return [
                {"id": i, "label": "a b c", "terms": '["a", "b", "c"]', "review_status": "pending"} for i in range(1, 4)
            ]

        def get_topic_doc_ids(self, topic_id: int, limit: int = 0) -> list[str]:
            return []

        def get_projection_counts_by_collection(self) -> dict[str, int]:
            return {}

    persisted: list[Any] = []
    monkeypatch.setattr(mcp_infra, "t2_index_write", lambda fn: persisted.append(fn))
    main_thread = threading.get_ident()
    # batch_size 1 gives three batches, so the pool runs three tasks.
    taxonomy_cmd.relabel_topics(Taxonomy(), batch_size=1, workers=3)  # type: ignore[arg-type]
    assert len(fake_backend.runs) == 3, f"each batch dispatched once: {fake_backend.runs}"
    assert all(t != main_thread for _, _, t in fake_backend.runs), "the batches ran on pool threads"
    assert {(p, s) for p, s, _ in fake_backend.runs} == {("llm_bound", "taxonomy_label")}


def test_hop_3_aspect_worker_thread_enters_the_scope_per_drained_row(monkeypatch: pytest.MonkeyPatch) -> None:
    import nexus.aspect_worker as aw
    import nexus.commands.enrich as enrich
    import nexus.mcp_infra as mcp_infra
    from nexus.db.t2.records import QueueRow

    seen: list[tuple[str, str | None, str]] = []
    worker = aw.AspectExtractionWorker(poll_interval=0.05)

    def fake_extract(**kwargs: Any) -> None:
        # Stop the loop whether or not the assert raises: a missing wrapper must fail this test,
        # not leave a non-daemon worker thread polling forever (the mutant run hung on that).
        worker._stop_event.set()
        seen.append((current_posture(), current_surface(), threading.current_thread().name))
        assert_llm_bound("claude_dispatch")
        return None

    claimed: list[Any] = []

    def claim() -> list[Any]:
        if claimed:
            return []
        row = QueueRow(
            collection="knowledge__scope-test__minilm-l6-v2-384__v1", source_path="/tmp/x.md",
            content_hash="h", content="body", retry_count=0,
        )
        claimed.append(row)
        return [row]

    monkeypatch.setattr(worker, "_claim_table_rows", claim)
    monkeypatch.setattr(worker._tuple_side, "supported", lambda: False)
    monkeypatch.setattr(aw, "_extract_aspects", fake_extract)
    monkeypatch.setattr(enrich, "_build_catalog_manifest_lookup", lambda: None)
    monkeypatch.setattr(mcp_infra, "t2_index_write", lambda fn: None)
    t = threading.Thread(target=worker._run_loop, name="aspect-worker-under-test", daemon=True)
    t.start()
    t.join(timeout=30)
    worker._stop_event.set()
    assert not t.is_alive(), "the worker loop stopped"
    assert seen == [("llm_bound", "aspect_worker_drain", "aspect-worker-under-test")], seen


# --------------------------------------------------------------------------- #
# Every current surface is wrapped, under its census row's name.
# --------------------------------------------------------------------------- #

#: (module, qualname) of every surface entry point -> the registry name it enters.
#: An entry is wrapped by ``@bound_to("<name>")`` or holds ``with llm_bound("<name>")``.
WRAPPED_ENTRY_POINTS: dict[tuple[str, str], str] = {
    **{("nexus.mcp.core", f"operator_{op}"): "nx_answer_operators"
       for op in ("extract", "rank", "compare", "summarize", "generate", "filter", "check", "verify", "groupby", "aggregate")},
    ("nexus.mcp.core", "nx_answer"): "nx_answer_operators",
    ("nexus.mcp.core", "_nx_answer_plan_miss"): "nx_answer_plan_helpers",
    ("nexus.mcp.core", "_nx_answer_request_coverage"): "nx_answer_plan_helpers",
    ("nexus.mcp.core", "_generalize_grown_match_description"): "nx_answer_plan_helpers",
    ("nexus.mcp.core", "nx_tidy"): "nx_tidy",
    ("nexus.mcp.core", "nx_enrich_beads"): "nx_enrich_beads",
    ("nexus.mcp.core", "nx_plan_audit"): "nx_plan_audit",
    ("nexus.plans.bundle", "dispatch_bundle"): "nx_answer_bundle",
    ("nexus.memory_rollup", "dispatch_summarizer._summarize"): "memory_rollup",
    ("nexus.commands.taxonomy_cmd", "relabel_topics"): "taxonomy_label",
    ("nexus.commands.taxonomy_cmd", "_review_auto"): "taxonomy_review_auto",
    ("nexus.commands.enrich", "enrich_aspects"): "enrich_aspects",
    ("nexus.commands.enrich", "_run_validation_sample"): "validate_sample",
    ("nexus.aspect_worker", "AspectExtractionWorker._process_row"): "aspect_worker_drain",
    ("nexus.aspect_worker", "AspectExtractionWorker._process_batch"): "aspect_worker_drain",
    ("nexus.aspect_worker", "AspectExtractionWorker._extract_tuple_rows"): "aspect_worker_drain",
}


def _find_def(tree: ast.AST, qualname: str) -> ast.AST | None:
    node: ast.AST = tree
    for part in qualname.split("."):
        found = None
        for child in ast.walk(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and child.name == part:
                found = child
                break
        if found is None:
            return None
        node = found
    return node


def _scope_names_in(node: ast.AST) -> set[str]:
    """Surface names a def enters: its ``bound_to`` decorators, plus ``with llm_bound(...)`` in its body."""
    names: set[str] = set()
    decorators = getattr(node, "decorator_list", [])
    for dec in decorators:
        if isinstance(dec, ast.Call) and getattr(dec.func, "id", getattr(dec.func, "attr", "")) == "bound_to":
            if dec.args and isinstance(dec.args[0], ast.Constant):
                names.add(str(dec.args[0].value))
    for sub in ast.walk(node):
        if isinstance(sub, (ast.With, ast.AsyncWith)):
            for item in sub.items:
                call = item.context_expr
                if isinstance(call, ast.Call) and getattr(call.func, "id", getattr(call.func, "attr", "")) == "llm_bound":
                    if call.args and isinstance(call.args[0], ast.Constant):
                        names.add(str(call.args[0].value))
    return names


@pytest.mark.parametrize(("entry", "surface"), sorted(WRAPPED_ENTRY_POINTS.items()))
def test_surface_entry_point_enters_the_scope_under_its_census_name(entry: tuple[str, str], surface: str) -> None:
    module, qualname = entry
    path = SRC / Path(*module.split(".")).with_suffix(".py")
    node = _find_def(ast.parse(path.read_text(encoding="utf-8")), qualname)
    assert node is not None, f"{module}:{qualname} does not exist"
    assert surface in _scope_names_in(node), (
        f"{module}:{qualname} must enter llm_bound({surface!r}) (@bound_to or a with-block)"
    )
    llm_surfaces.row_for(surface)  # KeyError: the name is not a registry row


@pytest.mark.lint
def test_every_surface_name_used_in_the_source_is_a_registry_row() -> None:
    used: dict[str, list[str]] = {}
    for path in sorted((SRC / "nexus").rglob("*.py")):
        if path.name == "sensitivity.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) in {"bound_to", "llm_bound"}:
                assert node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str), (
                    f"{path.relative_to(REPO_ROOT)}:{node.lineno}: the surface name must be a string literal"
                )
                used.setdefault(str(node.args[0].value), []).append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    known = {r.name for r in llm_surfaces.rows()}
    assert set(used) <= known, f"surface names that are not registry rows: {sorted(set(used) - known)}"
    wrapped_sites = sum(len(v) for v in used.values())
    assert wrapped_sites >= len(WRAPPED_ENTRY_POINTS), f"only {wrapped_sites} scope entries found in src/nexus"


#: The dispatch roots' own homes: the claude -p argv inside ``_run_claude_isolated`` and
#: inside ``CliBackend.run`` are the roots (or sit under them), which assert themselves.
ROOT_HOMES: frozenset[str] = frozenset({
    "nexus.aspect_extractor:_run_claude_isolated",
    "nexus.operators.dispatch:CliBackend.run",
})

#: Sites that hold a dispatch but are reached from a wrapped entry point rather than wrapped
#: themselves: site (``module:qualname``) -> the ``WRAPPED_ENTRY_POINTS`` keys it runs under.
#: This is an attested table, not a proof: a new site must be declared here or wrapped.
REACHED_UNDER: dict[str, tuple[tuple[str, str], ...]] = {
    "nexus.aspect_extractor:_api_inner_text": (
        ("nexus.commands.enrich", "enrich_aspects"),
        ("nexus.aspect_worker", "AspectExtractionWorker._process_row"),
        ("nexus.aspect_worker", "AspectExtractionWorker._process_batch"),
        ("nexus.aspect_worker", "AspectExtractionWorker._extract_tuple_rows"),
    ),
    "nexus.commands.taxonomy_cmd:_generate_labels_batch": (("nexus.commands.taxonomy_cmd", "relabel_topics"),),
    "nexus.commands.taxonomy_cmd:_generate_review_verdicts_batch": (("nexus.commands.taxonomy_cmd", "_review_auto"),),
}


def uncovered_sites(sites: Any, wrapped: dict[tuple[str, str], str], reached: dict[str, tuple[tuple[str, str], ...]]) -> list[str]:
    """Dispatch / claude-argv sites with no wrapped enclosing def and no declared wrapped entry point."""
    bad: list[str] = []
    for site in sites:
        if site.kind == "httpx" or site.where in ROOT_HOMES:
            continue
        parts = site.qualname.split(".")
        prefixes = [".".join(parts[: i + 1]) for i in range(len(parts))]
        if any((site.module, p) in wrapped for p in prefixes):
            continue
        entries = reached.get(site.where, ())
        if entries and all(e in wrapped for e in entries):
            continue
        bad.append(f"{site.where} (line {site.line})")
    return bad


@pytest.mark.lint
def test_every_dispatch_site_is_wrapped_or_declared_reached_under_a_wrapped_entry_point() -> None:
    """The autouse suite fixture hides a surface that loses its wrapper from every other test, and the
    entry-point table above only knows the surfaces someone listed. This walks the registry lint's own
    scan of ``src/nexus`` (every ``claude_dispatch`` / ``run_text_sync`` / ``claude -p`` site) so a
    site nobody listed fails here."""
    from tests import test_llm_surface_registry_lint as lint  # noqa: PLC0415

    sites, _files = lint.scan_repo()
    dispatching = [s for s in sites if s.kind != "httpx"]
    assert len(dispatching) >= 23, f"the scan saw only {len(dispatching)} dispatch/argv sites (23 at landing)"
    bad = uncovered_sites(sites, WRAPPED_ENTRY_POINTS, REACHED_UNDER)
    assert not bad, (
        "dispatch sites outside any llm_bound scope and not declared in REACHED_UNDER "
        "(wrap the surface with @bound_to / with llm_bound, add it to WRAPPED_ENTRY_POINTS, "
        f"or declare the entry point it runs under): {bad}"
    )
    found = {s.where for s in dispatching}
    stale = sorted(set(REACHED_UNDER) - found) + sorted(ROOT_HOMES - found)
    assert not stale, f"entries that name no dispatch site any more: {stale}"


def test_an_unwrapped_dispatch_site_is_reported_and_a_declared_one_is_not() -> None:
    from tests import test_llm_surface_registry_lint as lint  # noqa: PLC0415

    src = (
        "async def fresh_surface(x):\n    return await claude_dispatch(x, {})\n\n"
        "async def covered(x):\n    return await claude_dispatch(x, {})\n"
    )
    sites = lint.scan_source(src, "nexus.fake")
    assert len(sites) == 2
    wrapped = {("nexus.fake", "covered"): "nx_tidy"}
    assert uncovered_sites(sites, wrapped, {}) == ["nexus.fake:fresh_surface (line 2)"]
    assert uncovered_sites(sites, wrapped, {"nexus.fake:fresh_surface": (("nexus.fake", "covered"),)}) == []
    # a declared entry point that is not itself wrapped does not cover the site
    assert uncovered_sites(sites, wrapped, {"nexus.fake:fresh_surface": (("nexus.fake", "gone"),)}) != []


OPERATOR_ARGS: dict[str, dict[str, Any]] = {
    "extract": {"inputs": "a", "fields": "x"},
    "rank": {"items": "[\"a\"]", "criterion": "c"},
    "compare": {"items": "[\"a\", \"b\"]", "focus": "f"},
    "summarize": {"content": "text"},
    "generate": {"template": "t", "context": "c"},
    "filter": {"items": "[\"a\"]", "criterion": "c"},
    "check": {"items": "[\"a\"]", "check_instruction": "c"},
    "verify": {"claim": "c", "evidence": "e"},
    "groupby": {"items": "[\"a\"]", "key": "k"},
    "aggregate": {"groups": "[]", "reducer": "r"},
}


@pytest.mark.parametrize("op", sorted(OPERATOR_ARGS))
def test_each_operator_dispatches_inside_the_scope_without_the_environment_hint(
    op: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.mcp import core
    from nexus.operators import dispatch

    seen: list[tuple[str, str | None]] = []

    async def fake_dispatch(*args: Any, **kwargs: Any) -> dict:
        seen.append((current_posture(), current_surface()))
        assert_llm_bound("claude_dispatch")
        return {}

    monkeypatch.setattr(dispatch, "claude_dispatch", fake_dispatch)
    try:
        asyncio.run(getattr(core, f"operator_{op}")(**OPERATOR_ARGS[op]))
    except Exception:  # noqa: BLE001 - the canned reply may not satisfy the operator's post-processing; the dispatch is what is pinned
        pass
    assert seen == [("llm_bound", "nx_answer_operators")], seen
