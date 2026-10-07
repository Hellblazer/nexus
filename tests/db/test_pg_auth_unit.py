# SPDX-License-Identifier: AGPL-3.0-or-later
"""Password authentication for the nx-managed cluster, no PostgreSQL needed
(nexus-ja4pq).

The cluster used to be created with ``initdb --auth=trust`` and every local OS
account could open a superuser session on the loopback port. These tests pin the
parts that need no server: the ``pg_hba.conf`` rewrite, the exact ``initdb``
invocation (password by file, never by argv), where the superuser password goes
for a client process, the migration's refusal to leave a half-converted cluster,
and the ``nx doctor`` row. The real-server behaviour is in
``test_pg_scram_auth.py``.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from nexus.db import pg_provision as pp
from nexus.db.pg_auth import (
    cluster_auth_state,
    harden_hba_text,
    hba_has_trust,
)

#: What ``initdb --auth=trust`` writes on POSIX (PostgreSQL 17).
_TRUST_HBA_POSIX = """\
# TYPE  DATABASE        USER            ADDRESS                 METHOD

# "local" is for Unix domain socket connections only
local   all             all                                     trust
# IPv4 local connections:
host    all             all             127.0.0.1/32            trust
# IPv6 local connections:
host    all             all             ::1/128                 trust
# Allow replication connections from localhost, by a user with the
# replication privilege.
local   replication     all                                     trust
host    replication     all             127.0.0.1/32            trust
host    replication     all             ::1/128                 trust
"""

#: Windows ``initdb`` writes no ``local`` lines (a ``local`` line there is a parse error).
_TRUST_HBA_WINDOWS = """\
# TYPE  DATABASE        USER            ADDRESS                 METHOD
host    all             all             127.0.0.1/32            trust
host    all             all             ::1/128                 trust
host    replication     all             127.0.0.1/32            trust
host    replication     all             ::1/128                 trust
"""

_SECRET = "s3cr3t-superuser-pass-0123456789abcdef"


# ── pg_hba.conf rewrite ────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", [_TRUST_HBA_POSIX, _TRUST_HBA_WINDOWS], ids=["posix", "windows"])
def test_every_active_trust_line_becomes_scram(text: str) -> None:
    assert hba_has_trust(text)
    out = harden_hba_text(text)
    assert not hba_has_trust(out)
    active = [ln for ln in out.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert active, "the rewrite must not drop the connection lines"
    assert all(ln.split()[-1] == "scram-sha-256" for ln in active), active


def test_the_rewrite_changes_only_the_method_token() -> None:
    out = harden_hba_text(_TRUST_HBA_POSIX)
    before = _TRUST_HBA_POSIX.splitlines()
    after = out.splitlines()
    assert len(before) == len(after)
    for b, a in zip(before, after):
        if b == a:
            continue
        assert b.replace("trust", "scram-sha-256") == a, (b, a)


def test_the_rewrite_is_idempotent_byte_for_byte() -> None:
    once = harden_hba_text(_TRUST_HBA_POSIX)
    assert harden_hba_text(once) == once


def test_a_trust_word_in_a_comment_or_a_non_trust_line_is_left_alone() -> None:
    text = (
        "# do not use trust on a shared box\n"
        "host all all 10.0.0.0/8 md5   # was trust\n"
        "host all all 127.0.0.1/32 reject\n"
    )
    assert not hba_has_trust(text)
    assert harden_hba_text(text) == text


def test_a_two_token_address_still_finds_the_method() -> None:
    text = "host all all 127.0.0.1 255.255.255.255 trust\n"
    assert hba_has_trust(text)
    assert harden_hba_text(text) == "host all all 127.0.0.1 255.255.255.255 scram-sha-256\n"


def test_options_after_the_method_survive() -> None:
    assert harden_hba_text("local all all trust map=m\n") == "local all all scram-sha-256 map=m\n"


@pytest.mark.parametrize(
    ("text", "state"),
    [
        (_TRUST_HBA_POSIX, "trust"),
        (harden_hba_text(_TRUST_HBA_POSIX), "scram"),
        ("# nothing but a comment\n", "other"),
        ("host all all 127.0.0.1/32 md5\n", "other"),
    ],
)
def test_cluster_auth_state_reads_the_file(tmp_path: Path, text: str, state: str) -> None:
    (tmp_path / "pg_hba.conf").write_text(text)
    assert cluster_auth_state(tmp_path) == state


def test_cluster_auth_state_is_absent_without_a_file(tmp_path: Path) -> None:
    assert cluster_auth_state(tmp_path) == "absent"


# ── initdb: password by file, never by argv ────────────────────────────────────


def _bins(tmp_path: Path) -> pp.PgBinaries:
    return pp.PgBinaries.from_dir(tmp_path / "bin")


def test_initdb_uses_scram_and_a_private_pwfile_that_is_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_run(cmd, *, check=True, capture=True, env=None, timeout):
        seen["cmd"] = list(cmd)
        pwarg = next(a for a in cmd if str(a).startswith("--pwfile="))
        pwfile = Path(str(pwarg).split("=", 1)[1])
        seen["pwfile"] = pwfile
        seen["content"] = pwfile.read_text()
        seen["mode"] = stat.S_IMODE(pwfile.stat().st_mode)
        seen["pwfile_dir"] = pwfile.parent
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(pp, "_run", fake_run)
    pgdata = tmp_path / "cfg" / "postgres"
    assert pp._init_cluster(_bins(tmp_path), pgdata, "alice", superuser_password=_SECRET) is True

    cmd = seen["cmd"]
    assert "--auth=scram-sha-256" in cmd
    assert not any("trust" in str(a) for a in cmd), "no trust anywhere in the invocation"
    assert not any(_SECRET in str(a) for a in cmd), "the password must never be on a command line"
    assert seen["content"] == _SECRET + "\n"
    if sys.platform != "win32":
        assert seen["mode"] == 0o600, "owner-only before the secret is written"
    assert seen["pwfile_dir"] == pgdata.parent, "beside the data dir: initdb refuses a non-empty target"
    assert not Path(str(seen["pwfile"])).exists(), "the pwfile is deleted after initdb"
    assert cmd[cmd.index("--username") + 1] == "alice"


def test_the_pwfile_is_deleted_when_initdb_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Path] = {}

    def failing_run(cmd, *, check=True, capture=True, env=None, timeout):
        seen["pwfile"] = Path(next(str(a) for a in cmd if str(a).startswith("--pwfile=")).split("=", 1)[1])
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(pp, "_run", failing_run)
    with pytest.raises(subprocess.CalledProcessError):
        pp._init_cluster(_bins(tmp_path), tmp_path / "cfg" / "postgres", "alice", superuser_password=_SECRET)
    assert not seen["pwfile"].exists()


def test_an_existing_cluster_is_not_reinitialised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pgdata = tmp_path / "postgres"
    pgdata.mkdir()
    (pgdata / "PG_VERSION").write_text("17\n")
    monkeypatch.setattr(pp, "_run", lambda *a, **k: pytest.fail("initdb must not run"))
    assert pp._init_cluster(_bins(tmp_path), pgdata, "alice", superuser_password=_SECRET) is False


# ── clients: password in the child's environment, never in argv ───────────────


def _capture_run(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []

    def fake_run(cmd, *, check=True, capture=True, env=None, timeout):
        calls.append({"cmd": list(cmd), "env": env})
        return subprocess.CompletedProcess(cmd, 0, " 1 row\n", "")

    monkeypatch.setattr(pp, "_run", fake_run)
    return calls


def test_a_scoped_superuser_password_reaches_psql_through_pgpassword_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _capture_run(monkeypatch)
    with pp.superuser_auth(_SECRET):
        pp._psql(_bins(tmp_path), 5432, "postgres", "alice", "SELECT 1")
        pp._psql_tuples(_bins(tmp_path), 5432, "postgres", "alice", "SELECT 1")
        pp._create_db(_bins(tmp_path), 5432, "alice")
    assert len(calls) == 3
    for call in calls:
        assert call["env"]["PGPASSWORD"] == _SECRET
        assert not any(_SECRET in str(a) for a in call["cmd"])
        assert "-w" in call["cmd"], "never prompt: a missing password must fail, not wait on a tty"


def test_no_scope_means_no_password_in_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "empty"))
    calls = _capture_run(monkeypatch)
    pp._psql(_bins(tmp_path), 5432, "postgres", "alice", "SELECT 1")
    assert calls[0]["env"] is None


def test_the_scope_ends_with_the_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "empty"))
    with pp.superuser_auth(_SECRET):
        assert pp._superuser_password(5432) == _SECRET
    assert pp._superuser_password(5432) is None


def test_ambient_credentials_apply_only_to_their_own_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pg_credentials").write_text(f"PG_PORT=6543\nPG_SUPERUSER_PASS={_SECRET}\n")
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    assert pp._superuser_password(6543) == _SECRET
    assert pp._superuser_password(6544) is None, "another cluster must not borrow this password"


def test_psql_secret_keeps_the_sql_off_argv_and_deletes_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_run(cmd, *, check=True, capture=True, env=None, timeout):
        sql_path = Path(cmd[cmd.index("-f") + 1])
        seen["path"] = sql_path
        seen["text"] = sql_path.read_text()
        seen["mode"] = stat.S_IMODE(sql_path.stat().st_mode)
        seen["cmd"] = list(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(pp, "_run", fake_run)
    sql = f"ALTER ROLE x PASSWORD '{_SECRET}'"
    pp._psql_secret(_bins(tmp_path), 5432, "postgres", "alice", sql)
    assert seen["text"].strip() == sql
    assert not any(_SECRET in str(a) for a in seen["cmd"])
    assert "-c" not in seen["cmd"]
    if sys.platform != "win32":
        assert seen["mode"] == 0o600
    assert not Path(str(seen["path"])).exists()


def test_psql_secret_deletes_the_file_when_psql_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Path] = {}

    def failing(cmd, *, check=True, capture=True, env=None, timeout):
        seen["path"] = Path(cmd[cmd.index("-f") + 1])
        raise subprocess.CalledProcessError(2, cmd)

    monkeypatch.setattr(pp, "_run", failing)
    with pytest.raises(subprocess.CalledProcessError):
        pp._psql_secret(_bins(tmp_path), 5432, "postgres", "alice", f"SELECT '{_SECRET}'")
    assert not seen["path"].exists()


def test_sql_quoting_survives_a_hostile_value() -> None:
    assert pp._sql_literal("a'b") == "'a''b'"
    assert pp._sql_ident('we"ird') == '"we""ird"'


# ── credentials file ───────────────────────────────────────────────────────────


def test_write_credentials_records_the_superuser_password_and_keeps_it_private(tmp_path: Path) -> None:
    creds = tmp_path / "pg_credentials"
    pp._write_credentials(
        creds, tmp_path / "postgres", 5444, "adm", "svc", "tok", "diag", superuser_pass=_SECRET,
    )
    parsed = pp._read_credentials(creds)
    assert parsed["PG_SUPERUSER_PASS"] == _SECRET
    assert parsed["PG_PORT"] == "5444"
    if sys.platform != "win32":
        assert stat.S_IMODE(creds.stat().st_mode) == 0o600


def test_persist_superuser_password_appends_once(tmp_path: Path) -> None:
    creds = tmp_path / "pg_credentials"
    creds.write_text("PG_PORT=5444\nNX_DB_PASS=svc\n")
    pp._persist_superuser_password(creds, "first")
    pp._persist_superuser_password(creds, "second")
    parsed = pp._read_credentials(creds)
    assert parsed["PG_SUPERUSER_PASS"] == "first", "a re-run must not shadow the recorded password"
    assert parsed["NX_DB_PASS"] == "svc"
    assert creds.read_text().count("PG_SUPERUSER_PASS") == 1
    if sys.platform != "win32":
        assert stat.S_IMODE(creds.stat().st_mode) == 0o600


def test_pending_credentials_carry_no_port_so_they_never_look_provisioned(tmp_path: Path) -> None:
    creds = tmp_path / "pg_credentials"
    pp._write_pending_credentials(
        creds, admin_pass="a", svc_pass="s", diag_pass="d", service_token="t", superuser_pass=_SECRET,
        port=5432,
    )
    parsed = pp._read_credentials(creds)
    assert parsed["PG_SUPERUSER_PASS"] == _SECRET
    assert parsed["PG_PORT_PENDING"] == "5432", "a crash leaves the postmaster on this port"
    assert "PG_PORT" not in parsed, "the fast idempotency path keys on PG_PORT"
    assert not pp.is_provisioned(tmp_path)


def test_the_jvm_env_allowlist_does_not_carry_the_superuser_password() -> None:
    import inspect

    from nexus.daemon import storage_service_daemon as ssd

    src = inspect.getsource(ssd)
    assert "PG_SUPERUSER_PASS" not in src, (
        "the supervisor must not hand the engine the superuser's password; the engine "
        "connects as nexus_admin and nexus_svc only"
    )


# ── migration: never a half-converted cluster ──────────────────────────────────


class _Cluster:
    """A pgdata holding a legacy trust ``pg_hba.conf`` and a credentials file."""

    def __init__(self, root: Path) -> None:
        self.pgdata = root / "postgres"
        self.pgdata.mkdir()
        (self.pgdata / "pg_hba.conf").write_text(_TRUST_HBA_POSIX)
        self.creds = root / "pg_credentials"
        self.creds.write_text(
            "PG_PORT=5555\nNX_DB_ADMIN_PASS=adm\nNX_DB_PASS=svc\nNX_DB_DIAG_PASS=diag\n"
        )
        self.reloads = 0

    @property
    def hba(self) -> str:
        return (self.pgdata / "pg_hba.conf").read_text()


@pytest.fixture
def legacy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Cluster:
    c = _Cluster(tmp_path)

    def reload_(bins, pgdata):
        c.reloads += 1

    monkeypatch.setattr(pp, "_pg_reload", reload_)
    monkeypatch.setattr(pp, "_role_exists", lambda *a, **k: True)
    monkeypatch.setattr(pp, "_psql_secret", lambda *a, **k: subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(pp, "_confirm_scram", lambda *a, **k: None)
    return c


def _harden(c: _Cluster, tmp_path: Path) -> str:
    return pp.harden_cluster_auth(_bins(tmp_path), c.pgdata, 5555, "alice", c.creds, sleep=lambda s: None)


def test_migration_rewrites_the_file_and_reloads(legacy: _Cluster, tmp_path: Path) -> None:
    assert _harden(legacy, tmp_path) == pp.AUTH_MIGRATED
    assert not hba_has_trust(legacy.hba)
    assert legacy.reloads == 1
    assert pp._read_credentials(legacy.creds)["PG_SUPERUSER_PASS"], "recorded for later runs"


def test_migration_on_a_scram_cluster_does_nothing(legacy: _Cluster, tmp_path: Path) -> None:
    _harden(legacy, tmp_path)
    reloads = legacy.reloads
    before = (legacy.pgdata / "pg_hba.conf").read_bytes()
    assert _harden(legacy, tmp_path) == pp.AUTH_ALREADY_SCRAM
    assert (legacy.pgdata / "pg_hba.conf").read_bytes() == before
    assert legacy.reloads == reloads, "a no-op must not even reload"


def test_a_failed_password_step_leaves_trust_untouched(
    legacy: _Cluster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a, **k):
        raise subprocess.CalledProcessError(1, ["psql"], stderr="boom")

    monkeypatch.setattr(pp, "_psql_secret", boom)
    before = (legacy.pgdata / "pg_hba.conf").read_bytes()
    assert _harden(legacy, tmp_path) == pp.AUTH_FAILED
    assert (legacy.pgdata / "pg_hba.conf").read_bytes() == before, "never half-migrated"
    assert legacy.reloads == 0


def test_a_failed_confirmation_restores_the_original_file_and_reloads(
    legacy: _Cluster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pp, "_confirm_scram", lambda *a, **k: "role nexus_svc cannot authenticate")
    before = (legacy.pgdata / "pg_hba.conf").read_bytes()
    assert _harden(legacy, tmp_path) == pp.AUTH_FAILED
    assert (legacy.pgdata / "pg_hba.conf").read_bytes() == before
    assert legacy.reloads == 2, "one reload to apply scram, one to take it back"


def test_a_failed_reload_restores_the_original_file(
    legacy: _Cluster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    def flaky_reload(bins, pgdata):
        calls["n"] += 1
        if calls["n"] == 1:
            raise subprocess.CalledProcessError(1, ["pg_ctl"])

    monkeypatch.setattr(pp, "_pg_reload", flaky_reload)
    before = (legacy.pgdata / "pg_hba.conf").read_bytes()
    assert _harden(legacy, tmp_path) == pp.AUTH_FAILED
    assert (legacy.pgdata / "pg_hba.conf").read_bytes() == before


def test_migration_never_raises_on_an_unreadable_file(legacy: _Cluster, tmp_path: Path) -> None:
    (legacy.pgdata / "pg_hba.conf").unlink()
    assert _harden(legacy, tmp_path) == pp.AUTH_FAILED


def test_the_alter_statements_carry_the_recorded_passwords_in_one_session(
    legacy: _Cluster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []
    monkeypatch.setattr(
        pp, "_psql_secret", lambda bins, port, db, user, sql, **k: sent.append(sql) or subprocess.CompletedProcess([], 0, "", "")
    )
    _harden(legacy, tmp_path)
    (sql,) = sent
    assert sql.startswith("SET password_encryption = 'scram-sha-256';")
    assert 'ALTER ROLE "alice" PASSWORD' in sql
    for role, pw in (("nexus_admin", "adm"), ("nexus_svc", "svc"), ("nexus_diag", "diag")):
        assert f"ALTER ROLE \"{role}\" PASSWORD '{pw}';" in sql, role


def test_the_password_is_recorded_before_the_cluster_is_changed(
    legacy: _Cluster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash after the ALTER must not leave a database password nothing recorded."""
    order: list[str] = []

    def alter(*a, **k):
        order.append("alter")
        assert pp._read_credentials(legacy.creds).get("PG_SUPERUSER_PASS"), "recorded before the ALTER"
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(pp, "_psql_secret", alter)
    _harden(legacy, tmp_path)
    assert order == ["alter"]


def test_the_confirm_step_wants_a_refused_passwordless_login_then_working_passwords(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[tuple[str, str | None]] = []
    state = {"refused_after": 3}

    def login(bins, port, user, password):
        attempts.append((user, password))
        if password is None:
            state["refused_after"] -= 1
            return state["refused_after"] > 0  # accepted until the reload lands
        return True

    monkeypatch.setattr(pp, "_login_works", login)
    assert pp._confirm_scram(_bins(tmp_path), 5555, [("alice", "p1"), ("nexus_svc", "p2")], sleep=lambda s: None) is None
    assert attempts[:3] == [("alice", None)] * 3
    assert attempts[3:] == [("alice", "p1"), ("nexus_svc", "p2")]


def test_the_confirm_step_reports_a_reload_that_never_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pp, "_login_works", lambda *a: True)
    reason = pp._confirm_scram(
        _bins(tmp_path), 5555, [("alice", "p")], sleep=lambda s: None, timeout_s=0.0,
    )
    assert reason and "password-less" in reason


def test_the_confirm_step_reports_a_role_whose_password_does_not_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A password-less login is refused (the reload landed); nexus_svc's password is not.
    monkeypatch.setattr(
        pp, "_login_works",
        lambda bins, port, user, password: password is not None and user != "nexus_svc",
    )
    reason = pp._confirm_scram(_bins(tmp_path), 5555, [("alice", "p"), ("nexus_svc", "q")], sleep=lambda s: None)
    assert reason and "nexus_svc" in reason


def test_no_password_appears_in_a_logged_failure(
    legacy: _Cluster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import structlog

    def boom(*a, **k):
        raise RuntimeError(f"ALTER ROLE x PASSWORD '{_SECRET}' failed")

    monkeypatch.setattr(pp, "_psql_secret", boom)
    with structlog.testing.capture_logs() as logs:
        assert _harden(legacy, tmp_path) == pp.AUTH_FAILED
    assert _SECRET not in repr(logs)


# ── nx doctor ──────────────────────────────────────────────────────────────────


def _doctor_dir(tmp_path: Path, hba: str | None, *, pg_data: bool = True) -> Path:
    pgdata = tmp_path / "postgres"
    pgdata.mkdir()
    if hba is not None:
        (pgdata / "pg_hba.conf").write_text(hba)
    lines = ["PG_PORT=5555"]
    if pg_data:
        lines.append(f"PG_DATA={pgdata}")
    (tmp_path / "pg_credentials").write_text("\n".join(lines) + "\n")
    return tmp_path


def test_doctor_row_is_ok_on_a_scram_cluster(tmp_path: Path) -> None:
    from nexus.health import _check_local_pg_auth

    (row,) = _check_local_pg_auth(_doctor_dir(tmp_path, harden_hba_text(_TRUST_HBA_POSIX)))
    assert row.ok is True
    assert "scram-sha-256" in row.detail


def test_doctor_row_fails_loudly_on_a_trust_cluster(tmp_path: Path) -> None:
    from nexus.health import _check_local_pg_auth

    (row,) = _check_local_pg_auth(_doctor_dir(tmp_path, _TRUST_HBA_POSIX))
    assert row.ok is False
    assert "trust" in row.detail
    assert any("nx daemon service start" in f for f in row.fix_suggestions)


def test_doctor_row_is_not_applicable_where_there_is_no_local_cluster(tmp_path: Path) -> None:
    from nexus.health import _check_local_pg_auth

    assert _check_local_pg_auth(tmp_path) == [], "a virgin box has nothing to check"
    (tmp_path / "pg_credentials").write_text("NX_DB_URL=jdbc:postgresql://managed/x\n")
    assert _check_local_pg_auth(tmp_path) == [], "managed Postgres is not ours to check"


def test_doctor_row_does_not_vanish_when_the_hba_file_is_unreadable(tmp_path: Path) -> None:
    from nexus.health import _check_local_pg_auth

    (row,) = _check_local_pg_auth(_doctor_dir(tmp_path, None))
    assert row.ok is False
    assert "pg_hba.conf" in row.detail


def test_doctor_row_is_registered_in_the_doctor_run() -> None:
    import inspect

    import nexus.health as h

    assert "_check_local_pg_auth()" in inspect.getsource(h)
