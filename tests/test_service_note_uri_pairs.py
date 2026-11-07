# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-bivz3.28: ``nx daemon service note-uri-pairs`` runs the RDR-231 pair query as the platform's own superuser.

Unit level, and the same on every platform: no PostgreSQL binary runs. ``psql`` is replaced by a recorder that sees
the argv and environment ``nx`` would give it (the seam ``run=``), so the Windows role derivation, the password and
environment handling and the messages are pinned on Linux and on native Windows alike. What the recorder cannot
show is in ``tests/db/test_pg_scram_auth.py`` (a real scram cluster built from the bundle, the PGPASSWORD round
trip and the ``postgres`` fallback against the server's real answer) and
``tests/db/test_note_uri_pairs_integration.py`` (the query against a real migrated schema).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.aspect_readers import FILE_ROUTED_PREFIXES
from nexus.cli import main
from nexus.daemon.service_registry import ServiceIdentityError
from nexus.db import note_uri_pairs as mod
from nexus.db.note_uri_pairs import CHANGESET_SQL, PAIR_SQL, NoteUriPairsError, list_note_uri_pairs
from nexus.db.pg_provision import PgBinaries, windows_superuser_name

PASSWORD = "S3cr3t-pw-VALUE-9f2c"
SID = "S-1-5-21-1111111111-2222222222-3333333333-1001"
REPO = Path(__file__).resolve().parent.parent
RUNBOOK = REPO / "docs" / "migration-runbook.md"
BACKFILL_XML = REPO / "service" / "src" / "main" / "resources" / "db" / "changelog" / "note-uri-001-backfill.xml"

#: What the bundled server (scram on every hba line) answers for a role that does not exist: the same text as a wrong
#: password. Measured on the bundle's PostgreSQL 17.5; `role "x" does not exist` is never sent.
SCRAM_REFUSAL = 'psql: error: connection to server at "127.0.0.1", port 54329 failed: FATAL:  password authentication failed for user "x"'
CONNECTION_REFUSED = 'psql: error: connection to server at "127.0.0.1", port 54329 failed: Connection refused'


def _config_dir(tmp_path: Path, *, password: str | None = PASSWORD, port: str = "54329") -> Path:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    lines = ["# nexus-managed Postgres credentials", f"PG_DATA={cfg / 'postgres'}", f"PG_PORT={port}"]
    if password is not None:
        lines.append(f"PG_SUPERUSER_PASS={password}")
    lines += ["NX_DB_ADMIN_USER=nexus_admin", "NX_DB_ADMIN_PASS=admin-pass-not-this-one"]
    (cfg / "pg_credentials").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return cfg


def _rows(*rows: tuple[str, str, str, str]) -> str:
    """psql -0 -z output: every field and every record ends in NUL."""
    return "".join(f + "\0" for row in rows for f in row)


class Recorder:
    """A stand-in for psql: answers the pair query and the changeset query, records every call."""

    def __init__(self, *, pair_rows: str = "", changeset: str = "EXECUTED", fail=None) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.pair_rows = pair_rows
        self.changeset = changeset
        self.fail = fail  # callable(argv) -> CalledProcessError | None

    def __call__(self, argv, env):
        argv = list(argv)
        self.calls.append((argv, env))
        if self.fail is not None and (err := self.fail(argv)) is not None:
            raise err
        sql = argv[argv.index("-c") + 1]
        return self.pair_rows if sql == PAIR_SQL else (self.changeset + "\0" if self.changeset else "")

    def role(self, call: int = 0) -> str:
        argv = self.calls[call][0]
        return argv[argv.index("-U") + 1]

    def roles(self) -> list[str]:
        return [self.role(i) for i in range(len(self.calls))]


def _bins(tmp_path: Path, *, platform: str | None = None) -> PgBinaries:
    return PgBinaries.from_dir(tmp_path / "bin", platform=platform)


def _failed(stderr: str, *, rc: int = 2, argv=("psql",)) -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(rc, list(argv), output="", stderr=stderr)


def _list(cfg: Path, tmp_path: Path, run, **kw):
    """list_note_uri_pairs with a recorder, fake bins and a port that accepts (no socket is opened)."""
    kw.setdefault("platform", "linux")
    kw.setdefault("bins", _bins(tmp_path))
    kw.setdefault("accepting", lambda host, port: True)
    return list_note_uri_pairs(cfg, run=run, **kw)


# ── the superuser name ─────────────────────────────────────────────────────────


def test_windows_role_is_nx_plus_hash_of_the_service_sid_never_the_login(tmp_path, monkeypatch):
    """The Windows superuser is `nx_` + 16 hex of sha256(service SID), not USERNAME and not USER."""
    monkeypatch.setenv("USER", "someone")
    monkeypatch.setenv("USERNAME", "Someone Else")
    rec = Recorder()
    _list(_config_dir(tmp_path), tmp_path, rec, platform="win32", identity=lambda: SID, bins=_bins(tmp_path, platform="win32"))
    role = rec.role()
    assert role == windows_superuser_name(SID)
    assert re.fullmatch(r"nx_[0-9a-f]{16}", role)


def test_windows_refused_role_has_no_postgres_fallback_and_names_the_other_account(tmp_path):
    """Windows has one derivation: a refusal is reported, not retried as `postgres`, with the likely cause."""
    rec = Recorder(fail=lambda argv: _failed(SCRAM_REFUSAL))
    with pytest.raises(NoteUriPairsError) as ei:
        _list(_config_dir(tmp_path), tmp_path, rec, platform="win32", identity=lambda: SID)
    assert len(rec.calls) == 1
    assert windows_superuser_name(SID) in str(ei.value) and "another OS account" in str(ei.value)


def test_unreadable_windows_identity_is_a_message_not_a_traceback(tmp_path):
    def no_sid() -> str:
        raise ServiceIdentityError("the user's SID could not be read")

    with pytest.raises(NoteUriPairsError, match="could not be derived"):
        _list(_config_dir(tmp_path), tmp_path, Recorder(), platform="win32", identity=no_sid)


def test_posix_role_is_the_login_name(tmp_path, monkeypatch):
    monkeypatch.setenv("USER", "nxtester")
    rec = Recorder()
    _list(_config_dir(tmp_path), tmp_path, rec)
    assert rec.role() == "nxtester"


def test_posix_falls_back_to_postgres_on_the_servers_real_refusal(tmp_path, monkeypatch):
    """The bundle is scram everywhere, and scram answers a missing role like a wrong password (psql exit 2)."""
    monkeypatch.setenv("USER", "nxtester")

    def fail(argv):
        return _failed(SCRAM_REFUSAL) if argv[argv.index("-U") + 1] == "nxtester" else None

    rec = Recorder(fail=fail)
    report = _list(_config_dir(tmp_path), tmp_path, rec)
    assert report.role == "postgres"
    assert rec.roles() == ["nxtester", "postgres", "postgres"]


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(lambda: _failed(CONNECTION_REFUSED), id="connection refused is not a role problem"),
        pytest.param(lambda: _failed('ERROR:  relation "nexus.catalog_documents" does not exist', rc=1), id="a query error"),
    ],
)
def test_posix_does_not_retry_as_postgres_on_anything_but_a_connection_level_refusal(tmp_path, monkeypatch, failure):
    monkeypatch.setenv("USER", "nxtester")
    rec = Recorder(fail=lambda argv: failure())
    with pytest.raises(NoteUriPairsError):
        _list(_config_dir(tmp_path), tmp_path, rec)
    assert rec.roles() == ["nxtester"]


def test_both_roles_are_named_when_the_fallback_also_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("USER", "nxtester")
    rec = Recorder(fail=lambda argv: _failed(SCRAM_REFUSAL))
    with pytest.raises(NoteUriPairsError, match=r"neither role nxtester nor role postgres was accepted"):
        _list(_config_dir(tmp_path), tmp_path, rec)
    assert rec.roles() == ["nxtester", "postgres"]


# ── the password and the child environment ─────────────────────────────────────


def test_password_travels_only_as_pgpassword_in_the_childs_environment(tmp_path):
    rec = Recorder(pair_rows=_rows(("default", "1.1.9", "1.1.10", "chroma://knowledge__x/t")))
    report = _list(_config_dir(tmp_path), tmp_path, rec)
    assert rec.calls, "psql was never run"
    for argv, env in rec.calls:
        assert PASSWORD not in " ".join(argv), "the password is on argv"
        assert env["PGPASSWORD"] == PASSWORD
        assert "-w" in argv, "psql must never prompt"
    assert PASSWORD not in "\n".join(report.lines())
    assert "admin-pass-not-this-one" not in " ".join(a for call in rec.calls for a in call[0])


def test_child_environment_pins_encoding_and_message_language_even_with_no_password(tmp_path, monkeypatch):
    monkeypatch.setenv("PGOPTIONS", "-c statement_timeout=5s")
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    monkeypatch.delenv("PGPASSWORD", raising=False)
    rec = Recorder()
    _list(_config_dir(tmp_path, password=None), tmp_path, rec)
    for _argv, env in rec.calls:
        assert env["PGCLIENTENCODING"] == "UTF8"
        assert env["LC_ALL"] == "C"
        assert env["PGOPTIONS"] == "-c statement_timeout=5s -c lc_messages=C", "an ambient PGOPTIONS is kept"
        assert "PGPASSWORD" not in env


def test_errors_never_carry_the_password(tmp_path):
    """Even a psql that echoed the password in its stderr cannot get it into the message."""
    rec = Recorder(fail=lambda argv: _failed(f"psql: error: connection to server failed: password {PASSWORD} wrong", rc=1))
    with pytest.raises(NoteUriPairsError) as ei:
        _list(_config_dir(tmp_path), tmp_path, rec)
    assert PASSWORD not in str(ei.value)


def test_no_recorded_password_is_named_in_the_auth_error(tmp_path):
    rec = Recorder(fail=lambda argv: _failed("psql: error: fe_sendauth: no password supplied"))
    with pytest.raises(NoteUriPairsError, match="PG_SUPERUSER_PASS is recorded"):
        _list(_config_dir(tmp_path, password=None), tmp_path, rec, platform="win32", identity=lambda: SID)


# ── the psql client comes from the config dir ──────────────────────────────────


def test_psql_is_resolved_from_the_given_config_dir(tmp_path, monkeypatch):
    """--config-dir is honoured for the client as it is for the credentials (NEXUS_PG_BIN still wins)."""
    cfg = _config_dir(tmp_path)
    under = tmp_path / "bundle-bin"
    under.mkdir()
    found = PgBinaries.from_dir(under)  # psql.exe etc. on Windows
    for binary in (found.initdb, found.pg_ctl, found.psql, found.createdb):
        binary.write_text("")
    seen: list[Path] = []
    monkeypatch.delenv("NEXUS_PG_BIN", raising=False)
    monkeypatch.setattr(mod, "extracted_bin_dir", lambda d: (seen.append(d), under)[1])
    monkeypatch.setattr(mod, "discover_pg_binaries", lambda: pytest.fail("the ambient config dir's bundle was used"))
    rec = Recorder()
    list_note_uri_pairs(cfg, platform="linux", run=rec, accepting=lambda h, p: True)
    assert seen == [cfg]
    assert rec.calls[0][0][0] == str(PgBinaries.from_dir(under).psql)

    ambient = _bins(tmp_path)
    monkeypatch.setenv("NEXUS_PG_BIN", str(ambient.bin_dir))
    monkeypatch.setattr(mod, "discover_pg_binaries", lambda: ambient)
    rec = Recorder()
    list_note_uri_pairs(cfg, platform="linux", run=rec, accepting=lambda h, p: True)
    assert rec.calls[0][0][0] == str(ambient.psql), "NEXUS_PG_BIN is the highest-priority leg"


# ── output ─────────────────────────────────────────────────────────────────────


def test_pairs_are_listed_with_the_changeset_state(tmp_path):
    rows = _rows(
        ("default", "1.1.9", "1.1.10", "chroma://knowledge__a/with|pipe"),
        ("default", "1.1.20", "1.1.21", "chroma://knowledge__a/b"),
    )
    report = _list(_config_dir(tmp_path), tmp_path, Recorder(pair_rows=rows, changeset=""))
    assert [p.chroma_row for p in report.pairs] == ["1.1.9", "1.1.20"]
    assert report.pairs[0].source_uri == "chroma://knowledge__a/with|pipe"
    text = "\n".join(report.lines())
    assert "pairs in the local cluster (127.0.0.1:54329" in text and ": 2" in text.splitlines()[0]
    assert "tenant_id\tchroma_row\tnx_note_row\tsource_uri" in text
    assert "Changeset note-uri-001-1: not run" in text


def test_a_source_uri_with_any_separator_like_character_cannot_corrupt_the_table(tmp_path):
    """A title can hold a tab, a newline, U+2028, \\x85 or \\x1c; the NUL framing is indifferent to all of them."""
    nasty = "chroma://knowledge__a/t\tab\nnewline\u2028ls\x85nel\x1cfs\r\nend"
    rows = _rows(("default", "1.1.9", "1.1.10", nasty), ("default", "1.1.20", "1.1.21", "chroma://knowledge__a/b"))
    report = _list(_config_dir(tmp_path), tmp_path, Recorder(pair_rows=rows))
    assert [p.source_uri for p in report.pairs] == [nasty, "chroma://knowledge__a/b"]


def test_no_pairs_prints_a_zero_count_and_the_changeset_row(tmp_path):
    report = _list(_config_dir(tmp_path), tmp_path, Recorder())
    lines = report.lines()
    assert lines[0].endswith(": 0") and "tenant_id" not in "\n".join(lines)
    assert lines[-1] == "Changeset note-uri-001-1: EXECUTED"


def test_unreadable_changeset_table_is_reported_not_fatal(tmp_path):
    rec = Recorder(fail=lambda argv: _failed("relation public.databasechangelog does not exist", rc=1) if CHANGESET_SQL in argv else None)
    report = _list(_config_dir(tmp_path), tmp_path, rec)
    assert report.changeset is None and "could not be read" in report.lines()[-1]


def test_a_truncated_record_is_an_error_not_a_misparse(tmp_path):
    with pytest.raises(NoteUriPairsError, match="whole number"):
        _list(_config_dir(tmp_path), tmp_path, Recorder(pair_rows="only\0two\0"))


# ── failures: a message, never a traceback ─────────────────────────────────────


def test_no_credentials_file_says_managed_installs_have_no_local_cluster(tmp_path):
    with pytest.raises(NoteUriPairsError, match="managed or cloud install"):
        _list(tmp_path, tmp_path, Recorder())


@pytest.mark.parametrize("port", ["", "abc", "\u00b2", "5432x"])
def test_credentials_without_a_usable_port(tmp_path, port):
    """`²` passes str.isdigit() and then makes int() raise: it must be the message, not a ValueError."""
    with pytest.raises(NoteUriPairsError, match="PG_PORT"):
        _list(_config_dir(tmp_path, port=port), tmp_path, Recorder())


def test_a_port_nothing_listens_on_is_decided_before_psql_runs(tmp_path):
    rec = Recorder()
    with pytest.raises(NoteUriPairsError, match="not accepting connections"):
        _list(_config_dir(tmp_path), tmp_path, rec, accepting=lambda h, p: False)
    assert rec.calls == [], "psql ran against a port that was already known to be closed"


def test_a_slow_first_accept_is_probed_again_before_the_port_is_called_closed(tmp_path, monkeypatch):
    # A loaded or EDR-filtered Windows box can miss one 0.3 s loopback accept (confirm review, non-blocker 1).
    monkeypatch.setattr("nexus.db.note_uri_pairs._PROBE_PAUSE_S", 0.0)
    cfg = _config_dir(tmp_path)
    answers = iter([False, True])
    rec = Recorder()
    _list(cfg, tmp_path, rec, accepting=lambda h, p: next(answers))
    assert rec.calls, "one missed accept was taken as a closed port"
    probes: list[int] = []
    with pytest.raises(NoteUriPairsError, match="not accepting connections"):
        _list(cfg, tmp_path, Recorder(), accepting=lambda h, p: probes.append(p) or False)
    assert len(probes) == 3, f"a closed port is probed exactly three times, not {len(probes)}"


def test_the_start_advice_is_local_installs_only(tmp_path):
    cfg = _config_dir(tmp_path)
    with pytest.raises(NoteUriPairsError) as local:
        _list(cfg, tmp_path, Recorder(), accepting=lambda h, p: False)
    assert "nx daemon service start" in str(local.value)
    with pytest.raises(NoteUriPairsError) as managed:
        _list(cfg, tmp_path, Recorder(), accepting=lambda h, p: False, local_install=False)
    assert "nx daemon service start" not in str(managed.value) and "leftover" in str(managed.value)
    rec = Recorder(fail=lambda argv: _failed(CONNECTION_REFUSED))
    with pytest.raises(NoteUriPairsError) as raced:
        _list(cfg, tmp_path, rec, local_install=False)
    assert "nx daemon service start" not in str(raced.value), "the psql-text path gives the same advice"


@pytest.mark.parametrize(
    ("stderr", "needle"),
    [
        (SCRAM_REFUSAL, "was refused"),
        ('ERROR:  relation "nexus.catalog_documents" does not exist', "has not migrated"),
        ("something unexpected", "failed"),
    ],
)
def test_psql_failures_become_messages(tmp_path, stderr, needle, monkeypatch):
    monkeypatch.setenv("USER", "postgres")  # one role tried: no fallback to muddy the wording
    with pytest.raises(NoteUriPairsError, match=needle):
        _list(_config_dir(tmp_path), tmp_path, Recorder(fail=lambda a: _failed(stderr, rc=1 if "relation" in stderr else 2)))


def test_timeout_root_and_missing_client_become_messages(tmp_path, monkeypatch):
    cfg = _config_dir(tmp_path)

    def timeout(argv, env):
        raise subprocess.TimeoutExpired(argv, 60)

    with pytest.raises(NoteUriPairsError, match="did not answer"):
        _list(cfg, tmp_path, timeout)

    def root(argv, env):
        raise mod.PgRootUserError("nx will not run as root")

    with pytest.raises(NoteUriPairsError, match="root"):
        _list(cfg, tmp_path, root)

    def gone(argv, env):
        raise FileNotFoundError(2, "No such file", argv[0])

    with pytest.raises(NoteUriPairsError, match="could not start psql"):
        _list(cfg, tmp_path, gone)

    def no_bundle():
        raise mod.PgBinaryNotFoundError("No nexus PostgreSQL bundle found.")

    monkeypatch.delenv("NEXUS_PG_BIN", raising=False)
    monkeypatch.setattr(mod, "extracted_bin_dir", lambda d: None)
    monkeypatch.setattr(mod, "discover_pg_binaries", no_bundle)
    with pytest.raises(NoteUriPairsError, match="never uses a host psql"):
        list_note_uri_pairs(cfg, platform="linux", run=Recorder())


# ── the CLI ────────────────────────────────────────────────────────────────────


def _cli(args, env):
    return CliRunner().invoke(main, ["daemon", "service", "note-uri-pairs", *args], env=env, catch_exceptions=True)


def _patched(monkeypatch, tmp_path, rec, **extra):
    real = mod.list_note_uri_pairs

    def call(d, **kw):
        kw.update(platform="linux", bins=_bins(tmp_path), run=rec, accepting=lambda h, p: True)
        kw.update(extra)
        return real(d, **kw)

    monkeypatch.setattr(mod, "list_note_uri_pairs", call)


def test_verb_is_registered_under_daemon_service():
    service = main.commands["daemon"].commands["service"]
    assert "note-uri-pairs" in service.commands
    assert not service.commands["note-uri-pairs"].hidden


def test_cli_prints_the_report(tmp_path, monkeypatch):
    cfg = _config_dir(tmp_path)
    _patched(monkeypatch, tmp_path, Recorder(pair_rows=_rows(("default", "1.1.9", "1.1.10", "chroma://knowledge__x/t"))))
    result = _cli(["--config-dir", str(cfg)], {})
    assert result.exit_code == 0, result.output
    assert "1.1.9\t1.1.10\tchroma://knowledge__x/t" in result.output
    assert PASSWORD not in result.output


def test_cli_check_exits_three_with_pairs_and_zero_without(tmp_path, monkeypatch):
    cfg = _config_dir(tmp_path)
    rec = Recorder(pair_rows=_rows(("default", "1.1.9", "1.1.10", "chroma://knowledge__x/t")))
    _patched(monkeypatch, tmp_path, rec)
    with_pairs = _cli(["--config-dir", str(cfg), "--check"], {})
    assert with_pairs.exit_code == 3 and "1.1.9" in with_pairs.output, "the listing is printed before the status"
    rec.pair_rows = ""
    none = _cli(["--config-dir", str(cfg), "--check"], {})
    assert none.exit_code == 0 and "pairs in the local cluster" in none.output and ": 0" in none.output


def test_cli_without_check_exits_zero_even_with_pairs(tmp_path, monkeypatch):
    cfg = _config_dir(tmp_path)
    _patched(monkeypatch, tmp_path, Recorder(pair_rows=_rows(("default", "1.1.9", "1.1.10", "chroma://knowledge__x/t"))))
    assert _cli(["--config-dir", str(cfg)], {}).exit_code == 0


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


def test_cli_config_dir_on_a_managed_install_never_advises_starting_the_leftover_cluster(tmp_path, monkeypatch):
    """--config-dir skips the managed refusal (a leftover cluster can be read), but must not say 'start it'."""
    cfg = _config_dir(tmp_path)
    _patched(monkeypatch, tmp_path, Recorder(), accepting=lambda h, p: False)
    result = _cli(["--config-dir", str(cfg)], {"NX_LOCAL": "0"})
    assert result.exit_code == 1 and isinstance(result.exception, SystemExit)
    assert "nx daemon service start" not in result.output and "leftover" in result.output
    local = _cli(["--config-dir", str(cfg)], {"NX_LOCAL": "1"})
    assert "nx daemon service start" in local.output


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


def test_runbook_local_steps_use_the_verb_and_hand_no_credentials_except_to_an_older_client():
    section = _runbook_section()
    assert "nx daemon service note-uri-pairs" in section
    older = section.index("**An older client")
    fallback = section[older:section.index("In the cloud, run the query below", older)]
    assert "pg-bundle/bundle/bin/psql" in fallback and "Linux and macOS only" in fallback
    main_flow = section.replace(fallback, "")
    for gone in ("sed -n 's/^PG_SUPERUSER_PASS", 'PGPASSWORD="$(sed', "pg-bundle/bundle/bin/psql", "A verb that runs this for you is planned"):
        assert gone not in main_flow, f"the runbook still hands the operator credential handling: {gone!r}"


def test_runbook_tells_an_old_client_to_upgrade_first():
    head = " ".join(_runbook_section()[:1500].split())
    assert "Upgrade the client first" in head and "7.97.0" in head
    assert "uv tool upgrade conexus" in head and "does not block" in head


def test_runbook_steps_two_and_three_gate_on_check():
    section = _runbook_section()
    assert section.count("note-uri-pairs --check") >= 2
    assert "must exit 0" in section and "exits 0 (no pair)" in section


# ── PAIR_SQL against the engine's guard ────────────────────────────────────────

_PREFIXES = re.compile(r"'((?:[a-z]+__))'")


def _prefixes(text: str) -> list[str]:
    return list(dict.fromkeys(_PREFIXES.findall(text)))


def test_pair_sql_routed_prefixes_are_the_clients_and_the_engines():
    """An engine change to the file-routed list reds this, instead of the verb listing a different set than the guard."""
    xml = BACKFILL_XML.read_text(encoding="utf-8")
    assert _prefixes(PAIR_SQL) == list(FILE_ROUTED_PREFIXES), "PAIR_SQL and aspect_readers.FILE_ROUTED_PREFIXES disagree"
    lines = re.findall(r"^\s+(?:direct|nx-note|export):\s+(.*)$", xml, re.M)
    assert len(lines) == 3, "the changeset's three predicate lines moved"
    for line in lines:
        assert _prefixes(line) == list(FILE_ROUTED_PREFIXES), f"the engine guard's list differs: {line[:80]}"


def test_pair_sql_predicates_are_the_guards():
    """Each predicate fragment PAIR_SQL relies on is in the changeset's own text; a change on either side reds this."""
    xml = BACKFILL_XML.read_text(encoding="utf-8")
    fragments = [
        "starts_with(source_uri, 'chroma://')",
        "split_part(substr(source_uri, 10), '/', 1)",
        "source_uri ~ '^nxexp://[^/]*/chroma://'",
        "substring(source_uri from '^nxexp://[^/]*/chroma://([^/]*)')",
        "'nx-note://' || substr(source_uri, 10)",
        "regexp_replace(source_uri, '^(nxexp://[^/]*)/chroma://', '\\1/nx-note://')",
    ]
    for frag in fragments:
        assert frag in PAIR_SQL, f"PAIR_SQL no longer spells {frag!r}"
        assert frag in xml, f"the changeset no longer spells {frag!r}"
    # the join: same tenant, a LIVE twin equal to the rewritten URI, and the original live too
    for clause in ("t.tenant_id = o.tenant_id", "t.deleted_at IS NULL", "t.source_uri = o.new_uri", "deleted_at IS NULL"):
        assert clause in PAIR_SQL, f"PAIR_SQL lost {clause!r}"
    for clause in ("t.tenant_id = o.tenant_id", "t.deleted_at IS NULL", "o.deleted_at IS NULL"):
        assert clause in xml, f"the changeset's guard no longer says {clause!r}"
