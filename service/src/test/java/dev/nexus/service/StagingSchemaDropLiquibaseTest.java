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
import java.sql.DriverManager;
import java.sql.SQLWarning;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

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
 *   <li>{@link #namedTableWithADependentViewOutsideTheSchema_isLeftAlone_andTheWalkDoesNotAbort}
 *       and {@link #namedTableReferencedByAForeignKeyFromOutside_isLeftAlone_andTheWalkDoesNotAbort}:
 *       a table the changeset DID name that something outside the schema
 *       depends on (SQLSTATE 2BP01 from {@code DROP TABLE}) stays, with the
 *       schema, and the other six go.</li>
 *   <li>{@link #nosuperuserOwnerRole_dropsEverythingItOwns_andTheSchema} and
 *       {@link #nosuperuserOwnerRole_withForeignOwnedObjects_dropsWhatItNames_andLeavesWhatItDoesNot}:
 *       the production shape, a NOSUPERUSER role that created the schema (the
 *       nexus_admin shape), clean and with foreign-owned objects.</li>
 *   <li>{@link #nonOwnerRole_everyDropRaisesInsufficientPrivilege_andNothingIsDropped}:
 *       the {@code insufficient_privilege} handlers, which the superuser
 *       connection every other test uses can never reach; the NOTICEs are
 *       asserted on the statement's warning chain.</li>
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

    private static final String OWNER_ROLE = "nexus_admin_stg";
    private static final String OWNER_PASS = "nexus_admin_stg_pw";

    // Each Liquibase run gets its own connection: Liquibase#close() closes the
    // connection it was built on.
    private static void migrateUpToDropChangeset(PostgreSQLContainer<?> pg) throws Exception {
        try (Connection su = pg.createConnection("")) {
            walkUpToDropChangeset(su);
        }
    }

    private static void walkUpToDropChangeset(Connection conn) throws Exception {
        Database db = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(conn));
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

    private static void updateAll(PostgreSQLContainer<?> pg) throws Exception {
        try (Connection su = pg.createConnection("")) {
            updateAll(su);
        }
    }

    private static void updateAll(Connection conn) throws Exception {
        Database db = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(conn));
        try (Liquibase liquibase = new Liquibase(MASTER, new ClassLoaderResourceAccessor(), db)) {
            liquibase.update(new Contexts(), new LabelExpression());
        }
    }

    private static Connection ownerConnection(PostgreSQLContainer<?> pg) throws Exception {
        return DriverManager.getConnection(pg.getJdbcUrl(), OWNER_ROLE, OWNER_PASS);
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
    void namedTableWithADependentViewOutsideTheSchema_isLeftAlone_andTheWalkDoesNotAbort() throws Exception {
        try (PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated()) {
            migrateUpToDropChangeset(pg);

            // An ops-created view over a table the changeset DOES name: DROP TABLE of it
            // raises dependent_objects_still_exist (2BP01), the same class of "something
            // outside this changeset depends on it" as a foreign object in the schema.
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                DSL.using(su, SQLDialect.POSTGRES)
                    .createView(DSL.name("public", "ops_v"))
                    .as(DSL.select(TENANT_ID, CHUNK_ID).from(STAGING_FRECENCY))
                    .execute();
            }

            updateAll(pg); // must not throw: a dependent view never wedges the boot

            try (Connection su = pg.createConnection("")) {
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                assertThat(PgCatalogProbes.tablesInSchema(ctx, "staging"))
                    .as("the table the view depends on stays; the other six named tables go")
                    .containsExactly("frecency");
                assertThat(schemaExists(ctx)).as("a non-empty schema is not dropped").isTrue();
                assertThat(PgCatalogProbes.viewExists(ctx, "public", "ops_v"))
                    .as("the ops-created view is untouched").isTrue();
                assertThat(dropExecType(su)).isEqualTo("EXECUTED");
            }
            updateAll(pg); // and a later boot is clean too
        }
    }

    @Test
    void namedTableReferencedByAForeignKeyFromOutside_isLeftAlone_andTheWalkDoesNotAbort() throws Exception {
        try (PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated()) {
            migrateUpToDropChangeset(pg);

            // A DBA table in public with a foreign key INTO staging.frecency's primary key.
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                DSL.using(su, SQLDialect.POSTGRES)
                    .createTable(DSL.name("public", "ops_ref"))
                    .column(TENANT_ID, SQLDataType.CLOB)
                    .column(CHUNK_ID, SQLDataType.CLOB)
                    .constraint(DSL.foreignKey(TENANT_ID, CHUNK_ID)
                        .references(STAGING_FRECENCY, TENANT_ID, CHUNK_ID))
                    .execute();
            }

            updateAll(pg); // must not throw: a foreign key from outside never wedges the boot

            try (Connection su = pg.createConnection("")) {
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                assertThat(PgCatalogProbes.tablesInSchema(ctx, "staging"))
                    .as("the referenced table stays; the other six named tables go")
                    .containsExactly("frecency");
                assertThat(schemaExists(ctx)).isTrue();
                assertThat(PgCatalogProbes.tableExists(ctx, "public", "ops_ref")).isTrue();
                assertThat(dropExecType(su)).isEqualTo("EXECUTED");
            }
            updateAll(pg);
        }
    }

    /**
     * The nexus_admin shape: a NOSUPERUSER role that CREATED schema staging (so owns it and
     * every table in it), as production's migrator does. The clean case: it drops all seven
     * tables and the schema.
     */
    @Test
    void nosuperuserOwnerRole_dropsEverythingItOwns_andTheSchema() throws Exception {
        try (PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated()) {
            provisionOwnerRole(pg);
            try (Connection owner = ownerConnection(pg)) {
                walkUpToDropChangeset(owner);
            }
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                assertThat(schemaOwner(ctx))
                    .as("precondition: the NOSUPERUSER role owns the schema, as nexus_admin does")
                    .isEqualTo(OWNER_ROLE);
                assertThat(PgCatalogProbes.tablesInSchema(ctx, "staging"))
                    .containsExactlyInAnyOrderElementsOf(STAGING_TABLES);
            }
            try (Connection owner = ownerConnection(pg)) {
                updateAll(owner);
            }
            try (Connection su = pg.createConnection("")) {
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                assertThat(schemaExists(ctx)).as("the owner drops its own schema").isFalse();
                assertThat(dropExecType(su)).isEqualTo("EXECUTED");
            }
        }
    }

    /**
     * Same owner role, now with objects another role owns. PostgreSQL lets the owner of a
     * schema drop a table someone else owns, so a foreign-owned NAMED table goes with the rest
     * (probed by hand in the round-2 critique, 2026-10-01; asserted here). A foreign-owned
     * object the changeset did NOT name is what stays, and it keeps the schema alive. The walk
     * completes either way.
     */
    @Test
    void nosuperuserOwnerRole_withForeignOwnedObjects_dropsWhatItNames_andLeavesWhatItDoesNot() throws Exception {
        try (PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated()) {
            provisionOwnerRole(pg);
            try (Connection owner = ownerConnection(pg)) {
                walkUpToDropChangeset(owner);
            }
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                try (Statement st = su.createStatement()) {
                    st.execute("CREATE ROLE ops_dba NOLOGIN");
                    st.execute("ALTER TABLE staging.relevance_log OWNER TO ops_dba");
                }
                ctx.createTable(DSL.name("staging", "ops_note")).column("note", SQLDataType.CLOB).execute();
                try (Statement st = su.createStatement()) {
                    st.execute("ALTER TABLE staging.ops_note OWNER TO ops_dba");
                }
            }

            try (Connection owner = ownerConnection(pg)) {
                updateAll(owner); // must not throw
            }

            try (Connection su = pg.createConnection("")) {
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                assertThat(PgCatalogProbes.tablesInSchema(ctx, "staging"))
                    .as("all seven named tables go, the foreign-owned one included (the schema "
                        + "owner may drop it); only the object the changeset did not name stays")
                    .containsExactly("ops_note");
                assertThat(schemaExists(ctx)).isTrue();
                assertThat(dropExecType(su)).isEqualTo("EXECUTED");
            }
            try (Connection owner = ownerConnection(pg)) {
                updateAll(owner); // a later boot is clean too
            }
        }
    }

    /**
     * The {@code insufficient_privilege} handlers. The role every other test connects as is a
     * superuser, which can never raise 42501, so the changeset body is extracted from the
     * changelog and run under {@code SET ROLE} as a role that owns nothing: every DROP TABLE
     * and the DROP SCHEMA are refused, each refusal is a NOTICE, the block returns, and
     * nothing is dropped (rows included).
     */
    @Test
    void nonOwnerRole_everyDropRaisesInsufficientPrivilege_andNothingIsDropped() throws Exception {
        try (PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated()) {
            migrateUpToDropChangeset(pg);

            String body = extractChangesetBody(DROP_CHANGESET);
            assertThat(body).contains("DROP SCHEMA staging");

            List<String> notices = new ArrayList<>();
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                ctx.insertInto(STAGING_FRECENCY, TENANT_ID, CHUNK_ID).values("t-keep", "feedbeef").execute();
                try (Statement st = su.createStatement()) {
                    st.execute("CREATE ROLE staging_nonowner NOLOGIN");
                    st.execute("GRANT USAGE ON SCHEMA staging TO staging_nonowner");
                    st.execute("SET ROLE staging_nonowner");
                    try {
                        st.execute(body); // must not throw: a refused drop is a NOTICE, never an error
                        for (SQLWarning w = st.getWarnings(); w != null; w = w.getNextWarning()) {
                            notices.add(w.getMessage());
                        }
                    } finally {
                        st.execute("RESET ROLE");
                    }
                }

                assertThat(PgCatalogProbes.tablesInSchema(ctx, "staging"))
                    .as("a role that owns nothing drops nothing")
                    .containsExactlyInAnyOrderElementsOf(STAGING_TABLES);
                assertThat(schemaExists(ctx)).isTrue();
                assertThat(ctx.fetchCount(STAGING_FRECENCY)).as("the landed row survives").isEqualTo(1);
            }
            for (String t : STAGING_TABLES) {
                assertThat(notices)
                    .as("a NOTICE names each table left in place")
                    .anyMatch(n -> n.contains("staging." + t + " left in place"));
            }
            assertThat(notices)
                .as("and one names the schema")
                .anyMatch(n -> n.contains("schema staging left in place"));
        }
    }

    /**
     * The rollback is idempotent over a partial drop. The tolerant body can leave a table (and
     * the schema) behind; a rollback after such a boot meets objects that already exist. Its
     * body is extracted from the changelog and run twice: once over the partially dropped
     * schema (one table survived because a view depends on it), once over the now-complete one.
     */
    @Test
    void rollback_isIdempotent_overAPartiallyDroppedSchema() throws Exception {
        try (PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated()) {
            migrateUpToDropChangeset(pg);
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                DSL.using(su, SQLDialect.POSTGRES)
                    .createView(DSL.name("public", "ops_v"))
                    .as(DSL.select(TENANT_ID, CHUNK_ID).from(STAGING_FRECENCY))
                    .execute();
            }
            updateAll(pg);

            String rollback = extractChangesetRollback(DROP_CHANGESET);
            assertThat(rollback).contains("CREATE POLICY tenant_isolation");
            try (Connection su = pg.createConnection(""); Statement st = su.createStatement()) {
                su.setAutoCommit(true);
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                assertThat(PgCatalogProbes.tablesInSchema(ctx, "staging"))
                    .as("precondition: the tolerant drop left exactly one table behind")
                    .containsExactly("frecency");

                st.execute(rollback); // over a partial drop: frecency and its policy already exist
                st.execute(rollback); // and again over the complete structure

                assertThat(PgCatalogProbes.tablesInSchema(ctx, "staging"))
                    .containsExactlyInAnyOrderElementsOf(STAGING_TABLES);
                for (String t : STAGING_TABLES) {
                    assertThat(PgCatalogProbes.policyExists(ctx, "staging", t, "tenant_isolation"))
                        .as("staging.%s has exactly its tenant_isolation policy", t).isTrue();
                    assertThat(PgCatalogProbes.policies(ctx, "staging", t))
                        .as("staging.%s: one policy, not a duplicate per run", t).hasSize(1);
                }
            }
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

    private static final Pattern SQL_BODY = Pattern.compile("<sql(?:\\s[^>]*)?>(.*?)</sql>", Pattern.DOTALL);

    /** The {@code <sql>} text of one changeset, read from the changelog on the classpath. */
    private static String extractChangesetBody(String changesetId) throws Exception {
        String xml;
        try (var in = StagingSchemaDropLiquibaseTest.class.getClassLoader()
                .getResourceAsStream("db/changelog/staging-003-drop-landing-schema.xml")) {
            assertThat(in).as("staging-003 changelog on the classpath").isNotNull();
            xml = new String(in.readAllBytes(), java.nio.charset.StandardCharsets.UTF_8);
        }
        int at = xml.indexOf("<changeSet id=\"" + changesetId + "\"");
        assertThat(at).as("changeset " + changesetId).isGreaterThanOrEqualTo(0);
        Matcher m = SQL_BODY.matcher(xml);
        assertThat(m.find(at)).isTrue();
        return m.group(1);
    }

    private static final Pattern ROLLBACK_BODY = Pattern.compile("<rollback>(.*?)</rollback>", Pattern.DOTALL);

    /** The {@code <rollback>} text of one changeset, read from the changelog on the classpath. */
    private static String extractChangesetRollback(String changesetId) throws Exception {
        String xml;
        try (var in = StagingSchemaDropLiquibaseTest.class.getClassLoader()
                .getResourceAsStream("db/changelog/staging-003-drop-landing-schema.xml")) {
            assertThat(in).as("staging-003 changelog on the classpath").isNotNull();
            xml = new String(in.readAllBytes(), java.nio.charset.StandardCharsets.UTF_8);
        }
        int at = xml.indexOf("<changeSet id=\"" + changesetId + "\"");
        assertThat(at).as("changeset " + changesetId).isGreaterThanOrEqualTo(0);
        Matcher m = ROLLBACK_BODY.matcher(xml);
        assertThat(m.find(at)).isTrue();
        return m.group(1);
    }

    private static String schemaOwner(DSLContext ctx) {
        return ctx.select(DSL.function("pg_get_userbyid", String.class, DSL.field(DSL.name("nspowner"))))
            .from(DSL.table(DSL.name("pg_namespace")))
            .where(DSL.field(DSL.name("nspname"), String.class).eq("staging"))
            .fetchOne(0, String.class);
    }

    /**
     * Provision the cluster the way production's DBA does: a NOSUPERUSER role that will run
     * the walk (and so create and own schema staging), the service role, the CREATE grants a
     * schema creator needs, and the extension-relocation helper the mid-walk
     * search-path-001 changeset calls. Mirrors SchemaMigratorIntegrationTest's bootstrap.
     */
    private static void provisionOwnerRole(PostgreSQLContainer<?> pg) throws Exception {
        try (Connection su = pg.createConnection(""); Statement st = su.createStatement()) {
            su.setAutoCommit(true);
            st.execute("CREATE ROLE " + OWNER_ROLE + " LOGIN PASSWORD '" + OWNER_PASS
                + "' NOSUPERUSER NOCREATEDB NOCREATEROLE");
            st.execute("CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'"
                + " NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS");
            st.execute("GRANT CREATE ON DATABASE " + pg.getDatabaseName() + " TO " + OWNER_ROLE);
            st.execute("GRANT CREATE ON SCHEMA public TO " + OWNER_ROLE);
            st.execute("GRANT pg_monitor TO " + OWNER_ROLE + " WITH ADMIN OPTION");
            SchemaMigratorIntegrationTest.bootstrapVectorExtensionsForFreshWalk(su, OWNER_ROLE);
        }
    }
}
