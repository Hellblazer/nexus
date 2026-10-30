# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-232 section 2a (nexus-jypmj; Sam, 2026-10-30): a one-source request confines every secondary retrieval step
to the DOCUMENT it names.

The collection and owner narrowing of nexus-4xmpv left sibling documents of the same project in reach: re-measure 3
found 1 of 170 vector and graph secondary anchors in the named document. This module is the document-level
filter. A narrowed run carries a :class:`DocumentConfinement`: the document's tumbler, the full set of chunk
hashes its manifest lists (the primary read's ``_read_manifests`` rows, all of them before the selection samples
them, at most :data:`CHASH_ALLOW_MAX`), and the mode the engine allows.

**Two modes.** ``engine_filter``: the engine advertises ``vector_search_chash_allow_supported`` (``GET /version``),
so a ``search`` or ``query`` step carries ``chash_allow`` (the 64-hex chunk hashes) and the engine restricts the
candidates to those chunks and ranks exactly (``/v1/vectors`` search, hybrid-search, search-per-collection). An
engine without the flag, or an edge that trims it from ``/version``, would ignore the key and answer collection-wide,
so nothing is sent: ``steps_not_run``, the client-only design. No secondary retrieval step and no traversal is
dispatched; each leaves the empty structured output with a ``document_confined`` marker. The coverage repair keeps
its chash-membership filter in both modes.

**Which steps carry the filter** (:func:`confine_step`):

* ``search`` and ``query`` (plain corpus route): ``chash_allow`` rides the step. The runner puts it in the step's
  args; the dispatcher turns it into a context value (:func:`chash_allow_scope`) that
  ``search_engine.search_cross_corpus`` reads once, before it fans out to its worker threads, and sends on every
  engine call it makes (search, hybrid-search, search-per-collection).
* ``query`` with a catalog parameter (``author``, ``content_type``, ``follow_links``, ``subtree``) takes the
  combined-query routes, which have no chash filter: not run.
* ``search_metadata_scoped``, ``search_topic_scoped``, ``search_graph_hop``, ``search_aspect_scoped``: own routes
  (``/search-metadata-scoped`` and the others) that the engine does not extend with the filter: not run.
* ``traverse``: confined by the existing catalog ``scope_filter`` at document depth (the policy scope is the
  document's tumbler; ``ScopeConstraint.admits`` matches by equality, the engine's ``GraphScopeFilter.ofSubtree``
  the same). It needs no chash filter. In ``steps_not_run`` mode it is not run either.
* ``store_get_many``: its ids are cut to the document's chash set (a tumbler id must be the document's own), in
  every form the tool takes (a comma string, a flat list, a list of lists), with an aligned ``collections`` list
  cut together with the ids; an ``ids`` value it cannot parse becomes an empty list. A SAMPLED set cuts too: an id
  outside the sample is dropped, because the alternative is to let any sibling's chunk through. Every id a confined
  run holds came from a step confined to that same set (or from the primary read, which is inside the document),
  so the cut loses nothing a sample could have kept.

Pure apart from the context variables; stdlib only.
"""
from __future__ import annotations

import contextlib
import re
from collections.abc import Iterator, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "CHASH_ALLOW_MAX",
    "CONFINEMENT_MARKER",
    "CONFINED_TOOLS",
    "Confinement",
    "DocumentConfinement",
    "active_chash_allow",
    "build_confinement",
    "active_confinement",
    "chash_allow_scope",
    "confine_step",
    "confinement_scope",
    "empty_confined_output",
    "valid_chashes",
]

#: The most chunk hashes one request may send (the engine answers 400 above it).
CHASH_ALLOW_MAX: int = 3000

#: Key of the marker a skipped step's output carries.
CONFINEMENT_MARKER = "document_confined"

_CHASH = re.compile(r"^[0-9a-f]{64}$")

ConfinementMode = Literal["engine_filter", "steps_not_run"]

#: The retrieval tools whose route can carry ``chash_allow``.
_CHASH_TOOLS = frozenset({"search", "query"})
#: ``query`` parameters that route it through the catalog's combined-query functions.
_QUERY_CATALOG_PARAMS = ("author", "content_type", "follow_links", "subtree")
#: Retrieval tools on routes that have no chash filter.
_UNCONFINABLE_TOOLS = frozenset({
    "search_metadata_scoped", "search_topic_scoped", "search_graph_hop", "search_aspect_scoped",
})

#: Every tool the confinement acts on. Census: ``tests/test_rdr232_document_confinement.py`` pins that this equals
#: ``runner._RETRIEVAL_TOOLS | {"traverse"}``, so a retrieval tool added to the runner cannot run unconfined unnoticed.
CONFINED_TOOLS: frozenset[str] = _CHASH_TOOLS | _UNCONFINABLE_TOOLS | {"traverse", "store_get_many"}


def valid_chashes(candidates: Sequence[Any]) -> tuple[tuple[str, ...], int]:
    """``(distinct 64-hex hashes in first-seen order, how many inputs were dropped)``. A malformed hash would
    fail the whole request (the engine answers 400), so it is dropped here and counted."""
    seen: dict[str, None] = {}
    bad = 0
    for c in candidates:
        h = c.lower() if isinstance(c, str) else ""
        if _CHASH.match(h):
            seen.setdefault(h, None)
        else:
            bad += 1
    return tuple(seen), bad


@dataclass(frozen=True)
class DocumentConfinement:
    """One run's document confinement; see the module docstring."""

    tumbler: str
    collection: str
    mode: ConfinementMode
    #: The document's manifest chunk hashes (distinct, at most :data:`CHASH_ALLOW_MAX`).
    chashes: tuple[str, ...] = ()
    #: How many manifest rows the document has (M).
    manifest_rows: int = 0
    #: Rows the read left out of ``chashes`` because of a cap (the read cap, the engine's own sampling or
    #: :data:`CHASH_ALLOW_MAX`). Duplicate chunk text and malformed hashes are not sampling and are not counted.
    rows_dropped: int = 0
    #: The engine advertised ``vector_search_chash_allow_supported``. ``mode`` is ``steps_not_run`` either because it
    #: did not, or because it did and the manifest gave no hash to send; this tells the two apart.
    engine_filter_available: bool = False
    _set: frozenset[str] = field(default=frozenset(), repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_set", frozenset(self.chashes))

    @property
    def sampled(self) -> bool:
        """The chash set is a sample of the manifest: a cap left rows out of it. Not a manifest whose rows repeat a
        chunk's text (fewer distinct hashes than rows) or carry a malformed hash."""
        return self.rows_dropped > 0

    @property
    def sends_filter(self) -> bool:
        return self.mode == "engine_filter" and bool(self.chashes)

    def contains(self, chash: str) -> bool:
        return chash.lower() in self._set

    def allow_list(self) -> list[str]:
        return sorted(self.chashes)

    def to_wire(self) -> dict[str, Any]:
        return {
            "document": self.tumbler, "collection": self.collection, "mode": self.mode,
            "chashes": len(self.chashes), "manifest_rows": self.manifest_rows, "sampled": self.sampled,
        }


@dataclass(frozen=True)
class Confinement:
    """What :func:`confine_step` decided for one step: the args to dispatch (``skipped`` empty), or the reason the
    step is not run."""

    args: dict[str, Any]
    skipped: str = ""

    @property
    def run(self) -> bool:
        return not self.skipped


def build_confinement(
    *, tumbler: str, collection: str, evidence: Any, engine_filter: bool,
) -> DocumentConfinement:
    """The confinement for the document *tumbler*: its manifest hashes from the primary read *evidence*
    (:meth:`PrimaryEvidence.chashes_of`), and ``engine_filter`` mode only when the engine advertises the filter AND
    there is a hash to send. A document whose manifest was not read has no set to filter by, so its run is
    ``steps_not_run``: nothing is dispatched that could read a sibling document."""
    held = evidence.chashes_of(tumbler) if evidence is not None and hasattr(evidence, "chashes_of") else None
    chashes = tuple(held.chashes) if held is not None else ()
    rows = int(held.manifest_rows) if held is not None else 0
    dropped = int(getattr(held, "rows_dropped", 0) or 0) if held is not None else 0
    mode: ConfinementMode = "engine_filter" if engine_filter and chashes else "steps_not_run"
    return DocumentConfinement(
        tumbler, collection, mode, chashes, max(rows, len(chashes)), rows_dropped=dropped,
        engine_filter_available=bool(engine_filter),
    )


def confine_step(tool: str, args: Mapping[str, Any], c: DocumentConfinement | None) -> Confinement:
    """Apply *c* to one retrieval step's resolved *args*. ``c`` of ``None`` returns the args unchanged. Any tool
    this module does not name is returned unchanged (operators and the rest are not retrieval)."""
    out = dict(args)
    if c is None or tool not in CONFINED_TOOLS:
        return Confinement(out)
    if tool == "store_get_many":
        return Confinement(_cut_ids(out, c))
    if tool == "traverse":
        return Confinement(out) if c.mode == "engine_filter" else Confinement(out, "steps_not_run")
    if tool in _UNCONFINABLE_TOOLS:
        return Confinement(out, "route_has_no_chash_filter")
    if c.mode != "engine_filter" or not c.chashes:
        return Confinement(out, "steps_not_run")
    if tool == "query" and any(out.get(k) for k in _QUERY_CATALOG_PARAMS):
        return Confinement(out, "catalog_route_has_no_chash_filter")
    out["chash_allow"] = c.allow_list()
    return Confinement(out)


_TUMBLER = re.compile(r"\d+(\.\d+)+")


def _cut_ids(args: dict[str, Any], c: DocumentConfinement) -> dict[str, Any]:
    """*args* of a ``store_get_many`` step with its ids cut to the document, in every form the tool takes.

    A tumbler id must be the document's own, any other id must be one of its chunk hashes. ``ids`` may be a comma
    string, a flat list or a list of lists (parallel streams; each stream is cut, the stream count and any
    ``collections`` aligned to it stay). When ``collections`` is aligned 1:1 with the flat ids (the tool's per-id
    routing) it is cut together with them, so no id is read from another id's collection. Anything else in ``ids``
    (missing, a dict, a number) fails closed to an empty list."""

    def keep(i: Any) -> bool:
        s = str(i).strip()
        return s == c.tumbler if _TUMBLER.fullmatch(s) else c.contains(s)

    ids = args.get("ids")
    if isinstance(ids, list) and ids and all(isinstance(stream, list) for stream in ids):
        return {**args, "ids": [[i for i in stream if i and keep(i)] for stream in ids]}
    if isinstance(ids, list):
        id_list = [str(i) for i in ids if i]
    elif isinstance(ids, str):
        id_list = [p.strip() for p in ids.split(",") if p.strip()]
    else:
        return {**args, "ids": []}
    limit = args.get("limit_per_source")
    if isinstance(limit, int) and not isinstance(limit, bool) and limit >= 0:
        id_list = id_list[:limit]  # the tool truncates before it routes; align on the same list it will
    mask = [keep(i) for i in id_list]
    out = {**args, "ids": [i for i, k in zip(id_list, mask, strict=True) if k]}
    collections = args.get("collections")
    if isinstance(collections, list):
        coll_list = [str(x) for x in collections if x]
    elif isinstance(collections, str):
        coll_list = [x.strip() for x in collections.split(",") if x.strip()]
    else:
        coll_list = []
    if coll_list and len(coll_list) == len(id_list):
        kept = [x for x, k in zip(coll_list, mask, strict=True) if k]
        out["collections"] = kept if isinstance(collections, list) else ",".join(kept)
    elif len(coll_list) > 1 and len(out["ids"]) == len(coll_list):
        # A broadcast over several collections that the cut would turn into per-id routing (the tool reads equal
        # lengths as aligned): the document lives in one collection, so name only that one.
        out["collections"] = c.collection
    return out


def empty_confined_output(tool: str, step_index: int, c: DocumentConfinement, reason: str) -> dict[str, Any]:
    """The empty structured retrieval shape (the keys ``query`` and ``search`` return in structured mode, plus
    ``contents``) so a later ``$stepN.<key>`` reference resolves to the empty value, with the marker."""
    return {
        "ids": [], "tumblers": [], "distances": [], "hybrid_scores": [], "collections": [],
        "chunk_collections": [], "also_in": [], "also_in_ids": [], "chunk_text_hash": [],
        "contents": [],
        CONFINEMENT_MARKER: {
            "step_index": step_index, "tool": tool, "document": c.tumbler, "mode": c.mode, "reason": reason,
        },
    }


# ── run-scoped state ─────────────────────────────────────────────────────────────────────────────────────────

_ACTIVE: ContextVar[DocumentConfinement | None] = ContextVar("nexus_document_confinement", default=None)
_ALLOW: ContextVar[tuple[str, ...] | None] = ContextVar("nexus_chash_allow", default=None)


def active_confinement() -> DocumentConfinement | None:
    return _ACTIVE.get()


@contextlib.contextmanager
def confinement_scope(c: DocumentConfinement | None) -> Iterator[None]:
    """Make *c* the confinement the runner applies to every retrieval step for the duration of the block."""
    token = _ACTIVE.set(c)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def active_chash_allow() -> tuple[str, ...] | None:
    """The chunk hashes the tool call in progress must restrict its engine search to, or ``None``."""
    return _ALLOW.get()


@contextlib.contextmanager
def chash_allow_scope(allow: Sequence[str] | None) -> Iterator[None]:
    """Make *allow* the restriction ``search_cross_corpus`` sends for the tool call run inside the block."""
    token = _ALLOW.set(tuple(allow) if allow is not None else None)
    try:
        yield
    finally:
        _ALLOW.reset(token)
