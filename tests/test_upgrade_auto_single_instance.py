# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-75w5d: ``nx upgrade --auto`` runs one at a time per config dir, and the SessionStart hook
does not fork a child that cannot run.

The incident (2026-10-31, 7.90.0): the hook spawned a detached ``nx upgrade --auto`` per session start
with no ceiling and relied on a rung's lock that is taken inside ``converge()``, after every rung's
``detect()``. Nineteen children walked the cloud catalog at once. What these tests pin:

* the lock is a real OS lock on ``<config>/upgrade-auto.lock``; a holder that dies frees it;
* the ``nx`` root group takes it BEFORE the finish pass (which asks every rung ``detect()``) and
  before the command body; a second ``--auto`` exits 0, silently, having read nothing;
* the finish pass does not ask any rung ``detect()`` for an ``--auto`` run;
* the hook spawns nothing when a live holder exists, when ``NX_UPGRADE_AUTO=0``, or for a subagent.

Storage-free: the command body, the finish pass and the Popen seam are recorded, never run.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

import nexus.commands.upgrade as upgrade_mod
import nexus.upgrade_finish as upgrade_finish
from nexus._hook_runtime._io import HookResult
from nexus.cli import main
from nexus.hooks import upgrade_auto
from nexus.util import upgrade_auto_lock as lock_mod
from nexus.util.upgrade_auto_lock import acquire_auto_lock, argv_is_auto_upgrade, auto_lock_held
from tests._module_seam import setattr_in


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch) -> Path:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
    return cfg


# ── the lock ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["upgrade", "--auto"], True),
        (["-v", "upgrade", "--auto", "--skip-t3"], True),
        (["upgrade"], False),
        (["upgrade", "--dry-run"], False),
        (["upgrade", "--auto", "--help"], False),
        (["doctor", "--auto"], False),
        (["--auto"], False),
        ([], False),
    ],
)
def test_only_an_upgrade_auto_argv_takes_the_lock(argv: list[str], expected: bool) -> None:
    assert argv_is_auto_upgrade(argv) is expected


def test_a_second_acquire_finds_the_lock_held_and_a_release_frees_it(config_dir: Path) -> None:
    first = acquire_auto_lock()
    try:
        assert first.held_by_other is False and first.fd is not None
        assert (config_dir / lock_mod.LOCK_NAME).read_text(encoding="utf-8") == str(os.getpid())
        second = acquire_auto_lock()
        assert second.held_by_other is True and second.fd is None
        assert second.holder_pid() == os.getpid()
        assert auto_lock_held() is True
    finally:
        first.release()
    assert auto_lock_held() is False, "the probe takes and drops the lock: a free lock reads free"
    third = acquire_auto_lock()
    assert third.held_by_other is False
    third.release()


def test_a_killed_holder_does_not_wedge_the_lock(config_dir: Path) -> None:
    """Stale-pid tolerance: the pid in the file is for a human. The OS drops the lock with the process."""
    holder = subprocess.Popen(  # noqa: S603 — argv list, the test's own interpreter
        [sys.executable, "-c", textwrap.dedent(f"""
            import sys, time
            from pathlib import Path
            from nexus.util.upgrade_auto_lock import acquire_auto_lock
            lock = acquire_auto_lock(Path({str(config_dir)!r}))
            assert not lock.held_by_other
            print("held", flush=True)
            time.sleep(60)
        """)],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        assert auto_lock_held() is True, "a live holder in ANOTHER process blocks"
        assert (config_dir / lock_mod.LOCK_NAME).read_text(encoding="utf-8") == str(holder.pid)
    finally:
        holder.kill()
        holder.wait(timeout=10)
    assert auto_lock_held() is False, "the dead holder's pid is still in the file; the lock is free"


def test_an_uncreatable_lock_lets_the_run_go_on(tmp_path: Path, monkeypatch) -> None:
    """A lock problem must never stop the upgrade: the config dir's parent is a file."""
    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(blocker / "cfg"))
    lock = acquire_auto_lock()
    assert lock.held_by_other is False and lock.fd is None
    assert auto_lock_held() is False


# ── the root group ───────────────────────────────────────────────────────────


@pytest.fixture
def seams(monkeypatch, config_dir: Path) -> list[str]:
    """Record the work that must not happen for a second ``--auto``: the finish pass (which asks every
    rung ``detect()``) and the command body. ``sys.argv`` is what the root group reads."""
    calls: list[str] = []
    monkeypatch.setattr(sys, "argv", ["nx", "upgrade", "--auto"])
    monkeypatch.setattr(
        upgrade_finish, "check_version_transition", lambda *_a, **_k: calls.append("finish") or None,
    )
    monkeypatch.setattr(upgrade_mod, "_run_upgrade", lambda **_k: calls.append("run_upgrade"))
    monkeypatch.setattr(upgrade_mod, "_run_ladder", lambda **_k: calls.append("run_ladder"))
    monkeypatch.setattr(upgrade_mod, "_cycle_supervised_daemons_to_current", lambda **_k: None)
    monkeypatch.setattr(upgrade_mod, "_converge_preconditions", lambda **_k: None)
    return calls


def test_a_second_auto_exits_zero_having_done_no_work(seams: list[str]) -> None:
    holder = acquire_auto_lock()
    try:
        result = CliRunner().invoke(main, ["upgrade", "--auto"])
    finally:
        holder.release()
    assert result.exit_code == 0
    assert result.output == "", "silent: this runs from a hook"
    assert seams == [], "not the finish pass (rung detect()), not the command body, not the ladder"


def test_the_first_auto_runs_and_holds_the_lock_until_it_ends(seams: list[str], monkeypatch) -> None:
    """Positive control for the pin above: with no holder the same invocation does its work, and the
    lock is held while the command runs, so a concurrent one would have been refused."""
    seen: dict[str, bool] = {}
    monkeypatch.setattr(
        upgrade_mod, "_run_ladder",
        lambda **_k: (seen.__setitem__("held_during", auto_lock_held()), seams.append("run_ladder")),
    )
    result = CliRunner().invoke(main, ["upgrade", "--auto"])
    assert result.exit_code == 0
    assert seams[0] == "finish" and "run_upgrade" in seams and "run_ladder" in seams
    assert seen["held_during"] is True
    assert auto_lock_held() is False, "released when the command ends"


def test_an_interactive_upgrade_takes_no_lock(seams: list[str], monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["nx", "upgrade", "--dry-run"])
    for quiet in ("_migrate_repos_json_to_catalog", "_converge_plugins", "_refresh_generated_agents", "_emit_name_vs_embed_dim_advisory"):
        monkeypatch.setattr(upgrade_mod, quiet, lambda **_k: None)
    holder = acquire_auto_lock()
    try:
        result = CliRunner().invoke(main, ["upgrade", "--dry-run"])
    finally:
        holder.release()
    assert result.exit_code == 0
    assert "run_upgrade" in seams, "an interactive `nx upgrade` is not an --auto run and is not blocked by one"


def test_auto_dry_run_binds_the_deadline_for_the_rungs_detect(monkeypatch) -> None:
    """``nx upgrade --auto --dry-run`` walks detect() outside the runner; it is still unattended, so a rung's
    detect() must see the auto signal there too (interactive ``--dry-run`` binds none)."""
    from nexus.upgrade_ladder.deadline import Deadline, auto_active

    seen: list[bool] = []

    class _Rung:
        name = "probe"

        def detect(self):
            from nexus.upgrade_ladder.protocol import RungStatus

            seen.append(auto_active())
            return RungStatus(applicable=True, converged=True)

    monkeypatch.setattr("nexus.upgrade_ladder.registry.default_registry", lambda: [_Rung()])
    upgrade_mod._run_ladder(dry_run=True, auto_mode=True, deadline=Deadline(900))
    upgrade_mod._run_ladder(dry_run=True, auto_mode=False, deadline=None)
    assert seen == [True, False]


# ── the finish pass ──────────────────────────────────────────────────────────


def test_upgrade_invocation_reads_the_auto_flag() -> None:
    assert upgrade_finish.upgrade_invocation(["upgrade", "--auto"]).auto is True
    assert upgrade_finish.upgrade_invocation(["upgrade"]).auto is False


@pytest.mark.parametrize("argv, callouts", [(["upgrade", "--auto"], 0), (["upgrade"], 1)])
def test_the_finish_pass_asks_no_rung_detect_for_an_auto_run(
    tmp_path: Path, monkeypatch, argv: list[str], callouts: int,
) -> None:
    """The root group's finish pass calls ``pending_data_rung_callout`` -> every rung's ``detect()`` on a
    version transition, before the command runs. For ``nx upgrade --auto`` its only line would be 'this
    nx upgrade runs it next', so it must not read. The interactive run is the positive control."""
    from unittest.mock import patch

    (tmp_path / "last_seen_version").write_text("6.7.0\n")
    monkeypatch.setattr(sys, "argv", ["nx", *argv])
    with patch("nexus.upgrade_finish.running_from_tool_install", return_value=True), patch(
        "nexus.upgrade_finish.install_mtime_and_version", return_value=(0.0, "6.7.1"),
    ), patch(
        "nexus.upgrade_finish.detect_stale_processes",
        return_value=upgrade_finish.SkewReport(installed_version="6.7.1"),
    ), patch("nexus.upgrade_finish.pending_data_rung_callout", return_value=[]) as callout:
        upgrade_finish.check_version_transition(tmp_path, preview=False)
    assert callout.call_count == callouts


# ── the hook ─────────────────────────────────────────────────────────────────


class _Hook:
    """What the hook did: the Popen calls it made and the hook.log events it emitted."""

    def __init__(self) -> None:
        self.spawned: list[object] = []
        self.events: list[str] = []


@pytest.fixture
def hook(monkeypatch, config_dir: Path) -> _Hook:
    seen = _Hook()
    monkeypatch.setattr("nexus.util.nx_argv.which_off_cwd", lambda *_a, **_k: "/gen/bin/nx")

    def _record(*a, **k):
        seen.spawned.append(a)
        raise OSError("recorded, not spawned")

    setattr_in(monkeypatch, "nexus.hooks.upgrade_auto", "subprocess.Popen", _record)
    monkeypatch.setattr(upgrade_auto, "_emit", lambda level, event, **f: seen.events.append(event))
    return seen


def test_the_hook_spawns_nothing_while_a_live_auto_holds_the_lock(hook: _Hook, capsys) -> None:
    holder = acquire_auto_lock()
    try:
        result = upgrade_auto.run(None)
    finally:
        holder.release()
    assert isinstance(result, HookResult) and result.stdout is None
    assert hook.spawned == [], "a live holder: no fork at all"
    assert capsys.readouterr().err == "", "and no skew guidance: nothing failed"
    assert hook.events == ["upgrade_auto_skipped"]


def test_the_hook_spawns_when_no_holder_exists(hook: _Hook) -> None:
    """Positive control: the same call with the lock free reaches the spawn."""
    upgrade_auto.run(None)
    assert len(hook.spawned) == 1


@pytest.mark.parametrize("value", ["0", "false", "OFF", " no "])
def test_the_kill_switch_stops_the_hook_before_the_spawn(hook: _Hook, monkeypatch, value: str) -> None:
    monkeypatch.setenv("NX_UPGRADE_AUTO", value)
    result = upgrade_auto.run(None)
    assert result.stdout is None
    assert hook.spawned == []
    assert hook.events == ["upgrade_auto_disabled"], "one line says why"


@pytest.mark.parametrize("value", ["1", "", "yes", "true"])
def test_any_other_value_leaves_the_hook_on(hook: _Hook, monkeypatch, value: str) -> None:
    monkeypatch.setenv("NX_UPGRADE_AUTO", value)
    upgrade_auto.run(None)
    assert len(hook.spawned) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"hook_event_name": "SubagentStart", "agent_type": "developer"},
        {"hook_event_name": "SessionStart", "agent_id": "a1b2c3"},
    ],
)
def test_a_subagent_start_spawns_nothing(hook: _Hook, payload: dict) -> None:
    upgrade_auto.run(payload)
    assert hook.spawned == []
    assert hook.events == ["upgrade_auto_skipped"]


def test_a_session_start_payload_still_spawns(hook: _Hook) -> None:
    upgrade_auto.run({"hook_event_name": "SessionStart", "source": "startup", "session_id": "s1"})
    assert len(hook.spawned) == 1


def test_a_burst_of_session_starts_forks_one_child_at_most(config_dir: Path, monkeypatch) -> None:
    """The incident's shape, end to end through the real lock: the first start's child takes the lock (here the
    test stands in for it); every later start in the burst forks nothing."""
    spawned: list[object] = []
    monkeypatch.setattr("nexus.util.nx_argv.which_off_cwd", lambda *_a, **_k: "/gen/bin/nx")
    monkeypatch.setattr(upgrade_auto, "_emit", lambda *_a, **_k: None)

    def _record(*a, **k):
        spawned.append(a)
        raise OSError("recorded, not spawned")

    setattr_in(monkeypatch, "nexus.hooks.upgrade_auto", "subprocess.Popen", _record)
    holder = acquire_auto_lock()  # the first child, running
    try:
        for _ in range(19):
            upgrade_auto.run({"hook_event_name": "SessionStart", "source": "startup"})
    finally:
        holder.release()
    assert spawned == []

