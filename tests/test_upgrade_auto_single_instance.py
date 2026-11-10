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
from nexus.upgrade_ladder.registry import LadderRegistry
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
        # read past the locked byte: Windows refuses a read of it from any other handle
        assert lock_mod.read_holder_text(config_dir / lock_mod.LOCK_NAME) == f"{os.getpid()} pytest"  # a test's take is marked (nexus-iox43)
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
            import os, sys, time
            from pathlib import Path
            from nexus.util.upgrade_auto_lock import acquire_auto_lock
            lock = acquire_auto_lock(Path({str(config_dir)!r}))
            assert not lock.held_by_other
            print("held", os.getpid(), flush=True)
            time.sleep(60)
        """)],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        word, pid = holder.stdout.readline().split()
        assert word == "held"
        assert auto_lock_held() is True, "a live holder in ANOTHER process blocks"
        # The holder's own pid, not Popen's: on Windows a venv's python.exe is a launcher that
        # runs the interpreter as its child, so Popen.pid is the launcher's.
        assert lock_mod.read_holder_text(config_dir / lock_mod.LOCK_NAME) == f"{pid} pytest"  # inherits PYTEST_CURRENT_TEST
        # Across processes, which is the read Windows refused while the text sat on byte 0.
        assert lock_mod.AutoLock(fd=None, held_by_other=True, path=config_dir / lock_mod.LOCK_NAME).holder_pid() == int(pid)
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
    for quiet in ("_migrate_repos_json_to_catalog", "_converge_plugins", "_refresh_generated_agents", "_emit_name_vs_embed_dim_advisory", "_refresh_all_git_hooks"):  # the hook refresh reads the repo list from the catalog
        monkeypatch.setattr(upgrade_mod, quiet, lambda **_k: None)
    holder = acquire_auto_lock()
    try:
        result = CliRunner().invoke(main, ["upgrade", "--dry-run"])
    finally:
        holder.release()
    assert result.exit_code == 0
    assert "run_upgrade" in seams, "an interactive `nx upgrade` is not an --auto run and is not blocked by one"


def test_auto_dry_run_is_unattended_for_the_rungs_detect(monkeypatch) -> None:
    """``nx upgrade --auto --dry-run`` walks detect() outside the runner; it is still unattended, so a rung's
    detect() must see the signal there too (interactive ``--dry-run`` does not set it)."""
    from nexus.upgrade_ladder.deadline import Deadline
    from nexus.upgrade_ladder.unattended import is_unattended, unattended_run

    seen: list[bool] = []

    class _Rung:
        name = "probe"

        def detect(self):
            from nexus.upgrade_ladder.protocol import RungStatus

            seen.append(is_unattended())
            return RungStatus(applicable=True, converged=True)

    monkeypatch.setattr("nexus.upgrade_ladder.registry.default_registry", lambda: LadderRegistry((_Rung(),)))
    # upgrade() sets the signal around _run_ladder for --auto; the helper alone does not.
    with unattended_run():
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
        # ValueError, not OSError: on Windows the hook retries a spawn that raises OSError
        # without the job-breakaway flag, so one spawn would record twice. The hook's caller
        # catches ValueError the same way (upgrade_auto.run), so the path is the same.
        raise ValueError("recorded, not spawned")

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



# ── the explicit unattended signal (nexus-75w5d, fix round 1) ────────────────
#
# The lockstep action's detached child is a PLAIN `nx upgrade`: no --auto, no deadline. The first cut keyed
# the managed-service refusal on a bound deadline and the lock on `--auto` in argv, so that child took
# neither: on a cloud box with no labels-rung record it walked the catalog once per version drift per box.

from nexus.upgrade_ladder.rungs.note_tags_array import NoteTagsArrayRung  # noqa: E402
from nexus.upgrade_ladder.unattended import UNATTENDED_ENV, is_unattended, unattended_run  # noqa: E402
from nexus.lockstep_marker import UPGRADE_RETRY_MARKER, running_version  # noqa: E402
from nexus.util.upgrade_auto_lock import argv_is_unattended_upgrade, env_unattended  # noqa: E402
from tests.upgrade.conftest import InMemoryCompletionLedger  # noqa: E402


@pytest.fixture
def plain_upgrade(monkeypatch, config_dir: Path) -> dict[str, object]:
    """``nx upgrade`` with no flag, the ladder real, everything around it stubbed. ``seen`` records what the
    walk saw: whether the run was unattended and whether the single-instance lock was held while it ran."""
    monkeypatch.setattr(sys, "argv", ["nx", "upgrade"])
    monkeypatch.delenv(UNATTENDED_ENV, raising=False)
    monkeypatch.setattr(upgrade_finish, "check_version_transition", lambda *_a, **_k: None)
    monkeypatch.setattr(upgrade_mod, "_run_upgrade", lambda **_k: None)
    monkeypatch.setattr(upgrade_mod, "_cycle_supervised_daemons_to_current", lambda **_k: None)
    monkeypatch.setattr(upgrade_mod, "_converge_preconditions", lambda **_k: None)
    for quiet in ("_migrate_repos_json_to_catalog", "_converge_plugins", "_refresh_generated_agents", "_emit_name_vs_embed_dim_advisory", "_refresh_all_git_hooks"):  # the hook refresh reads the repo list from the catalog
        monkeypatch.setattr(upgrade_mod, quiet, lambda **_k: None)
    monkeypatch.setattr("nexus.upgrade_ladder.http_store.DeferredLadderLedger", InMemoryCompletionLedger)
    seen: dict[str, object] = {}
    real_ladder = upgrade_mod._run_ladder

    def _watch(**kw):
        seen["unattended"] = is_unattended()
        seen["lock_held"] = auto_lock_held()
        return real_ladder(**kw)

    monkeypatch.setattr(upgrade_mod, "_run_ladder", _watch)
    return seen


class _CountingReader:
    reads: list[str] = []

    def distinct_doc_collections(self):
        type(self).reads.append("collections")
        return []

    def close(self) -> None:
        pass


def _managed_note_tags_registry(monkeypatch) -> None:
    _CountingReader.reads = []
    rung = NoteTagsArrayRung(
        reader_factory=_CountingReader, writer_factory=lambda: None, managed_fn=lambda: True,
        record_fn=lambda: None,  # no completion on file (the default would ask the engine's ledger)
    )
    monkeypatch.setattr("nexus.upgrade_ladder.registry.default_registry", lambda: LadderRegistry((rung,)))


def test_a_plain_upgrade_with_the_env_refuses_the_walk_and_holds_the_lock(plain_upgrade, monkeypatch) -> None:
    """The ship-blocker pin. Managed posture, no completion record, no memo: the note-tags rung would list
    every note of the catalog. With NX_UPGRADE_UNATTENDED=1 and NO --auto, no deadline, it reads NOTHING and
    the run holds the lock while it works. Red when the env is ignored."""
    _managed_note_tags_registry(monkeypatch)
    monkeypatch.setenv(UNATTENDED_ENV, "1")
    result = CliRunner().invoke(main, ["upgrade"])
    assert result.exit_code == 0, result.output
    assert plain_upgrade == {"unattended": True, "lock_held": True}
    assert _CountingReader.reads == [], "zero catalog reads"
    assert auto_lock_held() is False, "released when the command ends"


def test_the_same_plain_upgrade_without_the_env_walks_and_takes_no_lock(plain_upgrade, monkeypatch) -> None:
    """Positive control: an interactive `nx upgrade` keeps the scan and takes no lock."""
    _managed_note_tags_registry(monkeypatch)
    result = CliRunner().invoke(main, ["upgrade"])
    assert result.exit_code == 0, result.output
    assert plain_upgrade == {"unattended": False, "lock_held": False}
    assert _CountingReader.reads, "the interactive run does the first scan"


def test_a_plain_unattended_upgrade_that_outlasts_the_holder_does_not_start_and_says_so(
    plain_upgrade, monkeypatch, config_dir,
) -> None:
    """The lockstep child waits for the lock (the hook starts an `--auto` in the same instant) and, when the
    holder outlasts the wait, exits 75; its detached starter wrote the version marker at spawn, so the run leaves the
    retry sentinel there and the next session's hook starts it again (nexus-emiky). A silent 0 would mark it done."""
    _managed_note_tags_registry(monkeypatch)
    monkeypatch.setenv(UNATTENDED_ENV, "1")
    monkeypatch.setattr(lock_mod, "PLAIN_WAIT_S", 0.2)
    monkeypatch.setattr(lock_mod, "_PLAIN_POLL_S", 0.05)
    marker = config_dir / "cli_lockstep_marker"
    monkeypatch.setenv("NX_LOCKSTEP_MARKER", str(marker))
    marker.write_text(running_version(), encoding="utf-8")  # what the action wrote at spawn (the plugin's pin, which the child runs)
    holder = acquire_auto_lock()
    try:
        result = CliRunner().invoke(main, ["upgrade"])
    finally:
        holder.release()
    assert result.exit_code == 75
    assert "holds the lock" in result.output
    # nexus-emiky: the lost run is retried by the next session, which reads the sentinel as skew.
    assert marker.read_text(encoding="utf-8") == UPGRADE_RETRY_MARKER
    assert plain_upgrade == {}, "the ladder never ran"
    assert _CountingReader.reads == []


@pytest.fixture
def marker_file(config_dir: Path, monkeypatch) -> Path:
    marker = config_dir / "cli_lockstep_marker"
    monkeypatch.setenv("NX_LOCKSTEP_MARKER", str(marker))
    return marker


def test_an_auto_upgrade_that_loses_the_lock_leaves_the_marker_alone(seams: list[str], marker_file: Path) -> None:
    """nexus-emiky review I1. The sentinel belongs to the plain run that lost the lock (exit 75); an `--auto`
    that loses exits 0 without work and has no claim on the marker. Red if the write moves above the
    `--auto` branch, where it would fire at every session start that finds a holder."""
    marker_file.write_text(running_version(), encoding="utf-8")
    holder = acquire_auto_lock()
    try:
        result = CliRunner().invoke(main, ["upgrade", "--auto"])
    finally:
        holder.release()
    assert result.exit_code == 0
    assert marker_file.read_text(encoding="utf-8") == running_version()


def test_a_plain_unattended_upgrade_that_gets_the_lock_leaves_the_version_marker_alone(
    plain_upgrade, monkeypatch, marker_file: Path,
) -> None:
    """The control for the 75 pin: the same invocation that acquires the lock and completes writes no sentinel."""
    _managed_note_tags_registry(monkeypatch)
    monkeypatch.setenv(UNATTENDED_ENV, "1")
    marker_file.write_text(running_version(), encoding="utf-8")
    result = CliRunner().invoke(main, ["upgrade"])
    assert result.exit_code == 0, result.output
    assert marker_file.read_text(encoding="utf-8") == running_version()


def test_a_plain_upgrade_that_completes_settles_a_sentinel_to_the_running_version(
    plain_upgrade, monkeypatch, marker_file: Path,
) -> None:
    """nexus-emiky review: the sentinel is owed work, not a state. An `nx upgrade` that does that work
    (interactive, or the unattended plain run) ends by putting the running version where the sentinel was,
    so the next session does not start it all again."""
    _managed_note_tags_registry(monkeypatch)
    marker_file.write_text(UPGRADE_RETRY_MARKER, encoding="utf-8")
    result = CliRunner().invoke(main, ["upgrade"])
    assert result.exit_code == 0, result.output
    assert marker_file.read_text(encoding="utf-8") == running_version()


def test_an_auto_upgrade_does_not_settle_the_sentinel(plain_upgrade, monkeypatch, marker_file: Path) -> None:
    """`--auto` skips plugin convergence, the agents and hooks refresh and the dimension advisory, which is
    what the sentinel says is owed: finishing it does not discharge the debt."""
    monkeypatch.setattr(sys, "argv", ["nx", "upgrade", "--auto"])
    _managed_note_tags_registry(monkeypatch)
    marker_file.write_text(UPGRADE_RETRY_MARKER, encoding="utf-8")
    result = CliRunner().invoke(main, ["upgrade", "--auto"])
    assert result.exit_code == 0, result.output
    assert marker_file.read_text(encoding="utf-8") == UPGRADE_RETRY_MARKER


def test_a_dry_run_does_not_settle_the_sentinel(plain_upgrade, monkeypatch, marker_file: Path) -> None:
    monkeypatch.setattr(sys, "argv", ["nx", "upgrade", "--dry-run"])
    _managed_note_tags_registry(monkeypatch)
    marker_file.write_text(UPGRADE_RETRY_MARKER, encoding="utf-8")
    result = CliRunner().invoke(main, ["upgrade", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert marker_file.read_text(encoding="utf-8") == UPGRADE_RETRY_MARKER


def test_a_skip_t3_upgrade_does_not_settle_the_sentinel(plain_upgrade, monkeypatch, marker_file: Path) -> None:
    """`--skip-t3` skips the engine install and the advisory the sentinel says are owed (review r2 B1)."""
    monkeypatch.setattr(sys, "argv", ["nx", "upgrade", "--skip-t3"])
    _managed_note_tags_registry(monkeypatch)
    marker_file.write_text(UPGRADE_RETRY_MARKER, encoding="utf-8")
    result = CliRunner().invoke(main, ["upgrade", "--skip-t3"])
    assert result.exit_code == 0, result.output
    assert marker_file.read_text(encoding="utf-8") == UPGRADE_RETRY_MARKER


def test_a_plain_upgrade_that_fails_keeps_the_sentinel(plain_upgrade, monkeypatch, marker_file: Path) -> None:
    def _boom(**_k):
        raise RuntimeError("the walk failed")

    monkeypatch.setattr(upgrade_mod, "_run_ladder", _boom)
    marker_file.write_text(UPGRADE_RETRY_MARKER, encoding="utf-8")
    result = CliRunner().invoke(main, ["upgrade"])
    assert result.exit_code != 0
    assert marker_file.read_text(encoding="utf-8") == UPGRADE_RETRY_MARKER


def test_a_plain_unattended_upgrade_waits_for_the_holder_and_then_runs(plain_upgrade, monkeypatch) -> None:
    """Positive control for the wait: the holder (an `--auto` at the same session start) ends inside the
    window, and the plain run goes on, unattended and holding the lock."""
    import threading

    _managed_note_tags_registry(monkeypatch)
    monkeypatch.setenv(UNATTENDED_ENV, "1")
    monkeypatch.setattr(lock_mod, "PLAIN_WAIT_S", 10.0)
    monkeypatch.setattr(lock_mod, "_PLAIN_POLL_S", 0.05)
    holder = acquire_auto_lock()
    timer = threading.Timer(0.3, holder.release)
    timer.start()
    try:
        result = CliRunner().invoke(main, ["upgrade"])
    finally:
        timer.join()
    assert result.exit_code == 0, result.output
    assert plain_upgrade == {"unattended": True, "lock_held": True}


def test_upgrade_auto_sets_the_signal_around_the_ladder_without_the_env(plain_upgrade, monkeypatch) -> None:
    """`--auto` is unattended by its flag, whatever deadline it builds."""
    monkeypatch.setattr(sys, "argv", ["nx", "upgrade", "--auto"])
    _managed_note_tags_registry(monkeypatch)
    result = CliRunner().invoke(main, ["upgrade", "--auto"])
    assert result.exit_code == 0, result.output
    assert plain_upgrade == {"unattended": True, "lock_held": True}
    assert _CountingReader.reads == []


def test_the_signal_does_not_leak_out_of_a_command(plain_upgrade, monkeypatch) -> None:
    monkeypatch.setenv(UNATTENDED_ENV, "1")
    _managed_note_tags_registry(monkeypatch)
    CliRunner().invoke(main, ["upgrade"])
    assert is_unattended() is False


@pytest.mark.parametrize(
    "argv, env, expected",
    [
        (["upgrade"], {}, False),
        (["upgrade"], {UNATTENDED_ENV: "1"}, True),
        (["upgrade"], {UNATTENDED_ENV: "true"}, True),
        (["upgrade"], {UNATTENDED_ENV: "0"}, False),
        (["upgrade"], {UNATTENDED_ENV: ""}, False),
        (["upgrade", "--auto"], {}, True),
        (["upgrade", "--help"], {UNATTENDED_ENV: "1"}, False),
        (["doctor"], {UNATTENDED_ENV: "1"}, False),
        (["-v", "upgrade", "--dry-run"], {UNATTENDED_ENV: "1"}, True),
    ],
)
def test_which_argv_and_env_take_the_unattended_lock(argv: list[str], env: dict, expected: bool) -> None:
    assert argv_is_unattended_upgrade(argv, env) is expected


def test_env_unattended_reads_only_a_true_value() -> None:
    assert env_unattended({UNATTENDED_ENV: " 1 "}) is True
    assert env_unattended({UNATTENDED_ENV: "no"}) is False
    assert env_unattended({}) is False


def test_unattended_run_nests_and_restores() -> None:
    assert is_unattended() is False
    with unattended_run():
        assert is_unattended() is True
        with unattended_run(False):
            assert is_unattended() is True
    assert is_unattended() is False


# ── the version-transition callout ───────────────────────────────────────────


def test_the_callout_asks_every_rung_detect_as_an_unattended_run_so_a_managed_catalog_is_not_walked(
    monkeypatch,
) -> None:
    """`check_version_transition` reaches `pending_data_rung_callout` in whichever nx command wins the
    one-shot claim, an interactive one included. It wants a cheap 'pending?', so each detect() runs
    unattended: managed + no record = zero catalog reads. Red when the callout does not set the signal."""
    _managed_note_tags_registry(monkeypatch)
    assert upgrade_finish.pending_data_rung_callout() == []
    assert _CountingReader.reads == [], "zero catalog reads on a managed service"
    assert is_unattended() is False, "and the signal is gone after the callout"


def test_the_callout_on_a_local_engine_still_detects(monkeypatch) -> None:
    """Positive control: local, the same rung reads (cheap there) and reports its state."""
    _CountingReader.reads = []
    rung = NoteTagsArrayRung(reader_factory=_CountingReader, writer_factory=lambda: None, managed_fn=lambda: False)
    monkeypatch.setattr("nexus.upgrade_ladder.registry.default_registry", lambda: LadderRegistry((rung,)))
    upgrade_finish.pending_data_rung_callout()
    assert _CountingReader.reads == ["collections"]


# ── B1: the hook's probe ─────────────────────────────────────────────────────


def test_the_probe_retries_once_before_it_says_held(config_dir: Path, monkeypatch) -> None:
    """The probe takes the lock for an instant, so two hooks probing together see each other's grab. A first
    'held' that clears by the retry is not a holder; a holder that stays is."""
    outcomes = iter([True, False])

    def _fake_acquire(config=None):
        held = next(outcomes)
        return lock_mod.AutoLock(fd=None, held_by_other=held, path=lock_mod.lock_path(config))

    monkeypatch.setattr(lock_mod, "acquire_auto_lock", _fake_acquire)
    monkeypatch.setattr(lock_mod, "_PROBE_RETRY_DELAY_S", 0.0)
    assert auto_lock_held() is False, "held once, free on the retry: no holder"

    always = iter([True, True, True])
    monkeypatch.setattr(
        lock_mod, "acquire_auto_lock",
        lambda config=None: lock_mod.AutoLock(fd=None, held_by_other=next(always), path=lock_mod.lock_path(config)),
    )
    assert auto_lock_held() is True
    assert next(always) is True, "exactly two probes were made, not three"


# ── the hook's child carries the signal too ──────────────────────────────────


def test_the_hook_child_env_carries_the_unattended_signal(config_dir: Path, monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr("nexus.util.nx_argv.which_off_cwd", lambda *_a, **_k: "/gen/bin/nx")
    monkeypatch.setattr(upgrade_auto, "_emit", lambda *_a, **_k: None)

    def _record(argv, **kw):
        captured.update(kw)
        captured["argv"] = argv
        raise OSError("recorded, not spawned")

    setattr_in(monkeypatch, "nexus.hooks.upgrade_auto", "subprocess.Popen", _record)
    upgrade_auto.run({"hook_event_name": "SessionStart"})
    assert captured["argv"][-2:] == ["upgrade", "--auto"]
    assert captured["env"][UNATTENDED_ENV] == "1"


def test_the_plain_wait_outlasts_the_auto_ceiling_with_room_for_what_the_ceiling_does_not_bound() -> None:
    """nexus-emiky: an `--auto` holder stops starting new work at its ceiling, then still runs the finish
    pass and the service cycle (about 60 s each). The wait must clear that, or the common loser is a run
    whose holder was merely slow."""
    from nexus.upgrade_ladder.deadline import DEFAULT_AUTO_DEADLINE_S

    assert lock_mod.PLAIN_WAIT_S >= DEFAULT_AUTO_DEADLINE_S + 2 * 60



def test_the_text_sits_behind_the_locked_byte(config_dir: Path) -> None:
    """Byte 0 is the byte msvcrt locks; the pid starts at byte 1, so it reads on Windows too."""
    acquire_auto_lock().release()  # read after the release: Windows refuses byte 0 while held
    raw = (config_dir / lock_mod.LOCK_NAME).read_bytes()
    assert raw[:1] == b" " and raw[1:].split()[0] == str(os.getpid()).encode()


def test_a_held_lock_on_windows_reads_past_the_locked_byte(config_dir: Path, monkeypatch) -> None:
    """A read of byte 0 fails on Windows while the lock is held; the text is read from byte 1."""
    path = config_dir / lock_mod.LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b" 48213")
    real_open = open

    class _LockedAtZero:
        def __init__(self, fh):
            self._fh, self._pos = fh, 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._fh.close()

        def seek(self, offset):
            self._pos = offset
            self._fh.seek(offset)

        def read(self):
            if self._pos == 0:
                raise PermissionError(13, "Permission denied")
            return self._fh.read()

    monkeypatch.setattr(lock_mod, "open", lambda p, mode: _LockedAtZero(real_open(p, mode)), raising=False)
    assert lock_mod.read_holder_text(path) == "48213"



def test_a_windows_spawn_the_job_refuses_retries_once_without_breakaway(monkeypatch) -> None:
    """A job that forbids breakaway refuses CREATE_BREAKAWAY_FROM_JOB; the hook starts the child
    without it, once (review r1 of the Windows lock fix: the retry had no test)."""
    calls: list[int] = []
    child = object()

    def fake_popen(argv, *, creationflags=0, **_kw):
        calls.append(creationflags)
        if creationflags & upgrade_auto._CREATE_BREAKAWAY_FROM_JOB:
            raise OSError("breakaway refused")
        return child

    monkeypatch.setattr(upgrade_auto, "_IS_WINDOWS", True)
    setattr_in(monkeypatch, "nexus.hooks.upgrade_auto", "subprocess.Popen", fake_popen)
    assert upgrade_auto._spawn_detached(["nx", "upgrade", "--auto"]) is child
    base = upgrade_auto._CREATE_NO_WINDOW | upgrade_auto._CREATE_NEW_PROCESS_GROUP
    assert calls == [base | upgrade_auto._CREATE_BREAKAWAY_FROM_JOB, base], "one refused try, one spawn"
