# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""research_add through the real run path (RDR-240 Test Plan, Research add; nexus-qo7ie.33).

These go through ``nexus.lua_runtime.run_script``, which lands with nexus-mdbds.9's wiring
(after the runtime of nexus-mdbds.5). Until then each is an expected failure that still
runs, never a skip: the moment run_script exists they run as ordinary tests. Only the
refusals that touch no store are here, so no test mints a tenant; the store round trip
(written with memory_put, read back byte for byte) is recorded on the bead for the
runtime's own store-backed suite.
"""
from __future__ import annotations

import pytest

import nexus.lua_runtime as lua_runtime
from nexus.lua_runtime.errors import ScriptError

run_script = getattr(lua_runtime, "run_script", None)
pending = pytest.mark.xfail(run_script is None, reason="run_script lands with nexus-mdbds.9", raises=TypeError, strict=True)

ARGS = {"rdr": 240, "classification": "verified", "method": "source_search", "finding": "x"}


def _refusal(args: dict) -> ScriptError:
    with pytest.raises(ScriptError) as e:
        run_script("research_add", args)  # type: ignore[misc]
    return e.value


@pending
@pytest.mark.parametrize("field,value", [("classification", "guessed"), ("method", "intuition")])
def test_a_value_outside_the_vocabulary_is_refused_by_the_script(field: str, value: str) -> None:
    err = _refusal({**ARGS, field: value})
    assert err.kind == "lua" and f"{field} must be one of" in str(err)


@pending
def test_a_missing_argument_is_refused_before_the_script_runs() -> None:
    err = _refusal({k: v for k, v in ARGS.items() if k != "finding"})
    assert err.kind == "args" and "finding" in str(err)


@pending
def test_a_caller_passing_repo_is_refused() -> None:
    err = _refusal({**ARGS, "repo": "elsewhere"})
    assert err.kind == "args" and "reserved" in str(err)
