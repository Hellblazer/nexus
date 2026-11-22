// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.CollectionIds;
import dev.nexus.service.db.SchemaMigrator;
import dev.nexus.service.jooq.nexus.Routines;
import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.types.YearToSecond;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.SQLException;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static dev.nexus.service.jooq.nexus.Tables.ASSIGN_FROM_CHASHES_1024;
import static dev.nexus.service.jooq.nexus.Tables.ASSIGN_FROM_CHASHES_384;
import static dev.nexus.service.jooq.nexus.Tables.ASSIGN_FROM_CHASHES_768;
import static dev.nexus.service.jooq.nexus.Tables.CANARY_QUERY_RING;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_OWNERS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_RECONCILE_ROWS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_RECONCILE_RUNS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNK_ORPHANED_AT;
import static dev.nexus.service.jooq.nexus.Tables.GC_AUDIT;
import static dev.nexus.service.jooq.nexus.Tables.GC_EXPIRE_QUARANTINE;
import static dev.nexus.service.jooq.nexus.Tables.GC_EXPIRE_QUARANTINE_BOUNDED;
import static dev.nexus.service.jooq.nexus.Tables.GC_QUARANTINE_ORPHANS;
import static dev.nexus.service.jooq.nexus.Tables.GC_QUARANTINE_ORPHANS_BOUNDED;
import static dev.nexus.service.jooq.nexus.Tables.GC_QUARANTINE_ORPHANS_FLOORED;
import static dev.nexus.service.jooq.nexus.Tables.GC_RESTORE_REREFERENCED_BOUNDED;
import static dev.nexus.service.jooq.nexus.Tables.QUARANTINE_RESTORE_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.REAPER_EXPIRE_CLIENT_QUARANTINE;
import static dev.nexus.service.jooq.nexus.Tables.REAPER_EXPIRE_QUARANTINE;
import static dev.nexus.service.jooq.nexus.Tables.REAPER_QUARANTINE_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS_RECOUNT_DOC_COUNT;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * Shared setup of the RDR-229 P3.1 stage 2b SQL family tests (nexus-p2m1x.17): the scenario model, the seeds that write the layout
 * before vectors-068, the refusal and walk helpers, and the three-state driver {@link #walk}. Not a test.
 *
 * <p>Each test class owns a group of functions and runs its scenarios through {@link #walk} on its own container:
 * {@code Rdr229P31SqlQuarantineDualWriteIntegrationTest}, {@code Rdr229P31SqlTriggerTaxonomyDualWriteIntegrationTest} and
 * {@code Rdr229P31SqlCatalogDualWriteIntegrationTest}, which also holds the attribute, rollback and census checks.
 */
final class P31SqlDualWriteSupport {

    private P31SqlDualWriteSupport() {}

    static final String ADMIN_PASS = "nexus_admin_p31_sqlfn_pass";
    static final String MASTER_CHANGELOG = "db/changelog/db.changelog-master.xml";
    static final String CHANGELOG = "db/changelog/vectors-069-collection-id-dual-write-functions.xml";
    static final Path CHANGELOG_FILE = Path.of("src", "main", "resources", CHANGELOG);
    static final Path CENSUS = Path.of("src", "test", "resources", "rdr229", "p31-writer-census.tsv");
    static final Path CHECKS = Path.of("src", "main", "resources", "db", "rdr229", "p31-checks.tsv");

    static final String EXPAND_FIRST = "vectors-068-1";
    static final String DUAL_FIRST = "vectors-069-1";
    static final String TAG = "before-p31-sql-dual-write";

    static final String T = "p31sql";
    static final String M768 = "bge-base-en-v15-768";
    static final String TS = "2026-11-21T00:00:00Z";
    static final String EXPIRES = "2026-12-21T00:00:00Z";

    /** The census function the changeset leaves alone, with the reason (it writes '' for its collection, so its id is NULL). */
    static final Set<String> NOT_REDEFINED = Set.of("purge_trash");

    // ---- the plumbing ---------------------------------------------------------------------------------------------

    record Env(Connection conn, DSLContext ctx) {
        Env(Connection conn) {
            this(conn, DSL.using(conn, SQLDialect.POSTGRES));
        }

        Env tenant(String tenant) {
            ctx.select(DSL.function("set_config", String.class, DSL.val("nexus.tenant"), DSL.val(tenant), DSL.val(false))).fetch();
            return this;
        }

        long id(String tenant, String name) {
            return CollectionIds.require(ctx, tenant, name);
        }
    }

    interface Act {
        void run(Env e) throws Exception;
    }

    /**
     * One function under test. {@code seed} runs before vectors-068 (the older layout, so its rows are unstamped), {@code call}
     * runs twice (refused before vectors-069 when {@code refusedBefore}, then for real), {@code verify} reads what the real call wrote.
     */
    record Scenario(String name, String tenant, boolean refusedBefore, Act seed, Act call, Act verify) {}

    // ---- names and seeds ------------------------------------------------------------------------------------------

    static String coll(String contentType, String tag, String model) {
        return contentType + "__p31sql-" + tag + "__" + model + "__v1";
    }

    static String docs(String tag) {
        return coll("docs", tag, M768);
    }

    static String quarantine(String origin) {
        return "quarantine-" + origin;
    }

    static void register(Env e, String tenant, String name) {
        PgContainerHelper.insertCollection(e.ctx(), tenant, name);
    }

    /** A chunk written under the older layout (no id column yet); returns its chash. */
    static byte[] chunk(Env e, String tenant, String coll, String tag, int dim, String metadataJson, int ageDays) {
        return chunk(e, tenant, coll, Chash.ofText(coll + "/" + tag).toBytes(), tag, dim, metadataJson, ageDays);
    }

    static byte[] chunk(Env e, String tenant, String coll, byte[] hash, String tag, int dim, String metadataJson, int ageDays) {
        e.ctx().insertInto(CHUNKS)
            .set(CHUNKS.TENANT_ID, tenant)
            .set(CHUNKS.COLLECTION, coll)
            .set(CHUNKS.CHASH, hash)
            .set(CHUNKS.EMBEDDING_MODEL, PgContainerHelper.collectionModel(e.ctx(), tenant, coll))
            .set(CHUNKS.CHUNK_TEXT, tag + " text")
            .set(VectorSeeds.chunkEmbedding(dim), VectorSeeds.vec(dim, 0.1f))
            .set(CHUNKS.METADATA, JSONB.valueOf(metadataJson))
            .set(CHUNKS.LAST_WRITTEN_AT, OffsetDateTime.now().minusDays(ageDays))
            .execute();
        return hash;
    }

    static String hex(byte[] chash) {
        return Chash.fromSha256Bytes(chash).toHex();
    }

    static Long chunkId(Env e, String tenant, String coll, byte[] chash) {
        return e.ctx().select(CHUNKS.COLLECTION_ID).from(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION.eq(coll)).and(CHUNKS.CHASH.eq(chash))
            .fetchOne(CHUNKS.COLLECTION_ID);
    }

    static void assertChunkId(Env e, String tenant, String coll, byte[] chash) {
        assertThat(chunkExists(e, tenant, coll, chash)).as("the chunk is in %s", coll).isTrue();
        assertThat(chunkId(e, tenant, coll, chash)).as("chunk id in %s", coll).isEqualTo(e.id(tenant, coll));
    }

    static boolean chunkExists(Env e, String tenant, String coll, byte[] chash) {
        return e.ctx().fetchExists(CHUNKS, CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(coll)).and(CHUNKS.CHASH.eq(chash)));
    }

    /** The gc_audit rows of {@code operation} for {@code coll}: each must carry the registry's id of {@code coll}. */
    static void assertAuditId(Env e, String tenant, String operation, String coll, String idOfName) {
        List<Long> ids = e.ctx().select(GC_AUDIT.COLLECTION_ID).from(GC_AUDIT)
            .where(GC_AUDIT.TENANT_ID.eq(tenant)).and(GC_AUDIT.OPERATION.eq(operation)).and(GC_AUDIT.COLLECTION.eq(coll))
            .fetch(GC_AUDIT.COLLECTION_ID);
        assertThat(ids).as("gc_audit %s rows for %s", operation, coll).hasSize(1);
        assertThat(ids.get(0)).as("gc_audit %s collection_id", operation).isEqualTo(e.id(tenant, idOfName));
    }

    static void doc(Env e, String tenant, String tumbler, String physical) {
        e.ctx().insertInto(CATALOG_DOCUMENTS)
            .set(CATALOG_DOCUMENTS.TENANT_ID, tenant)
            .set(CATALOG_DOCUMENTS.TUMBLER, tumbler)
            .set(CATALOG_DOCUMENTS.TITLE, "Doc " + tumbler)
            .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, physical)
            .execute();
    }

    static Long docId(Env e, String tenant, String tumbler) {
        return e.ctx().select(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID).from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler))
            .fetchOne(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID);
    }

    static void manifest(Env e, String tenant, String docId, int position, byte[] chash, String coll) {
        e.ctx().insertInto(CATALOG_DOCUMENT_CHUNKS)
            .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, tenant)
            .set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, docId)
            .set(CATALOG_DOCUMENT_CHUNKS.POSITION, position)
            .set(CATALOG_DOCUMENT_CHUNKS.CHASH, chash)
            .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, coll)
            .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, PgContainerHelper.collectionModel(e.ctx(), tenant, coll))
            .execute();
    }

    static long topic(Env e, String tenant, String coll, String label) {
        return e.ctx().insertInto(TOPICS)
            .set(TOPICS.TENANT_ID, tenant).set(TOPICS.LABEL, label).set(TOPICS.COLLECTION, coll)
            .set(TOPICS.CREATED_AT, OffsetDateTime.now())
            .returning(TOPICS.ID).fetchOne().getId();
    }

    static Long topicId(Env e, String tenant, long topic) {
        return e.ctx().select(TOPICS.COLLECTION_ID).from(TOPICS)
            .where(TOPICS.TENANT_ID.eq(tenant)).and(TOPICS.ID.eq(topic)).fetchOne(TOPICS.COLLECTION_ID);
    }

    static void centroid(Env e, String tenant, String coll, long topic, int dim, float fill) {
        e.ctx().insertInto(TAXONOMY_CENTROIDS)
            .set(TAXONOMY_CENTROIDS.TENANT_ID, tenant).set(TAXONOMY_CENTROIDS.COLLECTION, coll)
            .set(TAXONOMY_CENTROIDS.TOPIC_ID, topic)
            .set(TAXONOMY_CENTROIDS.EMBEDDING_MODEL, PgContainerHelper.collectionModel(e.ctx(), tenant, coll))
            .set(TAXONOMY_CENTROIDS.LABEL, "centroid")
            .set(VectorSeeds.centroidEmbedding(dim), VectorSeeds.vec(dim, fill))
            .execute();
    }

    /** A legacy assignment (older layout: no id column), as an install holds one before the stamp. */
    static void assignment(Env e, String tenant, byte[] chash, long topic, String coll, double similarity) {
        e.ctx().insertInto(TOPIC_ASSIGNMENTS)
            .set(TOPIC_ASSIGNMENTS.TENANT_ID, tenant).set(TOPIC_ASSIGNMENTS.DOC_ID, chash).set(TOPIC_ASSIGNMENTS.TOPIC_ID, topic)
            .set(TOPIC_ASSIGNMENTS.ASSIGNED_BY, "seed").set(TOPIC_ASSIGNMENTS.SIMILARITY, similarity)
            .set(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, coll)
            .set(TOPIC_ASSIGNMENTS.EMBEDDING_MODEL, PgContainerHelper.collectionModel(e.ctx(), tenant, coll))
            .execute();
    }

    static Long assignmentId(Env e, String tenant, byte[] chash, long topic) {
        return e.ctx().select(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID).from(TOPIC_ASSIGNMENTS)
            .where(TOPIC_ASSIGNMENTS.TENANT_ID.eq(tenant)).and(TOPIC_ASSIGNMENTS.DOC_ID.eq(chash)).and(TOPIC_ASSIGNMENTS.TOPIC_ID.eq(topic))
            .fetchOne(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID);
    }

    /** The manifest row of an owner document in a collection is the one place a restore's chunk is named; the FK to the chunk is bypassed for it. */
    static void manifestWithoutChunk(Env e, String tenant, String docId, int position, byte[] chash, String coll) throws SQLException {
        e.conn().setAutoCommit(true);
        PgContainerHelper.dropCatalogChunksFk(e.conn());
        manifest(e, tenant, docId, position, chash, coll);
        PgContainerHelper.readdCatalogChunksFkNotValid(e.conn());
    }


    // ---- the walk -----------------------------------------------------------------------------------------------------

    /** What the walk leaves for a caller that checks more: every nexus function's definition before and after vectors-069. */
    record Walk(Map<String, PgCatalogProbes.FunctionDefinition> before, Map<String, PgCatalogProbes.FunctionDefinition> after) {}

    /**
     * Creates the walk's migrating role on the container. The caller supplies it, so its role name is a literal the
     * role census (TestRoleUniquenessScanTest) reads in the caller's own file: it follows a parameter only to callers in the same file.
     */
    interface RoleBootstrap {
        void run(Connection su) throws Exception;
    }

    interface WalkBody {
        void run(PostgreSQLContainer<?> pg, HikariDataSource ds, Walk walk) throws Exception;
    }

    /**
     * Drives {@code scenarios} through the three states of one container: the seeds before vectors-068 (so the rows are unstamped),
     * the refusals with 068 applied and 069 not (at least {@code minRefused} scenarios, each by a collection id CHECK), then the real
     * calls and verifications with 069 applied. {@code bootstrap} creates {@code adminRole} (the caller's own
     * {@code bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS)}). {@code body}, when given, runs last with the container still up.
     */
    static void walk(String adminRole, RoleBootstrap bootstrap, List<Scenario> scenarios, int minRefused, WalkBody body) throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try {
            try (Connection su = pg.createConnection("")) {
                bootstrap.run(su);
            }
            try (HikariDataSource ds = adminPool(pg, adminRole)) {
                // phase 1: the older layout, so the seeded rows are the unstamped rows of an install the CHECKs reach
                migrateUpTo(ds, EXPAND_FIRST);
                List<String> failures = new ArrayList<>();
                try (Connection su = pg.createConnection("")) {
                    su.setAutoCommit(true);
                    PgContainerHelper.installTestObjectsAfterProductSchema(su); // the restore seeds drop and re-add the manifest's chunk foreign key
                    Env env = new Env(su);
                    assertThat(CollectionIdSeeds.hasIdColumns(env.ctx())).as("the seeds run on the layout before vectors-068").isFalse();
                    for (Scenario sc : scenarios) {
                        guard(failures, "seed " + sc.name(), () -> sc.seed().run(env));
                    }
                }
                assertThat(failures).as("the seeds").isEmpty();

                // phase 2: vectors-068 applied, vectors-069 not: the CHECKs refuse each call
                migrateUpTo(ds, DUAL_FIRST);
                tag(ds, TAG);
                Map<String, PgCatalogProbes.FunctionDefinition> before;
                int refused = 0;
                try (Connection su = pg.createConnection("")) {
                    su.setAutoCommit(true);
                    Env env = new Env(su);
                    assertThat(CollectionIdSeeds.hasIdColumns(env.ctx())).isTrue();
                    before = PgCatalogProbes.functionDefinitionsIn(env.ctx(), "nexus");
                    for (Scenario sc : scenarios) {
                        if (!sc.refusedBefore()) continue;
                        refused++;
                        env.tenant(sc.tenant());
                        String state = refusal(() -> sc.call().run(env));
                        if (state == null || !state.contains("23514") || !(state.contains("_id_check") || state.contains("collection_id_not_null"))) {
                            failures.add("before vectors-069, " + sc.name() + " should be refused with 23514 by a collection id CHECK, was: " + state);
                        }
                    }
                }
                assertThat(refused).as("non-vacuity: scenarios refused before the changeset").isGreaterThanOrEqualTo(minRefused);
                assertThat(failures).as("the refusals before vectors-069").isEmpty();

                // phase 3: vectors-069 applied: the same calls succeed and carry the ids
                SchemaMigrator.migrate(ds);
                Map<String, PgCatalogProbes.FunctionDefinition> after;
                try (Connection su = pg.createConnection("")) {
                    su.setAutoCommit(true);
                    Env env = new Env(su);
                    for (Scenario sc : scenarios) {
                        env.tenant(sc.tenant());
                        guard(failures, "call " + sc.name(), () -> sc.call().run(env));
                        guard(failures, "verify " + sc.name(), () -> sc.verify().run(env));
                    }
                    assertThat(failures).as("the calls and the ids after vectors-069").isEmpty();
                    after = PgCatalogProbes.functionDefinitionsIn(env.ctx(), "nexus");
                }
                if (body != null) body.run(pg, ds, new Walk(before, after));
            }
        } finally {
            pg.stop();
        }
    }

    static String stripComments(String body) {
        return body.replaceAll("(?m)--.*$", "").replaceAll("(?s)/\\*.*?\\*/", "");
    }

    static Set<String> censusFunctions() throws Exception {
        Set<String> out = new TreeSet<>();
        Pattern p = Pattern.compile("^nexus\\.(\\w+) \\[");
        for (String line : Files.readAllLines(CENSUS)) {
            if (line.startsWith("#") || line.isBlank()) continue;
            String[] f = line.split("\t");
            if (f.length > 5 && f[5].equals("sql_function")) {
                Matcher m = p.matcher(f[2]);
                assertThat(m.find()).as("census row names its function: %s", line).isTrue();
                out.add(m.group(1));
            }
        }
        return out;
    }

    static Set<String> redefinedFunctions() throws Exception {
        Set<String> out = new LinkedHashSet<>();
        Matcher m = Pattern.compile("(?i)CREATE\\s+OR\\s+REPLACE\\s+FUNCTION\\s+nexus\\.(\\w+)\\(").matcher(stripXmlComments(Files.readString(CHANGELOG_FILE)));
        while (m.find()) out.add(m.group(1));
        return new TreeSet<>(out);
    }

    static String stripXmlComments(String xml) {
        return xml.replaceAll("(?s)<!--.*?-->", "");
    }

    static Set<String> guardedTables() throws Exception {
        Set<String> out = new TreeSet<>();
        for (String line : Files.readAllLines(CHECKS)) {
            if (line.isBlank() || line.startsWith("#") || line.startsWith("table\t")) continue;
            out.add(line.split("\t")[0]);
        }
        return out;
    }

    // ---- helpers -----------------------------------------------------------------------------------------------------------

    interface Body {
        void run() throws Exception;
    }

    static void guard(List<String> failures, String what, Body b) {
        try {
            b.run();
        } catch (Throwable t) {
            failures.add(what + ": " + describe(t));
        }
    }

    static String describe(Throwable t) {
        StringBuilder sb = new StringBuilder(t.getClass().getSimpleName()).append(": ").append(t.getMessage());
        for (Throwable c = t.getCause(); c != null && c != c.getCause(); c = c.getCause()) {
            sb.append(" <- ").append(c.getClass().getSimpleName()).append(": ").append(c.getMessage());
            if (c.getCause() == c) break;
        }
        return sb.toString();
    }

    /** The SQLSTATE and message of the failure, "state: message" (the message of a wrapped SQLSTATE is in the text); null when the call succeeded. */
    static String refusal(Body b) {
        try {
            b.run();
            return null;
        } catch (Throwable t) {
            String state = null;
            for (Throwable c = t; c != null; c = c.getCause()) {
                if (c instanceof SQLException se && se.getSQLState() != null) {
                    state = se.getSQLState();
                    break;
                }
                if (c.getCause() == c) break;
            }
            return state + ": " + describe(t);
        }
    }

    static HikariDataSource adminPool(PostgreSQLContainer<?> pg, String role) {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(role);
        cfg.setPassword(ADMIN_PASS);
        cfg.setMaximumPoolSize(2);
        cfg.setPoolName("nexus-admin-p31-sqlfn");
        return new HikariDataSource(cfg);
    }

    /** Applies the HEAD changelog up to, but not including, {@code changesetId}. */
    static void migrateUpTo(HikariDataSource ds, String changesetId) throws Exception {
        try (Connection conn = ds.getConnection()) {
            Database database = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                List<liquibase.changelog.ChangeSet> unrun = liquibase.listUnrunChangeSets(new Contexts(), new LabelExpression());
                int idx = -1;
                for (int i = 0; i < unrun.size(); i++) {
                    if (changesetId.equals(unrun.get(i).getId())) {
                        idx = i;
                        break;
                    }
                }
                assertThat(idx).as("%s must be pending in the HEAD changelog", changesetId).isGreaterThanOrEqualTo(0);
                liquibase.update(idx, new Contexts(), new LabelExpression());
            }
        }
    }

    static void tag(HikariDataSource ds, String tag) throws Exception {
        try (Connection conn = ds.getConnection()) {
            Database database = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                liquibase.tag(tag);
            }
        }
    }

    static void rollbackTo(HikariDataSource ds, String tag) throws Exception {
        try (Connection conn = ds.getConnection()) {
            Database database = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                liquibase.rollback(tag, new Contexts(), new LabelExpression());
            }
        }
    }

    static boolean recorded(PostgreSQLContainer<?> pg, String id) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).fetchExists(DSL.selectOne().from(DSL.table(DSL.name("public", "databasechangelog")))
                .where(DSL.field(DSL.name("id"), String.class).eq(id)));
        }
    }
}
