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
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-223 Phase 3 step 2 (nexus-z0o2p.27): the {@code staging} landing schema
 * is dropped by {@value #DROP_CHANGESET} after the engine's {@code /v1/staging}
 * routes were retired (Sam's decision, 2026-09-30; RDR-223 F-7: no client ever
 * called them).
 *
 * <p>Two shapes, because a fresh walk cannot tell a drop that works from one
 * that is never reached:
 * <ol>
 *   <li><b>aged store:</b> walk the master changelog up to (not including) the
 *       drop changeset, land rows in two staging tables, create the
 *       {@code nexus_diag} role so the era-independent staging grant in
 *       {@code grants-nexus-diag-3} actually executes, then finish the walk.
 *       The schema and its rows are gone, and the walk did not fail: the
 *       runAlways grant changesets that name {@code staging} must tolerate its
 *       absence on this walk and on every later boot.</li>
 *   <li><b>second boot:</b> a further {@code update} is a clean no-op.</li>
 * </ol>
 *
 * <p>Hermetic: Testcontainers pgvector, requires Docker.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class StagingSchemaDropLiquibaseTest {

    static final String MASTER = "db/changelog/db.changelog-master.xml";
    static final String DROP_CHANGESET = "staging-6-drop-landing-schema";

    private static final List<String> STAGING_TABLES = List.of(
        "chunks", "document_chunks", "topic_assignments",
        "frecency", "relevance_log", "document_aspects", "aspect_extraction_queue");

    private static final Table<?> STAGING_FRECENCY = DSL.table(DSL.name("staging", "frecency"));
    private static final Table<?> STAGING_DOC_CHUNKS = DSL.table(DSL.name("staging", "document_chunks"));
    private static final Field<String> TENANT_ID = DSL.field(DSL.name("tenant_id"), String.class);
    private static final Field<String> CHUNK_ID = DSL.field(DSL.name("chunk_id"), String.class);
    private static final Field<String> DOC_ID = DSL.field(DSL.name("doc_id"), String.class);
    private static final Field<Integer> POSITION = DSL.field(DSL.name("position"), Integer.class);
    private static final Field<String> CHASH = DSL.field(DSL.name("chash"), String.class);

    PostgreSQLContainer<?> pg;

    @BeforeAll
    void startAll() throws Exception {
        // Dedicated cluster: nexus_diag is a cluster-global role, so creating it on the
        // shared cluster would change what every other test class's walk sees.
        pg = PgContainerHelper.startDedicated();
        migrateUpToDropChangeset();

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            // Aged store: staging exists, populated, and indexed by staging-2.
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(PgCatalogProbes.tablesInSchema(ctx, "staging"))
                .as("precondition: the landing tables exist before the drop")
                .containsExactlyInAnyOrderElementsOf(STAGING_TABLES);
            ctx.insertInto(STAGING_FRECENCY, TENANT_ID, CHUNK_ID).values("t-aged", "feedbeef").execute();
            ctx.insertInto(STAGING_DOC_CHUNKS, TENANT_ID, DOC_ID, POSITION, CHASH)
                .values("t-aged", "1.1.1", 0, "a".repeat(64)).execute();

            // grants-nexus-diag-3's staging branch only runs when the role exists.
            // Created here, after the pre-drop walk, so the FIRST walk that sees it
            // is the one where staging is already gone. bootstrapServiceRole is the
            // test tree's one role-creation path (jOOQ's open-source DSL has no
            // CREATE ROLE, and a raw statement here would need its own RawSqlGateTest
            // ceiling entry); it needs the nexus_test helpers installed first.
            PgContainerHelper.installTestObjects(su);
            PgContainerHelper.bootstrapServiceRole(su, "nexus_diag", "nexus_diag_drop_test_pw");
        }

        updateAll();
    }

    @AfterAll
    void stopAll() {
        if (pg != null) pg.stop();
    }

    // Each Liquibase run gets its own connection: Liquibase#close() closes the
    // connection it was built on.
    private void migrateUpToDropChangeset() throws Exception {
        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(su));
            try (Liquibase liquibase = new Liquibase(MASTER, new ClassLoaderResourceAccessor(), db)) {
                List<ChangeSet> unrun = liquibase.listUnrunChangeSets(new Contexts(), new LabelExpression());
                int idx = -1;
                for (int i = 0; i < unrun.size(); i++) {
                    if (DROP_CHANGESET.equals(unrun.get(i).getId())) {
                        idx = i;
                        break;
                    }
                }
                assertThat(idx).as(DROP_CHANGESET + " must be in the master changelog").isGreaterThanOrEqualTo(0);
                liquibase.update(idx, new Contexts(), new LabelExpression());
            }
        }
    }

    private void updateAll() throws Exception {
        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(su));
            try (Liquibase liquibase = new Liquibase(MASTER, new ClassLoaderResourceAccessor(), db)) {
                liquibase.update(new Contexts(), new LabelExpression());
            }
        }
    }

    @Test
    void schemaAndEveryLandingTableAreGone_rowsIncluded() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(PgCatalogProbes.tablesInSchema(ctx, "staging"))
                .as("no table may survive in schema staging")
                .isEmpty();
            for (String t : STAGING_TABLES) {
                assertThat(PgCatalogProbes.tableExists(ctx, "staging", t))
                    .as("staging.%s must be dropped", t).isFalse();
            }
            assertThat(ctx.fetchCount(DSL.table(DSL.name("pg_namespace")),
                    DSL.field(DSL.name("nspname"), String.class).eq("staging")))
                .as("the staging schema itself must be dropped, not just emptied")
                .isZero();
        }
    }

    @Test
    void walkWithNexusDiagPresent_doesNotFail_andLaterBootsStayClean() throws Exception {
        // startAll() already walked past the drop with nexus_diag present; a
        // further full update re-executes every runAlways grant changeset (the
        // second and every later production boot) against a store with no
        // staging schema.
        updateAll();
        try (Connection su = pg.createConnection("")) {
            assertThat(PgCatalogProbes.tablesInSchema(DSL.using(su, SQLDialect.POSTGRES), "staging")).isEmpty();
        }
    }
}
