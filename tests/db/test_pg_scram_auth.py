# SPDX-License-Identifier: AGPL-3.0-or-later
"""Password authentication on a REAL bundled cluster (nexus-ja4pq).

Drives ``provision()`` against the PostgreSQL bundle, with no engine and no JVM
(``no_service_jar``). The unit half, which needs no server, is
``test_pg_auth_unit.py``.

What this proves that the unit half cannot:

* a fresh cluster refuses a password-less connection as the superuser AND as
  every nexus role, and accepts each with the password recorded in
  ``pg_credentials``;
* a LEGACY ``trust`` cluster is converted on the next provision (the path every
  service start takes), keeps the passwords the engine already carries, and a
  second run changes nothing;
* a failed or unconfirmed conversion leaves the cluster on ``trust``, usable;
* a crash between ``initdb`` and the final credentials write does not lose the
  superuser's password.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from nexus._install.layout_core import exe_name
from nexus._winsec import owner_only_problem
from nexus.db import pg_provision as pp
from nexus.db.pg_auth import cluster_auth_state, hba_has_trust
from tests.db._service_fixture import pg_bin_dir

_PG_BIN = pg_bin_dir()
# initdb.exe on Windows: a bare "initdb" never exists there (nexus-ja4pq).
_INITDB = _PG_BIN / exe_name("initdb")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.no_service_jar,
    pytest.mark.skipif(
        not _INITDB.exists(),
        reason=f"skipped: nexus-pg bundle self-provisioning failed (no {_INITDB}). "
               "NOT a missing host PostgreSQL: these tests never use one.",
    ),
]


@pytest.fixture(scope="module", autouse=True)
def _pin_discovery_to_the_built_bundle():
    """Point the product's own discovery at the PG we build (see test_pg_provision.py)."""
    if os.environ.get("NEXUS_PG_BIN", "").strip():
        yield
        return
    mp = pytest.MonkeyPatch()
    mp.setenv("NEXUS_PG_BIN", str(_PG_BIN))
    try:
        yield
    finally:
        mp.undo()


# ── helpers ────────────────────────────────────────────────────────────────────


def _os_user() -> str:
    return pp.bootstrap_superuser()


def _login(bins: pp.PgBinaries, port: int, user: str, password: str | None, db: str = "postgres") -> subprocess.CompletedProcess:
    """One psql session as *user*. Never prompts, never reads ~/.pgpass."""
    env = {k: v for k, v in os.environ.items() if k != "PGPASSWORD"}
    env["PGPASSFILE"] = os.devnull
    if password is not None:
        env["PGPASSWORD"] = password
    return subprocess.run(
        [str(bins.psql), "-X", "-w", "-h", "127.0.0.1", "-p", str(port), "-U", user,
         "-d", db, "-t", "-A", "-c", "SELECT current_user"],
        capture_output=True, text=True, env=env, timeout=60,
    )


def _not_owner_only(path: Path) -> str | None:
    """Why *path* is not exactly what ``restrict_to_owner`` makes, or None.

    Stricter than ``owner_only_problem``, which also admits SYSTEM and
    Administrators: a file that merely inherited a tight parent DACL would pass
    that, so on Windows this demands the single ACE naming the current user."""
    if sys.platform == "win32":
        from nexus._winsec import _windows_dacl_trustees, _windows_user_sid  # noqa: PLC0415 — Windows-only

        trustees = _windows_dacl_trustees(str(path))
        user = _windows_user_sid()
        return None if trustees == [user] else f"DACL grants {trustees}, expected only {user}"
    mode = stat.S_IMODE(path.stat().st_mode)
    return None if mode == 0o600 else f"mode {oct(mode)}"


def _wait_for(predicate, *, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


class _Box:
    """A provisioned cluster in a private config dir, stopped on teardown."""

    def __init__(self, config_dir: Path, bins: pp.PgBinaries) -> None:
        self.config_dir = config_dir
        self.bins = bins
        self.pgdata = config_dir / "postgres"
        self.creds_path = config_dir / "pg_credentials"

    @property
    def creds(self) -> dict[str, str]:
        return pp._read_credentials(self.creds_path)

    @property
    def port(self) -> int:
        return int(self.creds["PG_PORT"])

    @property
    def hba_bytes(self) -> bytes:
        return (self.pgdata / "pg_hba.conf").read_bytes()

    def reload(self) -> None:
        subprocess.run([str(self.bins.pg_ctl), "-D", str(self.pgdata), "reload"],
                       capture_output=True, check=True, timeout=30)

    def make_legacy_trust(self) -> None:
        """Turn this cluster into what an install from before nexus-ja4pq looks like:
        ``trust`` everywhere, no recorded superuser password, none set in the database."""
        creds = self.creds
        super_pass = creds["PG_SUPERUSER_PASS"]
        text = self.hba_bytes.decode().replace("scram-sha-256", "trust")
        (self.pgdata / "pg_hba.conf").write_text(text)
        self.reload()
        assert _wait_for(lambda: _login(self.bins, self.port, _os_user(), None).returncode == 0)
        # Pre-ja4pq the superuser had no password; the three roles did.
        with pp.superuser_auth(super_pass):
            pp._psql(self.bins, self.port, "postgres", _os_user(), f'ALTER ROLE "{_os_user()}" PASSWORD NULL')
        lines = [ln for ln in self.creds_path.read_text().splitlines() if not ln.startswith("PG_SUPERUSER_PASS=")]
        self.creds_path.write_text("\n".join(lines) + "\n")
        assert "PG_SUPERUSER_PASS" not in self.creds


def _stop(bins: pp.PgBinaries, pgdata: Path) -> None:
    try:
        subprocess.run([str(bins.pg_ctl), "-D", str(pgdata), "-m", "immediate", "stop"],
                       capture_output=True, check=False, timeout=30)
    except Exception:  # noqa: BLE001 — teardown must not reraise
        pass


@pytest.fixture(scope="module")
def bins() -> pp.PgBinaries:
    return pp.discover_pg_binaries()


@pytest.fixture
def box(bins: pp.PgBinaries, tmp_path: Path):
    """A freshly provisioned cluster, one per test."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    try:
        pp.provision(config_dir, force_new_port=True)
    except subprocess.CalledProcessError as exc:
        # CalledProcessError's str() omits stderr, which is the part that says why.
        pg_log = config_dir / "postgres" / "pg.log"
        tail = pg_log.read_text()[-2000:] if pg_log.exists() else "(no pg.log)"
        raise AssertionError(f"provision failed: {exc}\nstderr: {exc.stderr!r}\npg.log tail:\n{tail}") from exc
    b = _Box(config_dir, bins)
    try:
        yield b
    finally:
        _stop(bins, b.pgdata)


# ── a fresh cluster ────────────────────────────────────────────────────────────


class TestFreshClusterDemandsPasswords:
    def test_pg_hba_has_no_trust_line(self, box: _Box) -> None:
        assert not hba_has_trust(box.hba_bytes.decode())
        assert cluster_auth_state(box.pgdata) == "scram"

    def test_the_superuser_is_refused_without_a_password(self, box: _Box) -> None:
        res = _login(box.bins, box.port, _os_user(), None)
        assert res.returncode != 0, "a trust cluster would have admitted this"
        assert "password" in res.stderr.lower()

    def test_the_superuser_is_refused_a_wrong_password(self, box: _Box) -> None:
        assert _login(box.bins, box.port, _os_user(), "not-the-password").returncode != 0

    def test_the_superuser_authenticates_with_the_recorded_password(self, box: _Box) -> None:
        res = _login(box.bins, box.port, _os_user(), box.creds["PG_SUPERUSER_PASS"])
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == _os_user()

    @pytest.mark.parametrize(
        ("role", "key"),
        [("nexus_admin", "NX_DB_ADMIN_PASS"), ("nexus_svc", "NX_DB_PASS"), ("nexus_diag", "NX_DB_DIAG_PASS")],
    )
    def test_every_nexus_role_needs_and_accepts_its_own_password(self, box: _Box, role: str, key: str) -> None:
        assert _login(box.bins, box.port, role, None, db="nexus").returncode != 0
        res = _login(box.bins, box.port, role, box.creds[key], db="nexus")
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == role

    def test_the_stored_passwords_are_scram_verifiers(self, box: _Box) -> None:
        with pp.superuser_auth(box.creds["PG_SUPERUSER_PASS"]):
            out = pp._psql_tuples(
                box.bins, box.port, "postgres", _os_user(),
                "SELECT count(*) FROM pg_authid WHERE rolpassword IS NOT NULL "
                "AND rolpassword NOT LIKE 'SCRAM-SHA-256$%'",
            )
        assert out == "0", "an md5 or plaintext verifier would defeat the point"

    def test_the_credentials_file_is_owner_only_and_carries_the_superuser_password(self, box: _Box) -> None:
        assert box.creds["PG_SUPERUSER_PASS"]
        if sys.platform != "win32":
            assert stat.S_IMODE(box.creds_path.stat().st_mode) == 0o600
        # Windows: the mode bits say 0o666 for every file; the DACL is the check.
        assert owner_only_problem(box.creds_path, box.creds_path.stat().st_mode) is None
        assert _not_owner_only(box.creds_path) is None

    def test_no_pwfile_or_credentials_scratch_is_left_behind(self, box: _Box) -> None:
        leftovers = sorted(p.name for p in box.config_dir.iterdir() if p.name.startswith((".pg_pwfile_", ".pg_creds_", ".pg_hba_")))
        assert leftovers == []

    def test_a_second_provision_is_a_no_op(self, box: _Box) -> None:
        before = box.hba_bytes
        creds_before = box.creds
        res = pp.provision(box.config_dir)
        assert res.already_provisioned is True
        assert res.auth_migrated is False
        assert box.hba_bytes == before
        assert box.creds["PG_SUPERUSER_PASS"] == creds_before["PG_SUPERUSER_PASS"]
        assert box.creds["NX_DB_PASS"] == creds_before["NX_DB_PASS"]


class TestSecretFilesArePrivateWhileInUse:
    """The initdb ``--pwfile`` and every ``_psql_secret`` file, seen at the moment
    the client reads it: present, owner-only (the DACL on Windows), and gone after.

    Observed through the module's one subprocess choke point, ``_run``, so the
    check runs on the real file the real initdb and psql open."""

    def test_pwfile_and_sql_files_are_owner_only_in_use_and_deleted_after(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        seen: list[tuple[str, Path, bool, str | None]] = []
        real_run = pp._run

        def spy(cmd, **kw):
            for i, arg in enumerate(cmd):
                path: Path | None = None
                kind = ""
                if arg.startswith("--pwfile="):
                    kind, path = "pwfile", Path(arg.split("=", 1)[1])
                elif arg == "-f" and i + 1 < len(cmd):
                    kind, path = "sql", Path(cmd[i + 1])
                if path is not None:
                    present = path.exists()
                    problem = _not_owner_only(path) if present else "missing"
                    seen.append((kind, path, present, problem))
            return real_run(cmd, **kw)

        monkeypatch.setattr(pp, "_run", spy)
        pgdata = config_dir / "postgres"
        try:
            pp.provision(config_dir, force_new_port=True)
        finally:
            _stop(pp.discover_pg_binaries(), pgdata)

        kinds = [k for k, *_ in seen]
        assert kinds.count("pwfile") == 1, seen
        assert kinds.count("sql") >= 1, "CREATE ROLE ... PASSWORD must go through _psql_secret"
        for kind, path, present, problem in seen:
            assert present, f"{kind} {path} absent when the client read it"
            assert problem is None, f"{kind} {path}: {problem}"
            assert not path.exists(), f"{kind} {path} left behind"
            if kind == "pwfile":
                assert path.parent == pgdata.parent, "beside the data directory, never inside it"


# ── an existing trust cluster ──────────────────────────────────────────────────


class TestLegacyTrustClusterIsMigrated:
    def test_the_next_provision_converts_it_and_keeps_the_engines_passwords(self, box: _Box) -> None:
        engine_creds = {k: box.creds[k] for k in ("NX_DB_ADMIN_PASS", "NX_DB_PASS", "NX_DB_DIAG_PASS")}
        box.make_legacy_trust()
        assert cluster_auth_state(box.pgdata) == "trust"
        assert _login(box.bins, box.port, _os_user(), None).returncode == 0, "legacy precondition: trust"

        res = pp.provision(box.config_dir)

        assert res.already_provisioned is True
        assert res.auth_migrated is True
        assert cluster_auth_state(box.pgdata) == "scram"
        assert _wait_for(lambda: _login(box.bins, box.port, _os_user(), None).returncode != 0)
        recorded = box.creds["PG_SUPERUSER_PASS"]
        assert _login(box.bins, box.port, _os_user(), recorded).returncode == 0
        for role, key in (("nexus_admin", "NX_DB_ADMIN_PASS"), ("nexus_svc", "NX_DB_PASS"), ("nexus_diag", "NX_DB_DIAG_PASS")):
            assert box.creds[key] == engine_creds[key], "the engine's credentials must not move"
            assert _login(box.bins, box.port, role, engine_creds[key], db="nexus").returncode == 0, role

    def test_converting_twice_changes_nothing(self, box: _Box) -> None:
        box.make_legacy_trust()
        pp.provision(box.config_dir)
        hba, creds = box.hba_bytes, box.creds
        res = pp.provision(box.config_dir)
        assert res.auth_migrated is False
        assert box.hba_bytes == hba
        assert box.creds == creds

    def test_a_stopped_legacy_cluster_is_converted_on_the_full_path(self, box: _Box) -> None:
        """A cluster that is stopped misses the fast path and lands on the full one."""
        box.make_legacy_trust()
        port = box.port
        _stop(box.bins, box.pgdata)
        assert _wait_for(lambda: not pp._port_accepting("127.0.0.1", port))
        res = pp.provision(box.config_dir)
        assert res.auth_migrated is True
        assert cluster_auth_state(box.pgdata) == "scram"
        assert _login(box.bins, box.port, _os_user(), box.creds["PG_SUPERUSER_PASS"]).returncode == 0

    def test_a_failed_conversion_leaves_trust_in_place_and_the_cluster_usable(
        self, box: _Box, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        box.make_legacy_trust()
        before = box.hba_bytes

        def boom(*a, **k):
            raise subprocess.CalledProcessError(1, ["psql"], stderr="simulated failure")

        monkeypatch.setattr(pp, "_psql_secret", boom)
        outcome = pp.harden_cluster_auth(box.bins, box.pgdata, box.port, _os_user(), box.creds_path)
        assert outcome == pp.AUTH_FAILED
        assert box.hba_bytes == before
        assert _login(box.bins, box.port, _os_user(), None).returncode == 0, "no lockout"
        for role, key in (("nexus_admin", "NX_DB_ADMIN_PASS"), ("nexus_svc", "NX_DB_PASS")):
            assert _login(box.bins, box.port, role, box.creds[key], db="nexus").returncode == 0

    def test_a_conversion_that_cannot_be_confirmed_is_rolled_back_to_trust(
        self, box: _Box, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        box.make_legacy_trust()
        before = box.hba_bytes
        monkeypatch.setattr(pp, "_confirm_scram", lambda *a, **k: "simulated: nexus_svc cannot authenticate")
        outcome = pp.harden_cluster_auth(box.bins, box.pgdata, box.port, _os_user(), box.creds_path)
        assert outcome == pp.AUTH_FAILED
        assert box.hba_bytes == before
        assert _wait_for(lambda: _login(box.bins, box.port, _os_user(), None).returncode == 0), (
            "the restored file must be live again, not just on disk"
        )

    def test_a_failure_does_not_stop_the_idempotent_rerun(self, box: _Box, monkeypatch: pytest.MonkeyPatch) -> None:
        box.make_legacy_trust()
        monkeypatch.setattr(pp, "_psql_secret", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("simulated")))
        res = pp.provision(box.config_dir)
        assert res.already_provisioned is True
        assert res.auth_migrated is False
        assert cluster_auth_state(box.pgdata) == "trust"
        monkeypatch.undo()
        res = pp.provision(box.config_dir)
        assert res.auth_migrated is True, "the next start finishes what the failed one could not"


# ── crash safety and the locked case ───────────────────────────────────────────


class TestPasswordIsNeverStranded:
    def test_a_crash_after_initdb_keeps_the_superuser_password(
        self, bins: pp.PgBinaries, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        pgdata = config_dir / "postgres"

        def crash(*a, **k):
            raise RuntimeError("simulated crash after initdb")

        with monkeypatch.context() as m:
            m.setattr(pp, "_create_db", crash)
            with pytest.raises(RuntimeError, match="simulated crash"):
                pp.provision(config_dir, force_new_port=True)
        try:
            pending = pp._read_credentials(config_dir / "pg_credentials")
            assert pending["PG_SUPERUSER_PASS"], "recorded before initdb baked it in"
            assert "PG_PORT" not in pending, "must not look provisioned to the fast path"
            assert cluster_auth_state(pgdata) == "scram"

            res = pp.provision(config_dir)  # the retry finishes the job

            final = pp._read_credentials(config_dir / "pg_credentials")
            assert final["PG_SUPERUSER_PASS"] == pending["PG_SUPERUSER_PASS"]
            assert final["NX_DB_PASS"] == pending["NX_DB_PASS"]
            port = int(final["PG_PORT"])
            assert _login(bins, port, _os_user(), final["PG_SUPERUSER_PASS"]).returncode == 0
            assert _login(bins, port, "nexus_svc", final["NX_DB_PASS"], db="nexus").returncode == 0
            assert res.cluster_created is False
        finally:
            _stop(bins, pgdata)

    def test_a_scram_cluster_with_no_recorded_password_is_refused_not_loosened(self, box: _Box) -> None:
        lines = [ln for ln in box.creds_path.read_text().splitlines() if not ln.startswith("PG_SUPERUSER_PASS=")]
        box.creds_path.write_text("\n".join(lines) + "\n")
        before = box.hba_bytes
        with pytest.raises(pp.PgAuthLockedError, match="PG_SUPERUSER_PASS"):
            pp.provision(box.config_dir)
        assert box.hba_bytes == before, "nx must never weaken pg_hba.conf to recover"

    def test_the_locked_case_on_the_full_path_is_refused_before_anything_starts(self, box: _Box) -> None:
        port = box.port
        _stop(box.bins, box.pgdata)
        assert _wait_for(lambda: not pp._port_accepting("127.0.0.1", port))
        box.creds_path.unlink()
        before = box.hba_bytes
        with pytest.raises(pp.PgAuthLockedError):
            pp.provision(box.config_dir)
        assert box.hba_bytes == before
        assert not pp._port_accepting("127.0.0.1", port), "refused before pg_ctl start"


# ── the repair legs outside provision() authenticate too ───────────────────────


class TestOutsideRepairLegsAuthenticate:
    def test_a_helper_run_in_a_superuser_scope_reaches_the_hardened_cluster(self, box: _Box) -> None:
        with pp.superuser_auth(box.creds["PG_SUPERUSER_PASS"]):
            out = pp._psql_tuples(box.bins, box.port, "nexus", _os_user(), "SELECT 1")
        assert out == "1"

    def test_without_a_scope_the_same_call_is_refused(self, box: _Box, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(box.config_dir / "elsewhere"))
        with pytest.raises(subprocess.CalledProcessError):
            pp._psql_tuples(box.bins, box.port, "nexus", _os_user(), "SELECT 1")

    def test_the_ambient_credentials_serve_a_caller_that_names_only_the_port(
        self, box: _Box, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(box.config_dir))
        assert pp._psql_tuples(box.bins, box.port, "nexus", _os_user(), "SELECT 1") == "1"
