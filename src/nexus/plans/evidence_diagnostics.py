# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-232 Phase 1 (nexus-jypmj.1.8): deterministic source and scope checks, per-stream and
reducer-input diagnostics.

``nx_answer`` returns one ``evidence_diagnostics`` object in its structured envelope and writes one
log event (:func:`log_evidence_diagnostics`) with the same counts. Neither is persisted before Phase 3
adds the run summary. The object answers four questions without a model call:

* **streams**: how many references each retrieval stream produced and from which collections
  (``vector``: the search family; ``graph``: ``traverse``; ``primary``: the named source's chunks;
  ``t2``: its lifecycle records);
* **reducer_input**: what the first evidence-consuming operator was actually handed, by stream and
  collection (:class:`ReducerInputRecorder`, fed by the runner's operator hydration);
* **source_check**: did the named primary document reach the reducer input (``covered``, ``partial``
  or ``missing``), and is its lifecycle facet resolved;
* **scope_check**: do the observed collections stay inside the caller's explicit scope
  (``satisfied``, ``widened``, ``conflict`` or ``violated``).

Provenance of a reducer item. A reserved item (primary chunk, T2 record) carries its stream and
collection on :class:`~nexus.plans.reserved_stream.ReservedItem`. Any other item is attributed by
content: a retrieval step's ``ids`` are the chunks' content hashes (``sha256`` of the chunk text), so
the hash of an item, with its ``[source: ...]`` line removed, finds the stream and collection that
produced it (a chunk both retrieval streams returned is attributed to the first stream in plan
order). An item that hashes to nothing the run retrieved (a manifest-joined document, a
rewritten text) is ``unattributed`` and counted as such, never assumed to be inside the scope.

No collection name is parsed here (RDR-204): counts are by collection name as a key, and in or out of
scope is :meth:`~nexus.plans.evidence.ScopeConstraint.admits`.

Pure: stdlib, ``structlog`` and the RDR-232 value types. The runner imports the recorder hooks.
"""
from __future__ import annotations

import contextlib
import hashlib
from collections.abc import Iterator, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import structlog

from nexus.plans.evidence import ScopeConstraint
from nexus.plans.reserved_stream import ReservedStream, strip_source_note
from nexus.plans.traversal_policy import capped_traversal_count

_log = structlog.get_logger(__name__)

__all__ = [
    "ReducerInputRecorder",
    "active_recorder",
    "build_evidence_diagnostics",
    "log_evidence_diagnostics",
    "note_reducer_input",
    "plan_tools",
    "reducer_input_scope",
    "set_recorder_step",
]

#: The retrieval tools whose references are the vector stream and the graph stream. A step whose
#: tool is neither is not a stream (``store_get_many`` hydrates, an operator reduces).
VECTOR_TOOLS: frozenset[str] = frozenset({
    "search", "query", "search_metadata_scoped", "search_topic_scoped", "search_aspect_scoped",
})
GRAPH_TOOLS: frozenset[str] = frozenset({"traverse", "search_graph_hop"})

#: ``by_collection`` key for references whose collection the step output does not say per chunk.
UNATTRIBUTED = ""

_SOURCE_PRIMARY_FIRST = ("missing", "partial", "covered")


# ── the reducer-input recorder ────────────────────────────────────────────────────────────────────────────


@dataclass
class ReducerInputRecorder:
    """The list of texts the first evidence-consuming operator was handed, as the runner built it.

    The runner marks the plan step it is about to hydrate (:func:`set_recorder_step`) and every
    hydration site that consumes retrieved evidence reports its final list (:func:`note_reducer_input`).
    The first step to report wins and a re-hydration of that same step replaces it (the
    oversized-bundle fallback, the continuation envelope); a later operator's input is the earlier
    operator's output, not retrieved evidence, so it is ignored. Recording reads; it changes nothing
    the operator receives.
    """

    items: tuple[str, ...] | None = None
    step: int | None = None
    current_step: int | None = None
    _consumed: bool = False

    def set_step(self, index: int | None) -> None:
        self.current_step = index

    def note(self, items: Sequence[Any]) -> None:
        if self._consumed and self.step != self.current_step:
            return
        self.items = tuple(i for i in items if isinstance(i, str))
        self.step = self.current_step
        self._consumed = True

    def reset_run(self) -> None:
        """A whole-plan re-run (the continuation fallback) starts from a clean state."""
        self.items, self.step, self.current_step, self._consumed = None, None, None, False


_ACTIVE: ContextVar[ReducerInputRecorder | None] = ContextVar("nexus_reducer_input_recorder", default=None)


def active_recorder() -> ReducerInputRecorder | None:
    return _ACTIVE.get()


@contextlib.contextmanager
def reducer_input_scope(recorder: ReducerInputRecorder | None) -> Iterator[None]:
    """Make *recorder* the one the runner's hydration reports to for the duration of the block."""
    token = _ACTIVE.set(recorder)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def set_recorder_step(index: int | None) -> None:
    """No-op without a recorder."""
    rec = _ACTIVE.get()
    if rec is not None:
        rec.set_step(index)


def note_reducer_input(items: Sequence[Any]) -> None:
    """No-op without a recorder."""
    rec = _ACTIVE.get()
    if rec is not None:
        rec.note(items)


def plan_tools(plan_json: Any) -> list[str]:
    """The tool name of each step of *plan_json* (a JSON string or a dict), in plan order; ``[]`` when
    it cannot be read, which leaves every step outside the streams unless it is a traverse output."""
    import json  # noqa: PLC0415 — stdlib, branch-local

    from nexus.plans.bundle import _extract_tool_name  # noqa: PLC0415 — deferred: bundle imports the runner's neighbours

    try:
        plan = json.loads(plan_json) if isinstance(plan_json, (str, bytes)) else plan_json
        return [_extract_tool_name(step) for step in (plan or {}).get("steps") or []]
    except (TypeError, ValueError, AttributeError):
        return []


# ── retrieval streams ──────────────────────────────────────────────────────────────────────────────────────


def _stream_of(tool: str, out: Mapping[str, Any]) -> str | None:
    if tool in VECTOR_TOOLS:
        return "vector"
    if tool in GRAPH_TOOLS:
        return "graph"
    if not tool and "traversal" in out:  # the plan's tools are unknown: a traverse output carries its report
        return "graph"
    return None


def _per_chunk_collections(out: Mapping[str, Any], n: int) -> list[str]:
    """The collection of each of *n* ids: the aligned per-chunk list, else the one collection every
    id must be in, else unattributed. Never a guess between several."""
    per = out.get("chunk_collections")
    if isinstance(per, list) and len(per) == n:
        return [str(c or UNATTRIBUTED) for c in per]
    cols = out.get("collections")
    if isinstance(cols, list) and len(cols) == 1:
        return [str(cols[0] or UNATTRIBUTED)] * n
    return [UNATTRIBUTED] * n


@dataclass
class _Stream:
    refs: dict[str, str]  # id -> collection, first occurrence wins
    steps: list[dict[str, Any]]

    def by_collection(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in self.refs.values():
            out[c] = out.get(c, 0) + 1
        return out


def _scope_counts(scope: ScopeConstraint, by_collection: Mapping[str, int]) -> tuple[int | None, int | None]:
    """``(in_scope, out_of_scope)`` over *by_collection*, or ``(None, None)`` when the scope cannot
    be judged from a collection (no scope, a catalog subtree) or a count is unattributed."""
    if scope.kind in ("none", "subtree") or scope.value == "all":
        return None, None
    if UNATTRIBUTED in by_collection:
        return None, None
    inside = sum(n for c, n in by_collection.items() if scope.admits(c))
    return inside, sum(by_collection.values()) - inside


def _traversal_summary(index: int, report: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("policy_applied", "policy_failed", "possibly_truncated", "contained", "nodes_walked", "nodes_retained",
            "dropped_out_of_scope", "dropped_role", "dropped_cap")
    return {"step_index": index, **{k: report.get(k) for k in keys}, "widened": report.get("widened") is not None}


# ── provenance ─────────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Attribution:
    stream: str
    collection: str
    doc_id: str = ""
    source: str = ""
    item_kind: str = ""


def _attribute(
    items: Sequence[str], reserved: ReservedStream | None, index: Mapping[str, tuple[str, str]],
) -> list[_Attribution]:
    by_text = {i.text: i for i in reserved.items} if reserved is not None else {}
    out: list[_Attribution] = []
    for text in items:
        hit = by_text.get(text)
        if hit is not None:
            out.append(_Attribution(hit.stream, hit.collection, hit.doc_id, hit.source))
            continue
        found = index.get(hashlib.sha256(strip_source_note(text).encode("utf-8")).hexdigest())
        out.append(_Attribution(found[0], found[1]) if found else _Attribution("unattributed", UNATTRIBUTED))
    return out


# ── the builder ────────────────────────────────────────────────────────────────────────────────────────────


def build_evidence_diagnostics(
    *,
    steps: Sequence[Any],
    tools: Sequence[str],
    scope: ScopeConstraint,
    scope_diagnostics: Sequence[Mapping[str, Any]] = (),
    resolved_args: Sequence[Mapping[str, Any]] = (),
    recorder: ReducerInputRecorder | None = None,
    reserved: ReservedStream | None = None,
    primary: Any = None,
    lifecycle: Any = None,
    named_sources: Any = None,
) -> dict[str, Any]:
    """The ``evidence_diagnostics`` object for one run (see the module docstring).

    *steps* are the run's step outputs in plan order and *tools* the plan's tool name per step (an
    unknown or missing name leaves a step outside the streams unless it is a traverse output).
    *primary*, *lifecycle* and *named_sources* are the :class:`PrimaryEvidence`,
    :class:`LifecycleStream` and :class:`NamedSourceResolution` the call read, or ``None``.
    """
    corpus_of_step = {int(a["step_index"]): a.get("corpus", "") for a in resolved_args if "step_index" in a}
    streams: dict[str, _Stream] = {"vector": _Stream({}, []), "graph": _Stream({}, [])}
    index: dict[str, tuple[str, str]] = {}
    skipped: list[dict[str, Any]] = []
    traversals: list[dict[str, Any]] = []
    traverse_outputs: list[Mapping[str, Any]] = []
    for i, out in enumerate(steps):
        if not isinstance(out, Mapping):
            continue
        tool = tools[i] if i < len(tools) else ""
        marker = out.get("scope_conflict")
        if isinstance(marker, Mapping):
            skipped.append({
                "step_index": i, "tool": str(marker.get("tool") or tool), "reason": "scope_conflict",
                "plan_corpus": marker.get("plan_corpus"), "caller_scope": marker.get("caller_scope"),
            })
            continue
        stream = _stream_of(tool, out)
        ids = out.get("ids")
        if stream is None or not isinstance(ids, list):
            continue
        cols = _per_chunk_collections(out, len(ids))
        s = streams[stream]
        for cid, col in zip(ids, cols, strict=True):
            s.refs.setdefault(str(cid), col)
            index.setdefault(str(cid).lower(), (stream, col))
        hashes = out.get("chunk_text_hash")
        if isinstance(hashes, list) and len(hashes) == len(ids):
            for h, col in zip(hashes, cols, strict=True):
                index.setdefault(str(h).lower(), (stream, col))
        s.steps.append({"step_index": i, "tool": tool, "corpus": corpus_of_step.get(i)})
        if stream == "graph":
            traverse_outputs.append(out)
            if isinstance(out.get("traversal"), Mapping):
                traversals.append(_traversal_summary(i, out["traversal"]))

    stream_wire: dict[str, Any] = {}
    for name, s in streams.items():
        by_col = s.by_collection()
        inside, outside = _scope_counts(scope, by_col)
        stream_wire[name] = {
            "references": len(s.refs), "by_collection": by_col, "in_scope": inside, "out_of_scope": outside,
            "steps": s.steps,
        }
    p_items = list(getattr(primary, "items", ()) or ())
    p_cols: dict[str, int] = {}
    p_sources: dict[str, int] = {}
    for it in p_items:
        p_cols[it.collection] = p_cols.get(it.collection, 0) + 1
        p_sources[it.source] = p_sources.get(it.source, 0) + 1
    p_in, p_out = _scope_counts(scope, p_cols)
    stream_wire["primary"] = {
        "references": len(p_items), "by_collection": p_cols, "in_scope": p_in, "out_of_scope": p_out,
        "by_source": p_sources,
    }
    t2_projects: dict[str, int] = {}
    for rec in getattr(lifecycle, "records", ()) or ():
        t2_projects[rec.project] = t2_projects.get(rec.project, 0) + 1
    stream_wire["t2"] = {"references": sum(t2_projects.values()), "by_project": t2_projects}

    attributions: list[_Attribution] | None = None
    reducer_wire: dict[str, Any] | None = None
    if recorder is not None and recorder.items is not None:
        attributions = _attribute(recorder.items, reserved, index)
        reducer_wire = _reducer_wire(recorder, attributions, scope)

    return {
        "streams": stream_wire,
        "reducer_input": reducer_wire,
        "source_check": _source_check(named_sources, primary, lifecycle, attributions),
        "scope_check": _scope_check(
            scope, streams, stream_wire, attributions, scope_diagnostics, traverse_outputs, p_cols,
        ),
        "skipped_steps": skipped,
        "traversals": traversals,
        "capped_traversals": capped_traversal_count(steps),
        "reserved": _reserved_wire(reserved),
    }


def _reducer_wire(rec: ReducerInputRecorder, att: Sequence[_Attribution], scope: ScopeConstraint) -> dict[str, Any]:
    by_stream: dict[str, int] = {}
    by_stream_collection: dict[str, dict[str, int]] = {}
    by_collection: dict[str, int] = {}
    for a in att:
        by_stream[a.stream] = by_stream.get(a.stream, 0) + 1
        if a.stream in ("t2", "unattributed"):
            continue
        by_stream_collection.setdefault(a.stream, {})
        by_stream_collection[a.stream][a.collection] = by_stream_collection[a.stream].get(a.collection, 0) + 1
        by_collection[a.collection] = by_collection.get(a.collection, 0) + 1
    unattributed = by_stream.get("unattributed", 0)
    attributed = {c: n for c, n in by_collection.items()}
    inside, outside = _scope_counts(scope, attributed)
    return {
        "step": rec.step,
        "items": len(att),
        "chars": sum(len(t) for t in (rec.items or ())),
        "by_stream": by_stream,
        "by_stream_collection": by_stream_collection,
        "by_collection": by_collection,
        "in_scope": inside,
        "out_of_scope": outside,
        "unattributed": unattributed,
    }


def _reserved_wire(reserved: ReservedStream | None) -> dict[str, Any] | None:
    if reserved is None:
        return None
    r = reserved.report()
    return {
        "items": r["reserved_items"], "merged": r["merged"], "over_share_dropped": r["over_share_dropped"],
        "deduped_secondary": r["deduped_secondary"], "secondary_cut": r["secondary_cut"], "capped": r["capped"],
        "dropped_before_terminal": r["dropped_before_terminal"],
    }


# ── the source check ───────────────────────────────────────────────────────────────────────────────────────

#: Lifecycle statuses that delivered nothing for an RDR source: the facet is unresolved, not absent.
_LIFECYCLE_UNRESOLVED = frozenset({"no_records", "project_undetermined", "read_capped", "read_failed", "over_cap"})
_LIFECYCLE_NOT_APPLICABLE = frozenset({"not_rdr", "duplicate", "not_resolved"})


def _lifecycle_state(
    ref: str, lifecycle: Any, att: Sequence[_Attribution] | None,
) -> dict[str, Any] | None:
    if lifecycle is None:
        return None
    entry = next((s for s in lifecycle.sources if s.source == ref), None)
    if entry is None:
        return None
    delivered = sum(1 for r in lifecycle.records if r.source == ref)
    reached = sum(1 for a in att if a.stream == "t2" and a.source == ref) if att is not None else None
    status = entry.status
    if status in _LIFECYCLE_UNRESOLVED:
        state = "unresolved"
    elif status in _LIFECYCLE_NOT_APPLICABLE:
        state = "not_applicable"
    elif entry.detail or delivered < 2 or (reached is not None and reached < delivered):
        state = "partial"  # at least one of the two records is absent, or lost before the reducer
    else:
        state = "resolved"
    return {"state": state, "status": status, "detail": entry.detail, "delivered": delivered,
            "reached_reducer": reached if reached is not None else 0}


def _source_check(
    named: Any, primary: Any, lifecycle: Any, att: Sequence[_Attribution] | None,
) -> dict[str, Any]:
    sources = tuple(getattr(named, "sources", ()) or ())
    recorded = att is not None
    if not sources:
        return {"state": "not_applicable", "sources": [], "reducer_input_recorded": recorded, "unresolved": []}
    rows: list[dict[str, Any]] = []
    unresolved: list[dict[str, str]] = []
    for s in sources:
        ref = s.ref.value
        row: dict[str, Any] = {
            "source": ref, "tumbler": s.tumbler, "selected": 0, "reached_reducer": 0, "manifest_rows": 0,
            "lifecycle": None,
        }
        coverage = next((d for d in getattr(primary, "documents", ()) if d.doc_id == s.tumbler), None) if s.tumbler else None
        if s.status != "resolved":
            row.update(state="missing", detail=f"the source is {s.status}: {s.detail}".rstrip(": "))
        elif coverage is None or coverage.status != "ok":
            why = "no primary evidence was read for it" if coverage is None else f"its manifest is {coverage.status}"
            row.update(state="missing", detail=why)
        else:
            selected = sum(1 for i in getattr(primary, "items", ()) if i.doc_id == s.tumbler)
            row.update(selected=selected, manifest_rows=coverage.manifest_rows)
            reached = (
                sum(1 for a in att if a.stream == "primary" and a.doc_id == s.tumbler) if att is not None else 0
            )
            row["reached_reducer"] = reached
            if att is None or reached == 0:
                row.update(state="missing", detail=(
                    "no reduction was handed the primary evidence" if att is None
                    else "none of its selected chunks reached the reducer input"))
            elif reached < selected or coverage.partial:
                row.update(state="partial", detail=(
                    f"{reached} of {coverage.manifest_rows} chunks reached the reducer"
                    + (f" ({selected} selected)" if selected != reached else "")))
            else:
                row.update(state="covered", detail="")
        if s.status == "resolved":
            row["lifecycle"] = _lifecycle_state(ref, lifecycle, att)
            lc = row["lifecycle"]
            if lc is not None and lc["state"] in ("unresolved", "partial"):
                unresolved.append({"source": ref, "facet": "lifecycle", "status": lc["status"]})
        rows.append(row)
    worst = min((r["state"] for r in rows), key=_SOURCE_PRIMARY_FIRST.index)
    return {"state": worst, "sources": rows, "reducer_input_recorded": recorded, "unresolved": unresolved}


# ── the scope check ────────────────────────────────────────────────────────────────────────────────────────


def _scope_check(
    scope: ScopeConstraint,
    streams: Mapping[str, _Stream],
    stream_wire: Mapping[str, Any],
    att: Sequence[_Attribution] | None,
    scope_diagnostics: Sequence[Mapping[str, Any]],
    traverse_outputs: Sequence[Mapping[str, Any]],
    primary_collections: Mapping[str, int],
) -> dict[str, Any]:
    reasons: list[dict[str, Any]] = []
    for out in traverse_outputs:
        widened = (out.get("traversal") or {}).get("widened") if isinstance(out.get("traversal"), Mapping) else None
        if widened:
            reasons.append({"stream": "graph", "reason": "traversal_widened", "detail": widened.get("reason", "")})
    conflict_steps = [int(d["step_index"]) for d in scope_diagnostics if d.get("outcome") == "conflict"]
    base: dict[str, Any] = {
        "scope": scope.value, "scope_kind": scope.kind, "conflict_steps": conflict_steps,
        "recorded_reasons": reasons, "violations": [], "streams_out_of_scope": {}, "out_of_scope": 0,
        "reducer_out_of_scope": 0, "reducer_unattributed": 0, "detail": "",
    }
    if att is not None:
        base["reducer_unattributed"] = sum(1 for a in att if a.stream == "unattributed")
    if scope.kind == "none" or scope.value == "all":
        return {**base, "state": "not_applicable"}
    if scope.kind == "subtree":
        return {**base, "state": "not_evaluated", "detail": (
            "a catalog subtree is judged on documents, which a multi-step plan does not intersect with its "
            "corpora; no collection was checked against it")}

    out_by_stream: dict[str, int] = {}
    violations: dict[tuple[str, str], int] = {}
    for name in ("vector", "graph"):
        for col in streams[name].refs.values():
            if col != UNATTRIBUTED and not scope.admits(col):
                out_by_stream[name] = out_by_stream.get(name, 0) + 1
                violations.setdefault((name, col), 0)
    reducer_out = 0
    for a in att or ():
        if a.stream in ("t2", "unattributed") or a.collection == UNATTRIBUTED or scope.admits(a.collection):
            continue
        reducer_out += 1
        if a.stream in ("vector", "graph"):
            violations[(a.stream, a.collection)] = violations.get((a.stream, a.collection), 0) + 1
    for col, n in primary_collections.items():
        if col != UNATTRIBUTED and not scope.admits(col):
            reasons.append({"stream": "primary", "reason": "named_source", "collection": col, "count": n})
    state = "satisfied"
    if any(reason.get("reason") == "named_source" for reason in reasons):
        state = "widened"
    if conflict_steps:
        state = "conflict"
    if violations:
        state = "violated"
    return {
        **base, "state": state, "streams_out_of_scope": out_by_stream,
        "out_of_scope": sum(out_by_stream.values()), "reducer_out_of_scope": reducer_out,
        "violations": [{"stream": s, "collection": c, "in_reducer": n} for (s, c), n in sorted(violations.items())],
        "detail": "",
    }


# ── the log event ──────────────────────────────────────────────────────────────────────────────────────────


def log_evidence_diagnostics(diag: Mapping[str, Any], *, plan_id: int) -> None:
    """One structured event with the counts and states of *diag* and nothing else. It carries no
    question, no source label, no document title and no evidence or record text, so ``trace=False``
    changes nothing about it."""
    streams = diag["streams"]
    reducer = diag.get("reducer_input") or {}
    source = diag["source_check"]
    scope = diag["scope_check"]
    reserved = diag.get("reserved") or {}
    _log.info(
        "nx_answer_evidence_diagnostics",
        plan_id=plan_id,
        vector_refs=streams["vector"]["references"],
        graph_refs=streams["graph"]["references"],
        primary_refs=streams["primary"]["references"],
        t2_refs=streams["t2"]["references"],
        reducer_items=reducer.get("items"),
        reducer_by_stream=reducer.get("by_stream"),
        reducer_out_of_scope=reducer.get("out_of_scope"),
        reducer_unattributed=reducer.get("unattributed"),
        source_state=source["state"],
        sources=len(source["sources"]),
        lifecycle_unresolved=len(source["unresolved"]),
        scope_state=scope["state"],
        scope_violations=len(scope["violations"]),
        scope_conflict_steps=len(scope["conflict_steps"]),
        skipped_steps=len(diag["skipped_steps"]),
        capped_traversals=diag["capped_traversals"],
        reserved_over_share_dropped=reserved.get("over_share_dropped"),
        reserved_dropped_before_terminal=(reserved.get("dropped_before_terminal") or {}).get("missing"),
        reserved_secondary_cut=reserved.get("secondary_cut"),
    )
