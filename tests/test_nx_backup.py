# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx backup`` (nexus-f3yqd.4): an explicit, user-run dump of the local engine's Postgres.

Sam's rule: NO automatic backup, ever. It is a verb the user runs; ``tests/test_nx_backup_pin.py`` pins that
nothing else imports or runs it.

One test mints no tenant and boots the session engine's Postgres (the round trip, ``ensure_engine``);
everything else needs no engine. The round trip runs the RUNBOOK's own commands: it reads the fenced blocks
out of ``docs/operations/backup-restore.md``, fills in the placeholders, and executes them against the dump
the verb just wrote, so a drift between the runbook and the code that builds the printed commands turns it red.
"""

from __future__ import annotations

import dataclasses
import errno
import contextlib
import os
import re
import shlex
import signal
import socket
import stat
import subprocess
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.commands import backup_cmd
from nexus.config import nexus_config_dir
from nexus.db.pg_provision import PgBinaries
from nexus import _winsec
from nexus.backup import pg_backup
from nexus.backup.pg_backup import (
    LOCK_WAIT_TIMEOUT,
    ONLY_COPY_SENTENCE,
    BackupFailed,
    BackupRefused,
    BackupTarget,
    _quote,
    default_destination,
    free_space_warning,
    resolve_target,
    restore_sections,
    run_dump,
    tool_path,
)
from tests._module_seam import setattr_in
from tests._changelog_entry import changelog_entry

REPO = Path(__file__).resolve().parent.parent
RUNBOOK = REPO / "docs" / "operations" / "backup-restore.md"
CLI_REFERENCE = REPO / "docs" / "cli-reference.md"

#: The tools PgBinaries resolves (initdb, pg_ctl, psql, createdb), read from the class so a fifth is covered
#: the day it is added; named here and not typed out as a list of names, which is not an argv.
_ENGINE_TOOLS = tuple(f.name for f in dataclasses.fields(PgBinaries) if f.name != "bin_dir")

posix_only = pytest.mark.skipif(os.name == "nt", reason="the stand-in pg_dump is a /bin/sh script")


# -- helpers -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The default destination is under HOME: never the real one."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


#: A pg_dump stand-in that writes a dump, and records its argv and environment when NX_TEST_RECORD is set.
_DUMP_OK = """#!/bin/sh
if [ -n "$NX_TEST_RECORD" ]; then printf '%s\\n' "$@" > "$NX_TEST_RECORD.argv"; env > "$NX_TEST_RECORD.env"; fi
while [ $# -gt 0 ]; do if [ "$1" = "-f" ]; then out="$2"; fi; shift; done
printf 'PGDMP stand-in dump' >> "$out"
exit 0
"""
_DUMP_EMPTY = """#!/bin/sh
exit 0
"""
_DUMP_FAILS_AFTER_PARTIAL_OUTPUT = """#!/bin/sh
while [ $# -gt 0 ]; do if [ "$1" = "-f" ]; then out="$2"; fi; shift; done
printf 'half a dump' >> "$out"
echo "pg_dump: error: stand-in failure mid-dump" >&2
exit 1
"""
#: pg_restore stand-ins: -l succeeds, or fails; either records its argv.
_RESTORE_OK = """#!/bin/sh
if [ -n "$NX_TEST_RECORD" ]; then printf '%s\\n' "$@" > "$NX_TEST_RECORD.restore"; fi
exit 0
"""
_RESTORE_FAILS = """#!/bin/sh
echo "pg_restore: error: did not find magic string in file header" >&2
exit 1
"""


def _stub_target(
    tmp_path: Path, dump: str | None = _DUMP_OK, restore: str | None = _RESTORE_OK, **kw: object,
) -> BackupTarget:
    """A target whose pg_dump and pg_restore are stand-in scripts (None: a path that points at nothing)."""
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir(exist_ok=True)
    for name, script in (("pg_dump", dump), ("pg_restore", restore)):
        path = tool_path(bin_dir, name)
        if script is not None:
            path.write_text(script, encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
    target = BackupTarget.from_bin_dir(bin_dir, host="127.0.0.1", port=1, user="u", dbname="d")
    return dataclasses.replace(target, **kw) if kw else target


def _no_siblings(directory: Path, expected: set[str]) -> None:
    assert {p.name for p in directory.iterdir()} == expected, "a partial file was left beside the destination"


def _verb(monkeypatch: pytest.MonkeyPatch, target: BackupTarget, *, size: int | None = None) -> None:
    """Point the verb at *target* (the resolver is the seam), with a known database size."""
    monkeypatch.setattr(backup_cmd, "resolve_target", lambda: target)
    monkeypatch.setattr(backup_cmd, "database_size", lambda t: size)


def _flat(text: str) -> str:
    return " ".join(text.split())


# -- (b) managed mode, and no local engine: refused, nothing written ---------------------------------


@pytest.mark.expects_unresolvable_endpoint  # no token resolves, on purpose; no tenant
def test_managed_mode_takes_the_cloud_arm_not_the_local_refusal(
    cloud_mode: None, monkeypatch: pytest.MonkeyPatch, _home: Path,
) -> None:
    """Managed mode used to be refused ("cloud backups are on the conexus side"); since nexus-csayy.3 it is the
    cloud arm (an export of the caller's tenant), covered by ``tests/test_nx_backup_cloud.py``. Here: with no
    token to read an export with, the verb says so, names how to set one, and writes nothing."""
    monkeypatch.setenv("NX_SERVICE_URL", "http://127.0.0.1:1")
    monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
    result = CliRunner().invoke(main, ["backup"])
    assert result.exit_code != 0
    assert "conexus side" not in result.output and "service_token" in result.output
    assert not (_home / "nexus-backups").exists(), "a refusal must not create the default directory"


def test_no_running_local_engine_is_refused_and_says_how_to_start_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _home: Path,
) -> None:
    with socket.socket() as s:  # a port nothing listens on
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
    cfg = nexus_config_dir()
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "pg_credentials").write_text(f"PG_PORT={dead_port}\n", encoding="utf-8")
    monkeypatch.setenv("NX_LOCAL", "1")
    result = CliRunner().invoke(main, ["backup"])
    assert result.exit_code != 0
    assert "nx daemon service start" in result.output
    assert not (_home / "nexus-backups").exists()


def test_a_box_with_no_local_database_is_refused(monkeypatch: pytest.MonkeyPatch, _home: Path) -> None:
    monkeypatch.setenv("NX_LOCAL", "1")
    result = CliRunner().invoke(main, ["backup"])
    assert result.exit_code != 0
    assert "nx init" in result.output
    assert not (_home / "nexus-backups").exists()


# -- (c) an existing destination is refused, its bytes untouched -------------------------------------


@posix_only
def test_an_existing_destination_is_refused_and_its_bytes_are_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    dest = tmp_path / "out" / "mine.dump"
    dest.parent.mkdir()
    dest.write_bytes(b"precious earlier backup")
    _verb(monkeypatch, _stub_target(tmp_path, _DUMP_FAILS_AFTER_PARTIAL_OUTPUT))  # fails loudly if ever run
    result = CliRunner().invoke(main, ["backup", "--to", str(dest)])
    assert result.exit_code != 0
    assert "already exists" in result.output and "never overwrites" in result.output
    assert "stand-in failure" not in result.output, "the dump must not even start"
    assert dest.read_bytes() == b"precious earlier backup"
    _no_siblings(dest.parent, {"mine.dump"})


_RACE_DUMP = """#!/bin/sh
while [ $# -gt 0 ]; do if [ "$1" = "-f" ]; then out="$2"; fi; shift; done
printf 'my dump' >> "$out"
printf 'someone else got here first' > "$NX_TEST_DEST"
exit 0
"""


@posix_only
def test_a_destination_that_appears_during_the_dump_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check at the start is not enough: someone else can create the file while pg_dump runs."""
    dest = tmp_path / "out" / "raced.dump"
    dest.parent.mkdir()
    monkeypatch.setenv("NX_TEST_DEST", str(dest))
    with pytest.raises(BackupRefused, match="already exists"):
        run_dump(_stub_target(tmp_path, _RACE_DUMP), dest, poll_s=0.01)
    assert dest.read_bytes() == b"someone else got here first"
    _no_siblings(dest.parent, {"raced.dump"})


# -- the publish fallback, for a filesystem without hard links (exFAT, an SMB share) -----------------


def _no_hard_links(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(src: object, dst: object, **kw: object) -> None:
        raise OSError(errno.EPERM, "Operation not permitted")

    setattr_in(monkeypatch, pg_backup, "os.link", refuse)


@posix_only
def test_without_hard_links_the_dump_is_still_published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_hard_links(monkeypatch)
    dest = tmp_path / "out" / "x.dump"
    result = run_dump(_stub_target(tmp_path), dest, poll_s=0.01)
    assert dest.read_bytes() == b"PGDMP stand-in dump" and result.size == len(b"PGDMP stand-in dump")
    _no_siblings(dest.parent, {"x.dump"})


@posix_only
@pytest.mark.parametrize("hard_links", [True, False], ids=["link", "no-hard-links"])
def test_the_dump_and_the_directory_it_creates_are_owner_only(
    hard_links: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The file holds every tenant's data: private from its first byte (nexus._winsec.open_private) on either publish path."""
    if not hard_links:
        _no_hard_links(monkeypatch)
    dest = tmp_path / "newdir" / "x.dump"
    previous = os.umask(0o022)  # a permissive umask must not matter
    try:
        run_dump(_stub_target(tmp_path), dest, poll_s=0.01)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert stat.S_IMODE(dest.parent.stat().st_mode) == 0o700


@posix_only
def test_without_hard_links_a_file_that_appears_is_still_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_hard_links(monkeypatch)
    dest = tmp_path / "out" / "raced.dump"
    dest.parent.mkdir()
    monkeypatch.setenv("NX_TEST_DEST", str(dest))
    with pytest.raises(BackupRefused, match="already exists"):
        run_dump(_stub_target(tmp_path, _RACE_DUMP), dest, poll_s=0.01)
    assert dest.read_bytes() == b"someone else got here first"
    _no_siblings(dest.parent, {"raced.dump"})


@posix_only
def test_without_hard_links_a_file_that_appears_just_before_the_reservation_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window the old check-then-rename left open: the file appears after the check and before the claim.
    The claim is an O_EXCL create, so it is refused; a plain check-then-rename never makes that call."""
    _no_hard_links(monkeypatch)
    dest = tmp_path / "out" / "x.dump"
    dest.parent.mkdir()
    real_open = os.open

    def racing_open(path: object, flags: int, mode: int = 0o777, **kw: object) -> int:
        if Path(os.fspath(path)) == dest:
            dest.write_bytes(b"created in the window")
        return real_open(path, flags, mode, **kw)

    setattr_in(monkeypatch, [pg_backup, _winsec], "os.open", racing_open)  # open_private makes the claim
    with pytest.raises(BackupRefused, match="already exists"):
        run_dump(_stub_target(tmp_path), dest, poll_s=0.01)
    assert dest.read_bytes() == b"created in the window"
    _no_siblings(dest.parent, {"x.dump"})


@posix_only
def test_without_hard_links_a_failed_rename_removes_the_placeholder_it_reserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_hard_links(monkeypatch)

    def fail(src: object, dst: object) -> None:
        raise OSError(errno.EIO, "I/O error")

    setattr_in(monkeypatch, pg_backup, "os.replace", fail)
    dest = tmp_path / "out" / "x.dump"
    with pytest.raises(OSError, match="I/O error"):
        run_dump(_stub_target(tmp_path), dest, poll_s=0.01)
    assert not dest.exists(), "the empty placeholder reserved for the rename was left behind"
    _no_siblings(dest.parent, set())


# -- (d) a failed dump leaves no partial file --------------------------------------------------------


def test_a_missing_pg_dump_binary_leaves_nothing_behind(tmp_path: Path) -> None:
    dest = tmp_path / "out" / "x.dump"
    with pytest.raises(BackupFailed):
        run_dump(_stub_target(tmp_path, None), dest, poll_s=0.01)  # the path points at nothing
    assert not dest.exists()
    _no_siblings(dest.parent, set())


@posix_only
def test_a_dump_that_fails_midway_removes_its_partial_file_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    dest = tmp_path / "out" / "x.dump"
    _verb(monkeypatch, _stub_target(tmp_path, _DUMP_FAILS_AFTER_PARTIAL_OUTPUT), size=1234)
    result = CliRunner().invoke(main, ["backup", "--to", str(dest)])
    assert result.exit_code != 0
    assert "stand-in failure mid-dump" in result.output
    assert not dest.exists()
    _no_siblings(dest.parent, set())


# -- an unwritable or overlong destination is a message, not a traceback -----------------------------


def _unwritable_cases(tmp_path: Path) -> list[Path]:
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory", encoding="utf-8")
    cases = [blocker / "sub" / "x.dump", tmp_path / ("n" * 300 + ".dump")]
    if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() != 0:
        ro = tmp_path / "read-only"
        ro.mkdir()
        ro.chmod(0o500)
        cases.append(ro / "x.dump")
    return cases


def test_an_unwritable_or_overlong_destination_is_a_clean_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _verb(monkeypatch, _stub_target(tmp_path))
    cases = _unwritable_cases(tmp_path)
    assert len(cases) >= 2
    try:
        for dest in cases:
            result = CliRunner().invoke(main, ["backup", "--to", str(dest)])
            assert result.exit_code == 1, (dest, result.output)
            assert "cannot write the backup" in result.output and "--to" in result.output
            assert not isinstance(result.exception, OSError), f"a raw {result.exception!r} escaped for {dest}"
            assert "Traceback" not in result.output
    finally:
        for p in tmp_path.glob("read-only"):
            p.chmod(0o700)


# -- the dump's arguments, the verification, the empty file, the lock timeout, the password ----------


@posix_only
def test_the_dump_is_uncompressed_custom_format_with_a_lock_timeout_and_the_password_only_in_the_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = tmp_path / "rec"
    monkeypatch.setenv("NX_TEST_RECORD", str(record))
    dest = tmp_path / "out" / "x.dump"
    _verb(monkeypatch, _stub_target(tmp_path, password="s3cr3t-pw-9f2c"), size=None)
    result = CliRunner().invoke(main, ["backup", "--to", str(dest)])
    assert result.exit_code == 0, result.output
    argv = Path(f"{record}.argv").read_text().splitlines()
    env = Path(f"{record}.env").read_text()
    assert "-Fc" in argv and "--compress=0" in argv, argv
    assert f"--lock-wait-timeout={LOCK_WAIT_TIMEOUT}" in argv, argv
    assert "PGPASSWORD=s3cr3t-pw-9f2c" in env.splitlines()
    assert not any("s3cr3t-pw-9f2c" in a for a in argv), "the password is on the command line"
    assert "s3cr3t-pw-9f2c" not in result.output, "the password was printed"
    # pg_restore -l was run on the finished file, by the bundled binary's path
    assert Path(f"{record}.restore").read_text().splitlines()[0] == "-l"


@posix_only
def test_a_dump_that_does_not_verify_is_not_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "out" / "x.dump"
    _verb(monkeypatch, _stub_target(tmp_path, restore=_RESTORE_FAILS))
    result = CliRunner().invoke(main, ["backup", "--to", str(dest)])
    assert result.exit_code != 0
    assert "does not verify" in result.output and "magic string" in result.output
    assert not dest.exists()
    _no_siblings(dest.parent, set())


@posix_only
def test_a_dump_that_wrote_nothing_is_a_failure_and_is_not_verified_or_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = tmp_path / "rec"
    monkeypatch.setenv("NX_TEST_RECORD", str(record))
    dest = tmp_path / "out" / "x.dump"
    with pytest.raises(BackupFailed, match="wrote nothing"):
        run_dump(_stub_target(tmp_path, _DUMP_EMPTY), dest, poll_s=0.01)
    assert not dest.exists() and not Path(f"{record}.restore").exists()
    _no_siblings(dest.parent, set())


#: What the real bundled pg_dump 17 prints when --lock-wait-timeout runs out (measured on the test cluster).
_REAL_LOCK_TIMEOUT_STDERR = (
    "pg_dump: error: query failed: ERROR:  canceling statement due to statement timeout\n"
    "pg_dump: detail: Query was: LOCK TABLE public.t IN ACCESS SHARE MODE\n"
)


@posix_only
def test_a_lock_timeout_says_a_migration_probably_holds_the_locks(tmp_path: Path) -> None:
    dest = tmp_path / "out" / "x.dump"
    target = _stub_target(tmp_path, f"""#!/bin/sh
printf '%s' {shlex.quote(_REAL_LOCK_TIMEOUT_STDERR)} >&2
exit 1
""")
    with pytest.raises(BackupFailed) as caught:
        run_dump(target, dest, poll_s=0.01)
    assert "schema migration" in str(caught.value) and LOCK_WAIT_TIMEOUT in str(caught.value)
    assert "statement timeout" in caught.value.detail and "LOCK TABLE" in caught.value.detail
    _no_siblings(dest.parent, set())


@posix_only
@pytest.mark.parametrize("stderr", [
    "pg_dump: error: query failed: ERROR:  canceling statement due to statement timeout\n",  # no LOCK TABLE: not ours
    "pg_dump: error: connection to server failed: FATAL:  password authentication failed\n",
])
def test_other_failures_do_not_get_the_migration_hint(stderr: str, tmp_path: Path) -> None:
    target = _stub_target(tmp_path, f"#!/bin/sh\nprintf '%s' {shlex.quote(stderr)} >&2\nexit 1\n")
    with pytest.raises(BackupFailed) as caught:
        run_dump(target, tmp_path / "out" / "x.dump", poll_s=0.01)
    assert "schema migration" not in str(caught.value)


def test_a_dump_blocked_by_a_lock_gives_up_with_the_hint_on_the_real_pg_dump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real text, not a stand-in: hold ACCESS EXCLUSIVE on a table of a scratch database of the session
    engine's cluster and run the real bundled pg_dump at it with a short lock-wait-timeout."""
    from tests._engine_substrate import ensure_engine  # noqa: PLC0415 - boots the session engine only for this test

    state = ensure_engine()
    base = BackupTarget.from_bin_dir(
        Path(state["pg_bin"]), host="127.0.0.1", port=int(state["pg_port"]), user=state["pg_user"],
        dbname="postgres",
    )
    scratch = f"nxbk_lock_{uuid.uuid4().hex[:8]}"
    _psql(base, "postgres", f"CREATE DATABASE {scratch}")
    target = dataclasses.replace(base, dbname=scratch)
    holder = None
    try:
        _psql(target, scratch, "CREATE TABLE public.t(id int)")
        holder = subprocess.Popen(  # noqa: S603 - holds the lock for a few seconds, then lets go
            [str(target.psql), "-X", "-q", "-h", target.host, "-p", str(target.port), "-U", target.user, "-d", scratch,
             "-c", "BEGIN; LOCK TABLE public.t IN ACCESS EXCLUSIVE MODE; SELECT pg_sleep(8); COMMIT;"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(100):  # wait until the lock is really held
            if _psql(target, scratch, "SELECT count(*) FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
                                         "WHERE c.relname = 't' AND l.mode = 'AccessExclusiveLock' AND l.granted") == "1":
                break
            time.sleep(0.1)
        else:
            pytest.fail("the lock was never taken")
        dest = tmp_path / "out" / "x.dump"
        with pytest.raises(BackupFailed) as caught:
            run_dump(target, dest, poll_s=0.05, lock_wait_timeout="1s")
        assert "schema migration" in str(caught.value) and "1s" in str(caught.value), str(caught.value)
        assert "statement timeout" in caught.value.detail and "LOCK TABLE" in caught.value.detail, caught.value.detail
        _no_siblings(dest.parent, set())
    finally:
        if holder is not None:
            holder.kill()
            holder.wait()
        _psql(base, "postgres", f"DROP DATABASE IF EXISTS {scratch} WITH (FORCE)")


class _RecordingReporter:
    """Stands in for the step reporter: records what the verb tells a step about its progress."""

    notes: list[tuple[float, str]] = []

    def __init__(self, total: int, **kw: object) -> None:
        type(self).notes = []

    def step(self, what: str, detail: str | None = None, **kw: object):  # noqa: ANN201 - a context manager
        import contextlib  # noqa: PLC0415 - one use

        notes = type(self).notes

        class _Step:
            def elapsed(self) -> float:
                return 2.0

            def progress(self, done: float, total: float | None = None, unit: str = "", note: str = "") -> None:
                notes.append((done, f"{total}|{unit}|{note}"))

        @contextlib.contextmanager
        def ctx():  # noqa: ANN202
            yield _Step()

        return ctx()


@posix_only
def test_progress_is_bytes_written_and_a_rate_with_no_fraction_and_no_time_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backup_cmd, "StepReporter", _RecordingReporter)
    _verb(monkeypatch, _stub_target(tmp_path), size=3_800_000_000)
    result = CliRunner().invoke(main, ["backup", "--to", str(tmp_path / "out" / "x.dump")])
    assert result.exit_code == 0, result.output
    done, note = _RecordingReporter.notes[-1]
    assert done == len(b"PGDMP stand-in dump")
    total, unit, text = note.split("|")
    assert total == "None" and unit == "bytes", "a total would invite a fraction of a number that is not the dump's size"
    assert text.startswith("written, ") and text.endswith("/s"), text
    assert not any(w in text for w in ("estimate", "left", "of about")), text


# -- the free-space warning (a warning, never a refusal) ---------------------------------------------


def test_too_little_free_space_warns_and_enough_does_not(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    setattr_in(monkeypatch, pg_backup, "shutil.disk_usage", lambda p: SimpleNamespace(free=5_000_000))
    warning = free_space_warning(tmp_path / "not-yet" / "x.dump", 800_000_000)
    assert warning and "5.0 MB" in warning and "800.0 MB" in warning and "not compressed" in warning
    assert free_space_warning(tmp_path / "x.dump", 1_000_000) is None
    assert free_space_warning(tmp_path / "x.dump", None) is None


@posix_only
def test_the_verb_warns_about_space_and_carries_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    setattr_in(monkeypatch, pg_backup, "shutil.disk_usage", lambda p: SimpleNamespace(free=10))
    dest = tmp_path / "out" / "x.dump"
    _verb(monkeypatch, _stub_target(tmp_path), size=900_000_000)
    result = CliRunner().invoke(main, ["backup", "--to", str(dest)])
    assert result.exit_code == 0 and dest.is_file(), result.output
    assert "Warning:" in result.output and "may not fit" in result.output


# -- what the verb says ------------------------------------------------------------------------------


@posix_only
def test_the_verb_says_where_how_big_the_only_copy_sentence_and_the_runbook_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    dest = tmp_path / "out" / "x.dump"
    _verb(monkeypatch, _stub_target(tmp_path), size=3_800_000_000)
    result = CliRunner().invoke(main, ["backup", "--to", str(dest)])
    assert result.exit_code == 0, result.output
    out = _flat(result.output)
    assert f"Backup written: {dest}" in out
    assert "The database is about 3.8 GB on disk" in out and "indexes are not dumped" in out
    assert ONLY_COPY_SENTENCE in out
    assert "The whole runbook, including the swap and the undo: https://github.com/Hellblazer/nexus/blob/main/docs/operations/backup-restore.md" in out
    assert "estimate" not in out and "left" not in out, "no fraction and no ETA: the total is not the dump's"


def test_the_help_the_reference_the_changelog_and_the_runbook_all_say_there_is_no_automatic_backup() -> None:
    help_text = _flat(CliRunner().invoke(main, ["backup", "--help"]).output)
    assert ONLY_COPY_SENTENCE in help_text and "NOT compressed" in help_text
    for name, text in (
        ("cli-reference", CLI_REFERENCE.read_text(encoding="utf-8")),
        ("runbook", RUNBOOK.read_text(encoding="utf-8")),
        ("changelog", changelog_entry("nexus-f3yqd.4.md", "nexus-f3yqd.4")),
    ):
        assert ONLY_COPY_SENTENCE in _flat(text), f"{name} lacks the only-copy sentence"
    for name, text in (("runbook", RUNBOOK.read_text(encoding="utf-8")), ("changelog", changelog_entry("nexus-f3yqd.4.md", "nexus-f3yqd.4"))):
        assert "dump is compressed" not in text and "an estimate" not in text, f"{name} still says the dump is compressed"


# -- the destination and the resolver ---------------------------------------------------------------


def test_the_default_destination_is_outside_the_engines_data_directory(_home: Path) -> None:
    dest = default_destination(now=datetime(2026, 11, 10, 7, 8, 9, tzinfo=UTC))
    assert dest == _home / "nexus-backups" / "nexus-20261110T070809Z.dump"
    assert nexus_config_dir() not in dest.parents


def _fake_bundle(tmp_path: Path, names: tuple[str, ...]) -> Path:
    bundle_bin = tmp_path / "bundle-bin"
    bundle_bin.mkdir()
    suffix = ".exe" if os.name == "nt" else ""
    for name in names:
        (bundle_bin / f"{name}{suffix}").write_text("", encoding="utf-8")
    return bundle_bin


def test_the_resolver_reads_the_port_and_password_the_engine_runs_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_bin = _fake_bundle(tmp_path, (*_ENGINE_TOOLS, "pg_dump", "pg_restore"))
    monkeypatch.setenv("NEXUS_PG_BIN", str(bundle_bin))
    monkeypatch.setenv("NX_LOCAL", "1")
    cfg = nexus_config_dir()
    cfg.mkdir(parents=True, exist_ok=True)
    with socket.socket() as listener:  # a live "cluster" for the accept probe
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        (cfg / "pg_credentials").write_text(f"PG_PORT={port}\nPG_SUPERUSER_PASS=s3cret\n", encoding="utf-8")
        target = resolve_target()
    assert (target.port, target.password, target.host, target.dbname) == (port, "s3cret", "127.0.0.1", "nexus")
    assert target.pg_dump == tool_path(bundle_bin, "pg_dump")
    assert target.pg_restore == tool_path(bundle_bin, "pg_restore")


def test_a_bundle_without_pg_dump_is_refused_not_found_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_bin = _fake_bundle(tmp_path, _ENGINE_TOOLS)  # the tools the engine needs, no pg_dump
    monkeypatch.setenv("NEXUS_PG_BIN", str(bundle_bin))
    monkeypatch.setenv("NX_LOCAL", "1")
    cfg = nexus_config_dir()
    cfg.mkdir(parents=True, exist_ok=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        (cfg / "pg_credentials").write_text(f"PG_PORT={listener.getsockname()[1]}\n", encoding="utf-8")
        with pytest.raises(BackupRefused, match="pg_dump"):
            resolve_target()


# -- Windows spellings, checked on any platform ------------------------------------------------------


def test_windows_tool_paths_carry_exe_and_posix_ones_do_not() -> None:
    win = BackupTarget.from_bin_dir(Path("C:/pg/bin"), host="h", port=1, user="u", dbname="d", platform="win32")
    assert [p.name for p in (win.pg_dump, win.pg_restore, win.psql)] == ["pg_dump.exe", "pg_restore.exe", "psql.exe"]
    nix = BackupTarget.from_bin_dir(Path("/pg/bin"), host="h", port=1, user="u", dbname="d", platform="linux")
    assert [p.name for p in (nix.pg_dump, nix.pg_restore, nix.psql)] == ["pg_dump", "pg_restore", "psql"]
    assert tool_path(Path("/b"), "pg_dump", "win32").name == "pg_dump.exe"


_AWKWARD = ["/tmp/a b/x.dump", "/tmp/$HOME/x.dump", "/tmp/`id`/x.dump", "/tmp/o'k/x.dump", '/tmp/q"d/x.dump',
            "/tmp/a\\b/x.dump", "/tmp/a!b/x.dump"]


@posix_only
@pytest.mark.parametrize("value", _AWKWARD)
def test_a_posix_path_with_a_special_character_survives_the_shell_unchanged(value: str) -> None:
    assert shlex.split(f"cmd {_quote(value, 'posix')}") == ["cmd", value]
    # and a real shell, which expands $VAR, `cmd` and \ inside double quotes, hands back the same bytes
    shell = subprocess.run(  # noqa: S603 - fixed argv; the quoted value is the thing under test
        ["/bin/sh", "-c", f"printf %s {_quote(value, 'posix')}"], capture_output=True, text=True, check=False,
        env={**os.environ, "HOME": "/expanded-home"},
    )
    assert shell.stdout == value
    line = restore_sections("/pg/bin", 5433, "u", value, platform="posix")["restore"].splitlines()[1]
    assert shlex.split(line)[-1] == value


@pytest.mark.parametrize(("value", "quoted"), [
    ("C:\\Program Files\\x.dump", '"C:\\Program Files\\x.dump"'),
    ("C:\\a$b\\x.dump", "'C:\\a$b\\x.dump'"),
    ("C:\\a`b\\x.dump", "'C:\\a`b\\x.dump'"),
    ("C:\\o'k$x\\x.dump", "'C:\\o''k$x\\x.dump'"),
])
def test_a_windows_path_with_a_special_character_is_single_quoted_with_doubled_quotes(value: str, quoted: str) -> None:
    assert _quote(value, "win32") == quoted
    line = restore_sections("C:\\pg\\bin", 5433, "u", value, platform="win32")["restore"].splitlines()[1]
    assert line.endswith(quoted) and line.startswith('& "C:\\pg\\bin\\pg_restore.exe"')


# -- the runbook is the code's own output ------------------------------------------------------------

_SECTIONS = {"restore", "pg_up", "pg_start", "check", "swap", "undo", "drop_restored", "drop"}


def _doc_blocks() -> dict[tuple[str, str], str]:
    """``<!-- nx-restore-commands: <platform> <section> -->`` then a fenced block, from the runbook."""
    text = RUNBOOK.read_text(encoding="utf-8")
    blocks = {}
    for m in re.finditer(r"<!-- nx-restore-commands: (\w+) (\w+) -->\s*```[^\n]*\n(.*?)```", text, re.DOTALL):
        blocks[(m.group(1), m.group(2))] = m.group(3).strip()
    return blocks


def _template(platform: str) -> dict[str, str]:
    return restore_sections("<pg_bin>", "<port>", "<user>", "<dump>", platform=platform)


@pytest.mark.parametrize("platform", ["posix", "win32"])
def test_the_runbook_blocks_are_exactly_what_the_code_renders(platform: str) -> None:
    """Drift between the runbook and the function that builds the printed commands is a red test."""
    blocks = _doc_blocks()
    template = _template(platform)
    assert set(template) == _SECTIONS
    for section, expected in template.items():
        assert blocks.get((platform, section)) == expected.strip(), f"{platform} {section} block drifted"
    assert {k for k in blocks if k[0] == platform} == {(platform, s) for s in _SECTIONS}


def test_the_commands_name_the_bundled_binary_by_path_never_one_on_the_path() -> None:
    for platform in ("posix", "win32"):
        for section, text in _template(platform).items():
            for line in text.splitlines():
                assert "<pg_bin>" in line, f"{section} calls a tool bare on {platform}: {line}"
                assert not re.search(r"(?<![\w/\\])(psql|createdb|pg_restore|pg_ctl|dropdb)(?:\.exe)?\s", line.split('"')[0]), line


def test_the_pg_start_command_is_the_argv_the_supervisor_uses(tmp_path: Path) -> None:
    """The runbook's way to start Postgres alone is the engine's own pg_ctl start, so it cannot drift from it.
    Rendered and built for the platform the test runs on (the Windows form is the one that once mixed
    separators: a native Windows run found `bin/pg_ctl` against the supervisor's `bin\\pg_ctl.exe`)."""
    from nexus.db import pg_provision as pgp  # noqa: PLC0415 - heavy import, one test

    captured: dict[str, list[str]] = {}

    class _Done:
        def wait(self, timeout: float | None = None) -> int:
            return 0

    def fake_popen(cmd: list[str], **kw: object) -> _Done:
        captured["cmd"] = cmd
        return _Done()

    bin_dir = tmp_path / "bin"
    pgdata = tmp_path / "pgdata"
    pgdata.mkdir()
    bins = pgp.PgBinaries.from_dir(bin_dir)
    pgp._pg_ctl_start_detached(bins, pgdata, 5433, popen=fake_popen)  # noqa: SLF001
    line = restore_sections(str(bin_dir), 5433, "u", "d", pgdata=str(pgdata))["pg_start"]
    if os.name == "nt":  # PowerShell: `& "path" arg ...`; split without shell escapes, strip the quotes
        argv = [t.strip('"') for t in shlex.split(line.removeprefix("& "), posix=False)]
    else:
        argv = shlex.split(line)
    assert argv == captured["cmd"]


@pytest.mark.parametrize("section", sorted(_SECTIONS))
def test_every_windows_section_uses_backslashes_and_exe_in_every_path(section: str) -> None:
    """Rendered on any platform: no forward slash survives in a Windows command, whatever spelling the
    caller's paths came in, and every bundle tool carries .exe."""
    text = restore_sections(
        "C:/pg/bin", 5433, "u", "C:/backups/x.dump", platform="win32", pgdata="C:/pgdata",
    )[section]
    assert "/" not in text, text
    assert not re.search(r"\\(psql|createdb|pg_restore|pg_ctl)(?!\.exe)\b", text), text
    assert "C:\\pg\\bin\\" in text


@pytest.mark.parametrize("platform", ["win32", "linux"])
@pytest.mark.parametrize("name", _ENGINE_TOOLS)
def test_tool_path_agrees_with_the_engines_own_resolver(name: str, platform: str) -> None:
    """tool_path is a copy of the rule PgBinaries.from_dir applies, so it is tied to it."""
    d = Path("/pg/bin")
    assert getattr(PgBinaries.from_dir(d, platform=platform), name) == tool_path(d, name, platform)


# -- (a) the round trip on the session engine's Postgres, through the runbook's own commands ---------


def _run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False, **kw)  # noqa: S603


def _psql(target: BackupTarget, db: str, sql: str) -> str:
    proc = _run([
        str(target.psql), "-X", "-q", "-w", "-h", target.host, "-p", str(target.port), "-U", target.user,
        "-d", db, "-t", "-A", "-v", "ON_ERROR_STOP=1", "-c", sql,
    ])
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _doc_argvs(
    section: str, target: BackupTarget, dump: Path, *, live: str, restored: str, old: str,
) -> list[list[str]]:
    """The runbook's posix block for *section*, scratch database names put in for nexus / nexus_restored /
    nexus_before_restore (before the paths go in, so a directory called nexus is not renamed), the
    placeholders filled, split the way a shell would."""
    out = []
    for line in _doc_blocks()[("posix", section)].splitlines():
        line = line.replace("nexus_before_restore", old).replace("nexus_restored", restored)
        line = re.sub(r"\bnexus\b", live, line)
        for placeholder, value in {
            "<pg_bin>": str(target.bin_dir), "<port>": str(target.port), "<user>": target.user, "<dump>": str(dump),
        }.items():
            line = line.replace(placeholder, value)
        out.append(shlex.split(line))
    return out


@pytest.mark.skipif(
    os.name == "nt", reason="the Windows blocks are compared with the renderer but not executed here (PowerShell form)",
)
def test_a_backup_restores_into_a_fresh_database_through_the_runbooks_own_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _home: Path,
) -> None:
    from tests._engine_substrate import ensure_engine  # noqa: PLC0415 - boots the session engine only for this test

    state = ensure_engine()
    target = BackupTarget.from_bin_dir(
        Path(state["pg_bin"]), host="127.0.0.1", port=int(state["pg_port"]), user=state["pg_user"],
        dbname=state["pg_dbname"],
    )
    suffix = uuid.uuid4().hex[:8]
    probe = f"nx_backup_probe_{suffix}"
    live, restored, old = f"nxbk_live_{suffix}", f"nxbk_new_{suffix}", f"nxbk_old_{suffix}"
    names = {"live": live, "restored": restored, "old": old}
    _psql(target, target.dbname, f"CREATE TABLE public.{probe}(id int, note text); "
                                  f"INSERT INTO public.{probe} VALUES (7, 'planted before the backup');")
    no_path = tmp_path / "empty-path"  # nothing on PATH: a bare psql/createdb/pg_restore cannot be found
    no_path.mkdir()

    def doc(section: str, dump: Path) -> subprocess.CompletedProcess[str]:
        """Run every line of the runbook's *section* by the bundled binary's path; the last result."""
        done = None
        for argv in _doc_argvs(section, target, dump, **names):
            assert Path(argv[0]).parent == target.bin_dir, f"not the bundled binary by its path: {argv[0]}"
            done = _run(argv, env={**os.environ, "PATH": str(no_path)})
            assert done.returncode == 0, f"{argv}\n{done.stderr}"
        assert done is not None
        return done

    def note_in(db: str) -> str:
        return _psql(target, db, f"SELECT coalesce(string_agg(note, ','), 'none') FROM public.{probe}")

    try:
        monkeypatch.setattr(backup_cmd, "resolve_target", lambda: target)
        dest = tmp_path / "out" / "round.dump"
        result = CliRunner().invoke(main, ["backup", "--to", str(dest)])
        assert result.exit_code == 0, result.output
        assert dest.is_file() and dest.stat().st_size > 0
        assert dest.read_bytes()[:5] == b"PGDMP", "not a pg_dump custom-format archive"
        _no_siblings(dest.parent, {"round.dump"})
        out = _flat(result.output)
        assert str(dest) in out and "on disk" in out and ONLY_COPY_SENTENCE in out
        printed = restore_sections(str(target.bin_dir), target.port, target.user, str(dest), platform="posix")
        for line in printed["restore"].splitlines():
            assert f"  {line}" in result.output.splitlines(), "the printed restore command is not the runbook's"

        # the live database stands in as `live`, holding a different row
        _psql(target, "postgres", f"CREATE DATABASE {live}")
        _psql(target, live, f"CREATE TABLE public.{probe}(id int, note text); "
                            f"INSERT INTO public.{probe} VALUES (1, 'the live one')")
        assert doc("pg_up", dest).stdout.strip() == "1"
        doc("restore", dest)  # a FRESH database, from the runbook's own text
        assert note_in(restored) == "planted before the backup" and note_in(live) == "the live one"

        # the connection check must read 0, and must read 1 when someone is connected (not a vacuous check)
        assert doc("check", dest).stdout.strip() == "0"
        holder = subprocess.Popen(  # noqa: S603 - a held connection to the live database
            [str(target.psql), "-X", "-q", "-h", target.host, "-p", str(target.port), "-U", target.user, "-d", live,
             "-c", "SELECT pg_sleep(4)"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            seen = "0"
            for _ in range(30):
                seen = doc("check", dest).stdout.strip()
                if seen == "1":
                    break
                time.sleep(0.1)
            assert seen == "1", "the check did not see the connection"
        finally:
            holder.kill()
            holder.wait()
        seen = "1"
        for _ in range(100):  # the server notices the closed connection when its sleep ends
            seen = doc("check", dest).stdout.strip()
            if seen == "0":
                break
            time.sleep(0.2)
        assert seen == "0"

        # swap: ONE psql -c holds both renames; afterwards live holds the restored data, old holds the old
        assert len(_doc_argvs("swap", target, dest, **names)) == 1
        doc("swap", dest)
        assert note_in(live) == "planted before the backup" and note_in(old) == "the live one"
        # undo: the other way, and it works as written (the names it moves to are free)
        assert len(_doc_argvs("undo", target, dest, **names)) == 1
        doc("undo", dest)
        assert note_in(live) == "the live one" and note_in(restored) == "planted before the backup"
        # swap again, then drop the one set aside
        doc("swap", dest)
        doc("drop", dest)
        assert note_in(live) == "planted before the backup"
        assert _psql(target, "postgres", f"SELECT count(*) FROM pg_database WHERE datname = '{old}'") == "0"
        # a second restore into the name that is free again, then drop_restored removes it
        doc("restore", dest)
        doc("drop_restored", dest)
        assert _psql(target, "postgres", f"SELECT count(*) FROM pg_database WHERE datname = '{restored}'") == "0"
    finally:
        for db in (restored, live, old):
            _psql(target, "postgres", f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        _psql(target, target.dbname, f"DROP TABLE IF EXISTS public.{probe}")
