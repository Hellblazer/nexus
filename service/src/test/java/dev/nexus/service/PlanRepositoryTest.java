package dev.nexus.service;

import dev.nexus.service.db.PlanRepository;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TenantScope;
import io.zonky.test.db.postgres.embedded.EmbeddedPostgres;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.*;

import java.sql.Connection;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Optional;

import static org.assertj.core.api.Assertions.*;
import static org.junit.jupiter.api.Assertions.assertThrows;

/**
 * RDR-152 bead nexus-gmiaf.11 — PlanRepository integration tests.
 *
 * <p>Hermetic embedded Postgres. Applies the full Liquibase master changelog
 * (memory + plans). Asserts:
 * <ol>
 *   <li>savePlan round-trip: id returned, row retrievable by id</li>
 *   <li>ON CONFLICT (tenant_id, project, query): second save with same key updates plan_json</li>
 *   <li>RLS isolation: tenant A plans invisible to tenant B</li>
 *   <li>delete: row removed by id</li>
 *   <li>disable/enable: disabled_at set/cleared; disabled plans excluded from listActivePlans</li>
 *   <li>searchPlans FTS: returns match on match_text ('english' config stemming)</li>
 *   <li>listActivePlans: returns only non-expired, non-disabled rows for the correct outcome</li>
 *   <li>incrementMatchMetrics: match_count increments; match_conf_sum increments when confidence given</li>
 *   <li>incrementRunStarted / incrementRunOutcome: counters update correctly</li>
 *   <li>importRow fidelity: all 7 fidelity fields (created_at + 6 counters incl. last_used) preserved</li>
 *   <li>planExists: boundary-safe tag match</li>
 *   <li>setScopeTags: field updated atomically</li>
 *   <li>listPlans: excludes disabled by default, includes when requested</li>
 *   <li>importRow GREATEST merge: re-import with stale source values does NOT clobber live PG counters</li>
 *   <li>RLS WITH CHECK: raw INSERT with mismatched tenant_id rejected by Postgres RLS policy</li>
 *   <li>disable with reason: appends disable-reason tag, replaces on re-disable, no-reason disable unchanged tags</li>
 * </ol>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class PlanRepositoryTest {

    private static final String TENANT_A = "plan-tenant-a";
    private static final String TENANT_B = "plan-tenant-b";
    private static final String SVC_ROLE = "svc_plan_test";
    private static final String SVC_PASS = "svc_plan_test_pass";

    EmbeddedPostgres pg;
    TenantScope tenantScope;
    PlanRepository repo;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = EmbeddedPostgres.builder().start();

        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN " +
                "    CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "'; " +
                "  END IF; " +
                "END $$");
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; " +
                "  END IF; " +
                "END $$");
        }

        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            Liquibase liquibase = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db);
            liquibase.update(new Contexts());
        }

        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.plans TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT USAGE ON SEQUENCE nexus.plans_id_seq TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }

        svcDs = buildSvcDataSource();
        tenantScope = new TenantScope(svcDs);
        repo = new PlanRepository(tenantScope);
    }

    @AfterAll
    void stopAll() throws Exception {
        if (svcDs != null) svcDs.close();
        if (pg != null)    pg.close();
    }

    @Test
    @Order(1)
    void savePlan_returnsId_andGetById_roundTrips() {
        long id = repo.savePlan(TENANT_A, "proj-a", "How to research RDRs",
                                "{\"steps\":[]}", "success", "research,rdr",
                                null, "rdr-research", "research", "global",
                                "{\"verb\":\"research\"}", null, null,
                                "rdr", "How to research RDRs. research rdr-research scope global");

        assertThat(id).as("savePlan must return a positive id").isPositive();

        var row = repo.getById(TENANT_A, id);
        assertThat(row).as("getById must return the saved plan").isPresent();
        assertThat(row.get().getQuery()).isEqualTo("How to research RDRs");
        assertThat(row.get().getPlanJson()).isEqualTo("{\"steps\":[]}");
        assertThat(row.get().getOutcome()).isEqualTo("success");
        assertThat(row.get().getTags()).isEqualTo("research,rdr");
        assertThat(row.get().getVerb()).isEqualTo("research");
        assertThat(row.get().getMatchText())
            .isEqualTo("How to research RDRs. research rdr-research scope global");
    }

    @Test
    @Order(2)
    void savePlan_onConflict_updatesPlanJson() {
        // Save initial
        long id1 = repo.savePlan(TENANT_A, "proj-conflict", "Conflict query test",
                                 "{\"v\":1}", "success", "test", null,
                                 null, null, null, null, null, null, "", "Conflict query test");
        // Save again with same (tenant, project, query) — must update plan_json
        long id2 = repo.savePlan(TENANT_A, "proj-conflict", "Conflict query test",
                                 "{\"v\":2}", "success", "test", null,
                                 null, null, null, null, null, null, "", "Conflict query test");

        // Both rows should be the same id (upsert)
        assertThat(id2).as("conflict save must return same id").isEqualTo(id1);
        var row = repo.getById(TENANT_A, id1);
        assertThat(row).isPresent();
        assertThat(row.get().getPlanJson())
            .as("plan_json must be updated on conflict").isEqualTo("{\"v\":2}");
    }

    @Test
    @Order(3)
    void rls_isolation_tenantBCannotSeeTenantsAPlans() {
        repo.savePlan(TENANT_A, "proj-rls", "Tenant A private plan",
                      "{}", "success", "", null, null, null, null, null, null, null, "", "");
        var result = repo.listPlans(TENANT_B, "proj-rls", 100, true);
        assertThat(result)
            .as("tenant B must not see tenant A's plans (RLS isolation)")
            .noneMatch(r -> "Tenant A private plan".equals(r.getQuery()));
    }

    @Test
    @Order(4)
    void delete_removesRow() {
        long id = repo.savePlan(TENANT_A, "proj-del", "Plan to delete",
                                "{}", "success", "", null,
                                null, null, null, null, null, null, "", "");
        assertThat(repo.getById(TENANT_A, id)).as("row exists before delete").isPresent();

        boolean deleted = repo.delete(TENANT_A, id);
        assertThat(deleted).as("delete must return true for existing row").isTrue();
        assertThat(repo.getById(TENANT_A, id)).as("row absent after delete").isEmpty();

        boolean notFound = repo.delete(TENANT_A, id);
        assertThat(notFound).as("delete of already-deleted row returns false").isFalse();
    }

    @Test
    @Order(5)
    void disable_and_enable_softDisable() {
        long id = repo.savePlan(TENANT_A, "proj-disable", "Plan to disable",
                                "{}", "success", "", null,
                                null, null, null, null, null, null, "", "");

        // Disable
        assertThat(repo.disable(TENANT_A, id)).isTrue();
        var row = repo.getById(TENANT_A, id);
        assertThat(row).isPresent();
        assertThat(row.get().getDisabledAt())
            .as("disabled_at must be set after disable").isNotNull();

        // listActivePlans excludes disabled
        var active = repo.listActivePlans(TENANT_A, "success", "proj-disable");
        assertThat(active)
            .as("listActivePlans must exclude disabled rows")
            .noneMatch(r -> r.getId().equals(id));

        // Enable
        assertThat(repo.enable(TENANT_A, id)).isTrue();
        row = repo.getById(TENANT_A, id);
        assertThat(row.get().getDisabledAt())
            .as("disabled_at must be null after enable").isNull();

        // listActivePlans now includes it
        active = repo.listActivePlans(TENANT_A, "success", "proj-disable");
        assertThat(active)
            .as("listActivePlans must include re-enabled row")
            .anyMatch(r -> r.getId().equals(id));
    }

    @Test
    @Order(6)
    void searchPlans_ftsStemming_matchesMatchText() {
        // Seed plan with match_text containing 'researching' (stem: 'research')
        repo.savePlan(TENANT_A, "fts-proj", "Walk from RDR to code modules",
                      "{\"steps\":[]}", "success", "research,rdr",
                      null, "walk-rdr", "research", "global",
                      "{\"verb\":\"research\"}", null, null, "rdr",
                      "Walk from RDR to code modules. research walk-rdr scope global");

        // Search with stem 'researching' — english config should match
        var results = repo.searchPlans(TENANT_A, "researching", "fts-proj", 10);
        assertThat(results)
            .as("searchPlans FTS (english stemming) must find 'research' when querying 'researching'")
            .isNotEmpty();
    }

    @Test
    @Order(7)
    void incrementMatchMetrics_countersUpdate() {
        long id = repo.savePlan(TENANT_A, "proj-metrics", "Metrics test plan",
                                "{}", "success", "", null,
                                null, null, null, null, null, null, "", "");

        // Without confidence (FTS fallback path)
        repo.incrementMatchMetrics(TENANT_A, id, null);
        var row = repo.getById(TENANT_A, id);
        assertThat(row.get().getMatchCount()).as("match_count must be 1 after first increment").isEqualTo(1);
        assertThat(row.get().getMatchConfSum()).as("match_conf_sum must still be 0 when confidence=null").isEqualTo(0.0);

        // With confidence
        repo.incrementMatchMetrics(TENANT_A, id, 0.85);
        row = repo.getById(TENANT_A, id);
        assertThat(row.get().getMatchCount()).as("match_count must be 2 after second increment").isEqualTo(2);
        assertThat(row.get().getMatchConfSum()).as("match_conf_sum must be 0.85").isEqualTo(0.85);
    }

    @Test
    @Order(8)
    void incrementRunStarted_and_incrementRunOutcome_update() {
        long id = repo.savePlan(TENANT_A, "proj-run", "Run metrics plan",
                                "{}", "success", "", null,
                                null, null, null, null, null, null, "", "");

        repo.incrementRunStarted(TENANT_A, id);
        var row = repo.getById(TENANT_A, id);
        assertThat(row.get().getUseCount()).isEqualTo(1);
        assertThat(row.get().getLastUsed()).isNotNull();

        repo.incrementRunOutcome(TENANT_A, id, true);
        row = repo.getById(TENANT_A, id);
        assertThat(row.get().getSuccessCount()).isEqualTo(1);
        assertThat(row.get().getFailureCount()).isEqualTo(0);

        repo.incrementRunOutcome(TENANT_A, id, false);
        row = repo.getById(TENANT_A, id);
        assertThat(row.get().getSuccessCount()).isEqualTo(1);
        assertThat(row.get().getFailureCount()).isEqualTo(1);
    }

    @Test
    @Order(9)
    void importRow_fidelity_preservesCountersAndTimestamp() {
        OffsetDateTime srcCreatedAt = OffsetDateTime.of(2025, 6, 1, 10, 0, 0, 0, ZoneOffset.UTC);
        OffsetDateTime srcLastUsed  = OffsetDateTime.of(2025, 6, 5, 12, 30, 0, 0, ZoneOffset.UTC);

        long id = repo.importRow(
            TenantConstants.DEFAULT_TENANT,
            "etl-proj", "ETL fidelity query", "{\"etl\":true}",
            "success", "etl,fidelity", srcCreatedAt, null,
            "etl-plan", "research", "global", "{\"verb\":\"research\"}",
            null, null,
            42,          // use_count
            srcLastUsed,
            99,          // match_count
            12.5,        // match_conf_sum
            40,          // success_count
            2,           // failure_count
            "knowledge", "ETL fidelity query. research etl-plan scope global",
            null);

        assertThat(id).isPositive();
        var row = repo.getById(TenantConstants.DEFAULT_TENANT, id);
        assertThat(row).isPresent();

        // Fidelity: created_at preserved
        assertThat(row.get().getCreatedAt().withOffsetSameInstant(ZoneOffset.UTC))
            .as("created_at must be preserved verbatim from source")
            .isEqualTo(srcCreatedAt);

        // Counters: preserved verbatim (not reset to 0)
        assertThat(row.get().getUseCount()).as("use_count must be 42").isEqualTo(42);
        assertThat(row.get().getMatchCount()).as("match_count must be 99").isEqualTo(99);
        assertThat(row.get().getMatchConfSum()).as("match_conf_sum must be 12.5").isEqualTo(12.5);
        assertThat(row.get().getSuccessCount()).as("success_count must be 40").isEqualTo(40);
        assertThat(row.get().getFailureCount()).as("failure_count must be 2").isEqualTo(2);
        // last_used: the 7th fidelity field — must not be lost
        assertThat(row.get().getLastUsed()).as("last_used must be preserved").isNotNull();
        assertThat(row.get().getLastUsed().withOffsetSameInstant(ZoneOffset.UTC))
            .as("last_used must equal srcLastUsed verbatim")
            .isEqualTo(srcLastUsed);

        // Idempotency: re-run with same data, same id, counters still from source
        long id2 = repo.importRow(
            TenantConstants.DEFAULT_TENANT,
            "etl-proj", "ETL fidelity query", "{\"etl\":true}",
            "success", "etl,fidelity", srcCreatedAt, null,
            "etl-plan", "research", "global", "{\"verb\":\"research\"}",
            null, null,
            42, srcLastUsed, 99, 12.5, 40, 2,
            "knowledge", "ETL fidelity query. research etl-plan scope global",
            null);

        assertThat(id2).as("idempotent re-import must return same id").isEqualTo(id);
    }

    @Test
    @Order(10)
    void planExists_boundaryTagMatch() {
        repo.savePlan(TENANT_A, "proj-exists", "Plan for exists check",
                      "{}", "success", "builtin-template,research", null,
                      null, null, null, null, null, null, "", "Plan for exists check");

        assertThat(repo.planExists(TENANT_A, "Plan for exists check", "builtin-template"))
            .as("planExists must return true for exact comma-bounded token").isTrue();
        assertThat(repo.planExists(TENANT_A, "Plan for exists check", "builtin"))
            .as("planExists must return false for prefix (not whole token)").isFalse();
        assertThat(repo.planExists(TENANT_A, "Plan for exists check", "not-there"))
            .as("planExists must return false for absent tag").isFalse();
    }

    @Test
    @Order(11)
    void setScopeTags_updatesField() {
        long id = repo.savePlan(TENANT_A, "proj-scope", "Scope tags test",
                                "{}", "success", "", null,
                                null, null, null, null, null, null, "", "");
        repo.setScopeTags(TENANT_A, id, "knowledge__nexus,rdr__nexus");
        var row = repo.getById(TENANT_A, id);
        assertThat(row.get().getScopeTags()).isEqualTo("knowledge__nexus,rdr__nexus");
    }

    @Test
    @Order(12)
    void listPlans_excludesDisabledByDefault_includesWhenRequested() {
        long activeId   = repo.savePlan(TENANT_A, "proj-list", "Active plan",
                                        "{}", "success", "", null, null, null, null, null, null, null, "", "");
        long disabledId = repo.savePlan(TENANT_A, "proj-list", "Disabled plan",
                                        "{}", "success", "", null, null, null, null, null, null, null, "", "");
        repo.disable(TENANT_A, disabledId);

        var excluded = repo.listPlans(TENANT_A, "proj-list", 100, false);
        assertThat(excluded).anyMatch(r -> r.getId().equals(activeId));
        assertThat(excluded).noneMatch(r -> r.getId().equals(disabledId));

        var included = repo.listPlans(TENANT_A, "proj-list", 100, true);
        assertThat(included).anyMatch(r -> r.getId().equals(activeId));
        assertThat(included).anyMatch(r -> r.getId().equals(disabledId));
    }

    @Test
    @Order(13)
    void importRow_greatestMerge_doesNotClobberLiveCounters() {
        // Seed via importRow with low source counters
        long id = repo.importRow(
            TENANT_A,
            "proj-greatest", "GREATEST merge test plan", "{\"v\":1}",
            "success", "test", OffsetDateTime.of(2025, 1, 1, 0, 0, 0, 0, ZoneOffset.UTC),
            null, null, null, null, null, null, null,
            5, null, 10, 2.5, 4, 1,     // low source counters
            "", "GREATEST merge test plan", null);

        // Simulate live traffic advancing counters on the PG side
        repo.incrementRunStarted(TENANT_A, id);
        repo.incrementRunStarted(TENANT_A, id);
        repo.incrementRunStarted(TENANT_A, id);     // use_count=3 (above source's 0? no, source=5)
        repo.incrementMatchMetrics(TENANT_A, id, 0.9);
        repo.incrementMatchMetrics(TENANT_A, id, 0.9);
        repo.incrementMatchMetrics(TENANT_A, id, 0.9);  // match_count = 10+3=13, conf_sum=2.5+2.7=5.2
        repo.incrementRunOutcome(TENANT_A, id, true);
        repo.incrementRunOutcome(TENANT_A, id, true);   // success_count=4+2=6

        // Verify PG counters are above source values (precondition for the test)
        var beforeReimport = repo.getById(TENANT_A, id).get();
        assertThat(beforeReimport.getMatchCount()).as("match_count must be above source after live increments")
            .isGreaterThan(10);
        int pgMatchCount = beforeReimport.getMatchCount();
        double pgConfSum   = beforeReimport.getMatchConfSum();
        int pgSuccessCount = beforeReimport.getSuccessCount();
        OffsetDateTime pgLastUsed = beforeReimport.getLastUsed();

        // Re-import with the STALE source values (lower counters)
        repo.importRow(
            TENANT_A,
            "proj-greatest", "GREATEST merge test plan", "{\"v\":1}",
            "success", "test", OffsetDateTime.of(2025, 1, 1, 0, 0, 0, 0, ZoneOffset.UTC),
            null, null, null, null, null, null, null,
            5, null, 10, 2.5, 4, 1,     // SAME stale source counters
            "", "GREATEST merge test plan", null);

        // Assert: live PG counters are NOT rolled back to stale source values
        var afterReimport = repo.getById(TENANT_A, id).get();
        assertThat(afterReimport.getMatchCount())
            .as("re-import must not clobber live match_count (GREATEST wins)")
            .isEqualTo(pgMatchCount);
        assertThat(afterReimport.getMatchConfSum())
            .as("re-import must not clobber live match_conf_sum (GREATEST wins)")
            .isEqualTo(pgConfSum);
        assertThat(afterReimport.getSuccessCount())
            .as("re-import must not clobber live success_count (GREATEST wins)")
            .isEqualTo(pgSuccessCount);
        assertThat(afterReimport.getLastUsed())
            .as("re-import must not clobber live last_used with stale null (GREATEST null-safe)")
            .isEqualTo(pgLastUsed);
    }

    @Test
    @Order(14)
    void rlsWithCheck_crossTenantInsert_rejected() {
        // The service role has FORCE RLS with tenant_isolation WITH CHECK.
        // Attempting to INSERT with tenant_id != GUC must raise a PSQLException.
        // We stamp TENANT_A in the GUC but try to insert with TENANT_B manually.
        // TenantScope.withTenant stamps the GUC, so we reach below it via raw SQL.
        assertThatThrownBy(() -> {
            try (var conn = svcDs.getConnection()) {
                conn.setAutoCommit(true);
                // Set GUC to TENANT_A but try to insert with TENANT_B — WITH CHECK violation
                conn.createStatement().execute(
                    "SET LOCAL nexus.tenant = '" + TENANT_A + "'");
                conn.createStatement().execute(
                    "INSERT INTO nexus.plans (tenant_id, project, query, plan_json) " +
                    "VALUES ('" + TENANT_B + "', 'bad-proj', 'RLS violation test', '{}')");
            }
        })
        .as("RLS WITH CHECK must reject INSERT where tenant_id != nexus.tenant GUC")
        .isInstanceOfAny(org.postgresql.util.PSQLException.class,
                         java.sql.SQLException.class);
    }

    @Test
    @Order(15)
    void disable_withReason_appendsTagAndStampsDisabledAt() {
        long id = repo.savePlan(TENANT_A, "proj-reason", "Plan for disable-reason test",
                                "{}", "success", "existing-tag", null,
                                null, null, null, null, null, null, "", "");

        // Disable with a reason
        assertThat(repo.disable(TENANT_A, id, "too slow")).isTrue();
        var row = repo.getById(TENANT_A, id).get();

        assertThat(row.getDisabledAt())
            .as("disabled_at must be stamped").isNotNull();
        assertThat(row.getTags())
            .as("tags must contain disable-reason:too slow")
            .contains("disable-reason:too slow");
        assertThat(row.getTags())
            .as("existing tag must be preserved")
            .contains("existing-tag");

        // Re-disable with a different reason — old disable-reason: replaced, not duplicated
        assertThat(repo.disable(TENANT_A, id, "replaced reason")).isTrue();
        row = repo.getById(TENANT_A, id).get();
        assertThat(row.getTags())
            .as("tags must have the NEW disable-reason only (old one replaced)")
            .contains("disable-reason:replaced reason");
        assertThat(row.getTags())
            .as("old disable-reason must be removed")
            .doesNotContain("disable-reason:too slow");

        // Disable without reason — no tag added; existing tags unchanged
        long id2 = repo.savePlan(TENANT_A, "proj-reason", "Plan for no-reason disable",
                                 "{}", "success", "tag-a", null,
                                 null, null, null, null, null, null, "", "");
        assertThat(repo.disable(TENANT_A, id2)).isTrue();
        row = repo.getById(TENANT_A, id2).get();
        assertThat(row.getTags()).as("no-reason disable must not modify tags").isEqualTo("tag-a");
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private com.zaxxer.hikari.HikariDataSource buildSvcDataSource() {
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl("jdbc:postgresql://localhost:" + pg.getPort() + "/postgres");
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        return new com.zaxxer.hikari.HikariDataSource(cfg);
    }
}
