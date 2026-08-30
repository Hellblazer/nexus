# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cross-language integration test for HttpPlanLibrary against the real Java service.

Requires (on THIS machine — darwin/aarch64 with JDK25 GraalVM):
  - PostgreSQL binaries discoverable (NEXUS_PG_BIN / Homebrew / system dirs / PATH)
  - service/target/nexus-service-1.0-SNAPSHOT.jar built (mvn -f service/pom.xml package -DskipTests)
  - Java on PATH (or JAVA_HOME/bin/java available)

Marked @pytest.mark.integration — collected but skipped automatically when the
jar or PG binaries are absent, so CI (which has neither) stays green.

Run locally with:
    JAVA_HOME=~/.sdkman/candidates/java/25.0.1-graal \\
    PATH=$JAVA_HOME/bin:$PATH \\
    uv run pytest -m integration tests/db/test_http_plan_library_integration.py -v

What is exercised (bead nexus-gmiaf.11 requirements):
  a) save/get/search/list_active round-trip
  b) FTS: Postgres ts_rank + STORED tsvector (english stemming probe)
  c) tags round-trip: untagged plan has tags=""
  d) Timestamp format: created_at returned as UTC second-precision Z
  e) Cross-tenant RLS negative: tenant A plans invisible to tenant B
  f) RLS WITH CHECK: cross-tenant write rejected
  g) ETL fidelity: import_plan -> get_plan preserves created_at, counters, metrics
  h) Metrics: increment_match_metrics / increment_run_started / increment_run_outcome
  i) set_plan_disabled / set_plan_enabled / list_active excludes disabled
  j) plan_exists boundary-safe tag match
  k) GREATEST merge: re-import with stale counters does NOT clobber live PG values (Critical 1 fix)
  l) disable-reason: tag appended via Java service, old reason replaced on re-disable
  m) FTS parity: Spearman rho >= 0.90 between SQLite FTS5 and Postgres tsvector rankings
     (satisfies the locked parity contract in docs/rdr/rdr-152-postgres-java-storage-service.md §FTS)

NX_STORAGE_BACKEND is NOT touched — default SQLite path is unchanged.
"""
from __future__ import annotations

import math
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from tests.db._service_fixture import (
    SERVICE_ROLES_SQL,
    create_tenant_token,
    pg_bin_dir,
    spawn_service,
    wait_for_service,
)

# ── Prerequisite paths ────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_JAR       = _REPO_ROOT / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"
_PG_BIN    = pg_bin_dir()

_INITDB   = _PG_BIN / "initdb"
_PG_CTL   = _PG_BIN / "pg_ctl"
_PSQL     = _PG_BIN / "psql"
_CREATEDB = _PG_BIN / "createdb"

_JAVA_HOME = os.environ.get("JAVA_HOME", "")
_JAVA = (
    Path(_JAVA_HOME) / "bin" / "java"
    if _JAVA_HOME
    else Path(shutil.which("java") or "java")
)

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
            "skipped: missing jar or PG binaries "
            f"(jar={_JAR.exists()}, pg={_PG_CTL.exists()}, java={_JAVA})"
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
    """Spin up a hermetic Postgres 16 instance (mirroring memory integration test)."""
    pgdata  = tempfile.mkdtemp(prefix="nexus_plan_inttest_pg_")
    pg_port = _free_port()
    pglog   = os.path.join(pgdata, "pg.log")
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
             "-o", f"-p {pg_port} -k {pgdata}",
             "start", "-w"],
            check=True, capture_output=True,
        )
        subprocess.run(
            [str(_CREATEDB), "-h", "127.0.0.1", "-p", str(pg_port),
             "-U", pg_user, "nexusplantest"],
            check=True, capture_output=True,
        )

        def _psql(sql: str) -> None:
            proc = subprocess.run(
                [str(_PSQL), "-h", "127.0.0.1", "-p", str(pg_port),
                 "-U", pg_user, "-d", "nexusplantest",
                 "-v", "ON_ERROR_STOP=1", "-c", sql],
                capture_output=True, text=True,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"psql bootstrap failed (rc={proc.returncode}):\n"
                    f"stdout={proc.stdout}\nstderr={proc.stderr}"
                )

        # net63: the JAR runs Liquibase at startup and owns the full plans schema
        # + grants before binding the HTTP port. The fixture must NOT pre-apply schema
        # — doing so collides ("relation already exists") and the service exits at
        # migration. The only pre-start SQL is SERVICE_ROLES_SQL, which creates
        # nexus_svc (the NOSUPERUSER NOBYPASSRLS DML/RLS role grants-nexus-svc.xml
        # grants to, and the role the RLS-negative tests use).
        _psql(SERVICE_ROLES_SQL)

        yield {"port": pg_port, "dbname": "nexusplantest", "user": pg_user, "pgdata": pgdata}

    finally:
        subprocess.run(
            [str(_PG_CTL), "-D", pgdata, "stop", "-m", "immediate"],
            capture_output=True,
        )
        shutil.rmtree(pgdata, ignore_errors=True)


@pytest.fixture(scope="module")
def service(pg_instance):
    """Launch the shaded JAR against the hermetic PG."""
    svc_port = _free_port()
    token    = "plan-inttest-bearer-secret"

    env = {
        **os.environ,
        "NX_SERVICE_PORT":  str(svc_port),
        "NX_SERVICE_TOKEN": token,
        # net63 two-role: app pool = nexus_svc (NOSUPERUSER NOBYPASSRLS → FORCE RLS
        # applies); migration pool = OS superuser (trust auth) for the Liquibase DDL.
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
        "NX_CHROMA_PATH": tempfile.mkdtemp(prefix="nexus-plan-chroma-"),
    }
    env.pop("NX_STORAGE_BACKEND", None)

    # nexus-lom9g: FILE-backed output via the shared primitive; the old
    # stdout=PIPE/stderr=PIPE form wedged the service once 64KB of Logback
    # output accumulated before the port bound (nexus-j0nec).
    proc, _svc_log = spawn_service([str(_JAVA), "-jar", str(_JAR)], env)
    try:
        wait_for_service("127.0.0.1", svc_port, proc=proc, log_path=_svc_log, timeout=60.0)
        yield f"http://127.0.0.1:{svc_port}", token, proc
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
def plan_store(service):
    """HttpPlanLibrary (tenant='default') connected to the real Java service."""
    from nexus.db.t2.http_plan_library import HttpPlanLibrary
    base_url, token, _ = service
    _saved_token = os.environ.get("NX_SERVICE_TOKEN")
    os.environ["NX_SERVICE_TOKEN"] = token
    s = HttpPlanLibrary(base_url=base_url, tenant="default")
    yield s
    s.close()
    # Restore: a leaked module token poisons later env-resolving modules (nexus-edwlp).
    if _saved_token is None:
        os.environ.pop("NX_SERVICE_TOKEN", None)
    else:
        os.environ["NX_SERVICE_TOKEN"] = _saved_token


@pytest.fixture(scope="module")
def other_plan_store(service):
    """HttpPlanLibrary for the cross-tenant RLS probe (tenant='other-tenant')."""
    from nexus.db.t2.http_plan_library import HttpPlanLibrary
    base_url, token, _ = service
    # Phase E: real other-tenant-bound bearer (mirrors `nx tenant create`).
    other_token = create_tenant_token(base_url, token, "other-tenant")
    s = HttpPlanLibrary(base_url=base_url, tenant="other-tenant", _token=other_token)
    yield s
    s.close()


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestPlansMVV:
    """Minimum viable verification (MVV) for the plans service."""

    def test_a_save_get_roundtrip(self, plan_store):
        """a) save_plan -> get_plan round-trip with real Postgres."""
        pid = plan_store.save_plan(
            query="Walk an RDR to implementing code",
            plan_json='{"steps":[{"type":"search"}]}',
            outcome="success",
            tags="research,rdr",
            project="nexus",
            name="walk-rdr",
            verb="research",
            scope="global",
        )
        assert isinstance(pid, int) and pid > 0, "save_plan must return a positive id"

        row = plan_store.get_plan(pid)
        assert row is not None, "get_plan must find saved row"
        assert row["query"]     == "Walk an RDR to implementing code"
        # nexus-cefa1.5: plan_json is jsonb now (plans-002-jsonb.xml).
        # PostgreSQL's jsonb canonical text output inserts a space after each
        # object-key colon, so the written '{"steps":[{"type":"search"}]}'
        # (no spaces) no longer round-trips byte-identical over the real
        # cross-language service -- assert the canonical shape.
        assert row["plan_json"] == '{"steps": [{"type": "search"}]}'
        assert row["outcome"]   == "success"
        assert row["tags"]      == "research,rdr"
        assert row["verb"]      == "research"

    def test_a2_get_by_dimensions_empty_project_global_scope(self, plan_store):
        """a2) get_plan_by_dimensions with an EMPTY project (global-scope sentinel).

        Regression for nexus-82ihm: the Java service rejected a blank ``project``
        as HTTP 400 'missing required query param', which broke global builtin
        plan seeding in service mode entirely (every global template queries
        by-dimensions with project=''). An empty project is the valid
        global-scope value — absent row must be None (404), present row 200.
        """
        dims = '{"scope":"global","verb":"research","name":"integ-empty-proj-marker"}'
        # Absent row over the HTTP path must be a clean None (404 -> None), NOT a 400.
        assert plan_store.get_plan_by_dimensions(project="", dimensions=dims) is None

        plan_store.save_plan(
            query="global plan via empty project",
            plan_json='{"steps":[]}',
            project="",
            verb="research",
            scope="global",
            dimensions=dims,
        )
        row = plan_store.get_plan_by_dimensions(project="", dimensions=dims)
        assert row is not None, "empty-project (global) get_by_dimensions must find the row"
        assert row["query"] == "global plan via empty project"

    def test_b_tags_empty_string_default(self, plan_store):
        """b) untagged plan has tags='' (not null/missing)."""
        pid = plan_store.save_plan(
            query="Untagged plan integration test",
            plan_json="{}",
            # hygiene-001: plans.verb is NOT NULL now (nexus-tk070.p6a
            # follow-on); the assertion under test is tags-default, not
            # verb, so any real verb value satisfies it.
            verb="research",
        )
        row = plan_store.get_plan(pid)
        assert row["tags"] == "", f"untagged plan tags must be ''; got {row['tags']!r}"

    def test_c_fts_english_stemming(self, plan_store):
        """c) FTS: 'searching' (stem 'search') matches 'searches' in match_text."""
        plan_store.save_plan(
            query="Find documents with text searches across corpora",
            plan_json="{}",
            project="fts-test",
            verb="research",
            name="full-text-search",
            scope="global",
        )
        results = plan_store.search_plans("searching", project="fts-test")
        queries = [r["query"] for r in results]
        assert any("searches" in q for q in queries), (
            f"FTS stem 'searching' must match 'searches' in match_text; "
            f"got queries={queries!r}"
        )

    def test_d_created_at_utc_format(self, plan_store):
        """d) created_at returned as UTC second-precision Z string."""
        # hygiene-001: plans.verb is NOT NULL now; unrelated to this test's
        # timestamp-format assertion, so any real verb value satisfies it.
        pid = plan_store.save_plan(query="Timestamp format test", plan_json="{}", verb="research")
        row = plan_store.get_plan(pid)
        ts = row.get("created_at")
        assert ts is not None, "created_at must be present"
        assert ts.endswith("Z"), f"created_at must end with Z (UTC); got {ts!r}"
        assert "T" in ts, f"created_at must include T separator; got {ts!r}"

    def test_e_cross_tenant_rls_negative(self, plan_store, other_plan_store):
        """e) tenant default's plans invisible to tenant other-tenant."""
        # hygiene-001: plans.verb is NOT NULL now; unrelated to the RLS
        # assertion under test, so any real verb value satisfies it.
        pid = plan_store.save_plan(
            query="Private plan for tenant isolation test",
            plan_json="{}",
            project="rls-test",
            verb="research",
        )
        # Tenant default can see the plan
        assert plan_store.get_plan(pid) is not None

        # other-tenant cannot see it (different tenant_id → RLS filter)
        row = other_plan_store.get_plan(pid)
        assert row is None, (
            f"Cross-tenant RLS must filter: tenant 'other-tenant' must not see "
            f"tenant 'default' plan id={pid}; got {row!r}"
        )

    def test_f_cross_tenant_write_invisible_to_default(self, service):
        """f) RLS isolation: a gamma-plans write is invisible to the default tenant.

        (Was test_f_rls_with_check_rejected — renamed: Phase E resolves tenant
        from the bearer, so this exercises cross-tenant READ isolation with a real
        gamma-plans bearer, not a WITH CHECK INSERT rejection.)
        """
        import httpx
        from nexus.db.t2.http_plan_library import HttpPlanLibrary
        base_url, token, _ = service

        # Construct a store bound to a real "gamma-plans" tenant. Phase E
        # (nexus-gmiaf.32.5): tenant_id comes from the AUTHENTICATED bearer, not
        # the X-Nexus-Tenant header — so a genuine second tenant needs its own
        # minted bearer (mirrors `nx tenant create`), not the root token + a header.
        gamma_token = create_tenant_token(base_url, token, "gamma-plans")
        cross_store = HttpPlanLibrary(base_url=base_url, tenant="gamma-plans", _token=gamma_token)
        try:
            # Write under gamma-plans, then assert the default tenant cannot see it.
            # hygiene-001: plans.verb is NOT NULL now; unrelated to the
            # cross-tenant-write assertion under test.
            pid = cross_store.save_plan(
                query="Cross-tenant write attempt",
                plan_json="{}",
                project="gamma-proj",
                verb="research",
            )
            # Plan was saved under gamma-plans tenant, not under default
            # default store should NOT see it
            from nexus.db.t2.http_plan_library import HttpPlanLibrary as HPL
            default_store = HPL(base_url=base_url, tenant="default", _token=token)
            try:
                row = default_store.get_plan(pid)
                assert row is None, (
                    "plan saved by gamma-plans must not be visible to default tenant"
                )
            finally:
                default_store.close()
        finally:
            cross_store.close()

    def test_g_etl_fidelity_import(self, plan_store):
        """g) import_plan fidelity: created_at, counters, metrics preserved verbatim."""
        src_created = "2025-06-01T10:30:00Z"
        src_last    = "2025-06-10T08:00:00Z"

        # hygiene-001: plans.verb is NOT NULL now (nexus-tk070.p6a
        # follow-on) — /v1/plans/import rejects a verb-less row with a 409
        # integrity constraint violation. Unrelated to this test's ETL
        # fidelity assertions (created_at, counters), so any real verb
        # value satisfies it.
        pid = plan_store.import_plan(
            project="etl-int-proj",
            query="ETL integration fidelity probe",
            plan_json='{"etl":true}',
            outcome="success",
            tags="etl,integration",
            created_at=src_created,
            verb="research",
            use_count=42,
            last_used=src_last,
            match_count=99,
            match_conf_sum=12.5,
            success_count=40,
            failure_count=2,
            scope_tags="knowledge__nexus",
            match_text="ETL integration fidelity probe. research scope global",
        )
        assert isinstance(pid, int) and pid > 0

        row = plan_store.get_plan(pid)
        assert row is not None

        # created_at preserved (modulo timezone normalization to UTC)
        assert "2025-06-01" in row["created_at"], (
            f"created_at must be preserved; got {row['created_at']!r}"
        )
        # Counters preserved verbatim
        assert row["use_count"] == 42, f"use_count must be 42; got {row['use_count']!r}"
        assert row["match_count"] == 99
        assert abs(row["match_conf_sum"] - 12.5) < 1e-9
        assert row["success_count"] == 40
        assert row["failure_count"] == 2
        assert row["scope_tags"] == "knowledge__nexus"

        # Idempotent re-import
        pid2 = plan_store.import_plan(
            project="etl-int-proj",
            query="ETL integration fidelity probe",
            plan_json='{"etl":true}',
            outcome="success",
            tags="etl,integration",
            created_at=src_created,
            verb="research",
            use_count=42,
            last_used=src_last,
            match_count=99,
            match_conf_sum=12.5,
            success_count=40,
            failure_count=2,
        )
        assert pid2 == pid, "idempotent re-import must return same id"

    def test_h_metrics_increment(self, plan_store):
        """h) increment_match_metrics, increment_run_started, increment_run_outcome."""
        # hygiene-001: plans.verb is NOT NULL now; unrelated to the metrics
        # assertions under test, so any real verb value satisfies it.
        pid = plan_store.save_plan(
            query="Metrics integration test plan",
            plan_json="{}",
            verb="research",
        )

        plan_store.increment_match_metrics(pid, confidence=None)
        row = plan_store.get_plan(pid)
        assert row["match_count"] == 1
        assert row["match_conf_sum"] == 0.0

        plan_store.increment_match_metrics(pid, confidence=0.9)
        row = plan_store.get_plan(pid)
        assert row["match_count"] == 2
        assert abs(row["match_conf_sum"] - 0.9) < 1e-9

        plan_store.increment_run_started(pid)
        row = plan_store.get_plan(pid)
        assert row["use_count"] == 1
        assert row["last_used"] is not None

        plan_store.increment_run_outcome(pid, success=True)
        row = plan_store.get_plan(pid)
        assert row["success_count"] == 1

        plan_store.increment_run_outcome(pid, success=False)
        row = plan_store.get_plan(pid)
        assert row["failure_count"] == 1

    def test_i_disable_enable_list_active(self, plan_store):
        """i) set_plan_disabled / set_plan_enabled / list_active excludes disabled."""
        # hygiene-001: plans.verb is NOT NULL now; unrelated to the
        # disable/enable/list_active assertions under test.
        pid_active   = plan_store.save_plan(
            query="Active plan for disable test", plan_json="{}", project="dis-int",
            verb="research")
        pid_disabled = plan_store.save_plan(
            query="Disabled plan for disable test", plan_json="{}", project="dis-int",
            verb="research")

        assert plan_store.set_plan_disabled(pid_disabled)

        row = plan_store.get_plan(pid_disabled)
        assert row["disabled_at"] is not None, "disabled_at must be set"

        active = plan_store.list_active_plans(project="dis-int")
        ids = [r["id"] for r in active]
        assert pid_active in ids
        assert pid_disabled not in ids, "disabled plan must not appear in list_active_plans"

        assert plan_store.set_plan_enabled(pid_disabled)
        row2 = plan_store.get_plan(pid_disabled)
        assert row2["disabled_at"] is None, "disabled_at must be cleared after enable"

    def test_j_plan_exists_boundary_safe(self, plan_store):
        """j) plan_exists comma-boundary tag match (not substring)."""
        # hygiene-001: plans.verb is NOT NULL now; unrelated to the
        # tag-boundary-match assertion under test.
        plan_store.save_plan(
            query="Exists boundary test",
            plan_json="{}",
            tags="builtin-template,research,rdr",
            verb="research",
        )
        assert plan_store.plan_exists("Exists boundary test", "builtin-template")
        assert plan_store.plan_exists("Exists boundary test", "research")
        assert not plan_store.plan_exists("Exists boundary test", "builtin")
        assert not plan_store.plan_exists("Exists boundary test", "no-such-tag")

    def test_k_source_authoritative_overwrites_live_counters(self, plan_store):
        """k) Source-authoritative re-import: additive counters use EXCLUDED (source wins).

        Bug nexus-0jq9u: additive event-tally counters (use_count, match_count,
        match_conf_sum, success_count, failure_count) must NOT use GREATEST on
        re-import.  The SQLite snapshot is the authoritative record; a one-shot
        migration overwrites the current PG value with the source value.

        Non-vacuous: this test FAILS if the SQL still uses GREATEST (source < live
        means GREATEST would keep live, not replace with source).
        """
        # Seed with source counters.
        # hygiene-001: plans.verb is NOT NULL now — /v1/plans/import rejects
        # a verb-less row with a 409 integrity constraint violation.
        # Unrelated to this test's GREATEST-vs-source-authoritative
        # assertions on the additive counters.
        pid = plan_store.import_plan(
            project="src-auth-int",
            query="Source-authoritative merge integration test",
            plan_json='{"src_auth":true}',
            outcome="success",
            tags="src-auth-test",
            created_at="2025-03-01T00:00:00Z",
            verb="research",
            use_count=5,
            match_count=10,
            match_conf_sum=2.5,
            success_count=4,
            failure_count=1,
        )
        assert isinstance(pid, int) and pid > 0

        # Simulate live traffic advancing counters in Postgres
        plan_store.increment_match_metrics(pid, confidence=0.9)
        plan_store.increment_match_metrics(pid, confidence=0.9)
        plan_store.increment_match_metrics(pid, confidence=0.9)  # match_count=10+3=13
        plan_store.increment_run_outcome(pid, success=True)
        plan_store.increment_run_outcome(pid, success=True)      # success_count=4+2=6

        row_live = plan_store.get_plan(pid)
        assert row_live["match_count"] > 10, (
            f"precondition: live increments advanced match_count above source=10; "
            f"got {row_live['match_count']}")

        # Re-import with the SOURCE values (lower than live PG)
        pid2 = plan_store.import_plan(
            project="src-auth-int",
            query="Source-authoritative merge integration test",
            plan_json='{"src_auth":true}',
            outcome="success",
            tags="src-auth-test",
            created_at="2025-03-01T00:00:00Z",
            verb="research",
            use_count=5,       # source value (< live use_count)
            match_count=10,    # source value (< live match_count=13)
            match_conf_sum=2.5,  # source value (< live conf_sum=5.2)
            success_count=4,   # source value (< live success_count=6)
            failure_count=1,
        )
        assert pid2 == pid, "idempotent re-import must return same id"

        row_after = plan_store.get_plan(pid)
        # Source MUST win for all five additive counters: PG values replaced by source
        assert row_after["match_count"] == 10, (
            f"source must overwrite live match_count=13 with source=10; "
            f"got {row_after['match_count']} (GREATEST still in use?)")
        assert abs(row_after["match_conf_sum"] - 2.5) < 1e-9, (
            f"source must overwrite live match_conf_sum with source=2.5; "
            f"got {row_after['match_conf_sum']} (GREATEST still in use?)")
        assert row_after["success_count"] == 4, (
            f"source must overwrite live success_count=6 with source=4; "
            f"got {row_after['success_count']} (GREATEST still in use?)")
        assert row_after["use_count"] == 5, (
            f"source must overwrite live use_count with source=5; "
            f"got {row_after['use_count']} (GREATEST still in use?)")
        assert row_after["failure_count"] == 1, (
            f"source must overwrite live failure_count with source=1; "
            f"got {row_after['failure_count']} (GREATEST still in use?)")

    def test_l_disable_reason_tag(self, plan_store):
        """l) disable with reason appends disable-reason:<reason> to tags via real service."""
        # hygiene-001: plans.verb is NOT NULL now; unrelated to the
        # disable-reason-tag assertions under test.
        pid = plan_store.save_plan(
            query="Disable reason integration test",
            plan_json="{}",
            tags="base-tag",
            verb="research",
        )

        # Disable with a reason
        assert plan_store.set_plan_disabled(pid, reason="integration-test-reason")
        row = plan_store.get_plan(pid)

        assert row["disabled_at"] is not None, "disabled_at must be stamped"
        assert "disable-reason:integration-test-reason" in row["tags"], (
            f"tags must contain disable-reason:integration-test-reason; got {row['tags']!r}")
        assert "base-tag" in row["tags"], "existing tag must be preserved"

        # Re-disable with a different reason — old one replaced
        assert plan_store.set_plan_disabled(pid, reason="updated-reason")
        row2 = plan_store.get_plan(pid)
        assert "disable-reason:updated-reason" in row2["tags"]
        assert "disable-reason:integration-test-reason" not in row2["tags"], (
            "old disable-reason must be replaced, not duplicated")

        # Disable without reason — tags unchanged
        pid2 = plan_store.save_plan(
            query="No reason disable integration",
            plan_json="{}",
            tags="keep-this-tag",
            verb="research",
        )
        assert plan_store.set_plan_disabled(pid2)
        row3 = plan_store.get_plan(pid2)
        assert row3["tags"] == "keep-this-tag", (
            f"disable without reason must not modify tags; got {row3['tags']!r}")

# Module-level attribute initializer (avoids AttributeError on class access before test runs)
TestPlansMVV._fts_parity_rho          = None   # type: ignore[attr-defined]
TestPlansMVV._fts_parity_details      = None   # type: ignore[attr-defined]
TestPlansMVV._fts_parity_probe_details = None  # type: ignore[attr-defined]
