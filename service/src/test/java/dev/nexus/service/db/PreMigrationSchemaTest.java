// SPDX-License-Identifier: AGPL-3.0-or-later
package dev.nexus.service.db;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.VectorSeeds;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.util.Arrays;
import java.util.Map;
import java.util.UUID;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd: {@link PreMigrationSchema} builds a real pre-{@code hygiene-001-5} schema, and the premise the
 * client guard rests on holds against it: the first full walk over that schema DELETEs a legacy-width chunk
 * and its manifest row and spares a 32-byte one.
 *
 * <p>One real NOSUPERUSER owner migrates, as in production; the walk is the real {@link SchemaMigrator#migrate}.
 * Each test takes its own empty database on the shared cluster (the role is created once and is exclusive to
 * this class).
 */
class PreMigrationSchemaTest {

    private static final String ROLE = "nexus_admin_premig_test";
    private static final String PASS = "nexus_admin_premig_test_pass";
    private static final String TENANT = "premig-tenant";
    private static final String COLLECTION = TENANT + "__coll__voyage-context-3__v1";
    private static final String DOC = TENANT + "-doc-1";

    private static final byte[] LEGACY_CHASH = "legacywidthchsh1".getBytes(StandardCharsets.US_ASCII);
    /** 38 bytes (anything over 32): the UTF-8 carry-over class rdr180 leaves for a longer non-hex id. Deleted by hygiene-001-5. */
    private static final byte[] LONG_LEGACY_CHASH = "longlegacy-chash-0123456789-0123456789".getBytes(StandardCharsets.US_ASCII);
    private static final byte[] GOOD_CHASH = filled(32, (byte) 0x5a);
    /** Exactly 32 bytes but not a digest: survives hygiene-001-5 by design (it keys on width only). */
    private static final byte[] NON_HEX_32_CHASH = "abcdefghijklmnopqrstuvwxyz012345".getBytes(StandardCharsets.US_ASCII);

    private final java.util.List<PostgreSQLContainer<?>> started = new java.util.ArrayList<>();

    private PostgreSQLContainer<?> freshDatabase() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase(ROLE);
        started.add(pg);
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapNonSuperuserOwner(su, ROLE, PASS);
        }
        return pg;
    }

    @AfterEach
    void stopAll() {
        started.forEach(PostgreSQLContainer::stop);
        started.clear();
    }

    /** Runs the tool CLI as the owner role. */
    private static Cli runTool(PostgreSQLContainer<?> pg, String... extraArgs) {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        ByteArrayOutputStream err = new ByteArrayOutputStream();
        String[] base = {"--jdbc-url", pg.getJdbcUrl(), "--user", ROLE, "--password", PASS};
        String[] args = new String[base.length + extraArgs.length];
        System.arraycopy(base, 0, args, 0, base.length);
        System.arraycopy(extraArgs, 0, args, base.length, extraArgs.length);
        int rc = PreMigrationSchema.run(args, Map.of(),
            new PrintStream(out, true, StandardCharsets.UTF_8), new PrintStream(err, true, StandardCharsets.UTF_8));
        return new Cli(rc, out.toString(StandardCharsets.UTF_8), err.toString(StandardCharsets.UTF_8));
    }

    private record Cli(int rc, String out, String err) {}

    private static boolean applied(Connection su, String changesetId) {
        return DSL.using(su, SQLDialect.POSTGRES).fetchExists(
            DSL.table(DSL.name("public", "databasechangelog")),
            DSL.field(DSL.name("id"), String.class).eq(changesetId));
    }

    @Test
    void stopsBeforeHygiene001_5_andReportsTheShapeItBuilt() throws Exception {
        PostgreSQLContainer<?> pg = freshDatabase();

        Cli cli = runTool(pg);

        assertThat(cli.rc()).as("exit code; stderr=%s", cli.err()).isEqualTo(PreMigrationSchema.EXIT_OK);
        try (Connection su = pg.createConnection("")) {
            assertThat(applied(su, "hygiene-001-5"))
                .as("the stop changeset must NOT be applied").isFalse();
            assertThat(applied(su, "hygiene-001-4"))
                .as("the changeset just before the stop must be applied").isTrue();
        }
        assertThat(cli.out()).contains("stop_before=hygiene-001-5\n");
        assertThat(cli.out()).contains("stop_changeset_applied=false\n");
        assertThat(cli.out()).contains("nexus.chunks.chash.data_type=bytea\n");
        assertThat(cli.out()).contains("nexus.chunks=plain\n");
        assertThat(cli.out()).contains("nexus.catalog_document_chunks=plain\n");
        assertThat(cli.out()).containsPattern("pending_ahead_of_stop=[1-9][0-9]*\n");
        // The report's "last applied" is read from the history, not echoed: it must be one the history holds.
        String last = cli.out().lines().filter(l -> l.startsWith("last_changeset_applied=")).findFirst()
            .orElseThrow().substring("last_changeset_applied=".length());
        try (Connection su = pg.createConnection("")) {
            assertThat(applied(su, last)).as("reported last changeset %s", last).isTrue();
        }
    }

    @Test
    void refusesAnUnknownChangeset_andAnAlreadyAppliedOne_leavingTheDatabaseAlone() throws Exception {
        PostgreSQLContainer<?> pg = freshDatabase();

        Cli unknown = runTool(pg, "--stop-before", "no-such-changeset-xyz");
        assertThat(unknown.rc()).isEqualTo(PreMigrationSchema.EXIT_REFUSED);
        assertThat(unknown.err()).contains("REFUSED").contains("not in");
        try (Connection su = pg.createConnection("")) {
            assertThat(DSL.using(su, SQLDialect.POSTGRES).fetchExists(
                DSL.table(DSL.name("pg_catalog", "pg_tables")),
                DSL.field(DSL.name("tablename"), String.class).eq("databasechangelog")))
                .as("a refused unknown id must not have started a walk").isFalse();
        }

        assertThat(runTool(pg).rc()).isEqualTo(PreMigrationSchema.EXIT_OK);
        Cli already = runTool(pg, "--stop-before", "hygiene-001-4");
        assertThat(already.rc()).isEqualTo(PreMigrationSchema.EXIT_REFUSED);
        assertThat(already.err()).contains("REFUSED").contains("already applied");

        // Re-running the same stop is not a refusal. Only runAlways changesets are pending before it (they re-run
        // at every walk), so the frontier the report names does not move.
        Cli again = runTool(pg);
        assertThat(again.rc()).as(again.err()).isEqualTo(PreMigrationSchema.EXIT_OK);
        assertThat(again.out()).contains("last_changeset_applied=hygiene-001-4\n").contains("stop_changeset_applied=false\n");
    }

    @Test
    void rejectsBadArguments() throws Exception {
        PostgreSQLContainer<?> pg = freshDatabase();
        ByteArrayOutputStream err = new ByteArrayOutputStream();
        int rc = PreMigrationSchema.run(new String[]{"--user", ROLE}, Map.of(),
            new PrintStream(new ByteArrayOutputStream()), new PrintStream(err, true, StandardCharsets.UTF_8));
        assertThat(rc).isEqualTo(PreMigrationSchema.EXIT_USAGE);
        assertThat(err.toString(StandardCharsets.UTF_8)).contains("--jdbc-url");
        // And the environment fallback is honoured.
        int ok = PreMigrationSchema.run(new String[]{"--stop-before", "hygiene-001-1"},
            Map.of("PREMIG_JDBC_URL", pg.getJdbcUrl(), "PREMIG_USER", ROLE, "PREMIG_PASSWORD", PASS),
            new PrintStream(new ByteArrayOutputStream()), new PrintStream(new ByteArrayOutputStream()));
        assertThat(ok).isEqualTo(PreMigrationSchema.EXIT_OK);
    }

    @Test
    void refusesWhileAnotherSessionHoldsTheMigrationLock_andChangesNothing() throws Exception {
        PostgreSQLContainer<?> pg = freshDatabase();
        try (Connection holder = pg.createConnection("")) {
            Boolean got = DSL.using(holder, SQLDialect.POSTGRES)
                .select(DSL.function("pg_try_advisory_lock", SQLDataType.BOOLEAN,
                    DSL.val(SchemaMigrator.MIGRATION_ADVISORY_LOCK_KEY)))
                .fetchOne(0, Boolean.class);
            assertThat(got).as("the test's own hold on the lock").isTrue();

            Cli cli = runTool(pg);

            assertThat(cli.rc()).as(cli.err()).isEqualTo(PreMigrationSchema.EXIT_REFUSED);
            assertThat(cli.err()).contains("REFUSED").contains("advisory lock");
            assertThat(DSL.using(holder, SQLDialect.POSTGRES).fetchExists(
                DSL.table(DSL.name("pg_catalog", "pg_tables")),
                DSL.field(DSL.name("tablename"), String.class).eq("databasechangelog")))
                .as("a refused run must not have started a walk").isFalse();
        }
        // The hold is gone with its session, and the tool then runs (and releases its own lock on exit).
        assertThat(runTool(pg).rc()).isEqualTo(PreMigrationSchema.EXIT_OK);
        try (Connection other = pg.createConnection("")) {
            assertThat(DSL.using(other, SQLDialect.POSTGRES)
                .select(DSL.function("pg_try_advisory_lock", SQLDataType.BOOLEAN,
                    DSL.val(SchemaMigrator.MIGRATION_ADVISORY_LOCK_KEY)))
                .fetchOne(0, Boolean.class)).as("the tool released the lock").isTrue();
        }
    }

    @Test
    void refusesADatabaseItDidNotMake_butRebuildsOnItsOwn() throws Exception {
        PostgreSQLContainer<?> pg = freshDatabase();
        assertThat(runTool(pg).rc()).isEqualTo(PreMigrationSchema.EXIT_OK);
        // Same database, marker removed: indistinguishable from a real store that has history.
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).dropTable(DSL.name("public", PreMigrationSchema.SCRATCH_MARKER)).execute();
        }
        Cli cli = runTool(pg);
        assertThat(cli.rc()).as(cli.err()).isEqualTo(PreMigrationSchema.EXIT_REFUSED);
        assertThat(cli.err()).contains("not made by this tool").contains("scratch");
    }

    @Test
    void refusesAnExistingChunksTableThatHasNoLiquibaseHistory_andChangesNothing() throws Exception {
        PostgreSQLContainer<?> pg = freshDatabase();
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.createSchemaIfNotExists(DSL.name("nexus")).execute();
            ctx.createTable(DSL.name("nexus", "chunks")).column("x", SQLDataType.INTEGER).execute();
        }

        Cli cli = runTool(pg);

        assertThat(cli.rc()).as(cli.err()).isEqualTo(PreMigrationSchema.EXIT_REFUSED);
        assertThat(cli.err()).contains("nexus.chunks exists with no Liquibase history");
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(ctx.fetchExists(DSL.table(DSL.name("pg_catalog", "pg_tables")),
                DSL.field(DSL.name("tablename"), String.class).in("databasechangelog", PreMigrationSchema.SCRATCH_MARKER)))
                .as("nothing applied, no marker left").isFalse();
        }
    }

    @Test
    void releasesTheAdvisoryLockOnTheToolsOwnSession_onEveryRefusalPath() throws Exception {
        PostgreSQLContainer<?> pg = freshDatabase();
        // Control that the probe can see a held lock at all: another session holds it.
        try (Connection holder = pg.createConnection("")) {
            DSL.using(holder, SQLDialect.POSTGRES).select(DSL.function("pg_advisory_lock", SQLDataType.OTHER,
                DSL.val(SchemaMigrator.MIGRATION_ADVISORY_LOCK_KEY))).fetch();
            assertThat(SchemaMigrator.migrationLockHolderPid(DSL.using(holder, SQLDialect.POSTGRES)))
                .as("probe control").isNotEqualTo(-1);
        }
        assertThat(runTool(pg).rc()).isEqualTo(PreMigrationSchema.EXIT_OK);

        // (a) refusal after Liquibase took the connection ("already applied"), (b) before it (not a scratch
        // database: marker removed). Both must have released the lock while the connection was still open.
        java.util.List<Integer> holderAfterRelease = new java.util.ArrayList<>();
        java.util.function.Consumer<Connection> probe =
            c -> holderAfterRelease.add(SchemaMigrator.migrationLockHolderPid(DSL.using(c, SQLDialect.POSTGRES)));

        try (Connection c = java.sql.DriverManager.getConnection(pg.getJdbcUrl(), ROLE, PASS)) {
            org.assertj.core.api.Assertions.assertThatThrownBy(
                    () -> PreMigrationSchema.apply(c, "hygiene-001-4", probe))
                .isInstanceOf(PreMigrationSchema.Refusal.class).hasMessageContaining("already applied");
        }
        assertThat(holderAfterRelease).as("released inside the Liquibase block: probe ran, lock gone")
            .isNotEmpty().containsOnly(-1);

        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).dropTable(DSL.name("public", PreMigrationSchema.SCRATCH_MARKER)).execute();
        }
        holderAfterRelease.clear();
        try (Connection c = java.sql.DriverManager.getConnection(pg.getJdbcUrl(), ROLE, PASS)) {
            org.assertj.core.api.Assertions.assertThatThrownBy(() -> PreMigrationSchema.apply(c, "hygiene-001-5", probe))
                .isInstanceOf(PreMigrationSchema.Refusal.class).hasMessageContaining("not made by this tool");
        }
        assertThat(holderAfterRelease).as("released on the pre-Liquibase refusal path: probe ran, lock gone")
            .isNotEmpty().containsOnly(-1);
    }

    @Test
    void refusesWhenARoleNamedSchemaHoldsTheHistoryAndPublicDoesNot() throws Exception {
        PostgreSQLContainer<?> pg = freshDatabase();
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // Owned by the migrating role: a history it cannot read is skipped by the walk's check, not refused.
            PgContainerHelper.createSchemaAuthorization(su, "stray_history", ROLE);
            var stray = DSL.table(DSL.name("stray_history", "databasechangelog"));
            ctx.createTable(stray).column("id", SQLDataType.VARCHAR).execute();
            ctx.insertInto(stray).set(DSL.field(DSL.name("id"), String.class), "x").execute();
            PgContainerHelper.alterTableOwner(su, stray, ROLE);
        }
        Cli cli = runTool(pg);
        assertThat(cli.rc()).as(cli.err()).isEqualTo(PreMigrationSchema.EXIT_REFUSED);
        assertThat(cli.err()).contains("history is split");
    }

    @Test
    void neverEchoesAnArgumentValue() {
        ByteArrayOutputStream err = new ByteArrayOutputStream();
        PrintStream out = new PrintStream(new ByteArrayOutputStream());
        PrintStream errStream = new PrintStream(err, true, StandardCharsets.UTF_8);
        for (String[] args : new String[][]{
                {"--password=hunter2-secret", "x"},   // unknown flag with an inline value
                {"--password=hunter2-secret"},        // the same, last: reported as a missing value
                {"hunter2-secret", "x"},              // a stray value in key position
                {"hunter2-secret"}}) {
            err.reset();
            assertThat(PreMigrationSchema.run(args, Map.of(), out, errStream)).isEqualTo(PreMigrationSchema.EXIT_USAGE);
            assertThat(err.toString(StandardCharsets.UTF_8)).as(String.join(" ", args).replace("hunter2", "h")).doesNotContain("hunter2");
        }
        err.reset();
        PreMigrationSchema.run(new String[]{"--password=hunter2-secret", "x"}, Map.of(), out, errStream);
        assertThat(err.toString(StandardCharsets.UTF_8)).contains("unknown argument --password");
    }

    /** What the real walk is given: a tool-built schema with legacy rows seeded after it. */
    @Test
    void aToolBuiltSchemaIsAcceptedByTheRealWalk_whichDeletesLegacyRowsAndSparesThe32ByteOnes() throws Exception {
        PostgreSQLContainer<?> pg = freshDatabase();
        assertThat(runTool(pg).rc()).isEqualTo(PreMigrationSchema.EXIT_OK);
        seedAndCheckControls(pg);

        HikariConfig cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(ROLE);
        cfg.setPassword(PASS);
        cfg.setMaximumPoolSize(2);
        cfg.setPoolName("premig-" + UUID.randomUUID());
        try (HikariDataSource ds = new HikariDataSource(cfg)) {
            SchemaMigrator.migrate(ds);
        }

        try (Connection su = pg.createConnection("")) {
            assertThat(applied(su, "hygiene-001-5")).as("the walk must have applied the stop changeset").isTrue();
            assertLegacyGoneAndSurvivorsKept(su, true);
        }
    }

    /**
     * The delete is hygiene-001-5's, not a later changeset's: stopping before hygiene-001-6 (so before
     * hygiene-001-8, which also removes legacy manifest rows) already shows the legacy chunk, its manifest row,
     * the long legacy row and the topic assignment cascaded off the chunk gone, and the 32-byte rows kept.
     */
    @Test
    void hygiene001_5Alone_deletesTheLegacyRowsAndCascadesTheTopicAssignment() throws Exception {
        PostgreSQLContainer<?> pg = freshDatabase();
        assertThat(runTool(pg).rc()).isEqualTo(PreMigrationSchema.EXIT_OK);
        seedAndCheckControls(pg);

        Cli cli = runTool(pg, "--stop-before", "hygiene-001-6");
        assertThat(cli.rc()).as(cli.err()).isEqualTo(PreMigrationSchema.EXIT_OK);
        assertThat(cli.out()).contains("last_changeset_applied=hygiene-001-5\n");

        try (Connection su = pg.createConnection("")) {
            assertThat(applied(su, "hygiene-001-5")).isTrue();
            assertThat(applied(su, "hygiene-001-6")).as("the stop").isFalse();
            assertThat(applied(su, "hygiene-001-8")).as("the later manifest cleanup has not run").isFalse();
            assertLegacyGoneAndSurvivorsKept(su, false);
        }
    }

    /**
     * 16-byte and 38-byte legacy chunks (the 16-byte one with a topic assignment), a 32-byte digest chunk and a
     * 32-byte non-hex one, each with a manifest row. At this walk point chunks_chash_octet_check and
     * catalog_document_chunks_chash_octet_check are live NOT VALID constraints, which still reject a NEW
     * non-32-byte row, so they are dropped for the seed and re-added NOT VALID after: the aged-install shape
     * rdr180-001 documents (same idiom as Hygiene001NotNullMigrationRlsTest).
     */
    private static void seedAndCheckControls(PostgreSQLContainer<?> pg) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            seed(su);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // Positive controls, read back: every "gone" assertion afterwards is vacuous without these.
            for (byte[] chash : new byte[][]{LEGACY_CHASH, LONG_LEGACY_CHASH, GOOD_CHASH, NON_HEX_32_CHASH}) {
                assertThat(ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(TENANT), CHUNKS.CHASH.eq(chash))).isEqualTo(1);
                assertThat(ctx.fetchCount(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT),
                    CATALOG_DOCUMENT_CHUNKS.CHASH.eq(chash))).isEqualTo(1);
            }
            assertThat(ctx.fetchCount(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID.eq(TENANT),
                TOPIC_ASSIGNMENTS.DOC_ID.eq(LEGACY_CHASH))).as("topic assignment on the legacy chunk").isEqualTo(1);
            assertThat(ctx.select(TOPICS.DOC_COUNT).from(TOPICS).where(TOPICS.TENANT_ID.eq(TENANT))
                .fetchOne(TOPICS.DOC_COUNT)).as("positive control: doc_count is 1 before the walk").isEqualTo(1);
            assertThat(applied(su, "hygiene-001-5")).isFalse();
        }
    }

    /** {@code afterFullWalk}: also assert what only the rest of the walk guarantees (doc_count recount). */
    private static void assertLegacyGoneAndSurvivorsKept(Connection su, boolean afterFullWalk) {
        DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
        for (byte[] gone : new byte[][]{LEGACY_CHASH, LONG_LEGACY_CHASH}) {
            String which = gone.length + "-byte";
            assertThat(ctx.fetchExists(CHUNKS, CHUNKS.TENANT_ID.eq(TENANT), CHUNKS.CHASH.eq(gone)))
                .as("the %s chunk is deleted", which).isFalse();
            assertThat(ctx.fetchExists(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT),
                CATALOG_DOCUMENT_CHUNKS.CHASH.eq(gone))).as("its manifest row is deleted with it").isFalse();
        }
        assertThat(ctx.fetchExists(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID.eq(TENANT),
            TOPIC_ASSIGNMENTS.DOC_ID.eq(LEGACY_CHASH)))
            .as("the topic assignment cascades off the deleted chunk").isFalse();
        if (afterFullWalk) {
            // Not asserted at the 001-5 stop: the cascade there leaves topics.doc_count at 1 (the trigger does not
            // follow the owner's cascade under FORCE RLS); hygiene-007-1's recount repairs it later in the walk.
            assertThat(ctx.select(TOPICS.DOC_COUNT).from(TOPICS).where(TOPICS.TENANT_ID.eq(TENANT))
                .fetchOne(TOPICS.DOC_COUNT)).as("topics.doc_count is repaired by the full walk").isZero();
        }
        for (byte[] kept : new byte[][]{GOOD_CHASH, NON_HEX_32_CHASH}) {
            assertThat(ctx.fetchExists(CHUNKS, CHUNKS.TENANT_ID.eq(TENANT), CHUNKS.CHASH.eq(kept)))
                .as("a 32-byte chunk survives (hygiene-001-5 keys on width only)").isTrue();
            assertThat(ctx.fetchExists(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT),
                CATALOG_DOCUMENT_CHUNKS.CHASH.eq(kept))).as("its manifest row survives").isTrue();
        }
        assertThat(ctx.select(CHUNKS.METADATA).from(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(TENANT), CHUNKS.CHASH.eq(GOOD_CHASH)).fetchOne(CHUNKS.METADATA))
            .as("hygiene-001-5 backfills the survivor's NULL metadata").isNotNull();
    }

    private static void seed(Connection su) throws Exception {
        DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
        ctx.insertInto(CATALOG_COLLECTIONS)
            .set(CATALOG_COLLECTIONS.TENANT_ID, TENANT)
            .set(CATALOG_COLLECTIONS.NAME, COLLECTION)
            .execute();
        ctx.insertInto(CATALOG_DOCUMENTS)
            .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT)
            .set(CATALOG_DOCUMENTS.TUMBLER, DOC)
            .set(CATALOG_DOCUMENTS.TITLE, "Doc")
            .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, COLLECTION)
            .execute();
        ctx.alterTable(CHUNKS).dropConstraint("chunks_chash_octet_check").execute();
        ctx.alterTable(CATALOG_DOCUMENT_CHUNKS).dropConstraint("catalog_document_chunks_chash_octet_check").execute();
        int position = 0;
        for (byte[] chash : new byte[][]{LEGACY_CHASH, LONG_LEGACY_CHASH, GOOD_CHASH, NON_HEX_32_CHASH}) {
            // The embedding binds as a real[] (not the generated vector binding): the extension is still in
            // `public` at this walk point, and real[] -> vector is looked up by type oid. It is 768-wide because
            // vectors-030-1 later copies each row into the model partition the collection resolves to (bge-768
            // here) and that leaf's dimension CHECK rejects any other width: a survivor must be copyable.
            ctx.insertInto(CHUNKS)
                .set(CHUNKS.TENANT_ID, TENANT)
                .set(CHUNKS.COLLECTION, COLLECTION)
                .set(CHUNKS.CHASH, chash)
                .set(CHUNKS.CHUNK_TEXT, "premig chunk " + chash.length)
                .set(DSL.field(DSL.name("embedding_768"), SQLDataType.REAL.array()), VectorSeeds.legacyEmbedding(768, 0f))
                .execute();
            ctx.insertInto(CATALOG_DOCUMENT_CHUNKS)
                .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, TENANT)
                .set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, DOC)
                .set(CATALOG_DOCUMENT_CHUNKS.POSITION, position++)
                .set(CATALOG_DOCUMENT_CHUNKS.CHASH, chash)
                .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, COLLECTION)
                .execute();
        }
        // An assignment on the legacy chunk: topic_assignments_chunk_fk is ON DELETE CASCADE, so hygiene-001-5's
        // chunk delete must remove it cleanly (and the doc_count trigger must follow).
        long topicId = ctx.insertInto(TOPICS)
            .set(TOPICS.TENANT_ID, TENANT)
            .set(TOPICS.LABEL, "premig-topic")
            .set(TOPICS.COLLECTION, COLLECTION)
            .set(TOPICS.CREATED_AT, DSL.currentOffsetDateTime())
            .returningResult(TOPICS.ID).fetchOne(TOPICS.ID);
        ctx.insertInto(TOPIC_ASSIGNMENTS)
            .set(TOPIC_ASSIGNMENTS.TENANT_ID, TENANT)
            .set(TOPIC_ASSIGNMENTS.DOC_ID, LEGACY_CHASH)
            .set(TOPIC_ASSIGNMENTS.TOPIC_ID, topicId)
            .set(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, COLLECTION)
            .execute();
        PgContainerHelper.readdChashOctetChecksNotValid(su);
    }

    private static byte[] filled(int n, byte b) {
        byte[] out = new byte[n];
        Arrays.fill(out, b);
        return out;
    }
}
