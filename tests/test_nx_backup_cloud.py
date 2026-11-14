# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx backup`` in cloud mode and ``nx backup --check FILE`` (nexus-csayy.3, RDR-248): the verb end to end.

The engine is the stand-in of ``tests/_fake_export_engine.py`` (a real HTTP server serving the documented
contract); the same verb against the REAL engine is ``tests/test_nx_backup_cloud_roundtrip.py``. The local arm's
tests stay in ``tests/test_nx_backup.py``.
"""

from __future__ import annotations

import os
import re
import signal
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.backup.cloud_export import EXCLUDED_TABLE_PHRASES
from nexus.backup.pg_backup import ONLY_COPY_SENTENCE
from nexus.backup.tenant_file import verify_file
from nexus.cli import main
from tests._fake_export_engine import FakeEngine, Reply, TableSpec, standard_tables


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


def _point_at(monkeypatch: pytest.MonkeyPatch, engine: FakeEngine, token: str = "tok") -> None:
    monkeypatch.setenv("NX_SERVICE_URL", engine.base_url)
    monkeypatch.setenv("NX_SERVICE_TOKEN", token)


def _flat(text: str) -> str:
    return " ".join(text.split())


# -- the verb in cloud mode ---------------------------------------------------------------------------


def test_cloud_backup_writes_a_verified_file_and_says_what_it_holds(
    cloud_mode: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    dest = tmp_path / "out" / "mine.nxtenant"
    with FakeEngine(standard_tables()) as engine:
        _point_at(monkeypatch, engine)
        result = CliRunner().invoke(main, ["backup", "--to", str(dest)])
    assert result.exit_code == 0, result.output
    out = _flat(result.output)
    assert f"Backup written: {dest}" in out and "Size:" in out
    assert "acme" in out and "572" in out, "tenant and schema changeset count"
    assert re.search(r"data\D+6 rows", out) and re.search(r"derived\D+2 rows", out), out
    assert ONLY_COPY_SENTENCE in out
    assert "Not in this backup:" in out
    for phrase in set(EXCLUDED_TABLE_PHRASES.values()):
        assert phrase in out, f"the run does not say it leaves out {phrase}"
    assert "sensitive" not in out.lower()
    assert f"nx backup --check {dest}" in out
    assert "pg_restore" not in out, "the local arm's restore commands are not for a tenant file"
    assert verify_file(dest).header["tenant_id"] == "acme"
    assert {p.name for p in dest.parent.iterdir()} == {"mine.nxtenant"}


def test_cloud_backup_says_when_the_file_holds_sensitive_documents(
    cloud_mode: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    tables = [TableSpec("catalog_documents", "data", [("tumbler", "text"), ("sensitive", "boolean")],
                        [[[b"1.1.1", b"\x01"], [b"1.1.2", b"\x01"], [b"1.1.3", b"\x00"]]])]
    with FakeEngine(tables) as engine:
        _point_at(monkeypatch, engine)
        result = CliRunner().invoke(main, ["backup", "--to", str(tmp_path / "s.nxtenant")])
    assert result.exit_code == 0, result.output
    assert "holds 2 sensitive documents" in _flat(result.output)


def test_cloud_backup_defaults_to_the_backups_directory_named_for_the_tenant(
    cloud_mode: None, monkeypatch: pytest.MonkeyPatch, _home: Path,
) -> None:
    with FakeEngine(standard_tables(), tenant="acme") as engine:
        _point_at(monkeypatch, engine)
        result = CliRunner().invoke(main, ["backup"])
    assert result.exit_code == 0, result.output
    files = list((_home / "nexus-backups").iterdir())
    assert len(files) == 1 and re.fullmatch(r"nexus-acme-\d{8}T\d{6}Z\.nxtenant", files[0].name), files
    if os.name != "nt":
        assert ((_home / "nexus-backups").stat().st_mode & 0o777) == 0o700
        assert (files[0].stat().st_mode & 0o777) == 0o600


def test_cloud_backup_with_an_older_engine_names_the_version_and_writes_nothing(
    cloud_mode: None, monkeypatch: pytest.MonkeyPatch, _home: Path,
) -> None:
    with FakeEngine(standard_tables(), supported=False, release="0.1.189") as engine:
        _point_at(monkeypatch, engine)
        result = CliRunner().invoke(main, ["backup"])
    assert result.exit_code != 0
    assert "0.1.191" in result.output and "tenant export" in result.output
    assert not (_home / "nexus-backups").exists(), "a refusal creates no directory"


def test_cloud_backup_refuses_an_existing_destination_without_touching_the_engine(
    cloud_mode: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    dest = tmp_path / "mine.nxtenant"
    dest.write_bytes(b"precious")
    with FakeEngine(standard_tables()) as engine:
        _point_at(monkeypatch, engine)
        result = CliRunner().invoke(main, ["backup", "--to", str(dest)])
        assert not engine.log
    assert result.exit_code != 0 and "already exists" in result.output
    assert dest.read_bytes() == b"precious"


@pytest.mark.expects_unresolvable_endpoint  # no token resolves, on purpose; no tenant
def test_cloud_backup_with_no_token_says_how_to_set_one(
    cloud_mode: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    with FakeEngine(standard_tables()) as engine:
        monkeypatch.setenv("NX_SERVICE_URL", engine.base_url)
        monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
        result = CliRunner().invoke(main, ["backup", "--to", str(tmp_path / "x.nxtenant")])
    assert result.exit_code != 0
    assert "service_token" in result.output
    assert not (tmp_path / "x.nxtenant").exists()


def test_cloud_backup_with_a_failing_engine_says_run_again_and_leaves_nothing(
    cloud_mode: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from nexus.backup import cloud_export as ce  # noqa: PLC0415
    from tests._fake_export_engine import Reply  # noqa: PLC0415

    monkeypatch.setattr(ce, "BACKOFF_LADDER", (0.0, 0.0, 0.0, 0.0))
    dest = tmp_path / "out" / "x.nxtenant"
    with FakeEngine(standard_tables()) as engine:
        engine.script.append(lambda r: Reply(500, {"error": "boom", "reason": "internal"}) if r.kind == "rows" and r.nth >= 1 else None)
        _point_at(monkeypatch, engine)
        result = CliRunner().invoke(main, ["backup", "--to", str(dest)])
        assert engine.deleted == engine.opened
    assert result.exit_code == 1
    assert "run nx backup again" in _flat(result.output)
    assert list(dest.parent.iterdir()) == []


def test_local_mode_still_takes_the_local_arm(monkeypatch: pytest.MonkeyPatch, _home: Path, tmp_path: Path) -> None:
    """Nothing about a local install changed: it is refused the local way, not sent to an export session."""
    monkeypatch.setenv("NX_LOCAL", "1")
    result = CliRunner().invoke(main, ["backup"])
    assert result.exit_code != 0 and "nx init" in result.output
    assert not (_home / "nexus-backups").exists()


# -- nx backup --check --------------------------------------------------------------------------------


@pytest.fixture
def good_file(cloud_mode: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    dest = tmp_path / "good.nxtenant"
    with FakeEngine(standard_tables()) as engine:
        _point_at(monkeypatch, engine)
        assert CliRunner().invoke(main, ["backup", "--to", str(dest)]).exit_code == 0
    return dest


def test_check_prints_the_header_and_each_tables_rows(good_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # no engine contact: point the service somewhere that would fail loudly if asked
    monkeypatch.setenv("NX_SERVICE_URL", "http://127.0.0.1:1")
    result = CliRunner().invoke(main, ["backup", "--check", str(good_file)])
    assert result.exit_code == 0, result.output
    out = _flat(result.output)
    assert f"{good_file}: OK" in out
    for fact in ("Tenant: acme", "Engine: 0.1.191 (export format 1), schema 572 changesets", "Snapshot taken: ",
                 "Tables: 3, rows: 8 (data 6, derived 2)"):
        assert fact in out, (fact, out)
    assert re.search(r"memory\s+data\s+6 rows\s+3 pages", result.output)
    assert re.search(r"frecency\s+derived\s+2 rows\s+1 page\b", result.output)
    assert re.search(r"plans\s+data\s+0 rows\s+1 page\b", result.output)


def test_check_refuses_a_damaged_file_naming_the_table(good_file: Path) -> None:
    raw = bytearray(good_file.read_bytes())
    at = raw.index(b"row 1-0")  # inside the second page of the memory table
    raw[at] ^= 0xFF
    good_file.write_bytes(bytes(raw))
    result = CliRunner().invoke(main, ["backup", "--check", str(good_file)])
    assert result.exit_code == 1
    assert "table memory page 2" in result.output and "hash" in result.output
    assert "OK" not in result.output


def test_check_refuses_a_truncated_file_naming_the_table(good_file: Path) -> None:
    raw = good_file.read_bytes()
    good_file.write_bytes(raw[: len(raw) - 300])
    result = CliRunner().invoke(main, ["backup", "--check", str(good_file)])
    assert result.exit_code == 1 and "truncated in the trailer" in result.output


def test_check_of_a_local_dump_says_it_is_not_a_tenant_backup(tmp_path: Path) -> None:
    dump = tmp_path / "x.dump"
    dump.write_bytes(b"PGDMP\x01\x0e\x00 a pg_dump archive")
    result = CliRunner().invoke(main, ["backup", "--check", str(dump)])
    assert result.exit_code == 1 and "not a nexus tenant backup" in result.output
    assert "pg_restore -l" in result.output, "points at how a local dump is checked"


def test_check_and_to_together_are_a_usage_error(good_file: Path, tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["backup", "--check", str(good_file), "--to", str(tmp_path / "y")])
    assert result.exit_code == 2 and "--check" in result.output and "--to" in result.output


def test_check_of_a_missing_file_is_a_usage_error(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["backup", "--check", str(tmp_path / "nope.nxtenant")])
    assert result.exit_code == 2


def test_check_works_in_local_mode_too(good_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NX_LOCAL", "1")
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    assert CliRunner().invoke(main, ["backup", "--check", str(good_file)]).exit_code == 0


def test_the_help_describes_the_cloud_arm_and_check() -> None:
    text = _flat(CliRunner().invoke(main, ["backup", "--help"]).output)
    assert "--check" in text and ".nxtenant" in text and "cloud" in text.lower()
    assert ONLY_COPY_SENTENCE in text and "NOT compressed" in text, "the local arm's words are unchanged"


# -- a real SIGTERM or SIGHUP mid-export (the verb's own signal handling, not a KeyboardInterrupt raised by hand) ----


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
@pytest.mark.parametrize("name", ["SIGTERM", "SIGHUP"])
def test_a_real_signal_mid_export_stops_the_verb_cleanly(
    name: str, cloud_mode: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """SIGTERM (a kill) and SIGHUP (a closed terminal) must clean up like Ctrl-C: no partial file, and a DELETE for
    every session the run opened. Without ``_signals_as_interrupt`` in the cloud arm the default action kills the
    process with the partial file on disk and the engine's snapshot held; here a recording handler stands in for
    that default (so a missing wiring fails this test instead of ending the test run), and it must never run."""
    signum = getattr(signal, name)
    stray: list[int] = []
    def recorder(s: int, _f: object) -> None:
        stray.append(s)

    previous = signal.signal(signum, recorder)
    dest = tmp_path / "out" / "x.nxtenant"
    try:
        with FakeEngine(standard_tables()) as engine:
            def fire(req) -> Reply | None:
                if req.kind == "rows" and req.nth == 1:
                    os.kill(os.getpid(), signum)
                return None

            engine.script.append(fire)
            _point_at(monkeypatch, engine)
            result = CliRunner().invoke(main, ["backup", "--to", str(dest)])
            assert len(engine.opened) == 1 and engine.deleted == engine.opened, "the snapshot is released at once"
        assert stray == [], "the verb's own handler took the signal"
        assert result.exit_code == 1 and "Aborted" in result.output, (result.exit_code, result.output)
        assert signal.getsignal(signum) is recorder, "the handler the verb found is the one it put back"
        assert list(dest.parent.iterdir()) == [], "no .partial"
    finally:
        signal.signal(signum, previous)


def test_resolve_target_still_refuses_managed_mode_for_a_direct_caller(cloud_mode: None) -> None:
    from nexus.backup.pg_backup import BackupRefused, resolve_target  # noqa: PLC0415

    with pytest.raises(BackupRefused, match="cloud arm"):
        resolve_target()


# -- --check on files that are not what the engine writes ---------------------------------------------------------


def _hand_built(path: Path, header: dict) -> Path:
    from nexus.backup.tenant_file import TenantFileWriter  # noqa: PLC0415

    with path.open("wb") as fh:
        w = TenantFileWriter(fh, check_complete=False)
        w.write_header(header)
        w.finish()
    return path


@pytest.mark.parametrize(("change", "says"), [({"tenant_id": None}, "no tenant"), ({"tables": []}, "no tables")])
def test_check_refuses_a_header_with_no_tenant_or_no_tables_cleanly(change: dict, says: str, tmp_path: Path) -> None:
    header = {"format_version": 1, "schema_changeset_count": 5, "tenant_id": "acme", "tables": [], **change}
    path = _hand_built(tmp_path / "h.nxtenant", header)
    result = CliRunner().invoke(main, ["backup", "--check", str(path)])
    assert result.exit_code == 1 and says in result.output
    assert "Traceback" not in result.output and isinstance(result.exception, SystemExit)


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), reason="needs a file this user cannot read")
def test_check_of_an_unreadable_file_is_a_clean_refusal(tmp_path: Path) -> None:
    path = tmp_path / "locked.nxtenant"
    path.write_bytes(b"NXTENANT")
    path.chmod(0)
    try:
        result = CliRunner().invoke(main, ["backup", "--check", str(path)])
    finally:
        path.chmod(0o600)
    assert result.exit_code in (1, 2) and "Traceback" not in result.output
    assert isinstance(result.exception, SystemExit), result.exception


# -- pg_backup._publish, the fallback both arms use on a filesystem without hard links ----------------------------


@pytest.mark.parametrize("fails", ["close", "replace"])
def test_publish_fallback_leaves_no_empty_placeholder_when_interrupted_after_the_claim(
    fails: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import errno  # noqa: PLC0415

    from nexus.backup import pg_backup  # noqa: PLC0415
    from tests._module_seam import setattr_in  # noqa: PLC0415

    src = tmp_path / "finished.partial"
    src.write_bytes(b"a finished backup")
    dest = tmp_path / "backup.nxtenant"

    def no_hard_links(*_a: object, **_k: object) -> None:
        raise OSError(errno.EPERM, "Operation not permitted")

    def interrupted(*_a: object, **_k: object) -> None:
        raise KeyboardInterrupt

    setattr_in(monkeypatch, pg_backup, "os.link", no_hard_links)
    if fails == "close":
        real_close = os.close

        def close_then_interrupt(fd: int) -> None:
            real_close(fd)
            raise KeyboardInterrupt

        setattr_in(monkeypatch, pg_backup, "os.close", close_then_interrupt)
    else:
        setattr_in(monkeypatch, pg_backup, "os.replace", interrupted)
    with pytest.raises(KeyboardInterrupt):
        pg_backup._publish(src, dest)
    assert not dest.exists(), "no empty placeholder at the destination"
    assert src.read_bytes() == b"a finished backup"


def test_publish_fallback_never_touches_a_file_that_is_already_there(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import errno  # noqa: PLC0415

    from nexus.backup import pg_backup  # noqa: PLC0415
    from tests._module_seam import setattr_in  # noqa: PLC0415

    src = tmp_path / "finished.partial"
    src.write_bytes(b"new")
    dest = tmp_path / "backup.nxtenant"
    dest.write_bytes(b"someone else's backup")
    setattr_in(monkeypatch, pg_backup, "os.link", lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EPERM, "no links")))
    with pytest.raises(pg_backup.BackupRefused, match="already exists"):
        pg_backup._publish(src, dest)
    assert dest.read_bytes() == b"someone else's backup"
