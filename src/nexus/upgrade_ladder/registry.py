# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-185 P0.1: the ordered ladder registry — RQ2's hard edges as data.

RDR-155 P4b reshaped the edge set: the t2-schema and substrate-etl rungs
(and their co-resident chunk-identity / embedder-era axes) died with the
Chroma + client-SQLite migration machinery. What remains:

1. package → everything            (:data:`PRECONDITION_PACKAGE` → ``*``)
2. engine → chash rekey            (:data:`PRECONDITION_ENGINE` → chash-rekey)

Package/engine/process are STATELESS preconditions (RDR-185 Constraints):
re-derived from ON-DISK state at every invocation, converged before the
ladder walks (wired in P3, nexus-n7u38.23) — they are NOT rungs and never
appear in :data:`RUNG_ORDER`. Their edges here document the walk contract;
the registry can only mechanically enforce rung-to-rung order.

The hooks/config axis has NO assigned position: that is the P3 decision
spike (nexus-n7u38.22, the gate's one genuine ambiguity — chicken-and-egg
stale-stanza hazard). Encoding a position now would silently assume the
answer.
"""
from __future__ import annotations

from collections.abc import Iterator, Sequence

from nexus.upgrade_ladder.protocol import Rung

# ── Canonical node names ─────────────────────────────────────────────────────

#: RDR-180 chash rekey rung (nexus-jxizy.6): the freeze-gated full-digest
#: cutover — needs the bytea schema + /v1/remap/rekey (ENGINE precondition).
#: RDR-155 P4b: the t2-schema and substrate-etl rungs died with the
#: migration machinery; the rekey rung is RDR-185's standing convergence
#: mechanism and the ladder's sole data rung.
RUNG_CHASH_REKEY = "chash-rekey"

#: Stateless preconditions — NOT rungs (see module docstring).
PRECONDITION_PACKAGE = "precondition:package"
PRECONDITION_ENGINE = "precondition:engine"
PRECONDITION_PROCESS = "precondition:process"

#: Wildcard target in an edge: "before every rung except the source".
ALL_RUNGS = "*"

#: Canonical total order over the known DATA rungs. New rungs are inserted
#: here (with their edges in :data:`HARD_EDGES`) — the order is validated
#: against the edges at import/construction time, so an inconsistent insert
#: fails immediately rather than walking in a wrong order.
RUNG_ORDER: tuple[str, ...] = (RUNG_CHASH_REKEY,)

#: RQ2 hard edges as ``(before, after)`` pairs; ``after == ALL_RUNGS`` means
#: the source precedes every rung.
HARD_EDGES: tuple[tuple[str, str], ...] = (
    (PRECONDITION_PACKAGE, ALL_RUNGS),
    (PRECONDITION_ENGINE, RUNG_CHASH_REKEY),
)

#: RQ2 edges 4–5 (chunk-identity / embedder-era co-residency inside the
#: substrate-ETL rung) retired with that rung at RDR-155 P4b. The mapping
#: stays as the co-residency REGISTRY (empty = no co-resident axes today);
#: a future rung hosting an in-flight transform registers it here.
CO_RESIDENT_AXES: dict[str, str] = {}


class LadderOrderError(ValueError):
    """A rung set or edge set violates the ladder's ordering contract."""


def expand_edges(
    edges: Sequence[tuple[str, str]],
    order: Sequence[str],
) -> tuple[tuple[str, str], ...]:
    """Expand ``ALL_RUNGS`` wildcards into concrete ``(before, after)`` pairs
    over the rungs in *order*. Self-edges are never produced."""
    expanded: list[tuple[str, str]] = []
    for before, after in edges:
        if after == ALL_RUNGS:
            expanded.extend((before, rung) for rung in order if rung != before)
        else:
            expanded.append((before, after))
    return tuple(expanded)


def validate_hard_edges(
    edges: Sequence[tuple[str, str]] = HARD_EDGES,
    order: Sequence[str] = RUNG_ORDER,
) -> None:
    """Validate that *edges* form an acyclic graph and that *order* is a
    topological order for every rung-to-rung edge.

    Raises :class:`LadderOrderError` on a cycle or an order violation.
    Precondition→rung edges are structural (preconditions converge before
    the walk by contract) and are exempt from the order-index check.
    """
    expanded = expand_edges(edges, order)

    # Cycle detection (iterative DFS with colors) over ALL expanded edges.
    graph: dict[str, list[str]] = {}
    for before, after in expanded:
        graph.setdefault(before, []).append(after)
        graph.setdefault(after, [])
    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(graph, WHITE)
    for root in graph:
        if color[root] != WHITE:
            continue
        stack: list[tuple[str, Iterator[str]]] = [(root, iter(graph[root]))]
        color[root] = GRAY
        while stack:
            node, children = stack[-1]
            advanced = False
            for child in children:
                if color[child] == GRAY:
                    raise LadderOrderError(
                        f"ladder edge cycle detected at {child!r} (via {node!r})"
                    )
                if color[child] == WHITE:
                    color[child] = GRAY
                    stack.append((child, iter(graph[child])))
                    advanced = True
                    break
            if not advanced:
                color[node] = BLACK
                stack.pop()

    # Order consistency for rung-to-rung edges.
    position = {name: i for i, name in enumerate(order)}
    for before, after in expanded:
        if before in position and after in position and position[before] >= position[after]:
            raise LadderOrderError(
                f"RUNG_ORDER violates hard-edge order {before!r} → {after!r}"
            )


class LadderRegistry:
    """Holds rungs in walk order, validated against :data:`HARD_EDGES`.

    The registry is an ordered container only — walking (with the RDR-142
    verify-before-record guard) is the runner's job (P0.3). Rung names not
    named by any hard edge (interim wrapped-verb rungs, test fixtures) are
    accepted in the order given; edges over KNOWN names are enforced.
    """

    def __init__(self, rungs: Sequence[Rung]) -> None:
        validate_hard_edges()
        names = [rung.name for rung in rungs]
        seen: set[str] = set()
        for name in names:
            if name in seen:
                raise LadderOrderError(f"duplicate rung name {name!r} in ladder registry")
            seen.add(name)
        position = {name: i for i, name in enumerate(names)}
        for before, after in expand_edges(HARD_EDGES, RUNG_ORDER):
            if before in position and after in position and position[before] >= position[after]:
                raise LadderOrderError(
                    f"registry order violates hard edge {before!r} → {after!r}"
                )
        self._rungs: tuple[Rung, ...] = tuple(rungs)

    @property
    def rungs(self) -> tuple[Rung, ...]:
        return self._rungs

    def __iter__(self) -> Iterator[Rung]:
        return iter(self._rungs)

    def __len__(self) -> int:
        return len(self._rungs)


def default_registry() -> LadderRegistry:
    """The production ladder.

    RDR-155 P4b: the t2-schema and substrate-etl rungs died with the
    migration machinery (Chroma + client-SQLite retirement) — the ladder
    is rekey-only. The RDR-180 chash-rekey rung SURVIVES (D-D): it is
    RDR-185's standing convergence mechanism, not migration plumbing.
    """
    from nexus.upgrade_ladder.rungs.chash_rekey import default_chash_rekey_rung  # noqa: PLC0415 — deferred to avoid import cycle

    return LadderRegistry((default_chash_rekey_rung(),))  # type: ignore[arg-type]
