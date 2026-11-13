// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.ExportTestClock;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TenantExport;
import dev.nexus.service.db.TenantExport.Column;
import dev.nexus.service.db.TenantExport.ExportClass;
import dev.nexus.service.db.TenantExport.ExportTable;
import dev.nexus.service.db.TenantExportKeys;
import dev.nexus.service.db.TenantExportReads;
import dev.nexus.service.db.TenantExportSessions;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.Name;
import org.jooq.Record;
import org.jooq.SQLDialect;
import org.jooq.Select;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.postgresql.PGConnection;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.ByteArrayInputStream;
import java.math.BigDecimal;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.time.Duration;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Collection;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeSet;
import java.util.UUID;
import java.util.function.Function;

/**
 * The tenant export battery (RDR-248 Phase 1, bead nexus-csayy.4): the routes against a real PostgreSQL with
 * every exported table of the registry seeded for two tenants, every column of it filled in at least one row
 * and left NULL in another. What it proves that the route and session tests do not:
 *
 * <ul>
 *   <li><b>Every table, every column, every type, no other tenant.</b> Each exported table is walked page by page
 *       over HTTP with tenant A's token, each page is loaded by PostgreSQL itself ({@code COPY ... FROM STDIN
 *       (FORMAT binary)} into a temp table of the live table's columns, which the header must name) and the loaded rows are compared, as a
 *       multiset in both directions, with the rows the superuser reads straight from the table. A column the page
 *       drops, a value it mangles, a row it repeats or loses, or a row of tenant B that it carries, is a
 *       difference. The seed itself is checked first: every exported column is non-NULL in some A row, and B has
 *       rows in every table, so neither the comparison nor the isolation check can pass on empty tables.</li>
 *   <li><b>Writes and deletes between pages.</b> The same walk with pages cut at 1,024 bytes (a row to a page for
 *       chunks), with inserts, updates and deletes committed after the first page of chunks, against a reference
 *       taken before the session opened.</li>
 *   <li><b>Types the registry's tables do not hold.</b> A probe table of the other types ({@code smallint},
 *       {@code uuid} as the key, arrays, {@code interval}, {@code inet}, an identity column, a generated column)
 *       is read through the page machinery as a synthetic registry entry.</li>
 *   <li><b>An idle session's snapshot is gone</b>, not only its pool connection.</li>
 * </ul>
 *
 * The routes' own mechanics (headers, whitelist, bounds, tokens) are {@code TenantExportRoutesTest}'s; the held
 * transaction and the key walk are {@code TenantExportSessionsTest}'s.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TenantExportBatteryTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String ROOT = "tok-root-battery-0123456789abcdefghij";
    private static final String A_TOKEN = "tok-a-battery-0123456789abcdefghijkl";
    private static final String B_TOKEN = "tok-b-battery-0123456789abcdefghijkl";
    private static final String A = "t-battery-a";
    private static final String B = "t-battery-b";
    /** The tenant the mid-export-writes test mutates: its own, so no test depends on the order the tests run in. */
    private static final String C = "t-battery-c";
    private static final String C_TOKEN = "tok-c-battery-0123456789abcdefghijkl";
    private static final String BGE = "bge-base-en-v15-768";
    private static final String MINILM = "minilm-l6-v2-384";
    private static final String VOYAGE = "voyage-context-3";
    private static final String PATH = "/v1/tenant/export";
    /** Created and seeded by db/changelog-test/db.changelog-test-export-battery-probe.xml. */
    private static final String PROBE = "export_battery_probe";
    /** The one staging table a comparison loads pages into; created, loaded and dropped per comparison. */
    private static final String STAGE = "export_battery_stage";

    private PostgreSQLContainer<?> pg;
    private HikariDataSource ds;
    private NexusService service;
    private final HttpClient http = TestHttp.client();
    private final List<TenantExportSessions> opened = new ArrayList<>();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.seedServiceToken(dsl, ROOT, TenantConstants.DEFAULT_TENANT,
                dev.nexus.service.db.TokenStore.ROOT_TOKEN_LABEL, dev.nexus.service.db.TokenStore.SCOPE_ROOT, null, null);
            PgContainerHelper.seedServiceToken(dsl, A_TOKEN, A, "tenant-a");
            PgContainerHelper.seedServiceToken(dsl, B_TOKEN, B, "tenant-b");
            PgContainerHelper.seedServiceToken(dsl, C_TOKEN, C, "tenant-c");
            PgContainerHelper.ensureTenantPartitions(dsl, A);
            PgContainerHelper.ensureTenantPartitions(dsl, B);
            PgContainerHelper.ensureTenantPartitions(dsl, C);
            List<String> failures = new ArrayList<>();
            Seed.rows(dsl, A, 3, failures);
            Seed.rows(dsl, B, 1, failures);
            Seed.rows(dsl, C, 3, failures);
            Seed.fillNulls(dsl, A, failures);
            Seed.fillNulls(dsl, B, failures);
            Seed.fillNulls(dsl, C, failures);
            assertThat(failures).as("the battery's seed").isEmpty();
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-export-battery-probe.xml",
                "databasechangelog_test_export_battery_probe", Map.of());
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(8);
        cfg.setAutoCommit(true);
        ds = new HikariDataSource(cfg);
        service = new NexusService(0, ROOT, ds);
        service.start();
        for (String token : List.of(A_TOKEN, B_TOKEN, C_TOKEN)) {   // the filter's first-request ghost sweep, before any test holds a lock
            assertThat(call("GET", "/v1/_whoami", token).statusCode()).isEqualTo(200);
        }
    }

    @AfterAll
    void stopAll() {
        if (service != null) service.stop();
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    @BeforeEach
    @AfterEach
    void freshSessions() {
        opened.forEach(TenantExportSessions::close);
        opened.clear();
        service.exportSessions(new TenantExportSessions(ds, TenantExportSessions.Config.defaults(),
            java.time.Clock.systemUTC()));
    }

    // ── 1. every exported table, every column, no other tenant ───────────────────────────────────

    @Test
    void everyExportedTableRoundTripsEveryColumnAndCarriesNoOtherTenantsRow() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            List<String> problems = new ArrayList<>();
            List<ExportTable> exported = TenantExport.TABLES.stream().filter(ExportTable::exported).toList();
            assertThat(exported).as("the registry's data and derived tables").hasSize(26);

            // the seed is not vacuous: every exported column is non-NULL in some A row and NULL-or-default in the
            // others, and B has rows in every table
            for (ExportTable t : exported) {
                for (LiveColumn c : liveColumns(ctx, t)) {
                    long filled = ctx.select(DSL.count(col(c.name))).from(tbl(t)).where(tenant(A)).fetchOne(0, Long.class);
                    if (filled == 0) problems.add(t.name() + "." + c.name + " is NULL in every seeded row of " + A);
                }
                if (ctx.fetchCount(tbl(t), tenant(B)) == 0) {
                    problems.add(t.name() + " has no row of " + B + ", so the isolation check on it would be vacuous");
                }
            }
            assertThat(problems).as("the seed").isEmpty();

            JsonNode header = open(A_TOKEN);
            String id = header.get("session_id").asText();
            assertThat(header.get("tenant_id").asText()).isEqualTo(A);

            // the header names every exported table in registry order, with exactly the live non-generated columns
            List<String> headerTables = new ArrayList<>();
            header.get("tables").forEach(n -> headerTables.add(n.get("name").asText()));
            assertThat(headerTables).containsExactlyElementsOf(exported.stream().map(ExportTable::name).toList());
            for (JsonNode th : header.get("tables")) {
                ExportTable t = TenantExport.exportable(th.get("name").asText()).orElseThrow();
                List<String> live = liveColumns(ctx, t).stream().map(c -> c.name + " " + norm(c.type)).toList();
                List<String> hdr = new ArrayList<>();
                th.get("columns").forEach(c -> hdr.add(c.get("name").asText() + " " + norm(c.get("type").asText())));
                if (!live.equals(hdr)) problems.add(t.name() + " header columns " + hdr + " differ from the live " + live);
            }

            // the types under test: the battery states what it covers, so a column of a new type fails here
            // until it is seeded, never passes unseen
            TreeSet<String> types = new TreeSet<>();
            long identityColumns = 0;
            for (ExportTable t : exported) {
                for (LiveColumn c : liveColumns(ctx, t)) {
                    types.add(norm(c.type).replaceAll("\\(\\d+(,\\d+)?\\)", "(n)"));
                    if (c.identity) identityColumns++;
                }
            }
            assertThat(types).as("the column types of the exported tables").containsExactlyInAnyOrder(
                "bigint", "bigint[]", "boolean", "bytea", "double precision", "integer", "integer[]", "jsonb",
                "numeric", "text", "timestamp with time zone", "vector(n)");
            assertThat(identityColumns).as("serial or identity columns of the exported tables").isGreaterThan(0);
            // the id the restore reassigns is a serial or identity column of the table, and it is in every page's rows
            for (ExportTable t : exported) {
                if (t.reassignedId() == null) continue;
                assertThat(liveColumns(ctx, t).stream().filter(c -> c.identity).map(c -> c.name).toList())
                    .as(t.name() + "'s reassigned id is its serial column").contains(t.reassignedId());
            }

            Map<String, Walk> walks = walk(A_TOKEN, id, header, null);
            for (JsonNode th : header.get("tables")) {
                ExportTable t = TenantExport.exportable(th.get("name").asText()).orElseThrow();
                compare(su, ctx, t, th, walks.get(t.name()), fields -> DSL.select(fields).from(tbl(t)).where(tenant(A)), A, problems);
            }
            assertThat(problems).as("export of every table of " + A).isEmpty();
            call("DELETE", PATH + "/" + id, A_TOKEN);
        }
    }

    // ── 2. writes and deletes committed between pages ────────────────────────────────────────────

    @Test
    void writesUpdatesAndDeletesCommittedBetweenPagesNeverReachAnyTable() throws Exception {
        service.exportSessions(new TenantExportSessions(ds,
            TenantExportSessions.Config.defaults().withPageBytes(1024), java.time.Clock.systemUTC()));
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            List<String> problems = new ArrayList<>();
            List<ExportTable> exported = TenantExport.TABLES.stream().filter(ExportTable::exported).toList();
            // the reference: tenant C's rows as they are BEFORE the session opens
            for (ExportTable t : exported) {
                ctx.createTemporaryTable(ref(t.name()))
                    .as(DSL.select(liveFields(ctx, t)).from(tbl(t)).where(tenant(C))).execute();
            }
            JsonNode header = open(C_TOKEN);
            String id = header.get("session_id").asText();

            long chunkRowsBefore = ctx.fetchCount(tbl("chunks"), tenant(C));
            Map<String, Walk> walks = walk(C_TOKEN, id, header, () -> mutateC(ctx));
            // the mutation really happened: the live tables differ from the reference now, in both directions
            for (String table : List.of("memory", "chunks", "catalog_meta", "search_telemetry")) {
                ExportTable t = TenantExport.exportable(table).orElseThrow();
                List<Field<?>> cols = liveFields(ctx, t);
                assertThat(ctx.fetchCount(DSL.select(cols).from(tbl(t)).where(tenant(C))
                        .exceptAll(DSL.select(cols).from(DSL.table(ref(table))))))
                    .as(table + " rows written since the reference").isGreaterThan(0);
            }
            for (String table : List.of("memory", "chunks", "catalog_meta", "frecency")) {
                ExportTable t = TenantExport.exportable(table).orElseThrow();
                List<Field<?>> cols = liveFields(ctx, t);
                assertThat(ctx.fetchCount(DSL.select(cols).from(DSL.table(ref(table)))
                        .exceptAll(DSL.select(cols).from(tbl(t)).where(tenant(C)))))
                    .as(table + " reference rows deleted or rewritten since").isGreaterThan(0);
            }

            for (JsonNode th : header.get("tables")) {
                ExportTable t = TenantExport.exportable(th.get("name").asText()).orElseThrow();
                compare(su, ctx, t, th, walks.get(t.name()), fields -> DSL.select(fields).from(DSL.table(ref(t.name()))), C, problems);
            }
            assertThat(problems).as("the export holds the state at the session's open, not what was written after")
                .isEmpty();
            assertThat(walks.get("chunks").pages().size()).as("chunks span many pages at 1,024 bytes")
                .isGreaterThan((int) (chunkRowsBefore / 2));

            // a page asked for again from the same key, after the writes, is the same bytes: a cut page costs one page
            List<byte[]> chunkPages = walks.get("chunks").pages();
            String cursor = walks.get("chunks").lastKeys().get(0);
            var again = rowsBytes(C_TOKEN, id, rowsQuery("chunks", cursor));
            assertThat(again.statusCode()).isEqualTo(200);
            assertThat(again.body()).as("the second page of chunks, asked for again").isEqualTo(chunkPages.get(1));
            call("DELETE", PATH + "/" + id, C_TOKEN);
        }
    }

    /** Inserts, updates and deletes in tables the walk has yet to read and in chunks past the first page. */
    private static void mutateC(DSLContext ctx) {
        try {
            Field<String> title = col("title", String.class);
            ctx.deleteFrom(tbl("memory")).where(tenant(C))
                .and(title.eq(DSL.field(DSL.select(DSL.min(title)).from(tbl("memory")).where(tenant(C))))).execute();
            ctx.insertInto(tbl("memory"), col("tenant_id"), col("project"), col("title"), col("content"), col("timestamp"))
                .values(C, "p", "written-after-open", "x", DSL.currentOffsetDateTime()).execute();
            ctx.update(tbl("memory")).set(col("content", String.class), "rewritten after open").where(tenant(C))
                .and(title.eq(DSL.field(DSL.select(DSL.max(title)).from(tbl("memory"))
                    .where(tenant(C)).and(title.ne("written-after-open"))))).execute();
            Field<String> key = col("key", String.class);
            ctx.deleteFrom(tbl("catalog_meta")).where(tenant(C))
                .and(key.eq(DSL.field(DSL.select(DSL.max(key)).from(tbl("catalog_meta")).where(tenant(C))))).execute();
            ctx.insertInto(tbl("catalog_meta"), col("tenant_id"), col("key"), col("value"))
                .values(C, "written-after-open", "v").execute();
            ctx.deleteFrom(tbl("frecency")).where(tenant(C)).execute();
            ctx.insertInto(tbl("search_telemetry"), col("tenant_id"), col("ts"), col("query_hash"), col("collection"),
                    col("raw_count"), col("kept_count"))
                .values(C, DSL.currentOffsetDateTime(), "after-open", "c", 1, 1).execute();
            // chunks: delete every chunk no manifest or topic row holds, and write new ones on both sides of the key space
            Table<?> c = tbl("chunks").as("c");
            Table<?> m = tbl("catalog_document_chunks").as("m");
            Table<?> a = tbl("topic_assignments").as("a");
            Table<?> o = tbl("chunk_orphaned_at").as("o");
            ctx.deleteFrom(c).where(DSL.field(DSL.name("c", "tenant_id"), String.class).eq(C))
                .andNotExists(DSL.selectOne().from(m).where(same(m, "tenant_id", "tenant_id"), same(m, "collection", "collection"),
                    same(m, "chash", "chash"), same(m, "embedding_model", "embedding_model")))
                .andNotExists(DSL.selectOne().from(a).where(same(a, "tenant_id", "tenant_id"), same(a, "source_collection", "collection"),
                    same(a, "doc_id", "chash"), same(a, "embedding_model", "embedding_model")))
                .andNotExists(DSL.selectOne().from(o).where(same(o, "tenant_id", "tenant_id"), same(o, "collection", "collection"),
                    same(o, "chash", "chash"), same(o, "embedding_model", "embedding_model")))
                .execute();
            for (String h : List.of("00", "ff")) {
                byte[] chash = new byte[32];
                java.util.Arrays.fill(chash, (byte) Integer.parseInt(h, 16));
                ctx.insertInto(tbl("chunks"), col("tenant_id"), col("collection"), col("chash"), col("embedding_model"),
                        col("chunk_text"), embedding(768))
                    .values(C, Seed.collection(C, 768), chash, BGE, "written after open", Seed.vector(768, 5)).execute();
            }
        } catch (RuntimeException e) {
            throw new IllegalStateException("the battery's mid-export writes failed", e);
        }
    }

    /** {@code alias.column = c.chunkColumn}, for the NOT EXISTS checks of {@link #mutateC}. */
    private static Condition same(Table<?> alias, String column, String chunkColumn) {
        return DSL.field(DSL.name(alias.getName(), column)).eq(DSL.field(DSL.name("c", chunkColumn)));
    }

    // ── 3. the types the registry's tables do not hold ───────────────────────────────────────────

    @Test
    void everyOtherColumnTypeRoundTripsThroughThePageMachineryWithAUuidKeyAndAnIdentityColumn() throws Exception {
        ExportTable probe = new ExportTable("nexus", PROBE, ExportClass.DATA, null,
            List.of(new Column("tenant_id", "text"), new Column("k", "uuid")), null, List.of(), Map.of(), java.util.Set.of(),
            java.util.Set.of(), java.util.Set.of());
        var sessions = new TenantExportSessions(ds,
            TenantExportSessions.Config.defaults().withPageBytes(1024).withSweepInterval(Duration.ofHours(1)),
            java.time.Clock.systemUTC());
        opened.add(sessions);
        var session = sessions.open(A).session();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            List<LiveColumn> cols = liveColumnsOf(ctx, "nexus", PROBE);
            assertThat(cols.stream().map(c -> norm(c.type)).toList()).as("the probe's types").contains(
                "smallint", "uuid", "bigint[]", "text[]", "interval", "inet", "real", "double precision", "numeric(12,4)",
                "date", "time without time zone", "timestamp without time zone", "timestamp with time zone", "json",
                "jsonb", "bytea", "boolean", "vector(3)", "bit(5)");
            assertThat(cols.stream().filter(c -> c.identity).map(c -> c.name).toList()).containsExactly("id");
            assertThat(cols.stream().map(c -> c.name).toList()).as("the generated column is not read").doesNotContain("g");

            List<byte[]> pages = new ArrayList<>();
            String after = null;
            TenantExportReads.Page page;
            long tableRows = -1;
            do {
                final String cursor = after;
                page = sessions.withPage(session, s -> sessions.reads().page(s.snapshotId(), A, probe,
                    cursor == null ? null : TenantExportKeys.binds(probe.primaryKey(), TenantExportKeys.decode(cursor, probe.primaryKey())),
                    sessions.config().pageBytes(), sessions.config().pageMaxRows()));
                pages.add(page.body());
                if (after == null) tableRows = page.tableRows();
                assertThat(page.lastKey()).as("the probe page after " + after + " advances").isNotEqualTo(after);
                assertThat(pages.size()).as("probe pages walked").isLessThan(500);
                after = page.lastKey();
            } while (!page.done());
            assertThat(tableRows).as("the probe's rows of " + A).isEqualTo(7);
            assertThat(pages.size()).as("a row of the uuid-keyed probe to a page").isGreaterThanOrEqualTo(5);

            List<String> problems = new ArrayList<>();
            stageAndCompare(su, ctx, PROBE, tbl(PROBE), cols, pages, 7,
                fields -> DSL.select(fields).from(tbl(PROBE)).where(tenant(A)), A, problems);
            assertThat(problems).isEmpty();
            // the page is ordered by the uuid key: the keyset walk of a uuid key lost and repeated nothing
            assertThat(ctx.select(DSL.countDistinct(col("k"))).from(tbl(PROBE)).where(tenant(A)).fetchOne(0, Long.class))
                .isEqualTo(7);
        }
    }

    // ── 4. an idle session's snapshot is released, not only its connection ───────────────────────

    @Test
    void anIdleSessionIsClosedAndItsSnapshotCanNoLongerBeImportedNorItsBackendFound() throws Exception {
        var clock = new ExportTestClock(Instant.parse("2026-11-12T00:00:00Z"));
        var sessions = new TenantExportSessions(ds,
            TenantExportSessions.Config.defaults().withSweepInterval(Duration.ofHours(1)), clock);
        opened.add(sessions);
        long others;
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            others = heldSnapshots(DSL.using(su, SQLDialect.POSTGRES));   // sessions of this class's other tests, if one leaked: the count below is relative to it
        }
        var session = sessions.open(A).session();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(heldSnapshots(ctx)).as("the open session holds its snapshot").isEqualTo(others + 1);
            assertThat(sessions.reads().page(session.snapshotId(), A, TenantExport.exportable("memory").orElseThrow(), null,
                1 << 20, 100_000).rows()).isGreaterThan(0);

            clock.advance(Duration.ofMinutes(10).plusSeconds(1));
            assertThat(sessions.sweep()).as("closed at the idle bound").isEqualTo(1);
            assertThat(heldSnapshots(ctx)).as("the snapshot's transaction ended").isEqualTo(others);
            org.assertj.core.api.Assertions.assertThatThrownBy(() -> sessions.reads().page(session.snapshotId(), A,
                    TenantExport.exportable("memory").orElseThrow(), null, 1 << 20, 100_000))
                .as("the snapshot is released").isInstanceOf(TenantExportReads.SnapshotGoneException.class);
        }
    }

    /** Backends idle in a transaction that exported a snapshot: the export sessions' held transactions. */
    private static long heldSnapshots(DSLContext ctx) {
        return ctx.fetchCount(DSL.table(DSL.name("pg_stat_activity")),
            DSL.field(DSL.name("state"), String.class).eq("idle in transaction")
                .and(DSL.field(DSL.name("query"), String.class).likeIgnoreCase("%pg_export_snapshot%")));
    }

    // ── the walk, the stage and the comparison ───────────────────────────────────────────────────

    /** One table's pages as the routes served them, the table's row count from its first page, each page's last key. */
    private record Walk(List<byte[]> pages, long tableRows, List<String> lastKeys, long rowsHeaderSum) {
    }

    /**
     * Every table of the header, page by page, each page checked against its own SHA-256 header; {@code afterFirstChunksPage}
     * runs once, between the first and second page of {@code chunks}.
     */
    private Map<String, Walk> walk(String token, String id, JsonNode header, Runnable afterFirstChunksPage) throws Exception {
        Map<String, Walk> out = new LinkedHashMap<>();
        boolean ran = afterFirstChunksPage == null;
        for (JsonNode th : header.get("tables")) {
            String table = th.get("name").asText();
            List<byte[]> pages = new ArrayList<>();
            List<String> keys = new ArrayList<>();
            String after = null;
            long tableRows = -1;
            long rowsSum = 0;
            while (true) {
                var r = rowsBytes(token, id, rowsQuery(table, after));
                assertThat(r.statusCode()).as(table + " page after " + after + ": " + new String(r.body(), StandardCharsets.UTF_8)).isEqualTo(200);
                assertThat(sha256Hex(r.body())).as(table + " page hash").isEqualTo(h(r, "X-Nexus-Export-Sha256"));
                pages.add(r.body());
                rowsSum += Long.parseLong(h(r, "X-Nexus-Export-Rows"));
                if (after == null) tableRows = Long.parseLong(h(r, "X-Nexus-Export-Table-Rows"));
                if (h(r, "X-Nexus-Export-Last-Key") != null) keys.add(h(r, "X-Nexus-Export-Last-Key"));
                if ("chunks".equals(table) && !ran && pages.size() == 1) {
                    afterFirstChunksPage.run();
                    ran = true;
                }
                if ("true".equals(h(r, "X-Nexus-Export-Done"))) break;
                String next = h(r, "X-Nexus-Export-Last-Key");
                assertThat(next).as(table + " a page that is not the last names its last key").isNotNull();
                assertThat(next).as(table + " the page after " + after + " advances").isNotEqualTo(after);
                assertThat(pages.size()).as(table + " pages walked (a walk that never ends is a failure)").isLessThan(500);
                after = next;
            }
            out.put(table, new Walk(pages, tableRows, keys, rowsSum));
        }
        return out;
    }

    /** Loads the walk's pages into the stage and compares them with the reference; the header must name the live columns. */
    private static void compare(Connection su, DSLContext ctx, ExportTable t, JsonNode th, Walk w,
                                Function<List<Field<?>>, Select<Record>> ref, String tenant, List<String> problems) throws Exception {
        List<LiveColumn> cols = liveColumns(ctx, t);
        List<String> hdr = new ArrayList<>();
        th.get("columns").forEach(c -> hdr.add(c.get("name").asText()));
        if (!hdr.equals(cols.stream().map(LiveColumn::name).toList())) {
            problems.add(t.name() + " header columns " + hdr + " are not the live columns, so its pages cannot be staged");
            return;
        }
        long refRows = ctx.fetchCount(ref.apply(fields(cols)));
        if (w.tableRows() != refRows) problems.add(t.name() + " first page says " + w.tableRows() + " rows, the reference has " + refRows);
        if (w.rowsHeaderSum() != w.tableRows()) problems.add(t.name() + " pages' row headers sum to " + w.rowsHeaderSum() + ", not " + w.tableRows());
        stageAndCompare(su, ctx, t.name(), tbl(t), cols, w.pages(), refRows, ref, tenant, problems);
    }

    /**
     * Creates the stage with {@code source}'s columns {@code cols} (types included), loads every page into it with
     * PostgreSQL's own binary COPY, and compares it with the reference as a multiset in both directions.
     */
    private static void stageAndCompare(Connection su, DSLContext ctx, String label, Table<?> source, List<LiveColumn> cols,
                                        List<byte[]> pages, long expectedRows, Function<List<Field<?>>, Select<Record>> ref,
                                        String tenant, List<String> problems) throws Exception {
        ctx.createTemporaryTable(DSL.name(STAGE)).as(DSL.select(fields(cols)).from(source).where(DSL.falseCondition())).execute();
        Table<?> stage = DSL.table(DSL.name(STAGE));
        try {
            for (byte[] page : pages) {
                // the one SQL string of this file: COPY ... FROM STDIN has no jOOQ form (SqlStringLiteralGateTest EXEMPT)
                su.unwrap(PGConnection.class).getCopyAPI().copyIn("COPY export_battery_stage FROM STDIN (FORMAT binary)",
                    new ByteArrayInputStream(page));
            }
            // json has no equality operator: its text is what is compared
            List<Field<?>> list = cols.stream().<Field<?>>map(c -> c.type.equals("json")
                ? col(c.name).cast(SQLDataType.CLOB).as(c.name) : col(c.name)).toList();
            long staged = ctx.fetchCount(stage);
            if (staged != expectedRows) problems.add(label + ": " + staged + " rows loaded from the pages, expected " + expectedRows);
            long onlyExported = ctx.fetchCount(DSL.select(list).from(stage).exceptAll(ref.apply(list)));
            long onlyLive = ctx.fetchCount(ref.apply(list).exceptAll(DSL.select(list).from(stage)));
            if (onlyExported != 0 || onlyLive != 0) {
                problems.add(label + ": " + onlyExported + " exported rows are not in the reference, " + onlyLive
                    + " reference rows are not in the export");
            }
            if (cols.stream().anyMatch(c -> c.name.equals("tenant_id"))) {
                long foreign = ctx.fetchCount(stage, col("tenant_id", String.class).ne(tenant));
                if (foreign != 0) problems.add(label + ": " + foreign + " rows of another tenant in the export");
            }
        } finally {
            ctx.dropTable(DSL.name(STAGE)).execute();
        }
    }

    // ── catalog and name helpers ─────────────────────────────────────────────────────────────────

    private record LiveColumn(String name, String type, boolean identity) {
    }

    private static List<LiveColumn> liveColumns(DSLContext ctx, ExportTable t) {
        return liveColumnsOf(ctx, t.schema(), t.table());
    }

    /** The table's non-generated columns in attribute order, read from pg_catalog, independently of the engine's own query. */
    private static List<LiveColumn> liveColumnsOf(DSLContext ctx, String schema, String table) {
        Table<?> a = DSL.table(DSL.name("pg_catalog", "pg_attribute")).as("a");
        Table<?> c = DSL.table(DSL.name("pg_catalog", "pg_class")).as("c");
        Table<?> n = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
        Table<?> d = DSL.table(DSL.name("pg_catalog", "pg_attrdef")).as("d");
        Field<Object> attrelid = DSL.field(DSL.name("a", "attrelid"));
        Field<Short> attnum = DSL.field(DSL.name("a", "attnum"), Short.class);
        Field<Object> adrelid = DSL.field(DSL.name("d", "adrelid"));
        Field<String> name = DSL.field(DSL.name("a", "attname")).cast(SQLDataType.CLOB);
        Field<String> type = DSL.function("format_type", String.class, DSL.field(DSL.name("a", "atttypid")),
            DSL.field(DSL.name("a", "atttypmod")));
        Condition serial = DSL.field(DSL.name("a", "attidentity")).cast(SQLDataType.CLOB).ne("")
            .or(DSL.exists(DSL.selectOne().from(d).where(adrelid.eq(attrelid))
                .and(DSL.field(DSL.name("d", "adnum"), Short.class).eq(attnum))
                .and(DSL.function("pg_get_expr", String.class, DSL.field(DSL.name("d", "adbin")), adrelid).like("nextval(%"))));
        return ctx.select(name, type, DSL.field(serial))
            .from(a)
            .join(c).on(attrelid.eq(DSL.field(DSL.name("c", "oid"))))
            .join(n).on(DSL.field(DSL.name("c", "relnamespace")).eq(DSL.field(DSL.name("n", "oid"))))
            .where(DSL.field(DSL.name("n", "nspname")).cast(SQLDataType.CLOB).eq(schema))
            .and(DSL.field(DSL.name("c", "relname")).cast(SQLDataType.CLOB).eq(table))
            .and(attnum.gt((short) 0))
            .and(DSL.field(DSL.name("a", "attisdropped"), Boolean.class).isFalse())
            .and(DSL.field(DSL.name("a", "attgenerated")).cast(SQLDataType.CLOB).eq(""))
            .orderBy(attnum)
            .fetch(r -> new LiveColumn(r.value1(), r.value2(), Boolean.TRUE.equals(r.value3())));
    }

    private static List<Field<?>> fields(List<LiveColumn> cols) {
        return cols.stream().<Field<?>>map(c -> col(c.name)).toList();
    }

    private static List<Field<?>> liveFields(DSLContext ctx, ExportTable t) {
        return fields(liveColumns(ctx, t));
    }

    private static String norm(String type) {
        return type.replace("nexus.", "");
    }

    private static Table<Record> tbl(ExportTable t) {
        return DSL.table(DSL.name(t.schema(), t.table()));
    }

    private static Table<Record> tbl(String table) {
        return DSL.table(DSL.name("nexus", table));
    }

    private static Name ref(String table) {
        return DSL.name("ref_" + table);
    }

    private static Field<Object> col(String name) {
        return DSL.field(DSL.name(name));
    }

    private static <T> Field<T> col(String name, Class<T> type) {
        return DSL.field(DSL.name(name), type);
    }

    private static Condition tenant(String tenant) {
        return col("tenant_id", String.class).eq(tenant);
    }

    private static Field<Vector> embedding(int dim) {
        return VectorSeeds.chunkEmbedding(dim);
    }

    // ── HTTP ─────────────────────────────────────────────────────────────────────────────────────

    private String url(String path) {
        return "http://127.0.0.1:" + service.getPort() + path;
    }

    private HttpResponse<String> call(String method, String path, String token) throws Exception {
        var b = TestHttp.request(url(path)).method(method, HttpRequest.BodyPublishers.noBody());
        if (token != null) b.header("Authorization", "Bearer " + token);
        return http.send(b.build(), HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<byte[]> rowsBytes(String token, String id, String query) throws Exception {
        var req = TestHttp.request(url(PATH + "/" + id + "/rows" + query)).header("Authorization", "Bearer " + token).GET().build();
        return http.send(req, HttpResponse.BodyHandlers.ofByteArray());
    }

    private JsonNode open(String token) throws Exception {
        var r = call("POST", PATH, token);
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        return MAPPER.readTree(r.body());
    }

    private static String rowsQuery(String table, String after) {
        String q = "?table=" + java.net.URLEncoder.encode(table, StandardCharsets.UTF_8);
        return after == null ? q : q + "&after=" + java.net.URLEncoder.encode(after, StandardCharsets.UTF_8);
    }

    private static String h(HttpResponse<?> r, String name) {
        return r.headers().firstValue(name).orElse(null);
    }

    private static String sha256Hex(byte[] body) throws Exception {
        return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(body));
    }

    // ── the seed ─────────────────────────────────────────────────────────────────────────────────

    /**
     * Two or more rows in every exported table for a tenant, then {@link #fillNulls}: the first row of each table gets
     * every nullable column filled, the others keep their NULLs and defaults. {@code scale} is the number of chunks per
     * model collection, so tenant A's chunks span many pages and B's are few.
     */
    static final class Seed {
        private Seed() {
        }

        private static final OffsetDateTime TS = OffsetDateTime.parse("2026-11-12T10:15:30.123456Z");

        static String collection(String tenant, int dim) {
            String tag = tenant.replace("-", "");
            return switch (dim) {
                case 384 -> "knowledge__" + tag + "384__" + MINILM + "__v1";
                case 768 -> "knowledge__" + tag + "768__" + BGE + "__v1";
                default -> "knowledge__" + tag + "1024__" + VOYAGE + "__v1";
            };
        }

        static Vector vector(int dim, int seed) {
            float[] f = new float[dim];
            for (int j = 0; j < dim; j++) f[j] = ((j * 31 + seed * 17) % 97) / 97.0f;
            return Vector.of(f);
        }

        private static String model(int dim) {
            return dim == 768 ? BGE : dim == 384 ? MINILM : VOYAGE;
        }

        private static void run(List<String> failures, String what, Runnable r) {
            try {
                r.run();
            } catch (RuntimeException e) {
                failures.add(what + ": " + String.valueOf(e.getMessage()).replace('\n', ' '));
            }
        }

        /** One INSERT of {@code rows} into {@code nexus.<table>}; a value that is a field goes in as it is, any other is bound by its Java type. */
        private static void insert(DSLContext ctx, String table, List<String> columns, Object[]... rows) {
            insert(ctx, table, columns.stream().<Field<?>>map(TenantExportBatteryTest::col).toList(), rows);
        }

        private static void insert(DSLContext ctx, String table, Collection<? extends Field<?>> columns, Object[]... rows) {
            var step = ctx.insertInto(tbl(table), columns);
            for (Object[] row : rows) {
                step.values(java.util.Arrays.stream(row).<Field<?>>map(v -> v instanceof Field<?> f ? f : DSL.val(v)).toList());
            }
            step.execute();
        }

        private static List<byte[]> chashes(DSLContext ctx, String t, String coll, boolean descending, int limit) {
            Field<byte[]> chash = col("chash", byte[].class);
            return ctx.select(chash).from(tbl("chunks")).where(tenant(t)).and(col("collection", String.class).eq(coll))
                .orderBy(descending ? chash.desc() : chash.asc()).limit(limit).fetch(chash);
        }

        private static List<Long> ids(DSLContext ctx, String table, String t) {
            Field<Long> id = col("id", Long.class);
            return ctx.select(id).from(tbl(table)).where(tenant(t)).orderBy(id).fetch(id);
        }

        static void rows(DSLContext ctx, String t, int scale, List<String> failures) {
            String c768 = collection(t, 768), c384 = collection(t, 384), c1024 = collection(t, 1024);
            Field<OffsetDateTime> now = DSL.currentOffsetDateTime();
            run(failures, "catalog_owners", () -> insert(ctx, "catalog_owners", List.of("tenant_id", "tumbler_prefix", "name", "owner_type"),
                new Object[] {t, "1.1", "owner-" + t, "curator"}, new Object[] {t, "1.2", "owner2-" + t, "repo"}));
            run(failures, "catalog_collections", () -> {
                PgContainerHelper.insertCollection(ctx, t, c768, BGE);
                PgContainerHelper.insertCollection(ctx, t, c384, MINILM);
                PgContainerHelper.insertCollection(ctx, t, c1024, VOYAGE);
            });
            run(failures, "embedding_profile", () -> insert(ctx, "embedding_profile", List.of("tenant_id", "content_type", "embedding_model", "dimension"),
                new Object[] {t, "knowledge", BGE, 768}, new Object[] {t, "code", VOYAGE, 1024}));
            run(failures, "catalog_documents", () -> {
                for (int i = 1; i <= 3; i++) {
                    insert(ctx, "catalog_documents", List.of("tenant_id", "tumbler", "title", "physical_collection"),
                        new Object[] {t, "1.1." + i, "doc " + i + " of " + t, c768});
                }
            });
            run(failures, "catalog_meta", () -> insert(ctx, "catalog_meta", List.of("tenant_id", "key", "value"),
                new Object[] {t, "k1", "v1"}, new Object[] {t, "k2", DSL.castNull(SQLDataType.CLOB)}, new Object[] {t, "it's", "quoted"}));
            run(failures, "catalog_sensitivity_changes", () -> insert(ctx, "catalog_sensitivity_changes",
                List.of("tenant_id", "tumbler", "old", "new", "reason", "actor"),
                new Object[] {t, "1.1.1", false, true, "r", "a"}, new Object[] {t, "1.1.2", true, false, "r2", "a2"}));
            run(failures, "chunks", () -> {
                for (int dim : List.of(768, 384, 1024)) {
                    String coll = collection(t, dim);
                    int n = dim == 768 ? scale * 8 : scale * 3;
                    List<Field<?>> columns = List.of(col("tenant_id"), col("collection"), col("chash"), col("embedding_model"),
                        col("chunk_text"), VectorSeeds.chunkEmbedding(dim));
                    for (int i = 0; i < n; i++) {
                        String text = "chunk " + dim + "-" + i + " of " + t + " é中";
                        insert(ctx, "chunks", columns, new Object[] {t, coll, sha256(t + text), model(dim), text,
                            DSL.val(vector(dim, i), VectorSeeds.chunkEmbedding(dim))});
                    }
                }
            });
            run(failures, "catalog_document_chunks", () -> {
                List<byte[]> chashes = chashes(ctx, t, c768, false, 4);
                for (int i = 0; i < chashes.size(); i++) {
                    insert(ctx, "catalog_document_chunks",
                        List.of("tenant_id", "doc_id", "position", "chash", "chunk_index", "collection", "embedding_model"),
                        new Object[] {t, "1.1." + (i % 2 + 1), i, chashes.get(i), i, c768, BGE});
                }
            });
            run(failures, "chunk_orphaned_at", () -> {
                for (byte[] chash : chashes(ctx, t, c768, true, 2)) {
                    insert(ctx, "chunk_orphaned_at", List.of("tenant_id", "collection", "chash", "orphaned_at", "embedding_model"),
                        new Object[] {t, c768, chash, now, BGE});
                }
            });
            run(failures, "catalog_links", () -> insert(ctx, "catalog_links", List.of("tenant_id", "from_tumbler", "to_tumbler", "link_type", "created_by"),
                new Object[] {t, "1.1.1", "1.1.2", "cites", "a"}, new Object[] {t, "1.1.2", "1.1.3", "relates", "b"}));
            run(failures, "document_aspects", () -> insert(ctx, "document_aspects",
                List.of("tenant_id", "collection", "source_path", "extracted_at", "model_version", "extractor_name", "source_uri", "doc_id"),
                new Object[] {t, c768, "p1", now, "m", "e", "u1", "1.1.1"}, new Object[] {t, c768, "p2", now, "m", "e", "u2", "1.1.2"}));
            run(failures, "document_highlights", () -> insert(ctx, "document_highlights",
                List.of("tenant_id", "doc_id", "source_uri", "collection", "ingested_at"),
                new Object[] {t, "1.1.1", "u1", c768, now}, new Object[] {t, "1.1.2", "u2", c768, now}));
            run(failures, "memory", () -> {
                for (int i = 0; i < 5; i++) {
                    insert(ctx, "memory", List.of("tenant_id", "project", "title", "content", "timestamp"),
                        new Object[] {t, "p", "title-" + i, "content " + i, now});
                }
            });
            run(failures, "memory_summaries", () -> {
                List<Long> ids = ids(ctx, "memory", t);
                insert(ctx, "memory_summaries", List.of("tenant_id", "project", "content", "source_ids", "model"),
                    new Object[] {t, "p", "summary", new Long[] {ids.get(0), ids.get(1)}, "m"},
                    new Object[] {t, "p", "summary 2", new Long[] {ids.get(1)}, "m"});
            });
            run(failures, "plans", () -> insert(ctx, "plans", List.of("tenant_id", "query", "plan_json", "created_at", "verb"),
                new Object[] {t, "q1", JSONB.valueOf("{\"steps\": []}"), now, "research"},
                new Object[] {t, "q2", JSONB.valueOf("{}"), now, "analyze"}));
            run(failures, "nx_answer_runs", () -> insert(ctx, "nx_answer_runs", List.of("tenant_id", "question", "created_at"),
                new Object[] {t, "question 1", now}, new Object[] {t, "question 2", now}));
            run(failures, "nx_answer_steps", () -> {
                long run = ids(ctx, "nx_answer_runs", t).get(0);
                insert(ctx, "nx_answer_steps", List.of("run_id", "tenant_id", "step_index", "operator", "source", "elapsed_ms", "ok"),
                    new Object[] {run, t, 0, "extract", "llm", 5, true}, new Object[] {run, t, 1, "rank", "sql", 6, false});
            });
            run(failures, "topics", () -> insert(ctx, "topics", List.of("tenant_id", "label", "collection", "created_at"),
                new Object[] {t, "topic 1", c768, now}, new Object[] {t, "topic 2", c768, now}, new Object[] {t, "topic 3", c768, now}));
            run(failures, "topic_assignments", () -> {
                List<Long> topics = ids(ctx, "topics", t);
                List<byte[]> chashes = chashes(ctx, t, c768, false, 2);
                for (int i = 0; i < 2; i++) {
                    insert(ctx, "topic_assignments", List.of("tenant_id", "doc_id", "topic_id", "source_collection", "embedding_model"),
                        new Object[] {t, chashes.get(i), topics.get(i), c768, BGE});
                }
            });
            run(failures, "topic_links", () -> {
                List<Long> topics = ids(ctx, "topics", t);
                insert(ctx, "topic_links", List.of("tenant_id", "from_topic_id", "to_topic_id"),
                    new Object[] {t, topics.get(0), topics.get(1)}, new Object[] {t, topics.get(1), topics.get(2)});
            });
            run(failures, "taxonomy_meta", () -> insert(ctx, "taxonomy_meta", List.of("tenant_id", "collection"),
                new Object[] {t, c768}, new Object[] {t, c384}));
            run(failures, "taxonomy_centroids", () -> {
                List<Long> topics = ids(ctx, "topics", t);
                int i = 0;
                for (int dim : List.of(768, 384, 1024)) {
                    List<Field<?>> columns = List.of(col("tenant_id"), col("collection"), col("topic_id"), col("label"),
                        col("embedding_model"), VectorSeeds.centroidEmbedding(dim));
                    insert(ctx, "taxonomy_centroids", columns, new Object[] {t, collection(t, dim), topics.get(i++), "centroid " + dim,
                        model(dim), DSL.val(vector(dim, 3), VectorSeeds.centroidEmbedding(dim))});
                }
            });
            run(failures, "frecency", () -> insert(ctx, "frecency", List.of("tenant_id", "chunk_id"),
                new Object[] {t, hex(t + "f1")}, new Object[] {t, hex(t + "f2")}));
            run(failures, "search_telemetry", () -> insert(ctx, "search_telemetry",
                List.of("tenant_id", "ts", "query_hash", "collection", "raw_count", "kept_count"),
                new Object[] {t, TS, "q1", "c", 1, 1}, new Object[] {t, TS, "q2", "c", 2, 1}));
            run(failures, "relevance_log", () -> insert(ctx, "relevance_log", List.of("tenant_id", "query", "chunk_id", "action", "timestamp"),
                new Object[] {t, "q", hex(t + "r1"), "hit", now}, new Object[] {t, "q", hex(t + "r2"), "miss", now}));
            run(failures, "claude_assisted_remediation_consents", () -> insert(ctx, "claude_assisted_remediation_consents",
                List.of("tenant_id", "scope", "ts", "granted"),
                new Object[] {t, "s", now, true}, new Object[] {t, "s2", now, false}));
        }

        /** Per-column values for a nullable column the generic fill by type cannot choose (a CHECK or a reference). */
        private static Field<?> override(String column, String t) {
            Field<Long> id = col("id", Long.class);
            Field<Long> plan = DSL.field(DSL.select(DSL.min(id)).from(tbl("plans")).where(tenant(t)));
            return switch (column) {
                case "catalog_documents.index_state" -> DSL.val("complete");
                case "nx_answer_runs.plan_id" -> plan;
                case "nx_answer_runs.run_summary" -> DSL.jsonbObject(DSL.key("run").value(DSL.jsonbObject(DSL.key("plan_id").value(plan))));
                case "topics.parent_id" -> DSL.field(DSL.select(DSL.max(id)).from(tbl("topics")).where(tenant(t)));
                default -> null;
            };
        }

        /** The first row of every exported table (by primary key) gets every NULL column filled; the rest keep their NULLs. */
        static void fillNulls(DSLContext ctx, String t, List<String> failures) {
            for (ExportTable table : TenantExport.TABLES) {
                if (!table.exported()) continue;
                run(failures, "fill " + table.name(), () -> {
                    List<Field<Object>> pk = table.primaryKey().stream().map(c -> col(c.name())).toList();
                    Record first = ctx.select(pk).from(tbl(table)).where(tenant(t)).orderBy(pk).limit(1).fetchOne();
                    if (first == null) throw new IllegalStateException("no rows");
                    Condition row = tenant(t);
                    for (int i = 0; i < pk.size(); i++) row = row.and(pk.get(i).eq(first.get(i)));
                    Map<Field<?>, Field<?>> sets = new LinkedHashMap<>();
                    var keys = table.primaryKey().stream().map(Column::name).toList();
                    var fks = table.references().stream().filter(r -> r.foreignKey()).flatMap(r -> r.columns().stream()).toList();
                    for (LiveColumn c : liveColumns(ctx, table)) {
                        String type = norm(c.type());
                        if (c.identity() || keys.contains(c.name()) || c.name().equals("tenant_id") || type.startsWith("vector(")) continue;
                        Field<?> override = override(table.table() + "." + c.name(), t);
                        if (fks.contains(c.name()) && override == null) continue;
                        Field<?> value = override != null ? override : DSL.val(valueFor(type, table.table() + "." + c.name()));
                        sets.put(col(c.name()), DSL.coalesce(col(c.name()), value));
                    }
                    if (!sets.isEmpty()) ctx.update(tbl(table)).set(sets).where(row).execute();
                });
            }
        }

        private static Object valueFor(String type, String where) {
            return switch (type) {
                case "text" -> "filled " + where + " é";
                case "bigint" -> 9007199254740993L;
                case "integer" -> 42;
                case "smallint" -> (short) 7;
                case "boolean" -> true;
                case "double precision" -> 0.1234567890123;
                case "numeric" -> new BigDecimal("12.3456789");
                case "timestamp with time zone" -> TS;
                case "bytea" -> new byte[] {0, (byte) 0xff, 0x10};
                case "jsonb" -> JSONB.valueOf("{\"k\": [1, 2, {\"n\": null}], \"s\": \"v\"}");
                case "bigint[]" -> new Long[] {5L, Long.MAX_VALUE};
                case "integer[]" -> new Integer[] {1, 2, 3};
                case "uuid" -> UUID.nameUUIDFromBytes(where.getBytes(StandardCharsets.UTF_8));
                default -> throw new IllegalStateException("the battery has no value for the column type " + type + " of " + where);
            };
        }

        private static String hex(String text) {
            return HexFormat.of().formatHex(sha256(text));
        }

        private static byte[] sha256(String text) {
            try {
                return MessageDigest.getInstance("SHA-256").digest(text.getBytes(StandardCharsets.UTF_8));
            } catch (java.security.NoSuchAlgorithmException e) {
                throw new IllegalStateException(e);
            }
        }
    }
}
