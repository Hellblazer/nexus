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
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.Statement;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-223 Phase 3 step 2 (nexus-z0o2p.27): the {@code staging} landing schema
 * is dropped by {@value #DROP_CHANGESET} after the engine's {@code /v1/staging}
 * routes were retired (Sam's decision, 2026-09-30).
 *
 * <p>The changeset is tolerant on purpose (the nexus-lgdel.l1 no-wedge
 * directive): it drops the seven tables it names, then drops the schema only if
 * that leaves it empty, and otherwise says so in a NOTICE. Each test walks a
 * dedicated store up to (not including) the changeset, sets up one shape, and
 * finishes the walk:
 * <ol>
 *   <li>{@link #agedStore_withRows_nexusDiagAndItsStagingGrants_isDropped}: the
 *       aged production shape. {@code nexus_diag} exists BEFORE the pre-drop
 *       walk, two staging tables hold rows, and nexus_diag holds the grants and
 *       default ACL the OLD {@code grants-nexus-diag-3} gave it on staging. The
 *       schema, the rows, the grants and the default ACL are gone, the walk did
 *       not fail, and a second boot is clean.</li>
 *   <li>{@link #foreignObjectInTheSchema_isLeftAlone_andTheWalkDoesNotAbort}: an
 *       object the changeset did not name keeps the schema alive; the named
 *       tables still go.</li>
 *   <li>{@link #missingSchema_marksTheChangesetRan}: the precondition's
 *       MARK_RAN branch.</li>
 * </ol>
 *
 * <p>Hermetic: Testcontainers pgvector, requires Docker. Dedicated clusters:
 * {@code nexus_diag} is a cluster-global role.
 */
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

    private static final Table<?> DATABASECHANGELOG = DSL.table(DSL.name("public", "databasechangelog"));
    private static final Field<String> DBCL_ID = DSL.field(DSL.name("id"), String.class);
    private static final Field<String> DBCL_EXECTYPE = DSL.field(DSL.name("exectype"), String.class);

    // Each Liquibase run gets its own connection: Liquibase#close() closes the
    // connection it was built on.
    private static void migrateUpToDropChangeset(PostgreSQLContainer<?> pg) throws Exception {
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

    private static void updateAll(PostgreSQLContainer<?> pg) throws Exception {
        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(su));
            try (Liquibase liquibase = new Liquibase(MASTER, new ClassLoaderResourceAccessor(), db)) {
                liquibase.update(new Contexts(), new LabelExpression());
            }
        }
    }

    private static String dropExecType(Connection su) {
        return DSL.using(su, SQLDialect.POSTGRES)
            .select(DBCL_EXECTYPE).from(DATABASECHANGELOG)
            .where(DBCL_ID.eq(DROP_CHANGESET)).fetchOne(DBCL_EXECTYPE);
    }

    private static boolean schemaExists(DSLContext ctx) {
        return ctx.fetchCount(DSL.table(DSL.name("pg_namespace")),
            DSL.field(DSL.name("nspname"), String.class).eq("staging")) > 0;
    }

    /** Default ACLs scoped to schema staging (pg_default_acl joined to pg_namespace). */
    private static int stagingDefaultAcls(DSLContext ctx) {
        Table<?> d = DSL.table(DSL.name("pg_default_acl")).as("d");
        Table<?> n = DSL.table(DSL.name("pg_namespace")).as("n");
        return ctx.fetchCount(d, DSL.exists(DSL.selectOne().from(n)
            .where(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("d", "defaclnamespace"))))
            .and(DSL.field(DSL.name("n", "nspname"), String.class).eq("staging"))));
    }

    /** Schema-scoped default ACLs whose schema no longer exists. */
    private static int orphanedDefaultAcls(DSLContext ctx) {
        Table<?> d = DSL.table(DSL.name("pg_default_acl")).as("d");
        Table<?> n = DSL.table(DSL.name("pg_namespace")).as("n");
        return ctx.fetchCount(d, DSL.field(DSL.name("d", "defaclnamespace"), Long.class).ne(0L)
            .and(DSL.notExists(DSL.selectOne().from(n)
                .where(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("d", "defaclnamespace")))))));
    }

    @Test
    void agedStore_withRows_nexusDiagAndItsStagingGrants_isDropped() throws Exception {
        try (PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated()) {
            // nexus_diag exists BEFORE the pre-drop walk, as it does on every production
            // cluster (created by pg_provision/the DBA, never by the changelog). jOOQ's
            // open-source DSL has no CREATE ROLE, so this is one raw statement.
            try (Connection su = pg.createConnection(""); Statement st = su.createStatement()) {
                su.setAutoCommit(true);
                st.execute("CREATE ROLE nexus_diag NOLOGIN");
            }

            migrateUpToDropChangeset(pg);

            // The aged shape: rows landed, and nexus_diag holds what the old diag-3 granted.
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                assertThat(PgCatalogProbes.tablesInSchema(ctx, "staging"))
                    .as("precondition: the landing tables exist before the drop")
                    .containsExactlyInAnyOrderElementsOf(STAGING_TABLES);
                ctx.insertInto(STAGING_FRECENCY, TENANT_ID, CHUNK_ID).values("t-aged", "feedbeef").execute();
                ctx.insertInto(STAGING_DOC_CHUNKS, TENANT_ID, DOC_ID, POSITION, CHASH)
                    .values("t-aged", "1.1.1", 0, "a".repeat(64)).execute();
                assertThat(ctx.fetchCount(STAGING_FRECENCY) + ctx.fetchCount(STAGING_DOC_CHUNKS))
                    .as("precondition: the aged rows landed, so the drop has data to discard")
                    .isEqualTo(2);
                try (Statement st = su.createStatement()) {
                    // What the OLD grants-nexus-diag-3 gave nexus_diag on staging (before nexus-z0o2p.27).
                    st.execute("GRANT USAGE ON SCHEMA staging TO nexus_diag;"
                        + " GRANT SELECT ON ALL TABLES IN SCHEMA staging TO nexus_diag;"
                        + " ALTER DEFAULT PRIVILEGES IN SCHEMA staging GRANT SELECT ON TABLES TO nexus_diag");
                }
                assertThat(stagingDefaultAcls(ctx))
                    .as("precondition: the old default ACL on schema staging is present")
                    .isPositive();
            }

            updateAll(pg);

            try (Connection su = pg.createConnection("")) {
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                assertThat(schemaExists(ctx)).as("schema staging must be dropped, not just emptied").isFalse();
                for (String t : STAGING_TABLES) {
                    assertThat(PgCatalogProbes.tableExists(ctx, "staging", t))
                        .as("staging.%s must be dropped", t).isFalse();
                }
                assertThat(orphanedDefaultAcls(ctx))
                    .as("no default ACL may outlive its schema: the one on staging dies with it")
                    .isZero();
                assertThat(dropExecType(su)).isEqualTo("EXECUTED");
            }

            // The second and every later boot: every runAlways grant re-executes against a
            // store with no staging schema and nexus_diag present.
            updateAll(pg);
            try (Connection su = pg.createConnection("")) {
                assertThat(schemaExists(DSL.using(su, SQLDialect.POSTGRES))).isFalse();
            }
        }
    }

    @Test
    void foreignObjectInTheSchema_isLeftAlone_andTheWalkDoesNotAbort() throws Exception {
        try (PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated()) {
            migrateUpToDropChangeset(pg);

            Table<?> foreign = DSL.table(DSL.name("staging", "ops_note"));
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                DSL.using(su, SQLDialect.POSTGRES)
                    .createTable(foreign).column("note", SQLDataType.CLOB).execute();
            }

            updateAll(pg); // must not throw: a foreign object never wedges the boot

            try (Connection su = pg.createConnection("")) {
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                assertThat(PgCatalogProbes.tablesInSchema(ctx, "staging"))
                    .as("the seven named tables are dropped; the object this changeset did not "
                        + "name is left exactly where it was")
                    .containsExactly("ops_note");
                assertThat(schemaExists(ctx)).isTrue();
                assertThat(dropExecType(su)).isEqualTo("EXECUTED");
            }
            updateAll(pg); // and a later boot is clean too
        }
    }

    @Test
    void missingSchema_marksTheChangesetRan() throws Exception {
        try (PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated()) {
            migrateUpToDropChangeset(pg);

            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                DSL.using(su, SQLDialect.POSTGRES).dropSchema(DSL.name("staging")).cascade().execute();
            }

            updateAll(pg);

            try (Connection su = pg.createConnection("")) {
                assertThat(dropExecType(su))
                    .as("a store whose staging schema is already gone marks the changeset ran")
                    .isEqualTo("MARK_RAN");
            }
        }
    }
}
