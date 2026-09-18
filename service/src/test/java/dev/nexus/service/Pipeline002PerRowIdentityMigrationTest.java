// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.changelog.ChangeSet;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Bead nexus-edjmu — proof that {@code pipeline-002-per-row-identity.xml}'s
 * backfill re-keys a POPULATED pre-002 buffer correctly, as the production
 * migration role would run it.
 *
 * <p>Uses a DEDICATED container ({@link PgContainerHelper#startDedicated()})
 * and the changeset-count-limited {@code update(int, ...)} idiom
 * ({@code AspectDocIdBackfillTest} / {@code Hygiene001NotNullMigrationRlsTest})
 * to migrate up to, but NOT including, {@code pipeline-002-1}, seed the
 * pipeline-001 shape through {@code DSL.table(DSL.name(...))} handles (the
 * generated jOOQ classes describe the POST-002 schema, where the WAL tables
 * have no content_hash column), then
 * apply the remainder and assert the post-state (typed jOOQ handles
 * throughout, RawSqlGateTest's reduce-only ratchet on the test tree):
 * <ul>
 *   <li>every surviving pdf_pages / pdf_chunks row carries the pipeline_id
 *   of the pdf_pipeline row with ITS tenant and content_hash: two tenants
 *   sharing a hash attach to their own parent, never each other's;</li>
 *   <li>WAL rows with no parent row are gone;</li>
 *   <li>content_hash is absent from both WAL tables, and pipeline_id,
 *   keyed_by ('content_hash' on every existing row) and the document UNIQUE
 *   are present on pdf_pipeline;</li>
 *   <li>the FK really cascades: deleting a parent removes its WAL rows.</li>
 * </ul>
 * The migration runs as a NOSUPERUSER/NOBYPASSRLS role, exactly like
 * {@code nexus_admin} in production, so a backfill that forgot the FORCE-RLS
 * toggle would match zero rows here and the SET NOT NULL that follows it
 * would fail the walk.
 */
class Pipeline002PerRowIdentityMigrationTest {

    private static final String TARGET_CHANGESET_ID = "pipeline-002-1";
    private static final String MASTER_CHANGELOG = "db/changelog/db.changelog-master.xml";

    private static final String T1 = "edjmu-tenant-1";
    private static final String T2 = "edjmu-tenant-2";
    private static final String SHARED = "h-shared-" + "0".repeat(24);
    private static final String ONLY_T1 = "h-only-t1-" + "0".repeat(24);
    private static final String ORPHAN = "h-orphan-" + "0".repeat(24);

    @Test
    void backfill_attachesEveryWalRowToItsOwnTenantsRun_andDropsOrphans() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated();
        try {
            final String role = "nexus_admin_pipeline002_test";
            final String pass = "nexus_admin_pipeline002_test_pass";
            Hygiene001NotNullMigrationRlsTest.bootstrapAdminRole(pg, role, pass);

            var cfg = new com.zaxxer.hikari.HikariConfig();
            cfg.setJdbcUrl(pg.getJdbcUrl());
            cfg.setUsername(role);
            cfg.setPassword(pass);
            cfg.setMaximumPoolSize(2);
            cfg.setPoolName("nexus-admin-pipeline002-test");

            try (var adminDs = new com.zaxxer.hikari.HikariDataSource(cfg)) {
                migrateUpTo(adminDs, TARGET_CHANGESET_ID);

                try (Connection su = pg.createConnection("")) {
                    su.setAutoCommit(true);
                    seedPre002Shape(su);
                }

                // The remainder of the changelog, pipeline-002-1..3 included.
                try (Connection conn = adminDs.getConnection()) {
                    Database database = DatabaseFactory.getInstance()
                        .findCorrectDatabaseImplementation(new JdbcConnection(conn));
                    try (Liquibase liquibase = new Liquibase(
                            MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                        liquibase.update(new Contexts(), new LabelExpression());
                    }
                }

                try (Connection su = pg.createConnection("")) {
                    su.setAutoCommit(true);
                    assertPost002Shape(su);
                }

                // ── The rollback over a POPULATED post-002 buffer: a second run
                // of the shared bytes (a document row at another path, with its
                // own WAL) exists alongside T1's legacy row, so restoring the
                // pipeline-001 keys is only possible if the rollback first
                // reduces each (tenant, hash) to one run. It must keep the
                // legacy row and drop the document row's row and WAL.
                try (Connection su = pg.createConnection("")) {
                    su.setAutoCommit(true);
                    seedSecondRunOfSharedBytes(su);
                }
                rollbackThroughPipeline002(pg, adminDs);
                try (Connection su = pg.createConnection("")) {
                    su.setAutoCommit(true);
                    assertRolledBackShape(su);
                }
            }
        } finally {
            pg.stop();
        }
    }

    // Typed handles for the PRE-002 shape (the generated jOOQ classes describe
    // the post-002 schema, where the WAL tables have no content_hash) and for
    // the post-002 reads below (RawSqlGateTest: no raw SQL text in tests).
    private static final Table<?> PIPELINE = DSL.table(DSL.name("nexus", "pdf_pipeline"));
    private static final Table<?> PAGES = DSL.table(DSL.name("nexus", "pdf_pages"));
    private static final Table<?> CHUNKS = DSL.table(DSL.name("nexus", "pdf_chunks"));
    private static final Field<String> TENANT_ID = DSL.field(DSL.name("tenant_id"), String.class);
    private static final Field<String> CONTENT_HASH = DSL.field(DSL.name("content_hash"), String.class);
    private static final Field<String> PDF_PATH = DSL.field(DSL.name("pdf_path"), String.class);
    private static final Field<String> COLLECTION = DSL.field(DSL.name("collection"), String.class);
    private static final Field<String> STATUS = DSL.field(DSL.name("status"), String.class);
    private static final Field<String> KEYED_BY = DSL.field(DSL.name("keyed_by"), String.class);
    private static final Field<Long> PIPELINE_ID = DSL.field(DSL.name("pipeline_id"), Long.class);
    private static final Field<Integer> PAGE_INDEX = DSL.field(DSL.name("page_index"), Integer.class);
    private static final Field<Integer> CHUNK_INDEX = DSL.field(DSL.name("chunk_index"), Integer.class);
    private static final Field<String> PAGE_TEXT = DSL.field(DSL.name("page_text"), String.class);
    private static final Field<String> CHUNK_TEXT = DSL.field(DSL.name("chunk_text"), String.class);
    private static final Field<String> CHUNK_ID = DSL.field(DSL.name("chunk_id"), String.class);
    private static final Field<OffsetDateTime> STARTED_AT = DSL.field(DSL.name("started_at"), OffsetDateTime.class);
    private static final Field<OffsetDateTime> UPDATED_AT = DSL.field(DSL.name("updated_at"), OffsetDateTime.class);
    private static final Field<OffsetDateTime> CREATED_AT = DSL.field(DSL.name("created_at"), OffsetDateTime.class);

    /** The pipeline-001 shape: one row per (tenant, hash); WAL rows keyed by
     *  hash; one parentless WAL family. Superuser, so FORCE RLS is bypassed. */
    private static void seedPre002Shape(Connection su) {
        DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        for (String[] row : new String[][] {
                {T1, SHARED, "/t1/shared.pdf", "knowledge__a"},
                {T2, SHARED, "/t2/shared.pdf", "knowledge__b"},
                {T1, ONLY_T1, "/t1/only.pdf", "knowledge__a"}}) {
            ctx.insertInto(PIPELINE, TENANT_ID, CONTENT_HASH, PDF_PATH, COLLECTION, STATUS, STARTED_AT, UPDATED_AT)
               .values(row[0], row[1], row[2], row[3], "completed", now, now)
               .execute();
        }
        // Two pages and two chunks per parent, plus a parentless family.
        for (String[] w : new String[][] {
                {T1, SHARED}, {T2, SHARED}, {T1, ONLY_T1}, {T1, ORPHAN}}) {
            for (int i = 0; i < 2; i++) {
                ctx.insertInto(PAGES, TENANT_ID, CONTENT_HASH, PAGE_INDEX, PAGE_TEXT, CREATED_AT)
                   .values(w[0], w[1], i, "p" + i, now)
                   .execute();
                ctx.insertInto(CHUNKS, TENANT_ID, CONTENT_HASH, CHUNK_INDEX, CHUNK_TEXT, CHUNK_ID, CREATED_AT)
                   .values(w[0], w[1], i, "c" + i, "cid-" + w[0] + "-" + i, now)
                   .execute();
            }
        }
    }

    private static void assertPost002Shape(Connection su) {
        DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
        // Columns: content_hash gone from the WAL tables; the new ones present.
        assertThat(columns(ctx, "pdf_pages")).doesNotContain("content_hash").contains("pipeline_id");
        assertThat(columns(ctx, "pdf_chunks")).doesNotContain("content_hash").contains("pipeline_id");
        assertThat(columns(ctx, "pdf_pipeline")).contains("pipeline_id", "keyed_by", "content_hash");

        // Parents: three rows, each with an id, all legacy-keyed.
        Map<String, Long> parentByTenantHash = new HashMap<>();
        for (var r : ctx.select(TENANT_ID, CONTENT_HASH, PIPELINE_ID, KEYED_BY).from(PIPELINE).fetch()) {
            assertThat(r.get(KEYED_BY)).isEqualTo("content_hash");
            parentByTenantHash.put(r.get(TENANT_ID) + "|" + r.get(CONTENT_HASH), r.get(PIPELINE_ID));
        }
        assertThat(parentByTenantHash).hasSize(3);
        assertThat(parentByTenantHash.get(T1 + "|" + SHARED))
            .isNotEqualTo(parentByTenantHash.get(T2 + "|" + SHARED));

        // WAL: every row points at the parent of ITS tenant + hash; the
        // orphan family is gone; nothing else was lost.
        var w = DSL.table(DSL.name("w"));
        var p = DSL.table(DSL.name("p"));
        for (Table<?> table : List.of(PAGES, CHUNKS)) {
            int rows = 0;
            var joined = ctx.select(DSL.field(DSL.name("w", "tenant_id"), String.class).as("w_tenant"),
                                    DSL.field(DSL.name("p", "content_hash"), String.class).as("p_hash"),
                                    DSL.field(DSL.name("p", "tenant_id"), String.class).as("p_tenant"))
                            .from(table.as("w"))
                            .join(PIPELINE.as("p"))
                            .on(DSL.field(DSL.name("p", "tenant_id"), String.class)
                                    .eq(DSL.field(DSL.name("w", "tenant_id"), String.class))
                                .and(DSL.field(DSL.name("p", "pipeline_id"), Long.class)
                                    .eq(DSL.field(DSL.name("w", "pipeline_id"), Long.class))))
                            .fetch();
            for (var r : joined) {
                rows++;
                assertThat(r.get("p_tenant", String.class)).isEqualTo(r.get("w_tenant", String.class));
                assertThat(r.get("p_hash", String.class)).isNotEqualTo(ORPHAN);
            }
            assertThat(rows).as(table + ": 3 parents x 2 rows survive, the orphan family does not").isEqualTo(6);
            assertThat(ctx.fetchCount(table)).isEqualTo(6);
        }
        // Cross-tenant check by content: T2's shared WAL rows belong to T2's parent.
        long t2Parent = parentByTenantHash.get(T2 + "|" + SHARED);
        assertThat(ctx.fetchCount(CHUNKS, PIPELINE_ID.eq(t2Parent).and(CHUNK_ID.like("cid-" + T2 + "-%"))))
            .isEqualTo(2);

        // Constraints: the document UNIQUE, the legacy partial index, the cascading FK.
        assertThat(ctx.fetchCount(DSL.table(DSL.name("pg_catalog", "pg_constraint")),
                DSL.field(DSL.name("conname"), String.class).eq("pdf_pipeline_document_uq")))
            .isEqualTo(1);
        assertThat(ctx.fetchCount(DSL.table(DSL.name("pg_catalog", "pg_indexes")),
                DSL.field(DSL.name("indexname"), String.class).eq("pdf_pipeline_legacy_uq")))
            .isEqualTo(1);
        long t1Only = parentByTenantHash.get(T1 + "|" + ONLY_T1);
        ctx.deleteFrom(PIPELINE).where(PIPELINE_ID.eq(t1Only)).execute();
        assertThat(ctx.fetchCount(PAGES, PIPELINE_ID.eq(t1Only))).isZero();
        assertThat(ctx.fetchCount(CHUNKS, PIPELINE_ID.eq(t1Only))).isZero();
        assertThat(ctx.fetchCount(PAGES)).isEqualTo(4);
    }

    /** A document-keyed run of T1's shared bytes at a second path, with one page. */
    private static void seedSecondRunOfSharedBytes(Connection su) {
        DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        Long id = ctx.insertInto(PIPELINE, TENANT_ID, CONTENT_HASH, PDF_PATH, COLLECTION, KEYED_BY,
                                 STATUS, STARTED_AT, UPDATED_AT)
                     .values(T1, SHARED, "/t1/second-copy.pdf", "knowledge__a", "document", "running", now, now)
                     .returning(PIPELINE_ID)
                     .fetchOne(PIPELINE_ID);
        ctx.insertInto(PAGES, TENANT_ID, PIPELINE_ID, PAGE_INDEX, PAGE_TEXT, CREATED_AT)
           .values(T1, id, 0, "second copy", now)
           .execute();
        assertThat(ctx.fetchCount(PIPELINE, TENANT_ID.eq(T1).and(CONTENT_HASH.eq(SHARED)))).isEqualTo(2);
    }

    /** Roll back every changeset executed at or after pipeline-002-1 (in
     *  DATABASECHANGELOG execution order, which is what Liquibase's counted
     *  rollback walks: the runAlways grants changesets that executed after
     *  it come off first, then pipeline-002-3, -2, -1). */
    private static void rollbackThroughPipeline002(PostgreSQLContainer<?> pg,
                                                   com.zaxxer.hikari.HikariDataSource adminDs) throws Exception {
        int toRollBack;
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            Table<?> log = DSL.table(DSL.name("public", "databasechangelog"));
            Field<Integer> order = DSL.field(DSL.name("orderexecuted"), Integer.class);
            Field<String> id = DSL.field(DSL.name("id"), String.class);
            toRollBack = ctx.fetchCount(log,
                order.ge(DSL.select(order).from(log).where(id.eq(TARGET_CHANGESET_ID))));
        }
        assertThat(toRollBack).as("pipeline-002-1 and everything after it must be in the changelog").isGreaterThanOrEqualTo(3);
        try (Connection conn = adminDs.getConnection()) {
            Database database = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(
                    MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                liquibase.rollback(toRollBack, new Contexts(), new LabelExpression());
            }
        }
    }

    private static void assertRolledBackShape(Connection su) {
        DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
        assertThat(columns(ctx, "pdf_pages")).contains("content_hash").doesNotContain("pipeline_id");
        assertThat(columns(ctx, "pdf_chunks")).contains("content_hash").doesNotContain("pipeline_id");
        assertThat(columns(ctx, "pdf_pipeline")).doesNotContain("pipeline_id", "keyed_by");
        // One run per (tenant, hash): T1's legacy row survived, its document
        // copy did not; T2's row is untouched (t1Only was deleted above).
        assertThat(ctx.select(TENANT_ID, PDF_PATH).from(PIPELINE).orderBy(TENANT_ID).fetch()
                      .map(r -> r.get(TENANT_ID) + ":" + r.get(PDF_PATH)))
            .containsExactly(T1 + ":/t1/shared.pdf", T2 + ":/t2/shared.pdf");
        assertThat(ctx.fetchCount(PAGES, CONTENT_HASH.eq(SHARED))).as("two survivors x two pages").isEqualTo(4);
        assertThat(ctx.fetchCount(PAGES, PAGE_TEXT.eq("second copy"))).as("the dropped run's WAL is gone").isZero();
        assertThat(ctx.fetchCount(CHUNKS, CONTENT_HASH.eq(SHARED))).isEqualTo(4);
        assertThat(ctx.fetchCount(DSL.table(DSL.name("pg_catalog", "pg_indexes")),
                DSL.field(DSL.name("indexname"), String.class).eq("pdf_pipeline_legacy_uq"))).isZero();
    }

    /** Column names of one nexus table, through jOOQ's information_schema reader. */
    private static List<String> columns(DSLContext ctx, String table) {
        return ctx.meta().getTables(DSL.name("nexus", table)).stream()
                  .flatMap(t -> java.util.Arrays.stream(t.fields()))
                  .map(Field::getName)
                  .toList();
    }

    /** Apply the master changelog UP TO, but NOT INCLUDING, {@code
     *  targetChangesetId} — index-based, robust against changesets added
     *  earlier in the chain (AspectDocIdBackfillTest's idiom). */
    private static void migrateUpTo(com.zaxxer.hikari.HikariDataSource adminDs,
                                    String targetChangesetId) throws Exception {
        try (Connection conn = adminDs.getConnection()) {
            Database database = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(
                    MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                List<ChangeSet> unrun = liquibase.listUnrunChangeSets(
                    new Contexts(), new LabelExpression());
                int idx = -1;
                for (int i = 0; i < unrun.size(); i++) {
                    if (targetChangesetId.equals(unrun.get(i).getId())) {
                        idx = i;
                        break;
                    }
                }
                assertThat(idx)
                    .as(targetChangesetId + " must be present in the master changelog")
                    .isGreaterThanOrEqualTo(0);
                liquibase.update(idx, new Contexts(), new LabelExpression());
            }
        }
    }
}
