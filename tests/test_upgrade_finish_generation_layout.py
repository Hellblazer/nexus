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
    uv_tools = tmp_path / ".local" / "share" / "uv" / "tools"
    legacy = uv_tools / "conexus"
    # nexus-orhp5: point uv's tool root AT this fixture. The gate now does a
    # containment check against install_layout.uv_tool_root() rather than a
    # substring match on "uv/tools/conexus", so a synthetic path that merely
    # SPELLS those segments under tmp_path is no longer inside the uv tree —
    # correctly, since the substring answered yes for decoys and no for the
    # real tree under a relocated UV_TOOL_DIR. The scenario this test means is
    # a genuine legacy install, so the fixture has to be one.
    monkeypatch.setenv("UV_TOOL_DIR", str(uv_tools))
    legacy.mkdir(parents=True)
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


# --------------------------------------------------------------------------
# (d) the VERDICT -- nexus-ycw67
#
# .10 moved the ENUMERATION half onto the layout and left the verdict on
# ``started < install_mtime``. Under generations that is wrong in a
# direction: a process bound to an OLD generation but started AFTER the
# current one was installed reads FRESH, because its start time is newer
# than current's dist-info mtime. That is the shim-bypass shape exactly.
#
# Every test here pins BOTH rows of the bead's own probe -- the false
# negative and an age-stale control -- so an empty verdict is a real answer
# and never a parse miss.
# --------------------------------------------------------------------------

def _layout(tmp_path, monkeypatch, *, stamps=("20260101T000000Z", "20260826T010000Z")):
    """Two generations with ``current`` on the LAST stamp."""
    from nexus import install_layout  # noqa: PLC0415 — file pattern: deferred imports

    tools = tmp_path / "tools"
    tools.mkdir()
    generations = [_fake_generation(tools, s) for s in stamps]
    (tools / install_layout.CURRENT_LINK_NAME).symlink_to(generations[-1])
    monkeypatch.setenv("NX_TOOLS_DIR", str(tools))
    return generations


def _pinned_install(monkeypatch, mtime=1_000.0):
    monkeypatch.setattr(
        upgrade_finish, "install_mtime_and_version",
        lambda: (mtime, "7.18.0"),
    )


def test_an_old_generation_holder_is_stale_even_when_recently_started(
    tmp_path, monkeypatch
) -> None:
    """THE false negative, reproduced from nexus-ycw67's probe.

    pid 4242 is bound to gen-00 and started 500s ago; the install mtime is
    1000 and now is 2000, so it started at 1500 -- AFTER the current
    generation was installed. The age rule calls that fresh. It is running
    gen-00's code.

    pid 4243 is the control: same generation, old enough to be stale under
    the age rule too. If the probe ever stops seeing rows at all, the control
    disappears with the finding and the test fails loudly instead of
    reporting a comfortable empty verdict."""
    old, _new = _layout(tmp_path, monkeypatch)
    _pinned_install(monkeypatch)

    ps_output = (
        "  PID ELAPSED COMMAND\n"
        f"  4242    08:20 {old}/bin/python {old}/bin/nx-mcp\n"
        f"  4243    40:00 {old}/bin/python {old}/bin/nx-mcp\n"
    )
    report = upgrade_finish.detect_stale_processes(ps_output, now=2_000.0)

    assert {p.pid for p in report.stale} == {4242, 4243}, (
        "a holder of a non-current generation is running old code whatever "
        "its start time says; deciding by age misses exactly the "
        "shim-bypass case (stale wrapper, PATH entry into a generation, "
        "absolute generation path in a plist)"
    )


def test_a_current_generation_holder_is_fresh_even_when_old(
    tmp_path, monkeypatch
) -> None:
    """The other direction, and it is deliberate.

    A flip NEVER touches an existing tree, so a holder of ``current`` is
    running current code by construction no matter how long it has been up.
    The age comparison was only ever a proxy for identity, and under
    side-by-side the proxy reports a process stale for having outlived a
    timestamp that says nothing about the tree it is executing."""
    _old, new = _layout(tmp_path, monkeypatch)
    _pinned_install(monkeypatch)

    ps_output = (
        "  PID ELAPSED COMMAND\n"
        f"  4244    40:00 {new}/bin/python {new}/bin/nx-mcp\n"
    )
    report = upgrade_finish.detect_stale_processes(ps_output, now=2_000.0)

    assert report.stale == []
    # Non-vacuity: the row must have been ENUMERATED, or "no stale processes"
    # is the same shape as "looked at nothing".
    assert [pid for pid, _, _ in upgrade_finish.enumerate_processes(ps_output)] == [4244]


def test_a_legacy_tree_holder_is_stale_on_a_migrated_box(
    tmp_path, monkeypatch
) -> None:
    """.7 registers the legacy uv tree as a ``gen-*`` POINTER, so its holders
    attribute to a generation that is not ``current`` and correctly read
    stale. ``_match_prefix`` resolves one level of symlink because a live
    holder's argv names the real path it exec'd from, never the ledger
    pointer."""
    from nexus import install_layout  # noqa: PLC0415 — file pattern: deferred imports

    tools = tmp_path / "tools"
    tools.mkdir()
    current = _fake_generation(tools, "20260826T010000Z")
    (tools / install_layout.CURRENT_LINK_NAME).symlink_to(current)

    legacy = tmp_path / "uv" / "tools" / "conexus"
    (legacy / "bin").mkdir(parents=True)
    (legacy / "nexus-install.json").write_text("{}")
    (tools / "gen-legacy").symlink_to(legacy)
    monkeypatch.setenv("NX_TOOLS_DIR", str(tools))
    _pinned_install(monkeypatch)

    ps_output = (
        "  PID ELAPSED COMMAND\n"
        f"  4245    08:20 {legacy}/bin/python {legacy}/bin/nx-mcp\n"
    )
    report = upgrade_finish.detect_stale_processes(ps_output, now=2_000.0)

    assert [p.pid for p in report.stale] == [4245]


def test_the_legacy_regime_still_decides_by_age(tmp_path, monkeypatch) -> None:
    """No layout, no identity verdict to make. In-place replacement really
    happens on an un-migrated box and mtime is the only discriminator there
    has ever been -- and .7 leaves boxes in that state until their legacy
    tree has zero holders, so this branch is live in the field."""
    monkeypatch.setenv("NX_TOOLS_DIR", str(tmp_path / "no-such-tools"))
    root = tmp_path / "uv" / "tools" / "conexus"
    site = root / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    monkeypatch.setattr(upgrade_finish, "_install_root", lambda: site)
    _pinned_install(monkeypatch)

    ps_output = (
        "  PID ELAPSED COMMAND\n"
        f"  4246    40:00 {root}/bin/python {root}/bin/nx-mcp\n"   # started 400 BEFORE
        f"  4247    08:20 {root}/bin/python {root}/bin/nx-mcp\n"   # started 500 AFTER
    )
    report = upgrade_finish.detect_stale_processes(ps_output, now=2_000.0)

    assert [p.pid for p in report.stale] == [4246]


def test_the_widened_verdict_still_classifies_what_it_reports(
    tmp_path, monkeypatch
) -> None:
    """``restart_stale`` SIGTERMs what this reports, so widening the verdict
    widens what gets cycled. The newly-caught rows must carry the same kind
    and restartability the old ones did -- a row that reads stale but
    classifies as ``other`` would be reported to a human and never cycled,
    and one misclassified the other way gets an unrequested SIGTERM."""
    old, _new = _layout(tmp_path, monkeypatch)
    _pinned_install(monkeypatch)

    ps_output = (
        "  PID ELAPSED COMMAND\n"
        f"  4248    08:20 {old}/bin/nx daemon aspect-worker start --tenant default\n"
        f"  4249    08:20 {old}/bin/python {old}/bin/nx-mcp\n"
    )
    report = upgrade_finish.detect_stale_processes(ps_output, now=2_000.0)

    kinds = {p.pid: p.kind for p in report.stale}
    assert kinds == {4248: "aspect-worker", 4249: "mcp-host"}
    assert [p.pid for p in report.restartable] == [4248]
    assert [p.pid for p in report.session_bound] == [4249]


# --------------------------------------------------------------------------
# (e) the KILL -- nexus-mjhwk, found by both RG-D reviewers independently
#
# Detecting a stale process and actually restarting it are two different
# claims, and only the first was tested. restart_stale's pre-kill
# pid-recycle re-check still matched the legacy _PROC_MARKERS, so on a
# migrated box every genuinely-stale aspect-worker was enumerated, reported
# by `nx doctor`, and then skipped as "gone or recycled" at the instant of
# signalling. `nx daemon restart-stale` -- the remedy doctor's own row
# recommends -- restarted nothing, and said nothing.
#
# .10's audit called this out as finding F5, a SEPARATE must-fix item with
# its own test. The test is these two.
# --------------------------------------------------------------------------

def test_restart_stale_actually_signals_a_generation_aspect_worker(
    tmp_path, monkeypatch
) -> None:
    """The whole point of the pass. A detection that never leads to a signal
    is the silent no-op this arc exists to remove, one layer further in."""
    import signal  # noqa: PLC0415 — file pattern: deferred imports

    old, _new = _layout(tmp_path, monkeypatch)
    command = (
        f"{old}/bin/python {old}/bin/nx daemon aspect-worker "
        "start --tenant default"
    )
    report = upgrade_finish.SkewReport(
        installed_version="7.18.0", install_mtime=1_000.0
    )
    report.stale.append(upgrade_finish.StaleProcess(
        pid=4250, kind="aspect-worker", command=command, age_s=500,
    ))

    calls: list[tuple[int, int]] = []

    def _kill(pid, sig):
        calls.append((pid, sig))
        if sig == 0:
            raise ProcessLookupError  # drained on the first liveness poll

    monkeypatch.setattr(
        "nexus.upgrade_finish.process_command", lambda pid: command
    )
    monkeypatch.setattr("nexus.upgrade_finish.os.kill", _kill)
    monkeypatch.setattr("nexus.upgrade_finish.time.sleep", lambda _s: None)

    actions = upgrade_finish.restart_stale(report)

    assert calls[0] == (4250, signal.SIGTERM), (
        "no signal was sent: neither legacy marker ('uv/tools/conexus', "
        "'.local/bin/nx') can appear in a shim-launched generation path, so "
        "the pre-kill re-check skipped a live worker as 'gone or recycled'"
    )
    # Wording changed when the branch began actually restarting the worker
    # rather than only draining it. What this test is really pinning is
    # that DETECTION LEADS TO A SIGNAL (asserted on `calls` above); the
    # action line just has to name the worker and not claim a restart it
    # did not perform -- this fixture stubs no respawn, so the honest
    # outcome here is the NEEDS HUMAN line.
    assert any(
        "aspect-worker" in a and ("cycled" in a or "NEEDS HUMAN" in a)
        for a in actions
    ), actions
    assert not any("restarted aspect-worker" in a for a in actions)


def test_the_pid_recycle_guard_still_refuses_a_foreign_command(
    tmp_path, monkeypatch
) -> None:
    """Non-vacuity for the above, and the reason the check exists at all.

    Widening the marker set must not degrade the re-check into "signal
    whatever the report named". The command below deliberately CONTAINS
    'aspect-worker' so the first half of the condition passes and the marker
    half is what has to refuse it -- a pid recycled onto an unrelated process
    between detection and signalling is the TOCTOU this guard was added for
    (review 38b7db3d High-3)."""
    _layout(tmp_path, monkeypatch)
    report = upgrade_finish.SkewReport(
        installed_version="7.18.0", install_mtime=1_000.0
    )
    report.stale.append(upgrade_finish.StaleProcess(
        pid=4251, kind="aspect-worker",
        command="/tools/gen-00/bin/nx daemon aspect-worker start", age_s=500,
    ))

    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "nexus.upgrade_finish.process_command",
        lambda pid: "/usr/bin/vim /notes/aspect-worker.md",
    )
    monkeypatch.setattr(
        "nexus.upgrade_finish.os.kill",
        lambda pid, sig: calls.append((pid, sig)),
    )

    actions = upgrade_finish.restart_stale(report)

    assert calls == []
    assert any("gone or recycled" in a for a in actions)
