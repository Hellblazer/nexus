# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""upgrade_finish under the generation layout. nexus-utpuw.10.

THREE INDEPENDENT KILLS, and fixing any one alone leaves the pass dead — which
is why each gets its own test rather than one end-to-end assertion that could
pass for the wrong reason.

(a) THE GATE. ``running_from_tool_install()`` is
    ``"uv/tools/conexus" in str(_install_root())``. Under generations the
    install root is ``<tools>/gen-<stamp>/lib/...``, so the gate is False on
    every migrated box — and its ``return None`` sits upstream of restart-stale,
    converge_engine, the diag-view heal, both launchagent unloads, and the
    pending-data-rung callout. The blast radius exceeds the bead's own text.

    The comment directly beneath that gate records nexus-p78a0, where this exact
    coupling — a ``return None`` aborting the WHOLE pass — was fixed one leg
    down for a ps-less box. The gate one leg up still does it, and the
    generation layout pulls that trigger everywhere rather than on odd hosts.

(b) THE MARKERS. ``enumerate_processes`` filters on
    ``_install_root().parents[2]`` — the CURRENT generation. A stale process by
    definition ran from a DIFFERENT one, so it can never match and
    ``report.stale`` is provably always empty even with (a) fixed.

    This is not a typo, it is a design inversion. The old layout kept the path
    CONSTANT across upgrades (in-place swap), so mtime was the only
    discriminator. Generations make the path itself the version, so a filter
    pinned to the current path excludes precisely the processes being looked
    for.

(c) THE CLASSIFIER. ``_classify`` matches bare substrings, so
    ``nx index /papers/mineru-benchmarks/`` is a mineru daemon. The
    aspect-worker TOCTOU re-verify re-checks the SAME substring, so a
    misclassified process survives the one check meant to catch it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus import upgrade_finish


def _fake_generation(tools: Path, stamp: str) -> Path:
    gen = tools / f"gen-{stamp}"
    (gen / "lib" / "python3.12" / "site-packages").mkdir(parents=True)
    (gen / "bin").mkdir(parents=True, exist_ok=True)
    (gen / "nexus-install.json").write_text("{}")
    return gen


# --------------------------------------------------------------------------
# (a) the gate
# --------------------------------------------------------------------------

def test_the_gate_is_true_when_running_from_a_generation(tmp_path, monkeypatch) -> None:
    """The pass must act from a managed install. A generation IS one."""
    tools = tmp_path / "tools"
    tools.mkdir()
    gen = _fake_generation(tools, "20260826T010000Z")
    monkeypatch.setenv("NX_TOOLS_DIR", str(tools))
    monkeypatch.setattr(
        upgrade_finish, "_install_root",
        lambda: gen / "lib" / "python3.12" / "site-packages",
    )

    assert upgrade_finish.running_from_tool_install() is True, (
        "the gate is False for a generation install, so its `return None` "
        "silently disables restart-stale, converge_engine, the diag-view heal "
        "and both launchagent unloads on every migrated box"
    )


def test_the_gate_is_still_true_for_a_legacy_uv_tool_install(tmp_path, monkeypatch) -> None:
    """A box that has not been migrated yet is still a managed install. The
    migration window (.7) is deliberately long -- the legacy tree lingers until
    it has zero holders -- so this must not become a no-op the other way."""
    tools = tmp_path / "tools"
    tools.mkdir()
    monkeypatch.setenv("NX_TOOLS_DIR", str(tools))
    legacy = tmp_path / ".local" / "share" / "uv" / "tools" / "conexus"
    monkeypatch.setattr(
        upgrade_finish, "_install_root",
        lambda: legacy / "lib" / "python3.12" / "site-packages",
    )

    assert upgrade_finish.running_from_tool_install() is True


def test_the_gate_is_false_for_a_dev_checkout(tmp_path, monkeypatch) -> None:
    """The property the gate exists to protect, and the reason it cannot simply
    return True: a dev venv's mtime says nothing about production processes on
    this box, and measuring -- let alone killing -- them from there is the
    cross-venv confusion class."""
    tools = tmp_path / "tools"
    tools.mkdir()
    monkeypatch.setenv("NX_TOOLS_DIR", str(tools))
    checkout = tmp_path / "git" / "nexus" / ".venv"
    monkeypatch.setattr(
        upgrade_finish, "_install_root",
        lambda: checkout / "lib" / "python3.12" / "site-packages",
    )

    assert upgrade_finish.running_from_tool_install() is False


# --------------------------------------------------------------------------
# (b) the markers
# --------------------------------------------------------------------------

def test_a_process_from_an_older_generation_is_enumerated(tmp_path, monkeypatch) -> None:
    """THE kill. A stale process runs from a generation that is not current --
    that is what makes it stale -- so a filter pinned to the current generation
    excludes exactly the processes the pass is looking for."""
    tools = tmp_path / "tools"
    tools.mkdir()
    old = _fake_generation(tools, "20260101T000000Z")
    new = _fake_generation(tools, "20260826T010000Z")
    monkeypatch.setenv("NX_TOOLS_DIR", str(tools))
    monkeypatch.setattr(
        upgrade_finish, "_install_root",
        lambda: new / "lib" / "python3.12" / "site-packages",
    )

    # _parse_ps_table skips line 0 as the header, exactly as real `ps` output
    # has one. A fixture without it loses its only row and the test fails for
    # a reason that has nothing to do with the code under test.
    ps_output = f"  PID ELAPSED COMMAND\n  4242    01:00 {old}/bin/python {old}/bin/nx-mcp\n"
    rows = upgrade_finish.enumerate_processes(ps_output)

    assert [pid for pid, _, _ in rows] == [4242], (
        "a live holder of an OLDER generation was not enumerated, so "
        "report.stale is empty by construction and restart_stale has nothing "
        "to act on -- the pass reports success having examined nothing"
    )


def test_the_current_generation_is_still_enumerated(tmp_path, monkeypatch) -> None:
    """Non-vacuity for the above: widening to every generation must not narrow
    to only the old ones."""
    tools = tmp_path / "tools"
    tools.mkdir()
    new = _fake_generation(tools, "20260826T010000Z")
    monkeypatch.setenv("NX_TOOLS_DIR", str(tools))
    monkeypatch.setattr(
        upgrade_finish, "_install_root",
        lambda: new / "lib" / "python3.12" / "site-packages",
    )

    ps_output = f"  PID ELAPSED COMMAND\n  4243    01:00 {new}/bin/python {new}/bin/nx-mcp\n"
    rows = upgrade_finish.enumerate_processes(ps_output)

    assert [pid for pid, _, _ in rows] == [4243]


# --------------------------------------------------------------------------
# (c) the classifier
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "command,expected",
    [
        ("/g/bin/nx daemon aspect-worker start --tenant default", "aspect-worker"),
        ("/g/bin/mineru-api --port 8899", "mineru"),
        ("/g/bin/python /g/bin/nx-mcp", "mcp-host"),
        ("/g/bin/nx daemon service start --foreground", "service"),
        # The false positives. A path is not a daemon.
        ("/g/bin/nx index /papers/mineru-benchmarks/", "other"),
        ("/g/bin/nx search aspect-worker", "other"),
        ("/usr/bin/vim /notes/mineru.md", "other"),
    ],
)
def test_classify_does_not_mistake_an_argument_for_a_daemon(command, expected) -> None:
    """``_classify`` decides who gets SIGTERMed. The aspect-worker TOCTOU
    re-verify re-checks the SAME substring, so a misclassification survives the
    one check placed there to catch it -- and the mineru branch has no pid
    re-verify at all, running an unrequested 300s stop/start."""
    assert upgrade_finish._classify(command) == expected
