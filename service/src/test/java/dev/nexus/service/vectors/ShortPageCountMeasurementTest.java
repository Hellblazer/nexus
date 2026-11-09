// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record;
import org.jooq.Result;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashSet;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.MANIFEST_QUALIFYING_HITS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-h7kw2: what the short-page count costs at 220,000 chunks. A hand-run measurement, no product behaviour pinned
 * here (the stable parts are plan pins in {@link ManifestQualifyingHitsPlanTest}; a wall clock is never asserted). It
 * prints {@code H7KW2 ...} lines and the numbers go in the bead.
 *
 * <p>Three questions, one fixture.
 * <ol>
 *   <li>The hybrid dense branch's count applies the text gate ({@code <%} trigram OR {@code plainto_tsquery}, per row,
 *       nexus-zk7rw). On a COMMON label the count stops at its cap of qualifying rows, so its cost is where the gate's
 *       matches sit in the manifest's scan order. Gates: 5,000-odd matches spread evenly, the same number placed last
 *       by manifest position (the nested-loop worst) and last by chash (the merge-join worst), and a 600-match gate
 *       (the selective shape the planner turns into a Hash Join over sequential scans once the tables are analyzed,
 *       nexus-zk7rw review r1). Both table states: never analyzed, and analyzed.</li>
 *   <li>Route (a)'s distinct count: the read at the cap and at twice the cap, per label, with how many distinct chunks
 *       the narrow read held (so whether the widening fires). Labels: a mid-sized one with one owner per chunk, one
 *       whose chunks have three owners laid next to each other, and two with a share of two-owner chunks.</li>
 *   <li>End to end through {@code vec.search} with the scan cap at its minimum, so a mid-sized label's page is short and
 *       the count decides between a skip and the exact re-run. Run on the code before and after the change.</li>
 * </ol>
 *
 * <p>Opt-in: tag {@code measurement}, which the default run and the {@code integration} group leave out. Run:
 * {@code ./mvnw -B test -Dtest.excluded.groups= -Dgroups=measurement -Dtest=ShortPageCountMeasurementTest
 * -Dnexus.test.fork.timeout=0}; {@code -Dnexus.h7kw2.n=} lowers the chunk count for a smoke run. Non-vacuity: the
 * fixture's own counts, each gate's match count, and a floor on the cells examined are asserted when it runs.
 */
@Tag("measurement")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ShortPageCountMeasurementTest {

    private static final String SVC_ROLE = "svc_h7kw2";
    private static final String SVC_PASS = "svc_h7kw2_pass";
    private static final String TENANT = "h7kw2";
    private static final String COLLECTION = "docs__h7kw2__minilm-l6-v2-384__v1";
    private static final String MODEL = "minilm-l6-v2-384";
    private static final String QUERY = "h7kw2probe";

    private static final int N = Integer.getInteger("nexus.h7kw2.n", 220_000);
    private static final int BATCH = 20_000;
    private static final int CHUNKS_PER_DOC = 110;
    private static final int RUNS = Integer.getInteger("nexus.h7kw2.runs", 5);

    /** The four gate words, each unlike the others and unlike the body text under the trigram arm. */
    private static final String ALPHA = "zebraquilt";
    private static final String BETA = "walrusknot";
    private static final String GAMMA = "otterhaze";
    private static final String DELTA = "ferretdrum";

    /** Matches per gate: about 2.3 percent of the chunks (past the engine's 5,000 selective-gate cutoff at 220,000). */
    private static final int ALPHA_EVERY = 43;
    private static final int LAST_N = N / 43 + 1;
    /** About 0.27 percent: the selective gate. */
    private static final int DELTA_EVERY = 367;

    /** A gate is the query text the count is given, or null for none. */
    private record Gate(String name, String text) {}

    private static final List<Gate> GATES = List.of(
        new Gate("none", null),
        new Gate("alpha-spread-2.3pct", ALPHA),
        new Gate("beta-last-by-position-2.3pct", BETA),
        new Gate("gamma-last-by-chash-2.3pct", GAMMA),
        new Gate("delta-spread-0.27pct", DELTA));

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    PgVectorRepository vec;
    private int cells;

    /** Embedder with the one query vector (1, 0, ...), the start of the quarter circle the chunks are laid on. */
    private static final class Flat implements Embedder {
        @Override
        public List<float[]> embed(List<String> texts) {
            List<float[]> out = new ArrayList<>();
            for (String t : texts) {
                float[] v = new float[384];
                v[0] = 1.0f;
                out.add(v);
            }
            return out;
        }
    }

    // ── fixture ────────────────────────────────────────────────────────────────────────────────────────────────────

    private static Field<byte[]> chashOf(Field<Integer> i) {
        Field<String> iText = DSL.cast(i, SQLDataType.VARCHAR);
        return DSL.function("decode", SQLDataType.BLOB,
            DSL.concat(DSL.md5(DSL.concat(DSL.inline("h7-a-"), iText)), DSL.md5(DSL.concat(DSL.inline("h7-b-"), iText))),
            DSL.inline("hex"));
    }

    private static String md5Hex(String s) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("MD5").digest(s.getBytes(StandardCharsets.UTF_8)));
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    /** The chash (hex) at or above which a chunk is among the {@code n} highest, the "last by chash" gate. */
    private static String chashThreshold(int n) {
        String[] all = new String[N];
        for (int i = 1; i <= N; i++) {
            all[i - 1] = md5Hex("h7-a-" + i) + md5Hex("h7-b-" + i);
        }
        Arrays.sort(all);
        return all[N - n];
    }

    private static Field<String> gateWord(org.jooq.Condition when, String word) {
        return DSL.when(when, DSL.inline(" " + word)).otherwise(DSL.inline(""));
    }

    /** The manifest label of chunk i, and how many owners it has (the first owner plus extras). */
    private static Field<String> labelOf(Field<Integer> i) {
        return DSL.when(i.mod(200).eq(7), DSL.inline("mid-x"))
            .when(i.mod(200).eq(13), DSL.inline("multi-x"))
            .when(i.mod(200).eq(21), DSL.inline("share10-x"))
            .when(i.mod(200).eq(29), DSL.inline("share50-x"))
            .otherwise(DSL.inline("body"));
    }

    private static Field<Integer> ownersOf(Field<Integer> i) {
        return DSL.when(i.mod(200).eq(13), DSL.inline(3))
            .when(i.mod(200).eq(21).and(i.mod(2000).eq(21)), DSL.inline(2))
            .when(i.mod(200).eq(29).and(i.div(200).mod(2).eq(0)), DSL.inline(2))
            .otherwise(DSL.inline(1));
    }

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.startDedicated("6g", 10L * 1024 * 1024 * 1024);
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(2);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        var embedder = new Flat();
        vec = new PgVectorRepository(tenantScope, embedder, embedder);

        String gammaFrom = chashThreshold(LAST_N);
        long t0 = System.nanoTime();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(seed, TENANT, COLLECTION);
            Map<String, String> off = Map.of("autovacuum_enabled", "false");
            PgContainerHelper.setTableStorageParams(su, CATALOG_DOCUMENT_CHUNKS, off);
            PgContainerHelper.setTableStorageParams(su, CATALOG_DOCUMENTS, off);

            int docs = (N - 1) / CHUNKS_PER_DOC + 1;
            var docSeries = DSL.generateSeries(0, docs - 1).as("d", "n");
            Field<Integer> n = docSeries.field("n", Integer.class);
            seed.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                    CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.SOURCE_URI)
                .select(seed.select(DSL.inline(TENANT), DSL.concat(DSL.inline("h7-d"), DSL.cast(n, SQLDataType.VARCHAR)),
                        DSL.concat(DSL.inline("h7-d"), DSL.cast(n, SQLDataType.VARCHAR), DSL.inline(".md")),
                        DSL.inline(COLLECTION),
                        DSL.concat(DSL.inline("file:///h7-d"), DSL.cast(n, SQLDataType.VARCHAR), DSL.inline(".md")))
                    .from(docSeries))
                .execute();
            for (String twin : List.of("h7-twin-a", "h7-twin-b")) {
                seed.insertInto(CATALOG_DOCUMENTS)
                    .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT)
                    .set(CATALOG_DOCUMENTS.TUMBLER, twin)
                    .set(CATALOG_DOCUMENTS.TITLE, twin + ".md")
                    .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, COLLECTION)
                    .set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///" + twin + ".md")
                    .execute();
            }

            boolean leafTuned = false;
            for (int lo = 1; lo <= N; lo += BATCH) {
                int hi = Math.min(N, lo + BATCH - 1);
                var series = DSL.generateSeries(lo, hi).as("s", "i");
                Field<Integer> i = series.field("i", Integer.class);
                Field<String> iText = DSL.cast(i, SQLDataType.VARCHAR);
                Field<byte[]> chash = chashOf(i);
                Field<Double> theta = DSL.cast(i.minus(1), SQLDataType.DOUBLE)
                    .times(Math.PI / 2.0 / Math.max(1, N - 1));
                Field<String> vector = DSL.concat(DSL.inline("["), DSL.cast(DSL.cos(theta), SQLDataType.VARCHAR),
                    DSL.inline(","), DSL.cast(DSL.sin(theta), SQLDataType.VARCHAR), DSL.inline(",0".repeat(382)),
                    DSL.inline("]"));
                Field<String> text = DSL.concat(DSL.inline("h7 chunk "), iText,
                    gateWord(i.mod(ALPHA_EVERY).eq(0), ALPHA),
                    gateWord(i.gt(N - LAST_N), BETA),
                    gateWord(chash.ge(DSL.val(HexFormat.of().parseHex(gammaFrom), SQLDataType.BLOB)), GAMMA),
                    gateWord(i.mod(DELTA_EVERY).eq(0), DELTA));
                seed.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.EMBEDDING_MODEL, CHUNKS.CHASH,
                        CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_384)
                    .select(seed.select(DSL.val(TENANT, CHUNKS.TENANT_ID), DSL.val(COLLECTION, CHUNKS.COLLECTION),
                            DSL.val(MODEL, CHUNKS.EMBEDDING_MODEL), chash, text,
                            DSL.cast(vector, CHUNKS.EMBEDDING_384.getDataType()))
                        .from(series))
                    .execute();
                if (!leafTuned) {
                    // The tenant's leaf exists once its first chunk does: turn autovacuum off there too, so the
                    // never-analyzed phase of the measurement stays that.
                    PgContainerHelper.setTableStorageParams(su, DSL.table(DSL.name("nexus", leafName(MODEL, TENANT))), off);
                    leafTuned = true;
                }

                var owners = DSL.generateSeries(0, 2).as("o", "k");
                Field<Integer> k = owners.field("k", Integer.class);
                Field<String> docId = DSL.when(k.eq(0),
                        DSL.concat(DSL.inline("h7-d"), DSL.cast(i.minus(1).div(CHUNKS_PER_DOC), SQLDataType.VARCHAR)))
                    .when(k.eq(1), DSL.inline("h7-twin-a")).otherwise(DSL.inline("h7-twin-b"));
                Field<Integer> position = DSL.when(k.eq(0), i.minus(1).mod(CHUNKS_PER_DOC)).otherwise(i);
                seed.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID,
                        CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                        CATALOG_DOCUMENT_CHUNKS.COLLECTION, CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL,
                        CATALOG_DOCUMENT_CHUNKS.SECTION_TYPE, CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN)
                    .select(seed.select(DSL.val(TENANT, CATALOG_DOCUMENT_CHUNKS.TENANT_ID), docId, position, chash,
                            DSL.val(COLLECTION, CATALOG_DOCUMENT_CHUNKS.COLLECTION),
                            DSL.val(MODEL, CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL), labelOf(i), DSL.inline(true))
                        .from(series).crossJoin(owners)
                        .where(k.lt(ownersOf(i)))
                        .orderBy(i, k))
                    .execute();
                System.out.println("H7KW2 seeded through chunk " + hi + " of " + N);
            }
            System.out.println("H7KW2 seeded in " + (System.nanoTime() - t0) / 1_000_000_000L + " s");

            // Non-vacuity: the fixture is what the numbers below are about.
            assertThat(seed.fetchCount(CHUNKS)).as("chunks").isEqualTo(N);
            assertGateMatches(seed, ALPHA, N / ALPHA_EVERY);
            assertGateMatches(seed, BETA, LAST_N);
            assertGateMatches(seed, GAMMA, LAST_N);
            assertGateMatches(seed, DELTA, N / DELTA_EVERY);
            for (String label : List.of("mid-x", "multi-x", "share10-x", "share50-x")) {
                long chunksWith = seed.fetchCount(seed.selectDistinct(CATALOG_DOCUMENT_CHUNKS.CHASH)
                    .from(CATALOG_DOCUMENT_CHUNKS).where(CATALOG_DOCUMENT_CHUNKS.SECTION_TYPE.eq(label)));
                long owners = seed.fetchCount(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.SECTION_TYPE.eq(label));
                long multi = seed.fetchCount(seed.select(CATALOG_DOCUMENT_CHUNKS.CHASH).from(CATALOG_DOCUMENT_CHUNKS)
                    .where(CATALOG_DOCUMENT_CHUNKS.SECTION_TYPE.eq(label))
                    .groupBy(CATALOG_DOCUMENT_CHUNKS.CHASH).having(DSL.count().gt(1)));
                System.out.println("H7KW2 label " + label + ": chunks=" + chunksWith + " owner_rows=" + owners
                    + " chunks_with_several_owners=" + multi);
                assertThat(chunksWith).as("label %s has chunks", label).isPositive();
            }
            long allChunks = N;
            long multiAll = seed.fetchCount(seed.select(CATALOG_DOCUMENT_CHUNKS.CHASH).from(CATALOG_DOCUMENT_CHUNKS)
                .groupBy(CATALOG_DOCUMENT_CHUNKS.CHASH).having(DSL.count().gt(1)));
            System.out.println("H7KW2 fixture multi-owner share: " + multiAll + " of " + allChunks + " chunks ("
                + String.format(java.util.Locale.ROOT, "%.3f", 100.0 * multiAll / allChunks) + " pct)");
        }
    }

    private static void assertGateMatches(DSLContext seed, String word, int expected) {
        long got = seed.fetchCount(CHUNKS, CHUNKS.CHUNK_TEXT.contains(word));
        System.out.println("H7KW2 gate word " + word + ": chunks=" + got + " expected=" + expected);
        assertThat(got).as("gate %s", word).isBetween((long) expected - 1, (long) expected + 1);
    }

    private static String leafName(String model, String tenant) {
        return "chunks_m" + sha256Hex(model).substring(0, 8) + "_t_" + sha256Hex(tenant).substring(0, 16);
    }

    private static String sha256Hex(String text) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(text.getBytes(StandardCharsets.UTF_8)));
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    @AfterAll
    void stop() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── measurement helpers ────────────────────────────────────────────────────────────────────────────────────────

    private static ManifestPushdown.Plan plan(String label) {
        ManifestPushdown.Plan plan = ManifestPushdown.of(ManifestLabelStage.split(Map.<String, Object>of("section_type", label)).labels());
        assertThat(plan).isNotNull();
        return plan;
    }

    private Result<? extends Record> read(DSLContext ctx, String label, int cap, String queryText) {
        ManifestPushdown.Plan p = plan(label);
        return ctx.selectFrom(MANIFEST_QUALIFYING_HITS.call(new String[] {COLLECTION}, null, null, p.containment(),
            p.jsonPath(), cap, MODEL, TENANT, queryText, false)).fetch();
    }

    private static int distinct(Result<? extends Record> rows) {
        Set<String> chunks = new HashSet<>();
        for (Record r : rows) {
            chunks.add(r.get(MANIFEST_QUALIFYING_HITS.CHUNK_COLLECTION) + "/"
                       + HexFormat.of().formatHex(r.get(MANIFEST_QUALIFYING_HITS.CHUNK_CHASH)));
        }
        return chunks.size();
    }

    /**
     * Median and minimum milliseconds (to the tenth) of {@code RUNS} calls after two warm-ups. A call whose first warm-up
     * takes over a second (the 14 s shapes) is timed three times after that one warm-up, to keep the run finite.
     */
    private static double[] timed(Runnable r) {
        long w = System.nanoTime();
        r.run();
        boolean slow = System.nanoTime() - w > 1_000_000_000L;
        if (!slow) {
            r.run();
        }
        int runs = slow ? 3 : RUNS;
        long[] ns = new long[runs];
        for (int j = 0; j < runs; j++) {
            long start = System.nanoTime();
            r.run();
            ns[j] = System.nanoTime() - start;
        }
        Arrays.sort(ns);
        return new double[] {ns[runs / 2] / 1e6, ns[0] / 1e6};
    }

    private static String ms(double[] t) {
        return String.format(java.util.Locale.ROOT, "median_ms=%.1f min_ms=%.1f", t[0], t[1]);
    }

    private static String shape(String plan) {
        List<String> parts = new ArrayList<>();
        for (String s : List.of("Hash Join", "Merge Join", "Nested Loop")) {
            if (plan.contains(s)) parts.add(s);
        }
        if (plan.contains("Seq Scan on catalog_document_chunks")) parts.add("SeqScan(manifest)");
        if (plan.matches("(?s).*Seq Scan on chunks_.*")) parts.add("SeqScan(chunks)");
        if (plan.contains("Sort")) parts.add("Sort");
        if (plan.contains("Aggregate")) parts.add("Aggregate");
        return String.join("+", parts);
    }

    private String planOf(String label, int cap, String queryText) {
        return tenantScope.withTenant(TENANT, ctx -> {
            ManifestPushdown.Plan p = plan(label);
            return PgContainerHelper.explainPlanInlined(ctx, ctx.selectFrom(MANIFEST_QUALIFYING_HITS.call(
                new String[] {COLLECTION}, null, null, p.containment(), p.jsonPath(), cap, MODEL, TENANT, queryText, false)));
        });
    }

    private void gatedCounts(String state) {
        for (Gate gate : GATES) {
            for (int cap : new int[] {11, 22, 41, 82}) {
                final int c = cap;
                int[] rows = new int[1];
                double[] t = timed(() -> rows[0] = tenantScope.withTenant(TENANT,
                    ctx -> read(ctx, "body", c, gate.text())).size());
                String plan = planOf("body", cap, gate.text());
                cells++;
                assertThat(rows[0]).as("gate %s cap %d returns rows of a common label", gate.name(), cap)
                    .isPositive().isLessThanOrEqualTo(cap);
                System.out.println("H7KW2 gated-count state=" + state + " label=body gate=" + gate.name() + " cap=" + cap
                    + " rows=" + rows[0] + " " + ms(t) + " plan=" + shape(plan));
                if (cap == 11 || cap == 82) {
                    for (String line : plan.split("\n")) {
                        System.out.println("H7KW2 PLAN " + state + " " + gate.name() + " cap=" + cap + " | " + line);
                    }
                }
            }
        }
    }

    /**
     * The route (a) primitives per label: the read at the cap and at twice the cap, and whether the narrow read held
     * fewer distinct chunks than its cap (the case that widens).
     */
    private void distinctReads(String state) {
        for (String label : List.of("body", "mid-x", "multi-x", "share10-x", "share50-x")) {
            for (int cap : new int[] {6, 11, 41}) {
                final int c = cap;
                Result<? extends Record> narrow = tenantScope.withTenant(TENANT, ctx -> read(ctx, label, c, null));
                Result<? extends Record> wide = tenantScope.withTenant(TENANT, ctx -> read(ctx, label, 2 * c, null));
                double[] tn = timed(() -> tenantScope.withTenant(TENANT, ctx -> read(ctx, label, c, null)));
                double[] tw = timed(() -> tenantScope.withTenant(TENANT, ctx -> read(ctx, label, 2 * c, null)));
                int dn = distinct(narrow);
                boolean widens = narrow.size() == c && dn < c;
                // The two algorithms end to end, over the same function: the one before this change (a single read of
                // twice the cap, deduped) and PgVectorRepository#distinctQualifying (the cap, widened only on repeats).
                int[] oldCount = new int[1];
                int[] newCount = new int[1];
                double[] told = timed(() -> oldCount[0] = tenantScope.withTenant(TENANT, ctx -> {
                    Result<? extends Record> rows = read(ctx, label, 2 * c, null);
                    int d = distinct(rows);
                    return rows.size() < 2 * c ? d : Math.max(d, c);
                }));
                double[] tnew = timed(() -> newCount[0] = tenantScope.withTenant(TENANT,
                    ctx -> PgVectorRepository.distinctQualifying(c, n -> keys(read(ctx, label, n, null)))));
                cells++;
                assertThat(narrow.size()).as("%s cap %d", label, cap).isPositive().isLessThanOrEqualTo(cap);
                // Same decision against a page one row short of the cap: both at or above the cap, or both the same
                // exact count below it.
                assertThat(newCount[0] >= c).as("%s cap %d: old %d, new %d", label, cap, oldCount[0], newCount[0])
                    .isEqualTo(oldCount[0] >= c);
                if (newCount[0] < c) {
                    assertThat(newCount[0]).isEqualTo(oldCount[0]);
                }
                System.out.println("H7KW2 distinct-read state=" + state + " label=" + label + " cap=" + cap
                    + " narrow_rows=" + narrow.size() + " narrow_distinct=" + dn + " narrow " + ms(tn)
                    + " | wide_rows=" + wide.size() + " wide_distinct=" + distinct(wide) + " wide " + ms(tw)
                    + " | widens=" + widens
                    + " | OLD(one read of 2cap)=" + oldCount[0] + " " + ms(told)
                    + " | NEW(distinctQualifying)=" + newCount[0] + " " + ms(tnew));
            }
        }
    }

    private static List<String> keys(Result<? extends Record> rows) {
        List<String> keys = new ArrayList<>(rows.size());
        for (Record r : rows) {
            keys.add(r.get(MANIFEST_QUALIFYING_HITS.CHUNK_COLLECTION) + "/"
                     + HexFormat.of().formatHex(r.get(MANIFEST_QUALIFYING_HITS.CHUNK_CHASH)));
        }
        return keys;
    }

    private void endToEnd(String state) {
        PgSession.overrideScanBudgetForTests(1000, 100);
        try {
            for (String label : List.of("mid-x", "multi-x", "share10-x", "share50-x")) {
                Map<String, Object> where = new LinkedHashMap<>();
                where.put("section_type", label);
                long rerun0 = PgVectorRepository.shortPageRerunCount();
                long skip0 = PgVectorRepository.shortPageSkipCount();
                int[] rows = new int[1];
                double[] t = timed(() -> rows[0] = vec.search(TENANT, QUERY, List.of(COLLECTION), 10, where).size());
                long reruns = PgVectorRepository.shortPageRerunCount() - rerun0;
                long skips = PgVectorRepository.shortPageSkipCount() - skip0;
                cells++;
                if (N >= 50_000 && label.equals("mid-x")) {
                    assertThat(reruns).as("the mid-sized label's short page is re-run (the case this measures)").isPositive();
                }
                System.out.println("H7KW2 end-to-end state=" + state + " label=" + label + " scan_cap=1000 rows=" + rows[0]
                    + " " + ms(t) + " reruns=" + reruns + " skips=" + skips);
            }
        } finally {
            PgSession.resetScanBudgetForTests();
        }
    }

    @Test
    void measure() throws Exception {
        gatedCounts("unanalyzed");
        distinctReads("unanalyzed");
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.analyzeTable(su, CHUNKS);
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENT_CHUNKS);
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENTS);
        }
        gatedCounts("analyzed");
        distinctReads("analyzed");
        endToEnd("analyzed");
        // 2 states x 5 gates x 4 caps, 2 x 5 labels x 3 caps, and 4 end-to-end labels.
        assertThat(cells).as("cells measured").isEqualTo(2 * 5 * 4 + 2 * 5 * 3 + 4);
    }
}
