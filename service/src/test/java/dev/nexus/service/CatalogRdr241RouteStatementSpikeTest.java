// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TenantScope;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.Query;
import org.jooq.Record;
import org.jooq.Record1;
import org.jooq.SQLDialect;
import org.jooq.Select;
import org.jooq.SelectFieldOrAsterisk;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Locale;
import java.util.function.Function;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-uwggq.2.1 (RDR-241 Phase 2 spike, Critical Assumptions): does an engine filter on {@code author}, on a
 * {@code metadata} key, or on the sensitive locators (the {@code alias_of} leg included) answer fast enough at 50,000
 * live documents without a new index? Hand-run measurement; it adds no index and no changeset, it only decides.
 *
 * <p>One tenant, 50,000 live {@code catalog_documents} rows and 2,000 soft-deleted ones, seeded from the Java model
 * {@link Fx} (every expected count below is computed from that model, never from the statement under test, so a wrong
 * statement fails its count assertion instead of timing fast). The three route statements are built as jOOQ queries in
 * the shape the routes will run (tenant equality, {@code deleted_at IS NULL}, tumbler order, {@code LIMIT},
 * {@code count(*) OVER ()}), and run under the service role inside {@link TenantScope#withTenant}, so FORCE row-level
 * security applies as in production. Each is explained with {@code EXPLAIN (ANALYZE, BUFFERS)}: one warm-up, five timed
 * runs, the median {@code Execution Time}. The decision rule is fixed by the bead: a median over
 * {@value #THRESHOLD_MS} ms means the route ships an index. The plans are custom plans (the query is rendered with
 * literal values, as {@code PgContainerHelper.explainPlanWith} does), the best case for the planner.
 *
 * <p>Opt-in: tag {@code measurement}. Run: {@code scripts/mvnw-leased.sh --lab-run lab-m -B test
 * -Dtest.excluded.groups= -Dgroups=measurement -Dtest=CatalogRdr241RouteStatementSpikeTest
 * -Dnexus.test.fork.timeout=0}. Output: {@code RDR241SPIKE} lines on stdout, and the same text in
 * {@code target/rdr241-spike.txt} ({@code -Dnexus.rdr241.out=} moves it).
 */
@Tag("measurement")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogRdr241RouteStatementSpikeTest {

    private static final String SVC_ROLE = "svc_rdr241_spike";
    private static final String SVC_PASS = "svc_rdr241_spike_pass";
    private static final String TENANT = TenantConstants.DEFAULT_TENANT;
    private static final double THRESHOLD_MS = 100.0;
    private static final int RUNS = 5;
    private static final int PAGE = 1000;
    private static final Pattern EXEC_TIME = Pattern.compile("Execution Time: ([0-9.]+) ms");
    private static final Pattern INTEGER_TEXT = Pattern.compile("^-?[0-9]+$");

    // ── the fixture model ───────────────────────────────────────────────────

    /** The Java model of the fixture: rows 0..49,999 are live, 50,000..51,999 soft-deleted. */
    static final class Fx {
        static final int LIVE = 50_000;
        static final int DELETED = 2_000;
        private static final String[] NON_ASCII = {"José Müller", "Łukasz Żółć", "李明", "Ünal Çağlar"};
        private static final String[] TYPES = {"prose", "code", "paper", "knowledge"};
        private static final String NULL_EXT = "\0json-null";

        static boolean live(int i) { return i < LIVE; }

        static String tumbler(int i) { return "7." + i + ".1"; }

        static String author(int i) {
            if (i % 10 == 9) return null;
            if (i % 150 == 0) return switch ((i / 150) % 3) {
                case 0 -> "Leslie Lamport";
                case 1 -> "LAMPORT, L.";
                default -> "Lamport & Lynch";
            };
            if (i % 211 == 2) return NON_ASCII[(i / 211) % NON_ASCII.length];
            if (i % 997 == 1) return "100% Author_Fan " + i;
            return "Author" + (i % 5000) + " Surname" + (i % 7919);
        }

        static String contentType(int i) { return TYPES[(i / 3) % TYPES.length]; }

        /** The text form of quality_gate_overridden ({@code ->>}), or null when the key is absent. */
        static String qgo(int i) {
            int r = (i / 7) % 10;
            return r < 3 ? "true" : r < 6 ? "false" : null;
        }

        /** extraction_source: null absent, "" empty, "file", "mineru", or the NULL_EXT marker for a JSON null. */
        static String ext(int i) {
            if (i % 997 == 5) return NULL_EXT;
            int r = (i / 13) % 20;
            return r < 10 ? null : r < 12 ? "" : r < 18 ? "file" : "mineru";
        }

        /** The text form of miss_count, or null when absent; {@code asString} says whether jsonb stores it as a string. */
        static String miss(int i) {
            if (i % 1013 == 5) return null;
            if (i % 1009 == 0) return "n/a";
            if (i % 1019 == 3) return "2.5";
            int r = (i / 11) % 50;
            int v = r < 35 ? 0 : r < 40 ? 1 : r < 44 ? 2 : r < 47 ? 3 : r < 49 ? 4 : 5;
            return Integer.toString(v);
        }

        static boolean missIsString(int i) { return i % 9 == 0 || i % 1009 == 0 || i % 1019 == 3; }

        static boolean sensitive(int i) { return i % 100 == 7; }

        /** alias_of: 250m+3 names the sensitive row 100m+7; 2500k+1 a DELETED sensitive row; 2500k+2 a live non-sensitive row. */
        static String aliasOf(int i) {
            if (i % 250 == 3) return tumbler(100 * ((i - 3) / 250) + 7);
            if (i % 2500 == 1) return tumbler(Fx.LIVE + 7 + 100 * ((i / 2500) % 20));
            if (i % 2500 == 2) return tumbler(i + 1);
            return "";
        }

        static String metadataJson(int i) {
            var sb = new StringBuilder("{\"seed\":").append(i);
            String q = qgo(i);
            if (q != null) sb.append(",\"quality_gate_overridden\":").append(q);
            String e = ext(i);
            if (NULL_EXT.equals(e)) sb.append(",\"extraction_source\":null");
            else if (e != null) sb.append(",\"extraction_source\":\"").append(e).append('"');
            String m = miss(i);
            if (m != null) {
                sb.append(",\"miss_count\":");
                if (missIsString(i)) sb.append('"').append(m).append('"'); else sb.append(m);
            }
            return sb.append('}').toString();
        }

        // expected counts, over the live rows only
        static long count(java.util.function.IntPredicate p) {
            long n = 0;
            for (int i = 0; i < LIVE; i++) if (p.test(i)) n++;
            return n;
        }

        static boolean authorContains(int i, String term) {
            String a = author(i);
            return a != null && a.toLowerCase(Locale.ROOT).contains(term.toLowerCase(Locale.ROOT));
        }

        /** meta_default semantics: absent, JSON null and "" read as the default. */
        static boolean extDefault(int i, String dflt, String wanted) {
            String e = ext(i);
            String have = (e == null || NULL_EXT.equals(e) || e.isEmpty()) ? dflt : e;
            return have.equals(wanted);
        }

        static boolean missAtLeast(int i, int n) {
            String m = miss(i);
            return m != null && INTEGER_TEXT.matcher(m).matches() && Long.parseLong(m) >= n;
        }

        static boolean ctMatches(int i, String ct) { return ct == null || contentType(i).equals(ct); }
    }

    // ── harness ─────────────────────────────────────────────────────────────

    private PostgreSQLContainer<?> pg;
    private HikariDataSource svcDs;
    private TenantScope scope;
    private final List<String> report = new ArrayList<>();
    private final List<String> decisionLines = new ArrayList<>();

    /** One explained statement: warm-up, five runs, the median Execution Time; the last run's plan is reported. */
    private double measure(String group, String name, Function<DSLContext, Query> build) {
        return scope.withTenant(TENANT, ctx -> {
            Query q = build.apply(ctx);
            PgContainerHelper.explainPlanWith(ctx, q, PgContainerHelper.ExplainOptions.ANALYZE_BUFFERS);   // warm-up
            List<Double> times = new ArrayList<>();
            String plan = null;
            for (int r = 0; r < RUNS; r++) {
                plan = PgContainerHelper.explainPlanWith(ctx, q, PgContainerHelper.ExplainOptions.ANALYZE_BUFFERS);
                Matcher m = EXEC_TIME.matcher(plan);
                assertThat(m.find()).as("Execution Time in the plan of " + name + ":\n" + plan).isTrue();
                times.add(Double.parseDouble(m.group(1)));
            }
            List<Double> sorted = new ArrayList<>(times);
            Collections.sort(sorted);
            double median = sorted.get(RUNS / 2);
            String verdict = median > THRESHOLD_MS ? "OVER " + THRESHOLD_MS + " ms" : "at or under " + THRESHOLD_MS + " ms";
            String line = "RDR241SPIKE " + group + " | " + name + " | median " + String.format(Locale.ROOT, "%.3f", median)
                + " ms | runs " + times + " | " + verdict;
            emit(line);
            emit("RDR241SPIKE plan of " + name + " (last run, EXPLAIN (ANALYZE, BUFFERS)):\n" + plan);
            decisionLines.add(group + " | " + name + " | " + String.format(Locale.ROOT, "%.3f", median) + " ms | " + verdict);
            return median;
        });
    }

    private void emit(String s) {
        System.out.println(s);
        report.add(s);
    }

    private static List<SelectFieldOrAsterisk> docColumns() {
        return List.of(CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.AUTHOR, CATALOG_DOCUMENTS.YEAR,
            CATALOG_DOCUMENTS.CONTENT_TYPE, CATALOG_DOCUMENTS.FILE_PATH, CATALOG_DOCUMENTS.CORPUS,
            CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.CHUNK_COUNT, CATALOG_DOCUMENTS.HEAD_HASH,
            CATALOG_DOCUMENTS.INDEXED_AT, CATALOG_DOCUMENTS.METADATA, CATALOG_DOCUMENTS.ALIAS_OF, CATALOG_DOCUMENTS.SOURCE_URI);
    }

    private static Condition liveOfTenant(String tenant) {
        return CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.DELETED_AT.isNull());
    }

    /** One page of rows plus the total on every row ({@code count(*) OVER ()}), in tumbler order. */
    private static Select<Record> page(DSLContext ctx, Condition where, int limit, int offset) {
        List<SelectFieldOrAsterisk> cols = new ArrayList<>(docColumns());
        cols.add(DSL.count().over());
        return ctx.select(cols).from(CATALOG_DOCUMENTS).where(where).orderBy(CATALOG_DOCUMENTS.TUMBLER).limit(limit).offset(offset);
    }

    /** The exact-count statement for the same predicate (the second-statement form of S1). */
    private static Select<Record1<Integer>> countOf(DSLContext ctx, Condition where) {
        return ctx.select(DSL.count()).from(CATALOG_DOCUMENTS).where(where);
    }

    private static long fetchCount(DSLContext ctx, Condition where) {
        return ctx.fetchValue(countOf(ctx, where)).longValue();
    }

    // ── statement builders (the shape the routes will run) ──────────────────

    /** Literal contains-pattern: backslash, % and _ escaped as CatalogLinksRepository.likePrefix escapes, wrapped in %. */
    static String containsPattern(String term) {
        return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%";
    }

    static Condition authorContains(String tenant, String term) {
        return liveOfTenant(tenant).and(DSL.lower(CATALOG_DOCUMENTS.AUTHOR)
            .like(DSL.lower(DSL.val(containsPattern(term))), '\\'));
    }

    private static Field<String> metaText(String key) {
        return DSL.jsonbGetAttributeAsText(CATALOG_DOCUMENTS.METADATA, key);
    }

    /** {@code meta.<key>=<value>}: text comparison; an absent key (or JSON null) is SQL NULL and matches nothing. */
    static Condition metaEq(String key, String value) {
        return metaText(key).eq(value);
    }

    /** {@code meta.<key>=<value>&meta_default.<key>=<dflt>}: absent, JSON null and "" read as the default. */
    static Condition metaEqDefault(String key, String value, String dflt) {
        return DSL.coalesce(DSL.nullif(metaText(key), ""), DSL.inline(dflt)).eq(value);
    }

    /** {@code meta_min.<key>=<n>}, the rule of bead nexus-uwggq.2.2, in its written form: regex AND numeric cast. */
    static Condition metaAtLeastAnd(String key, int n) {
        Field<String> t = metaText(key);
        return t.likeRegex("^-?[0-9]+$").and(t.cast(SQLDataType.NUMERIC).ge(BigDecimal.valueOf(n)));
    }

    /**
     * The same rule as route 2.4 runs it: {@code CASE WHEN text ~ '^-?[0-9]+$' THEN text::numeric >= n ELSE false END}.
     * The cast sits inside the THEN branch, which PostgreSQL evaluates only for rows the WHEN accepted, so a non-integer
     * text never reaches it (an AND gives no such order).
     */
    static Condition metaAtLeastCase(String key, int n) {
        Field<String> t = metaText(key);
        Field<Boolean> guarded = DSL.when(t.likeRegex("^-?[0-9]+$"), DSL.field(t.cast(SQLDataType.NUMERIC).ge(BigDecimal.valueOf(n))))
            .otherwise(DSL.inline(false));
        return DSL.condition(guarded);
    }

    private static Condition metaWhere(String tenant, String contentType, Condition meta) {
        Condition c = liveOfTenant(tenant).and(meta);
        return contentType == null ? c : c.and(CATALOG_DOCUMENTS.CONTENT_TYPE.eq(contentType));
    }

    /** The sensitive-locator statement, one SELECT with an IN sub-select for the alias_of leg. */
    static Select<? extends Record> locatorsOr(DSLContext ctx, String tenant, int fetchLimit) {
        var d = CATALOG_DOCUMENTS.as("d");
        var s = CATALOG_DOCUMENTS.as("s");
        var sensitiveTumblers = ctx.select(s.TUMBLER).from(s)
            .where(s.TENANT_ID.eq(tenant).and(DSL.condition(s.SENSITIVE)).and(s.DELETED_AT.isNull()));
        return ctx.select(d.TUMBLER, d.SOURCE_URI, d.FILE_PATH, d.ALIAS_OF, d.PHYSICAL_COLLECTION, DSL.count().over())
            .from(d)
            .where(d.TENANT_ID.eq(tenant).and(d.DELETED_AT.isNull())
                .and(DSL.condition(d.SENSITIVE).or(d.ALIAS_OF.in(sensitiveTumblers))))
            .orderBy(d.TUMBLER).limit(fetchLimit);
    }

    /** The same answer as two legs, UNION (a document both sensitive and an alias of a sensitive one is listed once). */
    static Select<? extends Record> locatorsUnion(DSLContext ctx, String tenant, int fetchLimit) {
        var d = CATALOG_DOCUMENTS.as("d");
        var a = CATALOG_DOCUMENTS.as("a");
        var s = CATALOG_DOCUMENTS.as("s");
        var sensLeg = ctx.select(d.TUMBLER, d.SOURCE_URI, d.FILE_PATH, d.ALIAS_OF, d.PHYSICAL_COLLECTION).from(d)
            .where(d.TENANT_ID.eq(tenant).and(DSL.condition(d.SENSITIVE)).and(d.DELETED_AT.isNull()));
        var aliasLeg = ctx.select(a.TUMBLER, a.SOURCE_URI, a.FILE_PATH, a.ALIAS_OF, a.PHYSICAL_COLLECTION).from(a)
            .join(s).on(s.TENANT_ID.eq(a.TENANT_ID).and(s.TUMBLER.eq(a.ALIAS_OF)))
            .where(a.TENANT_ID.eq(tenant).and(a.DELETED_AT.isNull())
                .and(DSL.condition(s.SENSITIVE)).and(s.DELETED_AT.isNull()));
        var u = sensLeg.union(aliasLeg).asTable("u", "tumbler", "source_uri", "file_path", "alias_of", "physical_collection");
        return ctx.select(u.field("tumbler"), u.field("source_uri"), u.field("file_path"), u.field("alias_of"),
                u.field("physical_collection"), DSL.count().over())
            .from(u).orderBy(u.field("tumbler")).limit(fetchLimit);
    }

    // ── lifecycle ───────────────────────────────────────────────────────────

    @BeforeAll
    void seed() throws Exception {
        pg = PgContainerHelper.start();
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
        scope = new TenantScope(svcDs);

        long t0 = System.nanoTime();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
            Long[] fxIds = new Long[5];   // P3.1: each document names a registered collection, by id
            for (int c = 0; c < 5; c++) {
                PgContainerHelper.insertCollection(seed, TENANT, "knowledge__fx-" + c + "__v1");
                fxIds[c] = dev.nexus.service.db.CollectionIds.require(seed, TENANT, "knowledge__fx-" + c + "__v1");
            }
            OffsetDateTime deletedAt = OffsetDateTime.parse("2026-10-01T12:00:00+00:00");
            int total = Fx.LIVE + Fx.DELETED;
            for (int lo = 0; lo < total; lo += 1000) {
                var ins = seed.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                    CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.AUTHOR, CATALOG_DOCUMENTS.CONTENT_TYPE,
                    CATALOG_DOCUMENTS.FILE_PATH, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.SOURCE_URI,
                    CATALOG_DOCUMENTS.METADATA, CATALOG_DOCUMENTS.ALIAS_OF, CATALOG_DOCUMENTS.SENSITIVE,
                    CATALOG_DOCUMENTS.DELETED_AT, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID);
                var step = ins;
                for (int i = lo; i < Math.min(total, lo + 1000); i++) {
                    step = step.values(TENANT, Fx.tumbler(i), "Doc " + i, Fx.author(i), Fx.contentType(i),
                        "/fx/" + i + ".md", "knowledge__fx-" + (i % 5) + "__v1", "file:///fx/" + i + ".md",
                        JSONB.valueOf(Fx.metadataJson(i)), Fx.aliasOf(i), Fx.sensitive(i), Fx.live(i) ? null : deletedAt,
                        fxIds[i % 5]);
                }
                step.execute();
            }
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENTS);
        }
        try (Connection su = pg.createConnection("")) {
            DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
            emit("RDR241SPIKE postgres: " + seed.fetchValue(DSL.function("version", SQLDataType.CLOB)));
            emit("RDR241SPIKE seeded " + (Fx.LIVE + Fx.DELETED) + " rows in " + (System.nanoTime() - t0) / 1_000_000L + " ms");
        }
        // non-vacuity: the fixture is the shape the bead names
        scope.withTenant(TENANT, ctx -> {
            assertThat(ctx.fetchCount(CATALOG_DOCUMENTS, liveOfTenant(TENANT))).as("live rows").isEqualTo(Fx.LIVE);
            assertThat(ctx.fetchCount(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT).and(CATALOG_DOCUMENTS.DELETED_AT.isNotNull())))
                .as("soft-deleted rows").isEqualTo(Fx.DELETED);
            assertThat(fetchCount(ctx, authorContains(TENANT, "lamport"))).as("live lamport authors (a few hundred)")
                .isBetween(200L, 600L);
            assertThat(ctx.fetchCount(CATALOG_DOCUMENTS, liveOfTenant(TENANT).and(DSL.condition(CATALOG_DOCUMENTS.SENSITIVE))))
                .as("live sensitive rows, about 1%").isEqualTo(500);
            return null;
        });
    }

    @AfterAll
    void stop() throws Exception {
        emit("RDR241SPIKE DECISIONS (threshold " + THRESHOLD_MS + " ms, median of " + RUNS + " after one warm-up):");
        decisionLines.forEach(l -> emit("RDR241SPIKE   " + l));
        Path out = Path.of(System.getProperty("nexus.rdr241.out", "target/rdr241-spike.txt"));
        try {
            if (out.getParent() != null) Files.createDirectories(out.getParent());
            Files.writeString(out, String.join("\n", report) + "\n", StandardCharsets.UTF_8);
        } catch (java.io.IOException e) {
            System.out.println("RDR241SPIKE could not write " + out + ": " + e);
        }
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── 1. by author ────────────────────────────────────────────────────────

    @Test
    void byAuthor() {
        record Case(String term, int offset, long expectCount) {}
        List<Case> cases = new ArrayList<>();
        for (String term : List.of("lamport", "author")) {
            long expect = Fx.count(i -> Fx.authorContains(i, term));
            cases.add(new Case(term, 0, expect));
            cases.add(new Case(term, 40_000, expect));
        }
        // a term with wildcard characters in it: matched literally, so only the "100% Author_Fan" rows
        cases.add(new Case("0% author_f", 0, Fx.count(i -> Fx.authorContains(i, "0% author_f"))));
        // a bare wildcard: escaped, only the rows that hold that character; unescaped, every author (review of
        // 00b3c4902: "0% author_f" alone matched the same 44 rows escaped or not, so it could not catch a lost escape)
        cases.add(new Case("%", 0, Fx.count(i -> Fx.authorContains(i, "%"))));
        cases.add(new Case("_", 0, Fx.count(i -> Fx.authorContains(i, "_"))));
        assertThat(cases.get(0).expectCount).as("lamport matches (non-vacuity)").isBetween(200L, 600L);
        assertThat(cases.get(2).expectCount).as("author matches beyond offset 40,000 (non-vacuity)").isGreaterThan(41_000L);
        assertThat(cases.get(4).expectCount).as("literal-wildcard term matches (non-vacuity)").isGreaterThan(0L);
        long withAuthor = Fx.count(i -> Fx.author(i) != null);
        for (int k = 5; k <= 6; k++) {
            assertThat(cases.get(k).expectCount)
                .as("escaped '%s' matches only rows holding it, far fewer than every author (%d), so a lost escape fails",
                    cases.get(k).term, withAuthor)
                .isGreaterThan(0L).isLessThan(withAuthor / 10);
        }

        for (Case c : cases) {
            Condition where = authorContains(TENANT, c.term);
            String tag = "author~'" + c.term + "'";
            // the page, with count(*) OVER (): rows returned and the window total are checked against the model
            scope.withTenant(TENANT, ctx -> {
                var rows = page(ctx, where, PAGE, c.offset).fetch();
                long expectRows = Math.max(0, Math.min(PAGE, c.expectCount - c.offset));
                assertThat((long) rows.size()).as(tag + " page size at offset " + c.offset).isEqualTo(expectRows);
                if (!rows.isEmpty()) {
                    assertThat(rows.get(0).get(rows.get(0).size() - 1, Long.class)).as(tag + " window count").isEqualTo(c.expectCount);
                }
                assertThat(fetchCount(ctx, where)).as(tag + " count(*)").isEqualTo(c.expectCount);
                return null;
            });
            measure("by-author", tag + " page LIMIT " + PAGE + " OFFSET " + c.offset + " + count(*) OVER ()",
                ctx -> page(ctx, where, PAGE, c.offset));
            if (c.offset == 0) {
                measure("by-author", tag + " count(*) statement", ctx -> countOf(ctx, where));
            }
        }
    }

    // ── 2. by metadata ──────────────────────────────────────────────────────

    @Test
    void byMetadata() {
        record Case(String name, Condition meta, java.util.function.IntPredicate model) {}
        List<Case> cases = List.of(
            new Case("meta.quality_gate_overridden=true", metaEq("quality_gate_overridden", "true"),
                i -> "true".equals(Fx.qgo(i))),
            new Case("meta.extraction_source=mineru&meta_default.extraction_source=file",
                metaEqDefault("extraction_source", "mineru", "file"), i -> Fx.extDefault(i, "file", "mineru")),
            new Case("meta.extraction_source=file&meta_default.extraction_source=file",
                metaEqDefault("extraction_source", "file", "file"), i -> Fx.extDefault(i, "file", "file")),
            new Case("meta_min.miss_count>=2 (CASE form)", metaAtLeastCase("miss_count", 2), i -> Fx.missAtLeast(i, 2)),
            new Case("meta_min.miss_count>=4 (CASE WHEN regex THEN cast>=n ELSE false)", metaAtLeastCase("miss_count", 4), i -> Fx.missAtLeast(i, 4)));
        for (String ct : new String[] {null, "paper"}) {
            for (Case c : cases) {
                long expect = Fx.count(i -> Fx.ctMatches(i, ct) && c.model.test(i));
                assertThat(expect).as(c.name + " matches something (non-vacuity)").isGreaterThan(0L);
                Condition where = metaWhere(TENANT, ct, c.meta);
                String tag = c.name + (ct == null ? "" : " content_type=" + ct);
                scope.withTenant(TENANT, ctx -> {
                    var rows = page(ctx, where, PAGE, 0).fetch();
                    assertThat((long) rows.size()).as(tag + " page size").isEqualTo(Math.min(PAGE, expect));
                    assertThat(rows.get(0).get(rows.get(0).size() - 1, Long.class)).as(tag + " window count").isEqualTo(expect);
                    assertThat(fetchCount(ctx, where)).as(tag + " count(*)").isEqualTo(expect);
                    return null;
                });
                measure("by-meta", tag + " page LIMIT " + PAGE + " + count(*) OVER ()", ctx -> page(ctx, where, PAGE, 0));
                measure("by-meta", tag + " count(*) statement", ctx -> countOf(ctx, where));
            }
        }
    }

    /**
     * The bound exactly as bead nexus-uwggq.2.2 writes it ({@code regex AND numeric cast}). PostgreSQL does not promise
     * the order of AND operands, so a row whose text is not an integer ("n/a", "2.5" are in the fixture) can reach the
     * cast and raise 22P02. This test records which happens and never asserts it: it is a finding for the route bead.
     */
    @Test
    void boundWrittenAsRegexAndCast() {
        long expect = Fx.count(i -> Fx.missAtLeast(i, 2));
        Condition where = metaWhere(TENANT, null, metaAtLeastAnd("miss_count", 2));
        String outcome;
        try {
            long got = scope.withTenant(TENANT, ctx -> fetchCount(ctx, where));
            assertThat(got).as("regex AND cast count, when it runs").isEqualTo(expect);
            outcome = "RAN, count " + got + " (equals the model)";
            measure("by-meta", "meta_min.miss_count>=2 (regex AND cast, as written in nexus-uwggq.2.2) count(*) statement",
                ctx -> countOf(ctx, where));
        } catch (RuntimeException e) {
            outcome = "RAISED " + e.getMessage().lines().findFirst().orElse("");
        }
        emit("RDR241SPIKE meta_min.miss_count>=2 regex AND cast: " + outcome);
        decisionLines.add("by-meta | regex AND cast form: " + outcome);
    }

    // ── 3. sensitive locators ───────────────────────────────────────────────

    @Test
    void sensitiveLocators() {
        long sensitiveLive = Fx.count(Fx::sensitive);
        long aliasLive = Fx.count(i -> {
            String a = Fx.aliasOf(i);
            if (a.isEmpty()) return false;
            for (int j = 0; j < Fx.LIVE; j++) {
                if (Fx.tumbler(j).equals(a)) return Fx.sensitive(j);
            }
            return false;
        });
        assertThat(sensitiveLive).as("live sensitive rows").isEqualTo(500L);
        assertThat(aliasLive).as("live alias rows naming a live sensitive row (about 200)").isEqualTo(200L);
        long expect = sensitiveLive + aliasLive;   // no row is both: sensitive rows carry alias_of ''

        int fetchLimit = 10_001;
        record Shape(String name, java.util.function.BiFunction<DSLContext, Integer, Select<? extends Record>> build) {}
        for (Shape shape : List.of(
                new Shape("OR + IN sub-select", (ctx, lim) -> locatorsOr(ctx, TENANT, lim)),
                new Shape("UNION of two legs", (ctx, lim) -> locatorsUnion(ctx, TENANT, lim)))) {
            scope.withTenant(TENANT, ctx -> {
                var rows = shape.build().apply(ctx, fetchLimit).fetch();
                assertThat((long) rows.size()).as(shape.name() + " rows").isEqualTo(expect);
                assertThat(rows.get(0).get(rows.get(0).size() - 1, Long.class)).as(shape.name() + " window count").isEqualTo(expect);
                // a deleted alias, an alias to a deleted sensitive row and an alias to a non-sensitive row are NOT listed
                var listed = rows.map(r -> r.get(0, String.class));
                assertThat(listed).doesNotContain(Fx.tumbler(50_000 + 3), Fx.tumbler(2501), Fx.tumbler(2502));
                return null;
            });
            measure("sensitive-locators", shape.name() + " LIMIT " + fetchLimit + " + count(*) OVER ()",
                ctx -> shape.build().apply(ctx, fetchLimit));
        }
        // the sensitive half alone, the statement /v1/catalog/sensitive runs today (baseline)
        measure("sensitive-locators", "sensitive leg only (CatalogSensitiveReads.listQuery, baseline)",
            ctx -> dev.nexus.service.db.CatalogSensitiveReads.listQuery(ctx, TENANT, fetchLimit));
    }
}
