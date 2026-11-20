// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import dev.nexus.service.AtomicWriteTestBase;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.net.URI;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * RDR-229 P2.5 D1 (Sam, 2026-11-20, T2 {@code nexus/sam-ruling-rdr229-p12-row-decisions-2026-11-20}): a
 * registration with a blank or absent {@code model_version} is refused with a 400 and writes nothing; the ETL
 * import paths take the version from a conformant {@code __vN} name when the imported row carries none, and refuse
 * otherwise. Real PostgreSQL; the 400 is asserted through {@link CatalogHandler}, not only the repository.
 */
class CatalogHandlerCollectionUpsertModelVersionTest extends AtomicWriteTestBase {

    private CatalogHandler handler;

    @BeforeAll
    void wireHandler() {
        handler = new CatalogHandler(repo);
    }

    private String newTenant() {
        return "mv" + seq.incrementAndGet();
    }

    private String conformant(String tag, int version) {
        return "knowledge__" + tag + seq.incrementAndGet() + "__minilm-l6-v2-384__v" + version;
    }

    private static Map<String, Object> row(String name, String version) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("name", name);
        m.put("content_type", "knowledge");
        m.put("owner_id", "o");
        m.put("embedding_model", "minilm-l6-v2-384");
        if (version != null) m.put("model_version", version);
        return m;
    }

    private CapturingExchange upsert(String tenant, String json) throws Exception {
        CapturingExchange ex = new CapturingExchange("POST", URI.create("/v1/catalog/collections/upsert"), json);
        RequestContext.set(new RequestContext.Principal(tenant, null, false, false, "tenant", "test-credential-hash"));
        try {
            handler.handle(ex);
        } finally {
            RequestContext.clear();
        }
        return ex;
    }

    private String version(String tenant, String name) {
        var c = repo.getCollection(tenant, name);
        return c == null ? null : String.valueOf(c.get("model_version"));
    }

    // ── upsert ───────────────────────────────────────────────────────────────

    @Test
    void aBlankOrAbsentModelVersionAnswers400_namingTheField_andWritesNoRow() throws Exception {
        String t = newTenant();
        String absent = conformant("absent", 1);
        String blank = conformant("blank", 1);

        CapturingExchange a = upsert(t, "{\"name\":\"" + absent + "\",\"content_type\":\"knowledge\","
            + "\"owner_id\":\"o\",\"embedding_model\":\"minilm-l6-v2-384\"}");
        CapturingExchange b = upsert(t, "{\"name\":\"" + blank + "\",\"content_type\":\"knowledge\","
            + "\"owner_id\":\"o\",\"embedding_model\":\"minilm-l6-v2-384\",\"model_version\":\"  \"}");

        assertThat(a.status).isEqualTo(400);
        assertThat(a.bodyString()).contains("model_version").contains(absent);
        assertThat(b.status).isEqualTo(400);
        assertThat(b.bodyString()).contains("model_version").contains(blank);
        assertThat(repo.getCollection(t, absent)).as("nothing was written").isNull();
        assertThat(repo.getCollection(t, blank)).as("nothing was written").isNull();
    }

    @Test
    void aRegistrationThatNamesAVersionIsStoredWithIt() throws Exception {
        String t = newTenant();
        String name = conformant("named", 1);

        CapturingExchange ex = upsert(t, "{\"name\":\"" + name + "\",\"content_type\":\"knowledge\","
            + "\"owner_id\":\"o\",\"embedding_model\":\"minilm-l6-v2-384\",\"model_version\":\"v4\"}");

        assertThat(ex.status).isEqualTo(200);
        assertThat(version(t, name)).isEqualTo("v4");
    }

    @Test
    void aReRegistrationOfAnExistingRowWithNoVersionStillReaches200_andKeepsTheRowsVersion() throws Exception {
        // The version is bound only on a fresh INSERT (an existing row's is never re-pointed), so the refusal is
        // the new-row branch's; a revive of a tombstone, which sends none, keeps working.
        String t = newTenant();
        String name = conformant("exists", 1);
        repo.upsertCollection(t, row(name, "v2"));

        CapturingExchange ex = upsert(t, "{\"name\":\"" + name + "\",\"content_type\":\"knowledge\","
            + "\"owner_id\":\"o\",\"embedding_model\":\"minilm-l6-v2-384\"}");

        assertThat(ex.status).isEqualTo(200);
        assertThat(version(t, name)).isEqualTo("v2");
    }

    // ── import ───────────────────────────────────────────────────────────────

    @Test
    void anImportOfAConformantNameWithNoVersionStoresTheNamesVersion() {
        String t = newTenant();
        String single = conformant("one", 3);
        String viaBatch = conformant("batch", 12);

        repo.importCollection(t, row(single, null));
        repo.importCollectionsBatch(t, List.of(row(viaBatch, "")));

        assertThat(version(t, single)).isEqualTo("v3");
        assertThat(version(t, viaBatch)).as("a blank version is a missing one").isEqualTo("v12");
    }

    @Test
    void reImportingAnExistingLegacyRowWithABlankVersionIsANoOp_notA400() throws Exception {
        // Review: the version check judges a row that will be INSERTed; a name already registered is skipped first.
        String t = newTenant();
        String legacy = "my_legacy_collection_" + seq.incrementAndGet();
        repo.importCollection(t, row(legacy, "v1"));
        try (var su = pg.createConnection("")) {
            org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES)
                .update(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS)
                .set(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.MODEL_VERSION, "")
                .where(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.TENANT_ID.eq(t)
                    .and(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.NAME.eq(legacy))).execute();
        }

        repo.importCollection(t, row(legacy, null));
        repo.importCollectionsBatch(t, List.of(row(legacy, "  ")));

        assertThat(version(t, legacy)).as("an existing row is left as it was").isEmpty();
    }

    @Test
    void aBatchLargerThanOneBindChunkIsImportedWhole_andAReImportSkipsEveryExistingRowIncludingTheLaterChunk()
            throws Exception {
        // The existing-names lookup binds one parameter per name and is chunked like the INSERT. The re-import sends
        // every row with a blank version and a legacy name: a row the lookup missed (one in a later chunk) would be
        // judged as new and refused 400, so this fails if the chunking drops a name. (jOOQ also splits a long IN
        // list for PostgreSQL, so an unchunked lookup does not fail here; this pins the skip, not the splitting.)
        String t = newTenant();
        int n = 33_000;   // over PostgreSQL's 32767 bind limit
        List<Map<String, Object>> first = new java.util.ArrayList<>(n);
        List<Map<String, Object>> again = new java.util.ArrayList<>(n);
        for (int i = 0; i < n; i++) {
            first.add(row("legacy_big_" + i, "v1"));
            again.add(row("legacy_big_" + i, ""));
        }

        repo.importCollectionsBatch(t, first);
        assertThat(rowCount(t)).as("every row of the first import landed").isEqualTo(n);
        repo.importCollectionsBatch(t, again);

        assertThat(rowCount(t)).as("the re-import added nothing").isEqualTo(n);
        assertThat(version(t, "legacy_big_0")).isEqualTo("v1");
        assertThat(version(t, "legacy_big_" + (n - 1))).as("a row in the later chunk was skipped, not judged").isEqualTo("v1");
    }

    private int rowCount(String tenant) throws Exception {
        try (var su = pg.createConnection("")) {
            return org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES)
                .fetchCount(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS,
                    dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.TENANT_ID.eq(tenant));
        }
    }

    @Test
    void anImportRowThatCarriesAVersionKeepsIt_overTheNamesSuffix() {
        String t = newTenant();
        String name = conformant("keeps", 3);

        repo.importCollection(t, row(name, "v7"));

        assertThat(version(t, name)).isEqualTo("v7");
    }

    @Test
    void anImportOfANonConformantNameWithNoVersionIsRefused_andWritesNothing() {
        String t = newTenant();
        String legacy = "my_legacy_collection_" + seq.incrementAndGet();
        String conformantOk = conformant("ok", 2);

        assertThatThrownBy(() -> repo.importCollection(t, row(legacy, null)))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("model_version").hasMessageContaining(legacy);
        assertThatThrownBy(() -> repo.importCollectionsBatch(t, List.of(row(conformantOk, null), row(legacy, "  "))))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining(legacy);
        assertThat(repo.getCollection(t, legacy)).isNull();
        assertThat(repo.getCollection(t, conformantOk)).as("the batch is refused whole").isNull();
    }

    @Test
    void aNonConformantNameThatCarriesAVersionIsImportedAsSent() {
        String t = newTenant();
        String legacy = "legacy_with_version_" + seq.incrementAndGet();

        repo.importCollection(t, row(legacy, "v1"));

        assertThat(version(t, legacy)).isEqualTo("v1");
    }
}
