# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""`nx doctor` tells the truth about a legacy uv tree. nexus-hibpr / nexus-k52g0.

Two rows, two corrections:

* "Orphan uv install" rendered a REGISTERED tree (in gc.sh's ledger, reaped by
  the next `nx self install` once free) identically to an UNREGISTERED one
  (never reaped by anything). Whether the box converges on its own is the
  one fact the row is for.
* "Holders" never asked about the legacy tree at all, so 9 processes bound to
  the 7.19.0 uv tree rendered as "nothing is still bound to an older
  generation" on 2026-08-27.
"""
from __future__ import annotations

from pathlib import Path

from nexus import health, install_census, install_layout

# Fixtures and helpers shared with the layout suite; importing them by name
# is how pytest discovers the fixture in this module.
from tests.test_health_generation_layout import _generation, _result, layout  # noqa: F401


def _legacy_tree(uv_tools: Path) -> Path:
    legacy = uv_tools / "conexus"
    (legacy / "bin").mkdir(parents=True)
    (legacy / "bin" / "python").write_text("#!/bin/sh\n")
    (legacy / "bin" / "nx").write_text("#!/bin/sh\n")
    return legacy


def _healthy(layout):
    tools, bin_dir = layout
    gen = _generation(tools, "20260826T010000Z")
    (tools / "current").symlink_to(gen)
    (bin_dir / "nx").write_text("#!/bin/sh\n")
    return tools, gen


def test_an_unregistered_tree_says_nothing_will_reap_it(layout, tmp_path, monkeypatch) -> None:
    tools, _ = _healthy(layout)
    uv_tools = tmp_path / "uv-tools"
    legacy = _legacy_tree(uv_tools)
    monkeypatch.setenv("UV_TOOL_DIR", str(uv_tools))

    row = _result(health._check_generation_layout(), "uv install")

    assert row.ok is False
    assert "NOT in the generation ledger" in row.detail, row.detail
    assert str(legacy) in row.detail
    assert any("nx self install" in s for s in row.fix_suggestions), row.fix_suggestions


def test_a_registered_tree_says_it_is_reaped_once_free(layout, tmp_path, monkeypatch) -> None:
    tools, _ = _healthy(layout)
    uv_tools = tmp_path / "uv-tools"
    legacy = _legacy_tree(uv_tools)
    monkeypatch.setenv("UV_TOOL_DIR", str(uv_tools))
    install_layout.legacy_generation_link(tools=tools).symlink_to(legacy)

    row = _result(health._check_generation_layout(), "uv install")

    # Still a WARN -- uv's receipt is still valid and a stray upgrade still
    # rebuilds it -- but the reader now knows the box converges by itself.
    assert row.ok is False
    assert "registered for reap" in row.detail, row.detail
    assert "NOT in the generation ledger" not in row.detail


def test_holders_on_the_legacy_tree_are_named(layout, tmp_path, monkeypatch) -> None:
    """THE silent-green regression guard for nexus-k52g0: a process on the
    legacy tree must show up under Holders, with its pid."""
    tools, _ = _healthy(layout)
    uv_tools = tmp_path / "uv-tools"
    legacy = _legacy_tree(uv_tools)
    monkeypatch.setenv("UV_TOOL_DIR", str(uv_tools))
    monkeypatch.setattr(
        install_census, "ps_snapshot",
        lambda: f"49234 {legacy}/bin/python -m nexus.aspect_worker\n",
    )

    row = _result(health._check_generation_layout(), "Holders")

    assert row.ok is True, "holders are a fact, not a fault"
    assert "legacy uv tree" in row.detail, row.detail
    assert "49234" in row.detail, row.detail
    assert "nothing is still bound" not in row.detail


def test_no_holders_on_the_legacy_tree_stays_green(layout, tmp_path, monkeypatch) -> None:
    """Non-vacuity: a legacy tree with nothing running from it is not a holder."""
    tools, _ = _healthy(layout)
    uv_tools = tmp_path / "uv-tools"
    _legacy_tree(uv_tools)
    monkeypatch.setenv("UV_TOOL_DIR", str(uv_tools))
    monkeypatch.setattr(install_census, "ps_snapshot", lambda: "1 /sbin/launchd\n")

    row = _result(health._check_generation_layout(), "Holders")

    assert row.ok is True
    assert "nothing is still bound" in row.detail, row.detail
