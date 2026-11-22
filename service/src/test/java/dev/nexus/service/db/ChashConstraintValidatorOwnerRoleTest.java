// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.ChashConstraintValidator.Outcome;
import dev.nexus.service.db.ChashConstraintValidator.Step;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestMethodOrder;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.security.MessageDigest;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;

/**
 * nexus-f3yqd, engine 0.1.190: the VALIDATE job as the table OWNER that is not a superuser and not BYPASSRLS, which is what
 * production's {@code nexus_admin} is ({@link ChashConstraintValidator}, "Role and connection"). Every table the job touches
 * is FORCE row-level security with a policy keyed to the tenant stamp, which this session never sets, so a count taken as the
 * session reads no row. The {@code nexus.chunks} leaves carry an owner SELECT policy for the migrating role (here the role is
 * made a member of the one the policy names, as production's admin role is it); the manifest has none. The job must (1) read
 * every tenant's rows where the role can, (2) report the pre-check as unavailable and not guess where it cannot, letting the
 * VALIDATE refuse, and (3) never take a lock stronger than ACCESS SHARE or lift FORCE: a reader that holds ACCESS SHARE on
 * every table through the whole run must not block it, which an ACCESS EXCLUSIVE {@code NO FORCE ROW LEVEL SECURITY} would.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class ChashConstraintValidatorOwnerRoleTest {

    private static final String OWNER = "chash_val_owner";
    private static final String OWNER_PASS = "chashvalownerpw";
    private static final String TENANT_A = "t-chashown-a";
    private static final String TENANT_B = "t-chashown-b";
    private static final String COLLECTION = "knowledge__chashown__bge-base-en-v15-768__v1";
    private static final String MODEL = "bge-base-en-v15-768";
    private static final String LEGACY_HEX = "b".repeat(32);

    private static final String SVC_ROLE = "svc_chashown";
    private static final String SVC_PASS = "svc_chashown_pw";

    private PostgreSQLContainer<?> pg;
    private com.zaxxer.hikari.HikariDataSource svcDs;

    /** What a run told: the plan, every step, and nothing that asks it to stop. */
    private static final class Recorder implements ChashConstraintValidator.Listener {
        int total = -1;
        final List<Step> steps = new ArrayList<>();

        @Override
        public void planned(int total) {
            this.total = total;
        }

        @Override
        public void current(String table, Long bytes) {
        }

        @Override
        public void stepped(Step step) {
            steps.add(step);
        }

        @Override
        public String stopReason() {
            return null;
        }
    }

    private static byte[] sha256(String text) throws Exception {
        return MessageDigest.getInstance("SHA-256").digest(text.getBytes(java.nio.charset.StandardCharsets.UTF_8));
    }

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.seedServiceToken(ctx, "tok-a-chashown-0123456789abcdef", TENANT_A, "a");
            PgContainerHelper.seedServiceToken(ctx, "tok-b-chashown-0123456789abcdef", TENANT_B, "b");
            PgContainerHelper.ensureTenantPartitions(ctx, TENANT_A);
            PgContainerHelper.ensureTenantPartitions(ctx, TENANT_B);
            ctx.alterTable(CATALOG_DOCUMENT_CHUNKS).dropConstraintIfExists("fk_catalog_chunks_chunk").execute();
            ctx.alterTable(CHUNKS).dropConstraintIfExists("chunks_chash_octet_check").execute();
            ctx.alterTable(CATALOG_DOCUMENT_CHUNKS).dropConstraintIfExists("catalog_document_chunks_chash_octet_check").execute();
            for (String tenant : new String[]{TENANT_A, TENANT_B}) {
                PgContainerHelper.insertCollection(ctx, tenant, COLLECTION);
                insertChunk(ctx, tenant, sha256("clean " + tenant), "clean");
                insertManifest(ctx, tenant, "1.1", sha256("clean " + tenant));
            }
            // tenant A's chunk AND manifest rows each carry a 16-byte chash
            byte[] legacy = HexFormat.of().parseHex(LEGACY_HEX);
            insertChunk(ctx, TENANT_A, legacy, "poisoned");
            insertManifest(ctx, TENANT_A, "9.9", legacy);
            PgContainerHelper.readdChashOctetChecksNotValid(su);
            PgContainerHelper.createLoginRole(su, OWNER, OWNER_PASS, false);
            PgContainerHelper.handProductSchemaTo(su, OWNER);
            // chunks_gate_probe_owner_read names the role that migrated the schema; production's admin role is that role
            PgContainerHelper.grantRoleMembership(su, pg.getUsername(), OWNER, true, false);
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
    }

    @AfterAll
    void stop() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private static void insertChunk(DSLContext ctx, String tenant, byte[] chash, String text) {
        ctx.insertInto(CHUNKS)
            .set(CHUNKS.TENANT_ID, tenant).set(CHUNKS.COLLECTION, COLLECTION).set(CHUNKS.CHASH, chash)
            .set(CHUNKS.EMBEDDING_MODEL, MODEL).set(CHUNKS.CHUNK_TEXT, text)
            .set(CHUNKS.EMBEDDING_768, Vector.of(new float[768])).execute();
    }

    private static void insertManifest(DSLContext ctx, String tenant, String docId, byte[] chash) {
        ctx.insertInto(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.TENANT_ID, tenant).set(CATALOG_DOCUMENTS.TUMBLER, docId)
            .set(CATALOG_DOCUMENTS.TITLE, "doc " + docId).onConflictDoNothing().execute();
        ctx.insertInto(CATALOG_DOCUMENT_CHUNKS)
            .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, tenant).set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, docId)
            .set(CATALOG_DOCUMENT_CHUNKS.POSITION, 0).set(CATALOG_DOCUMENT_CHUNKS.CHASH, chash)
            .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, COLLECTION).set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, MODEL).execute();
    }

    /** The owner role's validator, with the service role's tenant scope to count a refused table's bad rows by tenant. */
    private ChashConstraintValidator validator() {
        if (svcDs == null) {
            var cfg = new com.zaxxer.hikari.HikariConfig();
            cfg.setJdbcUrl(pg.getJdbcUrl());
            cfg.setUsername(SVC_ROLE);
            cfg.setPassword(SVC_PASS);
            cfg.setMaximumPoolSize(3);
            cfg.setAutoCommit(true);
            svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        }
        return new ChashConstraintValidator(new AdminConnection(pg.getJdbcUrl(), OWNER, OWNER_PASS), new TenantScope(svcDs),
            () -> List.of(TENANT_A, TENANT_B));
    }

    /** relname to relforcerowsecurity, for the manifest and every relation of the chunks tree, read as superuser. */
    private java.util.Map<String, Boolean> forceFlags() throws Exception {
        try (Connection su = pg.createConnection("")) {
            var c = DSL.table(DSL.name("pg_catalog", "pg_class")).as("c");
            var n = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
            var out = new java.util.TreeMap<String, Boolean>();
            for (var r : DSL.using(su, SQLDialect.POSTGRES)
                    .select(DSL.field(DSL.name("c", "relname"), String.class), DSL.field(DSL.name("c", "relforcerowsecurity"), Boolean.class))
                    .from(c).join(n).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
                    .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
                    .and(DSL.field(DSL.name("c", "relname"), String.class).likeRegex("^(chunks.*|catalog_document_chunks)$"))
                    .and(DSL.field(DSL.name("c", "relkind"), String.class).in("r", "p")).fetch()) {
                out.put(r.value1(), r.value2());
            }
            return out;
        }
    }

    /** Open transactions on a superuser connection that each hold ACCESS SHARE on every table the job touches. */
    private Connection holdReaders() throws Exception {
        Connection c = pg.createConnection("");
        c.setAutoCommit(false);
        var ctx = DSL.using(c, SQLDialect.POSTGRES);
        ctx.selectCount().from(CHUNKS).fetch();   // the parent locks every partition under it
        ctx.selectCount().from(CATALOG_DOCUMENT_CHUNKS).fetch();
        return c;
    }

    @Test
    @Order(1)
    void theOwnerReadsEveryTenantsChunkRows_butSaysUnavailableForTheManifest_andAltersNothingOnADryRun() throws Exception {
        var before = forceFlags();
        assertThat(before.values()).as("the tables are FORCE row-level security").doesNotContain(false).isNotEmpty();

        Recorder dry = new Recorder();
        var result = validator().run(true, dry);
        System.out.println("owner dry run: " + dry.steps);

        assertThat(result.stopped()).isNull();
        assertThat(dry.total).isPositive();
        var refused = dry.steps.stream().filter(s -> s.outcome() == Outcome.REFUSED).toList();
        assertThat(refused).as("tenant A's leaf, read through the owner policy, with its one bad row").hasSize(1);
        assertThat(refused.get(0).reason()).isEqualTo("bad_rows");
        assertThat(refused.get(0).badRows()).isEqualTo(1L);
        assertThat(refused.get(0).exampleChashes()).containsExactly(LEGACY_HEX);
        assertThat(refused.get(0).precheck()).isEqualTo("bad_rows");
        assertThat(refused.get(0).table()).startsWith("nexus.chunks_");
        assertThat(refused.get(0).detail().unattributed()).as("a leaf's counts are any tenant's rows").isTrue();
        assertThat(refused.get(0).detail().chunks()).hasSize(1);
        assertThat(refused.get(0).detail().chunks().get(0).owners().owners()).extracting(o -> o.docId()).containsExactly("9.9");

        Step manifest = dry.steps.stream().filter(s -> s.table().equals("nexus.catalog_document_chunks")).findFirst().orElseThrow();
        assertThat(manifest.outcome()).as("a dry run does not guess that the manifest would validate").isEqualTo(Outcome.DEFERRED);
        assertThat(manifest.reason()).isEqualTo("precheck_unavailable");
        assertThat(manifest.precheck()).isEqualTo("unavailable");
        assertThat(dry.steps.stream().filter(s -> s.outcome() == Outcome.WOULD_VALIDATE)).as("the clean chunk leaves").isNotEmpty();
        assertThat(forceFlags()).as("FORCE ROW LEVEL SECURITY was never lifted").isEqualTo(before);
    }

    @Test
    @Order(2)
    void aBadManifestRow_isRefusedByTheValidateItself_countedPerTenantAfterwards_andTheConstraintStaysNotValid() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).update(CHUNKS).set(CHUNKS.CHASH, sha256("fixed"))
                .where(CHUNKS.CHUNK_TEXT.eq("poisoned")).execute();
        }
        Recorder run = new Recorder();
        var result = validator().run(false, run);
        System.out.println("owner real run, bad manifest: " + run.steps);

        assertThat(result.allValidated()).isFalse();
        Step manifest = run.steps.stream().filter(s -> s.table().equals("nexus.catalog_document_chunks")).findFirst().orElseThrow();
        assertThat(manifest.outcome()).isEqualTo(Outcome.REFUSED);
        assertThat(manifest.reason()).isEqualTo("bad_rows");
        assertThat(manifest.precheck()).as("the pre-check stays unavailable; the count is the refusal's own").isEqualTo("unavailable");
        assertThat(manifest.badRows()).as("counted per known tenant through its scoped session, after the VALIDATE failed")
            .isEqualTo(1L);
        assertThat(manifest.tenantRows()).containsExactly(java.util.Map.entry(TENANT_A, 1L));
        assertThat(manifest.exampleChashes()).containsExactly(LEGACY_HEX);
        assertThat(manifest.badRowsCapped()).isFalse();
        // the pre-check was unavailable, so the account is the per-tenant one through the scoped sessions, exact and attributed
        assertThat(manifest.detail()).as("the per-reason account by tenant, not an RLS-subset floor").isNotNull();
        assertThat(manifest.detail().reasonCounts()).containsOnlyKeys("manifest_without_chunk").containsEntry("manifest_without_chunk", 1L);
        assertThat(manifest.detail().countsCapped()).isFalse();
        assertThat(manifest.detail().tenants()).containsExactly(TENANT_A);
        assertThat(manifest.detail().unattributed()).isFalse();
        assertThat(manifest.detail().documents().get("manifest_without_chunk").docs()).extracting(d -> d.docId()).containsExactly("9.9");
        assertThat(run.steps.stream().filter(s -> s.table().startsWith("nexus.chunks")).map(Step::outcome))
            .as("the chunks tree validated all the same").containsOnly(Outcome.VALIDATED);
        try (Connection su = pg.createConnection("")) {
            var rows = ChashConformanceConstraints.query(DSL.using(su, SQLDialect.POSTGRES)).fetch();
            assertThat(rows.stream().filter(r -> r.value3().equals("catalog_document_chunks")).map(r -> r.value5()))
                .as("the manifest's constraint is still NOT VALID").containsExactly(false);
        }
    }

    @Test
    @Order(3)
    void afterTheManifestRowIsFixed_aRerunValidatesIt_andForceIsStillOn() throws Exception {
        var before = forceFlags();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENT_CHUNKS)
                .set(CATALOG_DOCUMENT_CHUNKS.CHASH, sha256("fixed manifest")).where(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("9.9")).execute();
        }
        Recorder run = new Recorder();
        var result = validator().run(false, run);

        assertThat(result.stopped()).isNull();
        assertThat(result.allValidated()).isTrue();
        assertThat(run.steps.stream().filter(s -> s.outcome() == Outcome.REFUSED || s.outcome() == Outcome.DEFERRED)).isEmpty();
        assertThat(run.steps.stream().filter(s -> s.outcome() == Outcome.VALIDATED)).containsExactly(
            run.steps.stream().filter(s -> s.table().equals("nexus.catalog_document_chunks")).findFirst().orElseThrow());
        assertThat(forceFlags()).isEqualTo(before);
    }

    /**
     * No ACCESS EXCLUSIVE, ever: a reader holding ACCESS SHARE on every table for the whole run does not stop a dry run or a
     * real run. {@code NO FORCE ROW LEVEL SECURITY} (the lift the first version of the manifest pre-check used) needs ACCESS
     * EXCLUSIVE and would wait behind that reader until its lock timeout.
     */
    @Test
    @Order(4)
    void aReaderHoldingAccessShareOnEveryTable_neverBlocksTheJob_soNoAccessExclusiveIsTaken() throws Exception {
        System.setProperty(ChashConstraintValidator.LOCK_TIMEOUT_PROPERTY, "200");
        System.setProperty(ChashConstraintValidator.LOCK_ATTEMPTS_PROPERTY, "1");
        // rows fresh: the manifest's constraint is valid after the previous test, so reset it NOT VALID for a real VALIDATE
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.alterTable(CHUNKS).dropConstraintIfExists("chunks_chash_octet_check").execute();
            ctx.alterTable(CATALOG_DOCUMENT_CHUNKS).dropConstraintIfExists("catalog_document_chunks_chash_octet_check").execute();
            PgContainerHelper.readdChashOctetChecksNotValid(su);
        }
        Connection reader = holdReaders();
        try {
            long start = System.nanoTime();
            Recorder dry = new Recorder();
            assertThat(validator().run(true, dry).stopped()).isNull();
            assertThat(dry.steps.stream().filter(s -> "lock_timeout".equals(s.reason())))
                .as("no step waited on the reader's lock").isEmpty();
            Recorder real = new Recorder();
            var result = validator().run(false, real);
            assertThat(result.stopped()).isNull();
            assertThat(real.steps.stream().filter(s -> s.outcome() == Outcome.DEFERRED))
                .as("a SHARE UPDATE EXCLUSIVE VALIDATE does not conflict with ACCESS SHARE").isEmpty();
            assertThat(result.allValidated()).isTrue();
            assertThat(java.time.Duration.ofNanos(System.nanoTime() - start)).as("and it was not waiting out a lock timeout")
                .isLessThan(java.time.Duration.ofSeconds(20));
        } finally {
            reader.rollback();
            reader.close();
            System.clearProperty(ChashConstraintValidator.LOCK_TIMEOUT_PROPERTY);
            System.clearProperty(ChashConstraintValidator.LOCK_ATTEMPTS_PROPERTY);
        }
    }

    /**
     * The per-tenant count of a refused manifest is ACCESS SHARE only: with a reader holding ACCESS SHARE on every table, a
     * real run over a re-seeded bad manifest row still refuses it with its count and defers nothing. Kills: a count that
     * lifts FORCE or locks the table stronger (it would wait behind the reader and time out).
     */
    @Test
    @Order(5)
    void theCountByTenant_takesNoLockStrongerThanAccessShare() throws Exception {
        System.setProperty(ChashConstraintValidator.LOCK_TIMEOUT_PROPERTY, "200");
        System.setProperty(ChashConstraintValidator.LOCK_ATTEMPTS_PROPERTY, "1");
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.alterTable(CHUNKS).dropConstraintIfExists("chunks_chash_octet_check").execute();
            ctx.alterTable(CATALOG_DOCUMENT_CHUNKS).dropConstraintIfExists("catalog_document_chunks_chash_octet_check").execute();
            ctx.update(CATALOG_DOCUMENT_CHUNKS).set(CATALOG_DOCUMENT_CHUNKS.CHASH, HexFormat.of().parseHex(LEGACY_HEX))
                .where(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("9.9")).execute();
            PgContainerHelper.readdChashOctetChecksNotValid(su);
        }
        Connection reader = holdReaders();
        try {
            Recorder run = new Recorder();
            validator().run(false, run);
            Step manifest = run.steps.stream().filter(s -> s.table().equals("nexus.catalog_document_chunks")).findFirst().orElseThrow();
            assertThat(manifest.outcome()).isEqualTo(Outcome.REFUSED);
            assertThat(manifest.badRows()).isEqualTo(1L);
            assertThat(run.steps.stream().filter(s -> s.outcome() == Outcome.DEFERRED)).as("nothing waited on the reader").isEmpty();
        } finally {
            reader.rollback();
            reader.close();
            System.clearProperty(ChashConstraintValidator.LOCK_TIMEOUT_PROPERTY);
            System.clearProperty(ChashConstraintValidator.LOCK_ATTEMPTS_PROPERTY);
        }
    }
}
