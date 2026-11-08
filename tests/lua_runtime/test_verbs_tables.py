# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``tables.get`` (RDR-242 Technical Design 9 and 10; nexus-mdbds.7).

The packaged closed-vocabulary tables, converted to plain JSON. The deadline
tests inject the loader and the clock, so they are deterministic and sleep
nowhere; the real tables are the happy path.
"""

from __future__ import annotations

import importlib.resources
import json
import time

import pytest

from nexus.lua_runtime.verb_types import (
    VERB_RESULT_CAP_BYTES,
    ParamSpec,
    VerbContext,
    VerbError,
    VerbTimeout,
    check_args,
)
from nexus.lua_runtime.verbs_tables import TABLE_FILES, VERBS
from nexus.tables.load import Dimension, Row, Table, load_packaged_table

(_SPEC,) = VERBS
_CTX = VerbContext(repo_root=None)


def _get(name: str, *, budget: float = 30.0):
    args = check_args(_SPEC, [name])
    return _SPEC.fn(*args, deadline=time.monotonic() + budget, ctx=_CTX)


def _is_plain(value: object) -> bool:
    if value is None or isinstance(value, (bool, int, float, str)):
        return True
    if type(value) is list:
        return all(_is_plain(v) for v in value)
    if type(value) is dict:
        return all(type(k) is str and _is_plain(v) for k, v in value.items())
    return False


def _json(value: object) -> object:
    return json.loads(json.dumps(value))


class _Clock:
    """A fake monotonic clock: ``ticks[n]`` for the nth call, then ``after``."""

    def __init__(self, *, ok_calls: int, now: float = 0.0, after: float = 1000.0):
        self.calls = 0
        self._ok, self._now, self._after = ok_calls, now, after

    def __call__(self) -> float:
        self.calls += 1
        return self._now if self.calls <= self._ok else self._after


def _table(rows: int, outcome: str = "x") -> Table:
    dim = Dimension("status", ("a", "b"))
    return Table(
        id="fixture",
        kind="decision-table",
        dimensions={"status": dim},
        match_keys=("status",),
        rows=tuple(
            Row(f"r{i}", {"status": "a"}, {}, "refuse", outcome) for i in range(rows)
        ),
    )


# ── The verb and its name map ───────────────────────────────────────────────


def test_the_param_matches_the_rdr_signature() -> None:
    assert _SPEC.name == "tables.get"
    assert _SPEC.params == (ParamSpec("name", "string"),)


#: A shipped table deliberately NOT reachable from a script. Empty today: a new
#: .toml must be mapped in TABLE_FILES or named here, so it cannot be forgotten.
_EXCLUDED_TABLE_FILES: frozenset[str] = frozenset()


def test_every_shipped_table_is_mapped_or_explicitly_excluded() -> None:
    shipped = {
        p.name
        for p in importlib.resources.files("nexus.tables").iterdir()
        if p.name.endswith(".toml")
    }
    assert shipped, "the sweep found no packaged tables: it examined nothing"
    mapped = set(TABLE_FILES.values())
    assert not (mapped & _EXCLUDED_TABLE_FILES)
    assert shipped == mapped | _EXCLUDED_TABLE_FILES


def test_every_mapped_table_loads() -> None:
    assert "rdr-lifecycle" in TABLE_FILES
    for name in TABLE_FILES:
        assert _get(name)["id"] == load_packaged_table(TABLE_FILES[name]).id


# ── The conversion to plain JSON ────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(TABLE_FILES))
def test_a_table_is_plain_json_and_faithful_to_the_loaded_table(name: str) -> None:
    out = _get(name)
    table = load_packaged_table(TABLE_FILES[name])
    assert _is_plain(out), "no tuple, FrozenMapping or dataclass reaches Lua"
    assert out["id"] == table.id
    assert out["kind"] == table.kind
    assert out["match_keys"] == list(table.match_keys)
    assert out["dimensions"] == {
        d.name: {"domain": list(d.domain), "kind": d.kind}
        for d in table.dimensions.values()
    }
    assert [r["id"] for r in out["rows"]] == [r.id for r in table.rows]
    for got, row in zip(out["rows"], table.rows):
        # json's own round trip is the oracle: tuples become lists
        assert got["match"] == _json(dict(row.match))
        assert got["guard"] == _json(dict(row.guard))
        assert got["outcome_kind"] == row.outcome_kind
        assert got["outcome"] == _json(row.outcome)
        assert got["escape"] is row.escape
    assert out["impossible"] == [_json(dict(p)) for p in table.impossible]


def test_an_unknown_name_is_a_verb_error_naming_the_known_tables() -> None:
    with pytest.raises(VerbError) as exc:
        _get("no-such-table")
    assert "no-such-table" in str(exc.value)
    assert "rdr-lifecycle" in str(exc.value)


# ── The deadline ────────────────────────────────────────────────────────────


def test_a_deadline_already_past_raises_before_the_table_is_loaded() -> None:
    loaded: list[str] = []

    def load(resource: str) -> Table:
        loaded.append(resource)
        return _table(1)

    with pytest.raises(VerbTimeout):
        _SPEC.fn("rdr-lifecycle", deadline=time.monotonic() - 1.0, ctx=_CTX, load=load)
    assert loaded == []


def test_a_load_that_outlasts_the_deadline_raises_after_the_load() -> None:
    clock = _Clock(ok_calls=1)  # the entry check passes; the clock jumps in load

    def slow_load(resource: str) -> Table:
        clock.calls = 10  # the load took the budget
        return _table(0)

    with pytest.raises(VerbTimeout):
        _SPEC.fn("rdr-lifecycle", deadline=50.0, ctx=_CTX, load=slow_load, clock=clock)


def test_the_deadline_is_checked_between_rows() -> None:
    rows = 1000
    clock = _Clock(ok_calls=5)  # the budget runs out five clock reads in

    with pytest.raises(VerbTimeout):
        _SPEC.fn(
            "rdr-lifecycle",
            deadline=50.0,
            ctx=_CTX,
            load=lambda resource: _table(rows),
            clock=clock,
        )
    assert clock.calls < rows, "the conversion stopped early, not after every row"


def test_a_conversion_inside_the_budget_completes() -> None:
    clock = _Clock(ok_calls=10**9)  # the clock never reaches the deadline
    out = _SPEC.fn(
        "rdr-lifecycle", deadline=50.0, ctx=_CTX, load=lambda r: _table(40), clock=clock
    )
    assert len(out["rows"]) == 40
    assert clock.calls >= 40, "a check per row"


# ── The result cap ──────────────────────────────────────────────────────────


def test_a_table_over_the_cap_is_refused_never_truncated() -> None:
    huge = _table(1, outcome="x" * (VERB_RESULT_CAP_BYTES + 1))
    with pytest.raises(VerbError, match="cap"):
        _SPEC.fn(
            "rdr-lifecycle",
            deadline=time.monotonic() + 60,
            ctx=_CTX,
            load=lambda r: huge,
        )


def test_the_shipped_tables_fit_the_cap_with_room_to_spare() -> None:
    for name in TABLE_FILES:
        size = len(json.dumps(_get(name)).encode())
        assert size < VERB_RESULT_CAP_BYTES // 100, (name, size)


def test_tables_get_serves_the_rdr_research_vocabulary() -> None:
    # nexus-qo7ie.33: the research_add procedure reads its vocabulary here.
    out = _get("rdr-research-vocab")
    assert out["dimensions"]["classification"]["domain"] == ["verified", "documented", "assumed"]
    assert out["dimensions"]["method"]["domain"] == ["source_search", "spike", "docs_only"]
