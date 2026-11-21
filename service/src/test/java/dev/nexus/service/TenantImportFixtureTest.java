// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import com.fasterxml.jackson.databind.JsonNode;
import dev.nexus.service.db.TenantExport;
import dev.nexus.service.db.TenantExport.ExportTable;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.io.IOException;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;

/**
 * The committed fixture (RDR-248 Step 6, "Risks": a schema change between export and import; bead nexus-csayy.7). The
 * file in {@code src/test/resources/fixtures} is a real {@code .nxtenant} backup written by the engine of the
 * release that shipped the import, a few rows in every data and derived table with every column type the registry
 * exports, and this class loads it on whatever engine this build is. A migration that drops or retypes an exported
 * column, or a registry change that no longer reads an old file, goes red HERE and has to say what it does to
 * the backups users already hold: by a retired-list entry, or by a reviewed decision to refuse old files.
 *
 * <p>The fixture cannot be replaced by accident: {@link #PINNED_SHA256} and {@link #PINNED_ENGINE_VERSION} name the
 * bytes and the engine that wrote them, so a regenerated file fails {@link #theCommittedFileIsTheOneThatWasReviewed}
 * until a reviewed change edits both constants and says why. To regenerate on purpose see {@link #regenerate}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TenantImportFixtureTest {

    private static final org.slf4j.Logger LOG = org.slf4j.LoggerFactory.getLogger(TenantImportFixtureTest.class);

    static final String RESOURCE = "/fixtures/rdr248-tenant-backup.nxtenant";
    static final String RESOURCE_PATH = "src/test/resources/fixtures/rdr248-tenant-backup.nxtenant";
    /** The property that makes {@link #regenerate} write the file; its value is the reason, printed with the new pins. */
    static final String REGENERATE_PROPERTY = "nexus.fixture.regenerate";

    /** SHA-256 of the committed file. Edited only by a reviewed change that regenerates the file (see {@link #regenerate}). */
    static final String PINNED_SHA256 = "551f2c92e77b7f5e03f8de4bfa7f85057730c1fc7b8fec9f2dab91da678a94c6";
    /** The {@code engine_version} of the committed file's header: the engine that wrote it. */
    static final String PINNED_ENGINE_VERSION = "unstamped+48654a703cf0";
    /**
     * SHA-256 of the file's header frame payload: what {@code nx backup restore} sends as {@code header_sha256} and the restore
     * marker records. {@code tests/test_nx_backup_fixture.py} reads this constant and compares it with the Python client's own
     * {@code index_file(...).header_sha256}, so the Java writer and the client cannot disagree on what "the header" is.
     */
    static final String PINNED_HEADER_SHA256 = "29e9c949a8822ecb53d7a6a567be259e1e44f8a2e012e6f2002121da6746dd83";
    /** The tables of the file: every exported table of the registry the day it was written. */
    static final int PINNED_TABLE_COUNT = 26;

    private ImportTestEnv env;

    @BeforeAll
    void startAll() throws Exception {
        env = ImportTestEnv.start();
    }

    @AfterAll
    void stopAll() {
        if (env != null) env.close();
    }

    private static byte[] committed() throws IOException {
        try (var in = TenantImportFixtureTest.class.getResourceAsStream(RESOURCE)) {
            assertThat(in).as("the committed fixture " + RESOURCE).isNotNull();
            return in.readAllBytes();
        }
    }

    private static String sha256Hex(byte[] bytes) throws Exception {
        return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(bytes));
    }

    // ── the pin ─────────────────────────────────────────────────────────────────────────────────

    @Test
    void theCommittedFileIsTheOneThatWasReviewed() throws Exception {
        byte[] bytes = committed();
        ImportTestEnv.BackupFile file = TenantBackupFiles.read(bytes);

        assertThat(sha256Hex(bytes)).as("the fixture was regenerated or edited: a reviewed change must update PINNED_SHA256 and say why"
            + " (see TenantImportFixtureTest.regenerate)").isEqualTo(PINNED_SHA256);
        // Redundant with the SHA-256 above (any change to the header changes the file), and it stays as a label: a test engine is
        // unstamped, so this names the commit that generated the file, which a reviewer reads without hashing anything.
        assertThat(file.header.get("engine_version").asText()).as("the engine that wrote the fixture").isEqualTo(PINNED_ENGINE_VERSION);
        assertThat(file.headerSha()).as("the header frame's SHA-256, as the client and the restore marker compute it").isEqualTo(PINNED_HEADER_SHA256);
        assertThat(file.tableNames()).as("tables of the fixture").hasSize(PINNED_TABLE_COUNT);
    }

    @Test
    void theFileHoldsRowsInEveryTableAndEveryColumnTypeTheEngineExports() throws Exception {
        ImportTestEnv.BackupFile file = TenantBackupFiles.read(committed());

        Map<String, Long> rows = new LinkedHashMap<>();
        for (String t : file.tableNames()) rows.put(t, file.rows(t));
        assertThat(rows.entrySet().stream().filter(e -> e.getValue() == 0).map(Map.Entry::getKey).toList())
            .as("a table with no row proves no load of it").isEmpty();

        // the types of the header of an export taken now, from a tenant with no row: what the engine exports today
        String token = env.newTenant("t-fixture-types");
        ImportTestEnv.BackupFile now = env.export(token);
        Set<String> engineTypes = new TreeSet<>(), fixtureTypes = new TreeSet<>();
        for (JsonNode t : now.header.get("tables")) t.get("columns").forEach(c -> engineTypes.add(c.get("type").asText()));
        for (JsonNode t : file.header.get("tables")) t.get("columns").forEach(c -> fixtureTypes.add(c.get("type").asText()));
        assertThat(fixtureTypes).as("every column type the engine exports must be in the fixture, or a retype of one goes unseen")
            .containsAll(engineTypes);
        assertThat(engineTypes).as("the check examined the engine's types").hasSizeGreaterThan(8);
    }

    // ── the load ────────────────────────────────────────────────────────────────────────────────

    @Test
    void theCommittedFileLoadsOnTheCurrentEngineWithEveryRowOfItsTables() throws Exception {
        ImportTestEnv.BackupFile file = TenantBackupFiles.read(committed());
        String tenant = "t-fixture-load";
        JsonNode end = env.restore(env.newTenant(tenant), file);

        assertThat(end.get("status").asText()).as(end.toString()).isEqualTo("committed");
        assertThat(end.get("report").get("skipped_tables").size()).as("a skipped table is a table the fixture no longer tests").isZero();
        // A table a later migration dropped is in the file and not in the engine: the load skips it and says so in the status
        // notes (RDR-248 "Derived tables and cross-release"), and nothing else about it is loaded or counted. Only a table the
        // registry retired by name may be absent from the engine, so a drop that was not entered in TenantExport.RETIRED still fails here.
        Set<String> retiredTables = new TreeSet<>();
        TenantExport.RETIRED.stream().filter(r -> r.column() == null).forEach(r -> retiredTables.add(r.table()));
        List<String> inFileAndRetired = file.tableNames().stream().filter(retiredTables::contains).toList();
        assertThat(inFileAndRetired).as("the fixture still carries the tables that were retired after it was written (frecency: telemetry-019-1)")
            .containsExactly("frecency");
        for (String t : inFileAndRetired) {
            assertThat(end.get("notes").toString()).as("the status names the retired table it skipped").contains("table " + t + " is retired");
            assertThat(TenantExport.exportable(t)).as(t + " is no longer in the registry").isEmpty();
        }
        for (String t : file.tableNames()) {
            if (retiredTables.contains(t)) continue;
            long expected = file.rows(t);
            long actual = env.count(t, tenant);
            // catalog_meta: the file's rows (its own ghost-sweep row is the tenant's already) and the marker of this restore
            assertThat(actual).as("rows of " + t).isEqualTo(t.equals("catalog_meta") ? expected + 1 : expected);
        }
        assertThat(end.get("report").get("rows_dropped").asLong()).isZero();
    }

    // ── RDR-229 P3.11: a backup taken before P3.1 loads with every collection id filled ────────────

    /**
     * The fixture was written before RDR-229's P3.1, so it carries no {@code collection_id}. The load must still leave none
     * null: each row takes the id of the restored registry row with the same {@code (tenant_id, name)} (RDR-229 item 10),
     * because P3.1's CHECK refuses a row with a name and no id. The file is NOT regenerated for this: that it is older than
     * the column is the whole point, and {@link #theCommittedFileIsTheOneThatWasReviewed} pins it.
     */
    @Test
    void theCommittedFileLoadsWithEveryCollectionIdFilledFromTheRestoredRegistry() throws Exception {
        ImportTestEnv.BackupFile file = TenantBackupFiles.read(committed());
        // the precondition that makes this test mean something: the file has no collection id of any kind
        for (String t : file.tableNames()) {
            for (JsonNode c : file.table(t).withArray("columns")) {
                assertThat(c.get("name").asText()).as("column of " + t + " in the committed pre-P3.1 file")
                    .isNotIn("collection_id", "superseded_by_id", "source_collection_id", "target_collection_id", "physical_collection_id");
            }
        }
        String tenant = "t-fixture-cid";

        JsonNode end = env.restore(env.newTenant(tenant), file);

        assertThat(end.get("status").asText()).as(end.toString()).isEqualTo("committed");
        var registry = env.su(ctx -> CollectionIdSites.registryAudit(ctx, tenant));
        assertThat(registry.examined()).as("the fixture's registry rows").isEqualTo(file.rows("catalog_collections")).isPositive();
        assertThat(registry.nullIds()).as("registry rows with a null collection_id").isZero();
        assertThat(registry.mismatched()).as("registry rows whose tombstone fields disagree").isZero();
        List<CollectionIdSites.Finding> found = env.su(ctx -> CollectionIdSites.audit(ctx, tenant, CollectionIdSites.exportedSites()));
        assertThat(found.stream().filter(f -> f.nullIds() > 0).map(Object::toString).toList())
            .as("child rows of the load whose collection_id is null although their name names a restored registry row").isEmpty();
        assertThat(found.stream().filter(f -> f.mismatched() > 0).map(Object::toString).toList())
            .as("child rows whose collection_id is not that registry row's").isEmpty();
        // A check that examined nothing proves nothing. The fixture holds rows in every exported table (see regenerate), so every
        // exported site must examine some; a zero is a setup failure. The tables the backup excludes are not in a backup file, so
        // the round-trip test audits those, on a source it seeds itself.
        assertThat(CollectionIdSites.unexamined(found)).as("sites with no row; rows examined per site: " + CollectionIdSites.counts(found)).isEmpty();
        assertThat(found).as("the fixture audit covered every exported site").hasSize(CollectionIdSites.exportedSites().size());
        // the strict history audit: the fixture's history rows must be refilled by name too. Its rows name no registry row (the
        // file cannot be regenerated, and its history names "c" or nothing), so only a wrong id fails here and the counts are printed;
        // the round-trip tests are the ones that seed history rows naming real collections.
        List<CollectionIdSites.HistoryFinding> history = env.su(ctx -> CollectionIdSites.auditHistoryStrict(ctx, tenant));
        LOG.info("history_strict tenant={} found={}", tenant, history);
        assertThat(CollectionIdSites.historyProblems(history, false)).as("history rows after the load; counts " + history).isEmpty();
        Integer nullChunkIds = env.su(ctx -> ctx.fetchCount(DSL.table(DSL.name("nexus", "chunks")),
            DSL.field(DSL.name("tenant_id"), String.class).eq(tenant).and(DSL.field(DSL.name("collection_id")).isNull())));
        assertThat(nullChunkIds).as("chunks with a null collection_id").isZero();
    }

    // ── what a migration does to it ─────────────────────────────────────────────────────────────

    @Test
    void aMigrationThatDropsAnExportedColumnTurnsTheLoadRedNamingTheColumn() throws Exception {
        try (ImportTestEnv own = ImportTestEnv.start()) {
            own.su(ctx -> ctx.alterTable(DSL.table(DSL.name("nexus", "memory"))).dropColumn(DSL.field(DSL.name("access_count"))).execute());
            HttpResponse<String> r = own.open(own.newTenant("t-fixture-dropped"), TenantBackupFiles.read(committed()));

            assertThat(r.statusCode()).as(r.body()).isEqualTo(422);
            assertThat(ImportTestEnv.json(r).get("reason").asText()).isEqualTo("file_not_loadable");
            assertThat(ImportTestEnv.json(r).get("error").asText()).contains("memory.access_count");
        }
    }

    @Test
    void aMigrationThatRetypesAnExportedColumnTurnsTheLoadRedNamingTheColumn() throws Exception {
        try (ImportTestEnv own = ImportTestEnv.start()) {
            own.su(ctx -> ctx.alterTable(DSL.table(DSL.name("nexus", "memory"))).alterColumn(DSL.name("access_count")).set(SQLDataType.BIGINT).execute());
            HttpResponse<String> r = own.open(own.newTenant("t-fixture-retyped"), TenantBackupFiles.read(committed()));

            assertThat(r.statusCode()).as(r.body()).isEqualTo(422);
            assertThat(ImportTestEnv.json(r).get("reason").asText()).isEqualTo("file_not_loadable");
            assertThat(ImportTestEnv.json(r).get("error").asText()).contains("memory.access_count").contains("integer").contains("bigint");
        }
    }

    // ── regenerating, on purpose ────────────────────────────────────────────────────────────────

    /**
     * Writes a new fixture from this engine. Not part of a normal run: it does nothing unless
     * {@code -Dnexus.fixture.regenerate="<why the old file must be replaced>"} is given, so
     * <pre>scripts/mvnw-leased.sh -Dtest=TenantImportFixtureTest#regenerate -Dnexus.fixture.regenerate="why" test</pre>
     * writes {@value #RESOURCE_PATH} (its header's engine_version is the engine's own, or, from an unstamped test engine,
     * {@code unstamped+<HEAD commit>}) and prints the two constants to put in this class. The reviewed change that
     * commits the new file edits {@link #PINNED_SHA256} and {@link #PINNED_ENGINE_VERSION} (and {@link #PINNED_TABLE_COUNT}
     * if the registry grew), with the reason in its message. The old file is the proof that old backups still load: replace it only when
     * the engine has deliberately stopped reading it (and say so in the changelog), or when the file no longer
     * holds a table or a column type the registry exports.
     */
    @Test
    void regenerate() throws Exception {
        String why = System.getProperty(REGENERATE_PROPERTY, "");
        assumeTrue(!why.isBlank(), "set -D" + REGENERATE_PROPERTY + "=<reason> to write a new fixture");
        String tenant = "t-fixture-source";
        String token = env.newTenant(tenant);
        List<String> failures = new ArrayList<>();
        env.su(ctx -> {
            TenantExportBatteryTest.Seed.rows(ctx, tenant, 1, failures);
            TenantExportBatteryTest.Seed.fillNulls(ctx, tenant, failures);
            return null;
        });
        assertThat(failures).as("the seed").isEmpty();
        ImportTestEnv.BackupFile file = env.export(token);
        for (ExportTable t : TenantExport.TABLES) {
            if (t.exported()) assertThat(file.rows(t.name())).as("the fixture seed fills " + t.name()).isPositive();
        }
        Path base = Path.of(System.getProperty("basedir", System.getProperty("user.dir")));
        if ("unknown".equals(file.header.get("engine_version").asText())) {
            // a test engine is unstamped; the file names the code that wrote it instead, so the pin says something
            file.header.put("engine_version", "unstamped+" + headCommit(base));
        }
        byte[] bytes = TenantBackupFiles.write(file);
        Path target = base.resolve(RESOURCE_PATH);
        Files.createDirectories(target.getParent());
        Files.write(target, bytes);
        System.out.println("FIXTURE REGENERATED (" + why + "): " + target + "\n  PINNED_SHA256 = \"" + sha256Hex(bytes)
            + "\"\n  PINNED_HEADER_SHA256 = \"" + TenantBackupFiles.read(bytes).headerSha()
            + "\"\n  PINNED_ENGINE_VERSION = \"" + file.header.get("engine_version").asText() + "\"\n  PINNED_TABLE_COUNT = " + file.tableNames().size());
    }

    private static String headCommit(Path dir) throws Exception {
        Process p = new ProcessBuilder("git", "rev-parse", "--short=12", "HEAD").directory(dir.toFile()).redirectErrorStream(true).start();
        String out = new String(p.getInputStream().readAllBytes(), java.nio.charset.StandardCharsets.UTF_8).trim();
        assertThat(p.waitFor()).as("git rev-parse: " + out).isZero();
        return out;
    }
}
