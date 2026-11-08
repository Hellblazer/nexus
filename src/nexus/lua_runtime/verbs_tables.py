# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The ``tables.get`` verb: a packaged closed-vocabulary table as plain JSON.

RDR-242 Technical Design 9 and 10; nexus-mdbds.7. ``tables.get(name)`` maps a
name to a packaged table file (``TABLE_FILES``, data), loads it with
``nexus.tables.load.load_packaged_table`` and converts it to dicts, lists and
scalars: no ``FrozenMapping``, tuple or dataclass reaches Lua.

Loading is one unit of work and each row's conversion is another. The verb
checks the run's deadline before the load, after it, and before every row,
and raises ``VerbTimeout`` once it has passed (gate r3 S2). A table whose
JSON would exceed ``VERB_RESULT_CAP_BYTES`` is refused with ``VerbError``
(``encode_result``), never truncated.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from nexus.lua_runtime.verb_types import (
    JSONValue,
    ParamSpec,
    VerbContext,
    VerbError,
    VerbSpec,
    VerbTimeout,
    encode_result,
)
from nexus.tables.load import Row, Table, load_packaged_table

#: name -> packaged table file in ``nexus/tables/``. One line per table; a
#: table not listed here is not reachable from a script.
TABLE_FILES: dict[str, str] = {
    "aspect-validation-outcomes": "aspect-validation-outcomes.toml",
    "audit-tags": "audit-tags.toml",
    "chunk-metadata-keys": "chunk-metadata-keys.toml",
    "llm-surfaces": "llm-surfaces.toml",
    "rdr-lifecycle": "rdr-lifecycle.toml",
    "rdr-research-vocab": "rdr-research-vocab.toml",
    "review-rounds": "review-rounds.toml",
    "target-family-lifecycle": "target-family-lifecycle.toml",
    "target-family-obligation-states": "target-family-obligation-states.toml",
    "target-family-slot-kinds": "target-family-slot-kinds.toml",
    "target-family-witness-kinds": "target-family-witness-kinds.toml",
}


def _plain(value: Any) -> JSONValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    raise VerbError(f"tables.get: cannot convert a {type(value).__name__} to JSON")


def _row(row: Row) -> dict[str, JSONValue]:
    return {
        "id": row.id,
        "match": _plain(row.match),
        "guard": _plain(row.guard),
        "outcome_kind": row.outcome_kind,
        "outcome": _plain(row.outcome),
        "escape": row.escape,
    }


def _check_time(deadline: float, clock: Callable[[], float]) -> None:
    if clock() >= deadline:
        raise VerbTimeout("tables.get: the run's wall-time budget is spent")


def _get(
    name: str,
    *,
    deadline: float,
    ctx: VerbContext,
    load: Callable[[str], Table] = load_packaged_table,
    clock: Callable[[], float] = time.monotonic,
) -> JSONValue:
    """``load`` and ``clock`` are seams for the deadline tests; a run never sets them."""
    resource = TABLE_FILES.get(name)
    if resource is None:
        raise VerbError(
            f"tables.get: unknown table {name!r}; known tables: "
            + ", ".join(sorted(TABLE_FILES))
        )
    _check_time(deadline, clock)
    table = load(resource)
    _check_time(deadline, clock)
    rows: list[JSONValue] = []
    for row in table.rows:
        _check_time(deadline, clock)
        rows.append(_row(row))
    result: dict[str, JSONValue] = {
        "id": table.id,
        "kind": table.kind,
        "dimensions": {
            d.name: {"domain": list(d.domain), "kind": d.kind}
            for d in table.dimensions.values()
        },
        "match_keys": list(table.match_keys),
        "rows": rows,
        "impossible": [_plain(p) for p in table.impossible],
    }
    encode_result(result)  # raises VerbError past the cap; the table is not cut
    return result


VERBS: tuple[VerbSpec, ...] = (
    VerbSpec("tables.get", (ParamSpec("name", "string"),), _get),
)
