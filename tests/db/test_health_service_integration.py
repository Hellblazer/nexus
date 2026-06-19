# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for _check_storage_service_health, _check_migration_state,
_check_rls_present against a real hermetic Postgres 16 + Java service.

Requires (on THIS machine — darwin/aarch64 with JDK25 GraalVM):
  - /opt/homebrew/opt/postgresql@16/bin/{initdb,pg_ctl,psql,createdb} present
  - service/target/nexus-service-1.0-SNAPSHOT.jar built
  - Java on PATH (or JAVA_HOME/bin/java available)

Marked @pytest.mark.integration — skipped automatically in CI.

Run locally with:
    JAVA_HOME=~/.sdkman/candidates/java/25.0.1-graal \\
    PATH=$JAVA_HOME/bin:$PATH \\
    uv run pytest -m integration tests/db/test_health_service_integration.py -v
"""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.db._service_fixture import SERVICE_ROLES_SQL

# ── Prerequisite paths ────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_JAR       = _REPO_ROOT / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"
_PG_BIN    = Path("/opt/homebrew/opt/postgresql@16/bin")

_INITDB   = _PG_BIN / "initdb"
_PG_CTL   = _PG_BIN / "pg_ctl"
_PSQL     = _PG_BIN / "psql"
_CREATEDB = _PG_BIN / "createdb"

_JAVA_HOME = os.environ.get("JAVA_HOME", "")
_JAVA = Path(_JAVA_HOME) / "bin" / "java" if _JAVA_HOME else Path(shutil.which("java") or "java")

_ALL_PREREQS = (
    _JAR.exists()
    and _INITDB.exists()
    and _PG_CTL.exists()
    and _PSQL.exists()
    and _CREATEDB.exists()
    and (_JAVA.exists() if _JAVA_HOME else shutil.which("java") is not None)
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _ALL_PREREQS,
        reason=(
            "skipped: missing jar or pg16 binaries "
            f"(jar={_JAR.exists()}, pg16={_PG_CTL.exists()}, java={_JAVA})"
        ),
    ),
]


# ── Port helpers ──────────────────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_tcp(host: str, port: int, timeout: float = 30.0) -> None:
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"port {port} on {host} not reachable after {timeout}s")


# ── Module-scoped fixtures ────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def pg_instance():
    """Hermetic Postgres 16 cluster (trust auth)."""
    pgdata = tempfile.mkdtemp(prefix="nexus_health_inttest_pg_")
    pg_port = _free_port()
    pglog = os.path.join(pgdata, "pg.log")
    pg_user = os.environ["USER"]

    try:
        subprocess.run(
            [str(_INITDB), "-D", pgdata, "--no-locale", "-E", "UTF8", "--auth=trust"],
            check=True, capture_output=True,
        )
        with open(os.path.join(pgdata, "postgresql.conf"), "a") as f:
            f.write(f"\nport = {pg_port}\nlisten_addresses = '127.0.0.1'\n")
        subprocess.run(
            [str(_PG_CTL), "-D", pgdata, "-l", pglog,
             "-o", f"-p {pg_port} -k {pgdata}", "start", "-w"],
            check=True, capture_output=True,
        )
        subprocess.run(
            [str(_CREATEDB), "-h", "127.0.0.1", "-p", str(pg_port),
             "-U", pg_user, "nexustest"],
            check=True, capture_output=True,
        )
        proc = subprocess.run(
            [str(_PSQL), "-h", "127.0.0.1", "-p", str(pg_port),
             "-U", pg_user, "-d", "nexustest",
             "-v", "ON_ERROR_STOP=1", "-c", SERVICE_ROLES_SQL],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"SERVICE_ROLES_SQL failed: {proc.stderr}"
            )
        yield {"port": pg_port, "dbname": "nexustest", "user": pg_user, "pgdata": pgdata}
    finally:
        subprocess.run(
            [str(_PG_CTL), "-D", pgdata, "stop", "-m", "immediate"],
            capture_output=True,
        )
        shutil.rmtree(pgdata, ignore_errors=True)


@pytest.fixture(scope="module")
def service(pg_instance):
    """Java service with real Liquibase migrations applied."""
    svc_port = _free_port()
    token = "inttest-health-bearer"

    env = {
        **os.environ,
        "NX_SERVICE_PORT": str(svc_port),
        "NX_SERVICE_TOKEN": token,
        "NX_DB_URL": (
            f"jdbc:postgresql://127.0.0.1:{pg_instance['port']}"
            f"/{pg_instance['dbname']}"
        ),
        "NX_DB_USER": "nexus_svc",
        "NX_DB_PASS": "nexus_svc_pass",
        "NX_POOL_SIZE": "3",
        "NX_DB_ADMIN_URL": (
            f"jdbc:postgresql://127.0.0.1:{pg_instance['port']}"
            f"/{pg_instance['dbname']}"
        ),
        "NX_DB_ADMIN_USER": pg_instance["user"],
        "NX_DB_ADMIN_PASS": "",
        "NX_CHROMA_PATH": tempfile.mkdtemp(prefix="nexus-health-chroma-"),
    }
    env.pop("NX_STORAGE_BACKEND", None)

    proc = subprocess.Popen(
        [str(_JAVA), "-jar", str(_JAR)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid,
    )
    try:
        _wait_tcp("127.0.0.1", svc_port, timeout=30.0)
        yield {
            "port": svc_port,
            "token": token,
            "pg_port": pg_instance["port"],
            "pg_dbname": pg_instance["dbname"],
            "pg_user": pg_instance["user"],
        }
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.fixture(scope="module")
def creds_file(service, tmp_path_factory):
    """Write a pg_credentials file pointing at the hermetic PG."""
    tmp = tmp_path_factory.mktemp("health_creds")
    creds_path = tmp / "pg_credentials"
    content = (
        f"PG_PORT={service['pg_port']}\n"
        f"NX_DB_ADMIN_URL=jdbc:postgresql://127.0.0.1:{service['pg_port']}/{service['pg_dbname']}\n"
        f"NX_DB_ADMIN_USER={service['pg_user']}\n"
        f"NX_DB_ADMIN_PASS=\n"
        f"NX_DB_URL=jdbc:postgresql://127.0.0.1:{service['pg_port']}/{service['pg_dbname']}\n"
        f"NX_DB_USER=nexus_svc\n"
        f"NX_DB_PASS=nexus_svc_pass\n"
    )
    creds_path.write_text(content)
    return creds_path


# ── Integration tests ─────────────────────────────────────────────────────────

class TestStorageServiceHealthIntegration:

    def test_health_check_up(self, service, creds_file):
        """Real service running -> ok result."""
        from nexus.health import _check_storage_service_health
        results = _check_storage_service_health(
            creds_path=creds_file,
            endpoint=("127.0.0.1", service["port"]),
        )
        assert len(results) == 1
        r = results[0]
        assert r.ok is True
        assert r.fatal is False

    def test_health_check_wrong_port_fatal(self, creds_file):
        """Unreachable port -> fatal."""
        from nexus.health import _check_storage_service_health
        dead_port = _free_port()  # grabbed but not bound
        results = _check_storage_service_health(
            creds_path=creds_file,
            endpoint=("127.0.0.1", dead_port),
        )
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.fatal is True


class TestMigrationStateIntegration:

    def test_migration_state_ok(self, service, creds_file):
        """After JAR start (Liquibase ran) -> all EXECUTED."""
        from nexus.health import _check_migration_state
        results = _check_migration_state(
            creds_path=creds_file,
            psql_bin=_PSQL,
        )
        assert len(results) == 1
        r = results[0]
        assert r.ok is True
        assert "EXECUTED" in r.detail


class TestRlsPresentIntegration:

    def test_rls_present(self, service, creds_file):
        """After JAR start (Liquibase ran) -> all RLS policies present."""
        from nexus.health import _check_rls_present
        results = _check_rls_present(
            creds_path=creds_file,
            psql_bin=_PSQL,
        )
        assert len(results) == 1
        r = results[0]
        assert r.ok is True
        assert r.fatal is False

    def test_rls_absent_after_policy_drop_is_fatal(self, service, creds_file):
        """Drop a policy on nexus.memory -> _check_rls_present returns fatal.

        NON-VACUOUS negative test: directly modifies DB state, confirms
        the check catches the regression.
        """
        from nexus.health import _check_rls_present

        pg_port = service["pg_port"]
        pg_dbname = service["pg_dbname"]
        pg_user = service["pg_user"]

        # Find and drop a policy on nexus.memory (superuser connection).
        # Step 1: list policies
        list_proc = subprocess.run(
            [str(_PSQL), "-h", "127.0.0.1", "-p", str(pg_port),
             "-U", pg_user, "-d", pg_dbname,
             "-t", "-A",
             "-c", "SELECT policyname FROM pg_policies WHERE schemaname='nexus' AND tablename='memory' LIMIT 1;"],
            capture_output=True, text=True, check=False,
        )
        policy_name = list_proc.stdout.strip()
        if not policy_name:
            pytest.skip("No policy on nexus.memory to drop — cannot run negative test")

        # Step 2: drop the policy
        drop_proc = subprocess.run(
            [str(_PSQL), "-h", "127.0.0.1", "-p", str(pg_port),
             "-U", pg_user, "-d", pg_dbname,
             "-t", "-A",
             "-c", f"DROP POLICY {policy_name} ON nexus.memory;"],
            capture_output=True, text=True, check=False,
        )
        if drop_proc.returncode != 0:
            pytest.skip(f"Could not drop policy: {drop_proc.stderr}")

        try:
            results = _check_rls_present(
                creds_path=creds_file,
                psql_bin=_PSQL,
            )
            assert len(results) == 1
            r = results[0]
            assert r.ok is False, "Expected fatal when policy is dropped"
            assert r.fatal is True
            assert "nexus.memory" in r.detail or "memory" in r.detail.lower()
        finally:
            # Restore: re-create the policy so other tests are unaffected.
            # (Module-scoped service; policy restore is best-effort.)
            subprocess.run(
                [str(_PSQL), "-h", "127.0.0.1", "-p", str(pg_port),
                 "-U", pg_user, "-d", pg_dbname,
                 "-c", (
                     f"CREATE POLICY {policy_name} ON nexus.memory "
                     "USING (tenant_id = current_setting('app.tenant_id')) "
                     "WITH CHECK (tenant_id = current_setting('app.tenant_id'));"
                 )],
                capture_output=True, text=True, check=False,
            )
