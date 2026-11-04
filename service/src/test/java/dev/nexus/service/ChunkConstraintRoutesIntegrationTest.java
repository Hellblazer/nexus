// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.fasterxml.jackson.databind.JsonNode;
import dev.nexus.service.ChunkReaper.RunResult;
import dev.nexus.service.ChunkReaper.Settings;
import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.LadderRepository;
import dev.nexus.service.db.Rdr192BackfillGate;
import dev.nexus.service.db.TokenStore;
import dev.nexus.service.vectors.ChunkConstraintAdmin;
import dev.nexus.service.vectors.ChunkMetadataKeys;
import dev.nexus.service.vectors.ReaperRepository;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestMethodOrder;

import java.net.http.HttpResponse;
import java.sql.Connection;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * nexus-6pfgb.43 slice B: the table-wide routes {@code add-constraints} and {@code validate-constraints}, through the
 * real service and a real PostgreSQL, in the order an operator runs them. One container, so the steps are ordered: the
 * constraints, once added, are on {@code nexus.chunks} for every test after them.
 *
 * <p>Three tenants. {@code cc-a} holds retired keys (to be swept), a quarantine row that carries them and a chunk the
 * real reaper moved to quarantine with its retired document keys; {@code cc-b} is clean; {@code cc-c} is clean except
 * one row whose {@code chunk_text_hash} is not its chash (a row the allow-list sweep does not touch and the equality
 * CHECK would refuse at VALIDATE).
 */
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class ChunkConstraintRoutesIntegrationTest extends ChunkKeysTestBase {

    private static final String A = "cc-a";
    private static final String B = "cc-b";
    private static final String C = "cc-c";
    private static final Clock CLOCK = Clock.fixed(Instant.parse("2026-10-01T12:00:00Z"), ZoneOffset.UTC);
    private static final List<String> ALL_RESIDUES = List.of("legacy_label_rows", "keyless_notes", "keyless_ttl_rows",
        "homeless_documents", "null_span_rows", "generated_nulls");
    private static final List<String> FIVE = List.of("chunks_metadata_keys_check", "chunks_chunk_text_hash_check",
        "chunks_embedding_model_check", "chunks_quarantine_keys_check", "chunks_chash_octet_check");

    private String tokenA;
    private String liveA;
    private String quarantineA;
    private String liveC;
    private String mismatchedHash;
    private String reaperOrigin;
    private String reaperChash;
    private String restoreOrigin;
    private String restoreChash;

    private ChunkConstraintAdmin admin() {
        return new ChunkConstraintAdmin(new AdminConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword()));
    }

    @org.junit.jupiter.api.BeforeAll
    void seedTenants() throws Exception {
        tokenA = tenantToken(A);
        tenantToken(B);
        tenantToken(C);
        liveA = conformant("cc-a");
        quarantineA = "quarantine-" + liveA;
        Map<String, Map<String, Object>> a = new LinkedHashMap<>();
        for (int i = 1; i <= 3; i++) {
            Map<String, Object> m = keyed(A, liveA, "a" + i, "title", "t" + i, "tags", "x,y", "doc_id", "gone");
            a.put("a" + i, m);
        }
        seed(A, liveA, a);
        seed(A, quarantineA, Map.of("qa1", keyed(A, quarantineA, "qa1", "title", "t", "quarantined_at",
            "2026-01-02T00:00:00Z", "origin_collection", liveA)));
        seed(B, conformant("cc-b"), Map.of("b1", keyed(B, conformant("cc-b"), "b1"),
            "b2", keyed(B, conformant("cc-b"), "b2")));
        liveC = conformant("cc-c");
        Map<String, Object> badHash = new LinkedHashMap<>(keyed(C, liveC, "c1"));
        badHash.put("chunk_text_hash", "0".repeat(64));
        seed(C, liveC, Map.of("c1", badHash, "c2", keyed(C, liveC, "c2")));
        mismatchedHash = hexOf(C, liveC, "c1");

        reaperOrigin = "code" + "_".repeat(2) + "cc-r" + "_".repeat(2) + MODEL + "_".repeat(2) + "v1";
        reaperChash = reaperQuarantined("cc-reaper", reaperOrigin, "gc-restore", true);
        restoreOrigin = "code" + "_".repeat(2) + "cc-q" + "_".repeat(2) + MODEL + "_".repeat(2) + "v1";
        restoreChash = reaperQuarantined("cc-reaper", restoreOrigin, "quarantine-restore", false);
    }

    /**
     * A chunk holding retired document keys, moved to its quarantine sibling by the real reaper. With {@code
     * reReference} a live document of the origin names it again (the foreign key bypassed, the one way a manifest row
     * can name a chunk that lives only in quarantine), which is what {@code gc_restore_rereferenced} needs.
     */
    private String reaperQuarantined(String tenant, String origin, String slug, boolean reReference) throws Exception {
        tenantToken(tenant);
        String doc = tenant + "." + slug;
        String chash = Chash.ofText(origin + "/" + slug).toHex();
        Map<String, Object> legacy = new LinkedHashMap<>();
        legacy.put("title", slug);
        legacy.put("catalog_doc_id", doc);
        legacy.put("doc_id", doc);
        legacy.put("chunk_index", 0);
        legacy.put("content_hash", "h-" + slug);
        legacy.put("tags", "a,b");
        legacy.put("chunk_text_hash", chash);
        legacy.put("embedding_model", MODEL);
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, tenant, origin);
            PgContainerHelper.insertChunks(ctx, tenant, origin, List.of(chash), List.of(slug + " text"),
                embedder.embed(List.of(slug + " text")), List.of(legacy));
        }
        var ladder = new LadderRepository(tenantScope);
        ladder.record(tenant, Rdr192BackfillGate.RUNG_NAME, "7.99.0", "");
        RunResult run = new ChunkReaper(new ReaperRepository(tenantScope), vectors, repo,
            new Rdr192BackfillGate(ladder), () -> List.of(tenant), Settings.defaults(), CLOCK).runOnce(Duration.ZERO);
        assertThat(run.tenant(tenant).collection(origin).moved()).as("fixture: the reaper moved it").isEqualTo(1);
        assertThat(metadataOf(tenant, "quarantine-" + origin, chash)).as("fixture: it carries its retired keys")
            .containsKeys("title", "doc_id", "catalog_doc_id", "quarantined_by");
        if (reReference) {
            repo.upsertDocument(tenant, Map.of("tumbler", doc, "title", slug, "content_type", "code", "corpus", "code",
                "physical_collection", origin, "chunk_count", 1));
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                PgContainerHelper.dropCatalogChunksFk(su);
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID,
                        CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                        CATALOG_DOCUMENT_CHUNKS.COLLECTION, CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL)
                    .values(tenant, doc, 0, Chash.fromHex(chash).toBytes(), origin,
                        PgContainerHelper.collectionModel(ctx, tenant, origin)).execute();
                PgContainerHelper.readdCatalogChunksFkNotValid(su);
            }
        }
        return chash;
    }

    private static List<String> tenantNames(JsonNode body) {
        List<String> out = new ArrayList<>();
        body.path("tenants").forEach(t -> out.add(t.path("tenant").asText()));
        return out;
    }

    private HttpResponse<String> add(String bearer, Object... extra) throws Exception {
        return post("/v1/admin/chunk-keys/add-constraints", bearer, plan(extra));
    }

    private HttpResponse<String> validate(String bearer) throws Exception {
        return post("/v1/admin/chunk-keys/validate-constraints", bearer, plan());
    }

    // ---- 0. before the sweep: a legacy-keyed row is still written --------------------------------------------

    @Test
    @Order(0)
    void aLegacyKeyedRowIsStillUpsertedBeforeTheSweep_noCheckExistsYet() throws Exception {
        String a1 = hexOf(A, liveA, "a1");

        vectors.upsertChunks(A, liveA, List.of(a1), List.of("a1 text"),
            List.of(Map.<String, Object>of("source_path", "/legacy/writer", "title", "t1")));

        assertThat(metadataOf(A, liveA, a1)).as("the write landed, merged onto the row").containsEntry("source_path",
            "/legacy/writer");
        assertThat(admin().states()).filteredOn(ChunkConstraintAdmin.State::present)
            .extracting(ChunkConstraintAdmin.State::name).containsExactly("chunks_chash_octet_check");
    }

    // ---- 1. refusals before anything is swept -------------------------------------------------------------------

    @Test
    @Order(1)
    void addIsRefusedWhileATenantIsUnswept_namingIt_andAddsNothing() throws Exception {
        HttpResponse<String> r = add(ROOT);

        assertThat(r.statusCode()).as(r.body()).isEqualTo(409);
        JsonNode body = json(r);
        assertThat(body.path("reason").asText()).isEqualTo("tenant_unswept");
        assertThat(tenantNames(body)).as("only the unswept tenant is named").containsExactly(A);
        assertThat(body.path("tenants").get(0).path("unswept_rows").asLong()).isEqualTo(3);
        assertThat(r.body()).doesNotContain(B).doesNotContain(C);
        assertThat(admin().states()).filteredOn(ChunkConstraintAdmin.State::present)
            .extracting(ChunkConstraintAdmin.State::name).containsExactly("chunks_chash_octet_check");
    }

    @Test
    @Order(2)
    void validateIsRefusedWhileATenantStillBreaksAConstraint_namingEach() throws Exception {
        HttpResponse<String> r = validate(ROOT);

        assertThat(r.statusCode()).as(r.body()).isEqualTo(409);
        JsonNode body = json(r);
        assertThat(body.path("reason").asText()).isEqualTo("tenant_violates_constraint");
        assertThat(tenantNames(body)).containsExactlyInAnyOrder(A, C);
        for (JsonNode t : body.path("tenants")) {
            if (A.equals(t.path("tenant").asText())) {
                assertThat(t.path("retired_key_rows").asLong()).isEqualTo(3);
            } else {
                assertThat(t.path("chunk_text_hash_mismatch").asLong()).isEqualTo(1);
            }
        }
    }

    @Test
    @Order(3)
    void theTableWideRoutesNeedTheRootToken() throws Exception {
        assertThat(add(tokenA).statusCode()).isEqualTo(403);
        assertThat(validate(tokenA).statusCode()).isEqualTo(403);
        assertThat(add(null).statusCode()).isEqualTo(401);
        assertThat(post("/v1/admin/chunk-keys/add-constraints", token(A, TokenStore.SCOPE_DATA), plan())
            .statusCode()).isEqualTo(403);
    }

    // ---- 2. sweep, add ------------------------------------------------------------------------------------------

    @Test
    @Order(4)
    void theSweepLeavesTheQuarantineRowsRetiredKeysAlone() throws Exception {
        HttpResponse<String> r = sweep(tokenA, plan("dry_run", false, "accept", ALL_RESIDUES));

        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        assertThat(json(r).path("rows_swept").asLong()).isEqualTo(3);
        assertThat(metadataOf(A, quarantineA, hexOf(A, quarantineA, "qa1"))).containsKeys("title", "quarantined_at");
        assertThat(metadataOf("cc-reaper", "quarantine-" + reaperOrigin, reaperChash))
            .as("a chunk the reaper moved still holds its retired keys: only the restore strips them")
            .containsKeys("title", "doc_id", "catalog_doc_id", "quarantined_by");
    }

    @Test
    @Order(5)
    void addAddsTheFourConstraintsNotValid_andARerunLeavesThemBe() throws Exception {
        HttpResponse<String> r = add(ROOT);

        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        JsonNode body = json(r);
        assertThat(body.path("tenants_checked").asInt()).isGreaterThanOrEqualTo(4);
        Map<String, String> actions = new LinkedHashMap<>();
        body.path("constraints").forEach(c -> actions.put(c.path("name").asText(), c.path("action").asText()));
        assertThat(actions).containsOnlyKeys("chunks_metadata_keys_check", "chunks_chunk_text_hash_check",
            "chunks_embedding_model_check", "chunks_quarantine_keys_check")
            .containsValue("added").doesNotContainValue("present");
        for (JsonNode c : body.path("constraints")) {
            assertThat(c.path("present").asBoolean()).isTrue();
            assertThat(c.path("validated").asBoolean()).as(c.path("name").asText() + " is NOT VALID").isFalse();
        }
        String allowList = body.path("constraints").get(0).path("definition").asText();
        assertThat(allowList).contains("quarantine-").contains("chunk_text_hash").contains("embedding_model")
            .contains("quarantined_by");
        assertThat(allowList).as("the bypass prefixes are exempt").contains("starts_with");

        JsonNode again = json(add(ROOT));
        again.path("constraints").forEach(c -> assertThat(c.path("action").asText()).isEqualTo("present"));
    }

    @Test
    @Order(6)
    void replaceDropsAndReAddsInOneStatement_forAChangedKeyTable() throws Exception {
        HttpResponse<String> r = add(ROOT, "replace", true);

        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        json(r).path("constraints").forEach(c -> {
            assertThat(c.path("action").asText()).isEqualTo("replaced");
            assertThat(c.path("validated").asBoolean()).isFalse();
        });
    }

    @Test
    @Order(7)
    void afterTheAdd_aLegacyKeyedWriteToALiveCollectionIsRefusedByTheDatabase_theBypassCollectionsAreNot()
            throws Exception {
        String hex = Chash.ofText("after-add legacy").toHex();
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThatThrownBy(() -> PgContainerHelper.insertChunks(ctx, B, conformant("cc-b"), List.of(hex),
                List.of("t"), List.of(new float[384]), List.of(Map.<String, Object>of("title", "legacy"))))
                .as("a document key on a live collection's row").hasMessageContaining("chunks_metadata_keys_check");
            PgContainerHelper.insertChunks(ctx, B, conformant("cc-b"), List.of(hex), List.of("t"),
                List.of(new float[384]), List.of(Map.<String, Object>of("chunk_text_hash", hex, "embedding_model",
                    MODEL)));
            String q = "quarantine-" + conformant("cc-b");
            PgContainerHelper.insertCollection(ctx, B, q, MODEL);
            PgContainerHelper.insertChunks(ctx, B, q, List.of(hex), List.of("t"), List.of(new float[384]),
                List.of(Map.<String, Object>of("title", "legacy is fine in quarantine")));
            String a1 = hexOf(A, liveA, "a1");
            assertThatThrownBy(() -> vectors.upsertChunks(A, liveA, List.of(a1), List.of("a1 text"),
                List.of(Map.<String, Object>of("title", "a late legacy writer"))))
                .as("the engine's own upsert path meets the same CHECK").hasMessageContaining("chunks_metadata_keys_check");
            vectors.upsertChunks(A, liveA, List.of(a1), List.of("a1 text"),
                List.of(Map.<String, Object>of("chunk_text_hash", a1, "embedding_model", MODEL)));
            assertThat(metadataOf(A, liveA, a1).keySet()).as("a swept row still takes a CHUNK-key write")
                .containsExactlyInAnyOrder("chunk_text_hash", "embedding_model");
            assertThatThrownBy(() -> PgContainerHelper.insertChunks(ctx, B, conformant("cc-b"),
                List.of(Chash.ofText("wrong hash").toHex()), List.of("t"), List.of(new float[384]),
                List.of(Map.<String, Object>of("chunk_text_hash", "0".repeat(64)))))
                .hasMessageContaining("chunks_chunk_text_hash_check");
            assertThatThrownBy(() -> PgContainerHelper.insertChunks(ctx, B, conformant("cc-b"),
                List.of(Chash.ofText("half stamped").toHex()), List.of("t"), List.of(new float[384]),
                List.of(Map.<String, Object>of("quarantined_by", "engine"))))
                .hasMessageContaining("chunks_quarantine_keys_check");
        }
    }

    // ---- 3. validate --------------------------------------------------------------------------------------------

    @Test
    @Order(8)
    void validateIsStillRefusedForTheTenantWhoseHashDiffers_andOnlyThatOne() throws Exception {
        HttpResponse<String> r = validate(ROOT);

        assertThat(r.statusCode()).as(r.body()).isEqualTo(409);
        JsonNode body = json(r);
        assertThat(tenantNames(body)).containsExactly(C);
        assertThat(body.path("tenants").get(0).path("chunk_text_hash_mismatch").asLong()).isEqualTo(1);
        assertThat(admin().states()).filteredOn(s -> "chunks_chunk_text_hash_check".equals(s.name()))
            .allSatisfy(s -> assertThat(s.validated()).isFalse());
    }

    @Test
    @Order(9)
    void validateValidatesAllFive_readingConvalidatedFirst_andARerunIsANoOp() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).update(CHUNKS)
                .set(CHUNKS.METADATA, JSONB.jsonb("{\"chunk_text_hash\":\"" + mismatchedHash
                    + "\",\"embedding_model\":\"" + MODEL + "\"}"))
                .where(CHUNKS.TENANT_ID.eq(C)).and(CHUNKS.CHASH.eq(Chash.fromHex(mismatchedHash).toBytes())).execute();
        }

        HttpResponse<String> r = validate(ROOT);

        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        Map<String, String> actions = new LinkedHashMap<>();
        json(r).path("constraints").forEach(c -> actions.put(c.path("name").asText(), c.path("action").asText()));
        assertThat(actions.keySet()).containsExactlyElementsOf(FIVE);
        assertThat(actions.values()).containsOnly("validated");
        assertThat(admin().states()).hasSize(5).allSatisfy(s -> {
            assertThat(s.present()).as(s.name()).isTrue();
            assertThat(s.validated()).as(s.name() + " is validated").isTrue();
        });

        JsonNode again = json(validate(ROOT));
        again.path("constraints").forEach(c -> assertThat(c.path("action").asText()).isEqualTo("already_valid"));
    }

    // ---- 4. the restores pass the validated CHECK ---------------------------------------------------------------

    @Test
    @Order(10)
    void gcRestoreRereferencedBringsALegacyKeyedQuarantineRowBackAsChunkKeysOnly_throughTheValidatedCheck()
            throws Exception {
        assertThat(vectors.restoreRereferenced("cc-reaper", "quarantine-" + reaperOrigin, reaperOrigin))
            .isEqualTo(1L);

        Map<String, Object> meta = metadataOf("cc-reaper", reaperOrigin, reaperChash);
        assertThat(meta.keySet()).as("only the CHUNK keys come back").isEqualTo(ChunkMetadataKeys.CHUNK);
        assertThat(meta.get("chunk_text_hash")).isEqualTo(reaperChash);
    }

    @Test
    @Order(11)
    void quarantineRestoreChunksBringsALegacyKeyedQuarantineRowBackAsChunkKeysOnly_throughTheValidatedCheck()
            throws Exception {
        var out = vectors.quarantineRestore("cc-reaper", restoreOrigin, "quarantine-" + restoreOrigin,
            List.of(restoreChash), "test", false, false);

        assertThat(out.restored()).containsExactly(restoreChash);
        assertThat(metadataOf("cc-reaper", restoreOrigin, restoreChash).keySet()).isEqualTo(ChunkMetadataKeys.CHUNK);
    }
}
