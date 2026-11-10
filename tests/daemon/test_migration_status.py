# SPDX-License-Identifier: AGPL-3.0-or-later
"""The supervisor's migration record and what commands say from it
(nexus-mjmjl). Clocks are fakes moved by hand."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from nexus.daemon import migration_status as ms
from nexus.progress import boot_work


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_the_notice_counts_from_when_migrating_began_not_from_the_last_progress(tmp_path: Path) -> None:
    clock = Clock()
    rec = ms.Recorder(tmp_path, clock=clock)
    rec.tick(migrating=True, changeset="vectors-058::gate", pending=3)
    for _ in range(50):  # progress keeps arriving; the run time must keep growing
        clock.now += 5
        rec.tick(migrating=True, changeset="vectors-058::gate", pending=3)
    lines = boot_work.notice(tmp_path, clock=clock)
    assert lines[0] == (
        "The engine is migrating its database schema (at vectors-058::gate, 3 changeset(s) left), "
        "running for 4 m 10 s."
    )
    assert "interrupting this command does not stop it" in lines[1]
    assert ms.ON_STOP in lines[1]


def test_writes_are_throttled_unless_the_changeset_moves(tmp_path: Path) -> None:
    clock = Clock()
    rec = ms.Recorder(tmp_path, clock=clock)
    rec.tick(migrating=True, changeset="a", pending=2)
    first = ms.status_path(tmp_path).read_text(encoding="utf-8")
    clock.now += 1
    rec.tick(migrating=True, changeset="a", pending=2)
    assert ms.status_path(tmp_path).read_text(encoding="utf-8") == first  # within WRITE_EVERY_S, nothing new
    rec.tick(migrating=True, changeset="b", pending=1)
    assert '"at b, 1 changeset(s) left"' not in first and "at b" in ms.status_path(tmp_path).read_text(encoding="utf-8")


def test_leaving_the_migrating_phase_clears_the_record_at_once(tmp_path: Path) -> None:
    clock = Clock()
    rec = ms.Recorder(tmp_path, clock=clock)
    rec.tick(migrating=True, changeset=None, pending=None)
    assert boot_work.notice(tmp_path, clock=clock)
    rec.tick(migrating=False, changeset=None, pending=None)  # POST_MIGRATION
    assert boot_work.notice(tmp_path, clock=clock) == []
    assert not ms.status_path(tmp_path).exists()


def test_a_stale_record_from_a_dead_supervisor_is_ignored(tmp_path: Path) -> None:
    clock = Clock()
    ms.Recorder(tmp_path, clock=clock).tick(migrating=True, changeset=None, pending=None)
    clock.now += ms.STALE_S + 1
    assert boot_work.notice(tmp_path, clock=clock) == []


def test_malformed_records_are_ignored_and_never_raise(tmp_path: Path) -> None:
    path = ms.status_path(tmp_path)
    path.parent.mkdir(parents=True)
    for text in ("{not json", '{"what": "x", "updated": NaN}', '{"what": 3, "updated": 1}', "[]"):
        path.write_text(text, encoding="utf-8")
        assert ms.read(tmp_path) is None
    assert boot_work.render([{"what": "x", "started_at": "garbage", "done": "many"}], now=0) == [
        "The engine is x.",
        "  Commands that need the engine wait until it finishes; interrupting this command does not stop it.",
    ]


def test_a_write_to_an_unwritable_place_never_raises(tmp_path: Path) -> None:
    (tmp_path / "service").write_text("a file where the directory should be", encoding="utf-8")
    ms.Recorder(tmp_path).tick(migrating=True, changeset=None, pending=None)
    assert ms.read(tmp_path) is None


def test_the_engines_boot_work_entries_render_with_progress_and_step() -> None:
    """The /v1/status shape agreed with the engine side (nexus-f3yqd.3)."""
    entry = {
        "id": "pci-reconcile", "what": "building search indexes after an upgrade",
        "started_at": "2027-01-15T08:00:00Z", "done": 3, "total": 11, "unit": "indexes",
        "step": {"what": "building index: loading tuples", "done": 41230, "total": 98000, "unit": "blocks"},
        "safe_to_stop": True,
        "on_stop": "the index in progress is dropped and rebuilt from the start next boot; finished ones are kept",
    }
    now = boot_work._started(entry) + 90
    assert boot_work.render([entry], now=now) == [
        "The engine is building search indexes after an upgrade, running for 1 m 30 s: "
        "3 of 11 indexes.",
        "  now: building index: loading tuples (41,230 of 98,000 blocks)",
        "  Interrupting this command does not stop it. Stopping the service now: the index in progress is "
        "dropped and rebuilt from the start next boot; finished ones are kept.",
    ]


def test_the_announcer_speaks_on_first_sight_then_every_repeat_and_once_when_done() -> None:
    clock = Clock()
    said: list[str] = []
    entries: list[dict] = [ms.entry(changeset="c1", pending=1, started=clock.now, updated=clock.now)]
    ann = boot_work.Announcer(said.append, lambda: list(entries), clock=clock, repeat_s=30)
    ann.poll()
    first = len(said)
    clock.now += 10
    ann.poll()
    assert len(said) == first  # not yet due
    clock.now += 25
    ann.poll()
    assert len(said) == 2 * first and "running for 35 s" in said[first]
    entries.clear()
    ann.poll()
    ann.poll()
    assert said[-1] == "The engine is no longer doing that work." and said.count("The engine is no longer doing that work.") == 1


def test_the_announcer_never_raises() -> None:
    def broken() -> list[dict]:
        raise RuntimeError("unreadable")

    boot_work.Announcer(lambda _s: None, broken).poll()


def test_nx_doctor_says_so_before_it_waits(tmp_path: Path, monkeypatch) -> None:
    """Through the CLI: the line is on stderr before doctor does anything else
    (the run stops right after, on a mode --json does not support)."""
    from nexus.commands.doctor import doctor_cmd

    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    ms.Recorder(tmp_path).tick(migrating=True, changeset="vectors-058", pending=1)
    result = CliRunner().invoke(doctor_cmd, ["--json", "--check-taxonomy"])
    assert "The engine is migrating its database schema (at vectors-058" in result.stderr
    assert "The engine is migrating" not in result.stdout


def test_nx_upgrade_says_so_and_auto_keeps_stdout_quiet(tmp_path: Path, monkeypatch, capsys) -> None:
    from nexus.commands import upgrade

    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    upgrade._say_if_the_engine_is_migrating(auto_mode=False)
    assert capsys.readouterr().out == ""
    ms.Recorder(tmp_path).tick(migrating=True, changeset="vectors-058", pending=1)
    upgrade._say_if_the_engine_is_migrating(auto_mode=False)
    assert capsys.readouterr().out.startswith("The engine is migrating its database schema")
    upgrade._say_if_the_engine_is_migrating(auto_mode=True)
    assert capsys.readouterr().out == ""


def test_nx_inits_lease_wait_says_so_while_it_waits(tmp_path: Path, capsys) -> None:
    """The wait itself announces (the critique's case: a migration that begins
    after the command started). A short real wait over an empty registry."""
    from nexus.commands.init import _poll_service_lease

    ms.Recorder(tmp_path).tick(migrating=True, changeset="vectors-058", pending=2)
    assert _poll_service_lease(tmp_path, timeout=0.6) is None
    err = capsys.readouterr().err
    assert err.startswith("The engine is migrating its database schema (at vectors-058, 2 changeset(s) left)")


def test_a_lease_wait_that_gives_up_mid_migration_says_it_is_still_running(tmp_path: Path) -> None:
    from nexus.commands.daemon import still_migrating_message

    text = still_migrating_message(60.0, tmp_path)
    assert text.startswith("The storage service is still migrating its database after 60s")
    assert "Run this command again once it finishes" in text and str(tmp_path / "logs") in text


CS_030 = "db/changelog/vectors-030-model-tenant-partition-functions.xml::vectors-030-1::nexus-3wh8d.7"


def test_the_per_model_copy_says_its_step_its_estimate_and_not_to_stop(tmp_path: Path) -> None:
    """vectors-030-1 runs inside Liquibase before HTTP binds: the record is the
    only channel. Wording agreed with the engine side."""
    clock = Clock()
    samples = iter([
        ("DO $w3$ BEGIN INSERT INTO nexus.chunks_new ...", 10_000, 4_000),  # first sample: the baseline
        ("DO $w3$ BEGIN INSERT INTO nexus.chunks_new ...", 11_680, 4_000),  # +1680 of 4000: 42%
        ("DO $w3$ ...", 99_000, 4_000),                                     # capped below 100
        ("DO $w6$ BEGIN ...", 99_000, 4_000),                               # past the copy: no estimate
        ("ANALYZE nexus.chunks", 99_000, 4_000),
    ])
    rec = ms.Recorder(tmp_path, clock=clock, statement_probe=lambda: next(samples))
    rec.tick(migrating=True, changeset=CS_030, pending=4)
    lines = boot_work.notice(tmp_path, clock=clock)
    assert lines[0].startswith("The engine is migrating its database schema (at vectors-030-1, 4 changeset(s) left)")
    assert lines[1] == "  now: copying chunks into the per-model tables: about 0% copied (estimate)"
    assert lines[2] == "  Do not stop nexus: stopping restarts this step from the beginning. Your data is safe."
    seen = []
    for _ in range(4):
        clock.now += ms.STEP_EVERY_S
        rec.tick(migrating=True, changeset=CS_030, pending=4)
        seen.append(boot_work.notice(tmp_path, clock=clock)[1])
    assert seen == [
        "  now: copying chunks into the per-model tables: about 42% copied (estimate)",
        "  now: copying chunks into the per-model tables: about 99% copied (estimate)",
        "  now: checking the copy row by row",
        "  now: finishing up",
    ]


def test_another_changeset_gets_no_step_and_the_generic_stop_line(tmp_path: Path) -> None:
    calls: list[int] = []

    def probe():
        calls.append(1)
        return ("CREATE INDEX ...", 1, 1)

    clock = Clock()
    ms.Recorder(tmp_path, clock=clock, statement_probe=probe).tick(
        migrating=True, changeset="db/changelog/vectors-058.xml::vectors-058-1::x", pending=1,
    )
    lines = boot_work.notice(tmp_path, clock=clock)
    assert len(lines) == 2 and "interrupting this command does not stop it" in lines[1]
    assert calls == []  # the probe runs only for the walked changeset


def test_the_probe_output_parses_and_a_bad_line_reads_as_unknown() -> None:
    from nexus.daemon.storage_service_daemon import _parse_migration_probe

    assert _parse_migration_probe("123|45|DO $w3$ a|b\n") == ("DO $w3$ a|b", 123, 45)
    assert _parse_migration_probe("") == (None, None, None)
    assert _parse_migration_probe("x|²|") == (None, None, None)


def test_the_copy_estimate_is_capped_and_refuses_nonsense() -> None:
    assert ms.copy_estimate(150, 100, 100) == 50
    assert ms.copy_estimate(1000, 100, 100) == 99
    assert ms.copy_estimate(50, 100, 100) is None
    assert ms.copy_estimate(150, 100, 0) is None


def test_a_waiting_phase_shows_the_phase_without_numbers() -> None:
    entry = {
        "id": "pci-reconcile", "what": "building search indexes after an upgrade",
        "started_at": "2027-01-15T08:00:00Z", "done": 1, "total": 2, "unit": "indexes",
        "step": {"what": "waiting for writers before build", "done": None, "total": None, "unit": "blocks"},
        "safe_to_stop": True, "on_stop": "x",
    }
    lines = boot_work.render([entry], now=boot_work._started(entry) + 5)
    assert lines[0].endswith(": 1 of 2 indexes.")
    assert lines[1] == "  now: waiting for writers before build"


def test_a_top_level_total_not_yet_planned_shows_the_build_in_progress() -> None:
    entry = {
        "id": "pci-reconcile", "what": "building search indexes", "started_at": "2027-01-15T08:00:00Z",
        "done": 2, "total": None, "unit": "indexes", "step": None, "safe_to_stop": True,
        "on_stop": "the index build in progress is lost; it is rebuilt from the start on a later pass",
    }
    lines = boot_work.render([entry], now=boot_work._started(entry) + 5)
    assert lines[0].endswith(": index 3.")
    assert "safe to stop" not in " ".join(lines).lower()
    assert lines[1].endswith("it is rebuilt from the start on a later pass.")
