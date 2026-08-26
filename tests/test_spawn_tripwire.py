# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The spawn-time generation tripwire. nexus-utpuw.12 / design point 6.

A new spawn LOGS (never fails) when the generation it is running from is not
the one ``<tools>/current`` points at. One readlink at startup.

WHAT THIS ACTUALLY CATCHES, since a reader who works it out later will
otherwise delete it as dead code: a shim-launched ``nx`` readlinks ``current``
and execs ``<gen>/bin/nx``, so ``sys.prefix == current`` at spawn and the
tripwire is silent BY CONSTRUCTION. It fires exactly when something bypassed
the shim -- a PATH entry pointing straight into a generation, a stale wrapper
script, an absolute generation path baked into a launchd plist or a hook
config. That is the nexus-q3xrx leak shape, and it is otherwise invisible.

Long-lived hosts are the other half of design point 6 and are NOT this
mechanism's job: they start fresh and go stale later, which the per-call
MCP hook catches (``tests/test_stale_host.py``).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from nexus import install_layout, upgrade_finish


def _generation(tools: Path, stamp: str) -> Path:
    gen = tools / f"gen-{stamp}"
    (gen / "bin").mkdir(parents=True)
    (gen / "nexus-install.json").write_text("{}")
    return gen


@pytest.fixture
def layout(tmp_path, monkeypatch):
    """A tools root with two generations; ``current`` is gen-B."""
    tools = tmp_path / "tools"
    tools.mkdir()
    old = _generation(tools, "20260826T010000Z")
    new = _generation(tools, "20260826T020000Z")
    (tools / "current").symlink_to(new)
    monkeypatch.setenv(install_layout.TOOLS_DIR_ENV, str(tools))
    monkeypatch.setattr(upgrade_finish, "_TRIPWIRE_FIRED", False)
    return {"tools": tools, "old": old, "new": new}


@pytest.fixture
def logged(monkeypatch):
    """Capture the tripwire's structured emission."""
    records: list[dict] = []
    monkeypatch.setattr(
        upgrade_finish, "_tripwire_log", lambda **kw: records.append(kw)
    )
    return records


# -- the predicate ----------------------------------------------------------


def test_generation_of_recognises_a_generation(layout) -> None:
    assert upgrade_finish.generation_of(layout["old"]) == layout["old"]


def test_generation_of_rejects_a_path_outside_the_tools_root(
    layout, tmp_path
) -> None:
    """A dev checkout's venv is not a generation, however it is named."""
    stray = tmp_path / "checkout" / "gen-20260826T010000Z"
    stray.mkdir(parents=True)
    assert upgrade_finish.generation_of(stray) is None


def test_generation_of_rejects_a_sibling_that_is_not_gen_prefixed(
    layout
) -> None:
    other = layout["tools"] / "scratch"
    other.mkdir()
    assert upgrade_finish.generation_of(other) is None


def test_generation_of_accepts_a_receipt_less_tree(layout) -> None:
    """Deliberate, and NOT a third definition of "generation".

    Contract 4 ("a gen-* directory CONTAINING a receipt") governs
    ENUMERATION -- what ``list_generations()`` reports and what ``gc.sh``
    may reap, which must agree exactly. This predicate answers a different
    question: is the tree I am RUNNING from part of the side-by-side
    layout. A running tree is side-by-side whether or not its build
    finished writing the receipt, and answering "no" here would fail
    CLOSED -- the silent-no-op shape nexus-utpuw.10 exists to close.
    """
    partial = layout["tools"] / "gen-20260826T030000Z"
    partial.mkdir()
    assert upgrade_finish.generation_of(partial) == partial


def test_running_generation_reads_sys_prefix(layout, monkeypatch) -> None:
    monkeypatch.setattr(sys, "prefix", str(layout["old"]))
    assert upgrade_finish.running_generation() == layout["old"]


def test_running_generation_is_none_from_a_checkout(
    layout, monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "checkout" / ".venv"))
    assert upgrade_finish.running_generation() is None


# -- the tripwire -----------------------------------------------------------


def test_silent_when_the_running_generation_is_current(
    layout, logged, monkeypatch
) -> None:
    monkeypatch.setattr(sys, "prefix", str(layout["new"]))
    upgrade_finish.spawn_tripwire()
    assert logged == [], "a shim-launched spawn must say nothing"


def test_logs_when_the_running_generation_differs(
    layout, logged, monkeypatch
) -> None:
    monkeypatch.setattr(sys, "prefix", str(layout["old"]))
    upgrade_finish.spawn_tripwire()
    assert len(logged) == 1
    assert logged[0]["running"] == str(layout["old"])
    assert logged[0]["current"] == str(layout["new"])


def test_logs_exactly_once_per_process(layout, logged, monkeypatch) -> None:
    """Not "at least once" -- a per-invocation line on a long-lived host is
    log spam, and design point 6 says once."""
    monkeypatch.setattr(sys, "prefix", str(layout["old"]))
    upgrade_finish.spawn_tripwire()
    upgrade_finish.spawn_tripwire()
    upgrade_finish.spawn_tripwire()
    assert len(logged) == 1


def test_the_message_names_both_causes_and_promises_neither(
    layout, logged, monkeypatch
) -> None:
    """nexus-utpuw.12 asks the line to state plainly that a skewed holder is
    CONSISTENT -- its tree is intact -- so it must not read as an error.

    It must ALSO not read as self-healing. Design point 6's phrasing
    ("converges at next spawn") describes a LONG-LIVED holder that went
    stale under a flip; this function fires on a process that has only just
    bound, where the dominant cause is a launcher resolving a generation
    path outside the shim -- which recurs identically every spawn. One
    observation cannot separate that from a flip landing mid-startup, so the
    line names both. An earlier draft asserted convergence unconditionally
    and this test pinned the false wording in place (substantive-critic,
    2026-08-26).
    """
    monkeypatch.setattr(sys, "prefix", str(layout["old"]))
    upgrade_finish.spawn_tripwire()
    detail = logged[0]["detail"].lower()
    assert "intact" in detail, "the consistency the bead asks for is unstated"
    assert "next spawn" in detail, "the transient cause is unnamed"
    assert "every spawn" in detail, "the RECURRING cause is unnamed"
    assert "shim" in detail, "nothing tells the reader what to go fix"


def test_silent_from_a_dev_checkout(layout, logged, monkeypatch, tmp_path) -> None:
    """Not this rule's business: a checkout has no generation to compare."""
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "checkout" / ".venv"))
    upgrade_finish.spawn_tripwire()
    assert logged == []


def test_no_current_pointer_is_silent_and_raises_nothing(
    layout, logged, monkeypatch
) -> None:
    """An un-migrated box has no ``current`` at all. ``is_stale`` surfaces
    that as InstallLayoutError rather than a verdict; a spawn must absorb
    it, not die on it."""
    (layout["tools"] / "current").unlink()
    monkeypatch.setattr(sys, "prefix", str(layout["old"]))
    upgrade_finish.spawn_tripwire()
    assert logged == []


def test_a_dangling_current_pointer_logs_rather_than_raising(
    layout, logged, monkeypatch
) -> None:
    """Pointer present, target reaped: it resolves to a path that can never
    equal a live baseline, so it differs. It must report, not raise."""
    import shutil

    shutil.rmtree(layout["new"])
    monkeypatch.setattr(sys, "prefix", str(layout["old"]))
    upgrade_finish.spawn_tripwire()
    assert len(logged) == 1
    assert logged[0]["current"] == str(layout["new"])


def test_never_raises_when_the_layout_is_unreadable(
    layout, logged, monkeypatch
) -> None:
    """A spawn is never failed by its own tripwire."""
    def _boom(*a, **kw):
        raise RuntimeError("layout unreadable")

    monkeypatch.setattr(sys, "prefix", str(layout["old"]))
    monkeypatch.setattr(install_layout, "current_generation", _boom)
    upgrade_finish.spawn_tripwire()
    assert logged == []


def test_a_broken_logger_does_not_fail_the_spawn(layout, monkeypatch) -> None:
    """Mutation guard for the test above: the swallow must cover the EMIT,
    not only the comparison.

    Also pins the ORDER. The once-flag is set AFTER a successful emit, so a
    transient sink failure leaves the notice still owed rather than silently
    consuming it -- setting it first would degrade "logs at most once" into
    "logs zero times", and nothing would say so (code-review-expert,
    2026-08-26).
    """
    attempts: list[int] = []

    def _boom(**kw):
        attempts.append(1)
        raise RuntimeError("logger exploded")

    monkeypatch.setattr(sys, "prefix", str(layout["old"]))
    monkeypatch.setattr(upgrade_finish, "_tripwire_log", _boom)
    upgrade_finish.spawn_tripwire()
    upgrade_finish.spawn_tripwire()
    assert len(attempts) == 2, "a failed emit must not consume the one-shot"


# -- the wiring -------------------------------------------------------------
#
# The tripwire is only worth anything if the entry points actually call it,
# and "I added the line" is precisely the claim this session kept finding
# unfalsified elsewhere. Each entry point gets a pin.


def test_the_cli_entry_fires_the_tripwire(monkeypatch) -> None:
    from click.testing import CliRunner

    from nexus.cli import main

    calls: list[int] = []
    monkeypatch.setattr(upgrade_finish, "spawn_tripwire", lambda: calls.append(1))
    # A subcommand's --help still runs the GROUP callback, which is where the
    # tripwire sits; it does no storage work, so this stays a fast test.
    CliRunner().invoke(main, ["config", "--help"])
    assert calls == [1]


@pytest.mark.parametrize("module_name", ["nexus.mcp.core", "nexus.mcp.catalog"])
def test_the_mcp_entries_fire_the_tripwire(module_name: str) -> None:
    """Source-level pin, deliberately: both ``main()`` bodies start a server
    that never returns, so there is no way to invoke them and observe the
    call. Scoped to the function's own source rather than the file's, so an
    occurrence in an unrelated helper cannot satisfy it.
    """
    import importlib
    import inspect

    main = importlib.import_module(module_name).main
    assert "spawn_tripwire()" in inspect.getsource(main), (
        f"{module_name}.main() does not fire the spawn tripwire"
    )
