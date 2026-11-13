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
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.postgresql.PGConnection;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.ByteArrayInputStream;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeSet;
import java.util.function.Function;

/**
 * The tenant export battery (RDR-248 Phase 1, bead nexus-csayy.4): the routes against a real PostgreSQL with
 * every exported table of the registry seeded for two tenants, every column of it filled in at least one row
 * and left NULL in another. What it proves that the route and session tests do not:
 *
 * <ul>
 *   <li><b>Every table, every column, every type, no other tenant.</b> Each exported table is walked page by page
 *       over HTTP with tenant A's token, each page is loaded by PostgreSQL itself ({@code COPY ... FROM STDIN
 *       (FORMAT binary)} into a temp table whose columns are the HEADER's) and the loaded rows are compared, as a
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
    private static final String PROBE = "export_battery_probe";

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
            Seed.tenant(dsl, A, 3, failures);
            Seed.tenant(dsl, B, 1, failures);
            Seed.tenant(dsl, C, 3, failures);
            Seed.fillNulls(dsl, A, failures);
            Seed.fillNulls(dsl, B, failures);
            Seed.fillNulls(dsl, C, failures);
            createProbe(su);
            assertThat(failures).as("the battery's seed").isEmpty();
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
            List<String> problems = new ArrayList<>();
            List<ExportTable> exported = TenantExport.TABLES.stream().filter(ExportTable::exported).toList();
            assertThat(exported).as("the registry's data and derived tables").hasSize(26);

            // the seed is not vacuous: every exported column is non-NULL in some A row and NULL-or-default in the
            // others, and B has rows in every table
            for (ExportTable t : exported) {
                for (LiveColumn c : liveColumns(su, t)) {
                    long filled = scalar(su, "SELECT count(" + q(c.name) + ") FROM " + qn(t) + " WHERE tenant_id = '" + A + "'");
                    if (filled == 0) problems.add(t.name() + "." + c.name + " is NULL in every seeded row of " + A);
                }
                if (scalar(su, "SELECT count(*) FROM " + qn(t) + " WHERE tenant_id = '" + B + "'") == 0) {
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
                List<String> live = liveColumns(su, t).stream().map(c -> c.name + " " + norm(c.type)).toList();
                List<String> hdr = new ArrayList<>();
                th.get("columns").forEach(c -> hdr.add(c.get("name").asText() + " " + norm(c.get("type").asText())));
                if (!live.equals(hdr)) problems.add(t.name() + " header columns " + hdr + " differ from the live " + live);
            }

            // the types under test: the battery states what it covers, so a column of a new type fails here
            // until it is seeded, never passes unseen
            TreeSet<String> types = new TreeSet<>();
            long identityColumns = 0;
            for (ExportTable t : exported) {
                for (LiveColumn c : liveColumns(su, t)) {
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
                assertThat(liveColumns(su, t).stream().filter(c -> c.identity).map(c -> c.name).toList())
                    .as(t.name() + "'s reassigned id is its serial column").contains(t.reassignedId());
            }

            Map<String, Walk> walks = walk(A_TOKEN, id, header, null);
            for (JsonNode th : header.get("tables")) {
                ExportTable t = TenantExport.exportable(th.get("name").asText()).orElseThrow();
                compare(su, t, th, walks.get(t.name()), refLive(su, t, th), A, problems);
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
            List<String> problems = new ArrayList<>();
            List<ExportTable> exported = TenantExport.TABLES.stream().filter(ExportTable::exported).toList();
            // the reference: tenant C's rows as they are BEFORE the session opens
            for (ExportTable t : exported) {
                String cols = String.join(", ", liveColumns(su, t).stream().map(c -> q(c.name)).toList());
                exec(su, "CREATE TEMP TABLE " + q("ref_" + t.name()) + " AS SELECT " + cols + " FROM " + qn(t)
                    + " WHERE tenant_id = '" + C + "'");
            }
            JsonNode header = open(C_TOKEN);
            String id = header.get("session_id").asText();

            long chunkRowsBefore = scalar(su, "SELECT count(*) FROM nexus.chunks WHERE tenant_id = '" + C + "'");
            Map<String, Walk> walks = walk(C_TOKEN, id, header, () -> mutateC(su));
            // the mutation really happened: the live tables differ from the reference now, in both directions
            for (String table : List.of("memory", "chunks", "catalog_meta", "search_telemetry")) {
                ExportTable t = TenantExport.exportable(table).orElseThrow();
                String cols = String.join(", ", liveColumns(su, t).stream().map(c -> q(c.name)).toList());
                assertThat(scalar(su, "SELECT count(*) FROM (SELECT " + cols + " FROM " + qn(t) + " WHERE tenant_id = '" + C + "' EXCEPT ALL SELECT " + cols + " FROM " + q("ref_" + table) + ") d"))
                    .as(table + " rows written since the reference").isGreaterThan(0);
            }
            for (String table : List.of("memory", "chunks", "catalog_meta", "frecency")) {
                ExportTable t = TenantExport.exportable(table).orElseThrow();
                String cols = String.join(", ", liveColumns(su, t).stream().map(c -> q(c.name)).toList());
                assertThat(scalar(su, "SELECT count(*) FROM (SELECT " + cols + " FROM " + q("ref_" + table) + " EXCEPT ALL SELECT " + cols + " FROM " + qn(t) + " WHERE tenant_id = '" + C + "') d"))
                    .as(table + " reference rows deleted or rewritten since").isGreaterThan(0);
            }

            for (JsonNode th : header.get("tables")) {
                ExportTable t = TenantExport.exportable(th.get("name").asText()).orElseThrow();
                compare(su, t, th, walks.get(t.name()), cols -> "SELECT " + cols + " FROM " + q("ref_" + t.name()), C, problems);
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
    private static void mutateC(Connection su) {
        try {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.execute("DELETE FROM nexus.memory WHERE tenant_id = ? AND title = (SELECT min(title) FROM nexus.memory WHERE tenant_id = ?)", C, C);
            ctx.execute("INSERT INTO nexus.memory (tenant_id, project, title, content, timestamp) VALUES (?, 'p', 'written-after-open', 'x', now())", C);
            ctx.execute("UPDATE nexus.memory SET content = 'rewritten after open' WHERE tenant_id = ? AND title = (SELECT max(title) FROM nexus.memory WHERE tenant_id = ? AND title <> 'written-after-open')", C, C);
            ctx.execute("DELETE FROM nexus.catalog_meta WHERE tenant_id = ? AND key = (SELECT max(key) FROM nexus.catalog_meta WHERE tenant_id = ?)", C, C);
            ctx.execute("INSERT INTO nexus.catalog_meta (tenant_id, key, value) VALUES (?, 'written-after-open', 'v')", C);
            ctx.execute("DELETE FROM nexus.frecency WHERE tenant_id = ?", C);
            ctx.execute("INSERT INTO nexus.search_telemetry (tenant_id, ts, query_hash, collection, raw_count, kept_count) VALUES (?, now(), 'after-open', 'c', 1, 1)", C);
            // chunks: delete every chunk no manifest or topic row holds, and write new ones on both sides of the key space
            ctx.execute("DELETE FROM nexus.chunks c WHERE c.tenant_id = ? AND NOT EXISTS (SELECT 1 FROM nexus.catalog_document_chunks m WHERE m.tenant_id = c.tenant_id AND m.collection = c.collection AND m.chash = c.chash AND m.embedding_model = c.embedding_model) AND NOT EXISTS (SELECT 1 FROM nexus.topic_assignments a WHERE a.tenant_id = c.tenant_id AND a.source_collection = c.collection AND a.doc_id = c.chash AND a.embedding_model = c.embedding_model) AND NOT EXISTS (SELECT 1 FROM nexus.chunk_orphaned_at o WHERE o.tenant_id = c.tenant_id AND o.collection = c.collection AND o.chash = c.chash AND o.embedding_model = c.embedding_model)", C);
            for (String h : List.of("00", "ff")) {
                byte[] chash = new byte[32];
                java.util.Arrays.fill(chash, (byte) Integer.parseInt(h, 16));
                ctx.execute("INSERT INTO nexus.chunks (tenant_id, collection, chash, embedding_model, chunk_text, embedding_768) VALUES (?, ?, ?, ?, 'written after open', ?::nexus.vector)",
                    C, Seed.collection(C, 768), chash, BGE, Seed.vectorText(768, 5));
            }
        } catch (RuntimeException e) {
            throw new IllegalStateException("the battery's mid-export writes failed", e);
        }
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
            List<LiveColumn> cols = liveColumnsOf(su, "nexus", PROBE);
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
            stageAndCompare(su, PROBE, cols, pages, 7, c -> "SELECT " + c + " FROM nexus." + PROBE + " WHERE tenant_id = '" + A + "'", A, problems);
            assertThat(problems).isEmpty();
            // the page is ordered by the uuid key: the keyset walk of a uuid key lost and repeated nothing
            assertThat(scalar(su, "SELECT count(DISTINCT k) FROM nexus." + PROBE + " WHERE tenant_id = '" + A + "'")).isEqualTo(7);
        }
    }

    // ── 4. an idle session's snapshot is released, not only its connection ───────────────────────

    @Test
    void anIdleSessionIsClosedAndItsSnapshotCanNoLongerBeImportedNorItsBackendFound() throws Exception {
        var clock = new ExportTestClock(Instant.parse("2026-11-12T00:00:00Z"));
        var sessions = new TenantExportSessions(ds,
            TenantExportSessions.Config.defaults().withSweepInterval(Duration.ofHours(1)), clock);
        opened.add(sessions);
        final String held = "SELECT count(*) FROM pg_stat_activity WHERE state = 'idle in transaction' AND query ILIKE '%pg_export_snapshot%'";
        long others;
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            others = scalar(su, held);   // sessions of this class's other tests, if one leaked: the count below is relative to it
        }
        var session = sessions.open(A).session();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            assertThat(scalar(su, held)).as("the open session holds its snapshot").isEqualTo(others + 1);
            assertThat(sessions.reads().page(session.snapshotId(), A, TenantExport.exportable("memory").orElseThrow(), null,
                1 << 20, 100_000).rows()).isGreaterThan(0);

            clock.advance(Duration.ofMinutes(10).plusSeconds(1));
            assertThat(sessions.sweep()).as("closed at the idle bound").isEqualTo(1);
            assertThat(scalar(su, held)).as("the snapshot's transaction ended").isEqualTo(others);
            org.assertj.core.api.Assertions.assertThatThrownBy(() -> sessions.reads().page(session.snapshotId(), A,
                    TenantExport.exportable("memory").orElseThrow(), null, 1 << 20, 100_000))
                .as("the snapshot is released").isInstanceOf(TenantExportReads.SnapshotGoneException.class);
        }
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

    private static java.util.function.Function<String, String> refLive(Connection su, ExportTable t, JsonNode th) {
        return cols -> "SELECT " + cols + " FROM " + qn(t) + " WHERE tenant_id = '" + A + "'";
    }

    /** Loads the walk's pages into a temp table of the header's columns and compares them with the reference. */
    private void compare(Connection su, ExportTable t, JsonNode th, Walk w, Function<String, String> ref, String tenant,
                         List<String> problems) throws Exception {
        List<LiveColumn> cols = new ArrayList<>();
        th.get("columns").forEach(c -> cols.add(new LiveColumn(c.get("name").asText(), c.get("type").asText(), false)));
        long refRows = scalar(su, "SELECT count(*) FROM (" + ref.apply("*") + ") r");
        if (w.tableRows() != refRows) problems.add(t.name() + " first page says " + w.tableRows() + " rows, the reference has " + refRows);
        if (w.rowsHeaderSum() != w.tableRows()) problems.add(t.name() + " pages' row headers sum to " + w.rowsHeaderSum() + ", not " + w.tableRows());
        stageAndCompare(su, t.name().replace('.', '_'), cols, w.pages(), refRows, ref, tenant, problems);
    }

    private static void stageAndCompare(Connection su, String label, List<LiveColumn> cols, List<byte[]> pages, long expectedRows,
                                        Function<String, String> ref, String tenant, List<String> problems) throws Exception {
        String stage = "stg_" + label;
        exec(su, "SET search_path TO nexus, public, pg_catalog");
        exec(su, "CREATE TEMP TABLE " + q(stage) + " (" + String.join(", ",
            cols.stream().map(c -> q(c.name) + " " + c.type).toList()) + ")");
        try {
            for (byte[] page : pages) {
                su.unwrap(PGConnection.class).getCopyAPI().copyIn("COPY " + q(stage) + " FROM STDIN (FORMAT binary)",
                    new ByteArrayInputStream(page));
            }
            // json has no equality operator: its text is what is compared
            String list = String.join(", ", cols.stream().map(c -> c.type.equals("json") ? q(c.name) + "::text AS " + q(c.name) : q(c.name)).toList());
            long staged = scalar(su, "SELECT count(*) FROM " + q(stage));
            if (staged != expectedRows) problems.add(label + ": " + staged + " rows loaded from the pages, expected " + expectedRows);
            long onlyExported = scalar(su, "SELECT count(*) FROM (SELECT " + list + " FROM " + q(stage) + " EXCEPT ALL " + ref.apply(list) + ") d");
            long onlyLive = scalar(su, "SELECT count(*) FROM (" + ref.apply(list) + " EXCEPT ALL SELECT " + list + " FROM " + q(stage) + ") d");
            if (onlyExported != 0 || onlyLive != 0) {
                problems.add(label + ": " + onlyExported + " exported rows are not in the reference, " + onlyLive
                    + " reference rows are not in the export");
            }
            if (cols.stream().anyMatch(c -> c.name.equals("tenant_id"))) {
                long foreign = scalar(su, "SELECT count(*) FROM " + q(stage) + " WHERE tenant_id <> '" + tenant + "'");
                if (foreign != 0) problems.add(label + ": " + foreign + " rows of another tenant in the export");
            }
        } finally {
            exec(su, "DROP TABLE " + q(stage));
        }
    }

    // ── fixtures: the probe table ────────────────────────────────────────────────────────────────

    private void createProbe(Connection su) throws SQLException {
        exec(su, "CREATE TABLE nexus." + PROBE + " ("
            + "tenant_id text NOT NULL, k uuid NOT NULL, id bigint GENERATED ALWAYS AS IDENTITY, s smallint, i integer, big bigint,"
            + " ba bigint[], ta text[], f4 real, f8 double precision, n numeric(12,4), b boolean, d date, t time, tsn timestamp,"
            + " tstz timestamp with time zone, iv interval, ip inet, by bytea, j json, jb jsonb, v nexus.vector(3), bt bit(5),"
            + " g integer GENERATED ALWAYS AS (i * 2) STORED, pad text, PRIMARY KEY (tenant_id, k))");
        exec(su, "ALTER TABLE nexus." + PROBE + " ENABLE ROW LEVEL SECURITY");
        exec(su, "CREATE POLICY probe_tenant ON nexus." + PROBE + " USING (tenant_id = current_setting('"
            + TenantConstants.GUC_NAME + "', true))");
        exec(su, "GRANT SELECT ON nexus." + PROBE + " TO " + PgContainerHelper.SVC_USERNAME);
        String pad = "x".repeat(1200);
        for (int r = 0; r < 5; r++) {
            exec(su, "INSERT INTO nexus." + PROBE + " (tenant_id, k, s, i, big, ba, ta, f4, f8, n, b, d, t, tsn, tstz, iv, ip, by, j, jb, v, bt, pad) VALUES ("
                + "'" + A + "', gen_random_uuid(), " + (-32768 + r * 16000) + ", " + (r - 2) + ", " + (Long.MIN_VALUE + r) + ", ARRAY[" + r + ", 9223372036854775807, -1]::bigint[],"
                + " ARRAY['a', 'b,c', NULL, 'it''s', '']::text[], " + (r + 0.5) + ", " + (r * 1.0e-300) + ", " + (r * 1234.5678) + ", " + (r % 2 == 0) + ","
                + " DATE '2026-11-1" + r + "', TIME '12:34:5" + r + ".123456', TIMESTAMP '2026-11-12 10:15:30.123456', TIMESTAMPTZ '2026-11-12 10:15:30.123456+00',"
                + " INTERVAL '" + (r + 1) + " days 03:04:05.678901', INET '192.168.0." + r + "/24', '\\x00ff" + r + "0'::bytea, '{\"a\": [1, 2, {\"b\": null}]}'::json, '{\"z\": 1, \"a\": " + r + "}'::jsonb,"
                + " '[" + r + ", 2, 3]'::nexus.vector, B'1010" + (r % 2) + "', '" + pad + "')");
        }
        exec(su, "INSERT INTO nexus." + PROBE + " (tenant_id, k) VALUES ('" + A + "', gen_random_uuid())");   // an all-NULL row
        exec(su, "INSERT INTO nexus." + PROBE + " (tenant_id, k, s, pad) VALUES ('" + A + "', gen_random_uuid(), NULL, 'nulls elsewhere'), ('" + B + "', gen_random_uuid(), 7, 'other tenant')");
    }

    // ── catalog helpers ──────────────────────────────────────────────────────────────────────────

    private record LiveColumn(String name, String type, boolean identity) {
    }

    private static List<LiveColumn> liveColumns(Connection su, ExportTable t) throws SQLException {
        return liveColumnsOf(su, t.schema(), t.table());
    }

    /** The table's non-generated columns in attribute order, read independently of the engine's own query. */
    private static List<LiveColumn> liveColumnsOf(Connection su, String schema, String table) throws SQLException {
        List<LiveColumn> cols = new ArrayList<>();
        try (Statement st = su.createStatement();
             ResultSet rs = st.executeQuery("SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attidentity <> ''"
                 + " OR EXISTS (SELECT 1 FROM pg_attrdef d WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND pg_get_expr(d.adbin, d.adrelid) LIKE 'nextval(%') FROM pg_attribute a"
                 + " JOIN pg_class c ON a.attrelid = c.oid JOIN pg_namespace n ON c.relnamespace = n.oid"
                 + " WHERE n.nspname = '" + schema + "' AND c.relname = '" + table + "' AND a.attnum > 0 AND NOT a.attisdropped"
                 + " AND a.attgenerated = '' ORDER BY a.attnum")) {
            while (rs.next()) cols.add(new LiveColumn(rs.getString(1), rs.getString(2), rs.getBoolean(3)));
        }
        return cols;
    }

    private static String norm(String type) {
        return type.replace("nexus.", "");
    }

    private static String q(String ident) {
        return "\"" + ident.replace("\"", "\"\"") + "\"";
    }

    private static String qn(ExportTable t) {
        return q(t.schema()) + "." + q(t.table());
    }

    private static long scalar(Connection su, String sql) throws SQLException {
        try (Statement st = su.createStatement(); ResultSet rs = st.executeQuery(sql)) {
            rs.next();
            return rs.getLong(1);
        }
    }

    private static void exec(Connection su, String sql) throws SQLException {
        try (Statement st = su.createStatement()) {
            st.execute(sql);
        }
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

        static String collection(String tenant, int dim) {
            String tag = tenant.replace("-", "");
            return switch (dim) {
                case 384 -> "knowledge__" + tag + "384__" + MINILM + "__v1";
                case 768 -> "knowledge__" + tag + "768__" + BGE + "__v1";
                default -> "knowledge__" + tag + "1024__" + VOYAGE + "__v1";
            };
        }

        static String vectorText(int dim, int seed) {
            StringBuilder sb = new StringBuilder("[");
            for (int j = 0; j < dim; j++) {
                if (j > 0) sb.append(',');
                sb.append(((j * 31 + seed * 17) % 97) / 97.0f);
            }
            return sb.append(']').toString();
        }

        private static void run(List<String> failures, String what, Runnable r) {
            try {
                r.run();
            } catch (RuntimeException e) {
                failures.add(what + ": " + e.getMessage().replace('\n', ' '));
            }
        }

        static void tenant(DSLContext ctx, String t, int scale, List<String> failures) {
            String c768 = collection(t, 768), c384 = collection(t, 384), c1024 = collection(t, 1024);
            run(failures, "catalog_owners", () -> ctx.execute("INSERT INTO nexus.catalog_owners (tenant_id, tumbler_prefix, name, owner_type) VALUES (?, '1.1', ?, 'curator'), (?, '1.2', ?, 'repo')",
                t, "owner-" + t, t, "owner2-" + t));
            run(failures, "catalog_collections", () -> {
                PgContainerHelper.insertCollection(ctx, t, c768, BGE);
                PgContainerHelper.insertCollection(ctx, t, c384, MINILM);
                PgContainerHelper.insertCollection(ctx, t, c1024, VOYAGE);
            });
            run(failures, "embedding_profile", () -> ctx.execute("INSERT INTO nexus.embedding_profile (tenant_id, content_type, embedding_model, dimension) VALUES (?, 'knowledge', ?, 768), (?, 'code', ?, 1024)",
                t, BGE, t, VOYAGE));
            run(failures, "catalog_documents", () -> {
                for (int i = 1; i <= 3; i++) {
                    ctx.execute("INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, physical_collection) VALUES (?, ?, ?, ?)",
                        t, "1.1." + i, "doc " + i + " of " + t, c768);
                }
            });
            run(failures, "catalog_meta", () -> ctx.execute("INSERT INTO nexus.catalog_meta (tenant_id, key, value) VALUES (?, 'k1', 'v1'), (?, 'k2', NULL), (?, 'it''s', 'quoted')", t, t, t));
            run(failures, "catalog_sensitivity_changes", () -> ctx.execute("INSERT INTO nexus.catalog_sensitivity_changes (tenant_id, tumbler, old, new, reason, actor) VALUES (?, '1.1.1', false, true, 'r', 'a'), (?, '1.1.2', true, false, 'r2', 'a2')", t, t));
            run(failures, "chunks", () -> {
                for (int dim : List.of(768, 384, 1024)) {
                    String coll = collection(t, dim);
                    String model = dim == 768 ? BGE : dim == 384 ? MINILM : VOYAGE;
                    int n = dim == 768 ? scale * 8 : scale * 3;
                    for (int i = 0; i < n; i++) {
                        String text = "chunk " + dim + "-" + i + " of " + t + " é中";
                        ctx.execute("INSERT INTO nexus.chunks (tenant_id, collection, chash, embedding_model, chunk_text, embedding_" + dim + ") VALUES (?, ?, ?, ?, ?, ?::nexus.vector)",
                            t, coll, sha256(t + text), model, text, vectorText(dim, i));
                    }
                }
            });
            run(failures, "catalog_document_chunks", () -> {
                var chashes = ctx.fetch("SELECT chash FROM nexus.chunks WHERE tenant_id = ? AND collection = ? ORDER BY chash LIMIT 4", t, c768);
                for (int i = 0; i < chashes.size(); i++) {
                    ctx.execute("INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash, chunk_index, collection, embedding_model) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        t, "1.1." + (i % 2 + 1), i, chashes.get(i).get(0, byte[].class), i, c768, BGE);
                }
            });
            run(failures, "chunk_orphaned_at", () -> {
                var chashes = ctx.fetch("SELECT chash FROM nexus.chunks WHERE tenant_id = ? AND collection = ? ORDER BY chash DESC LIMIT 2", t, c768);
                for (var r : chashes) {
                    ctx.execute("INSERT INTO nexus.chunk_orphaned_at (tenant_id, collection, chash, orphaned_at, embedding_model) VALUES (?, ?, ?, now(), ?)",
                        t, c768, r.get(0, byte[].class), BGE);
                }
            });
            run(failures, "catalog_links", () -> ctx.execute("INSERT INTO nexus.catalog_links (tenant_id, from_tumbler, to_tumbler, link_type, created_by) VALUES (?, '1.1.1', '1.1.2', 'cites', 'a'), (?, '1.1.2', '1.1.3', 'relates', 'b')", t, t));
            run(failures, "document_aspects", () -> ctx.execute("INSERT INTO nexus.document_aspects (tenant_id, collection, source_path, extracted_at, model_version, extractor_name, source_uri, doc_id) VALUES (?, ?, 'p1', now(), 'm', 'e', 'u1', '1.1.1'), (?, ?, 'p2', now(), 'm', 'e', 'u2', '1.1.2')", t, c768, t, c768));
            run(failures, "document_highlights", () -> ctx.execute("INSERT INTO nexus.document_highlights (tenant_id, doc_id, source_uri, collection, ingested_at) VALUES (?, '1.1.1', 'u1', ?, now()), (?, '1.1.2', 'u2', ?, now())", t, c768, t, c768));
            run(failures, "memory", () -> {
                for (int i = 0; i < 5; i++) {
                    ctx.execute("INSERT INTO nexus.memory (tenant_id, project, title, content, timestamp) VALUES (?, 'p', ?, ?, now())", t, "title-" + i, "content " + i);
                }
            });
            run(failures, "memory_summaries", () -> {
                Long[] ids = ctx.fetch("SELECT id FROM nexus.memory WHERE tenant_id = ? ORDER BY id LIMIT 2", t).getValues(0, Long.class).toArray(new Long[0]);
                ctx.execute("INSERT INTO nexus.memory_summaries (tenant_id, project, content, source_ids, model) VALUES (?, 'p', 'summary', ?::bigint[], 'm'), (?, 'p', 'summary 2', ?::bigint[], 'm')",
                    t, "{" + ids[0] + "," + ids[1] + "}", t, "{" + ids[1] + "}");
            });
            run(failures, "plans", () -> ctx.execute("INSERT INTO nexus.plans (tenant_id, query, plan_json, created_at, verb) VALUES (?, 'q1', '{\"steps\": []}'::jsonb, now(), 'research'), (?, 'q2', '{}'::jsonb, now(), 'analyze')", t, t));
            run(failures, "nx_answer_runs", () -> ctx.execute("INSERT INTO nexus.nx_answer_runs (tenant_id, question, created_at) VALUES (?, 'question 1', now()), (?, 'question 2', now())", t, t));
            run(failures, "nx_answer_steps", () -> {
                long run = ctx.fetchValue("SELECT min(id) FROM nexus.nx_answer_runs WHERE tenant_id = ?", t) instanceof Number n ? n.longValue() : -1;
                ctx.execute("INSERT INTO nexus.nx_answer_steps (run_id, tenant_id, step_index, operator, source, elapsed_ms, ok) VALUES (?, ?, 0, 'extract', 'llm', 5, true), (?, ?, 1, 'rank', 'sql', 6, false)", run, t, run, t);
            });
            run(failures, "topics", () -> ctx.execute("INSERT INTO nexus.topics (tenant_id, label, collection, created_at) VALUES (?, 'topic 1', ?, now()), (?, 'topic 2', ?, now()), (?, 'topic 3', ?, now())", t, c768, t, c768, t, c768));
            run(failures, "topic_assignments", () -> {
                var topics = ctx.fetch("SELECT id FROM nexus.topics WHERE tenant_id = ? ORDER BY id", t);
                var chashes = ctx.fetch("SELECT chash FROM nexus.chunks WHERE tenant_id = ? AND collection = ? ORDER BY chash LIMIT 2", t, c768);
                for (int i = 0; i < 2; i++) {
                    ctx.execute("INSERT INTO nexus.topic_assignments (tenant_id, doc_id, topic_id, source_collection, embedding_model) VALUES (?, ?, ?, ?, ?)",
                        t, chashes.get(i).get(0, byte[].class), topics.get(i).get(0, Long.class), c768, BGE);
                }
            });
            run(failures, "topic_links", () -> {
                var topics = ctx.fetch("SELECT id FROM nexus.topics WHERE tenant_id = ? ORDER BY id", t);
                ctx.execute("INSERT INTO nexus.topic_links (tenant_id, from_topic_id, to_topic_id) VALUES (?, ?, ?), (?, ?, ?)",
                    t, topics.get(0).get(0, Long.class), topics.get(1).get(0, Long.class), t, topics.get(1).get(0, Long.class), topics.get(2).get(0, Long.class));
            });
            run(failures, "taxonomy_meta", () -> ctx.execute("INSERT INTO nexus.taxonomy_meta (tenant_id, collection) VALUES (?, ?), (?, ?)", t, c768, t, c384));
            run(failures, "taxonomy_centroids", () -> {
                var topics = ctx.fetch("SELECT id FROM nexus.topics WHERE tenant_id = ? ORDER BY id", t);
                int i = 0;
                for (int dim : List.of(768, 384, 1024)) {
                    String model = dim == 768 ? BGE : dim == 384 ? MINILM : VOYAGE;
                    ctx.execute("INSERT INTO nexus.taxonomy_centroids (tenant_id, collection, topic_id, label, embedding_model, embedding_" + dim + ") VALUES (?, ?, ?, ?, ?, ?::nexus.vector)",
                        t, collection(t, dim), topics.get(i++).get(0, Long.class), "centroid " + dim, model, vectorText(dim, 3));
                }
            });
            run(failures, "frecency", () -> ctx.execute("INSERT INTO nexus.frecency (tenant_id, chunk_id) VALUES (?, ?), (?, ?)", t, hex(t + "f1"), t, hex(t + "f2")));
            run(failures, "search_telemetry", () -> ctx.execute("INSERT INTO nexus.search_telemetry (tenant_id, ts, query_hash, collection, raw_count, kept_count) VALUES (?, '2026-11-12 10:15:30.123456+00', 'q1', 'c', 1, 1), (?, '2026-11-12 10:15:30.123456+00', 'q2', 'c', 2, 1)", t, t));
            run(failures, "relevance_log", () -> ctx.execute("INSERT INTO nexus.relevance_log (tenant_id, query, chunk_id, action, timestamp) VALUES (?, 'q', ?, 'hit', now()), (?, 'q', ?, 'miss', now())", t, hex(t + "r1"), t, hex(t + "r2")));
            run(failures, "claude_assisted_remediation_consents", () -> ctx.execute("INSERT INTO nexus.claude_assisted_remediation_consents (tenant_id, scope, ts, granted) VALUES (?, 's', now(), true), (?, 's2', now(), false)", t, t));
        }

        /** Per-column values for a nullable column the generic fill by type cannot choose (a CHECK or a reference). */
        private static final Map<String, String> OVERRIDES = Map.of(
            "catalog_documents.index_state", "'complete'",
            "nx_answer_runs.plan_id", "(SELECT min(id) FROM nexus.plans WHERE tenant_id = '%T%')",
            "nx_answer_runs.run_summary", "jsonb_build_object('run', jsonb_build_object('plan_id', (SELECT min(id) FROM nexus.plans WHERE tenant_id = '%T%')))",
            "topics.parent_id", "(SELECT max(id) FROM nexus.topics WHERE tenant_id = '%T%')");

        /** The first row of every exported table (by primary key) gets every NULL column filled; the rest keep their NULLs. */
        static void fillNulls(DSLContext ctx, String t, List<String> failures) {
            for (ExportTable table : TenantExport.TABLES) {
                if (!table.exported()) continue;
                run(failures, "fill " + table.name(), () -> {
                    String pk = String.join(", ", table.primaryKey().stream().map(c -> "\"" + c.name() + "\"").toList());
                    String first = ctx.fetchValue("SELECT (" + pk + ")::text FROM nexus." + table.table() + " WHERE tenant_id = ? ORDER BY " + pk + " LIMIT 1", t) instanceof String s ? s : null;
                    if (first == null) throw new IllegalStateException("no rows");
                    var cols = ctx.fetch("SELECT a.attname, format_type(a.atttypid, a.atttypmod) FROM pg_attribute a JOIN pg_class c ON a.attrelid = c.oid"
                        + " JOIN pg_namespace n ON c.relnamespace = n.oid WHERE n.nspname = 'nexus' AND c.relname = ? AND a.attnum > 0 AND NOT a.attisdropped"
                        + " AND a.attgenerated = '' AND a.attidentity = '' ORDER BY a.attnum", table.table());
                    List<String> sets = new ArrayList<>();
                    var keys = table.primaryKey().stream().map(Column::name).toList();
                    var fks = table.references().stream().filter(r -> r.foreignKey()).flatMap(r -> r.columns().stream()).toList();
                    for (var r : cols) {
                        String name = r.get(0, String.class), type = r.get(1, String.class).replace("nexus.", "");
                        if (keys.contains(name) || name.equals("tenant_id") || type.startsWith("vector(")) continue;
                        String override = OVERRIDES.get(table.table() + "." + name);
                        if (fks.contains(name) && override == null) continue;
                        String value = override != null ? override.replace("%T%", t) : valueFor(type, table.table() + "." + name);
                        sets.add("\"" + name + "\" = COALESCE(\"" + name + "\", " + value + ")");
                    }
                    if (!sets.isEmpty()) {
                        ctx.execute("UPDATE nexus." + table.table() + " SET " + String.join(", ", sets) + " WHERE tenant_id = ? AND (" + pk + ")::text = ?", t, first);
                    }
                });
            }
        }

        private static String valueFor(String type, String where) {
            return switch (type) {
                case "text" -> "'filled " + where + " é'";
                case "bigint" -> "9007199254740993";
                case "integer" -> "42";
                case "smallint" -> "7";
                case "boolean" -> "true";
                case "double precision" -> "0.1234567890123";
                case "numeric" -> "12.3456789";
                case "timestamp with time zone" -> "TIMESTAMPTZ '2026-11-12 10:15:30.123456+00'";
                case "bytea" -> "'\\x00ff10'::bytea";
                case "jsonb" -> "'{\"k\": [1, 2, {\"n\": null}], \"s\": \"v\"}'::jsonb";
                case "bigint[]" -> "ARRAY[5, 9223372036854775807]::bigint[]";
                case "integer[]" -> "ARRAY[1, 2, 3]::integer[]";
                case "uuid" -> "gen_random_uuid()";
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
