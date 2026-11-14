// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import com.fasterxml.jackson.databind.JsonNode;
import dev.nexus.service.db.TenantExport;
import dev.nexus.service.db.TenantExport.ExportTable;
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

    static final String RESOURCE = "/fixtures/rdr248-tenant-backup.nxtenant";
    static final String RESOURCE_PATH = "src/test/resources/fixtures/rdr248-tenant-backup.nxtenant";
    /** The property that makes {@link #regenerate} write the file; its value is the reason, printed with the new pins. */
    static final String REGENERATE_PROPERTY = "nexus.fixture.regenerate";

    /** SHA-256 of the committed file. Edited only by a reviewed change that regenerates the file (see {@link #regenerate}). */
    static final String PINNED_SHA256 = "551f2c92e77b7f5e03f8de4bfa7f85057730c1fc7b8fec9f2dab91da678a94c6";
    /** The {@code engine_version} of the committed file's header: the engine that wrote it. */
    static final String PINNED_ENGINE_VERSION = "unstamped+48654a703cf0";
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
        assertThat(file.header.get("engine_version").asText()).as("the engine that wrote the fixture").isEqualTo(PINNED_ENGINE_VERSION);
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
        for (String t : file.tableNames()) {
            long expected = file.rows(t);
            long actual = env.count(t, tenant);
            // catalog_meta: the file's rows (its own ghost-sweep row is the tenant's already) and the marker of this restore
            assertThat(actual).as("rows of " + t).isEqualTo(t.equals("catalog_meta") ? expected + 1 : expected);
        }
        assertThat(end.get("report").get("rows_dropped").asLong()).isZero();
    }

    // ── what a migration does to it ─────────────────────────────────────────────────────────────

    @Test
    void aMigrationThatDropsAnExportedColumnTurnsTheLoadRedNamingTheColumn() throws Exception {
        try (ImportTestEnv own = ImportTestEnv.start()) {
            own.su(ctx -> ctx.execute("ALTER TABLE nexus.memory DROP COLUMN access_count"));
            HttpResponse<String> r = own.open(own.newTenant("t-fixture-dropped"), TenantBackupFiles.read(committed()));

            assertThat(r.statusCode()).as(r.body()).isEqualTo(422);
            assertThat(ImportTestEnv.json(r).get("reason").asText()).isEqualTo("file_not_loadable");
            assertThat(ImportTestEnv.json(r).get("error").asText()).contains("memory.access_count");
        }
    }

    @Test
    void aMigrationThatRetypesAnExportedColumnTurnsTheLoadRedNamingTheColumn() throws Exception {
        try (ImportTestEnv own = ImportTestEnv.start()) {
            own.su(ctx -> ctx.execute("ALTER TABLE nexus.memory ALTER COLUMN access_count TYPE bigint"));
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
            + "\"\n  PINNED_ENGINE_VERSION = \"" + file.header.get("engine_version").asText() + "\"\n  PINNED_TABLE_COUNT = " + file.tableNames().size());
    }

    private static String headCommit(Path dir) throws Exception {
        Process p = new ProcessBuilder("git", "rev-parse", "--short=12", "HEAD").directory(dir.toFile()).redirectErrorStream(true).start();
        String out = new String(p.getInputStream().readAllBytes(), java.nio.charset.StandardCharsets.UTF_8).trim();
        assertThat(p.waitFor()).as("git rev-parse: " + out).isZero();
        return out;
    }
}
