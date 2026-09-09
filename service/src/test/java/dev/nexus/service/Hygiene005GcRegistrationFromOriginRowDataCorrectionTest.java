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
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-204 engine, bead nexus-uxd2a -- proof that {@code hygiene-005-gc-
 * registration-from-origin-row.xml}'s {@code hygiene-005-3} changeset
 * corrects {@code nexus.catalog_collections} rows already registered by
 * hygiene-002-2's buggy {@code gc_quarantine_orphans} self-registration
 * (a quarantine-shaped sibling whose content_type still carries a literal
 * "quarantine-" prefix or the 'unknown' fallback), and promotes any
 * quarantine-shaped row the v0.1.109 ghost sweep demoted to 'dormant'
 * before its own remediation.
 *
 * <p>Single tenant, real NOBYPASSRLS migrating role, the same {@link
 * Hygiene004OwnerGrammarUnderscoreTest} idiom: migrate up to (not
 * including) hygiene-005-3, seed fixtures DIRECTLY via typed jOOQ DSL
 * (bypassing {@link PgContainerHelper#insertCollection}'s own correct
 * regex-based classification, since these fixtures deliberately reproduce
 * the SHIPPED DEFECT's post-registration shape -- a state
 * {@code insertCollection} itself would never produce), then run
 * hygiene-005-3 and assert the correction.
 */
class Hygiene005GcRegistrationFromOriginRowDataCorrectionTest {

    private static final String PRE_CHANGESET_ID = "hygiene-005-3";
    private static final String MASTER_CHANGELOG = "db/changelog/db.changelog-master.xml";
    private static final String TENANT = "hygiene005-correction-tenant";

    // ── Branch 1: quarantine-shaped (PREFIX), mislabelled content_type, a
    // registered origin sibling exists -- corrected from the origin's row.
    private static final String QUAR_WITH_ORIGIN = "quarantine-code__uxd2a-a__voyage-code-3__v1";
    private static final String ORIGIN_FOR_A = "code__uxd2a-a__voyage-code-3__v1";

    // ── Branch 2: quarantine-shaped (PREFIX), mislabelled content_type, NO
    // registered origin -- the literal "quarantine-" prefix is stripped;
    // lifecycle_state is left exactly as it is (seeded 'disputed' here to
    // prove it is never flipped to 'quarantine' by this branch).
    private static final String QUAR_NO_ORIGIN = "quarantine-docs__uxd2a-b__voyage-context-3__v1";

    // ── Branch 1 again, SUFFIX shape this time: content_type = 'unknown',
    // a registered origin sibling exists (name with the suffix stripped).
    private static final String SUFFIX_WITH_ORIGIN = "code__uxd2a-c__voyage-code-3__v1__quarantine";
    private static final String ORIGIN_FOR_C = "code__uxd2a-c__voyage-code-3__v1";

    // ── Coordinator addition: quarantine-shaped, content_type ALREADY
    // correct, but lifecycle_state = 'dormant' (the v0.1.109 first-request
    // ghost-sweep demotion) -- promoted back to 'quarantine', content_type
    // untouched. Mirrors the known live case,
    // quarantine-rdr__1-1__voyage-context-3__v1 on tenant nexus.
    private static final String DORMANT_QUAR = "quarantine-rdr__uxd2a-d__voyage-context-3__v1";

    // ── Control: NOT quarantine-shaped at all -- untouched by every branch
    // regardless of its own content_type/lifecycle_state values.
    private static final String NONQUAR_UNKNOWN = "code__uxd2a-e__voyage-code-3__v1";
    private static final String NONQUAR_DORMANT = "docs__uxd2a-f__voyage-context-3__v1";

    // ── Control: quarantine-shaped, but ALREADY correctly classified
    // (content_type never matches 'quarantine-%'/'unknown', lifecycle_state
    // is not 'dormant') -- byte-identical before and after.
    private static final String CONTROL_ALREADY_CORRECT = "quarantine-code__uxd2a-g__voyage-code-3__v1";

    @Test
    void correctsMislabelledQuarantineSiblingsAndPromotesDormantOnes() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated();
        try {
            final String role = "nexus_admin_hygiene005_test";
            final String pass = "nexus_admin_hygiene005_test_pass";
            Hygiene001NotNullMigrationRlsTest.bootstrapAdminRole(pg, role, pass);

            var cfg = new com.zaxxer.hikari.HikariConfig();
            cfg.setJdbcUrl(pg.getJdbcUrl());
            cfg.setUsername(role);
            cfg.setPassword(pass);
            cfg.setMaximumPoolSize(2);
            cfg.setPoolName("nexus-admin-hygiene005-test");

            try (var adminDs = new com.zaxxer.hikari.HikariDataSource(cfg)) {
                // Phase 1: migrate up to (NOT including) hygiene-005-3 -- every
                // constraint hygiene-002-1 adds (content_type/owner_id/
                // embedding_model non-empty CHECKs, the embedding_model FK,
                // lifecycle_state's enum CHECK + NOT NULL) is already live, and
                // hygiene-005-1/-2 (the function fix itself, no data effect on
                // pre-existing rows) have already run.
                migrateUpTo(adminDs, PRE_CHANGESET_ID);

                try (Connection adminConn = adminDs.getConnection()) {
                    PgContainerHelper.installTestObjects(adminConn);
                }

                // Phase 2: seed every fixture DIRECTLY -- these rows reproduce
                // the SHIPPED DEFECT's post-registration shape, which
                // PgContainerHelper.insertCollection's own correct parse would
                // never produce, so a raw jOOQ INSERT is deliberate here, not a
                // shortcut.
                try (Connection su = pg.createConnection("")) {
                    su.setAutoCommit(true);
                    seedFixtures(DSL.using(su, SQLDialect.POSTGRES));
                }

                // Phase 3: apply the rest of the changelog -- hygiene-005-3 and
                // everything after it.
                applyRemainingChangelog(adminDs);

                // Phase 4: assert every fixture's post-hygiene-005-3 outcome.
                try (Connection su = pg.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);

                    // Branch 1 (prefix, origin exists): every attribute copied
                    // from the origin, lifecycle_state = 'quarantine'.
                    Row a = readRow(ctx, QUAR_WITH_ORIGIN);
                    assertThat(a.contentType()).as("branch 1 (prefix) content_type").isEqualTo("code");
                    assertThat(a.ownerId()).as("branch 1 (prefix) owner_id").isEqualTo("uxd2a-a");
                    assertThat(a.embeddingModel()).as("branch 1 (prefix) embedding_model")
                        .isEqualTo("voyage-code-3");
                    assertThat(a.dimension()).as("branch 1 (prefix) dimension copied from origin")
                        .isEqualTo(1024);
                    assertThat(a.lifecycleState()).as("branch 1 (prefix) lifecycle_state")
                        .isEqualTo("quarantine");

                    // Branch 2 (prefix, no origin): only the literal
                    // "quarantine-" prefix is stripped; lifecycle_state is
                    // left exactly as seeded ('disputed').
                    Row b = readRow(ctx, QUAR_NO_ORIGIN);
                    assertThat(b.contentType())
                        .as("branch 2 (no origin) strips only the literal quarantine- prefix")
                        .isEqualTo("docs");
                    assertThat(b.lifecycleState())
                        .as("branch 2 (no origin) never touches lifecycle_state")
                        .isEqualTo("disputed");

                    // Branch 1 again, SUFFIX shape: same correction as the
                    // prefix shape.
                    Row c = readRow(ctx, SUFFIX_WITH_ORIGIN);
                    assertThat(c.contentType()).as("branch 1 (suffix) content_type").isEqualTo("code");
                    assertThat(c.ownerId()).as("branch 1 (suffix) owner_id").isEqualTo("uxd2a-c");
                    assertThat(c.dimension()).as("branch 1 (suffix) dimension copied from origin")
                        .isEqualTo(768);
                    assertThat(c.lifecycleState()).as("branch 1 (suffix) lifecycle_state")
                        .isEqualTo("quarantine");

                    // Coordinator addition: dormant quarantine-shaped row
                    // promoted back to 'quarantine'; content_type (already
                    // correct) is untouched.
                    Row d = readRow(ctx, DORMANT_QUAR);
                    assertThat(d.contentType()).as("dormant-promotion content_type untouched")
                        .isEqualTo("rdr");
                    assertThat(d.lifecycleState()).as("dormant quarantine-shaped row promoted")
                        .isEqualTo("quarantine");

                    // Controls: never touched.
                    Row e = readRow(ctx, NONQUAR_UNKNOWN);
                    assertThat(e.contentType())
                        .as("a non-quarantine-shaped name with content_type 'unknown' is untouched")
                        .isEqualTo("unknown");
                    assertThat(e.lifecycleState()).isEqualTo("disputed");

                    Row f = readRow(ctx, NONQUAR_DORMANT);
                    assertThat(f.lifecycleState())
                        .as("a non-quarantine-shaped dormant row is never promoted")
                        .isEqualTo("dormant");
                    assertThat(f.contentType()).isEqualTo("docs");

                    Row g = readRow(ctx, CONTROL_ALREADY_CORRECT);
                    assertThat(g.contentType())
                        .as("an already-correct quarantine-shaped row's content_type is untouched")
                        .isEqualTo("code");
                    assertThat(g.lifecycleState())
                        .as("an already-correct quarantine-shaped row's lifecycle_state is untouched")
                        .isEqualTo("quarantine");

                    // Non-vacuity: exactly the three rows this changeset's
                    // content_type-correction branches (1 and 2) touch flipped
                    // away from a mislabelled content_type, and exactly the one
                    // row its dormant-promotion branch touches is no longer
                    // 'dormant' -- no more, no fewer. Stands in for the three
                    // RAISE NOTICE counts this changeset also emits (not
                    // independently observable through the JDBC driver without
                    // extra listener wiring, same as Hygiene004's own idiom).
                    int stillMislabelledCount = ctx.selectCount().from(CATALOG_COLLECTIONS)
                        .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT))
                        .and(CATALOG_COLLECTIONS.CONTENT_TYPE.like("quarantine-%")
                            .or(CATALOG_COLLECTIONS.CONTENT_TYPE.eq("unknown")))
                        .and(CATALOG_COLLECTIONS.NAME.like("quarantine-%")
                            .or(CATALOG_COLLECTIONS.NAME.likeRegex("__quarantine$")))
                        .fetchOne(0, int.class);
                    assertThat(stillMislabelledCount)
                        .as("no quarantine-shaped row may still carry a mislabelled content_type "
                            + "after hygiene-005-3")
                        .isEqualTo(0);
                    int stillDormantQuarantineShaped = ctx.selectCount().from(CATALOG_COLLECTIONS)
                        .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT))
                        .and(CATALOG_COLLECTIONS.LIFECYCLE_STATE.eq("dormant"))
                        .and(CATALOG_COLLECTIONS.NAME.like("quarantine-%")
                            .or(CATALOG_COLLECTIONS.NAME.likeRegex("__quarantine$")))
                        .fetchOne(0, int.class);
                    assertThat(stillDormantQuarantineShaped)
                        .as("no quarantine-shaped row may still be 'dormant' after hygiene-005-3")
                        .isEqualTo(0);
                }

                // Phase 5: re-applying the FULL changelog against this SAME,
                // already-migrated database must execute ZERO changesets -- a
                // checksum mismatch on hygiene-005-3 would throw here.
                applyRemainingChangelog(adminDs);
                try (Connection su = pg.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    Row aAfterReapply = readRow(ctx, QUAR_WITH_ORIGIN);
                    assertThat(aAfterReapply.lifecycleState())
                        .as("re-applying the changelog must not re-run hygiene-005-3's DML")
                        .isEqualTo("quarantine");
                }
            }
        } finally {
            pg.stop();
        }
    }

    // ── Fixture seeding ──────────────────────────────────────────────────

    private static void seedFixtures(DSLContext ctx) {
        insertRow(ctx, ORIGIN_FOR_A, "code", "uxd2a-a", "voyage-code-3", "v1", 1024, "live");
        insertRow(ctx, QUAR_WITH_ORIGIN, "quarantine-code", "uxd2a-a", "voyage-code-3", "v1", null,
            "quarantine");

        insertRow(ctx, QUAR_NO_ORIGIN, "quarantine-docs", "uxd2a-b", "voyage-context-3", "v1", null,
            "disputed");

        insertRow(ctx, ORIGIN_FOR_C, "code", "uxd2a-c", "voyage-code-3", "v1", 768, "live");
        insertRow(ctx, SUFFIX_WITH_ORIGIN, "unknown", TENANT, "voyage-code-3", "v1", null, "disputed");

        insertRow(ctx, DORMANT_QUAR, "rdr", "uxd2a-d", "voyage-context-3", "v1", null, "dormant");

        insertRow(ctx, NONQUAR_UNKNOWN, "unknown", TENANT, "voyage-code-3", "v1", null, "disputed");
        insertRow(ctx, NONQUAR_DORMANT, "docs", "uxd2a-f", "voyage-context-3", "v1", null, "dormant");

        insertRow(ctx, CONTROL_ALREADY_CORRECT, "code", "uxd2a-g", "voyage-code-3", "v1", null,
            "quarantine");
    }

    private static void insertRow(DSLContext ctx, String name, String contentType, String ownerId,
                                   String embeddingModel, String modelVersion, Integer dimension,
                                   String lifecycleState) {
        ctx.insertInto(CATALOG_COLLECTIONS,
                CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME,
                CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
                CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.MODEL_VERSION,
                CATALOG_COLLECTIONS.DIMENSION, CATALOG_COLLECTIONS.LIFECYCLE_STATE)
            .values(TENANT, name, contentType, ownerId, embeddingModel, modelVersion, dimension,
                lifecycleState)
            .onConflictDoNothing()
            .execute();
    }

    // ── Assertions ───────────────────────────────────────────────────────

    private record Row(String contentType, String ownerId, String embeddingModel,
                        Integer dimension, String lifecycleState) {}

    private static Row readRow(DSLContext ctx, String name) {
        var r = ctx.select(CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
                CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.DIMENSION,
                CATALOG_COLLECTIONS.LIFECYCLE_STATE)
            .from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT))
            .and(CATALOG_COLLECTIONS.NAME.eq(name))
            .fetchOne();
        assertThat(r).as("fixture %s must exist for tenant %s", name, TENANT).isNotNull();
        return new Row(r.get(CATALOG_COLLECTIONS.CONTENT_TYPE), r.get(CATALOG_COLLECTIONS.OWNER_ID),
            r.get(CATALOG_COLLECTIONS.EMBEDDING_MODEL), r.get(CATALOG_COLLECTIONS.DIMENSION),
            r.get(CATALOG_COLLECTIONS.LIFECYCLE_STATE));
    }

    // ── Migration plumbing (Hygiene004OwnerGrammarUnderscoreTest's own idiom) ──

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
}
