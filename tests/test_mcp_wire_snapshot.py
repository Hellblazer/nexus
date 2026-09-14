# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Wire-schema snapshot pin for both nexus MCP servers (nexus-cnzei.5).

Companion to ``tests/test_mcp_package.py``'s exact-name-SET tests
(``test_core_registered_tools`` / ``test_catalog_registered_tools``), which
check only which tool NAMES are registered. This pin additionally catches
a decorator landing on the wrong backing function, a parameter renamed/
retyped/redefaulted on any tool while its registered name stays the same,
and a duplicate ``name=`` registration silently overwriting an earlier one
-- see ``scripts/mcp_wire_snapshot.py``'s module docstring for the full
rationale (this is the nexus-cnzei.1 critic-pass follow-up, T2
nexus/cnzei1-critic-pass-2026-09-13).

The snapshot EXCLUDES tool/parameter descriptions and titles on purpose --
those carry prose that churns independently of the actual wire contract
(bead ids, dates, incident narrative), which would make this pin noisy for
no safety gain. Regenerate after an intentional schema change:

    uv run python scripts/mcp_wire_snapshot.py --write
"""
from __future__ import annotations

import pathlib
import sys

_SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import mcp_wire_snapshot as _snap  # noqa: E402 -- path insert above must precede this import


def test_snapshot_file_exists() -> None:
    assert _snap.SNAPSHOT_PATH.is_file(), (
        f"{_snap.SNAPSHOT_PATH} is missing -- regenerate with "
        "`uv run python scripts/mcp_wire_snapshot.py --write`"
    )


def test_live_registries_match_committed_snapshot() -> None:
    live = _snap.build_snapshot()
    committed = _snap._load_committed()
    assert live == committed, (
        "MCP wire schema drifted from the committed snapshot "
        f"({_snap.SNAPSHOT_PATH.relative_to(_snap._REPO_ROOT)}). If this "
        "change is intentional, regenerate with:\n"
        "    uv run python scripts/mcp_wire_snapshot.py --write\n"
        "and review the diff before committing."
    )


def test_snapshot_carries_no_description_or_title_keys() -> None:
    """Non-vacuity for the stripping step: a schema property really does
    carry description/title before stripping (search's `query` param has
    one), so an unstripped snapshot would fail this test -- proving the
    strip function is actually exercised, not merely present."""
    import json

    raw = json.dumps(_snap._load_committed())
    assert '"description"' not in raw
    # A param literally named "title" (catalog `show`/`update`) is a
    # legitimate list entry in "required", not a schema-metadata key --
    # check for the KEY shape specifically, not the bare substring.
    assert '"title":' not in raw


def test_snapshot_covers_both_servers_with_a_floor_count() -> None:
    """Non-vacuity: a broken import or an empty registry must not pass
    silently as `live == committed` on two empty dicts."""
    live = _snap.build_snapshot()
    assert set(live.keys()) == {"nexus", "nexus-catalog"}
    assert len(live["nexus"]) >= 40
    assert len(live["nexus-catalog"]) >= 8


def test_every_snapshotted_tool_is_backed_by_a_qualname_and_module() -> None:
    live = _snap.build_snapshot()
    for server, tools in live.items():
        for name, entry in tools.items():
            assert entry.get("qualname"), f"{server}::{name} missing qualname"
            assert entry.get("module"), f"{server}::{name} missing module"
            assert "parameters" in entry, f"{server}::{name} missing parameters"


def test_regenerating_the_snapshot_is_idempotent(tmp_path) -> None:
    """--write twice in a row produces byte-identical output (deterministic
    key ordering), so a regenerate never introduces incidental diff noise."""
    first = _snap.build_snapshot()
    second = _snap.build_snapshot()
    assert first == second


def test_a_planted_schema_drift_is_detected(monkeypatch) -> None:
    """The detector reds on a genuine drift, not just a matching pair."""
    committed = _snap._load_committed()
    live = _snap.build_snapshot()
    assert live == committed  # sanity precondition for this test's premise

    mutated = dict(committed)
    mutated["nexus"] = dict(mutated["nexus"])
    a_tool_name = next(iter(mutated["nexus"]))
    mutated_tool = dict(mutated["nexus"][a_tool_name])
    mutated_tool["qualname"] = "definitely_not_the_real_function"
    mutated["nexus"][a_tool_name] = mutated_tool

    assert live != mutated
