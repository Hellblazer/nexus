// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.P31CheckReplay;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.QuarantineOrigin;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.Record;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Clock;
import java.time.Duration;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;

/**
 * RDR-229 Phase 3 blocking row 13 (nexus-p2m1x.22): the move {@code rdr229-quarantine-origin-id} that vectors-082-1 registers. A quarantine
 * chunk (its collection's {@code lifecycle_state} is {@code quarantine}) with no {@code origin_collection_id} key gets one: the registry id
 * of the row its {@code origin_collection} label resolves to in {@code labelResolutionOrder}, or JSON null (the no-id marker) where no
 * row carries the label or the row has no label. Nothing else changes: not a chunk outside quarantine, not a row that already has the key,
 * not the label or any other key. A killed run resumes and writes no row twice.
 */
class QuarantineOriginBackfillMoveIntegrationTest extends DataMoveIntegrationBase {

    private static final String LABEL_TEXT = QuarantineOriginBackfill.LABEL;
    private static final String KEY = QuarantineOrigin.ORIGIN_ID_KEY;
    private static final ObjectMapper JSON = new ObjectMapper();

    private static final String T1 = "qob-a";
    private static final String T2 = "qob-b";

    private static final String STAMP_LABEL = StampLabels.labelFor("chunks");

    private static Map<String, Object> registered;
    private static Map<String, Object> stampRegistered;

    @BeforeAll
    void captureRegistration() {
        Record row = ctx().selectFrom(table("nexus", "data_moves")).where(LABEL.eq(LABEL_TEXT)).fetchOne();
        registered = row == null ? null : new LinkedHashMap<>(row.intoMap());
        Record stamp = ctx().selectFrom(table("nexus", "data_moves")).where(LABEL.eq(STAMP_LABEL)).fetchOne();
        stampRegistered = stamp == null ? null : new LinkedHashMap<>(stamp.intoMap());
    }

    @AfterEach
    void removeSeeds() {
        for (String t : List.of(T1, T2)) {
            StampMoveSeeds.clean(ctx(), t);
        }
        for (String label : List.of(LABEL_TEXT, STAMP_LABEL)) {
            for (String run : new ArrayList<>(activeRuns(label))) {
                ctx().select(DSL.function(DSL.name("dml_utils", "archive_migration_run"), SQLDataType.BIGINT, DSL.val(run))).fetch();
            }
        }
    }

    // ── helpers ─────────────────────────────────────────────────────────────────────────────────────────────

    private void register(Map<String, Object> changes) {
        insertMove(registered, LABEL_TEXT, changes);
    }

    private void insertMove(Map<String, Object> captured, String label, Map<String, Object> changes) {
        assertThat(captured).as("the walk registers the move " + label).isNotNull();
        Map<String, Object> row = new LinkedHashMap<>(captured);
        row.putAll(changes);
        List<Field<?>> columns = new ArrayList<>();
        List<Object> values = new ArrayList<>();
        row.forEach((k, v) -> {
            columns.add(DSL.field(DSL.name(k)));
            values.add(v);
        });
        ctx().insertInto(table("nexus", "data_moves")).columns(columns).values(values).execute();
    }

    private DataMoveWorker productionWorker() {
        return worker(installed(), 0, new DataMoveFailureHandler(adminConnection, WORKER_NAME, Clock.systemUTC()), new VerifyingFinisher());
    }

    private static String name(String tenant, String what) {
        return "knowledge__" + tenant.replace("-", "") + "x" + what.toLowerCase() + "__" + StampMoveSeeds.BGE + "__v1";
    }

    private static String quarantine(String tenant, String what) {
        return QuarantineOrigin.PREFIX + name(tenant, what);
    }

    private void live(String tenant, String what) {
        PgContainerHelper.insertCollection(ctx(), tenant, name(tenant, what), StampMoveSeeds.BGE);
    }

    private void quarantineCollection(String tenant, String what) {
        PgContainerHelper.insertCollection(ctx(), tenant, quarantine(tenant, what), StampMoveSeeds.BGE);
        ctx().update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.LIFECYCLE_STATE, "quarantine")
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(quarantine(tenant, what))).execute();
    }

    /** {@code what} retired under {@code successor}: a rename tombstone, its id its own. */
    private void tombstone(String tenant, String what, String successor) {
        PgContainerHelper.insertCollection(ctx(), tenant, name(tenant, what), StampMoveSeeds.BGE);
        ctx().update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, name(tenant, successor))
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, id(tenant, successor))
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(name(tenant, what))).execute();
    }

    private Long id(String tenant, String what) {
        return StampMoveSeeds.id(ctx(), tenant, name(tenant, what));
    }

    private static String chash(String tenant, String key) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(("qob-" + tenant + "-" + key).getBytes(StandardCharsets.UTF_8)));
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    /** One chunk keyed {@code key} in {@code collection} with {@code metadata}. */
    private void chunk(String tenant, String collection, String key, Map<String, Object> metadata) {
        PgContainerHelper.insertChunks(ctx(), tenant, collection, List.of(chash(tenant, key)), List.of("text " + key), List.of(new float[768]),
            List.of(metadata));
    }

    private record Row(JsonNode metadata, String xmin) {
        boolean hasKey() {
            return metadata != null && metadata.has(KEY);
        }

        JsonNode key() {
            return metadata.get(KEY);
        }
    }

    private Row row(String tenant, String key) {
        Field<String> xmin = DSL.field(DSL.name("xmin")).cast(SQLDataType.VARCHAR);
        var r = ctx().select(CHUNKS.METADATA, xmin).from(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.CHASH.eq(HexFormat.of().parseHex(chash(tenant, key)))).fetchOne();
        assertThat(r).as("chunk %s/%s", tenant, key).isNotNull();
        try {
            JSONB m = r.value1();
            return new Row(m == null ? null : JSON.readTree(m.data()), r.value2());
        } catch (java.io.IOException e) {
            throw new IllegalStateException(e);
        }
    }

    private long verificationRows(String tenant) {
        String verification = registered.get("verification").toString();
        long[] n = new long[1];
        ctx().connection(c -> n[0] = MoveSql.countRows(c, verification, tenant, VerifyingFinisher.COUNT_CAP));
        return n[0];
    }

    private long adminTableWideRows() throws Exception {
        String verification = registered.get("verification").toString();
        try (var c = adminPool.getConnection()) {
            return MoveSql.countRows(c, verification, null, VerifyingFinisher.COUNT_CAP);
        }
    }

    private static Map<String, Object> tagged(String origin) {
        return Map.of("origin_collection", origin, "quarantined_by", "reaper");
    }

    // ── the rule ────────────────────────────────────────────────────────────────────────────────────────────

    /**
     * The shapes: a label of a live collection; a dangling label (no registry row); a label naming a rename tombstone whose successor is live
     * (the renamed-away tag: it resolves to the tombstone, the row a by-name read of that label found); a quarantine row with no label; a row
     * the mover already keyed; a label of the other tenant's collection (resolved in the row's own tenant: no row there); and a chunk outside
     * quarantine that carries a label, which is not the move's.
     */
    @Test
    void eachQuarantineChunkGetsTheIdItsLabelResolvesTo_orTheNullMarker_andNothingElseIsWritten() throws Exception {
        live(T1, "live");
        live(T1, "renamed");
        tombstone(T1, "old", "renamed");
        live(T2, "elsewhere");
        quarantineCollection(T1, "q");
        quarantineCollection(T2, "q");
        String q1 = quarantine(T1, "q");
        chunk(T1, q1, "live", tagged(name(T1, "live")));
        chunk(T1, q1, "dangling", tagged(name(T1, "gone")));
        chunk(T1, q1, "renamedAway", tagged(name(T1, "old")));
        chunk(T1, q1, "untagged", Map.of("quarantined_by", "reaper"));
        chunk(T1, q1, "keyed", Map.of("origin_collection", name(T1, "live"), KEY, 4242L));
        chunk(T1, q1, "crossTenant", tagged(name(T2, "elsewhere")));
        chunk(T1, name(T1, "live"), "notQuarantine", tagged(name(T1, "renamed")));
        chunk(T2, quarantine(T2, "q"), "other", tagged(name(T2, "elsewhere")));
        assertThat(ctx().fetchCount(CHUNKS, CHUNKS.TENANT_ID.in(T1, T2).and(CHUNKS.COLLECTION_ID.isNull())))
            .as("non-vacuity: every seeded chunk has its collection_id, as after the chunks stamp").isZero();

        assertThat(verificationRows(T1)).as("the verification sees T1's five unkeyed quarantine rows").isEqualTo(5);
        assertThat(verificationRows(T2)).isEqualTo(1);
        assertThat(adminTableWideRows()).as("and the migrating role's table-wide run sees all six").isEqualTo(6);
        Row keyedBefore = row(T1, "keyed");
        Row outsideBefore = row(T1, "notQuarantine");
        register(Map.of());

        assertThat(productionWorker().runCycle()).isEqualTo(1);

        assertThat(completedAt(LABEL_TEXT)).as("the move completes").isNotNull();
        assertThat(row(T1, "live").key().asLong()).as("a live label resolves to its row's id").isEqualTo(id(T1, "live"));
        assertThat(row(T1, "live").key().isNumber()).as("a JSON number, as the movers write it").isTrue();
        assertThat(row(T1, "dangling").hasKey()).as("a dangling label gets the key").isTrue();
        assertThat(row(T1, "dangling").key().isNull()).as("with JSON null, the no-id marker").isTrue();
        assertThat(row(T1, "renamedAway").key().asLong()).as("a label naming only a rename tombstone takes the tombstone's own id")
            .isEqualTo(id(T1, "old")).isNotEqualTo(id(T1, "renamed"));
        assertThat(row(T1, "untagged").key().isNull()).as("a row with no label gets the marker").isTrue();
        assertThat(row(T1, "crossTenant").key().isNull()).as("a label is resolved in the row's own tenant").isTrue();
        assertThat(row(T2, "other").key().asLong()).isEqualTo(id(T2, "elsewhere"));
        assertThat(row(T1, "keyed")).as("a row the mover keyed is not written").isEqualTo(keyedBefore);
        assertThat(row(T1, "notQuarantine")).as("a chunk outside quarantine is not written").isEqualTo(outsideBefore);
        for (String k : List.of("live", "dangling", "renamedAway", "crossTenant")) {
            assertThat(row(T1, k).metadata().get("origin_collection")).as("%s keeps its label", k).isNotNull();
            assertThat(row(T1, k).metadata().get("quarantined_by").asText()).as("%s keeps every other key", k).isEqualTo("reaper");
        }
        assertThat(verificationRows(T1)).isZero();
        assertThat(verificationRows(T2)).isZero();
        assertThat(adminTableWideRows()).isZero();
    }

    /** A collection by name and lifecycle state: {@code quarantine} or {@code live}, whatever its name says. */
    private void collectionInState(String tenant, String name, String state) {
        PgContainerHelper.insertCollection(ctx(), tenant, name, StampMoveSeeds.BGE);
        ctx().update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.LIFECYCLE_STATE, state)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(name)).execute();
    }

    private Long collectionIdOf(String tenant, String key) {
        return ctx().select(CHUNKS.COLLECTION_ID).from(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.CHASH.eq(HexFormat.of().parseHex(chash(tenant, key)))).fetchOne(CHUNKS.COLLECTION_ID);
    }

    /**
     * The critics' shapes (review and critique of 02f8a135a3): quarantine is the lifecycle state, never the name prefix, so a
     * {@code quarantine-}-named live collection is not the move's and a plainly named quarantine collection is; a row with NULL metadata gets
     * the marker; and a quarantine row the chunks stamp has not reached (collection_id NULL, the move run while the stamp is paused or
     * blocked, which DataMoveWorker.runCycle allows) is found by its name, given its own collection_id by the stamp's rule and the key. The
     * marker is pinned as the SQL predicates read it: the key exists ({@code jsonb_exists}) and its text is NULL.
     */
    @Test
    void quarantineIsTheLifecycleState_aNullMetadataRowGetsTheMarker_andAnUnstampedQuarantineRowIsStampedAndKeyed() throws Exception {
        live(T1, "live");
        String prefixedLive = quarantine(T1, "notreally");
        String plainQuarantine = name(T1, "plainq");
        collectionInState(T1, prefixedLive, "live");
        collectionInState(T1, plainQuarantine, "quarantine");
        quarantineCollection(T1, "q");
        String q1 = quarantine(T1, "q");
        chunk(T1, prefixedLive, "prefixedLive", tagged(name(T1, "live")));
        chunk(T1, plainQuarantine, "plainQuarantine", tagged(name(T1, "live")));
        chunk(T1, q1, "nullMetadata", Map.of());
        ctx().update(CHUNKS).set(CHUNKS.METADATA, (JSONB) null)
            .where(CHUNKS.TENANT_ID.eq(T1)).and(CHUNKS.CHASH.eq(HexFormat.of().parseHex(chash(T1, "nullMetadata")))).execute();
        assertThat(row(T1, "nullMetadata").metadata()).as("non-vacuity: SQL NULL metadata").isNull();
        // as an upgrade leaves a row the chunks stamp has not reached: the name set, the id NULL (the CHECK is lifted to write it, then restored NOT VALID)
        P31CheckReplay.lift(ctx(), StampMoveSeeds.CHECK_CHANGESET.get("chunks"));
        try {
            chunk(T1, q1, "unstamped", tagged(name(T1, "live")));
            ctx().update(CHUNKS).set(CHUNKS.COLLECTION_ID, (Long) null)
                .where(CHUNKS.TENANT_ID.eq(T1)).and(CHUNKS.CHASH.eq(HexFormat.of().parseHex(chash(T1, "unstamped")))).execute();
        } finally {
            P31CheckReplay.restore(ctx(), StampMoveSeeds.CHECK_CHANGESET.get("chunks"));
        }
        assertThat(collectionIdOf(T1, "unstamped")).as("non-vacuity: the row is unstamped").isNull();
        assertThat(verificationRows(T1)).as("plainQuarantine, nullMetadata and unstamped; not the quarantine-named live one").isEqualTo(3);
        Row prefixedBefore = row(T1, "prefixedLive");
        register(Map.of());

        assertThat(productionWorker().runCycle()).isEqualTo(1);

        assertThat(completedAt(LABEL_TEXT)).as("the move completes").isNotNull();
        assertThat(row(T1, "prefixedLive")).as("a quarantine-named live collection is not quarantine").isEqualTo(prefixedBefore);
        assertThat(row(T1, "plainQuarantine").key().asLong()).as("a plainly named quarantine collection is").isEqualTo(id(T1, "live"));
        assertThat(row(T1, "nullMetadata").hasKey()).as("NULL metadata gets the key").isTrue();
        assertThat(row(T1, "nullMetadata").key().isNull()).as("with the marker").isTrue();
        assertThat(row(T1, "unstamped").key().asLong()).as("the unstamped row is keyed").isEqualTo(id(T1, "live"));
        assertThat(collectionIdOf(T1, "unstamped")).as("and stamped, by the chunks stamp's rule")
            .isEqualTo(StampMoveSeeds.id(ctx(), T1, q1));
        Field<Boolean> exists = DSL.function("jsonb_exists", SQLDataType.BOOLEAN, CHUNKS.METADATA, DSL.val(KEY));
        Field<String> text = DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, KEY);
        var marker = ctx().select(exists, text).from(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(T1)).and(CHUNKS.CHASH.eq(HexFormat.of().parseHex(chash(T1, "nullMetadata")))).fetchOne();
        assertThat(marker.value1()).as("the marker as the gate-3 clause reads it: the key exists").isTrue();
        assertThat(marker.value2()).as("and as siblingNames reads it: no id").isNull();
        assertThat(verificationRows(T1)).isZero();
    }

    // ── one rule for collection_id (linda, 2026-11-23) ─────────────────────────────────────────────────────

    /** The SET of the chunks stamp's registered template: {@code collection_id = COALESCE(t.collection_id, <lookup by name>)}. */
    private static String stampFill() {
        assertThat(stampRegistered).as("the walk registers " + STAMP_LABEL).isNotNull();
        String template = stampRegistered.get("template").toString();
        int set = template.indexOf(" SET ");
        // the SET ends at the subselect's closing parens, before the top-level WHERE (the first " WHERE " is the subselect's own)
        int end = template.indexOf(")) WHERE ", set);
        assertThat(set).as("the stamp's template has a SET").isPositive();
        assertThat(end).as("and a top-level WHERE after the fill").isGreaterThan(set);
        return template.substring(set + " SET ".length(), end + 2);
    }

    private void unstamp(String tenant, String key) {
        P31CheckReplay.lift(ctx(), StampMoveSeeds.CHECK_CHANGESET.get("chunks"));
        try {
            ctx().update(CHUNKS).set(CHUNKS.COLLECTION_ID, (Long) null)
                .where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.CHASH.eq(HexFormat.of().parseHex(chash(tenant, key)))).execute();
        } finally {
            P31CheckReplay.restore(ctx(), StampMoveSeeds.CHECK_CHANGESET.get("chunks"));
        }
    }

    /**
     * The backfill writes a row's collection_id only where the chunks stamp has not, and by the stamp's own rule: the two registered templates
     * carry the same fill text (read from the registry rows the walk wrote, so an edit to either is seen here), and on the same unstamped row
     * the stamp and the backfill set the same id. vectors-070-15 is applied on every install and cannot change, so the text is the stamp's,
     * copied once and pinned here.
     */
    @Test
    void theBackfillSetsCollectionIdByTheChunksStampsOwnRule_theSameTextAndTheSameIdOnTheSameRow() throws Exception {
        String fill = stampFill();
        assertThat(fill).as("non-vacuity: the stamp's whole fill, its lookup predicate included")
            .startsWith("collection_id = COALESCE(t.collection_id, (SELECT cc.collection_id")
            .contains("cc.tenant_id = t.tenant_id AND cc.name = t.collection").endsWith("))");
        assertThat(registered.get("template").toString()).as("the backfill's template carries the stamp's fill, word for word").contains(fill);

        live(T1, "live");
        quarantineCollection(T1, "q");
        chunk(T1, quarantine(T1, "q"), "same", tagged(name(T1, "live")));
        unstamp(T1, "same");
        assertThat(collectionIdOf(T1, "same")).as("non-vacuity: unstamped").isNull();

        insertMove(stampRegistered, STAMP_LABEL, java.util.Collections.singletonMap("on_complete", null));   // the fill only: no VALIDATE here
        assertThat(productionWorker().runCycle()).isEqualTo(1);
        assertThat(completedAt(STAMP_LABEL)).as("the stamp ran").isNotNull();
        Long byStamp = collectionIdOf(T1, "same");
        assertThat(byStamp).as("the stamp's id").isNotNull();

        unstamp(T1, "same");
        register(Map.of());
        assertThat(productionWorker().runCycle()).isEqualTo(1);
        assertThat(completedAt(LABEL_TEXT)).isNotNull();
        assertThat(collectionIdOf(T1, "same")).as("the backfill sets the id the stamp set").isEqualTo(byStamp);
    }

    // ── gate 2 ──────────────────────────────────────────────────────────────────────────────────────────────

    /** Through CompletionCheck.evaluate: the label with no row and the label open keep gate 2 false; once the move completes it holds. */
    @Test
    void gate2WaitsForTheBackfill_noRowAndOpenAreIncomplete_completedIsComplete() throws Exception {
        var readSet = new CompletionCheck.ReadSet(List.of(), List.of(LABEL_TEXT));
        java.util.function.Supplier<CompletionCheck.Result> eval = () ->
            CompletionCheck.evaluate(ctx(), readSet, CompletionCheck.Gate3Source.NOT_INSTALLED, CompletionCheck.DIRECT);
        java.util.function.Function<CompletionCheck.Result, CompletionCheck.LabelState> state = r ->
            r.labels().stream().filter(l -> l.label().equals(LABEL_TEXT)).findFirst().orElseThrow();

        CompletionCheck.Result none = eval.get();
        assertThat(none.gate2()).as("no row: gate 2 waits").isFalse();
        assertThat(state.apply(none).status()).isEqualTo(CompletionCheck.Status.INCOMPLETE);
        assertThat(state.apply(none).why()).contains("no row");

        live(T1, "live");
        quarantineCollection(T1, "q");
        chunk(T1, quarantine(T1, "q"), "g", tagged(name(T1, "live")));
        register(Map.of());
        CompletionCheck.Result open = eval.get();
        assertThat(open.gate2()).as("open: gate 2 waits").isFalse();
        assertThat(state.apply(open).why()).isEqualTo("open");

        assertThat(productionWorker().runCycle()).isEqualTo(1);
        CompletionCheck.Result done = eval.get();
        assertThat(state.apply(done).status()).as("completed").isEqualTo(CompletionCheck.Status.COMPLETE);
        assertThat(done.gate2()).as("and gate 2 holds on it").isTrue();
    }

    // ── resumable ───────────────────────────────────────────────────────────────────────────────────────────

    @Test
    void aRunKilledAfterSomeChunksResumesAtTheNextChunk_andDoesNotWriteWhatItAlreadyWrote() throws Exception {
        live(T1, "live");
        quarantineCollection(T1, "q");
        List<String> keys = new ArrayList<>();
        for (int i = 0; i < 8; i++) {
            keys.add("k" + i);
            chunk(T1, quarantine(T1, "q"), "k" + i, tagged(name(T1, "live")));
        }
        register(Map.of("chunk_size", 1));
        int baselineFree = MovePreflight.freeWorkerSlots(ctx());
        var first = worker(installed(), 300);   // the engine that dies: the runs only, as the base's kill test builds it
        var cycle = async(first::runCycle);
        long deadline = System.nanoTime() + Duration.ofMinutes(2).toNanos();
        while (keys.stream().noneMatch(k -> row(T1, k).hasKey()) && System.nanoTime() < deadline) {
            Thread.sleep(20);
        }
        int pid = -1;
        for (int i = 0; i < 100 && pid < 0; i++) {
            pid = callPid();
            Thread.sleep(20);
        }
        ctx().select(DSL.function("pg_terminate_backend", SQLDataType.BOOLEAN, DSL.val(pid))).fetch();
        cycle.get(2, TimeUnit.MINUTES);
        first.close();
        long drain = System.nanoTime() + Duration.ofMinutes(1).toNanos();
        while (MovePreflight.freeWorkerSlots(ctx()) < baselineFree && System.nanoTime() < drain) {
            Thread.sleep(50);
        }
        Map<String, Row> midway = new LinkedHashMap<>();
        keys.forEach(k -> midway.put(k, row(T1, k)));
        long written = midway.values().stream().filter(Row::hasKey).count();
        assertThat(written).as("some chunks committed before the kill, and not all").isBetween(1L, 7L);
        assertThat(completedAt(LABEL_TEXT)).isNull();

        assertThat(productionWorker().runCycle()).isEqualTo(1);

        assertThat(completedAt(LABEL_TEXT)).isNotNull();
        for (String k : keys) {
            assertThat(row(T1, k).key().asLong()).as(k).isEqualTo(id(T1, "live"));
            if (midway.get(k).hasKey()) {
                assertThat(row(T1, k).xmin()).as("%s was written before the kill and is not written again", k).isEqualTo(midway.get(k).xmin());
            }
        }
        assertThat(verificationRows(T1)).isZero();
    }
}
