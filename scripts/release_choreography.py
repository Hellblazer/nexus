#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The ONE routed decision path both release gates share (RDR-201 P2.4/P2.5,
nexus-j9z30.14 / nexus-j9z30.15 / nexus-w2x5x).

``scripts/check_engine_release_floor.py`` and
``scripts/check_client_release_precondition.py`` keep their sensors
(subprocess git/gh calls, the HTTP probe, the ledger parse) imperative and
unchanged. What this module owns is the DECISION step they both route
through: reduce a sensor's outcome to a guard assignment, resolve
``docs/tables/release-choreography.toml`` (RDR-201 P2.3, nexus-j9z30.13)
for it, and emit that row's exit code plus the matching entry from
``release_messages.py``'s row-id-keyed catalog.

One module, not a copy per script, is the point (nexus-j9z30.15: "route
through the SAME table"): one table cache, one resolver, one emit. A test
that wants BOTH real gated scripts to resolve against an in-memory
corrupted table patches exactly one name here -- ``choreography_table`` --
which is how ``tests/scripts/test_release_table_parity.py``'s mutation
canary proves the production wiring of either script reads the table
(nexus-w2x5x).

Which branches call :func:`emit_choreography`: every branch that decides
an exit code. A DELEGATING branch (one that just returns a sub-call's own
return value) emits nothing itself -- the sub-function's row is the
decision. The table still carries a row for each delegating cell (the
fixture enumerates it), and the catalog carries a matching entry that
DESCRIBES the delegation; neither is ever printed.

History: P2.4/P2.5 (nexus-j9z30.14/.15) landed this alongside the
pre-table inline print()+return branches behind a ``DECISION_PATH``
switch, proved old == new over every enumerated cell of both scripts
(verdict and full text), and P2.6 (nexus-j9z30.16) deleted the old
branches and the switch. The fixture pins the table now.

stdlib + nexus.tables only; ``release_messages`` is imported lazily inside
:func:`emit_choreography` because that module imports BOTH gated scripts
eagerly at its own top (to build the static-remedy-constant catalog
entries), and those scripts import this one -- an eager import here would
be circular.
"""
from __future__ import annotations

import functools
import pathlib
import sys
from collections.abc import Callable

from nexus.tables.load import Row, Table, load_table
from nexus.tables.resolve import resolve as _resolve_table

#: The keys a row's ``emit`` table may carry. ``nexus.tables.load`` validates
#: only that ``emit`` is a table; a misspelt key (``strem = "stderr"``) would
#: otherwise be ignored silently and the exit-code default would apply.
EMIT_KEYS = frozenset({"exit_code", "message_key", "stream", "advisory"})

#: The one value ``emit.advisory`` may carry: the row is an exit-0 pass
#: that rests on a default or a fallback, and the emit appends the
#: no-bare-green line (nexus-1c7oq, ``nexus.gate_advisory``) after the
#: message so a summary can count it.
ADVISORY_PASSED_BY_DEFAULT = "passed-by-default"


class TableDefect(RuntimeError):
    """The table, the catalog, or a call site's guard is wrong -- a defect in
    the authored decision data, never a runtime condition for a gate to
    handle. RDR-201 § Failure Modes: the consumer refuses to run, exit 2
    (:func:`run_gate`) -- distinct from exit 1 (BLOCKED) and exit 3 (the
    floor script's tracker-not-recorded), so a crash can never be misread
    as a legitimate refusal."""


def run_gate(main: "Callable[[], int]") -> int:
    """Entry-point wrapper for both gated scripts: a :class:`TableDefect`
    raised anywhere under ``main`` is printed and exits 2."""
    try:
        return main()
    except TableDefect as exc:
        print(f"TABLE DEFECT (exit 2): {exc}", file=sys.stderr)
        return 2

CHOREOGRAPHY_TABLE_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "docs" / "tables" / "release-choreography.toml"
)


@functools.lru_cache(maxsize=1)
def choreography_table() -> Table:
    """Loaded once per process -- the table is immutable for the run's life.

    The ONE accessor every table-path emit in either script resolves
    through (via :func:`resolve_choreography_row`); patch THIS name to
    steer both real gated scripts onto an in-memory table without
    touching the file on disk.
    """
    return load_table(CHOREOGRAPHY_TABLE_PATH)


def resolve_choreography_row(function: str, guard: dict[str, str]) -> Row:
    """Resolve one row of the choreography table for ``function``'s match
    group. ``guard`` names only the dimensions THIS call site has actually
    reduced a sensor value to; every OTHER declared dimension (every other
    function's own) is filled with its domain's first member -- harmless by
    construction, since no row outside ``function``'s own match group ever
    examines them, and no row within it guards on a dimension this call
    omits (RDR-201 P2.3's short-circuit-by-omission table construction).
    The ONE place that completion rule lives: the parity harness resolves
    through this function too, never through a copy of it.

    A refusal (no-match / ambiguous-match / unknown-value) is a
    :class:`TableDefect`.
    """
    table = choreography_table()
    assignment: dict[str, str] = {"function": function}
    for key, value in guard.items():
        assignment[f"{function}.{key}"] = value
    for name, dim in table.dimensions.items():
        assignment.setdefault(name, dim.domain[0])
    resolution = _resolve_table(table, assignment)
    if resolution.refusal is not None:
        raise TableDefect(
            f"{function}: docs/tables/release-choreography.toml refused "
            f"assignment {assignment!r} -- {resolution.refusal} "
            f"{dict(resolution.detail)}. This is a table-authoring defect "
            "(RDR-201), not a runtime condition for this script to handle."
        )
    return resolution.row


def emit_choreography(
    function: str, guard: dict[str, str], substitutions: dict[str, str] | None = None,
) -> int:
    """Resolve ``function``'s row for ``guard``, print the catalog message
    (bracket placeholders filled from ``substitutions`` where the caller has
    a real value in hand; an unfilled bracket is left verbatim, and
    tests/scripts/test_release_table_parity.py's Role 2 reds on one) on the
    row's stream, and return the row's exit code.
    """
    import release_messages as _release_messages  # noqa: PLC0415 — deferred: release_messages imports both gated scripts eagerly at its own top, and they import this module; an eager import here would be circular

    row = resolve_choreography_row(function, guard)
    outcome = row.outcome
    assert isinstance(outcome, dict), (function, row.id, outcome)
    unknown_keys = set(outcome) - EMIT_KEYS
    if unknown_keys:
        raise TableDefect(f"row {row.id!r}: unknown emit key(s) {sorted(unknown_keys)}; allowed: {sorted(EMIT_KEYS)}")
    exit_code = int(outcome["exit_code"])
    message = _release_messages.get(row.id)
    for key, value in (substitutions or {}).items():
        message = message.replace(f"[{key}]", value)
    # Stream: stderr for a refusal, stdout for a pass -- unless the row says
    # otherwise. Exactly one row does (main_dispatch::main_bare_tracker_opt_out,
    # an exit-0 NOTE the pre-table code sent to stderr): the P2.6 per-stream
    # probe over all 89 cells found that one divergence, and the P2.4/P2.5
    # full-text parity could not, because it compared stdout+stderr as one.
    stream = outcome.get("stream", "stderr" if exit_code != 0 else "stdout")
    if stream not in ("stdout", "stderr"):
        raise TableDefect(f"row {row.id!r}: emit.stream must be stdout or stderr, got {stream!r}")
    out = sys.stderr if stream == "stderr" else sys.stdout
    print(message, file=out)
    advisory = outcome.get("advisory")
    if advisory is not None:
        if advisory != ADVISORY_PASSED_BY_DEFAULT:
            raise TableDefect(f"row {row.id!r}: emit.advisory must be {ADVISORY_PASSED_BY_DEFAULT!r}, got {advisory!r}")
        if exit_code != 0:
            raise TableDefect(f"row {row.id!r}: emit.advisory on a non-zero exit is a contradiction")
        from nexus.gate_advisory import passed_by_default  # noqa: PLC0415 — keeps this module importable without the package on sys.path for the table-only callers

        print(passed_by_default(function, f"{row.id}: an exit-0 pass on a declared default"), file=out)
    return exit_code
