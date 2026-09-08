// SPDX-License-Identifier: AGPL-3.0-or-later
package dev.nexus.service;

import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.changelog.ChangeSet;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import dev.nexus.service.jooq.test.Routines;
import org.jooq.DSLContext;
import org.jooq.Record3;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.EMBEDDING_MODELS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-204 Phase 1 items 1/5 (beads nexus-ft04v.4 / nexus-ft04v.5) -- proof that
 * {@code hygiene-002-collection-attributes-walk.xml}'s FORCE-RLS-toggled
 * backfill genuinely runs (rather than silently no-op'ing, the exact
 * nexus-1wjmq class {@link Hygiene001NotNullMigrationRlsTest} guards) when
 * Liquibase migrates as a real NOBYPASSRLS schema-owner role, across TWO
 * tenants, through every one of the walk's branches, and that the
 * constraints it adds in the SAME changeset (bead nexus-ft04v.5) genuinely
 * reject bad data afterward.
 *
 * <p>Uses a DEDICATED container ({@link PgContainerHelper#startDedicated()})
 * and the same changeset-count-limited {@code migrateUpTo} idiom {@link
 * Hygiene001NotNullMigrationRlsTest}/{@code Catalog036EmbeddingProfileSchemaLiquibaseTest}
 * already establish: migrate up to (NOT including) {@code hygiene-002-1} as
 * the real migrating role, seed every branch's fixture in TWO tenants via
 * typed jOOQ DSL over the superuser connection (RLS-bypassing by
 * construction, matching {@code Catalog036EmbeddingProfileSchemaLiquibaseTest}'s
 * own seeding idiom), THEN apply the rest of the changelog as the migrating
 * role and assert every branch's outcome, the global invariant (no blank
 * column, every {@code embedding_model} FK-valid), the two rejection cases,
 * and that a second full-changelog apply is a no-op.
 */
class Hygiene002CollectionAttributesWalkTest {

    private static final String TARGET_CHANGESET_ID = "hygiene-002-1";
    private static final String MASTER_CHANGELOG = "db/changelog/db.changelog-master.xml";
    private static final String TENANT_1 = "hygiene002-walk-tenant-1";
    private static final String TENANT_2 = "hygiene002-walk-tenant-2";

    /** One fixture's expected post-walk column values, independent of which tenant it runs under. */
    private record Expected(String contentType, String ownerId, String embeddingModel,
                             Integer dimension, String lifecycleState) {}

    /**
     * Every named branch from the bead's TESTS list, keyed by the fixture's
     * OWN name suffix (the caller prefixes/suffixes per tenant as needed).
     * {@code owner_id} for the no-separator branches is the TENANT itself
     * (branch D), so those two entries carry a placeholder overwritten per
     * tenant in {@link #assertBranchOutcomes}.
     */
    private static Map<String, Expected> expectedByFixture(String tenant) {
        return Map.ofEntries(
            // Branch A, agreement: name token matches the collection's one stored dimension.
            Map.entry("code__agree-owner__voyage-code-3__v1",
                new Expected("code", "agree-owner", "voyage-code-3", 1024, "live")),
            // Branch A, disagreement: voyage-context-3 is a KNOWN token (dim 1024) but the
            // fixture's chunk is stored at 768 -- kept (never guessed from the dimension).
            Map.entry("docs__disagree-owner__voyage-context-3__v1",
                new Expected("docs", "disagree-owner", "voyage-context-3", 768, "disputed")),
            // Branch A, unseeded token: voyage-3 is on the engine's MODEL_DIMS but deliberately
            // not seeded into embedding_models -- ALWAYS disputed, even with zero chunks.
            Map.entry("code__unseeded-owner__voyage-3__v1",
                new Expected("code", "unseeded-owner", "bge-base-en-v15-768", null, "disputed")),
            // Branch A, all-tombstoned: the one chunk's only manifest row points at a deleted
            // document, so live_chunks (and therefore collection_vector_stats) sees zero rows --
            // the walk treats this exactly like a genuinely chunkless collection: 'live'.
            Map.entry("rdr__tombstoned-owner__voyage-context-3__v1",
                new Expected("rdr", "tombstoned-owner", "voyage-context-3", null, "live")),
            // Branch B: quarantine- names are ALWAYS 'quarantine', independent of agreement.
            Map.entry("quarantine-code__quar-owner__voyage-code-3__v1",
                new Expected("code", "quar-owner", "voyage-code-3", 1024, "quarantine")),
            // Branch C, grandfathered 2-segment WITH chunks: no profile row exists at walk time,
            // so this always falls back and is 'disputed' (never guessed live).
            Map.entry("docs__legacy-with-chunks",
                new Expected("docs", "legacy-with-chunks", "bge-base-en-v15-768", 1024, "disputed")),
            // Branch C, grandfathered 2-segment WITHOUT chunks: nothing to dispute -> 'live'.
            Map.entry("docs__legacy-no-chunks",
                new Expected("docs", "legacy-no-chunks", "bge-base-en-v15-768", null, "live")),
            // Branch D, one segment (no "__" separator at all).
            Map.entry("onesegmentname",
                new Expected("unknown", tenant, "bge-base-en-v15-768", null, "disputed")),
            // Branch D, empty string -- the AspectRepository document_highlights registration shape.
            Map.entry("",
                new Expected("unknown", tenant, "bge-base-en-v15-768", null, "disputed"))
        );
    }

    @Test
    void walkPopulatesEveryBranchAcrossTwoTenants_constraintsReject_reapplyIsNoOp() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated();
        try {
            final String role = "nexus_admin_hygiene002_test";
            final String pass = "nexus_admin_hygiene002_test_pass";
            Hygiene001NotNullMigrationRlsTest.bootstrapAdminRole(pg, role, pass);

            var cfg = new com.zaxxer.hikari.HikariConfig();
            cfg.setJdbcUrl(pg.getJdbcUrl());
            cfg.setUsername(role);
            cfg.setPassword(pass);
            cfg.setMaximumPoolSize(2);
            cfg.setPoolName("nexus-admin-hygiene002-test");

            try (var adminDs = new com.zaxxer.hikari.HikariDataSource(cfg)) {
                // Phase 1: migrate up to (NOT including) hygiene-002-1, as the
                // real migrating role -- the fixtures below predate this
                // bead's own changeset entirely.
                migrateUpTo(adminDs, TARGET_CHANGESET_ID);

                // Install the nexus_test.* schema (insert_chunk_bare_vector
                // and friends) THROUGH the migrating role's own connection --
                // PgContainerHelper#installTestObjects's own ownership
                // contract: whichever role's Liquibase run creates
                // databasechangelog first owns it, and that role here is
                // the migrating role (adminDs), not the superuser -- calling
                // this via su would hit "permission denied for table
                // databasechangelog". The seeding below still runs via `su`;
                // that is fine regardless of which role owns the function,
                // since superuser bypasses privilege checks entirely.
                try (Connection adminConn = adminDs.getConnection()) {
                    PgContainerHelper.installTestObjects(adminConn);
                }

                // Phase 2: seed every branch's fixture in BOTH tenants, via
                // typed jOOQ DSL over the superuser connection (RLS-bypassing
                // by construction -- Catalog036EmbeddingProfileSchemaLiquibaseTest's
                // own seeding idiom).
                try (Connection su = pg.createConnection("")) {
                    su.setAutoCommit(true);
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    for (String tenant : List.of(TENANT_1, TENANT_2)) {
                        seedBranchFixtures(ctx, tenant);
                    }
                }

                // Phase 3: apply the rest of the changelog (hygiene-002-1
                // plus everything after) as the migrating role -- proves the
                // NO FORCE / FORCE toggle around the backfill is not a
                // silent no-op under real NOBYPASSRLS.
                applyRemainingChangelog(adminDs);

                // Phase 4: assert every branch's outcome, the global
                // invariant, and the two rejection cases, in BOTH tenants.
                try (Connection su = pg.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    for (String tenant : List.of(TENANT_1, TENANT_2)) {
                        assertBranchOutcomes(ctx, tenant);
                    }
                    assertNoBlankColumnsAndEveryModelKnown(ctx);
                    assertConstraintsReject(ctx);
                }

                // Phase 5: re-applying the FULL changelog against this SAME,
                // already-migrated database must execute ZERO changesets --
                // a checksum mismatch on hygiene-002-1 would throw here, and
                // a real re-execution would double the embedding_models seed.
                applyRemainingChangelog(adminDs);
                try (Connection su = pg.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    assertThat(ctx.selectCount().from(EMBEDDING_MODELS).fetchOne(0, int.class))
                        .as("re-applying the changelog must not re-seed embedding_models")
                        .isEqualTo(4);
                    for (String tenant : List.of(TENANT_1, TENANT_2)) {
                        assertBranchOutcomes(ctx, tenant);
                    }
                }
            }
        } finally {
            pg.stop();
        }
    }

    // ── Fixture seeding ──────────────────────────────────────────────────

    private static void seedBranchFixtures(DSLContext ctx, String tenant) {
        PgContainerHelper.insertCollection(ctx, tenant, "code__agree-owner__voyage-code-3__v1");
        Routines.insertChunkBareVector(ctx.configuration(), tenant, "code__agree-owner__voyage-code-3__v1",
            chashBytes(tenant + "-agree"), 1024);

        PgContainerHelper.insertCollection(ctx, tenant, "docs__disagree-owner__voyage-context-3__v1");
        Routines.insertChunkBareVector(ctx.configuration(), tenant, "docs__disagree-owner__voyage-context-3__v1",
            chashBytes(tenant + "-disagree"), 768);

        PgContainerHelper.insertCollection(ctx, tenant, "code__unseeded-owner__voyage-3__v1");
        // deliberately no chunk -- proves the unseeded-token rule fires with zero chunks too.

        PgContainerHelper.insertCollection(ctx, tenant,
            "rdr__tombstoned-owner__voyage-context-3__v1");
        String tombstonedChash = tenant + "-tombstoned";
        Routines.insertChunkBareVector(ctx.configuration(), tenant,
            "rdr__tombstoned-owner__voyage-context-3__v1", chashBytes(tombstonedChash), 1024);
        String tombstonedDoc = tenant + "-tombstoned-doc";
        ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.DELETED_AT)
            .values(tenant, tombstonedDoc, "Tombstoned Doc", "rdr__tombstoned-owner__voyage-context-3__v1",
                OffsetDateTime.now())
            .execute();
        ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID,
                CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION,
                CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.COLLECTION)
            .values(tenant, tombstonedDoc, 0, chashBytes(tombstonedChash),
                "rdr__tombstoned-owner__voyage-context-3__v1")
            .execute();

        PgContainerHelper.insertCollection(ctx, tenant,
            "quarantine-code__quar-owner__voyage-code-3__v1");
        Routines.insertChunkBareVector(ctx.configuration(), tenant,
            "quarantine-code__quar-owner__voyage-code-3__v1", chashBytes(tenant + "-quar"), 1024);

        PgContainerHelper.insertCollection(ctx, tenant, "docs__legacy-with-chunks");
        Routines.insertChunkBareVector(ctx.configuration(), tenant, "docs__legacy-with-chunks",
            chashBytes(tenant + "-legacy-with-chunks"), 1024);

        PgContainerHelper.insertCollection(ctx, tenant, "docs__legacy-no-chunks");
        // deliberately no chunk.

        PgContainerHelper.insertCollection(ctx, tenant, "onesegmentname");
        PgContainerHelper.insertCollection(ctx, tenant, "");
    }

    // ── Assertions ───────────────────────────────────────────────────────

    private static void assertBranchOutcomes(DSLContext ctx, String tenant) {
        for (var entry : expectedByFixture(tenant).entrySet()) {
            String name = entry.getKey();
            Expected want = entry.getValue();
            var row = ctx.select(CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
                    CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.DIMENSION,
                    CATALOG_COLLECTIONS.LIFECYCLE_STATE)
                .from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant))
                .and(CATALOG_COLLECTIONS.NAME.eq(name))
                .fetchOne();
            assertThat(row)
                .as("fixture %s must exist for tenant %s after the walk", name, tenant)
                .isNotNull();
            assertThat(row.get(CATALOG_COLLECTIONS.CONTENT_TYPE))
                .as("content_type for %s / %s", tenant, name).isEqualTo(want.contentType());
            assertThat(row.get(CATALOG_COLLECTIONS.OWNER_ID))
                .as("owner_id for %s / %s", tenant, name).isEqualTo(want.ownerId());
            assertThat(row.get(CATALOG_COLLECTIONS.EMBEDDING_MODEL))
                .as("embedding_model for %s / %s", tenant, name).isEqualTo(want.embeddingModel());
            assertThat(row.get(CATALOG_COLLECTIONS.DIMENSION))
                .as("dimension for %s / %s", tenant, name).isEqualTo(want.dimension());
            assertThat(row.get(CATALOG_COLLECTIONS.LIFECYCLE_STATE))
                .as("lifecycle_state for %s / %s", tenant, name).isEqualTo(want.lifecycleState());
        }
    }

    private static void assertNoBlankColumnsAndEveryModelKnown(DSLContext ctx) {
        List<Record3<String, String, String>> blank = ctx
            .select(CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.CONTENT_TYPE)
            .from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.in(TENANT_1, TENANT_2))
            .and(CATALOG_COLLECTIONS.CONTENT_TYPE.eq("")
                .or(CATALOG_COLLECTIONS.OWNER_ID.eq(""))
                .or(CATALOG_COLLECTIONS.EMBEDDING_MODEL.eq("")))
            .fetch();
        assertThat(blank)
            .as("no surviving row may carry a blank content_type/owner_id/embedding_model "
                + "after the walk (the invariant the constraints below depend on)")
            .isEmpty();

        var unknownModel = ctx.select(CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME,
                CATALOG_COLLECTIONS.EMBEDDING_MODEL)
            .from(CATALOG_COLLECTIONS)
            .leftJoin(EMBEDDING_MODELS).on(EMBEDDING_MODELS.EMBEDDING_MODEL.eq(CATALOG_COLLECTIONS.EMBEDDING_MODEL))
            .where(CATALOG_COLLECTIONS.TENANT_ID.in(TENANT_1, TENANT_2))
            .and(EMBEDDING_MODELS.EMBEDDING_MODEL.isNull())
            .fetch();
        assertThat(unknownModel)
            .as("every surviving row's embedding_model must exist in embedding_models "
                + "after the walk -- an FK-invalid model here would wedge the constraint added "
                + "in the same changeset")
            .isEmpty();
    }

    private static void assertConstraintsReject(DSLContext ctx) {
        assertThatThrownBy(() -> ctx.insertInto(CATALOG_COLLECTIONS,
                CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.CONTENT_TYPE,
                CATALOG_COLLECTIONS.OWNER_ID, CATALOG_COLLECTIONS.EMBEDDING_MODEL,
                CATALOG_COLLECTIONS.LIFECYCLE_STATE)
            .values(TENANT_1, "reject-blank-content-type", "", "some-owner", "bge-base-en-v15-768", "live")
            .execute())
            .as("the non-empty CHECK on content_type must genuinely reject a fresh blank insert "
                + "post-migration")
            .isInstanceOf(DataAccessException.class);

        assertThatThrownBy(() -> ctx.insertInto(CATALOG_COLLECTIONS,
                CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.CONTENT_TYPE,
                CATALOG_COLLECTIONS.OWNER_ID, CATALOG_COLLECTIONS.EMBEDDING_MODEL,
                CATALOG_COLLECTIONS.LIFECYCLE_STATE)
            .values(TENANT_1, "reject-unknown-model", "code", "some-owner", "not-a-real-model", "live")
            .execute())
            .as("the embedding_model FK to embedding_models must genuinely reject a fresh "
                + "unrecognised model post-migration")
            .isInstanceOf(DataAccessException.class);
    }

    // ── Migration plumbing (Hygiene001NotNullMigrationRlsTest's own idiom) ──

    private static void applyRemainingChangelog(com.zaxxer.hikari.HikariDataSource adminDs) throws Exception {
        try (Connection conn = adminDs.getConnection()) {
            Database database = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(
                    MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                liquibase.update(new Contexts(), new LabelExpression());
            }
        }
    }

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

    // ── Fixture helpers (SoftDeleteTest's own idiom for a genuine chash) ────

    private static byte[] chashBytes(String seed) {
        return HexFormat.of().parseHex(dev.nexus.service.db.Chash.ofText(seed).toHex());
    }
}
