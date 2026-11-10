// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.Record;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_LINKS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_OWNERS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_RECONCILE_ROWS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_RECONCILE_RUNS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_SENSITIVITY_CHANGES;

/**
 * RDR-193 Part A1 (nexus-u5m5g.3): the shared fixture of {@code CatalogReconcileCommitInvariantsTest} and
 * {@code CatalogReconcileCommitBehaviourTest}. One migrated database, the service role (NOSUPERUSER, NOBYPASSRLS, so
 * row-level security applies), a fresh tenant per test, and builders for the landing rows a client stages. Not a test.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
abstract class CatalogReconcileTestBase {

    static final Duration COMMIT_TIMEOUT = Duration.ofMinutes(5);
    private static final AtomicInteger TENANTS = new AtomicInteger();

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    TenantScope scope;
    CatalogRepository repo;

    /** The service role this class connects as (set by {@link #connect}); a literal in each subclass's own boot. */
    String svcUser;
    String svcPassword;

    /** Start the shared migrated database and apply the product schema; the subclass then bootstraps its literal role. */
    void migrate() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
    }

    /** Open the pool as the service role the subclass has just bootstrapped (NOSUPERUSER, NOBYPASSRLS: RLS applies). */
    void connect(String role, String password) {
        svcUser = role;
        svcPassword = password;
        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(role);
        config.setPassword(password);
        config.setMaximumPoolSize(6);
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

    // ── tenants, owners, seeded documents ───────────────────────────────

    String newTenant() {
        return "rc193-" + TENANTS.incrementAndGet();
    }

    /** A repo owner at {@code prefix} with the given repo_root. */
    void owner(String tenant, String prefix, String repoRoot) {
        repo.upsertOwner(tenant, Map.of(
            "tumbler_prefix", prefix, "name", "own-" + prefix, "owner_type", "repo",
            "repo_hash", "h-" + tenant + "-" + prefix, "repo_root", repoRoot));
    }

    /** Registers a document under {@code prefix} (the engine derives source_uri from file_path); returns its tumbler. */
    String seed(String tenant, String prefix, String filePath, Map<String, Object> extra) {
        Map<String, Object> f = new LinkedHashMap<>();
        f.put("title", filePath);
        f.put("file_path", filePath);
        f.put("content_type", "code");
        f.put("physical_collection", "code__seed");
        f.put("head_hash", "H0");
        f.putAll(extra);
        return repo.registerDocument(tenant, prefix, f);
    }

    String seed(String tenant, String prefix, String filePath) {
        return seed(tenant, prefix, filePath, Map.of());
    }

    // ── landing rows ────────────────────────────────────────────────────

    /** One staged row, built fluently. Defaults: kind present, collection {@code code__seed}, head {@code H1}. */
    static final class R {
        final Map<String, Object> m = new LinkedHashMap<>();

        R(int seq, String kind, String path) {
            m.put("seq", seq);
            m.put("kind", kind);
            m.put("file_path", path);
            m.put("content_type", "code");
            m.put("title", path.substring(path.lastIndexOf('/') + 1));
            m.put("physical_collection", "code__seed");
            m.put("head_hash", "H1");
            m.put("source_mtime", 1.0);
            m.put("content_hash", "");
            m.put("base_metadata", Map.of());
            m.put("stamp", Map.of());
            m.put("sensitive", false);
        }

        R set(String k, Object v) {
            m.put(k, v);
            return this;
        }

        R hash(String h) {
            m.put("content_hash", h);
            Map<String, Object> bm = new LinkedHashMap<>((Map<String, Object>) m.get("base_metadata"));
            if (!h.isEmpty()) bm.put("content_hash", h);
            m.put("base_metadata", bm);
            return this;
        }

        R stamp(Map<String, Object> s) {
            m.put("stamp", s);
            return this;
        }

        R sensitive() {
            m.put("sensitive", true);
            return this;
        }
    }

    static R present(int seq, String path) {
        return new R(seq, "present", path);
    }

    static R keep(int seq, String path) {
        return new R(seq, "keep", path);
    }

    static R deleted(int seq, String path) {
        return new R(seq, "deleted", path);
    }

    List<Map<String, Object>> maps(R... rows) {
        var out = new ArrayList<Map<String, Object>>();
        for (R r : rows) out.add(r.m);
        return out;
    }

    void stage(String tenant, String owner, String run, String walk, R... rows) {
        repo.reconcileStage(tenant, owner, run, walk, rows.length, maps(rows));
    }

    /** Stage {@code rows} in one page and commit; returns the outcome. */
    Map<String, Object> reconcile(String tenant, String owner, String run, String walk, R... rows) {
        stage(tenant, owner, run, walk, rows);
        return repo.reconcileCommit(tenant, owner, run, COMMIT_TIMEOUT);
    }

    // ── reading the outcome and the catalog ─────────────────────────────

    @SuppressWarnings("unchecked")
    static List<Map<String, Object>> outcomeRows(Map<String, Object> outcome) {
        return (List<Map<String, Object>>) outcome.get("rows");
    }

    static Map<String, Object> outcomeRow(Map<String, Object> outcome, String filePath) {
        return outcomeRows(outcome).stream().filter(r -> filePath.equals(r.get("file_path"))).findFirst()
            .orElseThrow(() -> new AssertionError("no outcome row for " + filePath + " in " + outcome));
    }

    static String actionOf(Map<String, Object> outcome, String filePath) {
        return (String) outcomeRow(outcome, filePath).get("action");
    }

    static String tumblerOf(Map<String, Object> outcome, String filePath) {
        return (String) outcomeRow(outcome, filePath).get("tumbler");
    }

    @SuppressWarnings("unchecked")
    static List<Object> listOf(Map<String, Object> outcome, String key) {
        Object v = outcome.get(key);
        return v == null ? List.of() : (List<Object>) v;
    }

    /** A catalog_documents row (live or not) by tumbler, as a column map; null when absent. */
    Map<String, Object> doc(String tenant, String tumbler) {
        return scope.withTenant(tenant, ctx -> {
            Record r = ctx.select().from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler))).fetchOne();
            return r == null ? null : r.intoMap();
        });
    }

    /** The live row of {@code filePath} under {@code prefix} (the greatest tumbler), or null. */
    Map<String, Object> liveDoc(String tenant, String prefix, String filePath) {
        return scope.withTenant(tenant, ctx -> {
            Record r = ctx.select().from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.FILE_PATH.eq(filePath))
                    .and(CATALOG_DOCUMENTS.TUMBLER.startsWith(prefix + ".")).and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
                .orderBy(CATALOG_DOCUMENTS.TUMBLER.desc()).limit(1).fetchOne();
            return r == null ? null : r.intoMap();
        });
    }

    /** One column of one document as a string (null when NULL or the row is absent). */
    String col(String tenant, String tumbler, String column) {
        Field<?> f = CATALOG_DOCUMENTS.field(column);
        return scope.withTenant(tenant, ctx -> {
            Record r = ctx.select(f).from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler))).fetchOne();
            return r == null || r.get(0) == null ? null : String.valueOf(r.get(0));
        });
    }

    java.time.OffsetDateTime indexedAt(String tenant, String tumbler) {
        return scope.withTenant(tenant, ctx -> ctx.select(CATALOG_DOCUMENTS.INDEXED_AT).from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler)))
            .fetchOne(CATALOG_DOCUMENTS.INDEXED_AT));
    }

    private static final com.fasterxml.jackson.databind.ObjectMapper JSON = new com.fasterxml.jackson.databind.ObjectMapper();

    /** {@code metadata[key]} of a document as compact JSON text, or null when the key (or the metadata) is absent. */
    String metaOf(String tenant, String tumbler, String key) {
        JSONB meta = scope.withTenant(tenant, ctx -> ctx.select(CATALOG_DOCUMENTS.METADATA).from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler)))
            .fetchOne(CATALOG_DOCUMENTS.METADATA));
        if (meta == null) return null;
        try {
            var node = JSON.readTree(meta.data()).get(key);
            return node == null ? null : node.toString();
        } catch (com.fasterxml.jackson.core.JsonProcessingException e) {
            throw new IllegalStateException(e);
        }
    }

    int liveCount(String tenant, String prefix) {
        return scope.withTenant(tenant, ctx -> ctx.fetchCount(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)
            .and(CATALOG_DOCUMENTS.TUMBLER.startsWith(prefix + ".")).and(CATALOG_DOCUMENTS.DELETED_AT.isNull())));
    }

    // ── writing fixtures ────────────────────────────────────────────────

    <T> void setDoc(String tenant, String tumbler, Field<T> field, T value) {
        scope.withTenant(tenant, ctx -> ctx.update(CATALOG_DOCUMENTS).set(field, value)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler))).execute());
    }

    /** Set one column on every document of the tenant. */
    <T> void setDocs(String tenant, Field<T> field, T value) {
        scope.withTenant(tenant, ctx -> ctx.update(CATALOG_DOCUMENTS).set(field, value)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).execute());
    }

    /** Tombstone a document directly, optionally flagging it sensitive first. */
    void tombstone(String tenant, String tumbler, boolean sensitive) {
        scope.withTenant(tenant, ctx -> ctx.update(CATALOG_DOCUMENTS)
            .set(CATALOG_DOCUMENTS.SENSITIVE, sensitive).set(CATALOG_DOCUMENTS.DELETED_AT, java.time.OffsetDateTime.now())
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler))).execute());
    }

    /** Insert a document row directly (title and file_path set; {@code more} adds or overrides columns). */
    void insertDoc(String tenant, String tumbler, String filePath, Map<Field<?>, Object> more) {
        Map<Field<?>, Object> values = new LinkedHashMap<>();
        values.put(CATALOG_DOCUMENTS.TENANT_ID, tenant);
        values.put(CATALOG_DOCUMENTS.TUMBLER, tumbler);
        values.put(CATALOG_DOCUMENTS.TITLE, filePath);
        values.put(CATALOG_DOCUMENTS.FILE_PATH, filePath);
        values.putAll(more);
        scope.withTenant(tenant, ctx -> ctx.insertInto(CATALOG_DOCUMENTS).set(values).execute());
    }

    <T> void setOwner(String tenant, String prefix, Field<T> field, T value) {
        scope.withTenant(tenant, ctx -> ctx.update(CATALOG_OWNERS).set(field, value)
            .where(CATALOG_OWNERS.TENANT_ID.eq(tenant).and(CATALOG_OWNERS.TUMBLER_PREFIX.eq(prefix))).execute());
    }

    Record ownerRow(String tenant, String prefix) {
        return scope.withTenant(tenant, ctx -> ctx.select().from(CATALOG_OWNERS)
            .where(CATALOG_OWNERS.TENANT_ID.eq(tenant).and(CATALOG_OWNERS.TUMBLER_PREFIX.eq(prefix))).fetchOne());
    }

    long nextSeq(String tenant, String prefix) {
        return ownerRow(tenant, prefix).get(CATALOG_OWNERS.NEXT_SEQ);
    }

    void link(String tenant, String from, String to, String type, String by, String fromSpan, String toSpan) {
        scope.withTenant(tenant, ctx -> ctx.insertInto(CATALOG_LINKS)
            .set(CATALOG_LINKS.TENANT_ID, tenant).set(CATALOG_LINKS.FROM_TUMBLER, from).set(CATALOG_LINKS.TO_TUMBLER, to)
            .set(CATALOG_LINKS.LINK_TYPE, type).set(CATALOG_LINKS.CREATED_BY, by)
            .set(CATALOG_LINKS.FROM_SPAN, fromSpan).set(CATALOG_LINKS.TO_SPAN, toSpan)
            .set(CATALOG_LINKS.METADATA, JSONB.valueOf("{\"note\":\"x\"}")).execute());
    }

    /** Every link of the tenant as {@code from>to:type:created_by:from_span:to_span:metadata}, sorted. */
    List<String> links(String tenant) {
        return scope.withTenant(tenant, ctx -> ctx.select().from(CATALOG_LINKS)
            .where(CATALOG_LINKS.TENANT_ID.eq(tenant)).fetch().map(r ->
                r.get(CATALOG_LINKS.FROM_TUMBLER) + ">" + r.get(CATALOG_LINKS.TO_TUMBLER) + ":" + r.get(CATALOG_LINKS.LINK_TYPE)
                    + ":" + r.get(CATALOG_LINKS.CREATED_BY) + ":" + nz(r.get(CATALOG_LINKS.FROM_SPAN)) + ":"
                    + nz(r.get(CATALOG_LINKS.TO_SPAN)) + ":" + (r.get(CATALOG_LINKS.METADATA) == null ? "" : r.get(CATALOG_LINKS.METADATA).data())))
            .stream().sorted().toList();
    }

    private static String nz(String v) {
        return v == null ? "" : v;
    }

    /** created_at of the one link (from, to, any type) of the tenant; null when the column is NULL or there is no such link. */
    java.time.OffsetDateTime linkCreatedAt(String tenant, String from, String to) {
        return scope.withTenant(tenant, ctx -> ctx.select(CATALOG_LINKS.CREATED_AT).from(CATALOG_LINKS)
            .where(CATALOG_LINKS.TENANT_ID.eq(tenant).and(CATALOG_LINKS.FROM_TUMBLER.eq(from)).and(CATALOG_LINKS.TO_TUMBLER.eq(to)))
            .fetchOne(CATALOG_LINKS.CREATED_AT));
    }

    void setLinkCreatedAt(String tenant, String from, String to, java.time.OffsetDateTime value) {
        scope.withTenant(tenant, ctx -> ctx.update(CATALOG_LINKS).set(CATALOG_LINKS.CREATED_AT, value)
            .where(CATALOG_LINKS.TENANT_ID.eq(tenant).and(CATALOG_LINKS.FROM_TUMBLER.eq(from)).and(CATALOG_LINKS.TO_TUMBLER.eq(to)))
            .execute());
    }

    /** The audit rows of the tenant as {@code tumbler|old|new|reason|actor}, in write order. */
    List<String> audit(String tenant) {
        return scope.withTenant(tenant, ctx -> ctx.select().from(CATALOG_SENSITIVITY_CHANGES)
            .where(CATALOG_SENSITIVITY_CHANGES.TENANT_ID.eq(tenant)).orderBy(CATALOG_SENSITIVITY_CHANGES.ID).fetch()
            .map(r -> r.get(CATALOG_SENSITIVITY_CHANGES.TUMBLER) + "|" + r.get(CATALOG_SENSITIVITY_CHANGES.OLD) + "|"
                + r.get(CATALOG_SENSITIVITY_CHANGES.NEW) + "|" + r.get(CATALOG_SENSITIVITY_CHANGES.REASON) + "|"
                + r.get(CATALOG_SENSITIVITY_CHANGES.ACTOR)));
    }

    // ── the landing tables ──────────────────────────────────────────────

    int stagedRows(String tenant, String run) {
        return scope.withTenant(tenant, ctx -> ctx.fetchCount(CATALOG_RECONCILE_ROWS,
            CATALOG_RECONCILE_ROWS.TENANT_ID.eq(tenant).and(CATALOG_RECONCILE_ROWS.RUN_ID.eq(run))));
    }

    String runState(String tenant, String run) {
        return scope.withTenant(tenant, ctx -> ctx.select(CATALOG_RECONCILE_RUNS.STATE).from(CATALOG_RECONCILE_RUNS)
            .where(CATALOG_RECONCILE_RUNS.TENANT_ID.eq(tenant).and(CATALOG_RECONCILE_RUNS.RUN_ID.eq(run)))
            .fetchOne(CATALOG_RECONCILE_RUNS.STATE));
    }

    String storedOutcome(String tenant, String run) {
        JSONB out = scope.withTenant(tenant, ctx -> ctx.select(CATALOG_RECONCILE_RUNS.OUTCOME).from(CATALOG_RECONCILE_RUNS)
            .where(CATALOG_RECONCILE_RUNS.TENANT_ID.eq(tenant).and(CATALOG_RECONCILE_RUNS.RUN_ID.eq(run)))
            .fetchOne(CATALOG_RECONCILE_RUNS.OUTCOME));
        return out == null ? null : out.data();
    }

    /** Age a run: backdate its created_at by {@code age} (as the service role, in the run's tenant). */
    void ageRun(String tenant, String run, java.time.Duration age) {
        scope.withTenant(tenant, ctx -> ctx.update(CATALOG_RECONCILE_RUNS)
            .set(CATALOG_RECONCILE_RUNS.CREATED_AT, java.time.OffsetDateTime.now().minus(age))
            .where(CATALOG_RECONCILE_RUNS.TENANT_ID.eq(tenant).and(CATALOG_RECONCILE_RUNS.RUN_ID.eq(run))).execute());
    }

    /** The whole state of the tenant's catalog, for "writes nothing" comparisons. */
    String snapshot(String tenant) {
        return scope.withTenant(tenant, ctx -> {
            var sb = new StringBuilder();
            sb.append(ctx.select().from(CATALOG_DOCUMENTS).where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant))
                .orderBy(CATALOG_DOCUMENTS.TUMBLER).fetch().formatCSV());
            sb.append(ctx.select().from(CATALOG_OWNERS).where(CATALOG_OWNERS.TENANT_ID.eq(tenant))
                .orderBy(CATALOG_OWNERS.TUMBLER_PREFIX).fetch().formatCSV());
            sb.append(ctx.select().from(CATALOG_LINKS).where(CATALOG_LINKS.TENANT_ID.eq(tenant))
                .orderBy(CATALOG_LINKS.ID).fetch().formatCSV());
            sb.append(ctx.select().from(CATALOG_SENSITIVITY_CHANGES).where(CATALOG_SENSITIVITY_CHANGES.TENANT_ID.eq(tenant))
                .orderBy(CATALOG_SENSITIVITY_CHANGES.ID).fetch().formatCSV());
            return sb.toString();
        });
    }

    /** One document row as CSV (every column), for "this row is not written" comparisons. */
    String snapshotOf(String tenant, String tumbler) {
        return scope.withTenant(tenant, ctx -> ctx.select().from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler))).fetch().formatCSV());
    }

    DSLContext suCtx(Connection su) {
        return DSL.using(su, SQLDialect.POSTGRES);
    }

    /**
     * Waits until {@code n} backends of this database are blocked on a lock; {@code commits} are the calls meant to be
     * queued, and the wait fails at once, with the lock state, if one of them has finished. The wait itself is what is
     * counted, not a backend's query text, and each read drops the held-lock connection's cached backend-status
     * snapshot ({@link PgLockWait}).
     */
    void awaitCommitsBlocked(Connection su, int n, java.util.concurrent.Future<?>... commits) throws Exception {
        PgLockWait.awaitBlocked(su, n, commits);
    }
}
