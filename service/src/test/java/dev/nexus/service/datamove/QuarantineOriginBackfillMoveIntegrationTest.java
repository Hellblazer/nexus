// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
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

    private static Map<String, Object> registered;

    @BeforeAll
    void captureRegistration() {
        Record row = ctx().selectFrom(table("nexus", "data_moves")).where(LABEL.eq(LABEL_TEXT)).fetchOne();
        registered = row == null ? null : new LinkedHashMap<>(row.intoMap());
    }

    @AfterEach
    void removeSeeds() {
        for (String t : List.of(T1, T2)) {
            StampMoveSeeds.clean(ctx(), t);
        }
        for (String run : new ArrayList<>(activeRuns(LABEL_TEXT))) {
            ctx().select(DSL.function(DSL.name("dml_utils", "archive_migration_run"), SQLDataType.BIGINT, DSL.val(run))).fetch();
        }
    }

    // ── helpers ─────────────────────────────────────────────────────────────────────────────────────────────

    private void register(Map<String, Object> changes) {
        assertThat(registered).as("the walk registers the move " + LABEL_TEXT).isNotNull();
        Map<String, Object> row = new LinkedHashMap<>(registered);
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
