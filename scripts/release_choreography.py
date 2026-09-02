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
through the SAME table"): one table cache, one switch, one resolver. A
test that wants BOTH real gated scripts to resolve against an in-memory
corrupted table patches exactly one name here --
``choreography_table`` -- which is how
``tests/scripts/test_release_table_parity.py``'s mutation canary proves
Role 2 can catch a wrong row on the real-function path of either script
(nexus-w2x5x).

``DECISION_PATH`` is the explicit switch (module-level flag, not an env
var): ``"old"`` (the default) leaves every pre-existing inline
print()+return branch in both scripts byte-for-byte unchanged; ``"table"``
routes through :func:`nexus.tables.resolve.resolve`. The parity harness
flips it around one call at a time. P2.6 (nexus-j9z30.16) deletes the
``"old"`` branches from both scripts and this switch with them once parity
is proven green over every (cell, event) pair.

Which branches get an arm: only a branch that itself PRINTS and DECIDES an
exit code gets an explicit ``if use_table_path(): return
emit_choreography(...)`` arm. A DELEGATING branch (one that just returns a
sub-call's own return value, printing nothing itself) is left untouched
in both paths -- the switch cascades automatically, since every
sub-function consults this same flag. The table still carries a row for
each delegating cell (the fixture enumerates it), and the catalog carries
a matching entry that DESCRIBES the delegation; neither is ever printed.

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

from nexus.tables.load import Row, Table, load_table
from nexus.tables.resolve import resolve as _resolve_table

DECISION_PATH: str = "old"


def use_table_path() -> bool:
    return DECISION_PATH == "table"


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
    reduced a sensor value to; every other declared dimension (including the
    table-wide, still guard-unreferenced ``event``/``mode``, and every OTHER
    function's own dimensions) is filled with its domain's first member --
    harmless by construction, since no row outside ``function``'s own match
    group ever examines them, and no row within it guards on a dimension
    this call omits (RDR-201 P2.3's short-circuit-by-omission table
    construction). Mirrors tests/scripts/test_release_table_parity.py's
    ``_assignment_for`` for the real, non-test call sites.
    """
    table = choreography_table()
    assignment: dict[str, str] = {"function": function}
    for key, value in guard.items():
        assignment[f"{function}.{key}"] = value
    for name, dim in table.dimensions.items():
        assignment.setdefault(name, dim.domain[0])
    resolution = _resolve_table(table, assignment)
    if resolution.refusal is not None:
        raise RuntimeError(
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
    a real value in hand; unfilled brackets are left verbatim -- P2.4 wires
    the DECISION, not a production-grade renderer, see release_messages.py's
    own docstring), and return the row's exit code.
    """
    import release_messages as _release_messages  # noqa: PLC0415 — deferred: release_messages imports both gated scripts eagerly at its own top, and they import this module; an eager import here would be circular

    row = resolve_choreography_row(function, guard)
    outcome = row.outcome
    assert isinstance(outcome, dict), (function, row.id, outcome)
    exit_code = int(outcome["exit_code"])
    message = _release_messages.get(row.id)
    for key, value in (substitutions or {}).items():
        message = message.replace(f"[{key}]", value)
    print(message, file=sys.stderr if exit_code != 0 else sys.stdout)
    return exit_code
