// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.ImportTestEnv.json;
import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TenantExport;
import dev.nexus.service.db.TenantExport.ExportClass;
import dev.nexus.service.db.TenantExport.ExportTable;
import dev.nexus.service.db.TenantExport.Retired;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.net.http.HttpResponse;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * What counts as an empty tenant, and what a restore does with rows the engine itself wrote (RDR-248 "Not empty",
 * "Derived tables and cross-release"; bead nexus-csayy.6): the seeded rows a tenant holds after its first request, the
 * default tenant's boot-written {@code embedding_profile}, a derived table the target already has rows in, and a
 * retired column. The test that fails when a new lazily written row is not covered by the registry's
 * {@code seededKeys} is {@link #aFreshTenantAfterOneRequestAndOneSearchHoldsNothingTheSeededKeysDoNotCover}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TenantImportSeededRowsTest {

    private static final String SRC = "t-imp-seed-source";
    private static final String BGE = "bge-base-en-v15-768";
    private static final String VOYAGE = "voyage-context-3";

    private ImportTestEnv env;
    private ImportTestEnv.BackupFile file;

    @BeforeAll
    void startAll() throws Exception {
        env = ImportTestEnv.start();
        String srcToken = env.newTenant(SRC);
        List<String> failures = new ArrayList<>();
        env.su(ctx -> {
            TenantExportBatteryTest.Seed.rows(ctx, SRC, 1, failures);
            TenantExportBatteryTest.Seed.fillNulls(ctx, SRC, failures);
            return null;
        });
        assertThat(failures).as("the seed").isEmpty();
        file = env.export(srcToken);
    }

    @AfterAll
    void stopAll() {
        if (env != null) env.close();
    }

    @BeforeEach
    void freshSessions() {
        env.freshSessions();
    }

    private static Field<Object> col(String name) {
        return DSL.field(DSL.name(name));
    }

    /** Rows {@code tenant} holds in a data table that no seeded key of the registry covers. */
    private long uncovered(ExportTable t, String tenant) {
        return env.su(ctx -> {
            Condition rows = DSL.field(DSL.name("tenant_id"), String.class).eq(tenant);
            for (var k : t.seededKeys()) {
                if (k.defaultTenantOnly() && !TenantConstants.DEFAULT_TENANT.equals(tenant)) continue;
                List<String> keyColumns = t.primaryKey().stream().map(TenantExport.Column::name).filter(n -> !n.equals("tenant_id")).toList();
                Condition one = DSL.trueCondition();
                for (int i = 0; i < keyColumns.size(); i++) one = one.and(DSL.field(DSL.name(keyColumns.get(i)), String.class).eq(k.values().get(i)));
                rows = rows.and(DSL.not(one));
            }
            return (long) ctx.fetchCount(DSL.table(DSL.name(t.schema(), t.table())), rows);
        });
    }

    private void insertUserRow(DSLContext ctx, String table, List<String> columns, Object... values) {
        ctx.insertInto(DSL.table(DSL.name("nexus", table)), columns.stream().map(TenantImportSeededRowsTest::col).toList())
            .values(java.util.Arrays.stream(values).<Field<?>>map(DSL::val).toList()).execute();
    }

    // ── a fresh tenant ──────────────────────────────────────────────────────────────────────────

    @Test
    void aFreshTenantAfterOneRequestAndOneSearchHoldsNothingTheSeededKeysDoNotCoverAndAnImportIntoItSucceeds() throws Exception {
        String tenant = "t-imp-seed-fresh";
        String token = env.newTenant(tenant);   // its first authenticated request
        HttpResponse<String> search = env.callJson("POST", "/v1/telemetry/search/batch", token,
            "{\"rows\": [[\"2026-11-13T10:00:00Z\", \"qhash\", \"some-collection\", 3, 2, null, null]]}");
        assertThat(search.statusCode()).as(search.body()).isEqualTo(200);

        long seededRows = 0;
        for (ExportTable t : TenantExport.TABLES) {
            if (t.exportClass() != ExportClass.DATA) continue;
            assertThat(uncovered(t, tenant)).as("rows of " + t.name() + " no seeded key covers: a new lazily written row"
                + " needs an entry in the registry's seededKeys, or the import refuses every tenant that made a request").isZero();
            seededRows += env.count(t.table(), tenant);
        }
        assertThat(seededRows).as("the tenant does hold the engine's own row (the check saw something)").isEqualTo(1);
        assertThat(env.count("search_telemetry", tenant)).as("and the search wrote a derived row").isEqualTo(1);

        JsonNode end = env.restore(token, file);

        assertThat(end.get("status").asText()).as(end.toString()).isEqualTo("committed");
        // the derived table already had a row: it is skipped and reported, and everything else loads
        assertThat(end.get("report").get("skipped_tables").has("search_telemetry")).isTrue();
        assertThat(end.get("report").get("skipped_tables").get("search_telemetry").asText()).contains("already holds rows");
        assertThat(env.count("search_telemetry", tenant)).as("not doubled and not replaced").isEqualTo(1);
        assertThat(env.count("chunks", tenant)).isEqualTo(env.count("chunks", SRC));
        assertThat(env.count("memory", tenant)).isEqualTo(env.count("memory", SRC));
        assertThat(env.count("catalog_meta", tenant)).as("the ghost-sweep row kept, the source's three, and the marker").isEqualTo(5);
    }

    @Test
    void anyOtherRowKeepsATenantFromBeingEmptyAndTheOpenNamesTheTable() throws Exception {
        for (Map.Entry<String, String> case_ : Map.of(
                "catalog_owners", "t-imp-seed-owner", "catalog_meta", "t-imp-seed-meta", "embedding_profile", "t-imp-seed-profile").entrySet()) {
            String tenant = case_.getValue();
            String token = env.newTenant(tenant);
            env.su(ctx -> {
                switch (case_.getKey()) {
                    case "catalog_owners" -> insertUserRow(ctx, "catalog_owners", List.of("tenant_id", "tumbler_prefix", "name", "owner_type"),
                        tenant, "1.1", "owner", "curator");
                    case "catalog_meta" -> insertUserRow(ctx, "catalog_meta", List.of("tenant_id", "key", "value"), tenant, "some_other_key", "v");
                    default -> insertUserRow(ctx, "embedding_profile", List.of("tenant_id", "content_type", "embedding_model", "dimension"),
                        tenant, "knowledge", BGE, 768);   // seeded for the default tenant only: on this tenant it is a user write
                }
                return null;
            });

            HttpResponse<String> r = env.open(token, file);

            assertThat(r.statusCode()).as(case_.getKey() + ": " + r.body()).isEqualTo(409);
            assertThat(json(r).get("table").asText()).isEqualTo(case_.getKey());
        }
    }

    // ── a retired column ────────────────────────────────────────────────────────────────────────

    @Test
    void aRetiredColumnIsSkippedAndReportedAndTheRestLoads() throws Exception {
        String tenant = "t-imp-seed-retired";
        String token = env.newTenant(tenant);
        env.importSessions(dev.nexus.service.db.TenantImportSessions.Config.defaults(),
            List.of(new Retired("memory", "legacy_flag", "memory-099-drop-legacy-flag")));
        ImportTestEnv.BackupFile older = file.withColumn("memory", "legacy_flag", "boolean", new byte[] {1}).seal();

        JsonNode end = env.restore(token, older);

        assertThat(end.get("status").asText()).as(end.toString()).isEqualTo("committed");
        assertThat(end.get("report").get("skipped_columns").toString()).contains("memory.legacy_flag");
        assertThat(env.count("memory", tenant)).isEqualTo(env.count("memory", SRC));
        // without the retired entry the same file is refused, naming the column
        String other = env.newTenant("t-imp-seed-unretired");
        env.importSessions(dev.nexus.service.db.TenantImportSessions.Config.defaults(), List.of());
        HttpResponse<String> refused = env.open(other, older);
        assertThat(refused.statusCode()).isEqualTo(422);
        assertThat(json(refused).get("error").asText()).contains("memory.legacy_flag");
    }

    @Test
    void aRetiredTableIsSkippedAndReportedNotRefused() throws Exception {
        String tenant = "t-imp-seed-retired-table";
        String token = env.newTenant(tenant);
        env.importSessions(dev.nexus.service.db.TenantImportSessions.Config.defaults(),
            List.of(new Retired("old_things", null, "old-001-drop-old-things")));
        ImportTestEnv.BackupFile f = file.copy();
        f.header.withArray("tables").addObject().put("name", "old_things").putArray("columns").addObject().put("name", "x").put("type", "text");
        f.pages.put("old_things", List.of());
        f.seal();

        HttpResponse<String> opened = env.open(token, f);

        assertThat(opened.statusCode()).as(opened.body()).isEqualTo(200);
        assertThat(json(opened).get("notes").toString()).contains("old_things");
        assertThat(json(opened).get("tables").toString()).doesNotContain("old_things");
        String id = json(opened).get("session_id").asText();
        assertThat(env.call("POST", ImportTestEnv.IMPORT + "/" + id + "/close?table=old_things&rows=0", token).statusCode())
            .as("a client that closes the retired table anyway is told it is skipped").isEqualTo(200);
        assertThat(env.call("DELETE", ImportTestEnv.IMPORT + "/" + id, token).statusCode()).isEqualTo(200);
    }

    // ── the local default tenant ────────────────────────────────────────────────────────────────

    @Test
    void theDefaultTenantsBootWrittenProfileMustEqualTheFilesAndAMismatchLeavesTheTenantAsItWas() throws Exception {
        String token = "tok-default-tenant-0123456789abcdefghijklmnopqrstuvwxyz";
        env.su(ctx -> {
            PgContainerHelper.seedServiceToken(ctx, token, TenantConstants.DEFAULT_TENANT, "default-tenant");
            PgContainerHelper.ensureTenantPartitions(ctx, TenantConstants.DEFAULT_TENANT);
            // what every boot of a local engine writes: a row per content type
            for (String type : List.of("knowledge", "docs", "rdr", "unknown")) {
                insertUserRow(ctx, "embedding_profile", List.of("tenant_id", "content_type", "embedding_model", "dimension"),
                    TenantConstants.DEFAULT_TENANT, type, type.equals("knowledge") ? VOYAGE : BGE, type.equals("knowledge") ? 1024 : 768);
            }
            insertUserRow(ctx, "embedding_profile", List.of("tenant_id", "content_type", "embedding_model", "dimension"),
                TenantConstants.DEFAULT_TENANT, "code", VOYAGE, 1024);
            return null;
        });
        assertThat(env.call("GET", "/v1/_whoami", token).statusCode()).isEqualTo(200);
        assertThat(uncovered(TenantExport.entry("embedding_profile").orElseThrow(), TenantConstants.DEFAULT_TENANT))
            .as("the boot's rows are seeded for the default tenant").isZero();

        // the file says knowledge is bge and the engine's own row says voyage: a different profile
        JsonNode failed = env.restore(token, file);

        assertThat(failed.get("status").asText()).startsWith("failed: ").contains("embedding_profile").contains("knowledge")
            .contains("embedding_model");
        for (String t : List.of("catalog_owners", "catalog_collections", "chunks", "memory")) {
            assertThat(env.count(t, TenantConstants.DEFAULT_TENANT)).as("rolled back: " + t).isZero();
        }
        assertThat(env.count("embedding_profile", TenantConstants.DEFAULT_TENANT)).as("the engine's own five rows").isEqualTo(5);

        // the profile now agrees with the file's rows (knowledge bge 768, code voyage 1024): equal rows load, and the others stay
        env.su(ctx -> {
            ctx.update(DSL.table(DSL.name("nexus", "embedding_profile"))).set(col("embedding_model"), BGE).set(col("dimension"), 768)
                .where(col("tenant_id").eq(TenantConstants.DEFAULT_TENANT), col("content_type").eq("knowledge")).execute();
            return null;
        });
        JsonNode end = env.restore(token, file);

        assertThat(end.get("status").asText()).as(end.toString()).isEqualTo("committed");
        assertThat(env.count("embedding_profile", TenantConstants.DEFAULT_TENANT)).isEqualTo(5);
        assertThat(env.count("chunks", TenantConstants.DEFAULT_TENANT)).isEqualTo(env.count("chunks", SRC));
        for (JsonNode t : end.get("report").get("tables")) {
            if (t.get("table").asText().equals("embedding_profile")) {
                assertThat(t.get("seeded_skipped").asLong()).as("the file's two rows were the engine's own").isEqualTo(2);
                assertThat(t.get("inserted").asLong()).isZero();
            }
        }
    }
}
