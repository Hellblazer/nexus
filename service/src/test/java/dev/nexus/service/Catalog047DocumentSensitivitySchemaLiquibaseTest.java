// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.changelog.ChangeSet;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.ResultSet;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-rfvyy.6.3 (RDR-243 Phase 1 Step 2a): {@code catalog-047-document-sensitivity.xml} adds the one-way
 * document flag {@code catalog_documents.sensitive}, its partial index, the audit table
 * {@code catalog_sensitivity_changes} and the {@code chunk_sensitive} / {@code chunk_sensitive_many} functions.
 *
 * <p>Two shapes: a walk (migrate to just before the changeset, assert the objects are absent, apply, roll the
 * three changesets back, reapply) and the behaviour on a migrated database, as the NOBYPASSRLS service role so
 * row-level security applies: the shared chunk (one sensitive and one plain live owner makes the chunk
 * sensitive; tombstoning the sensitive owner clears it), a collection that does not own the chash, and two
 * tenants (RLS keeps one tenant's flag, audit rows and chunk answers out of the other's reads).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Catalog047DocumentSensitivitySchemaLiquibaseTest {

    private static final String MASTER_CHANGELOG = "db/changelog/db.changelog-master.xml";
    /** The last changeset of the changelog before catalog-047 (the tail it was placed after). */
    private static final String PRIOR_CHANGESET_ID = "vectors-053-1";
    private static final String LAST_TARGET_CHANGESET_ID = "catalog-047-3";

    private static final String SVC_ROLE = "svc_sens047_test";
    private static final String SVC_PASS = "svc_sens047_pass";
    private static final String COLL_C = "knowledge__sens047-c__minilm-l6-v2-384__v1";
    private static final String COLL_D = "knowledge__sens047-d__minilm-l6-v2-384__v1";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    TenantScope scope;
    CatalogRepository repo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(4);
        config.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(config);
        scope = new TenantScope(svcDs);
        repo = new CatalogRepository(scope);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── the walk ────────────────────────────────────────────────────────

    @Test
    void addsTheColumnIndexTableAndFunctions_rollsBackAndReapplies() throws Exception {
        PostgreSQLContainer<?> dedicated = PgContainerHelper.startEmptyDatabase();
        try {
            try (Connection su = dedicated.createConnection("")) {
                migrateUpTo(su, PRIOR_CHANGESET_ID);
            }
            try (Connection su = dedicated.createConnection("")) {
                assertThat(documentColumns(su)).as("before the changeset").doesNotContainKey("sensitive");
                assertThat(tableExists(su, "catalog_sensitivity_changes")).isFalse();
                assertThat(functionNames(su)).doesNotContain("chunk_sensitive", "chunk_sensitive_many");
            }
            try (Connection su = dedicated.createConnection("")) {
                migrateUpTo(su, LAST_TARGET_CHANGESET_ID);
            }
            try (Connection su = dedicated.createConnection("")) {
                assertThat(documentColumns(su)).containsEntry("sensitive", "bool");
                try (ResultSet rs = su.getMetaData().getColumns(null, "nexus", "catalog_documents", "sensitive")) {
                    assertThat(rs.next()).isTrue();
                    assertThat(rs.getString("IS_NULLABLE")).as("NOT NULL").isEqualTo("NO");
                    assertThat(rs.getString("COLUMN_DEF")).as("DEFAULT false").isEqualTo("false");
                }
                assertThat(partialIndexDef(su)).as("the partial index on live sensitive documents")
                    .contains("(tenant_id, physical_collection)")
                    .contains("sensitive AND (deleted_at IS NULL)");
                assertThat(auditColumns(su)).containsOnlyKeys(
                    "id", "tenant_id", "tumbler", "old", "new", "reason", "actor", "changed_at");
                assertThat(auditRlsFlags(su)).as("ENABLE + FORCE row level security, one policy")
                    .containsExactly(true, true, 1);
                assertThat(functionNames(su)).contains("chunk_sensitive", "chunk_sensitive_many");
                assertThat(functionTraits(su, "chunk_sensitive")).as("STABLE SECURITY INVOKER SQL, boolean")
                    .containsExactly("s", false, "sql", "bool");
                assertThat(functionTraits(su, "chunk_sensitive_many")).as("STABLE SECURITY INVOKER SQL, set-returning")
                    .containsExactly("s", false, "sql", "record");
            }
            try (Connection su = dedicated.createConnection("")) {
                Database database = DatabaseFactory.getInstance()
                    .findCorrectDatabaseImplementation(new JdbcConnection(su));
                try (Liquibase liquibase = new Liquibase(
                        MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                    liquibase.rollback(3, new Contexts(), new LabelExpression());
                }
            }
            try (Connection su = dedicated.createConnection("")) {
                assertThat(documentColumns(su)).as("rollback drops the column")
                    .doesNotContainKey("sensitive").containsKeys("tumbler", "physical_collection", "deleted_at");
                assertThat(partialIndexDef(su)).as("rollback drops the index").isNull();
                assertThat(tableExists(su, "catalog_sensitivity_changes")).as("rollback drops the table").isFalse();
                assertThat(functionNames(su)).as("rollback drops both functions")
                    .doesNotContain("chunk_sensitive", "chunk_sensitive_many");
            }
            try (Connection su = dedicated.createConnection("")) {
                Database database = DatabaseFactory.getInstance()
                    .findCorrectDatabaseImplementation(new JdbcConnection(su));
                Liquibase liquibase = new Liquibase(MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database);
                liquibase.update(new Contexts(), new LabelExpression());
                su.setAutoCommit(true);
            }
            try (Connection su = dedicated.createConnection("")) {
                assertThat(documentColumns(su)).containsKey("sensitive");
                assertThat(tableExists(su, "catalog_sensitivity_changes")).isTrue();
                assertThat(functionNames(su)).contains("chunk_sensitive", "chunk_sensitive_many");
            }
        } finally {
            dedicated.stop();
        }
    }

    // ── behaviour on a migrated database ────────────────────────────────

    @Test
    void sharedChunk_isSensitiveWhileASensitiveLiveOwnerHoldsIt_andNotAfterTheTombstone() throws Exception {
        String tenant = "sens047-shared";
        String shared = hex("shared");
        String onlyPlain = hex("only-plain");
        String absent = hex("absent");
        seedCollection(tenant, COLL_C);
        seedCollection(tenant, COLL_D);
        seedChunks(tenant, COLL_C, shared, onlyPlain);
        seedChunks(tenant, COLL_D, shared);
        seedDoc(tenant, "47.1.1", COLL_C, shared);                 // A: sensitive
        seedDoc(tenant, "47.1.2", COLL_C, shared, onlyPlain);      // B: plain
        seedDoc(tenant, "47.1.3", COLL_D, shared);                 // D: plain owner of the same chash elsewhere
        setSensitive(tenant, "47.1.1", true);

        assertThat(sensitive(tenant, tenant, COLL_C, shared)).as("shared, A sensitive and live").isTrue();
        assertThat(sensitive(tenant, tenant, COLL_C, onlyPlain)).as("owned by the plain doc only").isFalse();
        assertThat(sensitive(tenant, tenant, COLL_C, absent)).as("no owner at all").isFalse();
        assertThat(sensitive(tenant, tenant, COLL_D, shared))
            .as("the same chash in another collection is owned by a plain document there").isFalse();
        assertThat(sensitiveMany(tenant, tenant, COLL_C, shared, onlyPlain, absent, shared))
            .as("the batched form answers one row per input chash, in input order")
            .containsExactly(true, false, false, true);
        assertThat(sensitiveMany(tenant, tenant, COLL_D, shared)).containsExactly(false);
        assertThat(sensitiveMany(tenant, tenant, COLL_C)).as("empty input").isEmpty();

        repo.deleteDocument(tenant, "47.1.1");

        assertThat(sensitive(tenant, tenant, COLL_C, shared)).as("after the sensitive owner is tombstoned").isFalse();
        assertThat(sensitiveMany(tenant, tenant, COLL_C, shared, onlyPlain)).containsExactly(false, false);
    }

    @Test
    void twoTenants_rlsKeepsTheFlagTheAuditRowsAndTheChunkAnswerApart() throws Exception {
        String a = "sens047-ta";
        String b = "sens047-tb";
        String chash = hex("two-tenants");
        for (String t : List.of(a, b)) {
            seedCollection(t, COLL_C);
            seedChunks(t, COLL_C, chash);
        }
        seedDoc(a, "47.2.1", COLL_C, chash);
        setSensitive(a, "47.2.1", true);
        seedDoc(b, "47.2.1", COLL_C, chash);      // tenant B's own, plain, owner of the same chash
        scope.withTenant(a, ctx -> ctx.execute(
            "INSERT INTO nexus.catalog_sensitivity_changes (tenant_id, tumbler, old, new, reason, actor) "
                + "VALUES (?, ?, false, true, 'set', 'tester')", a, "47.2.1"));

        assertThat(sensitive(a, a, COLL_C, chash)).as("tenant A sees its own sensitive owner").isTrue();
        assertThat(sensitive(b, a, COLL_C, chash))
            .as("tenant B asking about tenant A's chunk: SECURITY INVOKER lets RLS hide A's rows").isFalse();
        assertThat(sensitive(b, b, COLL_C, chash)).as("tenant B's own owner is plain").isFalse();
        assertThat(sensitiveMany(b, a, COLL_C, chash)).containsExactly(false);

        assertThat(auditRows(a)).as("tenant A reads its audit row").isEqualTo(1);
        assertThat(auditRows(b)).as("tenant B reads none of A's audit rows").isZero();
        assertThat(sensitiveDocumentCount(b)).as("tenant B sees none of A's flagged documents").isZero();
        assertThat(sensitiveDocumentCount(a)).isEqualTo(1);

        assertThatThrownBy(() -> scope.withTenant(b, ctx -> ctx.execute(
                "INSERT INTO nexus.catalog_sensitivity_changes (tenant_id, tumbler, old, new, reason, actor) "
                    + "VALUES (?, '47.2.1', false, true, 'forged', 'tester')", a)))
            .as("WITH CHECK refuses a row for another tenant")
            .hasMessageContaining("row-level security");
    }

    @Test
    void auditTable_isNotReadableByNexusDiag() {
        assertThat(CatalogRepository.NEXUS_DIAG_READABLE_TABLES)
            .as("the sensitivity audit trail is tenant-scoped; nexus_diag bypasses RLS and is granted nothing on it")
            .doesNotContain("catalog_sensitivity_changes");
    }

    // ── seeding and reading ─────────────────────────────────────────────

    private static String hex(String seed) {
        try {
            var md = java.security.MessageDigest.getInstance("SHA-256");
            var out = new StringBuilder();
            for (byte x : md.digest(seed.getBytes(java.nio.charset.StandardCharsets.UTF_8))) {
                out.append(String.format("%02x", x));
            }
            return out.toString();
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    private void seedCollection(String tenant, String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), tenant, collection);
        }
    }

    private void seedChunks(String tenant, String collection, String... chashHex) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var hexes = List.of(chashHex);
            PgContainerHelper.insertChunks(DSL.using(su, SQLDialect.POSTGRES), tenant, collection, hexes,
                hexes.stream().map(h -> "text " + h.substring(0, 8)).toList(),
                hexes.stream().map(h -> new float[384]).toList(),
                hexes.stream().map(h -> Map.<String, Object>of()).toList());
        }
    }

    private void seedDoc(String tenant, String tumbler, String collection, String... chashHex) {
        repo.upsertDocument(tenant, Map.of(
            "tumbler", tumbler, "title", "sens047 " + tumbler, "content_type", "prose", "corpus", "knowledge",
            "physical_collection", collection));
        var rows = new ArrayList<Map<String, Object>>();
        for (int i = 0; i < chashHex.length; i++) {
            rows.add(Map.<String, Object>of("position", i, "chash", chashHex[i], "chunk_index", i));
        }
        repo.writeManifest(tenant, tumbler, collection, rows);
    }

    private void setSensitive(String tenant, String tumbler, boolean value) throws Exception {
        scope.withTenant(tenant, ctx -> ctx.execute(
            "UPDATE nexus.catalog_documents SET sensitive = ? WHERE tenant_id = ? AND tumbler = ?",
            value, tenant, tumbler));
    }

    /** chunk_sensitive called under {@code guc}'s tenant GUC, asking about {@code asked}'s chunk. */
    private boolean sensitive(String guc, String asked, String collection, String chashHex) {
        byte[] chash = Chash.fromHex(chashHex).toBytes();
        return Boolean.TRUE.equals(scope.withTenant(guc, ctx -> ctx.fetchValue(
            "SELECT nexus.chunk_sensitive(?, ?, ?)", asked, collection, chash)));
    }

    private List<Boolean> sensitiveMany(String guc, String asked, String collection, String... chashHex) {
        // hex text[] decoded to bytea[] in SQL, keeping the order (a byte[][] bind would read as varargs)
        Object[] binds = {asked, collection, chashHex};
        return scope.withTenant(guc, ctx -> ctx.fetch(
            "SELECT sensitive FROM nexus.chunk_sensitive_many(?, ?, "
                + "(SELECT array_agg(decode(t.h, 'hex') ORDER BY t.o) FROM unnest(?::text[]) WITH ORDINALITY AS t(h, o)))",
            binds).getValues("sensitive", Boolean.class));
    }

    private int auditRows(String tenant) {
        return scope.withTenant(tenant, ctx -> ctx.fetchOne(
            "SELECT count(*)::int FROM nexus.catalog_sensitivity_changes").get(0, Integer.class));
    }

    private int sensitiveDocumentCount(String tenant) {
        return scope.withTenant(tenant, ctx -> ctx.fetchOne(
            "SELECT count(*)::int FROM nexus.catalog_documents WHERE sensitive").get(0, Integer.class));
    }

    // ── catalog metadata reads (JDBC metadata and pg_catalog) ───────────

    private static Map<String, String> documentColumns(Connection su) throws Exception {
        return columns(su, "catalog_documents");
    }

    private static Map<String, String> auditColumns(Connection su) throws Exception {
        return columns(su, "catalog_sensitivity_changes");
    }

    private static Map<String, String> columns(Connection su, String table) throws Exception {
        Map<String, String> out = new HashMap<>();
        try (ResultSet rs = su.getMetaData().getColumns(null, "nexus", table, null)) {
            while (rs.next()) out.put(rs.getString("COLUMN_NAME"), rs.getString("TYPE_NAME"));
        }
        return out;
    }

    private static boolean tableExists(Connection su, String table) throws Exception {
        try (ResultSet rs = su.getMetaData().getTables(null, "nexus", table, new String[] {"TABLE"})) {
            return rs.next();
        }
    }

    private static String partialIndexDef(Connection su) throws Exception {
        try (var st = su.createStatement();
             ResultSet rs = st.executeQuery("SELECT indexdef FROM pg_indexes WHERE schemaname = 'nexus' "
                 + "AND indexname = 'idx_catalog_documents_sensitive_live'")) {
            return rs.next() ? rs.getString(1) : null;
        }
    }

    /** relrowsecurity, relforcerowsecurity, policy count. */
    private static List<Object> auditRlsFlags(Connection su) throws Exception {
        try (var st = su.createStatement();
             ResultSet rs = st.executeQuery("SELECT c.relrowsecurity, c.relforcerowsecurity, "
                 + "(SELECT count(*)::int FROM pg_policy p WHERE p.polrelid = c.oid) "
                 + "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                 + "WHERE n.nspname = 'nexus' AND c.relname = 'catalog_sensitivity_changes'")) {
            assertThat(rs.next()).isTrue();
            return List.of(rs.getBoolean(1), rs.getBoolean(2), rs.getInt(3));
        }
    }

    private static List<String> functionNames(Connection su) throws Exception {
        List<String> out = new ArrayList<>();
        try (var st = su.createStatement();
             ResultSet rs = st.executeQuery("SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                 + "WHERE n.nspname = 'nexus'")) {
            while (rs.next()) out.add(rs.getString(1));
        }
        return out;
    }

    /** provolatile, prosecdef, language, return type name. */
    private static List<Object> functionTraits(Connection su, String name) throws Exception {
        try (var st = su.createStatement();
             ResultSet rs = st.executeQuery("SELECT p.provolatile::text, p.prosecdef, l.lanname, "
                 + "(SELECT typname FROM pg_type t WHERE t.oid = p.prorettype)::text "
                 + "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                 + "JOIN pg_language l ON l.oid = p.prolang "
                 + "WHERE n.nspname = 'nexus' AND p.proname = '" + name + "'")) {
            assertThat(rs.next()).as(name + " exists").isTrue();
            List<Object> out = List.of(rs.getString(1), rs.getBoolean(2), rs.getString(3), rs.getString(4));
            assertThat(rs.next()).as(name + " has one overload").isFalse();
            return out;
        }
    }

    private static void migrateUpTo(Connection conn, String targetChangesetId) throws Exception {
        Database database = DatabaseFactory.getInstance()
            .findCorrectDatabaseImplementation(new JdbcConnection(conn));
        Liquibase liquibase = new Liquibase(MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database);
        List<ChangeSet> unrun = liquibase.listUnrunChangeSets(new Contexts(), new LabelExpression());
        int idx = -1;
        for (int i = 0; i < unrun.size(); i++) {
            if (targetChangesetId.equals(unrun.get(i).getId())) {
                idx = i;
                break;
            }
        }
        assertThat(idx).as(targetChangesetId + " must be present in the master changelog").isGreaterThanOrEqualTo(0);
        liquibase.update(idx + 1, new Contexts(), new LabelExpression());
        conn.setAutoCommit(true);
    }
}
