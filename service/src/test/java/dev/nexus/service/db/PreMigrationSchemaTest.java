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
    private static final byte[] GOOD_CHASH = filled(32, (byte) 0x5a);

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
        assertThat(cli.out()).containsPattern("applied_this_run=[1-9][0-9]*\n");
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
    void fullMigration_deletesTheLegacyWidthChunkAndItsManifestRow_andSparesA32ByteOne() throws Exception {
        PostgreSQLContainer<?> pg = freshDatabase();
        assertThat(runTool(pg).rc()).isEqualTo(PreMigrationSchema.EXIT_OK);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            seedLegacyAndGood(su);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // Positive controls, read back: every "gone" assertion below is vacuous without these.
            assertThat(ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(TENANT), CHUNKS.CHASH.eq(LEGACY_CHASH))).isEqualTo(1);
            assertThat(ctx.fetchCount(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT),
                CATALOG_DOCUMENT_CHUNKS.CHASH.eq(LEGACY_CHASH))).isEqualTo(1);
            assertThat(ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(TENANT), CHUNKS.CHASH.eq(GOOD_CHASH))).isEqualTo(1);
            assertThat(applied(su, "hygiene-001-5")).isFalse();
        }

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
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(applied(su, "hygiene-001-5")).as("the walk must have applied the stop changeset").isTrue();
            assertThat(ctx.fetchExists(CHUNKS, CHUNKS.TENANT_ID.eq(TENANT), CHUNKS.CHASH.eq(LEGACY_CHASH)))
                .as("the 16-byte chunk is deleted by hygiene-001-5").isFalse();
            assertThat(ctx.fetchExists(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT),
                CATALOG_DOCUMENT_CHUNKS.CHASH.eq(LEGACY_CHASH)))
                .as("its manifest row is deleted with it").isFalse();
            assertThat(ctx.fetchExists(CHUNKS, CHUNKS.TENANT_ID.eq(TENANT), CHUNKS.CHASH.eq(GOOD_CHASH)))
                .as("the 32-byte chunk survives the whole walk").isTrue();
            assertThat(ctx.fetchExists(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT),
                CATALOG_DOCUMENT_CHUNKS.CHASH.eq(GOOD_CHASH)))
                .as("its manifest row survives").isTrue();
        }
    }

    /**
     * One 16-byte (un-rekeyed legacy) chunk and one 32-byte chunk, each with a manifest row, for one tenant.
     * At this walk point chunks_chash_octet_check and catalog_document_chunks_chash_octet_check are live NOT
     * VALID constraints, which still reject a NEW 16-byte row, so they are dropped for the seed and re-added NOT
     * VALID after, the exact aged-install shape rdr180-001 documents (same idiom as
     * Hygiene001NotNullMigrationRlsTest).
     */
    private static void seedLegacyAndGood(Connection su) throws Exception {
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
        for (byte[] chash : new byte[][]{LEGACY_CHASH, GOOD_CHASH}) {
            // The embedding binds as a real[] (not the generated vector binding): the extension is still in
            // `public` at this walk point, and real[] -> vector is looked up by type oid. It is 768-wide because
            // vectors-030-1 later copies each row into the model partition the collection resolves to (bge-768
            // here) and that leaf's dimension CHECK rejects any other width: the survivor must be copyable.
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
        PgContainerHelper.readdChashOctetChecksNotValid(su);
    }

    private static byte[] filled(int n, byte b) {
        byte[] out = new byte[n];
        Arrays.fill(out, b);
        return out;
    }
}
