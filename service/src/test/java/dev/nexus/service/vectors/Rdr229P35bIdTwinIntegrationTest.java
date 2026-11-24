// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.CollectionIds;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.SchemaMigrator;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.Record;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.EnumSource;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.Random;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.concurrent.atomic.AtomicInteger;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.HYBRID_SEARCH_384;
import static dev.nexus.service.jooq.nexus.Tables.HYBRID_SEARCH_384_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.MANIFEST_SEARCH_384;
import static dev.nexus.service.jooq.nexus.Tables.MANIFEST_SEARCH_384_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.PLAIN_SEARCH_384;
import static dev.nexus.service.jooq.nexus.Tables.PLAIN_SEARCH_384_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_METADATA_SCOPED_384;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_METADATA_SCOPED_384_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_HNSW_FIRST_384;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_HNSW_FIRST_384_BY_ID;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5b (nexus-p2m1x.24, linda 2026-11-22): the id-path twins {@code <name>_by_id} vectors-077 adds.
 *
 * <p><b>Same rows.</b> Over a stamped store (every chunk and manifest row carries its collection id), one twin of each family
 * (plain, hybrid, manifest, scoped, text-gated) returns exactly the rows its name function returns for the same collection,
 * in the same order. Both run on the exact route (index scans off), so neither result is an approximate HNSW walk and the
 * comparison is of the functions, not of two graph walks. Every result is non-empty and the sensitive test is on.
 *
 * <p><b>The id index is provable on the sixth call.</b> A per-collection index keyed on the id
 * ({@code WHERE collection_id = '<id>'::bigint}, the predicate P3.4 rebuilds the indexes with) is a partial index, and
 * PostgreSQL proves one only from a constant: a bound parameter under a generic plan never proves it. A prepared statement
 * and a plpgsql statement may switch to a generic plan from the sixth execution of a session. So each twin runs ten times on
 * ONE session, the way PgVectorRepository runs its name function (jOOQ, bound parameters, the serving settings and
 * {@code plan_cache_mode = force_custom_plan}), and the plans {@code auto_explain} logs for the sixth to tenth calls must walk
 * the id index. The scan alternatives are penalised (enable_seqscan, enable_bitmapscan, enable_sort, enable_hashjoin off), so
 * the planner takes the id index whenever it can prove it and the shared leaf index otherwise: the assertion tells the two
 * apart. hybrid_search ranks with window functions and walks no index; for it the sixth call's plan must carry the id as a
 * constant ({@code '{<id>}'::bigint[]}), which is what proves a partial index.
 *
 * <p>A dedicated container (PgContainerHelper#startDedicated's roster): {@code auto_explain} on {@code nexus_svc} is
 * cluster-wide and the plans are read from the container's own server log.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P35bIdTwinIntegrationTest {

    static final String TENANT = "p35b-twin";
    static final String MODEL = "minilm-l6-v2-384";
    /** Carries a per-collection index on its id. */
    static final String OWN = "knowledge__p35b-twin-own__minilm-l6-v2-384__v1";
    /** The rest of the leaf: the shared leaf index sees both collections. */
    static final String OTHER = "knowledge__p35b-twin-other__minilm-l6-v2-384__v1";
    static final String ID_INDEX = "pci_p35b_twin_own_id";
    /** The second collection's id index, and each collection's name index (the original's per-collection index). */
    static final String ID_INDEX_OTHER = "pci_p35b_twin_other_id";
    static final String NAME_INDEX = "pci_p35b_twin_own_name";
    static final String NAME_INDEX_OTHER = "pci_p35b_twin_other_name";
    static final Set<String> PER_COLLECTION_INDEXES = Set.of(ID_INDEX, ID_INDEX_OTHER, NAME_INDEX, NAME_INDEX_OTHER);
    static final String ADMIN_ROLE = "p35b_twin_admin";
    static final String ADMIN_PASS = "p35b_twin_admin_pass";
    static final int ROWS = 6000;
    static final int CHUNKS_PER_DOC = 5;
    static final int SENSITIVE_EVERY = 10;
    static final int K = 10;
    static final int CALLS = 10;
    /** The first call a session may plan generically: the sixth. */
    static final int FIRST_GENERIC_CANDIDATE = 6;
    static final String WORD = "alpha";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    HikariDataSource explainDs;
    TenantScope scope;
    TenantScope explainScope;
    String leaf;
    long ownId;
    long otherId;
    Vector query;
    final AtomicInteger sentinel = new AtomicInteger();

    enum Family { PLAIN, HYBRID, MANIFEST, SCOPED, TEXT_GATED }

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.startDedicated();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
        }
        var adminCfg = new HikariConfig();
        adminCfg.setJdbcUrl(pg.getJdbcUrl());
        adminCfg.setUsername(ADMIN_ROLE);
        adminCfg.setPassword(ADMIN_PASS);
        adminCfg.setMaximumPoolSize(2);
        try (var adminDs = new HikariDataSource(adminCfg)) {
            SchemaMigrator.migrate(adminDs);
            try (Connection c = adminDs.getConnection()) {
                PgContainerHelper.installTestObjects(c);
            }
        }
        leaf = PciCatalogIntegrationTest.leafName(MODEL, TENANT);
        seed();
        svcDs = svcPool("p35b-twin-svc", 3);
        scope = new TenantScope(svcDs);
        // After the seed: from here nexus_svc's NEW sessions log every plan. One connection, so every call is one session.
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.enableAutoExplainForService(su);
        }
        explainDs = svcPool("p35b-twin-explain", 1);
        explainScope = new TenantScope(explainDs);
    }

    private HikariDataSource svcPool(String name, int size) {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(size);
        cfg.setPoolName(name);
        cfg.setAutoCommit(true);
        return new HikariDataSource(cfg);
    }

    @AfterAll
    void stopAll() {
        if (explainDs != null) explainDs.close();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // -- fixture -----------------------------------------------------------------------------------------

    private static float[] gaussianUnit(Random r, int dim) {
        double[] g = new double[dim];
        double n = 0;
        for (int i = 0; i < dim; i++) {
            g[i] = r.nextGaussian();
            n += g[i] * g[i];
        }
        n = Math.sqrt(n);
        float[] f = new float[dim];
        for (int i = 0; i < dim; i++) f[i] = (float) (g[i] / n);
        return f;
    }

    private static byte[] sha256(String s) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(s.getBytes(StandardCharsets.UTF_8));
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    private void seed() throws Exception {
        Random rnd = new Random(20261122L);
        query = Vector.of(gaussianUnit(rnd, 384));
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (String c : List.of(OWN, OTHER)) {
                PgContainerHelper.insertCollection(ctx, TENANT, c);
                seedCollection(ctx, c, rnd);
            }
            ownId = CollectionIds.require(ctx, TENANT, OWN);
            otherId = CollectionIds.require(ctx, TENANT, OTHER);
            // Serial build (nexus-zpd9o): a parallel HNSW build does not fit a container's default /dev/shm.
            ctx.set(DSL.name("max_parallel_maintenance_workers"), DSL.inline(0)).execute();
            PgContainerHelper.createLeafHnswIndexOnId(su, "nexus", leaf, ID_INDEX, "embedding_384", ownId);
            PgContainerHelper.createLeafHnswIndexOnId(su, "nexus", leaf, ID_INDEX_OTHER, "embedding_384", otherId);
            PgContainerHelper.createLeafHnswIndex(su, "nexus", leaf, NAME_INDEX, "embedding_384", OWN);
            PgContainerHelper.createLeafHnswIndex(su, "nexus", leaf, NAME_INDEX_OTHER, "embedding_384", OTHER);
            PgContainerHelper.analyzeTable(su, DSL.table(DSL.name("nexus", leaf)));
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENT_CHUNKS);
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENTS);
        }
    }

    /** ROWS chunks, every one stamped with its collection id, in documents of CHUNKS_PER_DOC; every tenth document sensitive. */
    private void seedCollection(DSLContext ctx, String collection, Random rnd) {
        byte[][] chash = new byte[ROWS][];
        final Long collectionId = CollectionIds.require(ctx, TENANT, collection);
        for (int from = 0; from < ROWS; from += 250) {
            int to = Math.min(ROWS, from + 250);
            var step = ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.COLLECTION_ID, CHUNKS.CHASH,
                CHUNKS.EMBEDDING_MODEL, CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_384);
            for (int i = from; i < to; i++) {
                chash[i] = sha256(collection + "|" + i);
                step = step.values(TENANT, collection, collectionId, chash[i], MODEL,
                    (i % 3 == 0 ? WORD + " " : "") + "bravo t" + i, Vector.of(gaussianUnit(rnd, 384)));
            }
            step.execute();
        }
        int docs = ROWS / CHUNKS_PER_DOC;
        for (int d = 0; d < docs; d++) {
            ctx.insertInto(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT).set(CATALOG_DOCUMENTS.TUMBLER, doc(collection, d))
                .set(CATALOG_DOCUMENTS.TITLE, "doc " + d).set(CATALOG_DOCUMENTS.CONTENT_TYPE, "paper")
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, collection)
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, collectionId)
                .set(CATALOG_DOCUMENTS.SENSITIVE, d % SENSITIVE_EVERY == 0)
                .execute();
        }
        for (int from = 0; from < ROWS; from += 250) {
            int to = Math.min(ROWS, from + 250);
            var step = ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID,
                CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                CATALOG_DOCUMENT_CHUNKS.COLLECTION, CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID,
                CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL);
            org.jooq.InsertValuesStep7<?, String, String, Integer, byte[], String, Long, String> values = null;
            for (int i = from; i < to; i++) {
                values = (values == null ? step : values)
                    .values(TENANT, doc(collection, i / CHUNKS_PER_DOC), i % CHUNKS_PER_DOC, chash[i], collection,
                            collectionId, MODEL);
            }
            values.execute();
        }
    }

    private static String doc(String collection, int d) {
        return (collection.equals(OWN) ? "own" : "oth") + "-" + d;
    }

    // -- the calls ---------------------------------------------------------------------------------------

    /** The name function of {@code family} over OWN, as PgVectorRepository calls it. */
    private Table<?> byName(Family family) {
        String[] colls = {OWN};
        return switch (family) {
            case PLAIN -> PLAIN_SEARCH_384.call(query, colls, null, null, K, MODEL, TENANT, true);
            case HYBRID -> HYBRID_SEARCH_384.call(query, WORD, colls, null, K, true);
            case MANIFEST -> MANIFEST_SEARCH_384.call(query, null, null, colls, null, null, null, null, K, MODEL, TENANT, true);
            case SCOPED -> SEARCH_METADATA_SCOPED_384.call(query, colls, null, null, null, null, null, null, null, null, K,
                MODEL, TENANT, true);
            case TEXT_GATED -> TEXT_GATED_SEARCH_HNSW_FIRST_384.call(query, WORD, colls, null, null, K, MODEL, TENANT, true);
        };
    }

    /** The twin of {@code family} over OWN's id. */
    private Table<?> byId(Family family) {
        Long[] ids = {ownId};
        return switch (family) {
            case PLAIN -> PLAIN_SEARCH_384_BY_ID.call(query, ids, null, null, K, MODEL, TENANT, true);
            case HYBRID -> HYBRID_SEARCH_384_BY_ID.call(query, WORD, ids, null, K, true);
            case MANIFEST -> MANIFEST_SEARCH_384_BY_ID.call(query, null, null, ids, null, null, null, null, K, MODEL, TENANT,
                true);
            case SCOPED -> SEARCH_METADATA_SCOPED_384_BY_ID.call(query, ids, null, null, null, null, null, null, null, null, K,
                MODEL, TENANT, true);
            case TEXT_GATED -> TEXT_GATED_SEARCH_HNSW_FIRST_384_BY_ID.call(query, WORD, ids, null, null, K, MODEL, TENANT,
                true);
        };
    }

    private static String twinName(Family family) {
        return switch (family) {
            case PLAIN -> "plain_search_384_by_id";
            case HYBRID -> "hybrid_search_384_by_id";
            case MANIFEST -> "manifest_search_384_by_id";
            case SCOPED -> "search_metadata_scoped_384_by_id";
            case TEXT_GATED -> "text_gated_search_hnsw_first_384_by_id";
        };
    }

    /** Every row of {@code fn} on the exact route (index scans off), each row as its values' text. */
    private List<String> exactRows(Table<?> fn) {
        return scope.withTenant(TENANT, ctx -> {
            PgSession.disableIndexScanForExactFallback(ctx);
            PgSession.setSearchPlanCacheMode(ctx);
            List<String> out = new ArrayList<>();
            for (Record r : ctx.selectFrom(fn).fetch()) {
                out.add(r.intoList().toString());
            }
            return out;
        });
    }

    @ParameterizedTest(name = "{0}: the twin returns the name function's rows")
    @EnumSource(Family.class)
    void theTwinReturnsTheNameFunctionsRows_overAStampedStore(Family family) {
        List<String> named = exactRows(byName(family));
        List<String> twin = exactRows(byId(family));
        assertThat(named).as("%s: non-vacuity, the name function finds rows", family).isNotEmpty();
        assertThat(twin).as("%s: the twin's rows, in order, are the name function's", family).containsExactlyElementsOf(named);
    }

    // -- the sixth call's plan ---------------------------------------------------------------------------

    /** One call as the serving path makes it: the serving settings, a custom plan per execution, the scan alternatives penalised. */
    private int servingCall(Table<?> fn, boolean penalise) {
        return explainScope.withTenant(TENANT, ctx -> {
            PgSession.setLocal(ctx, "hnsw.iterative_scan", "relaxed_order");
            PgSession.setLocal(ctx, "hnsw.ef_search", "100");
            PgSession.setHnswScanBudget(ctx);
            PgSession.setSearchPlanCacheMode(ctx);
            if (penalise) {
                for (String guc : List.of("enable_seqscan", "enable_bitmapscan", "enable_sort", "enable_hashjoin")) {
                    PgSession.setLocal(ctx, guc, "off");
                }
            }
            return ctx.selectFrom(fn).fetch().size();
        });
    }

    /** The auto_explain entries of the server log, from {@code from} to a sentinel run after the calls, that name {@code fn}. */
    private List<String> loggedPlans(int from, String fn) throws Exception {
        String mark = "p35b-twin-sentinel-" + sentinel.incrementAndGet();
        explainScope.withTenant(TENANT, ctx -> ctx.fetch(DSL.select(DSL.inline(mark))).size());
        String delta = null;
        for (int attempt = 0; attempt < 200 && delta == null; attempt++) {
            String logs = PgContainerHelper.serverLog(pg);
            int at = logs.indexOf(mark, from);
            if (at >= 0) {
                delta = logs.substring(from, at);
            } else {
                Thread.sleep(100);
            }
        }
        assertThat(delta).as("the log shows the sentinel after the calls").isNotNull();
        List<String> out = new ArrayList<>();
        for (String entry : delta.split("LOG:  duration:")) {
            if (entry.contains(fn)) {
                out.add(entry);
            }
        }
        return out;
    }

    @ParameterizedTest(name = "{0}: the sixth call of a session proves the id index")
    @EnumSource(Family.class)
    void theSixthCallOfASession_provesThePerCollectionIdIndex(Family family) throws Exception {
        boolean hybrid = family == Family.HYBRID;
        int from = PgContainerHelper.serverLog(pg).length();
        for (int i = 0; i < CALLS; i++) {
            assertThat(servingCall(byId(family), !hybrid)).as("%s: call %d finds rows", family, i + 1).isPositive();
        }
        List<String> plans = loggedPlans(from, twinName(family));
        assertThat(plans).as("%s: auto_explain logged a plan for each of the %d calls", family, CALLS).hasSize(CALLS);
        String constant = "'{" + ownId + "}'::bigint[]";
        for (int call = FIRST_GENERIC_CANDIDATE; call <= CALLS; call++) {
            String plan = plans.get(call - 1);
            if (hybrid) {
                assertThat(plan).as("%s: call %d is planned with the id as a constant%n%s", family, call, plan)
                    .contains(constant);
            } else {
                assertThat(plan).as("%s: call %d walks the per-collection id index%n%s", family, call, plan)
                    .contains("using " + ID_INDEX + " on " + leaf);
            }
        }
    }

    // -- several collections in one call --------------------------------------------------------------

    /**
     * The chunks-leaf access path of a logged plan, as a kind: the per-collection index (any of the four partial ones), the
     * leaf's own unpredicated index, or the node type of a non-index read. A partial index on one collection's id or name
     * is implied only by a predicate that names that one collection (the kinds rank per-collection index, then the
     * leaf's unpredicated vector index, then a btree index of the leaf, then the node type of a read without an index): under a custom plan {@code collection_id = ANY('{a,b}')}
     * becomes {@code a OR b} for the proof and implies neither, and so does {@code collection = ANY('{x,y}')}.
     */
    static String accessKind(String plan, String leafName) {
        Matcher m = Pattern.compile("(Index Only Scan|Index Scan|Bitmap Heap Scan|Bitmap Index Scan|Seq Scan)(?: Backward)?"
            + "(?: using (\\S+))? on " + Pattern.quote(leafName) + "\\b").matcher(plan);
        List<String> rank = List.of("per-collection index", "leaf vector index", "leaf btree index");
        String kind = null;
        while (m.find()) {
            String index = m.group(2);
            String k = index == null ? m.group(1)
                : PER_COLLECTION_INDEXES.contains(index) ? "per-collection index"
                : index.contains("embedding") ? "leaf vector index" : "leaf btree index";
            int r = rank.indexOf(k);
            if (kind == null || (r >= 0 && (rank.indexOf(kind) < 0 || r < rank.indexOf(kind)))) kind = k;
        }
        return kind == null ? "no read of " + leafName : kind;
    }

    private Table<?> byNames(Family family, String[] colls) {
        return switch (family) {
            case PLAIN -> PLAIN_SEARCH_384.call(query, colls, null, null, K, MODEL, TENANT, true);
            case SCOPED -> SEARCH_METADATA_SCOPED_384.call(query, colls, null, null, null, null, null, null, null, null, K,
                MODEL, TENANT, true);
            default -> throw new IllegalArgumentException(family.name());
        };
    }

    private Table<?> byIds(Family family, Long[] ids) {
        return switch (family) {
            case PLAIN -> PLAIN_SEARCH_384_BY_ID.call(query, ids, null, null, K, MODEL, TENANT, true);
            case SCOPED -> SEARCH_METADATA_SCOPED_384_BY_ID.call(query, ids, null, null, null, null, null, null, null, null, K,
                MODEL, TENANT, true);
            default -> throw new IllegalArgumentException(family.name());
        };
    }

    private static String originalName(Family family) {
        return twinName(family).replace("_by_id", "");
    }

    /** The access kinds of calls 6..10 of one session of {@code fn}, read from auto_explain. */
    private List<String> sessionKinds(Table<?> fn, String logged) throws Exception {
        int from = PgContainerHelper.serverLog(pg).length();
        for (int i = 0; i < CALLS; i++) {
            assertThat(servingCall(fn, true)).as("%s: call %d finds rows", logged, i + 1).isPositive();
        }
        List<String> plans = new ArrayList<>();
        for (String p : loggedPlans(from, logged)) {
            if (!logged.endsWith("_by_id") && p.contains(logged + "_by_id")) continue;
            plans.add(p);
        }
        assertThat(plans).as("%s: auto_explain logged a plan for each of the %d calls", logged, CALLS).hasSize(CALLS);
        List<String> kinds = new ArrayList<>();
        for (int call = FIRST_GENERIC_CANDIDATE; call <= CALLS; call++) {
            kinds.add(accessKind(plans.get(call - 1), leaf));
        }
        System.out.printf("p35b-twin %s, calls %d..%d: %s%n%s%n", logged, FIRST_GENERIC_CANDIDATE, CALLS, kinds,
            plans.get(CALLS - 1));
        return kinds;
    }

    @ParameterizedTest(name = "{0}: two ids take the same kind of access path as two names")
    @EnumSource(value = Family.class, names = {"PLAIN", "SCOPED"})
    void twoCollectionsInOneCall_theTwinsAccessPathIsTheOriginalsKind(Family family) throws Exception {
        List<String> named = sessionKinds(byNames(family, new String[] {OWN, OTHER}), originalName(family));
        List<String> twin = sessionKinds(byIds(family, new Long[] {ownId, otherId}), twinName(family));
        assertThat(twin).as("%s: calls %d..%d of a session, two ids vs two names", family, FIRST_GENERIC_CANDIDATE, CALLS)
            .isEqualTo(named);
        assertThat(named).as("%s: non-vacuity, the plans name a read of the chunks leaf", family)
            .noneMatch(k -> k.startsWith("no read"));
    }

    @org.junit.jupiter.api.Test
    void accessKind_tellsThePerCollectionIndexFromTheLeafIndexAndAScan() {
        String l = "chunks_m1_t_2";
        assertThat(accessKind("->  Index Scan using " + ID_INDEX + " on " + l + " c  (cost=1)", l)).isEqualTo("per-collection index");
        assertThat(accessKind("->  Index Scan using " + l + "_embedding_384_idx on " + l + " c  (cost=1)", l)).isEqualTo("leaf vector index");
        assertThat(accessKind("->  Bitmap Index Scan on " + l + "_cid_uq  (cost=1)\n->  Index Scan using " + l
            + "_embedding_384_idx on " + l + " c", l)).isEqualTo("leaf vector index");
        assertThat(accessKind("->  Index Scan using " + l + "_cid_uq on " + l + " c  (cost=1)", l)).isEqualTo("leaf btree index");
        assertThat(accessKind("->  Seq Scan on " + l + " c  (cost=1)", l)).isEqualTo("Seq Scan");
        assertThat(accessKind("->  Seq Scan on other c", l)).startsWith("no read");
    }
}
