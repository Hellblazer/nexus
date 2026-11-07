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
        assertThat(cost.path("projection_is_lower_bound").asBoolean()).as("select only: a lower bound").isTrue();
        assertThat(cost.path("update_ms").isNull()).as("the update half of a batch is not measured").isTrue();
        assertThat(cost.path("samples")).hasSize(1);
        assertThat(cost.path("samples").get(0).path("rows").asLong()).as("the sampled batch SELECT saw the five rows")
            .isEqualTo(5);

        assertThat(metadataOf(f.tenant(), f.live(), f.legacyRows().get(0)))
            .as("nothing changed").isEqualTo(before);
        assertThat(retiredRowCount(f.tenant(), f.live())).isEqualTo(5);
    }

    // ---- the dry run is read-only (B4) --------------------------------------------------------------------------

    /**
     * The tuple-level xmax of every chunk row of a collection, as text. A row locked by FOR UPDATE or rewritten by an
     * UPDATE, even one rolled back, carries its locker's or updater's transaction id there; an untouched row reads 0.
     */
    private List<String> xmaxOf(String tenant, String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var xmax = DSL.cast(DSL.field(DSL.name("xmax")), org.jooq.impl.SQLDataType.VARCHAR);
            return DSL.using(su, SQLDialect.POSTGRES).select(xmax).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION.eq(collection))
                .orderBy(CHUNKS.CHASH).fetch(xmax);
        }
    }

    @Test
    void theDryRunRunsNoUpdateAndLocksNoRow_notEvenOneThatIsRolledBack() throws Exception {
        Fx f = tenantFixture("sw-readonly");
        assertThat(xmaxOf(f.tenant(), f.live())).as("fixture: untouched rows").containsOnly("0").hasSize(7);

        HttpResponse<String> r = sweep(f.token(), plan());

        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        assertThat(json(r).path("batch_cost").path("samples").get(0).path("rows").asLong()).isEqualTo(5);
        assertThat(xmaxOf(f.tenant(), f.live()))
            .as("a row the dry run locked or rewrote (even in a rolled-back transaction) would carry its xid")
            .containsOnly("0");
    }

    @Test
    void theDryRunTakesNoRowLock_soARowAWriterHoldsIsStillMeasured() throws Exception {
        Fx f = tenantFixture("sw-nolock");
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            // A writer holds every legacy row: a SELECT ... FOR UPDATE SKIP LOCKED would see none of them.
            DSL.using(holder, SQLDialect.POSTGRES).select(CHUNKS.CHASH).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(f.tenant())).and(CHUNKS.COLLECTION.eq(f.live())).forUpdate().fetch();

            JsonNode body = json(sweep(f.token(), plan()));

            assertThat(body.path("batch_cost").path("samples").get(0).path("rows").asLong())
                .as("a plain SELECT is not blocked or thinned by row locks").isEqualTo(5);
            holder.rollback();
        }
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
                (List<String>) plan().get("allowed_keys"), List.of(TAXONOMY_PREFIX, "quarantine-"),
                (List<String>) plan().get("quarantine_implies"));

            var applied = sweep.apply(mine.tenant(), plan, Map.of(), null, 1000, 5,
                java.time.Duration.ofSeconds(30), null);

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

    // ---- the keyset cursor, and the checks once per run (B5) ----------------------------------------------------

    @SuppressWarnings("unchecked")
    private Map<String, Object> asMap(JsonNode node) {
        return MAPPER.convertValue(node, Map.class);
    }

    private void setMetadata(String tenant, String collection, String hex, String json) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).update(CHUNKS).set(CHUNKS.METADATA, org.jooq.JSONB.jsonb(json))
                .where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION.eq(collection))
                .and(CHUNKS.CHASH.eq(dev.nexus.service.db.Chash.fromHex(hex).toBytes())).execute();
        }
    }

    @Test
    void aBatchContinuesAfterTheLastKeyItTook_andAResumedCallDoesNotRepeatTheChecks() throws Exception {
        String tenant = "sw-keyset";
        String token = tenantToken(tenant);
        String live = conformant(tenant);
        Map<String, Map<String, Object>> five = new LinkedHashMap<>();
        for (int i = 1; i <= 5; i++) five.put("k" + i, legacy(keyed(tenant, live, "k" + i)));
        List<String> order = new ArrayList<>(seed(tenant, live, five));
        java.util.Collections.sort(order);   // (collection, chash) order: the hex of equal-width chashes sorts as the bytes

        JsonNode first = json(sweep(token, applyBody("batch_size", 2, "max_batches", 1)));
        assertThat(first.path("rows_swept").asLong()).isEqualTo(2);
        assertThat(first.path("done").asBoolean()).isFalse();
        assertThat(first.path("remaining_rows").asLong()).isEqualTo(3);
        JsonNode resume = first.path("resume");
        assertThat(resume.path("cursor").path("collection").asText()).isEqualTo(live);
        assertThat(resume.path("cursor").path("chash").asText()).as("the last key the batch took").isEqualTo(order.get(1));

        // An already swept row (before the cursor) gets a retired key back, and a keyless note appears. The first call
        // did the checks; a continuation neither repeats them nor looks behind its cursor until the pass wraps.
        setMetadata(tenant, live, order.get(0), "{\"chunk_text_hash\":\"" + order.get(0) + "\",\"embedding_model\":\""
            + MODEL + "\",\"title\":\"back\"}");
        document(tenant, "note.late", "knowledge", live, null);

        JsonNode second = json(sweep(token, applyBody("batch_size", 2, "max_batches", 1, "resume", asMap(resume))));
        assertThat(second.path("rows_swept").asLong()).isEqualTo(2);
        assertThat(second.path("resume").path("cursor").path("chash").asText()).isEqualTo(order.get(3));
        assertThat(metadataOf(tenant, live, order.get(0))).as("behind the cursor: not read again yet")
            .containsKey("title");
        assertThat(metadataOf(tenant, live, order.get(2))).doesNotContainKey("title");
        assertThat(metadataOf(tenant, live, order.get(3))).doesNotContainKey("title");

        assertThat(sweep(token, applyBody()).statusCode()).as("a call with no resume does the checks, and the new note "
            + "blocks it").isEqualTo(409);

        JsonNode third = json(sweep(token, applyBody("batch_size", 2, "max_batches", 5,
            "resume", asMap(second.path("resume")))));
        assertThat(third.path("rows_swept").asLong()).as("the last row, then the wrapped pass takes the early one")
            .isEqualTo(2);
        assertThat(third.path("done").asBoolean()).isTrue();
        assertThat(third.path("resume").isNull()).isTrue();
        assertThat(retiredRowCount(tenant, live)).isZero();
    }

    @Test
    void aRowAWriterHoldsIsSkippedNotSwept_andTheRunIsNotDoneWhileItStands() throws Exception {
        String tenant = "sw-locked";
        String token = tenantToken(tenant);
        String live = conformant(tenant);
        Map<String, Map<String, Object>> three = new LinkedHashMap<>();
        for (int i = 1; i <= 3; i++) three.put("l" + i, legacy(keyed(tenant, live, "l" + i)));
        List<String> order = new ArrayList<>(seed(tenant, live, three));
        java.util.Collections.sort(order);

        JsonNode second;
        JsonNode first;
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            DSL.using(holder, SQLDialect.POSTGRES).select(CHUNKS.CHASH).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION.eq(live))
                .and(CHUNKS.CHASH.eq(dev.nexus.service.db.Chash.fromHex(order.get(0)).toBytes())).forUpdate().fetch();

            first = json(sweep(token, applyBody()));

            holder.rollback();
        }
        assertThat(first.path("rows_swept").asLong()).isEqualTo(2);
        assertThat(first.path("done").asBoolean()).as("a locked row is left: not done").isFalse();
        assertThat(first.path("locked_skipped_rows").asLong()).isEqualTo(1);
        assertThat(first.path("remaining_rows").asLong()).isEqualTo(1);
        assertThat(metadataOf(tenant, live, order.get(0))).containsKey("title");

        second = json(sweep(token, applyBody("resume", asMap(first.path("resume")))));
        assertThat(second.path("rows_swept").asLong()).isEqualTo(1);
        assertThat(second.path("done").asBoolean()).isTrue();
        assertThat(second.path("locked_skipped_rows").asLong()).isZero();
    }

    @Test
    void aRowWhoseMetadataIsNotAnObjectIsSkippedAndCounted_neverLoopedOn() throws Exception {
        String tenant = "sw-nonobject";
        String token = tenantToken(tenant);
        String live = conformant(tenant);
        List<String> hex = seed(tenant, live, rows("o1", legacy(keyed(tenant, live, "o1")),
            "o2", legacy(keyed(tenant, live, "o2"))));
        setMetadata(tenant, live, hex.get(1), "[1,2]");

        JsonNode dry = json(sweep(token, plan()));
        assertThat(dry.path("preconditions").path("non_object_metadata_rows").asLong()).isEqualTo(1);
        assertThat(dry.path("totals").path("retired_key_rows").asLong()).as("the array row has no key to retire")
            .isEqualTo(1);

        JsonNode applied = json(sweep(token, applyBody()));
        assertThat(applied.path("rows_swept").asLong()).isEqualTo(1);
        assertThat(applied.path("done").asBoolean()).isTrue();
        assertThat(applied.path("batches").asInt()).as("not selected again and again").isLessThanOrEqualTo(2);
        try (Connection su = pg.createConnection("")) {
            var kept = DSL.using(su, SQLDialect.POSTGRES).select(CHUNKS.METADATA).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.CHASH.eq(
                    dev.nexus.service.db.Chash.fromHex(hex.get(1)).toBytes())).fetchOne(CHUNKS.METADATA);
            assertThat(kept.data()).isEqualTo("[1, 2]");
        }
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
        Map<String, Object> accept = accept("homeless_documents", 1, "generated_nulls", 1);

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
     * One tenant built so each precondition counts what it should and not what it should not. Counted: c1 is owned by a
     * legacy manifest row and carries a label key; c2 is owned by a manifest row with no span and carries a line key; c10
     * carries a char-end key and its manifest row has every span but the char end (a partial NULL); c3 records a TTL no
     * owner records; note.1 has no put_at and no collection; the owner document and doc.notags lack derived meta keys
     * (the first lacks all three, the second only tags). c5/c6 break the two equalities, c8 is half stamped, c9
     * carries a row key in a live collection. Counter-examples, counted by nothing: c11 carries a line key and its
     * manifest row holds all four spans; note.2 has a put_at and a collection; doc.full holds every derived key.
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
        r.put("c10", keyed(tenant, live, "c10", "chunk_end_char", 9, "line_start", 1));
        r.put("c11", keyed(tenant, live, "c11", "line_end", 5));
        seed(tenant, live, r);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // c1, c2, c4, c10 and c11 are owned by a document of the collection (its manifest rows are legacy:
            // labels_written false, spans NULL), through the helper every other fixture uses.
            PgContainerHelper.ownChunks(ctx, tenant, live, hexOf(tenant, live, "c1"), hexOf(tenant, live, "c2"),
                hexOf(tenant, live, "c4"), hexOf(tenant, live, "c10"), hexOf(tenant, live, "c11"));
            // A note: content type knowledge, no put_at in its meta, no physical collection.
            ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                    CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.CONTENT_TYPE)
                .values(tenant, "note.1", "a note", "knowledge").execute();
        }
        // The partial-NULL span (c10: every column but the char end) and its counter-example (c11: all four).
        manifestSpans(tenant, live, hexOf(tenant, live, "c10"), 1, 2, 3, null);
        manifestSpans(tenant, live, hexOf(tenant, live, "c11"), 1, 2, 3, 4);
        // Counter-examples for the three document-level counts.
        document(tenant, "note.2", "knowledge", live, "{\"put_at\":\"2026-01-01T00:00:00Z\"}");
        document(tenant, "doc.full", "code", live, "{\"source_format\":\"py\",\"tags\":\"a\",\"category\":\"c\"}");
        document(tenant, "doc.notags", "code", live, "{\"source_format\":\"py\",\"category\":\"c\"}");
        return new Pre(tenant, token, live);
    }

    @Test
    void theDryRunCountsEachPreconditionFromTheRows() throws Exception {
        Pre p = preconditionTenant("sw-pre-count");

        JsonNode body = json(sweep(p.token(), plan()));

        JsonNode pre = body.path("preconditions");
        assertThat(pre.path("legacy_label_rows").asLong()).as("c1: a legacy manifest row whose label is on the row")
            .isEqualTo(1);
        assertThat(pre.path("null_span_rows").asLong())
            .as("c2 (no span in the manifest, one on the row) and c10 (only the char end is NULL); not c11 (all four set)")
            .isEqualTo(2);
        assertThat(pre.path("keyless_ttl_rows").asLong()).as("c3: a TTL only the row records").isEqualTo(1);
        assertThat(pre.path("keyless_notes").asLong()).as("note.1 has no put_at; note.2 has one").isEqualTo(1);
        assertThat(pre.path("homeless_documents").asLong())
            .as("note.1 has no physical collection; every other document has one").isEqualTo(1);
        assertThat(pre.path("generated_nulls").asLong())
            .as("the owner document (no derived key) and doc.notags (no tags); not doc.full").isEqualTo(2);

        JsonNode details = body.path("precondition_details");
        JsonNode byField = details.path("null_span_rows").path("by_field");
        assertThat(byField.path("line_start").asLong()).as("c2").isEqualTo(1);
        assertThat(byField.path("chunk_end_char").asLong()).as("c10").isEqualTo(1);
        assertThat(byField.path("line_end").asLong()).as("c11 holds line_end and its column is set").isZero();
        assertThat(byField.path("chunk_start_char").asLong()).isZero();
        JsonNode gen = details.path("generated_nulls");
        assertThat(gen.path("by_content_type").path("(none)").asLong()).as("the owner document has no type").isEqualTo(1);
        assertThat(gen.path("by_content_type").path("code").asLong()).as("doc.notags").isEqualTo(1);
        assertThat(gen.path("by_content_type").has("knowledge")).as("a note is not an indexed document").isFalse();
        assertThat(gen.path("missing_key").path("source_format").asLong()).isEqualTo(1);
        assertThat(gen.path("missing_key").path("tags").asLong()).isEqualTo(2);
        assertThat(gen.path("missing_key").path("category").asLong()).isEqualTo(1);
        assertThat(pre.path("chunk_text_hash_mismatch").asLong()).isEqualTo(1);
        assertThat(pre.path("embedding_model_mismatch").asLong()).isEqualTo(1);
        assertThat(pre.path("quarantine_implication_violations").asLong()).isEqualTo(1);
        assertThat(pre.path("row_keys_on_live_rows").asLong()).as("c8 and c9").isEqualTo(2);
        assertThat(pre.path("wrong_width_rows").asLong()).isZero();

        List<String> blocking = new ArrayList<>();
        body.path("blocking").forEach(n -> blocking.add(n.asText()));
        assertThat(blocking).containsExactlyInAnyOrderElementsOf(SIX_RESIDUES);
        assertThat(body.path("applicable").asBoolean()).isFalse();
        assertThat(body.path("totals").path("sweepable_rows").asLong())
            .as("c1, c2, c3, c10 and c11 hold a retired key").isEqualTo(5);
    }

    /** What the precondition tenant holds of each acceptable residue: the numbers an operator would name. */
    private static Map<String, Object> allResidues() {
        return accept("legacy_label_rows", 1, "keyless_notes", 1, "keyless_ttl_rows", 1, "homeless_documents", 1,
            "null_span_rows", 2, "generated_nulls", 2);
    }

    @Test
    void applyIsRefusedWhileAResidueIsNotAccepted_andProceedsOnceEachIsNamedWithItsCount() throws Exception {
        Pre p = preconditionTenant("sw-pre-gate");

        HttpResponse<String> refused = sweep(p.token(), applyBody());
        assertThat(refused.statusCode()).as(refused.body()).isEqualTo(409);
        JsonNode body = json(refused);
        assertThat(body.path("reason").asText()).isEqualTo("sweep_preconditions_not_met");
        assertThat(body.path("blocking")).hasSize(6);
        assertThat(body.path("report").path("preconditions").path("keyless_notes").asLong()).isEqualTo(1);
        assertThat(retiredRowCount(p.tenant(), p.live())).as("nothing was written").isEqualTo(5);

        Map<String, Object> five = allResidues();
        five.remove("generated_nulls");
        HttpResponse<String> partial = sweep(p.token(), applyBody("accept", five));
        assertThat(partial.statusCode()).as("five of six accepted is still refused").isEqualTo(409);
        assertThat(json(partial).path("blocking")).hasSize(1);

        // A number below the live count does not accept the residue: the count is a ceiling, not a name.
        Map<String, Object> tooFew = allResidues();
        tooFew.put("null_span_rows", 1);
        HttpResponse<String> ceiling = sweep(p.token(), applyBody("accept", tooFew));
        assertThat(ceiling.statusCode()).as(ceiling.body()).isEqualTo(409);
        JsonNode detail = json(ceiling).path("report").path("blocking_detail");
        assertThat(detail).hasSize(1);
        assertThat(detail.get(0).path("name").asText()).isEqualTo("null_span_rows");
        assertThat(detail.get(0).path("count").asLong()).isEqualTo(2);
        assertThat(detail.get(0).path("accepted").asLong()).isEqualTo(1);
        assertThat(retiredRowCount(p.tenant(), p.live())).as("still nothing written").isEqualTo(5);

        HttpResponse<String> ok = sweep(p.token(), applyBody("accept", allResidues()));
        assertThat(ok.statusCode()).as(ok.body()).isEqualTo(200);
        assertThat(json(ok).path("rows_swept").asLong()).isEqualTo(5);
        assertThat(retiredRowCount(p.tenant(), p.live())).isZero();
    }

    /**
     * nexus-qmrfi: {@code null_span_rows} counts a chunk key by VALUE, as the client's {@code manifest_span} does. The
     * writer's default for "not recorded" is 0, so a key that is present is not a position. z1 holds all four keys as 0
     * (counted by nothing); z2 holds a 0 char start beside a recorded end (a real start of 0: two columns counted);
     * z3 holds values that are not integers (a string, a float, a bool) and a 0 start beside an unrecorded end
     * (counted by nothing); z4 holds one positive line (one column). Filling the recorded columns brings the count to 0
     * while z1 and z3 keep their NULL columns: zero is reachable.
     */
    @Test
    void nullSpanRowsCountsARecordedValue_aZeroOrANonIntegerKeyIsNotAPosition() throws Exception {
        String tenant = "sw-span-value";
        String token = tenantToken(tenant);
        String live = conformant(tenant);
        Map<String, Map<String, Object>> r = new LinkedHashMap<>();
        r.put("z1", keyed(tenant, live, "z1", "line_start", 0, "line_end", 0, "chunk_start_char", 0, "chunk_end_char", 0));
        r.put("z2", keyed(tenant, live, "z2", "line_start", 0, "line_end", 0, "chunk_start_char", 0, "chunk_end_char", 9));
        r.put("z3", keyed(tenant, live, "z3", "line_start", "7", "line_end", true, "chunk_start_char", 0,
            "chunk_end_char", 9.0));
        r.put("z4", keyed(tenant, live, "z4", "line_start", 12, "line_end", 0, "chunk_start_char", 0, "chunk_end_char", 0));
        seed(tenant, live, r);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.ownChunks(DSL.using(su, SQLDialect.POSTGRES), tenant, live, hexOf(tenant, live, "z1"),
                hexOf(tenant, live, "z2"), hexOf(tenant, live, "z3"), hexOf(tenant, live, "z4"));
        }

        JsonNode pre = json(sweep(token, plan()));
        JsonNode count = pre.path("preconditions").path("null_span_rows");
        assertThat(count.asLong()).as("z2 and z4 record a position; z1 and z3 record none").isEqualTo(2);
        JsonNode byField = pre.path("precondition_details").path("null_span_rows").path("by_field");
        assertThat(byField.path("line_start").asLong()).as("z4 only: z3's \"7\" is a string").isEqualTo(1);
        assertThat(byField.path("line_end").asLong()).as("a 0 and a bool are not lines").isZero();
        assertThat(byField.path("chunk_start_char").asLong()).as("z2: 0 beside a recorded end; not z1, z3, z4").isEqualTo(1);
        assertThat(byField.path("chunk_end_char").asLong()).as("z2; z3's 9.0 is a float").isEqualTo(1);

        // The columns the client's fill rung writes: only what a chunk records. The rest stay NULL and stay uncounted.
        manifestSpans(tenant, live, hexOf(tenant, live, "z2"), null, null, 0, 9);
        manifestSpans(tenant, live, hexOf(tenant, live, "z4"), 12, null, null, null);
        JsonNode after = json(sweep(token, plan()));
        assertThat(after.path("preconditions").path("null_span_rows").asLong())
            .as("only unrecorded keys remain: the count reaches zero").isZero();
        assertThat(after.path("blocking").toString()).doesNotContain("null_span_rows");
    }

    /**
     * The span value rule held to ONE table (nexus-qmrfi, review of c58372716): {@code manifest-span-cases.json}, which
     * {@code tests/catalog/test_manifest_span_cases.py} holds the client's {@code manifest_span} to. One owned chunk per
     * case, its manifest row NULL in all four span columns; {@code null_span_rows} must count the cases that record
     * anything, and {@code by_field} each column the table records. A case whose chunk holds a value the sweep drops
     * unrecorded (a string, a float, a 0) is counted by nothing: the sweep may delete those keys, because they are not
     * spans {@code manifest_span} would write.
     */
    @Test
    void nullSpanRowsFollowsTheSharedSpanCaseTable() throws Exception {
        JsonNode table;
        try (java.io.InputStream in = ChunkKeySweepIntegrationTest.class.getResourceAsStream("/manifest-span-cases.json")) {
            assertThat(in).as("resource manifest-span-cases.json").isNotNull();
            table = MAPPER.readTree(in).path("cases");
        }
        assertThat(table.size()).as("the table is read whole").isGreaterThanOrEqualTo(20);
        List<String> spanKeys = List.of("line_start", "line_end", "chunk_start_char", "chunk_end_char");
        String tenant = "sw-span-table";
        String token = tenantToken(tenant);
        String live = conformant(tenant);
        Map<String, Map<String, Object>> rows = new LinkedHashMap<>();
        Map<String, Long> wantByField = new LinkedHashMap<>();
        for (String key : spanKeys) {
            wantByField.put(key, 0L);
        }
        long wantRows = 0;
        for (JsonNode c : table) {
            String id = c.path("id").asText();
            Map<String, Object> meta = keyed(tenant, live, id);
            meta.putAll(MAPPER.convertValue(c.path("chunk"), new com.fasterxml.jackson.core.type.TypeReference<Map<String, Object>>() { }));
            rows.put(id, meta);
            boolean any = false;
            for (String key : spanKeys) {
                if (!c.path("recorded").path(key).isNull()) {
                    wantByField.merge(key, 1L, Long::sum);
                    any = true;
                }
            }
            if (any) {
                wantRows++;
            }
        }
        seed(tenant, live, rows);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.ownChunks(DSL.using(su, SQLDialect.POSTGRES), tenant, live,
                rows.keySet().stream().map(id -> hexOf(tenant, live, id)).toArray(String[]::new));
        }

        JsonNode pre = json(sweep(token, plan()));

        assertThat(pre.path("preconditions").path("null_span_rows").asLong())
            .as("one row per case that records any column").isEqualTo(wantRows);
        JsonNode byField = pre.path("precondition_details").path("null_span_rows").path("by_field");
        for (var want : wantByField.entrySet()) {
            assertThat(byField.path(want.getKey()).asLong()).as("by_field %s", want.getKey()).isEqualTo(want.getValue());
        }
        assertThat(wantRows).as("the table has cases that record and cases that do not").isBetween(1L, (long) table.size() - 1);
    }

    @Test
    void aCountThatGrowsBetweenTheDryRunAndTheApplyIsRefused_theAcceptedNumberIsAnUpperBound() throws Exception {
        Pre p = preconditionTenant("sw-pre-grow");
        assertThat(sweep(p.token(), plan("accept", allResidues())).statusCode()).isEqualTo(200);

        document(p.tenant(), "note.3", "knowledge", p.live(), null);   // a second keyless note appears

        HttpResponse<String> r = sweep(p.token(), applyBody("accept", allResidues()));
        assertThat(r.statusCode()).as(r.body()).isEqualTo(409);
        assertThat(json(r).path("blocking")).extracting(JsonNode::asText).containsExactly("keyless_notes");
        assertThat(retiredRowCount(p.tenant(), p.live())).isEqualTo(5);
    }

    @Test
    void anAcceptThatIsNotANameToCountObject_orNamesAWrongWidthResidue_orHasAbadNumber_isABadRequest() throws Exception {
        Fx f = tenantFixture("sw-accept");
        HttpResponse<String> wrong = sweep(f.token(), applyBody("accept", accept("wrong_width_rows", 1)));
        assertThat(wrong.statusCode()).isEqualTo(400);
        assertThat(wrong.body()).contains("wrong_width_rows");
        HttpResponse<String> list = sweep(f.token(), applyBody("accept", List.of("keyless_notes")));
        assertThat(list.statusCode()).as("the old name-only list is not accepted").isEqualTo(400);
        assertThat(list.body()).contains("accept");
        assertThat(sweep(f.token(), applyBody("accept", accept("keyless_notes", -1))).statusCode()).isEqualTo(400);
        assertThat(sweep(f.token(), applyBody("accept", accept("keyless_notes", "all"))).statusCode()).isEqualTo(400);
        assertThat(sweep(f.token(), applyBody("accept", accept("keyless_notes", 1.5))).statusCode()).isEqualTo(400);
    }

    // ---- a census that did not finish blocks the collection (B9) -----------------------------------------------

    @Test
    void aCensusThatDidNotFinishBlocksItsCollection_andApplyLeavesItAlone() throws Exception {
        String tenant = "sw-census-unread";
        String live = conformant(tenant);
        List<String> hex = seed(tenant, live, rows("r1", legacy(keyed(tenant, live, "r1"))));
        try (var superDs = PgContainerHelper.superuserDataSource(pg)) {
            var scope = new dev.nexus.service.db.TenantScope(superDs);
            var unread = new dev.nexus.service.vectors.ChunkKeySweep(scope, (t, c, bound) -> {
                throw new IllegalStateException("canceling statement due to statement timeout");
            });
            @SuppressWarnings("unchecked")
            var plan = dev.nexus.service.vectors.ChunkKeyPlan.of((List<String>) plan().get("allowed_keys"),
                List.of(TAXONOMY_PREFIX, "quarantine-"), (List<String>) plan().get("quarantine_implies"));
            var report = unread.dryRun(tenant, plan, Map.of(), 1000, false);
            assertThat(report.collections()).hasSize(1);
            assertThat(report.collections().get(0).blockedReason()).isEqualTo("census_unread");
            assertThat(report.collections().get(0).legacyUnmanifested()).as("unknown is not zero").isNull();
            assertThat(report.sweepableRows()).isZero();

            var applied = unread.apply(tenant, plan, Map.of(), null, 1000, 5, java.time.Duration.ofSeconds(30), null);
            assertThat(applied.rowsSwept()).isZero();
            assertThat(applied.blocked()).extracting(dev.nexus.service.vectors.ChunkKeySweep.Blocked::reason)
                .containsExactly("census_unread");
        }
        assertThat(metadataOf(tenant, live, hex.get(0))).as("the unread collection was not swept").containsKey("title");
    }

    // ---- the resume token is not trusted (round 2, F2) ----------------------------------------------------------

    private static dev.nexus.service.vectors.ChunkKeyPlan enginePlan() {
        @SuppressWarnings("unchecked")
        var plan = dev.nexus.service.vectors.ChunkKeyPlan.of((List<String>) plan().get("allowed_keys"),
            List.of(TAXONOMY_PREFIX, "quarantine-"), (List<String>) plan().get("quarantine_implies"));
        return plan;
    }

    @Test
    void aForgedResumeNamingTaxonomyQuarantineAndCensusBlockedCollectionsSweepsNoneOfThem() throws Exception {
        String tenant = "sw-forged";
        String token = tenantToken(tenant);
        String live = conformant(tenant + "-live");
        String blocked = conformant(tenant + "-blocked");
        String quarantine = "quarantine-" + live;
        String taxonomy = TAXONOMY_PREFIX + tenant;
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCatalogDocument(DSL.using(su, SQLDialect.POSTGRES), tenant, "census.f");
        }
        List<String> liveRows = seed(tenant, live, rows("l1", legacy(keyed(tenant, live, "l1"))));
        List<String> blockedRows = seed(tenant, blocked, rows(
            "b1", legacy(keyed(tenant, blocked, "b1", "catalog_doc_id", "census.f"))));
        List<String> quarantineRows = seed(tenant, quarantine, rows(
            "q1", legacy(keyed(tenant, quarantine, "q1", "quarantined_at", "2026-01-02T00:00:00Z",
                "origin_collection", live))));
        List<String> taxonomyRows = seed(tenant, taxonomy, rows("t1", legacy(Map.of("anything", "goes"))));
        Map<String, Object> forged = new LinkedHashMap<>();
        forged.put("cursor", null);
        forged.put("collections", List.of(taxonomy, quarantine, blocked));
        forged.put("blocked", List.of());
        forged.put("remaining", 99);

        HttpResponse<String> r = sweep(token, applyBody("resume", forged,
            "accept", accept("homeless_documents", 1, "generated_nulls", 1)));

        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        JsonNode body = json(r);
        assertThat(body.path("resume_ignored").asBoolean()).as("an unsigned token is not a continuation").isTrue();
        assertThat(metadataOf(tenant, taxonomy, taxonomyRows.get(0))).containsKey("title");
        assertThat(metadataOf(tenant, quarantine, quarantineRows.get(0))).containsKey("title");
        assertThat(metadataOf(tenant, blocked, blockedRows.get(0))).as("the census-blocked collection is still "
            + "blocked: the call checked it itself").containsKeys("title", "catalog_doc_id");
        assertThat(metadataOf(tenant, live, liveRows.get(0))).as("the call did its own first-call work")
            .doesNotContainKey("title");
        assertThat(body.path("blocked_collections").get(0).path("collection").asText()).isEqualTo(blocked);
    }

    @Test
    void aSignedResumeWhoseCollectionsWereEditedIsIgnored_aGenuineOneIsTaken() throws Exception {
        String tenant = "sw-tamper";
        String token = tenantToken(tenant);
        String live = conformant(tenant);
        String taxonomy = TAXONOMY_PREFIX + tenant;
        Map<String, Map<String, Object>> rows = new LinkedHashMap<>();
        for (int i = 1; i <= 4; i++) rows.put("k" + i, legacy(keyed(tenant, live, "k" + i)));
        seed(tenant, live, rows);
        List<String> taxonomyRows = seed(tenant, taxonomy, rows("t1", legacy(Map.of("anything", "goes"))));

        JsonNode first = json(sweep(token, applyBody("batch_size", 1, "max_batches", 1)));
        assertThat(first.path("resume_ignored").asBoolean()).isFalse();
        assertThat(first.path("resume").path("sig").asText()).as("the engine signs what it hands out").isNotBlank();
        JsonNode second = json(sweep(token, applyBody("batch_size", 1, "max_batches", 1,
            "resume", asMap(first.path("resume")))));
        assertThat(second.path("resume_ignored").asBoolean()).as("an untouched token is a continuation").isFalse();
        assertThat(second.path("done").asBoolean()).isFalse();

        Map<String, Object> edited = asMap(second.path("resume"));
        edited.put("collections", List.of(live, taxonomy));
        JsonNode third = json(sweep(token, applyBody("resume", edited)));

        assertThat(third.path("resume_ignored").asBoolean()).as("an edited token fails its signature").isTrue();
        assertThat(metadataOf(tenant, taxonomy, taxonomyRows.get(0))).containsKey("title");
    }

    @Test
    void aForgedResumeWithAStringOver65535BytesIsIgnored_notA500() throws Exception {
        // nexus-gy3vq: writeUTF refused strings over 65535 bytes, so such a token made signing throw.
        String tenant = "sw-huge";
        String token = tenantToken(tenant);
        String huge = "x".repeat(70_000);
        Map<String, Object> cursor = new LinkedHashMap<>();
        cursor.put("collection", huge);
        cursor.put("chash", "0".repeat(64));
        List<Object> collections = List.of(huge);
        Map<String, Object> forged = new LinkedHashMap<>();
        forged.put("cursor", cursor);
        forged.put("collections", collections);
        forged.put("blocked", List.of());
        forged.put("remaining", 1);
        forged.put("sig", "00");
        HttpResponse<String> r = sweep(token, applyBody("resume", forged));
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        assertThat(json(r).path("resume_ignored").asBoolean()).isTrue();
    }

    @Test
    void aTokenNamingAStringOver65535BytesCanBeSignedAndVerifies() throws Exception {
        // nexus-gy3vq: the signed encoding frames each string by its UTF-8 length, with no 64 KiB ceiling.
        String tenant = "sw-huge-signed";
        String huge = "y".repeat(70_000);
        try (var superDs = PgContainerHelper.superuserDataSource(pg)) {
            var sweep = new dev.nexus.service.vectors.ChunkKeySweep(new dev.nexus.service.db.TenantScope(superDs),
                (t, c, bound) -> 0L);
            var resume = sweep.issueResume(tenant, new dev.nexus.service.vectors.ChunkKeySweep.Cursor(huge, "0".repeat(64)),
                List.of(huge), List.of(), 0);
            var applied = sweep.apply(tenant, enginePlan(), Map.of(), null, 1000, 5, java.time.Duration.ofSeconds(30),
                resume);
            assertThat(applied.resumeIgnored()).as("a token the instance signed is taken").isFalse();
        }
    }

    @Test
    void aSignedResumeFromAnotherTenantIsIgnored() throws Exception {
        String tenantA = "sw-xa";
        String tenantB = "sw-xb";
        String tokenA = tenantToken(tenantA);
        String tokenB = tenantToken(tenantB);
        for (String t : List.of(tenantA, tenantB)) {
            Map<String, Map<String, Object>> three = new LinkedHashMap<>();
            for (int i = 1; i <= 3; i++) three.put("k" + i, legacy(keyed(t, conformant(t), "k" + i)));
            seed(t, conformant(t), three);
        }
        JsonNode firstA = json(sweep(tokenA, applyBody("batch_size", 1, "max_batches", 1)));

        JsonNode asB = json(sweep(tokenB, applyBody("batch_size", 1, "max_batches", 1,
            "resume", asMap(firstA.path("resume")))));

        assertThat(asB.path("resume_ignored").asBoolean()).as("the signature binds the tenant").isTrue();
    }

    /** What a signed token that names bypass and blocked collections produced, and where the rows are. */
    private record Belt(dev.nexus.service.vectors.ChunkKeySweep.Applied applied, String tenant, String live,
                        String held, String quarantine, String taxonomy, String liveRow, String heldRow,
                        String quarantineRow, String taxonomyRow) {}

    /**
     * A token the engine itself signed (so the signature holds) naming collections the first call would never have
     * listed: the batch's own SQL and the token's blocked list must still keep them out.
     */
    private Belt signedTokenNamingBypassAndBlocked(String tenant) throws Exception {
        String live = conformant(tenant + "-live");
        String held = conformant(tenant + "-held");
        String quarantine = "quarantine-" + live;
        String taxonomy = TAXONOMY_PREFIX + tenant;
        List<String> liveRows = seed(tenant, live, rows("l1", legacy(keyed(tenant, live, "l1"))));
        List<String> heldRows = seed(tenant, held, rows("h1", legacy(keyed(tenant, held, "h1"))));
        List<String> quarantineRows = seed(tenant, quarantine, rows(
            "q1", legacy(keyed(tenant, quarantine, "q1", "quarantined_at", "2026-01-02T00:00:00Z",
                "origin_collection", live))));
        List<String> taxonomyRows = seed(tenant, taxonomy, rows("t1", legacy(Map.of("anything", "goes"))));
        try (var superDs = PgContainerHelper.superuserDataSource(pg)) {
            var sweep = new dev.nexus.service.vectors.ChunkKeySweep(new dev.nexus.service.db.TenantScope(superDs),
                (t, c, bound) -> 0L);
            var resume = sweep.issueResume(tenant, null, List.of(taxonomy, quarantine, live, held),
                List.of(new dev.nexus.service.vectors.ChunkKeySweep.Blocked(held, "legacy_unmanifested", 1)), 4);
            var applied = sweep.apply(tenant, enginePlan(), Map.of(), null, 1000, 5, java.time.Duration.ofSeconds(30),
                resume);
            assertThat(applied.resumeIgnored()).as("a token the instance signed is taken").isFalse();
            return new Belt(applied, tenant, live, held, quarantine, taxonomy, liveRows.get(0), heldRows.get(0),
                quarantineRows.get(0), taxonomyRows.get(0));
        }
    }

    @Test
    void aSignedResumeNamingBypassCollectionsStillLeavesThemAlone_theBatchExcludesThemInSql() throws Exception {
        Belt b = signedTokenNamingBypassAndBlocked("sw-belt-bypass");

        assertThat(metadataOf(b.tenant(), b.taxonomy(), b.taxonomyRow())).containsKey("title");
        assertThat(metadataOf(b.tenant(), b.quarantine(), b.quarantineRow())).containsKey("title");
        assertThat(metadataOf(b.tenant(), b.live(), b.liveRow())).as("and the live collection is swept")
            .doesNotContainKey("title");
    }

    @Test
    void aSignedResumeNamingBypassCollectionsIsStillDone_theFinalCountExcludesThemToo() throws Exception {
        Belt b = signedTokenNamingBypassAndBlocked("sw-belt-done");

        assertThat(b.applied().rowsSwept()).isEqualTo(1);
        assertThat(b.applied().remainingRows()).as("the bypass rows are not counted as left to do").isZero();
        assertThat(b.applied().done()).isTrue();
    }

    @Test
    void aSignedResumeThatListsACollectionAsBlockedDoesNotSweepIt() throws Exception {
        Belt b = signedTokenNamingBypassAndBlocked("sw-belt-held");

        assertThat(metadataOf(b.tenant(), b.held(), b.heldRow())).as("named blocked in the token itself")
            .containsKey("title");
        assertThat(b.applied().blocked()).extracting(dev.nexus.service.vectors.ChunkKeySweep.Blocked::collection)
            .containsExactly(b.held());
    }

    @Test
    void theApplyBudgetStartsAfterTheChecks_aSlowCensusDoesNotSpendIt() throws Exception {
        String tenant = "sw-budget";
        String live = conformant(tenant);
        Map<String, Map<String, Object>> three = new LinkedHashMap<>();
        for (int i = 1; i <= 3; i++) three.put("k" + i, legacy(keyed(tenant, live, "k" + i)));
        seed(tenant, live, three);
        try (var superDs = PgContainerHelper.superuserDataSource(pg)) {
            var slow = new dev.nexus.service.vectors.ChunkKeySweep(new dev.nexus.service.db.TenantScope(superDs),
                (t, c, bound) -> {
                    try {
                        Thread.sleep(1_500);
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                    }
                    return 0L;
                });

            var applied = slow.apply(tenant, enginePlan(), Map.of(), null, 1000, 5,
                java.time.Duration.ofMillis(500), null);

            assertThat(applied.totalMs()).as("fixture: the checks alone outlast the budget").isGreaterThan(1_400);
            assertThat(applied.batches()).as("a batch still ran: the budget began after the census")
                .isGreaterThanOrEqualTo(1);
            assertThat(applied.rowsSwept()).isEqualTo(3);
        }
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
