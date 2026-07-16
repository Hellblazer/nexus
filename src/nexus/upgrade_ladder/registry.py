# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-185 P0.1: the ordered ladder registry — RQ2's five hard edges as data.

The five hard ordering edges (RDR-185 Research RQ2, no cycles):

1. package → everything            (:data:`PRECONDITION_PACKAGE` → ``*``)
2. engine → substrate ETL          (:data:`PRECONDITION_ENGINE` → substrate-etl)
3. T2 schema → all T2 reads        (:data:`RUNG_T2_SCHEMA` → ``*``)
4. chunk-identity → T3 ETL         (co-resident, :data:`CO_RESIDENT_AXES`)
5. embedder-era + chunk-identity CO-RESIDENT inside the substrate-ETL rung
   (co-resident, :data:`CO_RESIDENT_AXES`)

Edges 1–3 are sequencing edges over preconditions and rungs; edges 4–5 are
encoded as CO-RESIDENCY — both axes are in-flight transforms INSIDE the
substrate-ETL rung, satisfied by construction rather than by sequencing.

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

#: T2 schema rung (native reference implementation lands in P1, bead .8).
RUNG_T2_SCHEMA = "t2-schema"
#: Substrate ETL rung (Chroma→pgvector with wire re-id; lands in P2).
RUNG_SUBSTRATE_ETL = "substrate-etl"

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
RUNG_ORDER: tuple[str, ...] = (RUNG_T2_SCHEMA, RUNG_SUBSTRATE_ETL)

#: RQ2 hard edges as ``(before, after)`` pairs; ``after == ALL_RUNGS`` means
#: the source precedes every rung. Edges 4–5 are in CO_RESIDENT_AXES.
HARD_EDGES: tuple[tuple[str, str], ...] = (
    (PRECONDITION_PACKAGE, ALL_RUNGS),
    (PRECONDITION_ENGINE, RUNG_SUBSTRATE_ETL),
    (RUNG_T2_SCHEMA, ALL_RUNGS),
)

#: RQ2 edges 4–5: axes that live INSIDE a rung as in-flight transforms.
#: chunk-identity is computed on the wire during the substrate ETL (the id is
#: derivable from the chunk text being carried); embedder-era remap is a
#: co-resident leg of the same rung — never a separate, sequenced rung.
CO_RESIDENT_AXES: dict[str, str] = {
    "chunk-identity": RUNG_SUBSTRATE_ETL,
    "embedder-era": RUNG_SUBSTRATE_ETL,
}


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


def default_registry(*, db_path_fn=None) -> LadderRegistry:
    """The production ladder. Native rungs land phase by phase: t2-schema
    (P1, here), substrate-etl (P2), each slotting into :data:`RUNG_ORDER`
    position.

    ``db_path_fn`` is an injectable T2 path seam: ``nx upgrade`` routes its
    own ``_db_path`` test seam through so patched-path tests never touch a
    live install's ``memory.db``; ``None`` uses the production config-dir
    default (correct for ``nx doctor``'s read-only detect sweep).
    """
    from nexus.upgrade_ladder.rungs.t2_schema import T2SchemaRung  # noqa: PLC0415 — deferred to avoid import cycle

    kwargs = {} if db_path_fn is None else {"db_path_fn": db_path_fn}
    return LadderRegistry((T2SchemaRung(**kwargs),))
