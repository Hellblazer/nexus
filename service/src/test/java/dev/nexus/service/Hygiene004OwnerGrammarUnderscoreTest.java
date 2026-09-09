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
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.HexFormat;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-204 engine, bead nexus-ztafa -- proof that {@code hygiene-004-owner-
 * grammar-underscore.xml}'s {@code hygiene-004-1} changeset re-files exactly
 * the {@code nexus.catalog_collections} rows that {@code hygiene-002-1}
 * (the prior, already-applied changeset -- never edited here) misclassified
 * into its branch D ({@code content_type = 'unknown'}, disputed) solely
 * because their owner segment carries an underscore, which hygiene-002-1's
 * grammar rejects but the client's collection-name check and {@link
 * PgContainerHelper}'s own fixture grammar both admit.
 *
 * <p>Single tenant, real NOBYPASSRLS migrating role (same {@link
 * Hygiene001NotNullMigrationRlsTest#bootstrapAdminRole} / {@link
 * PgContainerHelper#startDedicated()} idiom {@link
 * Hygiene002CollectionAttributesWalkTest} already established for this
 * table's own RLS-toggle mechanism) -- this test's job is to prove the NEW
 * branch-selection logic hygiene-004-1 adds, not to re-prove the RLS-toggle
 * machinery hygiene-002-1's own test already covers for the identical
 * pattern reused verbatim here.
 */
class Hygiene004OwnerGrammarUnderscoreTest {

    private static final String PRE_CHANGESET_ID = "hygiene-002-1";
    private static final String TARGET_CHANGESET_ID = "hygiene-004-1";
    private static final String MASTER_CHANGELOG = "db/changelog/db.changelog-master.xml";
    private static final String TENANT = "hygiene004-underscore-tenant";

    // Fixture names -- every one carries an underscore in its owner segment
    // except the control (already-conformant, no underscore) and the
    // genuinely-unparseable fixture (no owner segment to speak of).
    private static final String BRANCH_A_AGREE = "code__agree_owner__voyage-code-3__v1";
    private static final String BRANCH_B_QUARANTINE = "quarantine-code__quar_owner__voyage-code-3__v1";
    private static final String BRANCH_C_WITH_CHUNKS = "docs__legacy_owner";
    private static final String CONTROL_CONFORMANT = "code__control-owner__voyage-code-3__v1";
    private static final String UNPARSEABLE = "onesegmentname";

    // RDR-204 fix round (review finding 2c): fixtures proving the TIGHTENED
    // owner grammar ([a-zA-Z0-9-]+(?:_[a-zA-Z0-9-]+)*, single underscores
    // only, "__" never) still admits a single underscore (i, iii) while a
    // name whose owner would need a DOUBLE underscore to spell is left
    // genuinely unparseable rather than silently misclassified (ii).
    private static final String SINGLE_UNDERSCORE_FOUR_SEGMENT = "code__my_repo__voyage-code-3__v1";
    private static final String DOUBLE_UNDERSCORE_STAYS_DISPUTED = "code__my__repo__voyage-code-3__v1";
    private static final String SINGLE_UNDERSCORE_TWO_SEGMENT = "code__my_repo";

    @Test
    void refilesOnlyTheUnderscoredOwnerRowsHygiene002MisclassifiedAsDisputed() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated();
        try {
            final String role = "nexus_admin_hygiene004_test";
            final String pass = "nexus_admin_hygiene004_test_pass";
            Hygiene001NotNullMigrationRlsTest.bootstrapAdminRole(pg, role, pass);

            var cfg = new com.zaxxer.hikari.HikariConfig();
            cfg.setJdbcUrl(pg.getJdbcUrl());
            cfg.setUsername(role);
            cfg.setPassword(pass);
            cfg.setMaximumPoolSize(2);
            cfg.setPoolName("nexus-admin-hygiene004-test");

            try (var adminDs = new com.zaxxer.hikari.HikariDataSource(cfg)) {
                // Phase 1: migrate up to (NOT including) hygiene-002-1 -- the
                // fixtures below predate hygiene-002-1's own walk entirely,
                // exactly like Hygiene002CollectionAttributesWalkTest's own
                // Phase 1.
                migrateUpTo(adminDs, PRE_CHANGESET_ID);

                try (Connection adminConn = adminDs.getConnection()) {
                    PgContainerHelper.installTestObjects(adminConn);
                }

                // Phase 2: seed every fixture via typed jOOQ DSL over the
                // superuser connection (RLS-bypassing by construction).
                try (Connection su = pg.createConnection("")) {
                    su.setAutoCommit(true);
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    seedFixtures(ctx);
                }

                // Phase 3: migrate up to (NOT including) hygiene-004-1 --
                // this runs hygiene-002-1 (and hygiene-003) as the real
                // migrating role, so every underscored-owner fixture is now
                // misfiled into branch D exactly as the bead describes.
                // Snapshot the control fixture's row here: hygiene-004-1
                // must leave it byte-identical.
                migrateUpTo(adminDs, TARGET_CHANGESET_ID);

                Row controlBefore;
                try (Connection su = pg.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    assertPreHygiene004MisclassifiedAsBranchD(ctx, BRANCH_A_AGREE);
                    assertPreHygiene004MisclassifiedAsBranchD(ctx, BRANCH_B_QUARANTINE);
                    assertPreHygiene004MisclassifiedAsBranchD(ctx, BRANCH_C_WITH_CHUNKS);
                    assertPreHygiene004MisclassifiedAsBranchD(ctx, SINGLE_UNDERSCORE_FOUR_SEGMENT);
                    assertPreHygiene004MisclassifiedAsBranchD(ctx, DOUBLE_UNDERSCORE_STAYS_DISPUTED);
                    assertPreHygiene004MisclassifiedAsBranchD(ctx, SINGLE_UNDERSCORE_TWO_SEGMENT);
                    controlBefore = readRow(ctx, CONTROL_CONFORMANT);
                    assertThat(controlBefore.contentType())
                        .as("the control fixture must already be correctly classified by "
                            + "hygiene-002-1 alone -- its owner carries no underscore")
                        .isEqualTo("code");
                }

                // Phase 4: apply the rest of the changelog -- hygiene-004-1
                // and everything after it.
                applyRemainingChangelog(adminDs);

                // Phase 5: assert every fixture's post-hygiene-004-1 outcome.
                try (Connection su = pg.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);

                    // (1) four-segment underscored-owner name, known model,
                    // agreeing dimension -> 'live' with the right attributes.
                    Row a = readRow(ctx, BRANCH_A_AGREE);
                    assertThat(a.contentType()).as("branch A content_type").isEqualTo("code");
                    assertThat(a.ownerId()).as("branch A owner_id").isEqualTo("agree_owner");
                    assertThat(a.embeddingModel()).as("branch A embedding_model").isEqualTo("voyage-code-3");
                    assertThat(a.dimension()).as("branch A dimension").isEqualTo(1024);
                    assertThat(a.lifecycleState()).as("branch A lifecycle_state").isEqualTo("live");

                    // (2) quarantine-prefixed underscored-owner variant ->
                    // 'quarantine' UNCONDITIONALLY, even with a disagreeing
                    // dimension (the stored chunk is 768; the name's own
                    // token, voyage-code-3, is a known 1024-dim model).
                    Row b = readRow(ctx, BRANCH_B_QUARANTINE);
                    assertThat(b.contentType()).as("branch B content_type").isEqualTo("code");
                    assertThat(b.ownerId()).as("branch B owner_id").isEqualTo("quar_owner");
                    assertThat(b.embeddingModel()).as("branch B embedding_model").isEqualTo("voyage-code-3");
                    assertThat(b.lifecycleState())
                        .as("branch B lifecycle_state must be 'quarantine' regardless of "
                            + "dimension agreement")
                        .isEqualTo("quarantine");

                    // (3) two-segment underscored-owner name, with chunks and
                    // no profile row at walk time -> 'disputed', fallback
                    // model, stored dimension recorded (hygiene-002-1's own
                    // branch C rule for a chunked grandfathered name).
                    Row c = readRow(ctx, BRANCH_C_WITH_CHUNKS);
                    assertThat(c.contentType()).as("branch C content_type").isEqualTo("docs");
                    assertThat(c.ownerId()).as("branch C owner_id").isEqualTo("legacy_owner");
                    assertThat(c.embeddingModel()).as("branch C embedding_model")
                        .isEqualTo("bge-base-en-v15-768");
                    assertThat(c.dimension()).as("branch C dimension").isEqualTo(1024);
                    assertThat(c.lifecycleState()).as("branch C lifecycle_state").isEqualTo("disputed");

                    // (4) a genuinely unparseable name (no "__" separator at
                    // all) stays exactly as hygiene-002-1 left it: 'unknown'
                    // / disputed / owner = tenant -- the walk does not fail,
                    // and does not touch a row it cannot classify.
                    Row d = readRow(ctx, UNPARSEABLE);
                    assertThat(d.contentType()).as("unparseable content_type stays 'unknown'")
                        .isEqualTo("unknown");
                    assertThat(d.ownerId()).as("unparseable owner_id stays the tenant")
                        .isEqualTo(TENANT);
                    assertThat(d.lifecycleState()).as("unparseable lifecycle_state stays 'disputed'")
                        .isEqualTo("disputed");

                    // (4a) RDR-204 fix round (review finding 2c): a single
                    // underscore in a four-segment owner is still admitted --
                    // the tightening did not over-correct into rejecting the
                    // very case nexus-ztafa's original round exists to fix.
                    Row e = readRow(ctx, SINGLE_UNDERSCORE_FOUR_SEGMENT);
                    assertThat(e.contentType()).as("single-underscore branch A content_type")
                        .isEqualTo("code");
                    assertThat(e.ownerId()).as("single-underscore branch A owner_id")
                        .isEqualTo("my_repo");
                    assertThat(e.embeddingModel()).as("single-underscore branch A embedding_model")
                        .isEqualTo("voyage-code-3");
                    assertThat(e.dimension()).as("single-underscore branch A dimension")
                        .isEqualTo(1024);
                    assertThat(e.lifecycleState()).as("single-underscore branch A lifecycle_state")
                        .isEqualTo("live");

                    // (4b) an owner that would need a DOUBLE underscore to
                    // spell (e.g. "my" and "repo" joined by "__") can never be
                    // expressed under the tightened grammar -- "__" stays
                    // reserved, unconditionally, for the segment separator.
                    // This name matches NEITHER m4 nor m2 and is left exactly
                    // where hygiene-002-1 (and hygiene-004-1, finding no
                    // candidate re-match) left it: 'unknown' / disputed /
                    // owner = tenant. Under the FIRST, unrestricted
                    // [a-zA-Z0-9_-]+ owner grammar this exact name would have
                    // matched BOTH m4 (owner "my__repo") and m2 (owner
                    // "my__repo__voyage-code-3__v1") -- precisely the
                    // ambiguity this fix round closes at the grammar itself,
                    // proving branch C's precedence guard is now unreachable.
                    Row f = readRow(ctx, DOUBLE_UNDERSCORE_STAYS_DISPUTED);
                    assertThat(f.contentType()).as("double-underscore-shaped name stays 'unknown'")
                        .isEqualTo("unknown");
                    assertThat(f.ownerId()).as("double-underscore-shaped name owner_id stays the tenant")
                        .isEqualTo(TENANT);
                    assertThat(f.lifecycleState()).as("double-underscore-shaped name lifecycle_state stays 'disputed'")
                        .isEqualTo("disputed");

                    // (4c) single underscore, two-segment (grandfathered) --
                    // branch C, still admitted. No chunks were seeded for
                    // this fixture, so branch C's stats_dim_count = 0 case
                    // applies directly: 'live', dimension NULL.
                    Row g = readRow(ctx, SINGLE_UNDERSCORE_TWO_SEGMENT);
                    assertThat(g.contentType()).as("single-underscore branch C content_type")
                        .isEqualTo("code");
                    assertThat(g.ownerId()).as("single-underscore branch C owner_id")
                        .isEqualTo("my_repo");
                    assertThat(g.dimension()).as("single-underscore branch C dimension, no chunks seeded")
                        .isNull();
                    assertThat(g.lifecycleState())
                        .as("single-underscore branch C lifecycle_state, no chunks -> live")
                        .isEqualTo("live");

                    // (5) the control fixture -- already correctly classified
                    // by hygiene-002-1 alone -- is byte-identical before and
                    // after hygiene-004-1 (excluded by the WHERE: its
                    // content_type is never 'unknown').
                    Row controlAfter = readRow(ctx, CONTROL_CONFORMANT);
                    assertThat(controlAfter)
                        .as("a row hygiene-002-1 already classified correctly must be "
                            + "byte-identical after hygiene-004-1 -- its WHERE excludes it")
                        .isEqualTo(controlBefore);

                    // (6) non-vacuity: exactly the five underscored-owner
                    // candidate rows that DO match the tightened grammar
                    // flipped away from content_type = 'unknown' -- no more,
                    // no fewer. The double-underscore-shaped fixture (4b) is
                    // deliberately EXCLUDED from this list: it matches
                    // neither m4 nor m2 under the tightened grammar and must
                    // stay 'unknown', same as the genuinely unparseable
                    // fixture. A post-walk count query stands in for the
                    // RAISE NOTICE count this changeset also emits (not
                    // independently observable through the JDBC driver
                    // without extra listener wiring).
                    int reclassifiedCount = ctx.selectCount().from(CATALOG_COLLECTIONS)
                        .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT))
                        .and(CATALOG_COLLECTIONS.NAME.in(BRANCH_A_AGREE, BRANCH_B_QUARANTINE, BRANCH_C_WITH_CHUNKS,
                            SINGLE_UNDERSCORE_FOUR_SEGMENT, SINGLE_UNDERSCORE_TWO_SEGMENT))
                        .and(CATALOG_COLLECTIONS.CONTENT_TYPE.ne("unknown"))
                        .fetchOne(0, int.class);
                    assertThat(reclassifiedCount)
                        .as("exactly the five underscored-owner candidates that match the "
                            + "tightened grammar must have been re-filed away from "
                            + "content_type = 'unknown'")
                        .isEqualTo(5);
                    int stillUnknownCount = ctx.selectCount().from(CATALOG_COLLECTIONS)
                        .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT))
                        .and(CATALOG_COLLECTIONS.CONTENT_TYPE.eq("unknown"))
                        .fetchOne(0, int.class);
                    assertThat(stillUnknownCount)
                        .as("only the genuinely unparseable fixture and the double-underscore"
                            + "-shaped fixture (matches neither m4 nor m2) may remain "
                            + "content_type = 'unknown' after hygiene-004-1")
                        .isEqualTo(2);
                }

                // Phase 6: re-applying the FULL changelog against this SAME,
                // already-migrated database must execute ZERO changesets --
                // a checksum mismatch on hygiene-004-1 would throw here.
                applyRemainingChangelog(adminDs);
                try (Connection su = pg.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    Row aAfterReapply = readRow(ctx, BRANCH_A_AGREE);
                    assertThat(aAfterReapply.lifecycleState())
                        .as("re-applying the changelog must not re-run hygiene-004-1's DML")
                        .isEqualTo("live");
                }
            }
        } finally {
            pg.stop();
        }
    }

    // ── Fixture seeding ──────────────────────────────────────────────────

    private static void seedFixtures(DSLContext ctx) {
        PgContainerHelper.insertCollection(ctx, TENANT, BRANCH_A_AGREE);
        Routines.insertChunkBareVector(ctx.configuration(), TENANT, BRANCH_A_AGREE,
            chashBytes(TENANT + "-agree"), 1024);

        PgContainerHelper.insertCollection(ctx, TENANT, BRANCH_B_QUARANTINE);
        // Deliberately DISAGREEING dimension (768) against the name's own
        // token, voyage-code-3 (a known 1024-dim model) -- proves branch B's
        // 'quarantine' outcome is unconditional, not dimension-gated.
        Routines.insertChunkBareVector(ctx.configuration(), TENANT, BRANCH_B_QUARANTINE,
            chashBytes(TENANT + "-quar"), 768);

        PgContainerHelper.insertCollection(ctx, TENANT, BRANCH_C_WITH_CHUNKS);
        Routines.insertChunkBareVector(ctx.configuration(), TENANT, BRANCH_C_WITH_CHUNKS,
            chashBytes(TENANT + "-legacy"), 1024);

        PgContainerHelper.insertCollection(ctx, TENANT, CONTROL_CONFORMANT);
        Routines.insertChunkBareVector(ctx.configuration(), TENANT, CONTROL_CONFORMANT,
            chashBytes(TENANT + "-control"), 1024);

        PgContainerHelper.insertCollection(ctx, TENANT, UNPARSEABLE);

        // (i) single underscore, four-segment -- still admitted under the
        // tightened grammar; a chunk with the agreeing dimension (1024, the
        // name's own voyage-code-3 token) proves branch A's 'live' outcome.
        PgContainerHelper.insertCollection(ctx, TENANT, SINGLE_UNDERSCORE_FOUR_SEGMENT);
        Routines.insertChunkBareVector(ctx.configuration(), TENANT, SINGLE_UNDERSCORE_FOUR_SEGMENT,
            chashBytes(TENANT + "-single-underscore-four-segment"), 1024);

        // (ii) an owner that would need a DOUBLE underscore to spell -- no
        // chunks needed; this name matches neither m4 nor m2 under the
        // tightened grammar and is left exactly as hygiene-002-1 wrote it.
        PgContainerHelper.insertCollection(ctx, TENANT, DOUBLE_UNDERSCORE_STAYS_DISPUTED);

        // (iii) single underscore, two-segment (grandfathered) -- no chunks,
        // so branch C's stats_dim_count = 0 case applies directly ('live').
        PgContainerHelper.insertCollection(ctx, TENANT, SINGLE_UNDERSCORE_TWO_SEGMENT);
    }

    // ── Assertions ───────────────────────────────────────────────────────

    /**
     * Proves hygiene-002-1 ALONE (without hygiene-004-1) genuinely
     * misclassifies an underscored-owner fixture into branch D -- the bug
     * this changeset exists to fix. Without this check, a bug in the
     * candidate-selection WHERE (e.g. one that happened to also match an
     * already-correctly-classified row) could go unnoticed.
     */
    private static void assertPreHygiene004MisclassifiedAsBranchD(DSLContext ctx, String name) {
        Row row = readRow(ctx, name);
        assertThat(row.contentType())
            .as("hygiene-002-1 alone must misfile the underscored-owner fixture %s into "
                + "branch D ('unknown') -- otherwise this test is not proving what it claims", name)
            .isEqualTo("unknown");
        assertThat(row.ownerId())
            .as("branch D's owner_id is always the tenant itself, for %s", name)
            .isEqualTo(TENANT);
        assertThat(row.lifecycleState())
            .as("branch D's lifecycle_state is always 'disputed', for %s", name)
            .isEqualTo("disputed");
    }

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

    // ── Migration plumbing (Hygiene002CollectionAttributesWalkTest's own idiom) ──

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
