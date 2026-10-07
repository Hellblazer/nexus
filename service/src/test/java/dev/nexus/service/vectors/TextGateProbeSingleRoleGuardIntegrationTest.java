// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgCatalogProbes;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.ChunksIsolationCheck;
import dev.nexus.service.db.SchemaMigrator;
import dev.nexus.service.db.TenantScope;
import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.DirectoryResourceAccessor;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.concurrent.TimeUnit;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATE_PROBE_384;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * The guard on vectors-029's owner policy (nexus-wbfpw.48, the ship blocker of the first review round).
 *
 * <p>The changeset creates {@code CREATE POLICY chunks_gate_probe_owner_read ON nexus.chunks FOR SELECT TO
 * CURRENT_USER USING (true)}, which binds to whoever migrates. In the documented single-role dev posture
 * ({@code Main.buildMigrationDataSource} falls back to {@code NX_DB_*} when {@code NX_DB_ADMIN_*} is unset)
 * the migrator IS {@code nexus_svc}, and the unguarded policy would let the service role read every tenant's
 * chunks on every path. The changeset therefore carries a precondition (MARK_RAN) that skips the WHOLE
 * changeset when nexus_svc is, or has the privileges of, the migrating role.
 *
 * <p>This class migrates the product schema AS {@code nexus_svc} in a dedicated container (the owner
 * bootstrap with {@code nexus_svc} as the owner role) and asserts: no policy on nexus.chunks names
 * nexus_svc except the tenant policy, the three probes are not SECURITY DEFINER, and the end-to-end
 * isolation is intact (a tenant-stamped nexus_svc session sees its own tenant's chunks through the table and
 * through the probe, and nothing of another tenant). The two-role posture, where the policy must exist and
 * name the owner alone, is {@link Rdr192LiveCExplainEvidenceIntegrationTest}.
 *
 * <p>The membership shapes (second review round) each get a dedicated container migrated as
 * {@code nexus_admin}: {@code nexus_svc} a member WITH INHERIT TRUE (the changeset must be skipped) and WITH
 * INHERIT FALSE (it must run, since the role does not have the owner's privileges), another login role that
 * inherits (skipped), a BYPASSRLS one (not skipped: the policy gives it nothing), a post-condition forced by
 * running the changeset with its precondition removed (the walk aborts and nothing of vectors-029 persists),
 * and the runtime backstop, {@link ChunksIsolationCheck}, which refuses to boot a service whose role gained
 * the owner's privileges after the changeset ran.
 *
 * <p>Falsified by hand: with the precondition removed, the single-role container cannot even be set up
 * (the post-condition aborts the walk in {@code @BeforeAll}); with the precondition AND the post-condition's
 * role check removed, the first test fails (the policy exists and its roles are {nexus_svc}). Removing the
 * membership clause from the precondition fails the INHERIT TRUE test; removing the boot check fails the
 * boot test.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TextGateProbeSingleRoleGuardIntegrationTest {

    private static final String SVC = PgContainerHelper.SVC_USERNAME;
    private static final String TENANT_A = "single-role-a";
    private static final String TENANT_B = "single-role-b";
    private static final String COLL_A = "knowledge__single-role-a__minilm-l6-v2-384__v1";
    private static final String COLL_B = "knowledge__single-role-b__minilm-l6-v2-384__v1";
    private static final String TOKEN = "qzvkwx";
    private static final int PER_TENANT = 3;
    private static final int DIM = 384;

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.startDedicated();
        try (Connection su = pg.createConnection("")) {
            // nexus_svc IS the owner role: the single-role posture.
            PgContainerHelper.bootstrapNonSuperuserOwner(su, SVC, PgContainerHelper.SVC_PASSWORD);
        }
        // Exactly what Main.buildMigrationDataSource builds with NX_DB_ADMIN_* unset: a pool as nexus_svc.
        try (HikariDataSource migrate = svcPool("single-role-migrate", 2)) {
            SchemaMigrator.migrate(migrate);
        }
        svcDs = svcPool("single-role-svc", 4);
        tenantScope = new TenantScope(svcDs);
        seed();
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private HikariDataSource svcPool(String name, int size) {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(size);
        cfg.setPoolName(name);
        cfg.setAutoCommit(true);
        return new HikariDataSource(cfg);
    }

    private List<String> seedTenant(DSLContext ctx, String tenant, String coll, long seed) {
        PgContainerHelper.insertCollection(ctx, tenant, coll);
        Random rnd = new Random(seed);
        List<String> ids = new java.util.ArrayList<>();
        List<String> texts = new java.util.ArrayList<>();
        List<float[]> vecs = new java.util.ArrayList<>();
        List<Map<String, Object>> metas = new java.util.ArrayList<>();
        for (int i = 0; i < PER_TENANT; i++) {
            ids.add(Chash.ofText(tenant + "-chunk-" + i).toHex());
            texts.add(TOKEN + " single role chunk " + i + " of " + tenant);
            float[] v = new float[DIM];
            for (int k = 0; k < DIM; k++) v[k] = (float) rnd.nextGaussian();
            vecs.add(v);
            metas.add(Map.of());
        }
        PgContainerHelper.insertChunks(ctx, tenant, coll, ids, texts, vecs, metas);
        PgContainerHelper.ownChunks(ctx, tenant, coll, ids.toArray(String[]::new));
        return ids;
    }

    List<String> idsA;
    List<String> idsB;

    private void seed() throws Exception {
        // The superuser seeds both tenants (RLS bypassed); nexus_svc, the owner here, is then asked what it sees.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            idsA = seedTenant(ctx, TENANT_A, COLL_A, 1L);
            idsB = seedTenant(ctx, TENANT_B, COLL_B, 2L);
        }
    }

    /**
     * THE GUARD. Migrated as nexus_svc, vectors-029-1 must have been skipped whole: no
     * chunks_gate_probe_owner_read policy, no policy on nexus.chunks whose roles name nexus_svc other than the
     * tenant policy, and the three probes left as the SECURITY INVOKER functions vectors-023 made.
     */
    @Test
    void migratedAsTheServiceRole_theOwnerPolicyIsNeverCreated_andTheProbesStayInvoker() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            var policies = PgCatalogProbes.policyRoles(ctx, "nexus", "chunks");
            assertThat(policies.stream().map(PgCatalogProbes.PolicyRoles::toString).toList())
                .as("the only policy on nexus.chunks is the tenant policy, bound to PUBLIC; no policy names nexus_svc")
                .containsExactly("tenant_isolation roles={public} cmd=ALL");
            assertThat(PgCatalogProbes.policyExists(ctx, "nexus", "chunks", "chunks_gate_probe_owner_read")).isFalse();
            var probes = PgCatalogProbes.functionShapesIn(ctx, List.of("nexus")).stream()
                .filter(f -> f.identity().startsWith("text_gate_probe_")).toList();
            assertThat(probes).as("the three gate probes exist (the changelog walked past vectors-023)").hasSize(3);
            for (var f : probes) {
                assertThat(f.securityDefiner()).as("%s must stay SECURITY INVOKER", f).isFalse();
                assertThat(f.owner()).as("%s is owned by the migrating role, nexus_svc", f).isEqualTo(SVC);
            }
            var changeset = DSL.using(su, SQLDialect.POSTGRES).fetchOne(
                DSL.select(DSL.count()).from(DSL.table(DSL.name("databasechangelog")))
                    .where(DSL.field(DSL.name("id"), String.class).eq("vectors-029-1"))
                    .and(DSL.field(DSL.name("exectype"), String.class).eq("MARK_RAN")));
            assertThat(changeset.get(0, Integer.class))
                .as("vectors-029-1 is recorded as MARK_RAN: the guard fired, it did not run and fail").isEqualTo(1);
        }
    }

    /**
     * End to end under the single-role posture: the service role, tenant-stamped, sees its own tenant's chunks
     * through the table and through the probe and nothing of the other tenant's. Not vacuous: each tenant has
     * rows, and the superuser sees both.
     */
    @Test
    void migratedAsTheServiceRole_aTenantStampedSessionSeesOnlyItsOwnTenant() throws Exception {
        try (Connection su = pg.createConnection("")) {
            assertThat(DSL.using(su, SQLDialect.POSTGRES).fetchCount(CHUNKS))
                .as("the superuser sees both tenants' chunks").isEqualTo(2 * PER_TENANT);
        }
        int seenByA = tenantScope.withTenant(TENANT_A, ctx -> ctx.fetchCount(CHUNKS));
        int seenByB = tenantScope.withTenant(TENANT_B, ctx -> ctx.fetchCount(CHUNKS));
        assertThat(seenByA).as("nexus_svc stamped A sees only A's chunks through the table").isEqualTo(PER_TENANT);
        assertThat(seenByB).as("nexus_svc stamped B sees only B's chunks through the table").isEqualTo(PER_TENANT);
        var probe = TEXT_GATE_PROBE_384.call(TOKEN, new String[] {COLL_A, COLL_B}, null, null, 10_000);
        List<String> fromA = tenantScope.withTenant(TENANT_A, ctx -> ctx.selectFrom(probe).fetch()
            .getValues(0, byte[].class).stream().map(b -> HexFormat.of().formatHex(b)).toList());
        assertThat(fromA).as("the probe stamped A, naming both collections: A's chunks only")
            .containsExactlyInAnyOrderElementsOf(idsA);
        List<String> fromB = tenantScope.withTenant(TENANT_B, ctx -> ctx.selectFrom(probe).fetch()
            .getValues(0, byte[].class).stream().map(b -> HexFormat.of().formatHex(b)).toList());
        assertThat(fromB).as("the probe stamped B, naming both collections: B's chunks only")
            .containsExactlyInAnyOrderElementsOf(idsB);
    }

    // ---------------------------------------------------------------------------------------------------
    // Two-role posture, membership shapes (nexus-wbfpw.48, second review round). Each test owns a dedicated
    // container migrated as nexus_admin, because role membership and schema ownership are global state.
    // ---------------------------------------------------------------------------------------------------

    private static final String ADMIN = "nexus_admin";
    private static final String ADMIN_PASS = "nexus_admin_guard_pass";
    private static final String EXTRA = "nexus_extra_login";

    /** What a scenario does on the superuser connection after the owner is bootstrapped and before the walk. */
    private interface PreMigrate {
        void run(Connection su) throws Exception;
    }

    private HikariDataSource pool(PostgreSQLContainer<?> c, String user, String pass, String name, int size) {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(c.getJdbcUrl());
        cfg.setUsername(user);
        cfg.setPassword(pass);
        cfg.setMaximumPoolSize(size);
        cfg.setPoolName(name);
        cfg.setAutoCommit(true);
        return new HikariDataSource(cfg);
    }

    /** A dedicated container: owner bootstrapped, {@code pre} run, walked AS nexus_admin, both tenants seeded. */
    private PostgreSQLContainer<?> migratedAsAdmin(PreMigrate pre) throws Exception {
        PostgreSQLContainer<?> c = PgContainerHelper.startDedicated();
        try {
            try (Connection su = c.createConnection("")) {
                PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN, ADMIN_PASS);
                su.setAutoCommit(true);
                pre.run(su);
            }
            try (HikariDataSource migrate = pool(c, ADMIN, ADMIN_PASS, "guard-migrate", 2)) {
                SchemaMigrator.migrate(migrate);
            }
            try (Connection su = c.createConnection("")) {
                su.setAutoCommit(true);
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                idsA = seedTenant(ctx, TENANT_A, COLL_A, 1L);
                idsB = seedTenant(ctx, TENANT_B, COLL_B, 2L);
            }
            return c;
        } catch (Throwable t) {
            c.stop();
            throw t;
        }
    }

    private static List<String> execTypes(Connection su) {
        return DSL.using(su, SQLDialect.POSTGRES).select(DSL.field(DSL.name("exectype"), String.class))
            .from(DSL.table(DSL.name("databasechangelog")))
            .where(DSL.field(DSL.name("id"), String.class).eq("vectors-029-1"))
            .orderBy(DSL.field(DSL.name("orderexecuted")))
            .fetch(0, String.class);
    }

    private static boolean hasRole(DSLContext ctx, String user, String role) {
        return Boolean.TRUE.equals(ctx.select(DSL.function("pg_has_role", org.jooq.impl.SQLDataType.BOOLEAN,
                DSL.val(user), DSL.val(role), DSL.inline("USAGE"))).fetchOne(0, Boolean.class));
    }

    private static List<String> probeIds(TenantScope scope, String tenant) {
        var probe = TEXT_GATE_PROBE_384.call(TOKEN, new String[] {COLL_A, COLL_B}, null, null, 10_000);
        return scope.withTenant(tenant, ctx -> ctx.selectFrom(probe).fetch()
            .getValues(0, byte[].class).stream().map(b -> HexFormat.of().formatHex(b)).toList());
    }

    /** The service role, tenant-stamped, sees its own tenant's chunks only: through the table and the probe. */
    private void assertServiceSeesOnlyItsOwnTenant(PostgreSQLContainer<?> c) {
        try (HikariDataSource svc = pool(c, SVC, PgContainerHelper.SVC_PASSWORD, "guard-svc", 4)) {
            var scope = new TenantScope(svc);
            int seenByA = scope.withTenant(TENANT_A, ctx -> ctx.fetchCount(CHUNKS));
            int seenByB = scope.withTenant(TENANT_B, ctx -> ctx.fetchCount(CHUNKS));
            assertThat(seenByA).as("nexus_svc stamped A sees only A's chunks through the table").isEqualTo(PER_TENANT);
            assertThat(seenByB).as("nexus_svc stamped B sees only B's chunks through the table").isEqualTo(PER_TENANT);
            assertThat(probeIds(scope, TENANT_A)).as("probe stamped A").containsExactlyInAnyOrderElementsOf(idsA);
            assertThat(probeIds(scope, TENANT_B)).as("probe stamped B").containsExactlyInAnyOrderElementsOf(idsB);
        }
    }

    private static List<PgCatalogProbes.FunctionShape> definerProbes(DSLContext ctx) {
        return PgCatalogProbes.functionShapesIn(ctx, List.of("nexus")).stream()
            .filter(f -> f.identity().startsWith("text_gate_probe_")).filter(f -> f.securityDefiner()).toList();
    }

    private static void assertChangesetSkipped(Connection su, String why) {
        var ctx = DSL.using(su, SQLDialect.POSTGRES);
        assertThat(execTypes(su)).as("vectors-029-1 exec type: %s", why).containsExactly("MARK_RAN");
        assertThat(PgCatalogProbes.policyExists(ctx, "nexus", "chunks", "chunks_gate_probe_owner_read"))
            .as("no owner policy: %s", why).isFalse();
        assertThat(definerProbes(ctx)).as("every probe stays SECURITY INVOKER: %s", why).isEmpty();
    }

    /** I2: the membership branch of the precondition. Fails when pg_has_role(r.oid, current_user) is removed. */
    @Test
    void nexusSvcInheritingTheMigratingRole_theChangesetIsSkipped_andNothingIsExposed() throws Exception {
        PostgreSQLContainer<?> c = migratedAsAdmin(su ->
            PgContainerHelper.grantRoleMembership(su, ADMIN, SVC, true, false));
        try (Connection su = c.createConnection("")) {
            assertThat(hasRole(DSL.using(su, SQLDialect.POSTGRES), SVC, ADMIN))
                .as("non-vacuity: nexus_svc really has nexus_admin's privileges here").isTrue();
            assertChangesetSkipped(su, "nexus_svc is a member of the migrator WITH INHERIT TRUE");
            assertServiceSeesOnlyItsOwnTenant(c);
            try (HikariDataSource svc = pool(c, SVC, PgContainerHelper.SVC_PASSWORD, "guard-svc-check", 1)) {
                assertThat(ChunksIsolationCheck.violations(svc)).as("boot check on the service pool").isEmpty();
            }
        } finally {
            c.stop();
        }
    }

    /** I2, the other side: INHERIT FALSE means the role does not have the owner's privileges, so it must run. */
    @Test
    void nexusSvcMemberWithInheritFalse_theChangesetRuns_andTheServiceStillSeesOnlyItsTenant() throws Exception {
        PostgreSQLContainer<?> c = migratedAsAdmin(su ->
            PgContainerHelper.grantRoleMembership(su, ADMIN, SVC, false, false));
        try (Connection su = c.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(hasRole(ctx, SVC, ADMIN))
                .as("non-vacuity: a member that does not inherit has no USAGE of the role").isFalse();
            assertThat(execTypes(su)).as("the guard did not over-skip").containsExactly("EXECUTED");
            assertThat(PgCatalogProbes.policyRoles(ctx, "nexus", "chunks").stream()
                    .map(PgCatalogProbes.PolicyRoles::toString).toList())
                .containsExactlyInAnyOrder("tenant_isolation roles={public} cmd=ALL",
                    "chunks_gate_probe_owner_read roles={" + ADMIN + "} cmd=SELECT");
            assertThat(definerProbes(ctx)).as("the three probes are SECURITY DEFINER").hasSize(3);
            assertServiceSeesOnlyItsOwnTenant(c);
            try (HikariDataSource svc = pool(c, SVC, PgContainerHelper.SVC_PASSWORD, "guard-svc-check", 1)) {
                assertThat(ChunksIsolationCheck.violations(svc)).as("boot check on the service pool").isEmpty();
            }
        } finally {
            c.stop();
        }
    }

    /** I3: a login role that is NOT nexus_svc and inherits the migrating role is guarded too. */
    @Test
    void anotherLoginRoleInheritingTheMigratingRole_theChangesetIsSkipped() throws Exception {
        PostgreSQLContainer<?> c = migratedAsAdmin(su ->
            PgContainerHelper.grantRoleMembership(su, ADMIN, EXTRA, true, false));
        try (Connection su = c.createConnection("")) {
            assertThat(hasRole(DSL.using(su, SQLDialect.POSTGRES), SVC, ADMIN))
                .as("nexus_svc is not involved in this case").isFalse();
            assertChangesetSkipped(su, EXTRA + " is a login role with nexus_admin's privileges");
        } finally {
            c.stop();
        }
    }

    /** The exclusion: BYPASSRLS is not bound by the policy, so such a member must not skip the changeset. */
    @Test
    void aBypassRlsMemberOfTheMigratingRole_doesNotSkipTheChangeset() throws Exception {
        PostgreSQLContainer<?> c = migratedAsAdmin(su ->
            PgContainerHelper.grantRoleMembership(su, ADMIN, EXTRA, true, true));
        try (Connection su = c.createConnection("")) {
            assertThat(hasRole(DSL.using(su, SQLDialect.POSTGRES), EXTRA, ADMIN)).as("non-vacuity").isTrue();
            assertThat(execTypes(su)).as("RLS does not bind a BYPASSRLS role, nothing to guard")
                .containsExactly("EXECUTED");
        } finally {
            c.stop();
        }
    }

    /**
     * I3: the post-condition RAISE. The precondition is stripped from a copy of the changelog and the copy is
     * run through Liquibase as the migrator, with nexus_svc inheriting the migrator: the walk must abort with
     * the post-condition's own message, and nothing of vectors-029 may persist (policy, definer functions,
     * grants, a changelog row).
     */
    @Test
    void postConditionRaises_whenThePreconditionIsRemoved_andTheWholeChangesetRollsBack(
            @org.junit.jupiter.api.io.TempDir Path tmp) throws Exception {
        PostgreSQLContainer<?> c = migratedAsAdmin(su ->
            PgContainerHelper.grantRoleMembership(su, ADMIN, SVC, true, false));
        try (Connection su = c.createConnection("")) {
            assertChangesetSkipped(su, "baseline: the real changeset was skipped");
            String xml;
            try (InputStream in = getClass().getClassLoader()
                    .getResourceAsStream("db/changelog/vectors-029-text-gate-probe-definer.xml")) {
                assertThat(in).as("vectors-029 on the classpath").isNotNull();
                xml = new String(in.readAllBytes(), StandardCharsets.UTF_8);
            }
            String stripped = xml.replaceAll("(?s)<preConditions.*?</preConditions>", "");
            assertThat(stripped).as("the precondition really was removed").doesNotContain("<preConditions")
                .contains("POST-CONDITION");
            Files.writeString(tmp.resolve("vectors-029-no-precondition.xml"), stripped);

            try (HikariDataSource adminDs = pool(c, ADMIN, ADMIN_PASS, "guard-forced", 1);
                 Connection conn = adminDs.getConnection()) {
                Database database = DatabaseFactory.getInstance()
                    .findCorrectDatabaseImplementation(new JdbcConnection(conn));
                database.setLiquibaseSchemaName("public");
                database.setDefaultSchemaName("public");
                try (Liquibase lb = new Liquibase("vectors-029-no-precondition.xml",
                        new DirectoryResourceAccessor(tmp), database)) {
                    assertThatThrownBy(() -> lb.update(new Contexts(), new LabelExpression()))
                        .as("the post-condition RAISE aborts the walk")
                        .hasStackTraceContaining("have the privileges of the migrating role")
                        .hasStackTraceContaining(SVC);
                }
            }

            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(execTypes(su)).as("no EXECUTED row: the failed changeset left no changelog row")
                .containsExactly("MARK_RAN");
            assertThat(PgCatalogProbes.policyExists(ctx, "nexus", "chunks", "chunks_gate_probe_owner_read"))
                .as("the policy the failed changeset created was rolled back").isFalse();
            assertThat(definerProbes(ctx)).as("the definer bodies were rolled back with it").isEmpty();
            assertServiceSeesOnlyItsOwnTenant(c);
        } finally {
            c.stop();
        }
    }

    /**
     * I1: the runtime backstop. nexus_admin ran the walk and created the owner policy while nobody inherited
     * it; afterwards a membership is granted to nexus_svc AND to a differently named role (the service may
     * connect as whatever NX_DB_USER names). Identity-based, so both are caught: the check on each pool, the
     * status supplier, and a REAL Main process, which must exit 1 naming the policy.
     */
    @Test
    void aMembershipGrantedAfterTheWalk_isCaughtByTheBootCheck_andMainRefusesToStart() throws Exception {
        PostgreSQLContainer<?> c = migratedAsAdmin(su -> { });
        try {
            try (Connection su = c.createConnection("")) {
                assertThat(execTypes(su)).containsExactly("EXECUTED");
                // The clean state first, so the contrast below is the grant and nothing else.
                try (HikariDataSource svc = pool(c, SVC, PgContainerHelper.SVC_PASSWORD, "guard-svc-clean", 1)) {
                    assertThat(ChunksIsolationCheck.violations(svc)).as("clean two-role posture").isEmpty();
                    ChunksIsolationCheck.verifyAtStartup(svc);
                    assertThat(awaitAnswer(svc)).isTrue();
                    assertThat(statusBody(svc, "\"chunks_tenant_isolation_intact\":true"))
                        .as("NexusService wires the field").contains("\"chunks_tenant_isolation_intact\":true");
                }
                PgContainerHelper.grantRoleMembership(su, ADMIN, SVC, true, false);
                PgContainerHelper.grantRoleMembership(su, ADMIN, EXTRA, true, false);
            }
            for (String[] who : new String[][] {{SVC, PgContainerHelper.SVC_PASSWORD},
                                                 {EXTRA, PgContainerHelper.MEMBER_PASSWORD}}) {
                try (HikariDataSource svc = pool(c, who[0], who[1], "guard-" + who[0], 1)) {
                    assertThat(ChunksIsolationCheck.violations(svc).stream().map(Object::toString).toList())
                        .as("%s inherits nexus_admin, so the owner policy applies to it", who[0])
                        .containsExactly("Violation[policy=chunks_gate_probe_owner_read, role=" + ADMIN + "]");
                    assertThatThrownBy(() -> ChunksIsolationCheck.verifyAtStartup(svc))
                        .isInstanceOf(ChunksIsolationCheck.IsolationException.class)
                        .hasMessageContaining("chunks_gate_probe_owner_read")
                        .hasMessageContaining(who[0])
                        .hasMessageContaining(ADMIN);
                }
            }
            // The status supplier asks live (a grant made after boot shows) and answers from a cache for 30 seconds.
            try (HikariDataSource svc = pool(c, EXTRA, PgContainerHelper.MEMBER_PASSWORD, "guard-status", 1)) {
                var now = new Instant[] {Instant.parse("2026-10-04T12:00:00Z")};
                Clock clock = new Clock() {
                    @Override public ZoneOffset getZone() { return ZoneOffset.UTC; }
                    @Override public Clock withZone(java.time.ZoneId z) { return this; }
                    @Override public Instant instant() { return now[0]; }
                };
                var status = ChunksIsolationCheck.statusSupplier(svc, clock);
                assertThat(awaitNonNull(status)).as("status field is false while the policy applies").isFalse();
                now[0] = now[0].plusSeconds(1);
                assertThat(status.get()).isFalse();
                assertThat(statusBody(svc, "\"chunks_tenant_isolation_intact\":false"))
                    .as("NexusService wires the field").contains("\"chunks_tenant_isolation_intact\":false");
            }
            assertMainRefusesToStart(c);
        } finally {
            c.stop();
        }
    }

    /**
     * What a real NexusService on {@code ds} answers for GET /v1/status: the wiring, not just the handler. The
     * field is refreshed off the request thread, so poll until {@code fragment} shows (or give up and return
     * the last body for the assertion to describe).
     */
    private static String statusBody(HikariDataSource ds, String fragment) throws Exception {
        var service = new dev.nexus.service.NexusService(0, "guard-status-token", ds);
        service.start();
        try {
            String body = "";
            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(20);
            do {
                var resp = dev.nexus.service.TestHttp.client().send(
                    dev.nexus.service.TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/status").GET().build(),
                    java.net.http.HttpResponse.BodyHandlers.ofString());
                assertThat(resp.statusCode()).isEqualTo(200);
                body = resp.body();
                if (body.contains(fragment)) {
                    return body;
                }
                Thread.sleep(100);
            } while (System.nanoTime() < deadline);
            return body;
        } finally {
            service.stop();
        }
    }

    /** The status supplier's answer is computed in the background; poll until it has one. */
    private static Boolean awaitNonNull(java.util.function.Supplier<Boolean> status) throws InterruptedException {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(20);
        Boolean v;
        while ((v = status.get()) == null && System.nanoTime() < deadline) {
            Thread.sleep(50);
        }
        return v;
    }

    /** A fresh supplier's first answer (its construction primes a refresh). */
    private static Boolean awaitAnswer(HikariDataSource ds) throws InterruptedException {
        return awaitNonNull(ChunksIsolationCheck.statusSupplier(ds, Clock.systemUTC()));
    }

    // ---------------------------------------------------------------------------------------------------
    // Third review round (nexus-wbfpw.48): the branches of ChunksIsolationCheck.violations that no test could
    // fail, and the status field's structural half.
    // ---------------------------------------------------------------------------------------------------

    private static void exec(Connection su, String ddl) throws Exception {
        PgContainerHelper.runSuperuserDdl(su, ddl);
    }

    /**
     * I1: a role RLS does not bind is never a violation, even though pg_has_role answers true for a superuser
     * for every role and the owner policy exists. Three shapes: a SUPERUSER without the BYPASSRLS attribute
     * (isolates the rolsuper clause), a BYPASSRLS login that inherits the owner (isolates rolbypassrls), and,
     * as the control that the policy really does apply in this same container, an ordinary inheriting login.
     */
    @Test
    void aRoleRlsDoesNotBind_isNeverAViolation_whileAnOrdinaryInheritingRoleIs() throws Exception {
        String superLogin = "nexus_super_nobypass";
        String bypassLogin = "nexus_bypass_login";
        PostgreSQLContainer<?> c = migratedAsAdmin(su -> { });
        try {
            try (Connection su = c.createConnection("")) {
                assertThat(execTypes(su)).as("the owner policy exists in this container").containsExactly("EXECUTED");
                exec(su, "CREATE ROLE " + superLogin + " LOGIN PASSWORD '" + PgContainerHelper.MEMBER_PASSWORD
                    + "' SUPERUSER NOBYPASSRLS");
                PgContainerHelper.grantRoleMembership(su, ADMIN, bypassLogin, true, true);
                PgContainerHelper.grantRoleMembership(su, ADMIN, EXTRA, true, false);
                var ctx = DSL.using(su, SQLDialect.POSTGRES);
                assertThat(hasRole(ctx, superLogin, ADMIN)).as("non-vacuity: a superuser has every role's privileges").isTrue();
                assertThat(hasRole(ctx, bypassLogin, ADMIN)).as("non-vacuity: the bypass login inherits the owner").isTrue();
            }
            try (HikariDataSource p = pool(c, superLogin, PgContainerHelper.MEMBER_PASSWORD, "guard-super", 1)) {
                assertThat(ChunksIsolationCheck.violations(p)).as("SUPERUSER (not BYPASSRLS): RLS binds nobody here").isEmpty();
                ChunksIsolationCheck.verifyAtStartup(p);
            }
            try (HikariDataSource p = pool(c, bypassLogin, PgContainerHelper.MEMBER_PASSWORD, "guard-bypass", 1)) {
                assertThat(ChunksIsolationCheck.violations(p)).as("BYPASSRLS: RLS binds it to nothing").isEmpty();
                ChunksIsolationCheck.verifyAtStartup(p);
            }
            try (HikariDataSource p = pool(c, EXTRA, PgContainerHelper.MEMBER_PASSWORD, "guard-ordinary", 1)) {
                assertThat(ChunksIsolationCheck.violations(p)).as("control: the same policy applies to an ordinary role")
                    .hasSize(1);
            }
        } finally {
            c.stop();
        }
    }

    /**
     * I1: only PERMISSIVE policies count, and a policy for PUBLIC counts. A RESTRICTIVE USING (true) policy TO
     * PUBLIC only narrows (empty); the same policy PERMISSIVE widens, and the violation names PUBLIC.
     */
    @Test
    void aRestrictivePublicPolicyIsIgnored_aPermissivePublicPolicyIsOneViolationNamingPublic() throws Exception {
        PostgreSQLContainer<?> c = migratedAsAdmin(su -> { });
        try {
            try (HikariDataSource svc = pool(c, SVC, PgContainerHelper.SVC_PASSWORD, "guard-svc-public", 1)) {
                assertThat(ChunksIsolationCheck.violations(svc)).as("clean two-role posture").isEmpty();
                try (Connection su = c.createConnection("")) {
                    exec(su, "CREATE POLICY only_narrows ON nexus.chunks AS RESTRICTIVE FOR ALL TO PUBLIC USING (true)");
                }
                assertThat(ChunksIsolationCheck.violations(svc)).as("RESTRICTIVE policies only narrow").isEmpty();
                ChunksIsolationCheck.verifyAtStartup(svc);
                try (Connection su = c.createConnection("")) {
                    exec(su, "CREATE POLICY opens_to_all ON nexus.chunks AS PERMISSIVE FOR ALL TO PUBLIC USING (true)");
                }
                assertThat(ChunksIsolationCheck.violations(svc).stream().map(Object::toString).toList())
                    .as("a PERMISSIVE policy for PUBLIC reaches every role")
                    .containsExactly("Violation[policy=opens_to_all, role=public]");
                assertThatThrownBy(() -> ChunksIsolationCheck.verifyAtStartup(svc))
                    .isInstanceOf(ChunksIsolationCheck.IsolationException.class)
                    .hasMessageContaining("opens_to_all").hasMessageContaining("through PUBLIC")
                    .hasMessageContaining("read or write every tenant's chunks");
            }
        } finally {
            c.stop();
        }
    }

    /**
     * M1: the status field is also false when row security on nexus.chunks is not enabled or not forced, or the
     * tenant policy is gone. Since RDR-225 (nexus-3wh8d.16) the boot check asks this too, of the parent and of
     * every model partition and leaf (see {@code ChunksIsolationCheckPartitionsIntegrationTest}), so each of these
     * now refuses a start; before it, the check looked only at policies that widen and none of them did. The
     * earlier "boot still serves" assertions were the parent-only form of the same question and are superseded.
     */
    @Test
    void theStatusFieldIsFalseAndBootRefuses_whenRlsIsNotWiredOnChunks() throws Exception {
        PostgreSQLContainer<?> c = migratedAsAdmin(su -> { });
        try (HikariDataSource svc = pool(c, SVC, PgContainerHelper.SVC_PASSWORD, "guard-svc-rls", 2);
             Connection su = c.createConnection("")) {
            assertThat(awaitAnswer(svc)).as("baseline: intact").isTrue();

            exec(su, "ALTER TABLE nexus.chunks NO FORCE ROW LEVEL SECURITY");
            assertThat(awaitAnswer(svc)).as("row security not forced").isFalse();
            assertThatThrownBy(() -> ChunksIsolationCheck.verifyAtStartup(svc))
                .isInstanceOf(ChunksIsolationCheck.IsolationException.class)
                .hasMessageContaining("nexus.chunks").hasMessageContaining("FORCE");
            exec(su, "ALTER TABLE nexus.chunks FORCE ROW LEVEL SECURITY");
            assertThat(awaitAnswer(svc)).as("restored").isTrue();
            ChunksIsolationCheck.verifyAtStartup(svc);

            exec(su, "ALTER TABLE nexus.chunks DISABLE ROW LEVEL SECURITY");
            assertThat(awaitAnswer(svc)).as("row security disabled").isFalse();
            assertThatThrownBy(() -> ChunksIsolationCheck.verifyAtStartup(svc))
                .isInstanceOf(ChunksIsolationCheck.IsolationException.class)
                .hasMessageContaining("nexus.chunks").hasMessageContaining("not enabled");
            exec(su, "ALTER TABLE nexus.chunks ENABLE ROW LEVEL SECURITY");
            assertThat(awaitAnswer(svc)).as("restored").isTrue();
            ChunksIsolationCheck.verifyAtStartup(svc);

            exec(su, "DROP POLICY tenant_isolation ON nexus.chunks");
            assertThat(awaitAnswer(svc)).as("tenant policy missing").isFalse();
            assertThatThrownBy(() -> ChunksIsolationCheck.verifyAtStartup(svc))
                .isInstanceOf(ChunksIsolationCheck.IsolationException.class)
                .hasMessageContaining("tenant_isolation");
        } finally {
            c.stop();
        }
    }

    /** A real {@code dev.nexus.service.Main}, connecting as the differently named inheriting role. */
    private void assertMainRefusesToStart(PostgreSQLContainer<?> c) throws Exception {
        var pb = new ProcessBuilder(
            Path.of(System.getProperty("java.home"), "bin", "java").toString(),
            "-cp", System.getProperty("java.class.path"), "dev.nexus.service.Main")
            .redirectErrorStream(true);
        var env = pb.environment();
        env.put("NX_DB_URL", c.getJdbcUrl());
        env.put("NX_DB_USER", EXTRA);
        env.put("NX_DB_PASS", PgContainerHelper.MEMBER_PASSWORD);
        env.put("NX_DB_ADMIN_URL", c.getJdbcUrl());
        env.put("NX_DB_ADMIN_USER", ADMIN);
        env.put("NX_DB_ADMIN_PASS", ADMIN_PASS);
        env.put("NX_SERVICE_PORT", "0");
        env.remove("NX_PGBOUNCER_ADMIN_URL");
        Process p = pb.start();
        try {
            // Drain on a reader thread: a full pipe would stall the child, and waitFor must be able to time out.
            var out = new java.util.concurrent.CopyOnWriteArrayList<String>();
            Thread reader = new Thread(() -> {
                try (var r = new java.io.BufferedReader(
                        new java.io.InputStreamReader(p.getInputStream(), StandardCharsets.UTF_8))) {
                    String line;
                    while ((line = r.readLine()) != null) out.add(line);
                } catch (java.io.IOException ignored) {
                    // torn down mid-read
                }
            }, "main-boot-reader");
            reader.setDaemon(true);
            reader.start();
            boolean exited = p.waitFor(180, TimeUnit.SECONDS);
            reader.join(5_000);
            String log = String.join("\n", out);
            assertThat(exited).as("Main exits by itself; output:%n%s", log).isTrue();
            assertThat(p.exitValue()).as("Main refuses to boot (exit 1); output:%n%s", log).isEqualTo(1);
            assertThat(log).contains("event=chunks_isolation_check_failed")
                .contains("chunks_gate_probe_owner_read").contains(EXTRA);
            assertThat(log).as("it did not get as far as serving").doesNotContain("event=service_ready");
        } finally {
            p.destroyForcibly();
        }
    }
}
