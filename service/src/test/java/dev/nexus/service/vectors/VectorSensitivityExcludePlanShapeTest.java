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
 * <p><b>What a pass means (exclude = true).</b> In the main plan (outside any SubPlan) the only scan of a chunks leaf
 * is one HNSW index scan carrying an {@code Order By} on the distance; no plain Sort sits above it; the sensitive-owner
 * relations are never scanned by the main plan; and exactly one SubPlan that names {@code sensitive} is a Filter of
 * that scan, so the test runs on candidate rows. For {@code plain_search} and {@code manifest_search} the node skeleton
 * of the main plan equals the {@code exclude = false} plan's: the predicate adds a filter and nothing else.
 *
 * <p><b>What the filter's SubPlan is.</b> The test accepts both forms the planner produces for the NOT EXISTS: a
 * correlated probe per candidate row, and a HASHED SubPlan, which reads the tenant's whole sensitive-owner set once
 * per statement (at the fixture's size a Seq Scan of the tenant's manifest joined to the sensitive documents). The
 * form is named in every failure message and in the T2 record nexus_rdr/243-plan-evidence-2026-11-06; which one the
 * planner picks at production sizes is what the live measurement (scripts/spikes/rdr243_plan) is for. This class does
 * not decide that the hashed form is acceptable.
 *
 * <p><b>The metadata family is read under reachability.</b> At 6000 rows per collection the natural plan of
 * {@code search_metadata_scoped} is a Seq or Bitmap scan of the chunks leaf joined to the manifest, with or without
 * the predicate: the join leaves no selective filter and the HNSW start-up cost exceeds a 6000-row scan. So that
 * family is explained with the alternatives to the ordered HNSW scan penalised (the technique of
 * PgVectorRepositoryRawSqlPlanShapeTest#explainIndexReachable, nexus-h9zkm): the pin is that the predicate does not
 * make the index unreachable. Plain and manifest search are read with the natural planner, no {@code enable_*} setting.
 *
 * <p><b>Scope.</b> 6000 chunks per collection, 10 percent sensitive documents, one dimension (384) for the plan
 * assertions; the folding check covers all 30 dimension-specific functions. Custom plans only (the serving
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
    static final int ROWS = 6000;
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
                seedCollection(ctx, c, rnd);
            }
            PgContainerHelper.createLeafHnswIndex(su, "nexus", leaf, PCI_INDEX, "embedding_384", PCI);
            PgContainerHelper.analyzeTable(su, DSL.table(DSL.name("nexus", leaf)));
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENT_CHUNKS);
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENTS);
        }
    }

    private void seedCollection(DSLContext ctx, String collection, Random rnd) {
        byte[][] chash = new byte[ROWS][];
        for (int from = 0; from < ROWS; from += 250) {
            int to = Math.min(ROWS, from + 250);
            var step = ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH,
                CHUNKS.EMBEDDING_MODEL, CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_384);
            for (int i = from; i < to; i++) {
                chash[i] = sha256(collection + "|" + i);
                step = step.values(TENANT, collection, chash[i], MODEL, "t" + i, Vector.of(gaussianUnit(rnd, 384)));
            }
            step.execute();
        }
        int docs = ROWS / CHUNKS_PER_DOC;
        for (int d = 0; d < docs; d++) {
            ctx.insertInto(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT).set(CATALOG_DOCUMENTS.TUMBLER, doc(collection, d))
                .set(CATALOG_DOCUMENTS.TITLE, "doc " + d).set(CATALOG_DOCUMENTS.CONTENT_TYPE, "paper")
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, collection)
                .set(CATALOG_DOCUMENTS.SENSITIVE, d % SENSITIVE_EVERY == 0)
                .execute();
        }
        for (int from = 0; from < ROWS; from += 250) {
            int to = Math.min(ROWS, from + 250);
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

    private static String doc(String collection, int d) {
        return (collection.contains("shared") ? "s" : "p") + "-" + d;
    }

    // -- the plans ---------------------------------------------------------------------------------------

    enum Fn {
        PLAIN, MANIFEST, METADATA
    }

    private Table<?> call(Fn fn, String collection, boolean exclude) {
        String[] colls = {collection};
        return switch (fn) {
            case PLAIN -> PLAIN_SEARCH_384.call(query, colls, null, null, K, MODEL, TENANT, exclude);
            case MANIFEST -> MANIFEST_SEARCH_384.call(query, null, null, colls, null, null, null, null, K, MODEL,
                TENANT, exclude);
            case METADATA -> SEARCH_METADATA_SCOPED_384.call(query, colls, null, null, null, null, null, null, null,
                null, K, MODEL, TENANT, exclude);
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

    /** What the plan says about the sensitive test: the form of its SubPlan. */
    enum Form {
        /** A hashed SubPlan: the tenant's sensitive-owner set is built once per statement. */
        HASHED_SUBPLAN,
        /** A correlated SubPlan probed per candidate row. */
        CORRELATED_SUBPLAN,
        NONE
    }

    private static List<SubPlanBlock> sensitiveBlocks(Parsed p) {
        return p.blocks().stream().filter(SubPlanBlock::namesSensitive).toList();
    }

    static Form form(Parsed p) {
        List<SubPlanBlock> sens = sensitiveBlocks(p);
        if (sens.isEmpty()) return Form.NONE;
        String n = sens.get(0).name().replace("SubPlan ", "");
        return p.plan().contains("hashed SubPlan " + n) ? Form.HASHED_SUBPLAN : Form.CORRELATED_SUBPLAN;
    }

    /** The main-plan node types in order, with no costs: the skeleton two plans are compared on. */
    static List<String> skeleton(Parsed p) {
        return p.nodes().stream().filter(Node::mainPlan).map(n -> n.type()
            + (n.scansChunksLeaf() ? " [" + (n.indexName().contains("pci_") ? "pci" : "leaf") + "]" : "")).toList();
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
        if (p.nodes().stream().filter(Node::mainPlan).anyMatch(n -> n.text().contains(" on catalog_document_chunks sm ")
                || n.text().contains(" on catalog_documents sd "))) {
            bad.add("the sensitive-owner relations are scanned by the main plan, not by a filter on the candidates");
        }
        List<SubPlanBlock> sens = sensitiveBlocks(p);
        if (sens.size() != 1) {
            bad.add("exactly one SubPlan must name the sensitive test, found " + sens.size());
        } else {
            String n = sens.get(0).name();
            boolean onScan = scan.props().stream().anyMatch(l -> l.startsWith("Filter:") && l.contains(n));
            if (!onScan) {
                bad.add(n + " (the sensitive test) must be a Filter of the HNSW scan");
            }
        }
        return bad;
    }

    private void assertExcludeShape(Fn fn, String collection, String expectedIndex) {
        String on = plan(fn, collection, true);
        Parsed p = parse(on);
        List<String> bad = excludeShapeFailures(p, expectedIndex, fn != Fn.METADATA);
        assertThat(bad).as("%s on %s, exclude=true, sensitive form %s%n%s", fn, collection, form(p), on).isEmpty();
        if (fn != Fn.METADATA) {
            Parsed off = parse(plan(fn, collection, false));
            assertThat(skeleton(p)).as("%s on %s: exclude=true adds a filter, not a node%n--- true%n%s", fn,
                collection, on).isEqualTo(skeleton(off));
        }
    }

    // -- exclude = true: the three families, shared and PCI ----------------------------------------------

    @ParameterizedTest(name = "{0} walks the shared leaf index")
    @EnumSource(Fn.class)
    void excludeTrue_sharedLeafIndex_drivesTheScan_andTheSensitiveTestIsAFilterOnIt(Fn fn) {
        assertExcludeShape(fn, SHARED, "embedding_384");
    }

    @ParameterizedTest(name = "{0} walks the per-collection index")
    @EnumSource(Fn.class)
    void excludeTrue_perCollectionIndex_drivesTheScan_andTheSensitiveTestIsAFilterOnIt(Fn fn) {
        assertExcludeShape(fn, PCI, PCI_INDEX);
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
        assertThat(sensitiveBlocks(parse(on)))
            .as("%s_%d, exclude=true: the positive control, a plan that carries the sensitive test%n%s", fn, dim, on)
            .isNotEmpty();
        assertThat(sensitiveBlocks(parse(off)))
            .as("%s_%d, exclude=false: no SubPlan names the sensitive test%n%s", fn, dim, off).isEmpty();
        assertThat(off).as("%s_%d, exclude=false: nothing of the sensitive owner test reaches the plan", fn, dim)
            .doesNotContain("sensitive").doesNotContain(" sm ").doesNotContain(" sd ");
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

    /** The sensitive owners read first by the main plan: a join above the chunks scan. */
    private static final String SENSITIVE_FIRST_PLAN = """
        Limit  (cost=1.00..2.00 rows=10 width=102)
          ->  Hash Anti Join  (cost=1.00..9.00 rows=10 width=102)
                Hash Cond: (c.chash = sm.chash)
                ->  Index Scan using chunks_x_embedding_384_idx on chunks_mad318272_t_af8b38277810aea6 c  (cost=4564.40..118624.00 rows=3000 width=1639)
                      Order By: (embedding_384 OPERATOR(nexus.<=>) '[1]'::nexus.vector)
                ->  Hash  (cost=71.61..71.61 rows=240 width=20)
                      ->  Seq Scan on catalog_document_chunks sm  (cost=0.00..431.00 rows=12000 width=100)
        """;

    @Test
    void theShapeAssertions_rejectAPlanWithoutTheHnswScan_withoutTheFilter_andWithTheOwnersReadFirst() {
        assertThat(excludeShapeFailures(parse(SEQ_SCAN_PLAN), "embedding_384", true))
            .anyMatch(s -> s.contains("must be an Index Scan")).anyMatch(s -> s.contains("Order By"))
            .anyMatch(s -> s.contains("plain Sort"));
        assertThat(excludeShapeFailures(parse(NO_FILTER_PLAN), "embedding_384", true))
            .anyMatch(s -> s.contains("exactly one SubPlan"));
        assertThat(excludeShapeFailures(parse(SENSITIVE_FIRST_PLAN), "embedding_384", true))
            .anyMatch(s -> s.contains("not by a filter on the candidates"))
            .anyMatch(s -> s.contains("exactly one SubPlan"));
        assertThat(form(parse(SEQ_SCAN_PLAN))).isEqualTo(Form.HASHED_SUBPLAN);
    }

    @Test
    void theParser_readsTheSubPlansOfARealPlan_andNamesTheirForm() {
        String plan = plan(Fn.PLAIN, SHARED, true);
        Parsed p = parse(plan);
        assertThat(p.nodes().stream().filter(Node::mainPlan).map(Node::type))
            .as("the main plan of plain_search%n%s", plan).contains("Limit", "Index Scan");
        assertThat(p.blocks()).as("plain_search carries the live-owner SubPlan and the sensitive one")
            .hasSizeGreaterThanOrEqualTo(2);
        assertThat(sensitiveBlocks(p)).hasSize(1);
        assertThat(form(p)).isIn(Form.HASHED_SUBPLAN, Form.CORRELATED_SUBPLAN);
        assertThat(form(parse(plan(Fn.PLAIN, SHARED, false)))).isEqualTo(Form.NONE);
    }

    @Test
    void theFixtureIsBigEnoughToMeanSomething() throws Exception {
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(TENANT))).isEqualTo(2 * ROWS);
            assertThat(ctx.fetchCount(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT)
                .and(CATALOG_DOCUMENTS.SENSITIVE))).isEqualTo(2 * ROWS / CHUNKS_PER_DOC / SENSITIVE_EVERY);
        }
    }
}
