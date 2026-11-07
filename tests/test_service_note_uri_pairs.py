# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-bivz3.28: ``nx daemon service note-uri-pairs`` runs the RDR-231 pair query as the platform's own superuser.

Unit level, and the same on every platform: no PostgreSQL binary runs. ``psql`` is replaced by a recorder that sees
the argv and environment ``nx`` would give it (the seam ``run=``), so the Windows role derivation and the password
handling are pinned on Linux and on native Windows alike. The real query against a real migrated schema is
``tests/db/test_note_uri_pairs_integration.py``.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.db import note_uri_pairs as mod
from nexus.db.note_uri_pairs import CHANGESET_SQL, PAIR_SQL, NoteUriPairsError, list_note_uri_pairs
from nexus.db.pg_provision import PgBinaries, windows_superuser_name

PASSWORD = "S3cr3t-pw-VALUE-9f2c"
SID = "S-1-5-21-1111111111-2222222222-3333333333-1001"
REPO = Path(__file__).resolve().parent.parent
RUNBOOK = REPO / "docs" / "migration-runbook.md"


def _config_dir(tmp_path: Path, *, password: str | None = PASSWORD, port: str = "54329") -> Path:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    lines = ["# nexus-managed Postgres credentials", f"PG_DATA={cfg / 'postgres'}", f"PG_PORT={port}"]
    if password is not None:
        lines.append(f"PG_SUPERUSER_PASS={password}")
    lines += ["NX_DB_ADMIN_USER=nexus_admin", "NX_DB_ADMIN_PASS=admin-pass-not-this-one"]
    (cfg / "pg_credentials").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return cfg


class Recorder:
    """A stand-in for psql: answers the pair query and the changeset query, records every call."""

    def __init__(self, *, pair_rows: str = "", changeset: str = "EXECUTED", fail=None) -> None:
        self.calls: list[tuple[list[str], dict[str, str] | None]] = []
        self.pair_rows = pair_rows
        self.changeset = changeset
        self.fail = fail  # callable(argv) -> CalledProcessError | None

    def __call__(self, argv, env):
        argv = list(argv)
        self.calls.append((argv, env))
        if self.fail is not None and (err := self.fail(argv)) is not None:
            raise err
        sql = argv[argv.index("-c") + 1]
        return self.pair_rows if sql == PAIR_SQL else self.changeset + "\n"

    def role(self, call: int = 0) -> str:
        argv = self.calls[call][0]
        return argv[argv.index("-U") + 1]


def _bins(tmp_path: Path, *, platform: str | None = None) -> PgBinaries:
    return PgBinaries.from_dir(tmp_path / "bin", platform=platform)


def _failed(stderr: str, argv=("psql",)) -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(2, list(argv), output="", stderr=stderr)


# ── the superuser name ─────────────────────────────────────────────────────────


def test_windows_role_is_nx_plus_hash_of_the_service_sid_never_the_login(tmp_path, monkeypatch):
    """The Windows superuser is `nx_` + 16 hex of sha256(service SID), not USERNAME and not USER."""
    monkeypatch.setenv("USER", "someone")
    monkeypatch.setenv("USERNAME", "Someone Else")
    rec = Recorder()
    list_note_uri_pairs(
        _config_dir(tmp_path), platform="win32", identity=lambda: SID, bins=_bins(tmp_path, platform="win32"), run=rec
    )
    role = rec.role()
    assert role == windows_superuser_name(SID)
    assert re.fullmatch(r"nx_[0-9a-f]{16}", role)
    assert str(rec.calls[0][0][0]).endswith("psql.exe"), "the Windows bundle's client is psql.exe"


def test_windows_role_has_no_postgres_fallback(tmp_path):
    """A refused Windows role is reported, not retried as `postgres`: Windows has one derivation."""
    rec = Recorder(fail=lambda argv: _failed('FATAL:  role "nx_x" does not exist'))
    with pytest.raises(NoteUriPairsError):
        list_note_uri_pairs(_config_dir(tmp_path), platform="win32", identity=lambda: SID, bins=_bins(tmp_path), run=rec)
    assert len(rec.calls) == 1


def test_posix_role_is_the_login_name(tmp_path, monkeypatch):
    monkeypatch.setenv("USER", "nxtester")
    rec = Recorder()
    list_note_uri_pairs(_config_dir(tmp_path), platform="linux", bins=_bins(tmp_path), run=rec)
    assert rec.role() == "nxtester"


def test_posix_falls_back_to_postgres_when_the_login_role_is_absent(tmp_path, monkeypatch):
    """A cluster provisioned with neither USER nor LOGNAME has the superuser `postgres` (the runbook's second bullet)."""
    monkeypatch.setenv("USER", "nxtester")

    def fail(argv):
        return _failed('FATAL:  role "nxtester" does not exist') if argv[argv.index("-U") + 1] == "nxtester" else None

    rec = Recorder(fail=fail)
    report = list_note_uri_pairs(_config_dir(tmp_path), platform="linux", bins=_bins(tmp_path), run=rec)
    assert report.role == "postgres"
    assert [rec.role(i) for i in range(len(rec.calls))] == ["nxtester", "postgres", "postgres"]


# ── the password ───────────────────────────────────────────────────────────────


def test_password_travels_only_as_pgpassword_in_the_childs_environment(tmp_path):
    rec = Recorder(pair_rows="default|1.1.9|1.1.10|chroma://knowledge__x/t\n")
    report = list_note_uri_pairs(_config_dir(tmp_path), platform="linux", bins=_bins(tmp_path), run=rec)
    assert rec.calls, "psql was never run"
    for argv, env in rec.calls:
        assert PASSWORD not in " ".join(argv), "the password is on argv"
        assert env is not None and env["PGPASSWORD"] == PASSWORD
        assert "-w" in argv, "psql must never prompt"
    assert PASSWORD not in "\n".join(report.lines())
    assert "admin-pass-not-this-one" not in " ".join(a for call in rec.calls for a in call[0])


def test_errors_never_carry_the_password(tmp_path):
    """Even a psql that echoed the password in its stderr cannot get it into the message."""
    rec = Recorder(fail=lambda argv: _failed(f"psql: error: connection to server failed: password {PASSWORD} wrong"))
    with pytest.raises(NoteUriPairsError) as ei:
        list_note_uri_pairs(_config_dir(tmp_path), platform="linux", bins=_bins(tmp_path), run=rec)
    assert PASSWORD not in str(ei.value)


def test_no_recorded_password_is_named_in_the_auth_error(tmp_path):
    rec = Recorder(fail=lambda argv: _failed("psql: error: fe_sendauth: no password supplied"))
    with pytest.raises(NoteUriPairsError, match="PG_SUPERUSER_PASS"):
        list_note_uri_pairs(_config_dir(tmp_path, password=None), platform="linux", bins=_bins(tmp_path), run=rec)
    assert all(call[1] is None or "PGPASSWORD" not in call[1] or not call[1]["PGPASSWORD"] for call in rec.calls)


# ── output ─────────────────────────────────────────────────────────────────────


def test_pairs_are_listed_with_the_changeset_state(tmp_path):
    rows = (
        "default|1.1.9|1.1.10|chroma://knowledge__a/with|pipe\n"
        "default|1.1.20|1.1.21|chroma://knowledge__a/b\n"
    )
    report = list_note_uri_pairs(
        _config_dir(tmp_path), platform="linux", bins=_bins(tmp_path), run=Recorder(pair_rows=rows, changeset="")
    )
    assert [p.chroma_row for p in report.pairs] == ["1.1.9", "1.1.20"]
    assert report.pairs[0].source_uri == "chroma://knowledge__a/with|pipe", "source_uri is the last column and may hold the separator"
    text = "\n".join(report.lines())
    assert "pairs in the local cluster (127.0.0.1:54329" in text and ": 2" in text.splitlines()[0]
    assert "tenant_id\tchroma_row\tnx_note_row\tsource_uri" in text
    assert "Changeset note-uri-001-1: not run" in text


def test_no_pairs_prints_a_zero_count_and_the_changeset_row(tmp_path):
    report = list_note_uri_pairs(_config_dir(tmp_path), platform="linux", bins=_bins(tmp_path), run=Recorder())
    lines = report.lines()
    assert lines[0].endswith(": 0") and "tenant_id" not in "\n".join(lines)
    assert lines[-1] == "Changeset note-uri-001-1: EXECUTED"


def test_unreadable_changeset_table_is_reported_not_fatal(tmp_path):
    rec = Recorder(fail=lambda argv: _failed("relation public.databasechangelog does not exist") if CHANGESET_SQL in argv else None)
    report = list_note_uri_pairs(_config_dir(tmp_path), platform="linux", bins=_bins(tmp_path), run=rec)
    assert report.changeset is None and "could not be read" in report.lines()[-1]


def test_a_malformed_row_is_an_error_not_a_misparse(tmp_path):
    with pytest.raises(NoteUriPairsError, match="four columns"):
        list_note_uri_pairs(_config_dir(tmp_path), platform="linux", bins=_bins(tmp_path), run=Recorder(pair_rows="only|two\n"))


# ── failures: a message, never a traceback ─────────────────────────────────────


def test_no_credentials_file_says_managed_installs_have_no_local_cluster(tmp_path):
    with pytest.raises(NoteUriPairsError, match="managed or cloud install"):
        list_note_uri_pairs(tmp_path, platform="linux", bins=_bins(tmp_path), run=Recorder())


def test_credentials_without_a_port(tmp_path):
    cfg = _config_dir(tmp_path, port="")
    with pytest.raises(NoteUriPairsError, match="PG_PORT"):
        list_note_uri_pairs(cfg, platform="linux", bins=_bins(tmp_path), run=Recorder())


@pytest.mark.parametrize(
    ("stderr", "needle"),
    [
        ('psql: error: connection to server at "127.0.0.1", port 54329 failed: Connection refused', "not accepting connections"),
        ('FATAL:  password authentication failed for user "x"', "was refused"),
        ('ERROR:  relation "nexus.catalog_documents" does not exist', "has not migrated"),
        ("something unexpected", "failed"),
    ],
)
def test_psql_failures_become_messages(tmp_path, stderr, needle):
    with pytest.raises(NoteUriPairsError, match=needle):
        list_note_uri_pairs(
            _config_dir(tmp_path), platform="linux", bins=_bins(tmp_path), run=Recorder(fail=lambda a: _failed(stderr))
        )


def test_timeout_root_and_missing_client_become_messages(tmp_path, monkeypatch):
    cfg = _config_dir(tmp_path)

    def timeout(argv, env):
        raise subprocess.TimeoutExpired(argv, 60)

    with pytest.raises(NoteUriPairsError, match="did not answer"):
        list_note_uri_pairs(cfg, platform="linux", bins=_bins(tmp_path), run=timeout)

    def root(argv, env):
        raise mod.PgRootUserError("nx will not run as root")

    with pytest.raises(NoteUriPairsError, match="root"):
        list_note_uri_pairs(cfg, platform="linux", bins=_bins(tmp_path), run=root)

    def gone(argv, env):
        raise FileNotFoundError(2, "No such file", argv[0])

    with pytest.raises(NoteUriPairsError, match="could not start psql"):
        list_note_uri_pairs(cfg, platform="linux", bins=_bins(tmp_path), run=gone)

    def no_bundle():
        raise mod.PgBinaryNotFoundError("No nexus PostgreSQL bundle found.")

    monkeypatch.setattr(mod, "discover_pg_binaries", no_bundle)
    with pytest.raises(NoteUriPairsError, match="never uses a host psql"):
        list_note_uri_pairs(cfg, platform="linux", run=Recorder())


# ── the CLI ────────────────────────────────────────────────────────────────────


def _cli(args, env):
    return CliRunner().invoke(main, ["daemon", "service", "note-uri-pairs", *args], env=env, catch_exceptions=True)


def test_verb_is_registered_under_daemon_service():
    service = main.commands["daemon"].commands["service"]
    assert "note-uri-pairs" in service.commands
    assert not service.commands["note-uri-pairs"].hidden


def test_cli_prints_the_report(tmp_path, monkeypatch):
    cfg = _config_dir(tmp_path)
    rec = Recorder(pair_rows="default|1.1.9|1.1.10|chroma://knowledge__x/t\n")
    real = mod.list_note_uri_pairs
    monkeypatch.setattr(
        mod, "list_note_uri_pairs", lambda d, **kw: real(d, platform="linux", bins=_bins(tmp_path), run=rec, **kw)
    )
    result = _cli(["--config-dir", str(cfg)], {})
    assert result.exit_code == 0, result.output
    assert "1.1.9\t1.1.10\tchroma://knowledge__x/t" in result.output
    assert PASSWORD not in result.output


def test_cli_on_a_managed_install_exits_one_with_a_message(tmp_path):
    """service_url set (a managed install) and a leftover pg_credentials: refuse, no traceback."""
    cfg = tmp_path / "managed"
    cfg.mkdir()
    (cfg / "pg_credentials").write_text(f"PG_PORT=1\nPG_SUPERUSER_PASS={PASSWORD}\n", encoding="utf-8")
    result = _cli([], {"NEXUS_CONFIG_DIR": str(cfg), "NX_LOCAL": "0"})
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), f"a traceback escaped: {result.exception!r}"
    assert "managed or cloud service" in result.output and "Traceback" not in result.output
    assert PASSWORD not in result.output


def test_cli_with_no_local_cluster_exits_one_with_a_message(tmp_path):
    cfg = tmp_path / "empty"
    cfg.mkdir()
    result = _cli([], {"NEXUS_CONFIG_DIR": str(cfg), "NX_LOCAL": "1"})
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), f"a traceback escaped: {result.exception!r}"
    assert "no nx-managed local cluster" in result.output


# ── the runbook ────────────────────────────────────────────────────────────────


def _runbook_section() -> str:
    text = RUNBOOK.read_text(encoding="utf-8")
    start = text.index("## If the RDR-231 note-URI backfill halts startup")
    end = text.index("\n## ", start + 10) if "\n## " in text[start + 10:] else len(text)
    return text[start:end]


def test_runbook_cloud_query_is_the_verbs_query():
    """The cloud operator pastes the runbook's SQL; the verb runs PAIR_SQL. They must be the same text."""
    blocks = re.findall(r"```sql\n(.*?)```", _runbook_section(), re.S)
    assert blocks, "the runbook's query block is gone"
    norm = lambda s: re.sub(r"\s+", " ", s).strip()  # noqa: E731
    assert any(norm(b) == norm(PAIR_SQL) for b in blocks), "docs/migration-runbook.md and PAIR_SQL have drifted"


def test_runbook_local_steps_use_the_verb_and_no_hand_credentials():
    section = _runbook_section()
    assert "nx daemon service note-uri-pairs" in section
    for gone in ("sed -n 's/^PG_SUPERUSER_PASS", "PGPASSWORD=\"$(sed", "pg-bundle/bundle/bin/psql", "A verb that runs this for you is planned"):
        assert gone not in section, f"the runbook still hands the operator credential handling: {gone!r}"
