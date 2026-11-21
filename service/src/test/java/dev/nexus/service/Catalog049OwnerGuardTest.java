// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_OWNERS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import org.assertj.core.api.SoftAssertions;

import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
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
import java.sql.PreparedStatement;
import java.sql.SQLException;
import java.sql.SQLWarning;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;

/**
 * RDR-229 (bead nexus-p2m1x.14), catalog-049-2: catalog-044's two owner-from-documents triggers skip the rewrite of a
 * replaceable (slug, dotted, '') owner when the owner the documents name would land the collection on a
 * (tenant_id, content_type, owner_id, embedding_model, model_version) tuple that another live, unsuperseded collection
 * already holds. The old owner stays and a NOTICE names both collections and the owner not taken.
 *
 * <p>State (a), no unique index on the tuple: without the guard the rewrite creates a silent duplicate. State (b), with
 * the partial UNIQUE index RDR-229 plans for P3.8 (created here by the test): without the guard the rewrite raises a bare
 * 23505 from inside document registration. Both states keep the slug owner with the guard.
 *
 * <p>Falsifier, by hand: restore the catalog-049 file to its catalog-049-1-only form and (a) reads a duplicate tuple,
 * (b) throws 23505.
 */
class Catalog049OwnerGuardTest {

    private static final String MASTER_CHANGELOG = "db/changelog/db.changelog-master.xml";
    private static final String TENANT = "catalog049-guard-tenant";
    private static final String OTHER_TENANT = "catalog049-guard-other";
    private static final String DOCS_MODEL = "voyage-context-3";

    private static final String HOLDER = "docs__held-cafe0001__voyage-context-3__v1";
    private static final String SLUG_DOCS = "docs__legacy-ab12cd34__voyage-context-3__v1";
    private static final String SLUG_DOCS_V2 = "docs__legacy-ab12cd34__voyage-context-3__v2";
    private static final String SLUG_LATE = "docs__late-ef56ab78__voyage-context-3__v1";
    private static final String SLUG_FREE_CODE = "code__free-feed0002__voyage-code-3__v1";
    private static final String SUPERSEDED_HOLDER = "code__gone-0badf00d__voyage-code-3__v1";
    private static final String SLUG_OVER_SUPERSEDED = "code__over-5ca1ab1e__voyage-code-3__v1";

    private static final String UNIQUE_INDEX = "CREATE UNIQUE INDEX catalog_collections_live_tuple_uq "
        + "ON nexus.catalog_collections (tenant_id, content_type, owner_id, embedding_model, model_version) "
        + "WHERE lifecycle_state = 'live' AND superseded_by = ''";

    @FunctionalInterface
    private interface Body {
        void run(Connection su, DSLContext ctx) throws Exception;
    }

    @Test
    void withoutTheIndex_aSlugOwnerStaysWhenTheRewriteWouldDuplicateALiveTuple() throws Exception {
        withMigratedDatabase((su, ctx) -> {
            seed(ctx);

            // (1) AFTER trigger: a document lands in a slug-owned collection whose documents name an owner a live
            // collection of the same model and version already holds.
            List<String> notices = insertDocument(su, "1.33.1", SLUG_DOCS);
            assertThat(ownerOf(ctx, SLUG_DOCS)).as("the slug owner stays").isEqualTo("legacy-ab12cd34");
            assertThat(notices).as("a notice names both collections and the owner not taken")
                .anySatisfy(n -> assertThat(n).contains("catalog-049").contains(SLUG_DOCS).contains(HOLDER).contains("1-33"));
            assertThat(liveTupleCount(ctx, "docs", "1-33", DOCS_MODEL, "v1")).as("no duplicate tuple").isEqualTo(1);

            // (2) BEFORE trigger on INSERT: the documents exist first, then the collection registers
            insertDocument(su, "1.33.2", SLUG_LATE);
            List<String> beforeNotices = registerCollectionOn(su, SLUG_LATE, "docs", "late-ef56ab78", DOCS_MODEL, "v1");
            assertThat(ownerOf(ctx, SLUG_LATE)).as("a registration over existing documents keeps the slug")
                .isEqualTo("late-ef56ab78");
            assertThat(beforeNotices).as("the BEFORE-trigger notice names both collections and the owner not taken")
                .anySatisfy(n -> assertThat(n).contains("catalog-049").contains(SLUG_LATE).contains(HOLDER).contains("1-33"));
            assertThat(liveTupleCount(ctx, "docs", "1-33", DOCS_MODEL, "v1")).isEqualTo(1);

            // controls: a free tuple is still rewritten
            insertDocument(su, "1.33.3", SLUG_DOCS_V2);
            assertThat(ownerOf(ctx, SLUG_DOCS_V2)).as("same owner, different model version: free tuple, rewritten")
                .isEqualTo("1-33");
            List<String> free = insertDocument(su, "1.34.1", SLUG_FREE_CODE);
            assertThat(ownerOf(ctx, SLUG_FREE_CODE)).as("a free tuple is rewritten from the documents").isEqualTo("1-34");
            assertThat(free).anySatisfy(n -> assertThat(n).contains("catalog-044").contains("1-34"));

            // a superseded holder does not hold the tuple
            insertDocument(su, "1.35.1", SLUG_OVER_SUPERSEDED);
            assertThat(ownerOf(ctx, SLUG_OVER_SUPERSEDED)).as("a superseded row does not block the rewrite")
                .isEqualTo("1-35");
        });
    }

    @Test
    void withTheIndex_theSameRegistrationKeepsTheSlugOwnerAndRaisesNo23505() throws Exception {
        withMigratedDatabase((su, ctx) -> {
            seed(ctx);
            try (var st = su.createStatement()) {
                st.execute(UNIQUE_INDEX);
            }
            // non-vacuity: the index is live and does reject a duplicate tuple written directly
            assertThatThrownBy(() -> collection(ctx, "docs__dup-deadbeef__voyage-context-3__v1", "docs", "1-33",
                    DOCS_MODEL, "v1", "live", ""))
                .hasMessageContaining("catalog_collections_live_tuple_uq");

            List<String> notices = insertDocument(su, "1.33.1", SLUG_DOCS);
            assertThat(ownerOf(ctx, SLUG_DOCS)).as("the slug owner stays").isEqualTo("legacy-ab12cd34");
            assertThat(notices).anySatisfy(n -> assertThat(n).contains("catalog-049").contains(SLUG_DOCS).contains(HOLDER)
                .contains("1-33"));
            assertThat(liveTupleCount(ctx, "docs", "1-33", DOCS_MODEL, "v1")).isEqualTo(1);

            insertDocument(su, "1.33.2", SLUG_LATE);
            List<String> beforeNotices = registerCollectionOn(su, SLUG_LATE, "docs", "late-ef56ab78", DOCS_MODEL, "v1");
            assertThat(ownerOf(ctx, SLUG_LATE)).isEqualTo("late-ef56ab78");
            assertThat(beforeNotices).anySatisfy(n -> assertThat(n).contains("catalog-049").contains(SLUG_LATE)
                .contains(HOLDER).contains("1-33"));

            insertDocument(su, "1.34.1", SLUG_FREE_CODE);
            assertThat(ownerOf(ctx, SLUG_FREE_CODE)).as("a free tuple is still rewritten under the index").isEqualTo("1-34");
            insertDocument(su, "1.35.1", SLUG_OVER_SUPERSEDED);
            assertThat(ownerOf(ctx, SLUG_OVER_SUPERSEDED)).as("a superseded holder is outside the index").isEqualTo("1-35");
        });
    }

    /**
     * Each predicate of the guard's holder lookup is pinned by a holder that differs in exactly that one respect (it must NOT
     * hold the tuple, so the slug row is still rewritten), on both trigger paths: the AFTER trigger (the collection is
     * registered, then a document lands) and the BEFORE trigger (the documents exist, then the collection registers).
     * The last case pins the subject row's own predicate: a row that is not live, or is superseded, never counts toward the
     * tuple, so even a live holder does not stop its rewrite.
     */
    @Test
    void eachPredicateOfTheGuardIsPinned_aHolderThatDiffersInOneRespectDoesNotHoldTheTuple() throws Exception {
        withMigratedDatabase((su, ctx) -> {
            // Soft assertions: every case reports, so one run names each predicate that is not pinned.
            SoftAssertions soft = new SoftAssertions();
            String sn = "docs__%s__voyage-context-3__v1";
            // embedding_model
            assertRewrittenOnBothPaths(soft, su, ctx, "a holder of another embedding_model", 60, "live", "",
                o -> collection(ctx, "docs__" + o + "__other-model__v1", "docs", o, "voyage-code-3", "v1", "live", ""));
            // content_type
            assertRewrittenOnBothPaths(soft, su, ctx, "a holder of another content_type", 61, "live", "",
                o -> collection(ctx, "code__" + o + "__voyage-context-3__v1", "code", o, DOCS_MODEL, "v1", "live", ""));
            // tenant (the test connection bypasses RLS, so only the tenant predicate keeps it out)
            assertRewrittenOnBothPaths(soft, su, ctx, "a holder in another tenant", 62, "live", "",
                o -> collection(ctx, OTHER_TENANT, String.format(sn, o), "docs", o, DOCS_MODEL, "v1", "live", ""));
            // model_version
            assertRewrittenOnBothPaths(soft, su, ctx, "a holder of another model_version", 63, "live", "",
                o -> collection(ctx, "docs__" + o + "__voyage-context-3__v2", "docs", o, DOCS_MODEL, "v2", "live", ""));
            // the subject row's own state: a live holder of the tuple, a subject that does not count
            assertRewrittenOnBothPaths(soft, su, ctx, "a dormant subject row", 64, "dormant", "",
                o -> collection(ctx, String.format(sn, o), "docs", o, DOCS_MODEL, "v1", "live", ""));
            assertRewrittenOnBothPaths(soft, su, ctx, "a superseded subject row", 65, "live", "docs__gone__voyage-context-3__v1",
                o -> collection(ctx, String.format(sn, o), "docs", o, DOCS_MODEL, "v1", "live", ""));
            soft.assertAll();
        });
    }

    /**
     * Two writers onto one free tuple, under the partial UNIQUE index. Writer A's document moves slug collection S1 onto
     * 1-70 and stays uncommitted; writer B's document for S2 resolves to the same tuple and must wait for A. When A commits,
     * B sees the holder: no 23505, S1 owns the tuple, S2 keeps its slug and B's NOTICE names both collections. Without the
     * advisory lock B reads no holder, and its UPDATE ends in the unique index's 23505.
     */
    @Test
    void twoWritersOntoOneFreeTuple_theSecondWaitsThenKeepsItsSlug_underTheIndex() throws Exception {
        withMigratedDatabase((su, ctx) -> {
            try (var st = su.createStatement()) {
                st.execute(UNIQUE_INDEX);
            }
            String s1 = "docs__race-aa000001__voyage-context-3__v1";
            String s2 = "docs__race-bb000002__voyage-context-3__v1";
            collection(ctx, s1, "docs", "race-aa000001", DOCS_MODEL, "v1", "live", "");
            collection(ctx, s2, "docs", "race-bb000002", DOCS_MODEL, "v1", "live", "");
            try (Connection a = openConnection(su); Connection b = openConnection(su)) {
                a.setAutoCommit(false);
                b.setAutoCommit(false);
                int bPid;
                try (var st = b.createStatement(); var rs = st.executeQuery("SELECT pg_backend_pid()")) {
                    rs.next();
                    bPid = rs.getInt(1);
                }
                b.commit();
                insertDocumentOn(a, "1.70.1", s1);                     // A: S1 takes 1-70, uncommitted
                CompletableFuture<List<String>> bResult = CompletableFuture.supplyAsync(() -> {
                    try {
                        return insertDocumentOn(b, "1.70.2", s2);       // B: blocks behind A
                    } catch (SQLException e) {
                        throw new IllegalStateException(e);
                    }
                });
                long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(20);
                while (!isWaitingOnALock(su, bPid)) {
                    assertThat(bResult.isDone()).as("B must be waiting for A, not finished").isFalse();
                    assertThat(System.nanoTime()).as("B never started waiting").isLessThan(deadline);
                    Thread.sleep(50);
                }
                a.commit();
                List<String> bNotices = bResult.get(30, TimeUnit.SECONDS);   // a 23505 would surface here
                b.commit();
                assertThat(ownerOf(ctx, s1)).as("the first writer took the tuple").isEqualTo("1-70");
                assertThat(ownerOf(ctx, s2)).as("the second writer keeps its slug").isEqualTo("race-bb000002");
                assertThat(bNotices).anySatisfy(n -> assertThat(n).contains("catalog-049").contains(s2).contains(s1)
                    .contains("1-70"));
            }
        });
    }

    /** Makes the (non-)holder collection for one owner segment such as {@code 1-600}. */
    @FunctionalInterface
    private interface HolderMaker {
        void make(String owner);
    }

    /**
     * A fresh holder per path (the AFTER-trigger case rewrites its subject onto the owner, which would itself hold the tuple
     * against the BEFORE-trigger case), so the two paths use owners {@code 1-<n>0} and {@code 1-<n>1}.
     */
    private static void assertRewrittenOnBothPaths(SoftAssertions soft, Connection su, DSLContext ctx, String what, int n,
                                                   String lifecycle, String supersededBy, HolderMaker holder) throws SQLException {
        String afterOwner = "1-" + n + "0";
        holder.make(afterOwner);
        String after = "docs__a" + n + "-ab12cd34__voyage-context-3__v1";
        collection(ctx, after, "docs", "a" + n + "-ab12cd34", DOCS_MODEL, "v1", lifecycle, supersededBy);
        insertDocument(su, "1." + n + "0.1", after);
        soft.assertThat(ownerOf(ctx, after)).as("%s does not hold the tuple (AFTER trigger)", what).isEqualTo(afterOwner);

        String beforeOwner = "1-" + n + "1";
        holder.make(beforeOwner);
        String before = "docs__b" + n + "-cd34ef56__voyage-context-3__v1";
        insertDocument(su, "1." + n + "1.1", before);
        collection(ctx, before, "docs", "b" + n + "-cd34ef56", DOCS_MODEL, "v1", lifecycle, supersededBy);
        soft.assertThat(ownerOf(ctx, before)).as("%s does not hold the tuple (BEFORE trigger)", what).isEqualTo(beforeOwner);
    }

    private static boolean isWaitingOnALock(Connection su, int pid) throws SQLException {
        try (PreparedStatement ps = su.prepareStatement(
            "SELECT 1 FROM pg_stat_activity WHERE pid = ? AND wait_event_type = 'Lock'")) {
            ps.setInt(1, pid);
            try (var rs = ps.executeQuery()) {
                return rs.next();
            }
        }
    }

    /** One live holder of (docs, 1-33, model, v1), three slug-owned collections, and a superseded holder of (code, 1-35). */
    private static void seed(DSLContext ctx) {
        owner(ctx, "1.33", "some-repo", "repo");
        owner(ctx, "1.34", "other-repo", "repo");
        owner(ctx, "1.35", "third-repo", "repo");
        collection(ctx, HOLDER, "docs", "1-33", DOCS_MODEL, "v1", "live", "");
        collection(ctx, SLUG_DOCS, "docs", "legacy-ab12cd34", DOCS_MODEL, "v1", "live", "");
        collection(ctx, SLUG_DOCS_V2, "docs", "legacy-ab12cd34", DOCS_MODEL, "v2", "live", "");
        collection(ctx, SLUG_FREE_CODE, "code", "free-feed0002", "voyage-code-3", "v1", "live", "");
        collection(ctx, SUPERSEDED_HOLDER, "code", "1-35", "voyage-code-3", "v1", "live", SLUG_OVER_SUPERSEDED);
        collection(ctx, SLUG_OVER_SUPERSEDED, "code", "over-5ca1ab1e", "voyage-code-3", "v1", "live", "");
    }

    private static PostgreSQLContainer<?> pgInUse;

    private static void withMigratedDatabase(Body body) throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        pgInUse = pg;
        try {
            final String role = "nexus_admin_catalog049g_test";
            final String pass = "nexus_admin_catalog049g_test_pass";
            Hygiene001NotNullMigrationRlsTest.bootstrapAdminRole(pg, role, pass);
            var cfg = new com.zaxxer.hikari.HikariConfig();
            cfg.setJdbcUrl(pg.getJdbcUrl());
            cfg.setUsername(role);
            cfg.setPassword(pass);
            cfg.setMaximumPoolSize(2);
            cfg.setPoolName("nexus-admin-catalog049g-test");
            try (var adminDs = new com.zaxxer.hikari.HikariDataSource(cfg)) {
                try (Connection conn = adminDs.getConnection()) {
                    Database database = DatabaseFactory.getInstance()
                        .findCorrectDatabaseImplementation(new JdbcConnection(conn));
                    try (Liquibase liquibase = new Liquibase(MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                        liquibase.update(new Contexts(), new LabelExpression());
                    }
                }
                try (Connection su = pg.createConnection("")) {
                    su.setAutoCommit(true);
                    body.run(su, DSL.using(su, SQLDialect.POSTGRES));
                }
            }
        } finally {
            pg.stop();
        }
    }

    private static void owner(DSLContext ctx, String prefix, String name, String type) {
        ctx.insertInto(CATALOG_OWNERS, CATALOG_OWNERS.TENANT_ID, CATALOG_OWNERS.TUMBLER_PREFIX, CATALOG_OWNERS.NAME,
                CATALOG_OWNERS.OWNER_TYPE, CATALOG_OWNERS.REPO_ROOT, CATALOG_OWNERS.NEXT_SEQ)
            .values(TENANT, prefix, name, type, "", 0L).execute();
    }

    private static void collection(DSLContext ctx, String name, String contentType, String owner, String model,
                                   String version, String lifecycle, String supersededBy) {
        collection(ctx, TENANT, name, contentType, owner, model, version, lifecycle, supersededBy);
    }

    private static void collection(DSLContext ctx, String tenant, String name, String contentType, String owner,
                                   String model, String version, String lifecycle, String supersededBy) {
        ctx.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME,
                CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID, CATALOG_COLLECTIONS.EMBEDDING_MODEL,
                CATALOG_COLLECTIONS.MODEL_VERSION, CATALOG_COLLECTIONS.LIFECYCLE_STATE, CATALOG_COLLECTIONS.SUPERSEDED_BY)
            .values(tenant, name, contentType, owner, model, version, lifecycle, supersededBy).execute();
    }

    /** Inserts one live document and returns the server NOTICE messages the insert raised. */
    private static List<String> insertDocument(Connection su, String tumbler, String collection) throws SQLException {
        return insertDocumentOn(su, tumbler, collection);
    }

    private static List<String> insertDocumentOn(Connection conn, String tumbler, String collection) throws SQLException {
        conn.clearWarnings();
        List<String> notices = new ArrayList<>();
        try (PreparedStatement ps = conn.prepareStatement(
            "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, physical_collection) VALUES (?, ?, ?, ?)")) {
            ps.setString(1, TENANT);
            ps.setString(2, tumbler);
            ps.setString(3, "doc " + tumbler);
            ps.setString(4, collection);
            ps.executeUpdate();
            for (SQLWarning w = ps.getWarnings(); w != null; w = w.getNextWarning()) {
                notices.add(w.getMessage());
            }
        }
        for (SQLWarning w = conn.getWarnings(); w != null; w = w.getNextWarning()) {
            if (!notices.contains(w.getMessage())) {
                notices.add(w.getMessage());
            }
        }
        return notices;
    }

    /** Registers a live collection through plain JDBC and returns the server NOTICE messages the insert raised. */
    private static List<String> registerCollectionOn(Connection conn, String name, String contentType, String owner,
                                                     String model, String version) throws SQLException {
        List<String> notices = new ArrayList<>();
        try (PreparedStatement ps = conn.prepareStatement(
            "INSERT INTO nexus.catalog_collections (tenant_id, name, content_type, owner_id, embedding_model, model_version, "
                + "lifecycle_state, superseded_by) VALUES (?, ?, ?, ?, ?, ?, 'live', '')")) {
            ps.setString(1, TENANT);
            ps.setString(2, name);
            ps.setString(3, contentType);
            ps.setString(4, owner);
            ps.setString(5, model);
            ps.setString(6, version);
            ps.executeUpdate();
            for (SQLWarning w = ps.getWarnings(); w != null; w = w.getNextWarning()) {
                notices.add(w.getMessage());
            }
        }
        return notices;
    }

    /** A second, independent connection to the same database as {@code su}. */
    private static Connection openConnection(Connection su) throws SQLException {
        return pgInUse.createConnection("");
    }

    private static String ownerOf(DSLContext ctx, String name) {
        String owner = ctx.select(CATALOG_COLLECTIONS.OWNER_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(name))
            .fetchOne(CATALOG_COLLECTIONS.OWNER_ID);
        assertThat(owner).as("row %s exists", name).isNotNull();
        return owner;
    }

    private static int liveTupleCount(DSLContext ctx, String contentType, String owner, String model, String version) {
        return ctx.fetchCount(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)
            .and(CATALOG_COLLECTIONS.CONTENT_TYPE.eq(contentType)).and(CATALOG_COLLECTIONS.OWNER_ID.eq(owner))
            .and(CATALOG_COLLECTIONS.EMBEDDING_MODEL.eq(model)).and(CATALOG_COLLECTIONS.MODEL_VERSION.eq(version))
            .and(CATALOG_COLLECTIONS.LIFECYCLE_STATE.eq("live")).and(CATALOG_COLLECTIONS.SUPERSEDED_BY.eq("")));
    }
}
