# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-232 sections 1, 2a and 3 (nexus-jypmj.1.18): an explicit owner narrows the secondary streams.

An explicit ``owner`` or ``project`` binding (:func:`nexus.plans.evidence.resolve_owner_qualifier`)
selects the named source's project. Before this module it selected only the named PRIMARY source: the
vector stream still read every project's ``rdr`` collections, so another project's same-number RDR
reached the reducer (the golden fixture's wrong-source contamination, nexus-jypmj.1.8). The rule,
decided on nexus-jypmj.1.18:

* The owners are the explicit owners of the request (:func:`request_owners`): the ``owner`` and
  ``project`` bindings only. A catalog-subtree ``scope`` is the owner qualifier for source resolution
  (RDR-232 section 1) and is not intersected with any corpus (section 2a), so it narrows nothing here.
  An owner that resolution inferred does not narrow; neither does a qualifier that conflicted or did
  not resolve. The run then proceeds without owner narrowing and says so
  (``scope_check.owner_narrowing.not_applied``): the section 1 disclose-don't-choose rule.
* **Vector stream.** After the section 2a corpus narrowing, a corpus-taking retrieval step keeps
  only those collections of a project-owned content type (:data:`PROJECT_OWNED_TYPES`: ``code``,
  ``docs``, ``rdr``) whose owner segment (``1.2`` is ``1-2``) matches an owner. Any other content
  type (``knowledge``, ``paper``, ...) passes through unchanged. A corpus prefix is expanded to
  concrete collections first, by the same routing ``search`` and ``query`` use, so the step then
  reads exactly the collections the filter kept.
* A project-owned collection whose owner identity is not tumbler-shaped is **unattributable**: an old
  two-segment repository name (``docs__nexus-571b8edd``), or a collection with no catalog row whose name
  fails the grammar (``docs__default``). It cannot be matched to an owner, so it is kept and listed in
  the step's ``unattributed`` diagnostic instead of being dropped as foreign or passed in silence.
* A step the narrowing leaves with no collection is a conflict: the runner skips it, it contributes
  no evidence, and it is disclosed. It never runs unscoped. A step pinned through
  ``collection``/``collections`` keeps its pin and is reported as ``unscoped_collection_pin``.
* **Graph stream.** The traversal policy admits, among non-seed documents whose collection is of a
  project-owned type, only those whose tumbler lies under an owner; documents in any other collection
  pass, as the vector stream passes them, and unattributable collections pass and are reported
  (:mod:`nexus.plans.traversal_policy`, ``allowed_owners``; :func:`collection_kind` supplies the kind).

Collection identity is read from the catalog row (RDR-204). A collection with no cached row falls back
to :func:`nexus.corpus.collection_type_and_owner`, the name-grammar helper: a conformant name gives its
owner segment, a legacy or malformed name gives its remainder, which is not tumbler-shaped and so is
unattributable. This module itself never splits a name.

Disclosure: :meth:`OwnerNarrowing.to_diagnostic` is shaped like
:meth:`nexus.plans.evidence.CorpusNarrowing.to_diagnostic` (same keys, ``scope_kind`` ``owner``) plus
the owners and the collections removed, and goes in ``PlanResult.scope_diagnostics``.

Pure apart from the two seams, :func:`expand_corpus` and :func:`attribute_collection`, which read the
live collection listing and the catalog rows; tests replace them.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

import structlog

_log = structlog.get_logger(__name__)

__all__ = [
    "OWNERS_BINDING",
    "PROJECT_OWNED_TYPES",
    "CollectionKind",
    "OwnerNarrowing",
    "attribute_collection",
    "collection_kind",
    "expand_corpus",
    "narrow_args_by_owner",
    "owner_segments",
    "owner_tumblers",
    "owners_binding_value",
    "request_owners",
]

#: The internal run binding that carries the request's owners (comma-joined owner tumblers) to the
#: runner, beside ``_nx_scope``. ``nx_answer`` sets it after the evidence request is built; it is
#: never a plan binding and is not offered to the matcher.
OWNERS_BINDING: str = "_nx_owners"

#: Content types whose collections belong to one project. Every other type passes the owner filter.
PROJECT_OWNED_TYPES: frozenset[str] = frozenset({"code", "docs", "rdr"})

#: Where a step with no ``corpus`` at all reads: the shared default of the combined-query tools
#: (``search_metadata_scoped``, ``search_topic_scoped``, ``search_graph_hop``,
#: ``search_aspect_scoped``). ``search`` and ``query`` always have a corpus by the time owner
#: narrowing runs (the runner's own default corpus fills it).
TOOL_DEFAULT_CORPUS: str = "knowledge,code,docs"

#: The origins of an owner qualifier that the caller supplied.
_EXPLICIT_ORIGINS: frozenset[str] = frozenset({"owner", "project"})

_OWNER_TUMBLER_RE = re.compile(r"^\d+\.\d+(?:\.\d+)*$")

#: The shape of a collection's owner id when it names a catalog owner: ``1-2`` for owner tumbler ``1.2``.
_OWNER_ID_RE = re.compile(r"^\d+-\d+$")

#: How a collection stands against the owner filter: ``project_owned`` (a code, docs or rdr collection
#: with a tumbler-shaped owner: the filter applies), ``other`` (any other content type: it passes), or
#: ``unattributed`` (a project-owned type whose owner cannot be read, or no identity at all: it passes
#: and is disclosed).
CollectionKind = Literal["project_owned", "other", "unattributed"]


# ── the request's owners ────────────────────────────────────────────────────


def _owner_address(tumbler: str) -> str:
    """``1.2.4686`` -> ``1.2``: the owner a document tumbler lies under."""
    return ".".join(tumbler.split(".")[:2])


def request_owners(request: Any) -> tuple[str, ...]:
    """The explicit owners of *request* (an :class:`~nexus.plans.evidence.EvidenceRequest`), as owner
    tumblers (``1.2``): the ``owner`` and ``project`` bindings. Empty when none was supplied or the
    qualifier did not resolve to one owner (a conflicting or unresolved one picked nothing), and when
    the only qualifier is a catalog-subtree ``scope``, which is not an owner binding."""
    owner = getattr(request, "owner", None)
    if owner is None or getattr(owner, "status", "") != "resolved":
        return ()
    found: list[str] = []
    for origin, tumbler in getattr(owner, "contributions", ()) or ():
        if origin in _EXPLICIT_ORIGINS and _OWNER_TUMBLER_RE.match(str(tumbler)):
            address = _owner_address(str(tumbler))
            if address not in found:
                found.append(address)
    return tuple(found)


def owners_binding_value(owners: Iterable[str]) -> str:
    """The ``_nx_owners`` binding for *owners* (``""`` for none)."""
    return ",".join(dict.fromkeys(o for o in owners if o))


def owner_tumblers(value: object) -> tuple[str, ...]:
    """Owner tumblers from the ``_nx_owners`` binding (a comma string or a list); malformed
    entries are dropped."""
    parts = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
    out = [_owner_address(p.strip()) for p in parts if _OWNER_TUMBLER_RE.match(str(p).strip())]
    return tuple(dict.fromkeys(out))


def owner_segments(owners: Iterable[str]) -> frozenset[str]:
    """The collection-name owner segment of each owner tumbler (``1.2`` -> ``1-2``)."""
    from nexus.catalog.collection_name import owner_segment_for_tumbler  # noqa: PLC0415 — deferred: keeps this module's import light

    return frozenset(s for s in (owner_segment_for_tumbler(o) for o in owners) if s)


# ── the two seams ───────────────────────────────────────────────────────────


def expand_corpus(corpus: str) -> list[str]:
    """The concrete collections *corpus* routes to: the routing ``search`` and ``query`` use for the
    same string (quarantine and non-live collections excluded, small fan-out members under the
    floor excluded)."""
    from nexus.mcp import core as mcp_core  # noqa: PLC0415 — deferred: the mcp layer imports the runner
    from nexus.mcp_infra import get_t3  # noqa: PLC0415 — deferred: the mcp layer imports the runner

    return list(mcp_core._resolve_corpus_target(corpus, get_t3()))


def attribute_collection(name: str) -> tuple[str, str] | None:
    """``(content_type, owner_id)`` of collection *name*, from its catalog row (RDR-204), or from
    the name-grammar helper when it has no cached row; ``None`` when it has neither."""
    from nexus.mcp_infra import get_collection_row  # noqa: PLC0415 — deferred: circular-dep avoidance (mcp_infra)

    row = get_collection_row(name, refresh=False)
    if row is not None:
        return str(row["content_type"]), str(row["owner_id"])
    from nexus.corpus import collection_type_and_owner  # noqa: PLC0415 — deferred: keeps this module's import light

    try:
        return collection_type_and_owner(name)
    except ValueError:
        return None


def _identity(name: str) -> tuple[CollectionKind, str]:
    """``(kind, owner_id)`` of collection *name*; ``owner_id`` is ``""`` unless the kind is
    ``project_owned``."""
    found = attribute_collection(name)
    if found is None:
        return "unattributed", ""
    content_type, owner_id = found
    if content_type not in PROJECT_OWNED_TYPES:
        return "other", ""
    if not _OWNER_ID_RE.match(str(owner_id)):
        return "unattributed", ""
    return "project_owned", str(owner_id)


def collection_kind(name: str) -> CollectionKind:
    """How collection *name* stands against the owner filter (see :data:`CollectionKind`). The
    traversal policy takes this per collection so the graph stream applies the vector stream's rule."""
    return _identity(name)[0]


# ── the narrowing ───────────────────────────────────────────────────────────

OwnerStatus = Literal["narrowed", "within_scope", "conflict", "unscoped_collection_pin"]


@dataclass(frozen=True)
class _OwnerScope:
    """The ``scope`` face of an owner narrowing, shaped like
    :class:`~nexus.plans.evidence.ScopeConstraint` for the conflict marker and the notice."""

    value: str
    kind: str = "owner"


@dataclass(frozen=True)
class OwnerNarrowing:
    """The outcome of applying the request's owners to one retrieval step's corpus."""

    status: OwnerStatus
    owners: tuple[str, ...]
    plan_corpus: str | None
    corpus: str | None
    kept: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    detail: str = ""
    #: Kept collections whose owner could not be read (see :data:`CollectionKind`); disclosed, not judged.
    unattributed: tuple[str, ...] = ()

    @property
    def conflict(self) -> bool:
        return self.status == "conflict"

    @property
    def scope(self) -> _OwnerScope:
        return _OwnerScope(",".join(sorted(owner_segments(self.owners))) or ",".join(self.owners))

    def to_diagnostic(self, *, step_index: int, tool: str) -> dict[str, Any]:
        """Wire form for ``PlanResult.scope_diagnostics``: the keys of
        :meth:`~nexus.plans.evidence.CorpusNarrowing.to_diagnostic` plus the owners and the
        collections the filter removed."""
        return {
            "step_index": step_index,
            "tool": tool,
            "plan_corpus": self.plan_corpus,
            "caller_scope": self.scope.value,
            "scope_kind": "owner",
            "outcome": self.status,
            "resolved_corpus": self.corpus,
            "owners": list(self.owners),
            "kept_collections": len(self.kept),
            "removed_collections": list(self.removed),
            "unattributed": list(self.unattributed),
            "detail": self.detail,
        }


def narrow_args_by_owner(
    args: dict[str, Any], owners: object,
) -> tuple[dict[str, Any], OwnerNarrowing | None]:
    """Apply *owners* (the ``_nx_owners`` binding) to a corpus-taking retrieval step's resolved
    *args*; returns ``(args, narrowing)``. The caller has already checked the tool.

    ``narrowing`` is ``None`` exactly when no owner applies. A pinned step
    (``collection``/``collections``) is returned unchanged as ``unscoped_collection_pin``. Otherwise
    the step's corpus (the tool default when it has none) is expanded to collections and the
    project-owned ones of another owner are removed: nothing removed leaves the args as they were
    (``within_scope``); some removed gives the kept collections as the new ``corpus``
    (``narrowed``); none left is a ``conflict`` and the caller skips the step. A corpus that cannot
    be expanded is a ``conflict`` too, never an unscoped run.
    """
    tumblers = owner_tumblers(owners)
    if not tumblers:
        return args, None
    plan_corpus = str(args.get("corpus") or "") or None
    if any(args.get(k) for k in ("collection", "collections")):
        return args, OwnerNarrowing("unscoped_collection_pin", tumblers, plan_corpus, plan_corpus)
    segments = owner_segments(tumblers)
    corpus = plan_corpus or TOOL_DEFAULT_CORPUS
    try:
        collections = list(expand_corpus(corpus))
    except Exception as exc:  # noqa: BLE001 — the step is skipped and disclosed; it never runs unscoped
        _log.warning("owner_narrowing_expand_failed", corpus=corpus, error=str(exc))
        return args, OwnerNarrowing(
            "conflict", tumblers, plan_corpus, None,
            detail=f"the corpus could not be expanded to collections ({type(exc).__name__})",
        )
    kept: list[str] = []
    removed: list[str] = []
    unattributed: list[str] = []
    for name in collections:
        kind, owner_id = _identity(name)
        if kind == "project_owned" and owner_id not in segments:
            removed.append(name)
            continue
        kept.append(name)
        if kind == "unattributed":
            unattributed.append(name)
    if not kept:
        return args, OwnerNarrowing(
            "conflict", tumblers, plan_corpus, None, kept=(), removed=tuple(removed),
            detail=(
                f"no collection of {corpus!r} is owned by {', '.join(tumblers)}"
                if removed else f"{corpus!r} resolves to no collection"
            ),
        )
    if not removed:
        return args, OwnerNarrowing(
            "within_scope", tumblers, plan_corpus, plan_corpus, kept=tuple(kept),
            unattributed=tuple(unattributed),
        )
    out = dict(args)
    out["corpus"] = ",".join(kept)
    return out, OwnerNarrowing(
        "narrowed", tumblers, plan_corpus, out["corpus"], kept=tuple(kept), removed=tuple(removed),
        unattributed=tuple(unattributed),
    )
