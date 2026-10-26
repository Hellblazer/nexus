# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-232 section 3: the traversal evidence policy (nexus-jypmj.1.5).

``traverse`` walks the catalog link graph and used to return every reachable document's manifest
chashes in graph order, cut at 300 ids. A scoped ``nx_answer`` then received code chunks that
filled the cut ahead of the in-scope evidence. The policy runs INSIDE ``traverse``, after the walk
and before manifest expansion:

1. documents outside ``allowed_collections`` are dropped (:meth:`ScopeConstraint.admits`);
2. the rest are ranked by link role, then link distance, then tumbler (``traverse`` has no search
   relevance input, so none is invented) and the secondary ones capped at ``max_documents``;
3. manifests are read for the retained documents only;
4. the chunk budget ``max_chunks`` reserves a share per document, so one large document cannot
   consume it;
5. every returned id carries its document's collection (``chunk_collections``).

Seed documents are kept in their own stream: the rank and the cap never evict one (the scope still
applies to them, because an explicit scope wins). With ``preserve_seed_documents`` false a seed is an
ordinary candidate that ranks first (distance zero) and counts against the cap.

The engine caps its own walk at :data:`MAX_GRAPH_NODES` nodes, ordered by ``(min_depth, tumbler)``,
before any client sees them. A response that may have been cut is reported as possibly truncated and
never as contained (the report carries ``possibly_truncated``; :func:`capped_traversal_count` is
the count nexus-jypmj.1.12 posts on nexus-jypmj.1.13). The detector counts the nodes in the reply
plus the seeds that are missing from it: ``CatalogRepository.graphBFS`` joins every non-seed node to
a live document, so after the cut only a dead or missing seed can disappear, and a reply of 499
nodes with one missing seed may have been a 500-node walk. It is a detector, not a bound: the
engine's scoped-result marker (nexus-jypmj.1.13) is what stops the cut from reaching the client.

A policy that cannot be applied omits the graph stream with a warning. It never falls back to an
unrestricted walk.

This module imports only the standard library and :mod:`nexus.plans.evidence`.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from nexus.plans.evidence import ScopeConstraint

#: The engine's graph node cap, ``CatalogRepository.MAX_GRAPH_NODES``. A hand-kept copy: the
#: Java constant is private, and ``tests/test_rdr232_traversal_policy.py`` reads the Java source
#: and fails if the two drift.
MAX_GRAPH_NODES: int = 500

#: Secondary (non-seed) documents a policy keeps when the caller gave a scope. NOT calibrated: no
#: measurement of real ``nx_answer`` traversals exists yet (nexus-jypmj.1.12 records the first).
#: With no caller scope the runner derives :data:`MAX_GRAPH_NODES` instead, which binds nothing
#: (the engine never returns more nodes), so an uncalibrated DOCUMENT cut does not bind an unscoped
#: traverse until nexus-jypmj.1.12 has measured one. That is the only thing "unscoped" leaves alone:
#: ``nx_answer`` always supplies the policy (RDR-232 section 3), so an unscoped traverse is still ranked
#: by link role then distance, spends its chunk budget with a per-document share instead of the
#: graph-order prefix, and carries ``chunk_collections`` and ``traversal``; a one-role step drops the
#: other roles. ``test_the_unscoped_policy_output_differs_from_the_legacy_one_in_the_documented_ways``
#: pins exactly this.
DEFAULT_MAX_DOCUMENTS: int = 50

#: The secondary chunk budget; the same bound ``traverse`` has always put on ``ids``.
DEFAULT_MAX_CHUNKS: int = 300

LINK_ROLES: tuple[str, ...] = ("factual", "implementation", "citation", "related")

#: Role of a catalog link type. ``factual`` is not a link type's role; it names the step that
#: admits every role (the ``factual-evidence`` purpose: cites, implements, relates).
_ROLE_OF_LINK_TYPE: dict[str, str] = {
    "cites": "citation", "quotes": "citation", "supersedes": "citation",
    "implements": "implementation", "implements-heuristic": "implementation",
}

#: Rank order among roles when documents compete for the cap: a citation is the most direct
#: statement about its target, an implementation next, anything looser last.
_ROLE_RANK: dict[str, int] = {"citation": 0, "implementation": 1, "related": 2}

_WIRE_KEYS = frozenset({
    "allowed_collections", "preserve_seed_documents", "max_documents", "max_chunks", "require_link_role",
})


class TraversalPolicyError(ValueError):
    """The policy value is not one ``traverse`` can apply."""


def link_role(link_type: object) -> str:
    """The role of one catalog link type; an unknown type is ``related``."""
    return _ROLE_OF_LINK_TYPE.get(str(link_type), "related")


def _positive_int(wire: Mapping[str, Any], key: str, default: int) -> int:
    value = wire.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise TraversalPolicyError(f"{key} must be a positive integer, got {value!r}")
    return value


@dataclass(frozen=True)
class TraversalPolicy:
    """What a traversal may return as secondary evidence (RDR-232 section 3)."""

    allowed_collections: ScopeConstraint
    preserve_seed_documents: bool = True
    max_documents: int = DEFAULT_MAX_DOCUMENTS
    max_chunks: int = DEFAULT_MAX_CHUNKS
    #: One of :data:`LINK_ROLES`. ``factual`` admits every role; any other admits only documents
    #: whose best connecting link has that role.
    require_link_role: str = "factual"

    @classmethod
    def from_wire(cls, wire: object) -> TraversalPolicy:
        """Parse the dict form ``traverse`` receives; raise :class:`TraversalPolicyError` on any
        other shape, unknown key or value (a typo must not silently loosen the policy)."""
        if not isinstance(wire, Mapping):
            raise TraversalPolicyError(f"policy must be an object, got {type(wire).__name__}")
        unknown = sorted(set(wire) - _WIRE_KEYS)
        if unknown:
            raise TraversalPolicyError(f"unknown policy keys {unknown}")
        if "allowed_collections" not in wire:
            raise TraversalPolicyError("policy needs allowed_collections (an empty string allows all)")
        scope = wire["allowed_collections"]
        if scope is not None and not isinstance(scope, str):
            raise TraversalPolicyError(
                f"allowed_collections must be a string (one corpus prefix, collection or catalog subtree), got {scope!r}"
            )
        preserve = wire.get("preserve_seed_documents", True)
        if not isinstance(preserve, bool):
            raise TraversalPolicyError(f"preserve_seed_documents must be a boolean, got {preserve!r}")
        role = wire.get("require_link_role", "factual")
        if role not in LINK_ROLES:
            raise TraversalPolicyError(f"require_link_role must be one of {list(LINK_ROLES)}, got {role!r}")
        return cls(
            allowed_collections=ScopeConstraint.parse(scope),
            preserve_seed_documents=preserve,
            max_documents=_positive_int(wire, "max_documents", DEFAULT_MAX_DOCUMENTS),
            max_chunks=min(_positive_int(wire, "max_chunks", DEFAULT_MAX_CHUNKS), DEFAULT_MAX_CHUNKS),
            require_link_role=role,
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "allowed_collections": self.allowed_collections.value,
            "preserve_seed_documents": self.preserve_seed_documents,
            "max_documents": self.max_documents,
            "max_chunks": self.max_chunks,
            "require_link_role": self.require_link_role,
        }

    def admits_role(self, role: str) -> bool:
        return self.require_link_role == "factual" or role == self.require_link_role


def _as_list(value: object) -> list[str]:
    """A link-type value as a list: a bare string is one type, never its characters."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value] if isinstance(value, (list, tuple, set, frozenset)) else [str(value)]


def _caller_scope(scope: object) -> object:
    """The caller's scope as the one string a policy can hold. A list of one is that one, an empty
    list is no scope; several are returned as given so :meth:`TraversalPolicy.from_wire` refuses
    them (the graph stream is omitted with a warning) instead of stringifying them into a prefix
    that admits nothing."""
    if isinstance(scope, (list, tuple)):
        parts = [str(p).strip() for p in scope if str(p).strip()]
        if not parts:
            return ""
        return parts[0] if len(parts) == 1 else list(parts)
    return scope


def _tightened(derived: dict[str, Any], declared: object) -> dict[str, Any]:
    """*derived* with the tightening-only fields a plan declared: a lower ``max_documents`` or
    ``max_chunks``, and a ``require_link_role`` that narrows ``factual``. Nothing else is read, and
    a malformed value is ignored, so a plan can never loosen the caller's containment."""
    if not isinstance(declared, Mapping):
        return derived
    out = dict(derived)
    for key in ("max_documents", "max_chunks"):
        value = declared.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 1 <= value < out[key]:
            out[key] = value
    role = declared.get("require_link_role")
    if out["require_link_role"] == "factual" and role in LINK_ROLES:
        out["require_link_role"] = role
    return out


def derive_policy(step_args: Mapping[str, Any], caller_scope: object) -> dict[str, Any]:
    """The wire policy the runner gives a ``traverse`` step (RDR-232 section 3, "nx_answer always
    supplies the policy derived from request and plan").

    The scope is the caller's (an empty one allows everything); the link role is read from the
    step's own link types, so a ``find-implementations`` step asks for ``implementation`` and a
    ``factual-evidence`` one for ``factual``. With no caller scope (none, or ``all``) the document
    cap is :data:`MAX_GRAPH_NODES`, which binds nothing, until nexus-jypmj.1.12 calibrates one (the
    policy still ranks, allocates chunks per document and annotates the output); a scoped run keeps
    :data:`DEFAULT_MAX_DOCUMENTS`. A policy the plan declared is not used as given:
    only the fields that tighten the derived policy are merged (:func:`_tightened`).
    """
    from nexus.plans.purposes import resolve_purpose  # noqa: PLC0415 — deferred: reads a YAML registry on first use

    link_types = _as_list(step_args.get("link_types"))
    if not link_types and step_args.get("purpose"):
        link_types = resolve_purpose(str(step_args["purpose"]))
    roles = {link_role(t) for t in link_types}
    role = next(iter(roles)) if len(roles) == 1 else "factual"
    scope = _caller_scope(caller_scope)
    constraint = ScopeConstraint.parse(scope) if not isinstance(scope, list) else None
    unscoped = constraint is not None and (constraint.kind == "none" or constraint.value == "all")
    wire = TraversalPolicy(
        allowed_collections=constraint if constraint is not None else ScopeConstraint.parse(""),
        require_link_role=role,
        max_documents=MAX_GRAPH_NODES if unscoped else DEFAULT_MAX_DOCUMENTS,
    ).to_wire()
    if constraint is None:
        wire["allowed_collections"] = scope  # a scope the policy cannot hold: from_wire refuses it
    return _tightened(wire, step_args.get("policy"))


# ── selection ──────────────────────────────────────────────────────────────


def _tumbler_key(tumbler: str) -> tuple[Any, ...]:
    parts = tumbler.split(".")
    return (0, tuple(int(p) for p in parts)) if all(p.isdigit() for p in parts) else (1, tumbler)


def _count(content_types: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in content_types:
        out[c] = out.get(c, 0) + 1
    return out


@dataclass(frozen=True)
class Selection:
    """The documents a policy keeps, in output order (seeds first, then by rank), and its report."""

    kept: tuple[str, ...]
    collections: Mapping[str, str]
    report: dict[str, Any]


def _distance_and_role(
    seeds: Sequence[str], edges: Iterable[Any], direction: str,
) -> tuple[dict[str, int], dict[str, str]]:
    """Link distance from the nearest seed and the best role among the links that reach a node at
    that distance, by a breadth-first pass over the edges the engine returned.

    The engine does not send depth or role, so both are recomputed from its edges. Where the engine
    dropped nodes at its cap the recomputed distance can only be larger than the true one.
    """
    adj: dict[str, list[tuple[str, str]]] = {}
    for e in edges:
        frm, to, role = str(e.from_tumbler), str(e.to_tumbler), link_role(e.link_type)
        if direction in ("out", "both"):
            adj.setdefault(frm, []).append((to, role))
        if direction in ("in", "both"):
            adj.setdefault(to, []).append((frm, role))
    dist = {s: 0 for s in seeds}
    best: dict[str, str] = {}
    frontier = deque(seeds)
    while frontier:
        level = [frontier.popleft() for _ in range(len(frontier))]
        found: dict[str, str] = {}
        for u in level:
            for v, role in adj.get(u, ()):
                if v in dist:
                    continue
                prev = found.get(v)
                if prev is None or _ROLE_RANK.get(role, 2) < _ROLE_RANK.get(prev, 2):
                    found[v] = role
        for v, role in found.items():
            dist[v] = dist[level[0]] + 1
            best[v] = role
            frontier.append(v)
    return dist, best


def select_documents(
    nodes: Sequence[Any], edges: Sequence[Any], seeds: Sequence[Any], policy: TraversalPolicy,
    *, direction: str = "both",
) -> Selection:
    """Apply *policy* to one engine walk: scope, role, rank, cap. Reads no manifest."""
    collection_of = {str(n.tumbler): str(getattr(n, "physical_collection", "") or "") for n in nodes}
    # The counts are by the document's catalog content_type, a row field: never a parse of the
    # collection name (RDR-204, tests/test_collection_name_parse_census.py).
    type_of = {str(n.tumbler): str(getattr(n, "content_type", "") or "") or "unattributed" for n in nodes}
    seed_ids = [s for s in dict.fromkeys(str(t) for t in seeds) if s in collection_of]
    dist, role = _distance_and_role(seed_ids, edges, direction)

    scope = policy.allowed_collections
    dropped_by_corpus: dict[str, int] = {}
    dropped_scope = dropped_role = 0
    kept_seeds: list[str] = []
    candidates: list[str] = []
    for tumbler, collection in collection_of.items():
        if not scope.admits(collection, tumbler=tumbler):
            dropped_scope += 1
            dropped_by_corpus[type_of[tumbler]] = dropped_by_corpus.get(type_of[tumbler], 0) + 1
        elif tumbler in seed_ids and policy.preserve_seed_documents:
            kept_seeds.append(tumbler)
        elif tumbler not in seed_ids and not policy.admits_role(role.get(tumbler, "related")):
            dropped_role += 1
        else:
            candidates.append(tumbler)
    kept_seeds.sort(key=seed_ids.index)
    candidates.sort(key=lambda t: (
        -1 if t in seed_ids else _ROLE_RANK.get(role.get(t, "related"), 2),
        dist.get(t, MAX_GRAPH_NODES), _tumbler_key(t),
    ))
    secondary = candidates[: policy.max_documents]
    dropped_cap = len(candidates) - len(secondary)
    kept = tuple(kept_seeds + secondary)

    kept_collections = {t: collection_of[t] for t in kept}
    seed_types = {type_of[t] for t in kept_seeds}
    widening = [t for t in secondary if type_of[t] not in seed_types]
    unscoped = scope.kind == "none" or scope.value == "all"
    widened = None
    if unscoped and seed_types and widening:
        roles = sorted({role.get(t, "related") for t in widening})
        widened = {
            "reason": (
                f"no caller scope: {'/'.join(roles)} links admitted "
                f"{sorted({type_of[t] for t in widening})} beyond the seed corpus"
            ),
            "corpus_counts": _count(type_of[t] for t in kept),
        }
    conflict = None
    if dropped_scope and policy.require_link_role == "implementation":
        conflict = {
            "caller_scope": scope.value, "scope_kind": scope.kind,
            "link_role": policy.require_link_role, "dropped_by_corpus": dict(dropped_by_corpus),
        }
    # The engine slices the walk to MAX_GRAPH_NODES, then drops dead seeds (graphBFS joins every
    # other node to a live document), so a missing seed is the one thing a cut can hide. A reply
    # short of the cap by no more than its missing seeds may have been cut.
    missing_seeds = len({str(t) for t in seeds} - set(collection_of))
    capped = len(nodes) + missing_seeds >= MAX_GRAPH_NODES
    report = {
        "policy_applied": True,
        "policy_failed": False,
        "nodes_walked": len(nodes),
        "nodes_retained": len(kept),
        "dropped_out_of_scope": dropped_scope,
        "dropped_role": dropped_role,
        "dropped_cap": dropped_cap,
        "dropped_by_corpus": dropped_by_corpus,
        "corpus_counts": _count(type_of[t] for t in kept),
        "possibly_truncated": capped,
        "contained": not capped,
        "widened": widened,
        "scope_conflict": conflict,
    }
    return Selection(kept=kept, collections=kept_collections, report=report)


def allocate_chunks(
    order: Sequence[str], chashes_by_document: Mapping[str, Sequence[str]], max_chunks: int,
) -> tuple[list[tuple[str, str]], int]:
    """Spend the chunk budget over the documents in *order*, each keeping a reserved share.

    A chunk held by several documents belongs to the first of them in *order* (the legacy rule: one
    chash is one T3 row). Returns the ``(document, chash)`` pairs grouped by document in *order*,
    and the total before the budget. The first pass gives each document up to
    ``max_chunks // documents`` chunks (at least one); the second spends what a short document left
    unused, in rank order.
    """
    seen: set[str] = set()
    owned: dict[str, list[str]] = {}
    for doc in order:
        mine = [c for c in chashes_by_document.get(doc, ()) if c and c not in seen and not seen.add(c)]
        if mine:
            owned[doc] = mine
    total = sum(len(v) for v in owned.values())
    if total <= max_chunks:
        return [(d, c) for d, cs in owned.items() for c in cs], total
    reserve = max(1, max_chunks // len(owned))
    take: dict[str, int] = {}
    budget = max_chunks
    for doc, cs in owned.items():
        take[doc] = min(len(cs), reserve, budget)
        budget -= take[doc]
    for doc, cs in owned.items():
        if budget <= 0:
            break
        more = min(len(cs) - take[doc], budget)
        take[doc] += more
        budget -= more
    return [(d, c) for d, cs in owned.items() for c in cs[: take[d]]], total


def capped_traversal_count(outputs: Iterable[object]) -> int:
    """How many traverse outputs in *outputs* came back at the engine's node cap.

    nexus-jypmj.1.12 reads this over the golden set, the canary and a window of live traffic; the
    result decides whether the engine pre-cap filter (nexus-jypmj.1.13) is taken. Anything that is
    not a policy-annotated traverse output counts zero.
    """
    n = 0
    for out in outputs:
        report = out.get("traversal") if isinstance(out, Mapping) else None
        if isinstance(report, Mapping) and report.get("possibly_truncated") is True:
            n += 1
    return n
