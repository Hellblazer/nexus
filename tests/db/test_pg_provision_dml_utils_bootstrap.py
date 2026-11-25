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

Non-vacuity (the nexus-moht0 doctrine): every cluster fixture goes through
``_require_library``. On the pinned engine tag's bundle (``NEXUS_PG_BIN`` unset, which is
what CI uses) a bundle without the library FAILS the run, so the 12-odd cluster tests cannot
all skip green. Only an explicit ``NEXUS_PG_BIN`` override (the nightly CA-3 bundle) may skip,
with the same reason.
"""
from __future__ import annotations

import contextvars
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from nexus.db.pg_bootstrap import dml_utils as boot
from nexus.daemon.binary_install import PINNED_SERVICE_TAG
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
#: Read at import, before the autouse fixture below pins NEXUS_PG_BIN to the pinned bundle itself.
_PG_BIN_OVERRIDDEN = bool(os.environ.get("NEXUS_PG_BIN", "").strip())


class _RecordingLog:
    """Stands in for the module's structlog logger: (level, event, fields) per call."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def _record(self, level: str):
        return lambda event, **fields: self.events.append((level, event, fields))

    def __getattr__(self, level: str):
        if level in ("debug", "info", "warning", "error"):
            return self._record(level)
        raise AttributeError(level)

    def named(self, event: str) -> list[tuple[str, dict]]:
        return [(lvl, f) for lvl, ev, f in self.events if ev == event]


@pytest.fixture(scope="module", autouse=True)
def _pin_discovery_to_the_built_bundle():
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

    def as_admin(self, sql: str) -> subprocess.CompletedProcess:
        """*sql* in one psql session as nexus_admin (the engine's migrating role), with its own password."""
        env = {**os.environ, "PGPASSWORD": self.creds["NX_DB_ADMIN_PASS"]}
        return subprocess.run(
            [str(self.bins.psql), "-X", "-w", "-h", "127.0.0.1", "-p", str(self.port), "-U", "nexus_admin",
             "-d", "nexus", "-t", "-A", "-v", "ON_ERROR_STOP=1", "-c", sql],
            capture_output=True, text=True, encoding="utf-8", env=env, timeout=60,
        )

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


def _provision_failed(config_dir: Path, exc: subprocess.CalledProcessError) -> AssertionError:
    # CalledProcessError's str() omits stderr, which is the part that says why.
    pg_log = config_dir / "postgres" / "pg.log"
    tail = pg_log.read_text()[-2000:] if pg_log.exists() else "(no pg.log)"
    return AssertionError(f"provision failed: {exc}\nstderr: {exc.stderr!r}\npg.log tail:\n{tail}")


def _require_library(box: _Box) -> None:
    """The bundle under test must carry pg_background: a failure on the pinned bundle, a skip only
    under an explicit NEXUS_PG_BIN override (see the module docstring)."""
    if box.q("SELECT count(*) FROM pg_available_extensions WHERE name = 'pg_background'") != "0":
        return
    reason = f"the PostgreSQL bundle at {box.bins.bin_dir} has no pg_background control file"
    if _PG_BIN_OVERRIDDEN:
        pytest.skip(f"{reason} (NEXUS_PG_BIN is set to it)")
    pytest.fail(
        f"{reason}. NEXUS_PG_BIN is unset, so tests.db._service_fixture.pg_bin_dir() chose that directory: "
        "the pinned engine tag's bundle if it self-provisioned, a host PostgreSQL if discovery found one "
        f"first. CI's bundle ({PINNED_SERVICE_TAG}) carries the library, so on an unset NEXUS_PG_BIN this is "
        "a failure, not a skip; point NEXUS_PG_BIN at a bundle that has it, or set it to skip on purpose"
    )


@pytest.fixture
def fresh(bins: pp.PgBinaries):
    """A cluster provision() made with the bootstrap in place."""
    config_dir = Path(pg_data_tempdir("nexus_dmlutils_pg_"))
    try:
        try:
            pp.provision(config_dir, force_new_port=True)
        except subprocess.CalledProcessError as exc:
            raise _provision_failed(config_dir, exc) from exc
        apply_reloadable_throwaway_conf(bins.pg_ctl, config_dir)
        box = _Box(config_dir, bins)
        _require_library(box)
        yield box
    finally:
        stop_and_remove_cluster(bins.pg_ctl, config_dir / "postgres", remove_dir=config_dir)


@pytest.fixture
def legacy(bins: pp.PgBinaries):
    """A cluster provisioned before the bootstrap existed: the dml_utils step is stubbed out for the
    first provision(), so nothing of it is there."""
    config_dir = Path(pg_data_tempdir("nexus_dmlutils_legacy_pg_"))
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(boot, "bootstrap_dml_utils_best_effort", lambda *a, **k: None)
        try:
            pp.provision(config_dir, force_new_port=True)
        except subprocess.CalledProcessError as exc:
            raise _provision_failed(config_dir, exc) from exc
        finally:
            mp.undo()
        apply_reloadable_throwaway_conf(bins.pg_ctl, config_dir)
        box = _Box(config_dir, bins)
        _require_library(box)
        yield box
    finally:
        stop_and_remove_cluster(bins.pg_ctl, config_dir / "postgres", remove_dir=config_dir)


def _assert_fully_bootstrapped(box: _Box, *, schema_ours: bool = True) -> None:
    assert box.q("SELECT count(*) FROM pg_extension WHERE extname = 'pg_background'") == "1"
    flags = box.q(
        "SELECT rolname, rolcanlogin, rolbypassrls, rolsuper, rolcreaterole, rolcreatedb FROM pg_roles "
        "WHERE rolname IN ('dml_utils_owner','dml_utils_caller','ddl_utils_caller') ORDER BY 1"
    ).splitlines()
    assert flags == [
        "ddl_utils_caller|f|f|f|f|f", "dml_utils_caller|f|f|f|f|f", "dml_utils_owner|f|f|f|f|f",
    ], "every library role is NOLOGIN, NOBYPASSRLS, not a superuser, and cannot create roles or databases"
    assert box.q("SELECT has_database_privilege('dml_utils_owner', 'nexus', 'CREATE')") == "t"
    assert box.q("SELECT has_schema_privilege('dml_utils_owner', 'public', 'CREATE')") == "t"
    # (member, role, inherits its privileges, may SET ROLE to it)
    for member, role, usage, can_set in (
        ("dml_utils_owner", "ddl_utils_caller", True, True), ("dml_utils_owner", "pgbackground_role", True, True),
        ("nexus_admin", "dml_utils_owner", False, True),
        ("nexus_admin", "dml_utils_caller", True, True), ("nexus_admin", "pgbackground_role", True, True),
    ):
        got = (
            box.q(f"SELECT pg_has_role('{member}', '{role}', 'MEMBER')"),
            box.q(f"SELECT pg_has_role('{member}', '{role}', 'USAGE')"),
            box.q(f"SELECT pg_has_role('{member}', '{role}', 'SET')"),
        )
        assert got == ("t", "t" if usage else "f", "t" if can_set else "f"), (
            f"{member} -> {role}: (member, inherits, set) = {got}"
        )
    if schema_ours:
        owner = box.q("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'liquibase'")
        assert owner == "dml_utils_owner"


class TestFreshProvision:
    def test_creates_the_extension_roles_grants_and_schema(self, fresh: _Box) -> None:
        _assert_fully_bootstrapped(fresh)

    def test_the_engine_role_can_become_the_owner_and_create_in_the_schema(self, fresh: _Box) -> None:
        """The point of the grants: nexus_admin runs SET ROLE dml_utils_owner and the objects land owned by it."""
        res = fresh.as_admin(
            "SET ROLE dml_utils_owner; CREATE TABLE liquibase.probe(i int); "
            "SELECT tableowner FROM pg_tables WHERE tablename = 'probe'; DROP TABLE liquibase.probe"
        )
        assert res.returncode == 0, res.stderr
        assert "dml_utils_owner" in res.stdout

    def test_the_engine_role_holds_nothing_of_the_owner_until_it_sets_role(self, fresh: _Box) -> None:
        """INHERIT FALSE on dml_utils_owner: the owner's CREATE on the database and on public must not be
        ambient in a plain nexus_admin session, or nexus_admin's own CREATE on public stops being load-bearing
        (tests/db/test_pg_provision.py::TestPublicSchemaGrantIsLoadBearing) and it silently gains every
        privilege the owner has."""
        fresh.run("REVOKE CREATE ON SCHEMA public FROM nexus_admin")
        for target in ("public.leak_probe", "liquibase.leak_probe"):
            plain = fresh.as_admin(f"CREATE TABLE {target}(i int)")
            assert plain.returncode != 0 and "permission denied" in plain.stderr, (
                f"nexus_admin created {target} without SET ROLE: {plain.stdout!r} {plain.stderr!r}"
            )
        assert fresh.q("SELECT has_schema_privilege('nexus_admin', 'public', 'CREATE')") == "f"
        assert fresh.q("SELECT has_schema_privilege('nexus_admin', 'liquibase', 'CREATE')") == "f"
        owned = fresh.as_admin("SET ROLE dml_utils_owner; CREATE TABLE public.leak_probe(i int); DROP TABLE public.leak_probe")
        assert owned.returncode == 0, "after SET ROLE the owner's own CREATE on public applies: " + owned.stderr

    def test_the_engine_role_calls_the_library_functions_without_set_role(self, fresh: _Box) -> None:
        """The move runs as nexus_admin: pg_background's functions (pgbackground_role) must be callable as it is."""
        probe = "SELECT has_function_privilege('{role}', 'pg_background_list()', 'EXECUTE')"
        assert fresh.q(probe.format(role="nexus_admin")) == "t"
        assert fresh.q(probe.format(role="nexus_svc")) == "f", "control: the privilege comes from the role, not from PUBLIC"


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

    def test_an_inheriting_owner_membership_is_converged_to_set_only(self, legacy: _Box) -> None:
        """The first shape of this step granted dml_utils_owner to nexus_admin with the default INHERIT TRUE."""
        legacy.bootstrap()
        legacy.run("GRANT dml_utils_owner TO nexus_admin WITH INHERIT TRUE, SET TRUE")
        assert legacy.q("SELECT pg_has_role('nexus_admin', 'dml_utils_owner', 'USAGE')") == "t"
        pp.provision(legacy.config_dir)
        _assert_fully_bootstrapped(legacy)

    def test_a_library_role_that_became_a_superuser_is_converged(self, legacy: _Box) -> None:
        legacy.bootstrap()
        legacy.run("ALTER ROLE dml_utils_caller SUPERUSER CREATEROLE CREATEDB")
        assert legacy.q("SELECT rolsuper FROM pg_roles WHERE rolname = 'dml_utils_caller'") == "t"
        pp.provision(legacy.config_dir)
        _assert_fully_bootstrapped(legacy)

    def test_an_empty_liquibase_schema_someone_else_owns_is_taken(self, legacy: _Box) -> None:
        legacy.run("CREATE SCHEMA liquibase")
        assert legacy.q("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'liquibase'") != "dml_utils_owner"
        out = legacy.bootstrap()
        assert out.schema_problem is None and out.blocked_reason is None
        assert out.schema_reowned and not out.schema_created, "it was handed over, not created"
        _assert_fully_bootstrapped(legacy)

    @pytest.mark.parametrize(
        "their_object",
        [
            "CREATE TABLE liquibase.theirs (i int)",
            "CREATE FUNCTION liquibase.theirs() RETURNS int LANGUAGE sql AS 'SELECT 1'",
            "CREATE TYPE liquibase.theirs AS ENUM ('a')",
        ],
        ids=["table", "function-only", "type-only"],
    )
    def test_a_non_empty_liquibase_schema_someone_else_owns_is_left_with_a_reason_and_the_rest_still_applies(
        self, legacy: _Box, their_object: str
    ) -> None:
        """The hand-over counts relations, functions and types in one statement; a schema holding only a
        function or only a type is not empty."""
        legacy.run(f"CREATE SCHEMA liquibase; {their_object}")
        owner_before = legacy.q("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'liquibase'")
        assert owner_before != "dml_utils_owner"
        out = legacy.bootstrap()
        assert not out.schema_reowned
        assert out.applied, "the extension, roles and grants are in place, only the schema is a problem"
        assert out.schema_problem == f"liquibase schema owned by {owner_before} and not empty"
        assert out.blocked_reason == out.schema_problem, "the line a doctor can show"
        assert legacy.q("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'liquibase'") == owner_before
        _assert_fully_bootstrapped(legacy, schema_ours=False)

    def test_a_loser_of_the_creation_race_is_not_an_error(self, legacy: _Box, monkeypatch: pytest.MonkeyPatch) -> None:
        """A provision whose probe ran before a peer's commit sees nothing and tries to create everything:
        CREATE ROLE and CREATE SCHEMA meet what the winner made. The state stays the winner's."""
        legacy.bootstrap()
        before = legacy.snapshot()
        stale = {"installed": "0", "available": "1", "admin": "1", "schema_owner": "", "roles": ""}
        monkeypatch.setattr(boot, "_state", lambda *a, **k: stale)
        out = legacy.bootstrap()
        assert out.applied
        assert legacy.snapshot() == before
        assert (out.extension_created, out.roles_created, out.schema_created, out.schema_reowned) == (
            False, [], False, False,
        ), "every create lost the race: none of them is this call's"

    def test_two_provisions_racing_from_an_empty_cluster_both_succeed(
        self, legacy: _Box, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A barrier after each thread's first probe makes both see the empty cluster and then create."""
        real, barrier, seen = boot._state, threading.Barrier(2, timeout=60), threading.local()

        def probe_then_meet(*a, **k):
            state = real(*a, **k)
            if not getattr(seen, "met", False):
                seen.met = True
                barrier.wait()
            return state

        monkeypatch.setattr(boot, "_state", probe_then_meet)
        with pp.superuser_auth(legacy.creds["PG_SUPERUSER_PASS"]):
            args = (legacy.bins, legacy.port, pp.bootstrap_superuser())
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(contextvars.copy_context().run, boot.bootstrap_dml_utils, *args) for _ in range(2)]
                results = [f.result(timeout=120) for f in futures]
        assert all(r.applied for r in results)
        _assert_fully_bootstrapped(legacy)
        # Each object was created by exactly one of the two: a loser reports nothing it did not make.
        assert sorted(results[0].roles_created + results[1].roles_created) == sorted(_ROLES)
        assert [r.schema_created for r in results].count(True) == 1
        assert [r.extension_created for r in results].count(True) == 1


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
            if "CREATE EXTENSION" in sql:
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
        """A clean skip, not a swallowed exception: the skip event is logged at INFO (it fires on every start
        of an install whose bundle predates the library) and the failure event, which the best-effort wrapper
        emits for any exception, is NOT."""
        real = boot._state
        monkeypatch.setattr(boot, "_state", lambda *a, **k: {**real(*a, **k), "available": "0"})
        log = _RecordingLog()
        monkeypatch.setattr(boot, "_log", log)
        result = pp.provision(legacy.config_dir)
        assert result.already_provisioned, "daemon start must go on"
        assert legacy.q("SELECT count(*) FROM pg_extension WHERE extname = 'pg_background'") == "0"
        skipped = log.named("pg_dml_utils_bootstrap_skipped")
        assert [lvl for lvl, _ in skipped] == ["info"]
        assert "pg_background" in skipped[0][1]["reason"]
        assert log.named("pg_dml_utils_bootstrap_failed") == [], "an exception was swallowed as if it were a skip"
        assert [e for e in log.events if e[0] in ("warning", "error")] == []

    def test_a_failure_inside_the_bootstrap_never_fails_the_fast_path(
        self, legacy: _Box, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*a, **k):
            raise RuntimeError("psql exploded")

        monkeypatch.setattr(boot, "bootstrap_dml_utils", boom)
        log = _RecordingLog()
        monkeypatch.setattr(boot, "_log", log)
        result = pp.provision(legacy.config_dir)
        assert result.already_provisioned
        assert legacy.q("SELECT count(*) FROM pg_roles WHERE rolname LIKE '%utils%'") == "0"
        failed = log.named("pg_dml_utils_bootstrap_failed")
        assert [lvl for lvl, _ in failed] == ["warning"]
        assert "psql exploded" in failed[0][1]["error"]


class TestTheLibraryGuard:
    """No cluster: _require_library names the bin dir it looked at, whichever PostgreSQL that is."""

    class _Box:
        def __init__(self, bin_dir: Path) -> None:
            self.bins = pp.PgBinaries.from_dir(bin_dir)

        def q(self, sql: str) -> str:
            return "0"

    def test_an_unset_override_fails_naming_the_directory_and_not_only_the_pinned_tag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys.modules[__name__], "_PG_BIN_OVERRIDDEN", False)
        host_bin = tmp_path / "host-postgresql" / "bin"
        host_bin.mkdir(parents=True)
        with pytest.raises(pytest.fail.Exception) as info:
            _require_library(self._Box(host_bin))
        text = str(info.value)
        assert str(host_bin) in text, "which bin dir it used"
        assert "host PostgreSQL" in text and "pg_bin_dir()" in text
        assert "NEXUS_PG_BIN is unset" in text

    def test_an_explicit_override_skips_with_the_same_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys.modules[__name__], "_PG_BIN_OVERRIDDEN", True)
        with pytest.raises(pytest.skip.Exception) as info:
            _require_library(self._Box(tmp_path))
        assert str(tmp_path) in str(info.value)


class TestTheFailureLog:
    """No cluster: the best-effort wrapper's log line carries psql's stderr, bounded."""

    def test_the_stderr_of_a_failed_psql_is_in_the_line_and_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*a, **k):
            raise subprocess.CalledProcessError(2, ["psql", "-c", "..."], stderr=b"ERROR:  permission denied " * 200)

        monkeypatch.setattr(boot, "bootstrap_dml_utils", boom)
        log = _RecordingLog()
        monkeypatch.setattr(boot, "_log", log)
        assert boot.bootstrap_dml_utils_best_effort(None, 0, "u", via="test") is None
        (_, fields), = log.named("pg_dml_utils_bootstrap_failed")
        assert "permission denied" in fields["stderr"]
        assert len(fields["stderr"]) <= 503, "bounded, with the ellipsis"
        assert fields["via"] == "test"

    def test_a_str_stderr_and_a_missing_one_are_both_handled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for exc in (
            subprocess.CalledProcessError(1, ["psql"], stderr="ERROR: text mode"),
            RuntimeError("no stderr attribute"),
        ):
            def boom(*a, _e=exc, **k):
                raise _e

            monkeypatch.setattr(boot, "bootstrap_dml_utils", boom)
            log = _RecordingLog()
            monkeypatch.setattr(boot, "_log", log)
            boot.bootstrap_dml_utils_best_effort(None, 0, "u", via="test")
            (_, fields), = log.named("pg_dml_utils_bootstrap_failed")
            assert fields["stderr"] == ("ERROR: text mode" if isinstance(exc, subprocess.CalledProcessError) else "")


class TestStaleMissingPrerequisiteRetriesAreReset:
    """F33 (Sam's ruling, T2 nexus/sam-ruling-f33-client-reset-2026-11-21): once pg_background is present, the
    bootstrap clears the retry state a move recorded while it was missing, so the move does not wait out a backoff of
    up to 15 min for a cause that is gone. Only that reason; and no registry table is no error."""

    _TABLE = (
        "CREATE SCHEMA IF NOT EXISTS nexus; CREATE TABLE nexus.data_moves (label text PRIMARY KEY, "
        "transient_retries int NOT NULL DEFAULT 0, last_transient_at timestamptz, last_transient text)"
    )

    def _row(self, box: _Box, label: str) -> str:
        return box.q(
            "SELECT transient_retries || '|' || coalesce(last_transient_at::text, '-') || '|' || coalesce(last_transient, '-') "
            f"FROM nexus.data_moves WHERE label = '{label}'"
        ).strip()

    def test_a_pg_background_retry_is_reset_and_another_reason_is_untouched(self, fresh: _Box) -> None:
        fresh.q(self._TABLE)
        fresh.q(
            "INSERT INTO nexus.data_moves VALUES "
            "('stale', 7, now(), 'pg_background is not installed'), "
            "('prefixed', 3, now(), 'retry 3, last 2026-11-21T16:16:19Z: pg_background is not installed'), "
            "('other', 4, now(), 'could not obtain lock on relation nexus.chunks'), "
            "('clear', 0, NULL, NULL)"
        )
        out = fresh.bootstrap()
        assert out.stale_retries_reset == 2
        assert self._row(fresh, "stale") == "0|-|-"
        assert self._row(fresh, "prefixed") == "0|-|-"
        retries, at, reason = self._row(fresh, "other").split("|")
        assert (retries, reason) == ("4", "could not obtain lock on relation nexus.chunks") and at != "-"
        assert self._row(fresh, "clear") == "0|-|-"
        assert fresh.bootstrap().stale_retries_reset == 0, "a second run finds nothing to reset"

    def test_no_registry_table_is_no_error(self, fresh: _Box) -> None:
        assert fresh.q("SELECT to_regclass('nexus.data_moves') IS NULL").strip() == "t"
        out = fresh.bootstrap()
        assert out.stale_retries_reset == 0 and out.skipped_reason is None
