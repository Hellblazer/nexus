// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import dev.nexus.service.db.TokenStore;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.net.http.HttpResponse;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * nexus-6pfgb.43 slice A: {@code POST /v1/admin/chunk-keys/sweep}, the per-tenant retire-keys sweep, through the real
 * {@link NexusService}, its {@code AuthFilter} and a real PostgreSQL.
 *
 * <p>What is pinned, each against rows a superuser reads back: a request that does not say {@code dry_run:false}
 * writes nothing; apply strips exactly the keys outside the allow-list and keeps the rest; a rerun finds nothing;
 * {@code last_written_at} is untouched; another tenant, a {@code quarantine-} collection and a {@code taxonomy}
 * collection are left alone; a collection whose census reads {@code legacy-unmanifested} is skipped and a row with a
 * 16-byte chash is not rewritten; the preconditions are counted from the rows and refuse apply until each is accepted;
 * batches and the per-call batch limit work; a data credential and no credential are refused.
 */
class ChunkKeySweepIntegrationTest extends ChunkKeysTestBase {

    private static final List<String> SIX_RESIDUES = List.of("legacy_label_rows", "keyless_notes", "keyless_ttl_rows",
        "homeless_documents", "null_span_rows", "generated_nulls");

    private static Map<String, Map<String, Object>> rows(Object... pairs) {
        Map<String, Map<String, Object>> m = new LinkedHashMap<>();
        for (int i = 0; i < pairs.length; i += 2) {
            @SuppressWarnings("unchecked")
            Map<String, Object> meta = (Map<String, Object>) pairs[i + 1];
            m.put((String) pairs[i], meta);
        }
        return m;
    }

    private static Map<String, Object> legacy(Map<String, Object> keyed) {
        Map<String, Object> m = new LinkedHashMap<>(keyed);
        m.put("title", "a title");
        m.put("tags", "a,b");
        m.put("chunk_index", 3);
        m.put("source_path", "/x/y");
        m.put("git_meta", Map.of("branch", "main"));
        m.put("content_hash", "h");
        m.put("frecency_score", 0.25);
        return m;
    }

    // ---- the happy path: a fixture swept clean ------------------------------------------------------------------

    /** One tenant with a swept-able collection, a quarantine sibling and a taxonomy collection; another tenant beside. */
    private record Fx(String tenant, String token, String live, String quarantine, String taxonomy,
                      List<String> legacyRows, List<String> cleanRows, List<String> quarantineRows,
                      List<String> taxonomyRows) {}

    private Fx tenantFixture(String tenant) throws Exception {
        String token = tenantToken(tenant);
        String live = conformant(tenant);
        String quarantine = "quarantine-" + live;
        String taxonomy = TAXONOMY_PREFIX + tenant;
        Map<String, Map<String, Object>> liveRows = new LinkedHashMap<>();
        for (int i = 1; i <= 5; i++) {
            liveRows.put("legacy" + i, legacy(keyed(tenant, live, "legacy" + i)));
        }
        liveRows.put("clean1", keyed(tenant, live, "clean1"));
        liveRows.put("clean2", keyed(tenant, live, "clean2"));
        List<String> hex = seed(tenant, live, liveRows);
        List<String> quarantined = seed(tenant, quarantine, rows(
            "q1", legacy(keyed(tenant, quarantine, "q1", "quarantined_at", "2026-01-02T00:00:00Z",
                "origin_collection", live)),
            "q2", legacy(keyed(tenant, quarantine, "q2", "quarantined_at", "2026-01-02T00:00:00Z",
                "origin_collection", live))));
        List<String> taxo = seed(tenant, taxonomy, rows("t1", legacy(Map.of("anything", "goes"))));
        return new Fx(tenant, token, live, quarantine, taxonomy, hex.subList(0, 5), hex.subList(5, 7), quarantined,
            taxo);
    }

    private Map<String, Object> applyBody(Object... extra) {
        return plan(prepend("dry_run", false, extra));
    }

    private static Object[] prepend(Object k, Object v, Object[] rest) {
        Object[] out = new Object[rest.length + 2];
        out[0] = k;
        out[1] = v;
        System.arraycopy(rest, 0, out, 2, rest.length);
        return out;
    }

    @Test
    void aRequestThatDoesNotSayDryRunFalseWritesNothing_andCountsEachCollection() throws Exception {
        Fx f = tenantFixture("sw-dry");
        Map<String, Object> before = metadataOf(f.tenant(), f.live(), f.legacyRows().get(0));

        HttpResponse<String> r = sweep(f.token(), plan());   // no dry_run field at all

        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        JsonNode body = json(r);
        assertThat(body.path("dry_run").asBoolean()).isTrue();
        assertThat(body.path("collections")).hasSize(1);
        JsonNode c = body.path("collections").get(0);
        assertThat(c.path("collection").asText()).isEqualTo(f.live());
        assertThat(c.path("retired_key_rows").asLong()).isEqualTo(5);
        assertThat(c.path("sweepable_rows").asLong()).isEqualTo(5);
        assertThat(body.path("totals").path("sweepable_rows").asLong()).isEqualTo(5);
        assertThat(body.path("applicable").asBoolean()).isTrue();
        // The quarantine and taxonomy collections hold retired keys and are not counted: the allow-list leaves them be.
        assertThat(r.body()).doesNotContain(f.quarantine()).doesNotContain(f.taxonomy());

        JsonNode cost = body.path("batch_cost");
        assertThat(cost.path("rolled_back").asBoolean()).isTrue();
        assertThat(cost.path("rows").asLong()).as("one measured batch took the five rows").isEqualTo(5);

        assertThat(metadataOf(f.tenant(), f.live(), f.legacyRows().get(0)))
            .as("the measured batch was rolled back: nothing changed").isEqualTo(before);
        assertThat(retiredRowCount(f.tenant(), f.live())).isEqualTo(5);
    }

    @Test
    void applyStripsExactlyTheKeysOutsideTheAllowList_andKeepsEverythingElse() throws Exception {
        Fx f = tenantFixture("sw-apply");
        Map<String, Object> cleanBefore = metadataOf(f.tenant(), f.live(), f.cleanRows().get(0));

        HttpResponse<String> r = sweep(f.token(), applyBody());

        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        JsonNode body = json(r);
        assertThat(body.path("dry_run").asBoolean()).isFalse();
        assertThat(body.path("rows_swept").asLong()).isEqualTo(5);
        assertThat(body.path("done").asBoolean()).isTrue();
        assertThat(body.path("remaining_rows").asLong()).isZero();
        for (String hex : f.legacyRows()) {
            assertThat(metadataOf(f.tenant(), f.live(), hex).keySet())
                .as("only the CHUNK keys survive on a row that held no ROW key")
                .containsExactlyInAnyOrder("chunk_text_hash", "embedding_model");
            assertThat(metadataOf(f.tenant(), f.live(), hex).get("chunk_text_hash")).isEqualTo(hex);
        }
        assertThat(metadataOf(f.tenant(), f.live(), f.cleanRows().get(0))).isEqualTo(cleanBefore);
    }

    @Test
    void aRowThatHoldsRowKeysKeepsThem_whileItsRetiredKeysGo() throws Exception {
        String tenant = "sw-rowkeys";
        String token = tenantToken(tenant);
        String live = conformant(tenant);
        List<String> hex = seed(tenant, live, rows("r1", legacy(keyed(tenant, live, "r1",
            "quarantined_at", "2026-01-02T00:00:00Z", "origin_collection", "elsewhere"))));

        assertThat(sweep(token, applyBody()).statusCode()).isEqualTo(200);

        assertThat(metadataOf(tenant, live, hex.get(0)).keySet())
            .containsExactlyInAnyOrder("chunk_text_hash", "embedding_model", "quarantined_at", "origin_collection");
    }

    @Test
    void applyDoesNotMoveLastWrittenAt_thePlainDefaultOfAnUpdateThatSetsMetadataAlone() throws Exception {
        Fx f = tenantFixture("sw-stamp");
        assertThat(lastWrittenAt(f.tenant(), f.live(), f.legacyRows().get(0))).isEqualTo(STAMP);

        assertThat(sweep(f.token(), applyBody()).statusCode()).isEqualTo(200);

        for (String hex : f.legacyRows()) {
            assertThat(lastWrittenAt(f.tenant(), f.live(), hex)).as("the reaper's grace anchor").isEqualTo(STAMP);
        }
    }

    @Test
    void aRerunFindsNothing() throws Exception {
        Fx f = tenantFixture("sw-idem");
        assertThat(json(sweep(f.token(), applyBody())).path("rows_swept").asLong()).isEqualTo(5);

        JsonNode again = json(sweep(f.token(), applyBody()));

        assertThat(again.path("rows_swept").asLong()).isZero();
        assertThat(again.path("done").asBoolean()).isTrue();
        assertThat(again.path("remaining_rows").asLong()).isZero();
        JsonNode dry = json(sweep(f.token(), plan()));
        assertThat(dry.path("totals").path("retired_key_rows").asLong()).isZero();
        assertThat(dry.path("collections")).isEmpty();
    }

    @Test
    void anotherTenantAndTheBypassCollectionsAreUntouched() throws Exception {
        Fx mine = tenantFixture("sw-iso-a");
        Fx theirs = tenantFixture("sw-iso-b");
        Map<String, Object> theirRow = metadataOf(theirs.tenant(), theirs.live(), theirs.legacyRows().get(0));
        Map<String, Object> quarantined = metadataOf(mine.tenant(), mine.quarantine(), mine.quarantineRows().get(0));
        Map<String, Object> taxonomy = metadataOf(mine.tenant(), mine.taxonomy(), mine.taxonomyRows().get(0));

        assertThat(sweep(mine.token(), applyBody()).statusCode()).isEqualTo(200);

        assertThat(metadataOf(theirs.tenant(), theirs.live(), theirs.legacyRows().get(0)))
            .as("the other tenant's row").isEqualTo(theirRow);
        assertThat(retiredRowCount(theirs.tenant(), theirs.live())).isEqualTo(5);
        assertThat(metadataOf(mine.tenant(), mine.quarantine(), mine.quarantineRows().get(0)))
            .as("a quarantine collection's retired keys wait for the restore, which strips to the CHUNK keys")
            .isEqualTo(quarantined).containsKey("title");
        assertThat(metadataOf(mine.tenant(), mine.taxonomy(), mine.taxonomyRows().get(0)))
            .as("a taxonomy collection is never swept").isEqualTo(taxonomy).containsKey("title");
    }

    @Test
    @SuppressWarnings("unchecked")
    void theTenantPredicateIsExplicit_aRoleThatSeesEveryTenantStillSweepsOnlyOne() throws Exception {
        // The owner role is not row-filtered (RF-16: 599799 chunk rows under every tenant GUC), so a sweep that leaned
        // on the RLS stamp would rewrite or keep re-reading every tenant's rows. A superuser pool stands in for that
        // role, and the other tenant holds legacy rows with the SAME COLLECTION AND CHASHES: only the tenant predicate
        // keeps them apart, and a batch that still selected them would never run dry.
        Fx mine = tenantFixture("sw-owner-a");
        String theirTenant = "sw-owner-b";
        tenantToken(theirTenant);
        // The same chashes under the other tenant: a statement that named (collection, chash) alone would reach them.
        seedHashes(theirTenant, mine.live(), mine.legacyRows(), legacy(Map.of("embedding_model", MODEL)));
        List<String> theirs = mine.legacyRows();
        Map<String, Object> theirRow = metadataOf(theirTenant, mine.live(), theirs.get(0));
        try (var superDs = PgContainerHelper.superuserDataSource(pg)) {
            var scope = new dev.nexus.service.db.TenantScope(superDs);
            var sweep = new dev.nexus.service.vectors.ChunkKeySweep(scope,
                new dev.nexus.service.vectors.PgVectorRepository(scope, embedder, embedder));
            var plan = dev.nexus.service.vectors.ChunkKeyPlan.of(
                (List<String>) plan().get("allowed_keys"), List.of(TAXONOMY_PREFIX, "quarantine-"), List.of());

            var applied = sweep.apply(mine.tenant(), plan, java.util.Set.of(), null, 1000, 5,
                java.time.Duration.ofSeconds(30));

            assertThat(applied.rowsSwept()).isEqualTo(5);
            assertThat(applied.done()).as("the second batch finds nothing: the other tenant's rows are not its rows")
                .isTrue();
            assertThat(applied.batches()).isEqualTo(2);
            assertThat(applied.remainingRows()).isZero();
        }
        assertThat(retiredRowCount(mine.tenant(), mine.live())).isZero();
        assertThat(metadataOf(theirTenant, mine.live(), theirs.get(0))).isEqualTo(theirRow);
        assertThat(retiredRowCount(theirTenant, mine.live())).as("the other tenant's rows").isEqualTo(5);
    }

    // ---- batches ------------------------------------------------------------------------------------------------

    @Test
    void theBatchSizeAndTheBatchLimitBoundACall_andTheCallerLoopsUntilDone() throws Exception {
        String tenant = "sw-batch";
        String token = tenantToken(tenant);
        String live = conformant(tenant);
        Map<String, Map<String, Object>> five = new LinkedHashMap<>();
        for (int i = 1; i <= 5; i++) five.put("b" + i, legacy(keyed(tenant, live, "b" + i)));
        seed(tenant, live, five);

        JsonNode first = json(sweep(token, applyBody("batch_size", 2, "max_batches", 2)));
        assertThat(first.path("rows_swept").asLong()).isEqualTo(4);
        assertThat(first.path("batches").asInt()).isEqualTo(2);
        assertThat(first.path("done").asBoolean()).as("one row is left for the next call").isFalse();
        assertThat(first.path("remaining_rows").asLong()).isEqualTo(1);

        JsonNode second = json(sweep(token, applyBody("batch_size", 2, "max_batches", 2)));
        assertThat(second.path("rows_swept").asLong()).isEqualTo(1);
        assertThat(second.path("done").asBoolean()).isTrue();
        assertThat(retiredRowCount(tenant, live)).isZero();
    }

    // ---- what the sweep leaves alone ---------------------------------------------------------------------------

    @Test
    void aCollectionWhoseCensusReadsLegacyUnmanifestedIsSkipped_andItsRowsAreNotCounted() throws Exception {
        String tenant = "sw-census";
        String token = tenantToken(tenant);
        String blocked = conformant(tenant + "-blocked");
        String open = conformant(tenant + "-open");
        // A live document with no manifest row at all, found through the chunk's own forward key: the census's
        // legacy-unmanifested bucket. The sweep would remove the key the census reads it by.
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCatalogDocument(DSL.using(su, SQLDialect.POSTGRES), tenant, "census.1");
        }
        List<String> held = seed(tenant, blocked, rows(
            "held", legacy(keyed(tenant, blocked, "held", "catalog_doc_id", "census.1"))));
        List<String> swept = seed(tenant, open, rows("free", legacy(keyed(tenant, open, "free"))));
        List<String> accept = List.of("homeless_documents", "generated_nulls");

        JsonNode dry = json(sweep(token, plan("accept", accept)));
        assertThat(dry.path("totals").path("sweepable_rows").asLong()).as("the blocked collection is not counted")
            .isEqualTo(1);
        assertThat(dry.path("totals").path("blocked_rows").asLong()).isEqualTo(1);
        JsonNode blockedRow = null;
        for (JsonNode c : dry.path("collections")) {
            if (blocked.equals(c.path("collection").asText())) blockedRow = c;
        }
        assertThat(blockedRow).isNotNull();
        assertThat(blockedRow.path("blocked").asText()).isEqualTo("legacy_unmanifested");
        assertThat(blockedRow.path("legacy_unmanifested").asLong()).isEqualTo(1);

        JsonNode applied = json(sweep(token, applyBody("accept", accept)));
        assertThat(applied.path("rows_swept").asLong()).isEqualTo(1);
        assertThat(applied.path("blocked_collections")).hasSize(1);
        assertThat(metadataOf(tenant, open, swept.get(0)).keySet())
            .containsExactlyInAnyOrder("chunk_text_hash", "embedding_model");
        assertThat(metadataOf(tenant, blocked, held.get(0))).as("its forward key is still there for the census")
            .containsKey("catalog_doc_id").containsKey("title");
    }

    @Test
    void aRowWithASixteenByteChashIsNotRewritten_theWidthCheckWouldRefuseTheUpdate() throws Exception {
        String tenant = "sw-width";
        String token = tenantToken(tenant);
        String live = conformant(tenant);
        List<String> ok = seed(tenant, live, rows("ok", legacy(keyed(tenant, live, "ok"))));
        byte[] narrow = HexFormat.of().parseHex("ab".repeat(16));
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.alterTable(CHUNKS).dropConstraint("chunks_chash_octet_check").execute();
            try {
                ctx.insertInto(CHUNKS)
                    .set(CHUNKS.TENANT_ID, tenant).set(CHUNKS.COLLECTION, live).set(CHUNKS.CHASH, narrow)
                    .set(CHUNKS.EMBEDDING_MODEL, MODEL).set(CHUNKS.CHUNK_TEXT, "narrow")
                    .set(CHUNKS.EMBEDDING_384, dev.nexus.service.jooq.binding.Vector.of(new float[384]))
                    .set(CHUNKS.METADATA, org.jooq.JSONB.jsonb("{\"title\":\"legacy narrow\"}"))
                    .execute();
            } finally {
                PgContainerHelper.readdChashOctetChecksNotValid(su);
            }
        }

        JsonNode dry = json(sweep(token, plan()));
        JsonNode c = dry.path("collections").get(0);
        assertThat(c.path("retired_key_rows").asLong()).isEqualTo(2);
        assertThat(c.path("wrong_width_rows").asLong()).isEqualTo(1);
        assertThat(c.path("sweepable_rows").asLong()).isEqualTo(1);
        assertThat(dry.path("preconditions").path("wrong_width_rows").asLong()).isEqualTo(1);

        JsonNode applied = json(sweep(token, applyBody()));
        assertThat(applied.path("rows_swept").asLong()).isEqualTo(1);
        assertThat(applied.path("done").asBoolean()).isTrue();
        assertThat(metadataOf(tenant, live, ok.get(0)).keySet()).containsExactlyInAnyOrder("chunk_text_hash",
            "embedding_model");
        try (Connection su = pg.createConnection("")) {
            var narrowMeta = DSL.using(su, SQLDialect.POSTGRES).select(CHUNKS.METADATA).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.CHASH.eq(narrow)).fetchOne(CHUNKS.METADATA);
            assertThat(narrowMeta.data()).contains("legacy narrow");
        }
    }

    // ---- the preconditions --------------------------------------------------------------------------------------

    /**
     * One tenant built so each precondition counts exactly one: c1 is owned by a legacy manifest row and carries a label
     * key; c2 is owned by a manifest row with no span and carries a line key; c3 records a TTL no owner records; a note
     * with no put_at and no collection; an indexed document with none of the derived keys. c5/c6 break the two
     * equalities, c8 is half stamped, c9 carries a row key in a live collection.
     */
    private record Pre(String tenant, String token, String live) {}

    private Pre preconditionTenant(String tenant) throws Exception {
        String token = tenantToken(tenant);
        String live = conformant(tenant);
        String model = MODEL;
        Map<String, Map<String, Object>> r = new LinkedHashMap<>();
        r.put("c1", keyed(tenant, live, "c1", "section_title", "S"));
        r.put("c2", keyed(tenant, live, "c2", "line_start", 3));
        r.put("c3", keyed(tenant, live, "c3", "ttl_days", 7));
        r.put("c4", keyed(tenant, live, "c4"));
        r.put("c5", new LinkedHashMap<>(Map.of("chunk_text_hash", "deadbeef", "embedding_model", model)));
        r.put("c6", new LinkedHashMap<>(Map.of("chunk_text_hash", hexOf(tenant, live, "c6"),
            "embedding_model", "some-other-model")));
        r.put("c8", keyed(tenant, live, "c8", "quarantined_by", "engine"));
        r.put("c9", keyed(tenant, live, "c9", "quarantined_at", "2026-01-02T00:00:00Z"));
        seed(tenant, live, r);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // c1, c2 and c4 are owned by a document of the collection (its manifest rows are legacy: labels_written
            // false, spans NULL), through the helper every other fixture uses.
            PgContainerHelper.ownChunks(ctx, tenant, live, hexOf(tenant, live, "c1"), hexOf(tenant, live, "c2"),
                hexOf(tenant, live, "c4"));
            // A note: content type knowledge, no put_at in its meta, no physical collection.
            ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                    CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.CONTENT_TYPE)
                .values(tenant, "note.1", "a note", "knowledge").execute();
        }
        return new Pre(tenant, token, live);
    }

    @Test
    void theDryRunCountsEachPreconditionFromTheRows() throws Exception {
        Pre p = preconditionTenant("sw-pre-count");

        JsonNode body = json(sweep(p.token(), plan()));

        JsonNode pre = body.path("preconditions");
        assertThat(pre.path("legacy_label_rows").asLong()).as("c1: a legacy manifest row whose label is on the row")
            .isEqualTo(1);
        assertThat(pre.path("null_span_rows").asLong()).as("c2: a manifest row with no span, a row with one").isEqualTo(1);
        assertThat(pre.path("keyless_ttl_rows").asLong()).as("c3: a TTL only the row records").isEqualTo(1);
        assertThat(pre.path("keyless_notes").asLong()).as("note.1 has no put_at").isEqualTo(1);
        assertThat(pre.path("homeless_documents").asLong()).as("note.1 has no physical collection").isEqualTo(1);
        assertThat(pre.path("generated_nulls").asLong()).as("the owning indexed document has none of the derived keys")
            .isEqualTo(1);
        assertThat(pre.path("chunk_text_hash_mismatch").asLong()).isEqualTo(1);
        assertThat(pre.path("embedding_model_mismatch").asLong()).isEqualTo(1);
        assertThat(pre.path("quarantine_implication_violations").asLong()).isEqualTo(1);
        assertThat(pre.path("row_keys_on_live_rows").asLong()).as("c8 and c9").isEqualTo(2);
        assertThat(pre.path("wrong_width_rows").asLong()).isZero();

        List<String> blocking = new ArrayList<>();
        body.path("blocking").forEach(n -> blocking.add(n.asText()));
        assertThat(blocking).containsExactlyInAnyOrderElementsOf(SIX_RESIDUES);
        assertThat(body.path("applicable").asBoolean()).isFalse();
        assertThat(body.path("totals").path("sweepable_rows").asLong()).as("c1, c2 and c3 hold a retired key")
            .isEqualTo(3);
    }

    @Test
    void applyIsRefusedWhileAResidueIsNotAccepted_andProceedsOnceEachIsNamed() throws Exception {
        Pre p = preconditionTenant("sw-pre-gate");

        HttpResponse<String> refused = sweep(p.token(), applyBody());
        assertThat(refused.statusCode()).as(refused.body()).isEqualTo(409);
        JsonNode body = json(refused);
        assertThat(body.path("reason").asText()).isEqualTo("sweep_preconditions_not_met");
        assertThat(body.path("blocking")).hasSize(6);
        assertThat(body.path("report").path("preconditions").path("keyless_notes").asLong()).isEqualTo(1);
        assertThat(retiredRowCount(p.tenant(), p.live())).as("nothing was written").isEqualTo(3);

        HttpResponse<String> partial = sweep(p.token(), applyBody("accept", SIX_RESIDUES.subList(0, 5)));
        assertThat(partial.statusCode()).as("five of six accepted is still refused").isEqualTo(409);
        assertThat(json(partial).path("blocking")).hasSize(1);

        HttpResponse<String> ok = sweep(p.token(), applyBody("accept", SIX_RESIDUES));
        assertThat(ok.statusCode()).as(ok.body()).isEqualTo(200);
        assertThat(json(ok).path("rows_swept").asLong()).isEqualTo(3);
        assertThat(retiredRowCount(p.tenant(), p.live())).isZero();
    }

    @Test
    void anAcceptNamingSomethingThatIsNotAnAcceptableResidueIsABadRequest() throws Exception {
        Fx f = tenantFixture("sw-accept");
        HttpResponse<String> r = sweep(f.token(), applyBody("accept", List.of("wrong_width_rows")));
        assertThat(r.statusCode()).isEqualTo(400);
        assertThat(r.body()).contains("wrong_width_rows");
    }

    // ---- authorization and shape --------------------------------------------------------------------------------

    @Test
    void noCredentialIsRefused_aDataCredentialIsForbidden_aGetIsNotAllowed_aBadPlanIsABadRequest() throws Exception {
        Fx f = tenantFixture("sw-auth");
        assertThat(sweep(null, plan()).statusCode()).isEqualTo(401);
        assertThat(sweep(token("sw-auth", TokenStore.SCOPE_DATA), plan()).statusCode()).isEqualTo(403);
        assertThat(get("/v1/admin/chunk-keys/sweep", f.token()).statusCode()).isEqualTo(405);

        Map<String, Object> noChunkKey = plan();
        noChunkKey.put("allowed_keys", List.of("quarantined_at", "origin_collection"));
        HttpResponse<String> bad = sweep(f.token(), noChunkKey);
        assertThat(bad.statusCode()).isEqualTo(400);
        assertThat(bad.body()).contains("must keep the CHUNK key");

        Map<String, Object> keepsATitle = plan();
        @SuppressWarnings("unchecked")
        List<String> withTitle = new ArrayList<>((List<String>) keepsATitle.get("allowed_keys"));
        withTitle.add("title");
        keepsATitle.put("allowed_keys", withTitle);
        assertThat(sweep(f.token(), keepsATitle).statusCode()).as("a stale client cannot widen the allow-list")
            .isEqualTo(400);
        assertThat(retiredRowCount(f.tenant(), f.live())).isEqualTo(5);
    }
}
