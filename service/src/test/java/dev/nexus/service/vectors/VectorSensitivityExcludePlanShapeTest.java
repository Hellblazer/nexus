// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.VectorSeeds;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.QueryPart;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.EnumSource;
import org.junit.jupiter.params.provider.MethodSource;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.Random;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.MANIFEST_SEARCH_384;
import static dev.nexus.service.jooq.nexus.Tables.PLAIN_SEARCH_384;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_METADATA_SCOPED_384;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-243 Phase 2 plan evidence (nexus-rfvyy.7.16; review findings S2 and M4): what the planner does with the inline
 * {@code NOT EXISTS} that {@code sensitivity=exclude} puts in the 31 search functions (vectors-055). The A3 spike
 * measured one shared HNSW index, one tenant, no RLS, at most 50 percent sensitive; this class adds the plan under the
 * serving session settings, as {@code nexus_svc} under RLS, for {@code plain_search}, {@code manifest_search} and
 * {@code search_metadata_scoped}, on a collection that walks the shared leaf index and on a collection with its own
 * per-collection (PCI) index, plus a folding check of {@code exclude = false} over every function.
 *
 * <p><b>What a pass means (exclude = true).</b> In the main plan the only scan of a chunks leaf is one HNSW index scan
 * carrying an {@code Order By} on the distance, and no plain Sort sits above it. The sensitive test is an anti join
 * ABOVE that scan (the NOT EXISTS pulled up, vectors-055 as amended by nexus-rfvyy.7.16): the HNSW scan is its outer
 * side and its inner side probes the manifest by an index on {@code chash} (an Index Scan on
 * {@code catalog_document_chunks sm}), once per candidate row. No Seq Scan or Bitmap scan reads the manifest for that
 * test, so the cost does not grow with the tenant's manifest size. Without the anti join the main plan's node
 * skeleton equals the {@code exclude = false} plan's: the predicate adds a probe and nothing else.
 *
 * <p><b>What this replaces.</b> The first form of the predicate, {@code NOT p_exclude_sensitive OR NOT EXISTS (...)},
 * kept the SubLink inside an OR, so the planner could not pull it up and built a HASHED SubPlan: a Seq Scan of the
 * tenant's whole manifest joined to the sensitive documents, once per statement (+46 ms at 128k manifest rows, linear
 * in manifest size; the T2 record nexus_rdr/243-plan-evidence-2026-11-06). The flag now sits inside the sublink, which
 * makes the sublink unconditional, so it is an anti join; with the flag false the inner qual is a constant false and the
 * test folds away (the folding checks below cover all 31 functions: the 30 family-by-dimension calls and
 * manifest_qualifying_hits).
 *
 * <p><b>The metadata family is read under reachability.</b> At 6000 rows per collection the natural plan of
 * {@code search_metadata_scoped} is a Seq or Bitmap scan of the chunks leaf joined to the manifest, with or without
 * the predicate: the join leaves no selective filter and the HNSW start-up cost exceeds a 6000-row scan. So that
 * family is explained with the alternatives to the ordered HNSW scan penalised (the technique of
 * PgVectorRepositoryRawSqlPlanShapeTest#explainIndexReachable, nexus-h9zkm): the pin is that the predicate does not
 * make the index unreachable. Plain and manifest search are read with the natural planner, no {@code enable_*} setting.
 *
 * <p><b>Scope.</b> 6000 chunks per collection, 10 percent sensitive documents, one dimension (384) for the plan
 * assertions; the folding checks cover all 31 functions. Custom plans only (the serving
 * {@code plan_cache_mode}). Latency is not measured here.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class VectorSensitivityExcludePlanShapeTest {

    static final String TENANT = "rfvyy716-plan";
    static final String MODEL = "minilm-l6-v2-384";
    /** Walks the shared leaf index (no per-collection index). */
    static final String SHARED = "knowledge__rfvyy716-shared__minilm-l6-v2-384__v1";
    /** Has its own per-collection partial HNSW index. */
    static final String PCI = "knowledge__rfvyy716-pci__minilm-l6-v2-384__v1";
    static final String PCI_INDEX = "pci_rfvyy716_own";
    /** Empty collections that only give the 768 and 1024 leaves to exist, for the folding check. */
    static final String EMPTY_768 = "knowledge__rfvyy716-e768__bge-base-en-v15-768__v1";
    static final String EMPTY_1024 = "knowledge__rfvyy716-e1024__voyage-context-3__v1";
    /** A small collection the router sends down the exact route (no HNSW); the tenant's manifest is mostly elsewhere. */
    static final String EXACT = "knowledge__rfvyy716-exact__minilm-l6-v2-384__v1";
    /** Holds only manifest rows (no chunks): the rest of the tenant's manifest, which the exact route must not read. */
    static final String BALLAST = "knowledge__rfvyy716-ballast__minilm-l6-v2-384__v1";
    static final int ROWS = 6000;
    static final int EXACT_ROWS = 3000;
    static final int BALLAST_ROWS = 60_000;
    static final int CHUNKS_PER_DOC = 5;
    /** Every tenth document is sensitive. */
    static final int SENSITIVE_EVERY = 10;
    static final int K = 10;
    static final int[] DIMS = {384, 768, 1024};

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope scope;
    String leaf;
    Vector query;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(3);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        scope = new TenantScope(svcDs);
        leaf = PciCatalogIntegrationTest.leafName(MODEL, TENANT);
        seed();
    }

    @AfterAll
    void stopAll() {
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
        Random rnd = new Random(20261106L);
        query = Vector.of(gaussianUnit(rnd, 384));
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT, EMPTY_768);
            PgContainerHelper.insertCollection(ctx, TENANT, EMPTY_1024);
            for (String c : List.of(SHARED, PCI)) {
                PgContainerHelper.insertCollection(ctx, TENANT, c);
                seedCollection(ctx, c, ROWS, rnd);
            }
            PgContainerHelper.insertCollection(ctx, TENANT, EXACT);
            seedCollection(ctx, EXACT, EXACT_ROWS, rnd);
            addManifestBallast(su, TENANT, BALLAST, MODEL, "bal-", BALLAST_ROWS, SENSITIVE_EVERY);
            // Serial build (nexus-zpd9o): a parallel HNSW build holds the graph in dynamic shared memory, which
            // does not fit a container's default 64 MB /dev/shm. Session-level, this connection only.
            DSL.using(su, SQLDialect.POSTGRES).set(DSL.name("max_parallel_maintenance_workers"), DSL.inline(0)).execute();
            PgContainerHelper.createLeafHnswIndex(su, "nexus", leaf, PCI_INDEX, "embedding_384", PCI);
            PgContainerHelper.analyzeTable(su, DSL.table(DSL.name("nexus", leaf)));
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENT_CHUNKS);
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENTS);
        }
    }

    private void seedCollection(DSLContext ctx, String collection, int rows, Random rnd) {
        byte[][] chash = new byte[rows][];
        for (int from = 0; from < rows; from += 250) {
            int to = Math.min(rows, from + 250);
            var step = ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH,
                CHUNKS.EMBEDDING_MODEL, CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_384);
            for (int i = from; i < to; i++) {
                chash[i] = sha256(collection + "|" + i);
                step = step.values(TENANT, collection, chash[i], MODEL, "t" + i, Vector.of(gaussianUnit(rnd, 384)));
            }
            step.execute();
        }
        int docs = rows / CHUNKS_PER_DOC;
        for (int d = 0; d < docs; d++) {
            ctx.insertInto(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT).set(CATALOG_DOCUMENTS.TUMBLER, doc(collection, d))
                .set(CATALOG_DOCUMENTS.TITLE, "doc " + d).set(CATALOG_DOCUMENTS.CONTENT_TYPE, "paper")
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, collection)
                .set(CATALOG_DOCUMENTS.SENSITIVE, d % SENSITIVE_EVERY == 0)
                .execute();
        }
        for (int from = 0; from < rows; from += 250) {
            int to = Math.min(rows, from + 250);
            var step = ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID,
                CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                CATALOG_DOCUMENT_CHUNKS.COLLECTION, CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL);
            org.jooq.InsertValuesStep6<?, String, String, Integer, byte[], String, String> values = null;
            for (int i = from; i < to; i++) {
                values = (values == null ? step : values)
                    .values(TENANT, doc(collection, i / CHUNKS_PER_DOC), i % CHUNKS_PER_DOC, chash[i], collection,
                            MODEL);
            }
            values.execute();
        }
    }

    /**
     * {@code rows} manifest rows of {@code tenant} in {@code collection}, in documents of 5, with no chunk behind them,
     * written server side with the foreign keys to {@code nexus.chunks} off for the session
     * ({@code session_replication_role = replica}, so {@code su} must be a superuser connection). What a caller needs
     * from them is the growth of the tenant's {@code catalog_document_chunks}, its indexes and its statistics: a real
     * chunk carries an embedding of 1.5 to 4 KB and a large ballast of them does not fit a test cluster's tmpfs.
     * Documents are {@code <docPrefix><n>}; every {@code sensitiveEvery}th is sensitive (0: none). Analyzes both tables.
     */
    static void addManifestBallast(Connection su, String tenant, String collection, String model, String docPrefix,
                                   int rows, int sensitiveEvery) throws Exception {
        DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
        PgContainerHelper.insertCollection(ctx, tenant, collection);
        int docs = (rows + CHUNKS_PER_DOC - 1) / CHUNKS_PER_DOC;
        ctx.set(DSL.name("session_replication_role"), DSL.inline("replica")).execute();
        try {
            var g = DSL.generateSeries(0, rows - 1).as("g", "i");
            Field<Integer> i = g.field("i", Integer.class);
            Field<byte[]> chash = DSL.function("sha256", SQLDataType.VARBINARY,
                DSL.function("convert_to", SQLDataType.VARBINARY,
                    DSL.inline(docPrefix + "chunk|").concat(i.cast(SQLDataType.VARCHAR)), DSL.inline("UTF8")));
            var d = DSL.generateSeries(0, docs - 1).as("gd", "d");
            Field<Integer> di = d.field("d", Integer.class);
            Field<Boolean> sensitive = sensitiveEvery > 0 ? di.mod(sensitiveEvery).eq(0) : DSL.inline(false);
            ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                    CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.CONTENT_TYPE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION,
                    CATALOG_DOCUMENTS.SENSITIVE)
                .select(ctx.select(DSL.inline(tenant), DSL.inline(docPrefix).concat(di.cast(SQLDataType.VARCHAR)),
                    DSL.inline("doc ").concat(di.cast(SQLDataType.VARCHAR)), DSL.inline("paper"),
                    DSL.inline(collection), sensitive).from(d))
                .execute();
            ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                    CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                    CATALOG_DOCUMENT_CHUNKS.COLLECTION, CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL,
                    CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX)
                .select(ctx.select(DSL.inline(tenant),
                    DSL.inline(docPrefix).concat(DSL.floor(i.div(CHUNKS_PER_DOC)).cast(SQLDataType.INTEGER)
                        .cast(SQLDataType.VARCHAR)),
                    i.mod(CHUNKS_PER_DOC), chash, DSL.inline(collection), DSL.inline(model), i.mod(CHUNKS_PER_DOC))
                    .from(g))
                .execute();
        } finally {
            ctx.set(DSL.name("session_replication_role"), DSL.inline("origin")).execute();
        }
        PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENT_CHUNKS);
        PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENTS);
    }

    private static String doc(String collection, int d) {
        return (collection.contains("shared") ? "s" : collection.contains("exact") ? "e" : "p") + "-" + d;
    }

    // -- the plans ---------------------------------------------------------------------------------------

    enum Fn {
        PLAIN, MANIFEST, METADATA,
        /** The two text-gated functions run only on the exact route (a chash allow-list), so only that test uses them. */
        TEXT_ALLOWED, TEXT_BY_CHASH
    }

    private static final String HNSW_FAMILIES = "PLAIN,MANIFEST,METADATA";

    /** The chashes of every chunk of EXACT (seedCollection derives them as sha256(collection|i)). */
    private static byte[][] exactChashes() {
        byte[][] out = new byte[EXACT_ROWS][];
        for (int i = 0; i < EXACT_ROWS; i++) out[i] = sha256(EXACT + "|" + i);
        return out;
    }

    private Table<?> textGatedCall(boolean byChash, String collection, boolean exclude) {
        Field<?> chashes = DSL.val(exactChashes(), SQLDataType.VARBINARY.array());
        Field<?> m = DSL.val(MODEL);
        Field<?> t = DSL.val(TENANT);
        Field<?> n = DSL.val(K);
        List<Field<?>> a = byChash
            ? List.of(vec(384), chashes, textArray(collection), nullJsonb(), nullText(), n, m, t, DSL.val(exclude))
            : List.of(vec(384), DSL.val("t1"), chashes, textArray(collection), nullJsonb(), nullText(), n, m, t,
                      DSL.val(exclude));
        StringBuilder sql = new StringBuilder(byChash ? "nexus.text_gated_search_by_chash_384(" :
            "nexus.text_gated_search_allowed_384(");
        for (int i = 0; i < a.size(); i++) sql.append(i == 0 ? "{0}" : ", {" + i + "}");
        return DSL.table(sql.append(')').toString(), a.toArray(new QueryPart[0]));
    }

    private Table<?> call(Fn fn, String collection, boolean exclude) {
        String[] colls = {collection};
        return switch (fn) {
            case PLAIN -> PLAIN_SEARCH_384.call(query, colls, null, null, K, MODEL, TENANT, exclude);
            case MANIFEST -> MANIFEST_SEARCH_384.call(query, null, null, colls, null, null, null, null, K, MODEL,
                TENANT, exclude);
            case METADATA -> SEARCH_METADATA_SCOPED_384.call(query, colls, null, null, null, null, null, null, null,
                null, K, MODEL, TENANT, exclude);
            case TEXT_ALLOWED -> textGatedCall(false, collection, exclude);
            case TEXT_BY_CHASH -> textGatedCall(true, collection, exclude);
        };
    }

    /**
     * EXPLAIN under the serving session settings of PgVectorRepository#runPlainSearchStatement (iterative scan, the
     * ef_search the arm sets, the scan budget, a custom plan per execution), as nexus_svc under the tenant's RLS.
     * {@code reachable} adds the penalties of PgVectorRepositoryRawSqlPlanShapeTest#explainIndexReachable.
     */
    private String explain(Table<?> fn, boolean widest, boolean reachable) {
        return scope.withTenant(TENANT, ctx -> {
            PgSession.setLocal(ctx, "hnsw.iterative_scan", "relaxed_order");
            if (widest) {
                PgSession.setLocal(ctx, "hnsw.ef_search", "1000");
            } else {
                PgSession.setHnswEfSearch(ctx, K);
            }
            PgSession.setHnswScanBudget(ctx);
            PgSession.setSearchPlanCacheMode(ctx);
            if (reachable) {
                for (String guc : List.of("enable_seqscan", "enable_bitmapscan", "enable_sort", "enable_hashjoin")) {
                    PgSession.setLocal(ctx, guc, "off");
                }
            }
            return PgContainerHelper.explainPlan(ctx, ctx.select().from(fn));
        });
    }

    private String plan(Fn fn, String collection, boolean exclude) {
        return explain(call(fn, collection, exclude), SHARED.equals(collection), fn == Fn.METADATA);
    }

    // -- plan parsing ------------------------------------------------------------------------------------

    record Node(int indent, String text, List<String> props, String subplan) {
        String type() {
            int c = text.indexOf("  (cost=");
            String head = c >= 0 ? text.substring(0, c) : text;
            int u = head.indexOf(" using ");
            int o = head.indexOf(" on ");
            int cut = u >= 0 ? u : o >= 0 ? o : head.length();
            return head.substring(0, cut);
        }

        boolean mainPlan() {
            return subplan == null;
        }

        boolean scansChunksLeaf() {
            return text.contains(" on chunks_m");
        }

        String indexName() {
            int u = text.indexOf(" using ");
            if (u < 0) return "";
            int o = text.indexOf(" on ", u);
            return text.substring(u + 7, o);
        }
    }

    record SubPlanBlock(String name, List<String> lines) {
        boolean namesSensitive() {
            return lines.stream().anyMatch(l -> l.contains("sensitive"));
        }
    }

    record Parsed(List<Node> nodes, List<SubPlanBlock> blocks, String plan) { }

    private static final Pattern HEADER = Pattern.compile("^(\\s*)((?:SubPlan|InitPlan) \\d+)\\b.*$");

    static Parsed parse(String plan) {
        List<Node> nodes = new ArrayList<>();
        List<SubPlanBlock> blocks = new ArrayList<>();
        // open subplan headers: indent and block, innermost last
        List<int[]> openIndent = new ArrayList<>();
        List<SubPlanBlock> open = new ArrayList<>();
        Node current = null;
        List<String> currentProps = null;
        List<String> currentLines = null;
        for (String line : plan.split("\n")) {
            if (line.isBlank()) continue;
            int lead = line.length() - line.stripLeading().length();
            String stripped = line.strip();
            boolean isNode = stripped.contains("(cost=") && (lead == 0 || stripped.startsWith("->"));
            Matcher h = HEADER.matcher(line);
            boolean isHeader = !isNode && h.matches();
            int indent = isNode ? (stripped.startsWith("->") ? lead : 0) : lead;
            if (isNode || isHeader) {
                while (!open.isEmpty() && openIndent.get(openIndent.size() - 1)[0] >= indent) {
                    openIndent.remove(openIndent.size() - 1);
                    open.remove(open.size() - 1);
                }
            }
            if (isNode) {
                String text = stripped.startsWith("->") ? stripped.substring(2).strip() : stripped;
                String sp = open.isEmpty() ? null : open.get(open.size() - 1).name();
                currentProps = new ArrayList<>();
                current = new Node(indent, text, currentProps, sp);
                nodes.add(current);
                for (SubPlanBlock b : open) b.lines().add(line);
            } else if (isHeader) {
                SubPlanBlock b = new SubPlanBlock(h.group(2), new ArrayList<>());
                blocks.add(b);
                open.add(b);
                openIndent.add(new int[] {indent});
                current = null;
            } else {
                if (current != null && indent > current.indent()) currentProps.add(stripped);
                for (SubPlanBlock b : open) b.lines().add(line);
            }
        }
        return new Parsed(nodes, blocks, plan);
    }

    /** What the plan says about the sensitive test. */
    enum Form {
        /** An anti join above the scan, its inner side probing the manifest per candidate row. */
        ANTI_JOIN,
        /** A SubPlan (hashed or correlated): the first design, which this class now rejects. */
        SUBPLAN,
        NONE
    }

    private static List<SubPlanBlock> sensitiveBlocks(Parsed p) {
        return p.blocks().stream().filter(SubPlanBlock::namesSensitive).toList();
    }

    private static boolean isSensitiveProbe(Node n) {
        return n.text().contains(" on catalog_document_chunks sm ") || n.text().contains(" on catalog_documents sd ");
    }

    /** True when the plan reads the sensitive owners at all: a probe node or a SubPlan that names them. */
    static boolean carriesSensitiveTest(Parsed p) {
        return !sensitiveBlocks(p).isEmpty() || p.nodes().stream().anyMatch(VectorSensitivityExcludePlanShapeTest::isSensitiveProbe);
    }

    static Form form(Parsed p) {
        if (!sensitiveBlocks(p).isEmpty()) return Form.SUBPLAN;
        return carriesSensitiveTest(p) && antiJoinIndex(p) >= 0 ? Form.ANTI_JOIN : Form.NONE;
    }

    /**
     * With the flag false the NOT EXISTS keeps a constant-false inner qual. The planner has already pulled the SubLink
     * up by then, so the plan keeps a Nested Loop Anti Join node with {@code Join Filter: false} whose inner side is a
     * Result with a false One-Time Filter: no relation is read, but the node is there. True when every main-plan anti
     * join is of that kind.
     */
    static boolean antiJoinsAreFoldedToNothing(Parsed p) {
        for (int i = 0; i < p.nodes().size(); i++) {
            Node n = p.nodes().get(i);
            if (n.mainPlan() && n.type().contains("Anti Join") && !n.props().contains("Join Filter: false")) return false;
        }
        return true;
    }

    /** Index of the main-plan node that is the anti join of the sensitive test, or -1. */
    private static int antiJoinIndex(Parsed p) {
        for (int i = 0; i < p.nodes().size(); i++) {
            Node n = p.nodes().get(i);
            if (n.mainPlan() && n.type().contains("Anti Join")) return i;
        }
        return -1;
    }

    /** The node indexes of the anti join's inner (second child) subtree. */
    private static List<Integer> innerSubtree(Parsed p, int a) {
        List<Node> nodes = p.nodes();
        int base = nodes.get(a).indent();
        int childIndent = Integer.MAX_VALUE;
        for (int i = a + 1; i < nodes.size() && nodes.get(i).indent() > base; i++) {
            childIndent = Math.min(childIndent, nodes.get(i).indent());
        }
        List<Integer> inner = new ArrayList<>();
        int seen = 0;
        for (int i = a + 1; i < nodes.size() && nodes.get(i).indent() > base; i++) {
            if (nodes.get(i).indent() == childIndent && nodes.get(i).mainPlan()) seen++;
            if (seen >= 2) inner.add(i);
        }
        return inner;
    }

    /** The main-plan node types in order, with no costs and without the sensitive test's anti join and its probe. */
    static List<String> skeleton(Parsed p) {
        int a = antiJoinIndex(p);
        java.util.Set<Integer> drop = new java.util.HashSet<>();
        if (a >= 0) {
            drop.add(a);
            drop.addAll(innerSubtree(p, a));
        }
        List<String> out = new ArrayList<>();
        for (int i = 0; i < p.nodes().size(); i++) {
            Node n = p.nodes().get(i);
            if (!n.mainPlan() || drop.contains(i)) continue;
            out.add(n.type() + (n.scansChunksLeaf() ? " [" + (n.indexName().contains("pci_") ? "pci" : "leaf") + "]" : ""));
        }
        return out;
    }

    /**
     * The assertions of an exclude = true plan. Returns the failures, empty when the shape holds; the test turns a
     * non-empty list into a failure with the whole plan.
     */
    static List<String> excludeShapeFailures(Parsed p, String expectedIndex, boolean forbidPlainSort) {
        List<String> bad = new ArrayList<>();
        List<Node> chunkScans = p.nodes().stream().filter(Node::mainPlan).filter(Node::scansChunksLeaf).toList();
        if (chunkScans.size() != 1) {
            bad.add("the main plan must scan the chunks leaf exactly once, found " + chunkScans.size());
            return bad;
        }
        Node scan = chunkScans.get(0);
        if (!scan.type().equals("Index Scan") || !scan.indexName().contains(expectedIndex)) {
            bad.add("the chunks scan must be an Index Scan on the HNSW index '" + expectedIndex + "', it is "
                + scan.type() + " " + scan.indexName());
        }
        if (scan.props().stream().noneMatch(l -> l.startsWith("Order By:"))) {
            bad.add("the HNSW scan must carry the distance Order By");
        }
        if (forbidPlainSort && p.nodes().stream().filter(Node::mainPlan).anyMatch(n -> n.type().equals("Sort"))) {
            bad.add("a plain Sort sits in the main plan");
        }
        if (!sensitiveBlocks(p).isEmpty()) {
            bad.add("the sensitive test is a SubPlan; it must be an anti join (the NOT EXISTS pulled up)");
        }
        List<Node> probes = p.nodes().stream().filter(VectorSensitivityExcludePlanShapeTest::isSensitiveProbe).toList();
        if (probes.stream().anyMatch(n -> n.type().contains("Seq Scan") || n.type().contains("Bitmap"))) {
            bad.add("the sensitive test reads the manifest or the documents with a Seq or Bitmap scan; it must probe "
                + "per candidate by an index");
        }
        Node sm = probes.stream().filter(n -> n.text().contains(" on catalog_document_chunks sm ")).findFirst().orElse(null);
        if (sm == null) {
            bad.add("no probe of catalog_document_chunks sm: the sensitive test is missing");
        } else if (!sm.type().equals("Index Scan") || !sm.text().contains("chash")) {
            bad.add("the manifest probe must be an Index Scan on an index over chash, it is " + sm.type() + " "
                + sm.text());
        }
        int a = antiJoinIndex(p);
        if (a < 0) {
            bad.add("the sensitive test must be an anti join in the main plan");
        } else {
            List<Integer> inner = innerSubtree(p, a);
            boolean probeInner = inner.stream().anyMatch(i -> p.nodes().get(i).text().contains(" on catalog_document_chunks sm "));
            if (!probeInner) bad.add("the manifest probe must be the anti join's inner side");
            boolean scanOuter = a + 1 < p.nodes().size() && !inner.contains(p.nodes().indexOf(scan));
            if (!scanOuter) bad.add("the HNSW scan must be the anti join's outer side, not its inner one");
        }
        // plain and manifest search: nothing in the whole plan reads the manifest sequentially. (The metadata family
        // joins the manifest to the chunks in its own right and is read under reachability; its sm probe is checked
        // above.)
        if (forbidPlainSort && p.nodes().stream().anyMatch(n -> n.type().equals("Seq Scan")
                && n.text().contains(" on catalog_document_chunks"))) {
            bad.add("a Seq Scan reads catalog_document_chunks");
        }
        return bad;
    }

    private void assertExcludeShape(Fn fn, String collection, String expectedIndex) {
        String on = plan(fn, collection, true);
        Parsed p = parse(on);
        List<String> bad = excludeShapeFailures(p, expectedIndex, fn != Fn.METADATA);
        assertThat(bad).as("%s on %s, exclude=true, sensitive form %s%n%s", fn, collection, form(p), on).isEmpty();
        assertThat(form(p)).isEqualTo(Form.ANTI_JOIN);
        if (fn != Fn.METADATA) {
            Parsed off = parse(plan(fn, collection, false));
            assertThat(skeleton(p)).as("%s on %s: exclude=true adds a filter, not a node%n--- true%n%s", fn,
                collection, on).isEqualTo(skeleton(off));
        }
    }

    // -- exclude = true: the three families, shared and PCI ----------------------------------------------

    @ParameterizedTest(name = "{0} walks the shared leaf index")
    @EnumSource(value = Fn.class, names = {"PLAIN", "MANIFEST", "METADATA"})
    void excludeTrue_sharedLeafIndex_drivesTheScan_andTheSensitiveTestIsAnAntiJoinAboveIt(Fn fn) {
        assertExcludeShape(fn, SHARED, "embedding_384");
    }

    @ParameterizedTest(name = "{0} walks the per-collection index")
    @EnumSource(value = Fn.class, names = {"PLAIN", "MANIFEST", "METADATA"})
    void excludeTrue_perCollectionIndex_drivesTheScan_andTheSensitiveTestIsAnAntiJoinAboveIt(Fn fn) {
        assertExcludeShape(fn, PCI, PCI_INDEX);
    }

    // -- exclude = true on the exact route: the manifest read is bounded by the searched collection ----------

    /** The scans of {@code catalog_document_chunks sm}, the sensitive test's manifest side, in the main plan. */
    private static List<Node> smScans(Parsed p) {
        return p.nodes().stream().filter(Node::mainPlan)
            .filter(n -> n.text().contains(" on catalog_document_chunks sm ")).toList();
    }

    /**
     * The assertions of an exclude = true plan on the exact route (a collection small enough that the router skips
     * HNSW: index scans off, a Sort ranks the candidates). There the planner has thousands of candidate rows, so it
     * prefers a hash anti join to probing per row, and its build side must not read the tenant's whole manifest: a Seq
     * Scan of {@code catalog_document_chunks} filtered on the tenant alone grows with the tenant's manifest, not with
     * the collection searched (R2 of nexus-rfvyy.7.16). The build side has to be bounded by the searched collection
     * (an index condition on {@code collection}) or probe by {@code chash} per row. Returns the failures.
     */
    static List<String> exactRouteFailures(Parsed p) {
        List<String> bad = new ArrayList<>();
        if (p.nodes().stream().filter(Node::mainPlan).noneMatch(n -> n.type().equals("Sort"))) {
            bad.add("not the exact route: the plan has no Sort over the candidates");
        }
        if (p.nodes().stream().filter(Node::mainPlan).filter(Node::scansChunksLeaf)
                .anyMatch(n -> n.type().equals("Index Scan"))) {
            bad.add("not the exact route: the chunks leaf is read by an Index Scan");
        }
        List<Node> sm = smScans(p);
        if (sm.isEmpty()) {
            bad.add("no scan of catalog_document_chunks sm: the sensitive test is missing");
        }
        for (Node n : sm) {
            if (n.type().equals("Seq Scan")) {
                bad.add("Seq Scan of catalog_document_chunks sm: the sensitive test reads the tenant's whole manifest ("
                    + n.text() + ")");
                continue;
            }
            boolean bounded = n.props().stream().anyMatch(l -> (l.startsWith("Index Cond:") || l.startsWith("Recheck Cond:"))
                && (l.contains("collection") || l.contains("chash")));
            if (!bounded) {
                bad.add("the scan of catalog_document_chunks sm is bounded by neither the collection nor the chash ("
                    + n.text() + " " + n.props() + ")");
            }
        }
        return bad;
    }

    private String explainExact(Table<?> fn) {
        return scope.withTenant(TENANT, ctx -> {
            PgSession.disableIndexScanForExactFallback(ctx);
            PgSession.setSearchPlanCacheMode(ctx);
            return PgContainerHelper.explainPlan(ctx, ctx.select().from(fn));
        });
    }

    @ParameterizedTest(name = "{0} on the exact route")
    @EnumSource(Fn.class)
    void excludeTrue_exactRoute_readsTheSearchedCollectionsManifestNotTheTenants(Fn fn) {
        String on = explainExact(call(fn, EXACT, true));
        Parsed p = parse(on);
        assertThat(exactRouteFailures(p)).as("%s on %s, exclude=true%n%s", fn, EXACT, on).isEmpty();
        assertThat(sensitiveBlocks(p)).as("no SubPlan names the sensitive test%n%s", on).isEmpty();
        // exclude=false still folds the test away on this route
        String off = explainExact(call(fn, EXACT, false));
        assertThat(carriesSensitiveTest(parse(off))).as("exclude=false%n%s", off).isFalse();
    }

    /**
     * The exact route with nested loops and merge joins penalised, so the anti join is a hash anti join whatever the
     * row estimates. The natural plan of search_metadata_scoped and text_gated_search_allowed is a nested loop (their
     * filters estimate a handful of candidate rows), in which the collection restriction changes nothing; this is the
     * plan the same statement takes when the estimate is large, and it is where a missing restriction costs (R2).
     */
    private String explainExactHash(Table<?> fn) {
        return scope.withTenant(TENANT, ctx -> {
            PgSession.disableIndexScanForExactFallback(ctx);
            PgSession.setSearchPlanCacheMode(ctx);
            // set_config(..., true) is SET LOCAL; PgSession.setLocal whitelists only the serving settings
            for (String guc : List.of("enable_nestloop", "enable_mergejoin")) {
                ctx.select(DSL.function("set_config", String.class, DSL.inline(guc), DSL.inline("off"),
                    DSL.inline(true))).fetch();
            }
            return PgContainerHelper.explainPlan(ctx, ctx.select().from(fn));
        });
    }

    @ParameterizedTest(name = "{0} on the exact route, hash anti join forced")
    @EnumSource(Fn.class)
    void excludeTrue_exactRoute_aHashAntiJoinBuildsFromTheSearchedCollection(Fn fn) {
        String on = explainExactHash(call(fn, EXACT, true));
        Parsed p = parse(on);
        assertThat(p.nodes().stream().filter(Node::mainPlan).anyMatch(n -> n.type().startsWith("Hash")
                && n.type().contains("Anti Join")))
            .as("the forced plan is a hash anti join%n%s", on).isTrue();
        assertThat(exactRouteFailures(p)).as("%s on %s, exclude=true, hash anti join forced%n%s", fn, EXACT, on)
            .isEmpty();
    }

    /** The R2 plan: a hash anti join whose build side is a Seq Scan of the tenant's whole manifest. */
    private static final String EXACT_SEQ_BUILD_PLAN = """
        Limit  (cost=1.00..2.00 rows=10 width=102)
          ->  Sort  (cost=1.00..2.00 rows=10 width=102)
                Sort Key: ((c.embedding_384 OPERATOR(nexus.<=>) '[1]'::nexus.vector))
                ->  Hash Anti Join  (cost=1.00..9.00 rows=9000 width=102)
                      Hash Cond: ((c.collection = sm.collection) AND (c.chash = sm.chash))
                      ->  Bitmap Heap Scan on chunks_x c  (cost=1.00..9.00 rows=9000 width=1648)
                            Recheck Cond: (collection = 'k'::text)
                      ->  Hash  (cost=1.00..9.00 rows=12000 width=100)
                            ->  Hash Join  (cost=1.00..9.00 rows=12000 width=100)
                                  Hash Cond: (sm.doc_id = sd.tumbler)
                                  ->  Seq Scan on catalog_document_chunks sm  (cost=0.00..431.00 rows=128000 width=100)
                                        Filter: (tenant_id = 't'::text)
                                  ->  Hash  (cost=71.61..71.61 rows=240 width=20)
                                        ->  Bitmap Heap Scan on catalog_documents sd  (cost=0.00..71.61 rows=240 width=20)
                                              Filter: ((deleted_at IS NULL) AND sensitive)
        """;

    /** The shape the fix produces: the same hash anti join, its build side bounded by the searched collection. */
    private static final String EXACT_BOUNDED_BUILD_PLAN = """
        Limit  (cost=1.00..2.00 rows=10 width=102)
          ->  Sort  (cost=1.00..2.00 rows=10 width=102)
                Sort Key: ((c.embedding_384 OPERATOR(nexus.<=>) '[1]'::nexus.vector))
                ->  Hash Anti Join  (cost=1.00..9.00 rows=9000 width=102)
                      Hash Cond: ((c.collection = sm.collection) AND (c.chash = sm.chash))
                      ->  Bitmap Heap Scan on chunks_x c  (cost=1.00..9.00 rows=9000 width=1648)
                            Recheck Cond: (collection = 'k'::text)
                      ->  Hash  (cost=1.00..9.00 rows=1800 width=100)
                            ->  Hash Join  (cost=1.00..9.00 rows=1800 width=100)
                                  Hash Cond: (sm.doc_id = sd.tumbler)
                                  ->  Bitmap Heap Scan on catalog_document_chunks sm  (cost=1.00..9.00 rows=18000 width=100)
                                        Recheck Cond: ((tenant_id = 't'::text) AND (collection = ANY ('{k}'::text[])))
                                        ->  Bitmap Index Scan on idx_catalog_chunks_collection  (cost=0.00..9.00 rows=18000 width=0)
                                              Index Cond: ((tenant_id = 't'::text) AND (collection = ANY ('{k}'::text[])))
                                  ->  Hash  (cost=71.61..71.61 rows=240 width=20)
                                        ->  Bitmap Heap Scan on catalog_documents sd  (cost=0.00..71.61 rows=240 width=20)
                                              Filter: ((deleted_at IS NULL) AND sensitive)
        """;

    @Test
    void theExactRouteAssertion_rejectsAWholeManifestBuildSide_andAcceptsABoundedOne() {
        assertThat(exactRouteFailures(parse(EXACT_SEQ_BUILD_PLAN)))
            .anyMatch(s -> s.contains("Seq Scan of catalog_document_chunks sm"));
        assertThat(exactRouteFailures(parse(EXACT_BOUNDED_BUILD_PLAN))).isEmpty();
        assertThat(exactRouteFailures(parse(NO_FILTER_PLAN))).anyMatch(s -> s.contains("not the exact route"))
            .anyMatch(s -> s.contains("sensitive test is missing"));
    }

    // -- exclude = false: the predicate folds away in every function --------------------------------------

    private static final List<String> FAMILIES = List.of("plain_search", "hybrid_search", "manifest_search",
        "text_gated_search_allowed", "text_gated_search_by_chash", "text_gated_search_hnsw_first",
        "search_metadata_scoped", "search_aspect_scoped", "search_graph_hop", "search_topic_scoped");

    static Stream<org.junit.jupiter.params.provider.Arguments> familyByDim() {
        List<org.junit.jupiter.params.provider.Arguments> out = new ArrayList<>();
        for (String f : FAMILIES) {
            for (int dim : DIMS) out.add(org.junit.jupiter.params.provider.Arguments.of(f, dim));
        }
        return out.stream();
    }

    private static Field<?> vec(int dim) {
        float[] f = new float[dim];
        f[0] = 1f;
        return DSL.val(Vector.of(f), VectorSeeds.chunkEmbedding(dim));
    }

    private static Field<?> textArray(String... values) {
        return DSL.val(values, SQLDataType.VARCHAR.array());
    }

    private static Field<?> nullText() { return DSL.val(null, SQLDataType.VARCHAR); }
    private static Field<?> nullJsonb() { return DSL.val(null, SQLDataType.JSONB); }
    private static Field<?> nullInt() { return DSL.val(null, SQLDataType.INTEGER); }
    private static Field<?> nullDouble() { return DSL.val(null, SQLDataType.DOUBLE); }
    private static Field<?> nullBytea() { return DSL.val(null, SQLDataType.VARBINARY.array()); }
    private static Field<?> someChashes() {
        return DSL.val(new byte[][] {sha256("rfvyy716-someone")}, SQLDataType.VARBINARY.array());
    }

    private static String modelOf(int dim) {
        return switch (dim) {
            case 384 -> MODEL;
            case 768 -> "bge-base-en-v15-768";
            default -> "voyage-context-3";
        };
    }

    private static String collectionOf(int dim) {
        return switch (dim) {
            case 384 -> SHARED;
            case 768 -> EMPTY_768;
            default -> EMPTY_1024;
        };
    }

    /** The family's call with the p_exclude_sensitive argument last, the arguments of the engine's own callers. */
    private static Table<?> familyCall(String fn, int dim, boolean exclude) {
        Field<?> m = DSL.val(modelOf(dim));
        Field<?> t = DSL.val(TENANT);
        Field<?> colls = textArray(collectionOf(dim));
        Field<?> n = DSL.val(K);
        Field<?> text = DSL.val("rfvyy716");
        List<Field<?>> a = switch (fn) {
            case "plain_search" -> List.of(vec(dim), colls, nullJsonb(), nullText(), n, m, t);
            case "hybrid_search" -> List.of(vec(dim), text, colls, nullJsonb(), n);
            case "manifest_search" -> List.of(vec(dim), nullText(), nullBytea(), colls, nullJsonb(), nullText(),
                nullJsonb(), nullText(), n, m, t);
            // a NULL chash set makes these two a constant false (no plan to read), so they get one chash
            case "text_gated_search_allowed" -> List.of(vec(dim), text, someChashes(), colls, nullJsonb(), nullText(),
                n, m, t);
            case "text_gated_search_by_chash" -> List.of(vec(dim), someChashes(), colls, nullJsonb(), nullText(), n,
                m, t);
            case "text_gated_search_hnsw_first" -> List.of(vec(dim), text, colls, nullJsonb(), nullText(), n, m, t);
            case "search_metadata_scoped" -> List.of(vec(dim), colls, nullText(), nullText(), nullInt(), nullText(),
                nullText(), nullJsonb(), nullJsonb(), nullText(), n, m, t);
            case "search_aspect_scoped" -> List.of(vec(dim), colls, nullText(), nullText(), nullDouble(), nullJsonb(),
                nullJsonb(), nullText(), n, m, t);
            case "search_graph_hop" -> List.of(vec(dim), textArray("seed"), colls, DSL.val("cites"), DSL.val(1),
                DSL.val("out"), nullJsonb(), nullJsonb(), nullText(), n, m, t);
            case "search_topic_scoped" -> List.of(vec(dim), DSL.val("topic"), DSL.val(collectionOf(dim)), n, m, t);
            default -> throw new IllegalArgumentException(fn);
        };
        List<Field<?>> all = new ArrayList<>(a);
        all.add(DSL.val(exclude));
        StringBuilder sql = new StringBuilder("nexus.").append(fn).append('_').append(dim).append('(');
        for (int i = 0; i < all.size(); i++) sql.append(i == 0 ? "{0}" : ", {" + i + "}");
        return DSL.table(sql.append(')').toString(), all.toArray(new QueryPart[0]));
    }

    @ParameterizedTest(name = "{0}_{1}")
    @MethodSource("familyByDim")
    void excludeFalse_foldsTheSensitiveTestAway_andExcludeTrueCarriesIt(String fn, int dim) {
        String off = explain(familyCall(fn, dim, false), false, false);
        String on = explain(familyCall(fn, dim, true), false, false);
        assertThat(carriesSensitiveTest(parse(on)))
            .as("%s_%d, exclude=true: the positive control, a plan that carries the sensitive test%n%s", fn, dim, on)
            .isTrue();
        assertThat(carriesSensitiveTest(parse(off)))
            .as("%s_%d, exclude=false: nothing in the plan carries the sensitive test%n%s", fn, dim, off).isFalse();
        assertThat(antiJoinsAreFoldedToNothing(parse(off)))
            .as("%s_%d, exclude=false: an anti join, if any, has a constant-false join filter%n%s", fn, dim, off)
            .isTrue();
        assertThat(off).as("%s_%d, exclude=false: nothing of the sensitive owner test reaches the plan", fn, dim)
            .doesNotContain("sensitive").doesNotContain(" sm ").doesNotContain(" sd ");
    }

    /** manifest_qualifying_hits, the 31st function: not a family-by-dimension call, so it has its own fold check. */
    private static Table<?> qualifyingHitsCall(boolean exclude) {
        List<Field<?>> a = List.of(textArray(SHARED), nullJsonb(), nullText(), nullJsonb(), nullText(), DSL.val(100),
            DSL.val(MODEL), DSL.val(TENANT), nullText(), DSL.val(exclude));
        StringBuilder sql = new StringBuilder("nexus.manifest_qualifying_hits(");
        for (int i = 0; i < a.size(); i++) sql.append(i == 0 ? "{0}" : ", {" + i + "}");
        return DSL.table(sql.append(')').toString(), a.toArray(new QueryPart[0]));
    }

    @Test
    void manifestQualifyingHits_foldsTheSensitiveTestAway_andExcludeTrueCarriesIt() {
        String off = explain(qualifyingHitsCall(false), false, false);
        String on = explain(qualifyingHitsCall(true), false, false);
        assertThat(carriesSensitiveTest(parse(on)))
            .as("exclude=true: the positive control, a plan that carries the sensitive test%n%s", on).isTrue();
        assertThat(carriesSensitiveTest(parse(off)))
            .as("exclude=false: nothing in the plan carries the sensitive test%n%s", off).isFalse();
        assertThat(antiJoinsAreFoldedToNothing(parse(off))).as("exclude=false%n%s", off).isTrue();
        assertThat(off).doesNotContain("sensitive").doesNotContain(" sm ").doesNotContain(" sd ");
    }

    // -- the parser and the assertions can fail --------------------------------------------------------------

    /** A plan whose chunks leaf is read by a Seq Scan: the HNSW index is not driving. */
    private static final String SEQ_SCAN_PLAN = """
        Limit  (cost=3832.26..3832.28 rows=10 width=103)
          ->  Sort  (cost=3832.26..3832.33 rows=30 width=103)
                Sort Key: ((c.embedding_384 OPERATOR(nexus.<=>) '[1]'::nexus.vector))
                ->  Seq Scan on chunks_mad318272_t_af8b38277810aea6 c  (cost=0.00..3195.00 rows=6000 width=1648)
                      Filter: (NOT (ANY ((tenant_id = (hashed SubPlan 1).col1))))
                      SubPlan 1
                        ->  Seq Scan on catalog_documents sd  (cost=0.00..71.61 rows=240 width=20)
                              Filter: sensitive
        """;

    /** An HNSW scan with no sensitive test at all. */
    private static final String NO_FILTER_PLAN = """
        Limit  (cost=5135.06..5513.74 rows=10 width=102)
          ->  Index Scan using chunks_x_embedding_384_idx on chunks_mad318272_t_af8b38277810aea6 c  (cost=4564.40..118624.00 rows=3000 width=1639)
                Order By: (embedding_384 OPERATOR(nexus.<=>) '[1]'::nexus.vector)
                Filter: (embedding_384 IS NOT NULL)
        """;

    /** The first design: the sensitive test as a hashed SubPlan, a Seq Scan of the whole manifest per statement. */
    private static final String HASHED_SUBPLAN_PLAN = """
        Limit  (cost=1.00..2.00 rows=10 width=102)
          ->  Index Scan using chunks_x_embedding_384_idx on chunks_mad318272_t_af8b38277810aea6 c  (cost=4564.40..118624.00 rows=3000 width=1639)
                Order By: (embedding_384 OPERATOR(nexus.<=>) '[1]'::nexus.vector)
                Filter: ((NOT false) OR (NOT (ANY ((tenant_id = (hashed SubPlan 1).col1) AND (collection = (hashed SubPlan 1).col2)))))
                SubPlan 1
                  ->  Hash Join  (cost=71.61..431.00 rows=600 width=100)
                        ->  Seq Scan on catalog_document_chunks sm  (cost=0.00..431.00 rows=12000 width=100)
                        ->  Hash  (cost=71.61..71.61 rows=240 width=20)
                              ->  Seq Scan on catalog_documents sd  (cost=0.00..71.61 rows=240 width=20)
                                    Filter: sensitive
        """;

    /** An anti join whose inner side reads the whole manifest sequentially, once: right shape, wrong probe. */
    private static final String SEQ_PROBE_PLAN = """
        Limit  (cost=1.00..2.00 rows=10 width=102)
          ->  Hash Anti Join  (cost=1.00..9.00 rows=10 width=102)
                Hash Cond: (c.chash = sm.chash)
                ->  Index Scan using chunks_x_embedding_384_idx on chunks_mad318272_t_af8b38277810aea6 c  (cost=4564.40..118624.00 rows=3000 width=1639)
                      Order By: (embedding_384 OPERATOR(nexus.<=>) '[1]'::nexus.vector)
                ->  Hash  (cost=71.61..71.61 rows=240 width=20)
                      ->  Seq Scan on catalog_document_chunks sm  (cost=0.00..431.00 rows=12000 width=100)
        """;

    /** The shape the fix produces: an anti join, the HNSW scan outside, an index probe on chash inside. */
    private static final String ANTI_JOIN_PLAN = """
        Limit  (cost=1.00..2.00 rows=10 width=102)
          ->  Nested Loop Anti Join  (cost=4923.29..229189.51 rows=2850 width=1639)
                ->  Index Scan using chunks_x_embedding_384_idx on chunks_mad318272_t_af8b38277810aea6 c  (cost=4922.59..225259.00 rows=3000 width=1653)
                      Order By: (embedding_384 OPERATOR(nexus.<=>) '[1]'::nexus.vector)
                ->  Nested Loop  (cost=0.70..1.30 rows=1 width=94)
                      ->  Index Scan using idx_catalog_chunks_chash on catalog_document_chunks sm  (cost=0.41..0.96 rows=1 width=100)
                            Index Cond: ((tenant_id = 't'::text) AND (chash = c.chash))
                      ->  Index Scan using catalog_documents_pk on catalog_documents sd  (cost=0.28..0.33 rows=1 width=20)
                            Filter: ((deleted_at IS NULL) AND sensitive)
        """;

    @Test
    void theShapeAssertions_rejectAPlanWithoutTheHnswScan_withoutTheTest_andWithTheFirstDesignOrASeqProbe() {
        assertThat(excludeShapeFailures(parse(SEQ_SCAN_PLAN), "embedding_384", true))
            .anyMatch(s -> s.contains("must be an Index Scan")).anyMatch(s -> s.contains("Order By"))
            .anyMatch(s -> s.contains("plain Sort"));
        assertThat(excludeShapeFailures(parse(NO_FILTER_PLAN), "embedding_384", true))
            .anyMatch(s -> s.contains("anti join")).anyMatch(s -> s.contains("missing"));
        assertThat(excludeShapeFailures(parse(HASHED_SUBPLAN_PLAN), "embedding_384", true))
            .anyMatch(s -> s.contains("is a SubPlan")).anyMatch(s -> s.contains("Seq or Bitmap scan"));
        assertThat(excludeShapeFailures(parse(SEQ_PROBE_PLAN), "embedding_384", true))
            .anyMatch(s -> s.contains("Seq or Bitmap scan")).anyMatch(s -> s.contains("Seq Scan reads catalog_document_chunks"));
        assertThat(excludeShapeFailures(parse(ANTI_JOIN_PLAN), "embedding_384", true)).isEmpty();
        assertThat(form(parse(SEQ_SCAN_PLAN))).isEqualTo(Form.SUBPLAN);
        assertThat(form(parse(ANTI_JOIN_PLAN))).isEqualTo(Form.ANTI_JOIN);
        assertThat(skeleton(parse(ANTI_JOIN_PLAN))).containsExactly("Limit", "Index Scan [leaf]");
    }

    @Test
    void theParser_readsTheAntiJoinOfARealPlan_andNamesItsForm() {
        String plan = plan(Fn.PLAIN, SHARED, true);
        Parsed p = parse(plan);
        assertThat(p.nodes().stream().filter(Node::mainPlan).map(Node::type))
            .as("the main plan of plain_search%n%s", plan).contains("Limit", "Index Scan");
        assertThat(sensitiveBlocks(p)).as("no SubPlan names the sensitive test%n%s", plan).isEmpty();
        assertThat(form(p)).isEqualTo(Form.ANTI_JOIN);
        String off = plan(Fn.PLAIN, SHARED, false);
        assertThat(form(parse(off))).as("exclude=false%n%s", off).isEqualTo(Form.NONE);
    }

    @Test
    void theFixtureIsBigEnoughToMeanSomething() throws Exception {
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(TENANT))).isEqualTo(2 * ROWS + EXACT_ROWS);
            assertThat(ctx.fetchCount(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT)
                .and(CATALOG_DOCUMENTS.SENSITIVE)))
                .isEqualTo((2 * ROWS + EXACT_ROWS + BALLAST_ROWS) / CHUNKS_PER_DOC / SENSITIVE_EVERY);
            // the exact collection is a small part of the tenant's manifest: that is what gives the exact-route check meaning
            long manifest = ctx.fetchCount(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT));
            assertThat(manifest).isEqualTo(2L * ROWS + EXACT_ROWS + BALLAST_ROWS);
            assertThat(EXACT_ROWS * 20L).isLessThan(manifest);
        }
    }
}
