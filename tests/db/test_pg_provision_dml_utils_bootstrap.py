# SPDX-License-Identifier: AGPL-3.0-or-later
"""The dml_utils superuser bootstrap on a REAL bundled cluster (RDR-246 Phase 1 Step 4).

``provision()`` creates the ``pg_background`` extension, the three library roles
(NOLOGIN NOBYPASSRLS), their grants and the ``liquibase`` schema, on a fresh
cluster and, through its fast path, on one provisioned before the step existed.
No engine and no JVM (``no_service_jar``), no SQLite.

The "bundle has no pg_background library" cases drive the module's own probe and
its CREATE EXTENSION step; the shared test bundle always carries the library, so
the absence is injected at those two seams over a real cluster, and the
provision() fast path is then run for real to show a start never fails on it.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nexus.db import pg_dml_utils_bootstrap as boot
from nexus.db import pg_provision as pp
from tests._child_process import apply_reloadable_throwaway_conf, pg_data_tempdir, stop_and_remove_cluster
from tests.db._service_fixture import pg_bin_dir

from nexus._install.layout_core import exe_name

_INITDB = pg_bin_dir() / exe_name("initdb")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.no_service_jar,
    pytest.mark.skipif(
        not _INITDB.exists(),
        reason=f"skipped: nexus-pg bundle self-provisioning failed (no {_INITDB}).",
    ),
]

_ROLES = ("dml_utils_owner", "dml_utils_caller", "ddl_utils_caller")


@pytest.fixture(scope="module", autouse=True)
def _pin_discovery_to_the_built_bundle():
    import os

    if os.environ.get("NEXUS_PG_BIN", "").strip():
        yield
        return
    mp = pytest.MonkeyPatch()
    mp.setenv("NEXUS_PG_BIN", str(pg_bin_dir()))
    try:
        yield
    finally:
        mp.undo()


class _Box:
    def __init__(self, config_dir: Path, bins: pp.PgBinaries) -> None:
        self.config_dir = config_dir
        self.bins = bins
        self.creds_path = config_dir / "pg_credentials"

    @property
    def creds(self) -> dict[str, str]:
        return pp._read_credentials(self.creds_path)

    @property
    def port(self) -> int:
        return int(self.creds["PG_PORT"])

    def q(self, sql: str, db: str = "nexus") -> str:
        with pp.superuser_auth(self.creds["PG_SUPERUSER_PASS"]):
            return pp._psql_tuples(self.bins, self.port, db, pp.bootstrap_superuser(), sql)

    def run(self, sql: str) -> None:
        with pp.superuser_auth(self.creds["PG_SUPERUSER_PASS"]):
            pp._psql(self.bins, self.port, "nexus", pp.bootstrap_superuser(), sql)

    def bootstrap(self) -> boot.DmlUtilsBootstrap:
        with pp.superuser_auth(self.creds["PG_SUPERUSER_PASS"]):
            return boot.bootstrap_dml_utils(self.bins, self.port, pp.bootstrap_superuser())

    def snapshot(self) -> str:
        """Everything the bootstrap can touch, with identities, so a no-op shows as equal."""
        return self.q(
            "SELECT 'ext', oid::text, extname, extowner::text FROM pg_extension WHERE extname = 'pg_background' "
            "UNION ALL SELECT 'role', oid::text, rolname || rolcanlogin || rolbypassrls || rolsuper, '' "
            "FROM pg_roles WHERE rolname IN ('dml_utils_owner','dml_utils_caller','ddl_utils_caller','pgbackground_role') "
            "UNION ALL SELECT 'member', roleid::text, member::text, grantor::text || admin_option || inherit_option || set_option "
            "FROM pg_auth_members WHERE roleid IN (SELECT oid FROM pg_roles WHERE rolname LIKE '%utils%' OR rolname = 'pgbackground_role') "
            "UNION ALL SELECT 'schema', oid::text, nspname || nspowner::text, coalesce(nspacl::text, '') "
            "FROM pg_namespace WHERE nspname IN ('liquibase', 'public') "
            "UNION ALL SELECT 'db', oid::text, datname, coalesce(datacl::text, '') FROM pg_database WHERE datname = 'nexus' "
            "ORDER BY 1, 2, 3"
        )


@pytest.fixture(scope="module")
def bins() -> pp.PgBinaries:
    return pp.discover_pg_binaries()


def _provision(config_dir: Path, bins: pp.PgBinaries, *, skip_bootstrap: bool) -> None:
    """provision() a fresh cluster; with ``skip_bootstrap`` it is what an install from before this step looks like."""
    mp = pytest.MonkeyPatch()
    if skip_bootstrap:
        mp.setattr(boot, "bootstrap_dml_utils_best_effort", lambda *a, **k: None)
    try:
        try:
            pp.provision(config_dir, force_new_port=True)
        except subprocess.CalledProcessError as exc:
            pg_log = config_dir / "postgres" / "pg.log"
            tail = pg_log.read_text()[-2000:] if pg_log.exists() else "(no pg.log)"
            raise AssertionError(f"provision failed: {exc}\nstderr: {exc.stderr!r}\npg.log tail:\n{tail}") from exc
    finally:
        mp.undo()
    apply_reloadable_throwaway_conf(bins.pg_ctl, config_dir)


@pytest.fixture
def fresh(bins: pp.PgBinaries):
    """A cluster provision() made with the bootstrap in place."""
    config_dir = Path(pg_data_tempdir("nexus_dmlutils_pg_"))
    try:
        _provision(config_dir, bins, skip_bootstrap=False)
        yield _Box(config_dir, bins)
    finally:
        stop_and_remove_cluster(bins.pg_ctl, config_dir / "postgres", remove_dir=config_dir)


@pytest.fixture
def legacy(bins: pp.PgBinaries):
    """A cluster provisioned before the bootstrap existed: nothing of it is there."""
    config_dir = Path(pg_data_tempdir("nexus_dmlutils_legacy_pg_"))
    try:
        _provision(config_dir, bins, skip_bootstrap=True)
        box = _Box(config_dir, bins)
        if box.q("SELECT count(*) FROM pg_available_extensions WHERE name = 'pg_background'") == "0":
            pytest.skip(f"the bundle at {bins.bin_dir} has no pg_background (pinned engine tag's bundle has it)")
        yield box
    finally:
        stop_and_remove_cluster(bins.pg_ctl, config_dir / "postgres", remove_dir=config_dir)


def _assert_fully_bootstrapped(box: _Box) -> None:
    assert box.q("SELECT count(*) FROM pg_extension WHERE extname = 'pg_background'") == "1"
    flags = box.q(
        "SELECT rolname, rolcanlogin, rolbypassrls, rolsuper FROM pg_roles "
        "WHERE rolname IN ('dml_utils_owner','dml_utils_caller','ddl_utils_caller') ORDER BY 1"
    ).splitlines()
    assert flags == [
        "ddl_utils_caller|f|f|f", "dml_utils_caller|f|f|f", "dml_utils_owner|f|f|f",
    ], "every library role is NOLOGIN, NOBYPASSRLS and not a superuser"
    assert box.q("SELECT has_database_privilege('dml_utils_owner', 'nexus', 'CREATE')") == "t"
    assert box.q("SELECT has_schema_privilege('dml_utils_owner', 'public', 'CREATE')") == "t"
    for member, role in (
        ("dml_utils_owner", "ddl_utils_caller"), ("dml_utils_owner", "pgbackground_role"),
        ("nexus_admin", "dml_utils_owner"), ("nexus_admin", "dml_utils_caller"),
        ("nexus_admin", "pgbackground_role"),
    ):
        assert box.q(f"SELECT pg_has_role('{member}', '{role}', 'MEMBER')") == "t", f"{member} lacks {role}"
    assert box.q("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'liquibase'") == "dml_utils_owner"


class TestFreshProvision:
    def test_creates_the_extension_roles_grants_and_schema(self, fresh: _Box) -> None:
        _assert_fully_bootstrapped(fresh)

    def test_the_engine_role_can_become_the_owner_and_create_in_the_schema(self, fresh: _Box) -> None:
        """The point of the grants: nexus_admin runs SET ROLE dml_utils_owner and the objects land owned by it."""
        env_pass = fresh.creds["NX_DB_ADMIN_PASS"]
        import os

        env = {**os.environ, "PGPASSWORD": env_pass}
        res = subprocess.run(
            [str(fresh.bins.psql), "-X", "-w", "-h", "127.0.0.1", "-p", str(fresh.port), "-U", "nexus_admin",
             "-d", "nexus", "-t", "-A", "-v", "ON_ERROR_STOP=1", "-c",
             "SET ROLE dml_utils_owner; CREATE TABLE liquibase.probe(i int); "
             "SELECT tableowner FROM pg_tables WHERE tablename = 'probe'; DROP TABLE liquibase.probe"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res.returncode == 0, res.stderr
        assert "dml_utils_owner" in res.stdout


class TestBackfillOnAnInstallProvisionedBefore:
    def test_the_fixture_really_lacks_everything(self, legacy: _Box) -> None:
        assert legacy.q("SELECT count(*) FROM pg_extension WHERE extname = 'pg_background'") == "0"
        assert legacy.q("SELECT count(*) FROM pg_roles WHERE rolname IN ('dml_utils_owner','dml_utils_caller','ddl_utils_caller')") == "0"
        assert legacy.q("SELECT count(*) FROM pg_namespace WHERE nspname = 'liquibase'") == "0"

    def test_the_provision_fast_path_creates_everything(self, legacy: _Box) -> None:
        result = pp.provision(legacy.config_dir)
        assert result.already_provisioned, "this ran the fast path, the one every service start takes"
        _assert_fully_bootstrapped(legacy)

    def test_a_second_run_changes_nothing(self, legacy: _Box) -> None:
        pp.provision(legacy.config_dir)
        before = legacy.snapshot()
        assert before.count("\n") >= 10, "the snapshot must see the objects it claims are unchanged"
        again = legacy.bootstrap()
        assert (again.extension_created, again.roles_created, again.schema_created) == (False, [], False)
        pp.provision(legacy.config_dir)
        assert legacy.snapshot() == before

    def test_the_first_run_reports_what_it_made(self, legacy: _Box) -> None:
        first = legacy.bootstrap()
        assert first.applied
        assert first.extension_created and first.schema_created
        assert sorted(first.roles_created) == sorted(_ROLES)

    def test_a_drifted_role_is_converged_back_to_nologin_nobypassrls(self, legacy: _Box) -> None:
        legacy.bootstrap()
        legacy.run("ALTER ROLE dml_utils_caller LOGIN BYPASSRLS")
        pp.provision(legacy.config_dir)
        _assert_fully_bootstrapped(legacy)

    def test_a_liquibase_schema_someone_else_owns_is_left_alone(self, legacy: _Box) -> None:
        legacy.run("CREATE SCHEMA liquibase")
        legacy.bootstrap()
        owner = legacy.q("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'liquibase'")
        assert owner != "dml_utils_owner", "re-owning a schema we did not create is not the bootstrap's call"


class TestBundleWithoutTheLibrary:
    """Nothing is created, the reason is returned, and a start never fails."""

    def test_no_control_file_skips_everything_with_a_reason(self, legacy: _Box, monkeypatch: pytest.MonkeyPatch) -> None:
        real = boot._state
        monkeypatch.setattr(boot, "_state", lambda *a, **k: {**real(*a, **k), "available": "0"})
        out = legacy.bootstrap()
        assert not out.applied and "pg_background" in (out.skipped_reason or "")
        assert legacy.q("SELECT count(*) FROM pg_extension WHERE extname = 'pg_background'") == "0"
        assert legacy.q("SELECT count(*) FROM pg_roles WHERE rolname LIKE '%utils%'") == "0"
        assert legacy.q("SELECT count(*) FROM pg_namespace WHERE nspname = 'liquibase'") == "0"

    def test_a_control_file_with_no_loadable_library_skips_with_the_server_message(
        self, legacy: _Box, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_psql = boot._psql

        def refuse_extension(bins, port, db, user, sql, **kw):
            if sql.startswith("CREATE EXTENSION"):
                raise subprocess.CalledProcessError(
                    1, ["psql"], stderr=b'ERROR:  could not access file "$libdir/pg_background": No such file or directory'
                )
            return real_psql(bins, port, db, user, sql, **kw)

        monkeypatch.setattr(boot, "_psql", refuse_extension)
        out = legacy.bootstrap()
        assert not out.applied
        assert "could not access file" in (out.skipped_reason or "")
        assert legacy.q("SELECT count(*) FROM pg_roles WHERE rolname LIKE '%utils%'") == "0"

    def test_provision_fast_path_returns_and_creates_nothing_when_the_library_is_absent(
        self, legacy: _Box, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = boot._state
        monkeypatch.setattr(boot, "_state", lambda *a, **k: {**real(*a, **k), "available": "0"})
        result = pp.provision(legacy.config_dir)
        assert result.already_provisioned, "daemon start must go on"
        assert legacy.q("SELECT count(*) FROM pg_extension WHERE extname = 'pg_background'") == "0"

    def test_a_failure_inside_the_bootstrap_never_fails_the_fast_path(
        self, legacy: _Box, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*a, **k):
            raise RuntimeError("psql exploded")

        monkeypatch.setattr(boot, "bootstrap_dml_utils", boom)
        result = pp.provision(legacy.config_dir)
        assert result.already_provisioned
        assert legacy.q("SELECT count(*) FROM pg_roles WHERE rolname LIKE '%utils%'") == "0"
