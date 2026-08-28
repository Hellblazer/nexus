# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The holder census counts the legacy uv tree. nexus-k52g0.

``generation_match_pairs`` enumerated ``list_generations()``, which requires
a receipt. The legacy ``uv tool install`` tree is permanently receipt-less
(``legacy.sh``), so the ledger pointer ``.7`` registers -- the very symlink
``_match_prefix`` was written to resolve -- was filtered out before it got
there, and an UNREGISTERED tree was never in scope at all. Measured
2026-08-27: 9 processes on the 7.19.0 uv tree, ``nx doctor`` reporting
"nothing is still bound to an older generation" and "all match the installed
7.20.0". The instrument that exists to notice a stale box reported clean over
the exact population that made it stale.

The module docstring's own promise -- "a daemon class invented tomorrow is
attributed correctly on the day it ships" -- held for generations and failed
for the one tree that predates them. These pin both structural locations.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus import install_census, install_layout


def _receipted_generation(tools: Path, stamp: str) -> Path:
    gen = tools / f"gen-{stamp}"
    (gen / "bin").mkdir(parents=True)
    (gen / "nexus-install.json").write_text(json.dumps({
        "schema": 1, "version": "7.20.0", "spec": "/src/nexus",
        "source_kind": "directory", "source": "/src/nexus", "extras": [],
        "python": "3.12", "base_interpreter": "/usr/bin",
        "created_at": "2026-08-26T00:00:00Z", "installer_schema": 1,
    }))
    return gen


def _legacy_tree(uv_tools: Path) -> Path:
    legacy = uv_tools / "conexus"
    (legacy / "bin").mkdir(parents=True)
    (legacy / "bin" / "python").write_text("#!/bin/sh\n")
    return legacy


@pytest.fixture
def bed(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    uv_tools = tmp_path / "uv-tools"
    uv_tools.mkdir()
    monkeypatch.setenv("NX_TOOLS_DIR", str(tools))
    monkeypatch.setenv("UV_TOOL_DIR", str(uv_tools))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tools, uv_tools


def _paths(tools: Path) -> list[Path]:
    return [gen for _, gen in install_census.generation_match_pairs(tools=tools)]


def test_an_unregistered_legacy_tree_is_in_the_population(bed) -> None:
    """THE case on the box that found this: no ledger pointer at all."""
    tools, uv_tools = bed
    gen = _receipted_generation(tools, "20260826T010000Z")
    legacy = _legacy_tree(uv_tools)

    pairs = install_census.generation_match_pairs(tools=tools)

    assert set(_paths(tools)) == {gen, legacy}, pairs
    markers = {marker for marker, _ in pairs}
    assert f"{legacy}/" in markers, "no marker would ever match a process on the legacy tree"


def test_a_registered_legacy_tree_is_counted_once(bed) -> None:
    """Registered AND present at uv's root: the two structural routes name one
    tree, and it must appear once -- a double count would double every holder."""
    tools, uv_tools = bed
    _receipted_generation(tools, "20260826T010000Z")
    legacy = _legacy_tree(uv_tools)
    install_layout.legacy_generation_link(tools=tools).symlink_to(legacy)

    paths = _paths(tools)

    assert paths.count(legacy) == 1, paths


def test_a_registered_tree_outside_uvs_root_is_found_via_the_ledger(bed, tmp_path) -> None:
    """The ledger route on its own: a tree registered from a root uv no longer
    points at (UV_TOOL_DIR moved after migration) is still a tree with holders."""
    tools, uv_tools = bed
    elsewhere = tmp_path / "old-uv-root"
    legacy = _legacy_tree(elsewhere)
    install_layout.legacy_generation_link(tools=tools).symlink_to(legacy)

    assert legacy in _paths(tools)


def test_holders_on_the_legacy_tree_are_attributed(bed) -> None:
    """End to end through the one snapshot: a ps line naming the legacy tree
    is a holder of it, exactly as it would be of a generation."""
    tools, uv_tools = bed
    legacy = _legacy_tree(uv_tools)
    snapshot = (
        f"49234 {legacy}/bin/python -m nexus.aspect_worker\n"
        f"71640 /somewhere/else/bin/python -m nexus.mcp\n"
    )

    pids = install_census.generation_holder_pids(legacy, snapshot=snapshot)

    assert pids == [49234]


def test_a_legacy_dir_without_a_bin_is_not_a_tree(bed) -> None:
    tools, uv_tools = bed
    gen = _receipted_generation(tools, "20260826T010000Z")
    (uv_tools / "conexus").mkdir()

    assert _paths(tools) == [gen]


def test_no_legacy_tree_adds_nothing(bed) -> None:
    """Non-vacuity in the other direction: a clean box enumerates only what it has."""
    tools, uv_tools = bed
    gen = _receipted_generation(tools, "20260826T010000Z")

    assert _paths(tools) == [gen]
    assert install_census.legacy_tree_candidates(tools=tools) == []


def test_a_dangling_ledger_pointer_is_ignored(bed, tmp_path) -> None:
    """A pointer whose target was already reaped names nothing runnable."""
    tools, uv_tools = bed
    install_layout.legacy_generation_link(tools=tools).symlink_to(tmp_path / "gone")

    assert install_census.legacy_tree_candidates(tools=tools) == []
