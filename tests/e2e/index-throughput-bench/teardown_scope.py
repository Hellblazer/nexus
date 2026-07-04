"""Teardown scoping for the index-throughput benchmark (nexus-duoak.3).

Pure logic, unit-tested: decide exactly which collections the benchmark
may delete. Collection names embed the OWNER TUMBLER in dashed form
(``code__1-15__voyage-code-3__v1`` = owner 1.15), not the repo name, so
the bench marker cannot be matched against collection names directly.
Instead:

1. run.sh names every clone dir ``benchidx-<stamp>-w<N>``, so the catalog
   owner registered for it (``ensure_owner_for_repo`` uses the dir
   basename) carries the marker in its ``name``.
2. ``bench_tumblers`` selects owners whose name carries the marker.
3. ``plan_teardown`` deletes only collections that are BOTH new (absent
   from the before-snapshot) AND owned by a bench tumbler. New non-bench
   collections are reported as unexpected — surfaced, never deleted.
"""

from __future__ import annotations

BENCH_MARKER = "benchidx-"


def bench_tumblers(owners: list[dict]) -> set[str]:
    """Tumblers (dashed form, as embedded in collection names) of bench owners."""
    return {
        o["tumbler"].replace(".", "-")
        for o in owners
        if BENCH_MARKER in o.get("name", "")
    }


def _owner_segment(collection: str) -> str | None:
    parts = collection.split("__")
    return parts[1] if len(parts) == 4 else None


def plan_teardown(
    before: list[str], after: list[str], tumblers: set[str]
) -> tuple[list[str], list[str]]:
    """Return ``(to_delete, unexpected)`` — see module docstring."""
    pre = set(before)
    new = [c for c in after if c not in pre]
    to_delete = [c for c in new if _owner_segment(c) in tumblers]
    unexpected = [c for c in new if _owner_segment(c) not in tumblers]
    return to_delete, unexpected
