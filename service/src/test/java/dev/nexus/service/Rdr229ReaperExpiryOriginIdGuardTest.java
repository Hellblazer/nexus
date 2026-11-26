// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.db.Chash;
import dev.nexus.service.db.CollectionIds;
import dev.nexus.service.vectors.ReaperRepository;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.sql.Connection;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.temporal.ChronoUnit;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.function.Function;

/**
 * RDR-229 P3.7 (nexus-p2m1x.28, vectors-086): {@code nexus.reaper_expire_quarantine} and {@code nexus.reaper_expire_client_quarantine} guard a quarantine
 * row by its own {@code origin_collection_id}, and from the gate 3 latch leave a row with no such key alone. Rows are still selected by the label tag.
 * Every case runs both functions ({@code engine} true: the reaper-moved population and {@code reaper_expire_quarantine}; false: the client-moved population and
 * {@code reaper_expire_client_quarantine}), on a real database, with {@link ReaperRepository} calling the function as the reaper does.
 *
 * <ul>
 *   <li>The JSON null marker, latched: the key is present, so the latch term keeps the row in scope, and it expires (an engine marker row under any label, a
 *       client marker row under an unregistered label). A latch term read with {@code ->>} would turn a JSON null into SQL NULL and leak these rows.</li>
 *   <li>A key that is not an integer-valued number (5.5, "abc", a string holding another collection's id) must not raise and must not name any collection:
 *       each row is judged by the label guard alone. A bigint cast of the key text would raise on 5.5.</li>
 *   <li>A row with no key, latched: never expired, referenced or not, and not counted as protected. Before the latch it expires as before.</li>
 *   <li>A key naming another collection protects the row when that collection holds the chash and expires it when it does not; origin label A is registered
 *       under a new id for both functions.</li>
 * </ul>
 */
class Rdr229ReaperExpiryOriginIdGuardTest extends AtomicWriteTestBase {

    private static final Clock CLOCK = Clock.fixed(Instant.parse("2026-10-01T12:00:00Z"), ZoneOffset.UTC);
    private static final String MODEL = "minilm-l6-v2-384";
    private static final int ROW_LIMIT = 5_000;
    private static final int STATEMENT_MS = 25_000;
    private static final int LOCK_MS = 2_000;
    private static final org.jooq.Table<?> LATCH = DSL.table(DSL.name("nexus", "rdr229_gate_latch"));
    private static final org.jooq.Field<String> LATCH_GATE = DSL.field(DSL.name("nexus", "rdr229_gate_latch", "gate"), String.class);

    private <T> T su(Function<DSLContext, T> work) throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        }
    }

    private void whileGate3Latched(boolean latched, ThrowingRunnable work) throws Exception {
        if (!latched) {
            work.run();
            return;
        }
        su(ctx -> ctx.insertInto(LATCH).columns(LATCH_GATE, DSL.field(DSL.name("latched_at"), OffsetDateTime.class), DSL.field(DSL.name("engine_version"), String.class))
            .values("gate3", OffsetDateTime.now(), "p37-guard-test").onConflictDoNothing().execute());
        try {
            work.run();
        } finally {
            su(ctx -> ctx.deleteFrom(LATCH).where(LATCH_GATE.eq("gate3")).execute());
        }
    }

    @FunctionalInterface
    private interface ThrowingRunnable {
        void run() throws Exception;
    }

    private static String label(String tag, int n) {
        return "knowledge__p37gd" + tag + n + "__" + MODEL + "__v1";
    }

    private static String stamp(int ageDays) {
        return CLOCK.instant().minus(Duration.ofDays(ageDays)).truncatedTo(ChronoUnit.SECONDS).toString();
    }

    /** A tenant with the sibling and the named live collections registered. */
    private record World(String tenant, String sibling, boolean engine) { }

    private World world(String tag, boolean engine, String... liveLabels) throws Exception {
        int n = seq.incrementAndGet();
        String t = "p37gd-" + tag + n;
        String sibling = "quarantine-" + label("sib", n);
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, t, sibling);
            for (String l : liveLabels) {
                PgContainerHelper.insertCollection(ctx, t, l);
            }
            return null;
        });
        return new World(t, sibling, engine);
    }

    private long idOf(World w, String name) throws Exception {
        return su(ctx -> CollectionIds.require(ctx, w.tenant(), name));
    }

    /** A quarantine row of the sibling, 400 days old, tagged for {@code origin}; {@code key} is the id key's value, written only when {@code keyPresent}. */
    private String row(World w, String origin, String seed, Object key, boolean keyPresent) throws Exception {
        String hex = Chash.ofText(w.sibling() + "/" + seed).toHex();
        Map<String, Object> meta = new LinkedHashMap<>();
        meta.put("quarantined_at", stamp(400));
        meta.put("origin_collection", origin);
        if (keyPresent) {
            meta.put("origin_collection_id", key);   // a Long, a Double, a String, or null: the JSON null marker
        }
        if (w.engine()) {
            meta.put("quarantined_by", "engine-reaper");
            meta.put("reaper_quarantined_at", stamp(400));
        }
        su(ctx -> {
            PgContainerHelper.insertChunks(ctx, w.tenant(), w.sibling(), List.of(hex), List.of(seed), List.of(new float[384]), List.of(meta));
            return null;
        });
        return hex;
    }

    /** A live manifest row of {@code collection} naming {@code hex}, with the chunk it names, as a document of that collection holds it. */
    private void manifest(World w, String collection, String hex) throws Exception {
        long id = idOf(w, collection);
        su(ctx -> {
            // the manifest row's foreign key wants the chunk under the manifest's own collection (a live chunk of the collection that holds it)
            PgContainerHelper.insertChunks(ctx, w.tenant(), collection, List.of(hex), List.of("held"), List.of(new float[384]), List.of(Map.of()));
            String doc = w.tenant() + ".doc." + hex.substring(0, 12);
            ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.TITLE,
                    CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID)
                .values(w.tenant(), doc, "holds " + hex.substring(0, 12), collection, id).execute();
            ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                    CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.COLLECTION,
                    CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID)
                .values(w.tenant(), doc, 0, Chash.fromHex(hex).toBytes(), collection, MODEL, id).execute();
            return null;
        });
    }

    private boolean held(World w, String hex) throws Exception {
        return su(ctx -> ctx.fetchExists(CHUNKS, CHUNKS.TENANT_ID.eq(w.tenant()).and(CHUNKS.CHASH.eq(Chash.fromHex(hex).toBytes()))
            .and(CHUNKS.COLLECTION_ID.eq(CollectionIds.require(ctx, w.tenant(), w.sibling())))));
    }

    /** One call of the function under test, as the reaper makes it: the 14-day cutoff, the origin label as listed. */
    private ReaperRepository.Expiry call(World w, String origin) {
        var reaper = new ReaperRepository(tenantScope);
        String cutoff = stamp(14);
        return w.engine()
            ? reaper.expire(w.tenant(), w.sibling(), origin, cutoff, ROW_LIMIT, STATEMENT_MS, LOCK_MS)
            : reaper.expireClientMoved(w.tenant(), w.sibling(), origin, cutoff, ROW_LIMIT, STATEMENT_MS, LOCK_MS);
    }

    // ---- the JSON null marker, latched --------------------------------------------------------------------------------------------

    /**
     * Latched, a marker row (the key is the JSON null) of a label no registry row holds is in scope and expires, for the engine-moved population (any
     * label) and the client-moved one (an unregistered label, the 083-7 marker rule).
     */
    @ParameterizedTest(name = "engine={0}")
    @ValueSource(booleans = {true, false})
    void latched_aMarkerRowOfAnUnregisteredLabel_expires(boolean engine) throws Exception {
        World w = world("m" + engine, engine);
        String dead = label("dead", seq.get());
        String marker = row(w, dead, "marker", null, true);
        whileGate3Latched(true, () -> {
            var out = call(w, dead);
            assertThat(out.expired()).as("the marker row, key present, is in scope and expires").isEqualTo(1);
            assertThat(held(w, marker)).isFalse();
        });
    }

    // ---- keys that are not integer-valued numbers ---------------------------------------------------------------------------------

    /**
     * Rows keyed 5.5, "abc" and a string holding another collection's id, each with its chash held by collection B (never by the origin A): none of them names a
     * collection by id, so the label guard alone judges them, A holds none of the three, and all three expire without an error. A row keyed 5.5 whose chash A
     * holds is kept by the label guard, and counted.
     */
    @ParameterizedTest(name = "engine={0}")
    @ValueSource(booleans = {true, false})
    void aKeyThatIsNotAnIntegerNumber_isJudgedByTheLabelGuardAlone_andNeverRaises(boolean engine) throws Exception {
        String a = label("a", seq.incrementAndGet());
        String b = label("b", seq.get());
        World w = world("k" + engine, engine, a, b);
        long bId = idOf(w, b);
        String fractional = row(w, a, "fractional", 5.5, true);
        String text = row(w, a, "text", "abc", true);
        String idAsText = row(w, a, "id-as-text", String.valueOf(bId), true);
        String fractionalHeldByA = row(w, a, "fractional-held-by-a", 5.5, true);
        for (String hex : List.of(fractional, text, idAsText)) {
            manifest(w, b, hex);
        }
        manifest(w, a, fractionalHeldByA);
        whileGate3Latched(true, () -> {
            var out = call(w, a);
            assertThat(out.expired()).as("the three rows A does not hold").isEqualTo(3);
            assertThat(out.protectedCount()).as("the row A holds, by the label guard").isEqualTo(1);
            assertThat(held(w, fractional)).isFalse();
            assertThat(held(w, text)).isFalse();
            assertThat(held(w, idAsText)).as("a string is not a number, so it names no collection even when it spells one's id").isFalse();
            assertThat(held(w, fractionalHeldByA)).isTrue();
        });
    }

    // ---- a row with no key ---------------------------------------------------------------------------------------------------------

    /**
     * Latched, a row with no origin_collection_id key is outside the call: an unreferenced one is kept (it leaks), a referenced one is kept, and neither is
     * counted as protected. Beside them a keyed unreferenced row expires and a keyed referenced row is counted: the call is not vacuous.
     */
    @ParameterizedTest(name = "engine={0}")
    @ValueSource(booleans = {true, false})
    void latched_aRowWithNoKey_isNeverExpired_andIsNotCountedAsProtected(boolean engine) throws Exception {
        String a = label("a", seq.incrementAndGet());
        World w = world("n" + engine, engine, a);
        long aId = idOf(w, a);
        String keylessUnreferenced = row(w, a, "keyless-unreferenced", null, false);
        String keylessReferenced = row(w, a, "keyless-referenced", null, false);
        String keyedUnreferenced = row(w, a, "keyed-unreferenced", aId, true);
        String keyedReferenced = row(w, a, "keyed-referenced", aId, true);
        manifest(w, a, keylessReferenced);
        manifest(w, a, keyedReferenced);
        whileGate3Latched(true, () -> {
            var out = call(w, a);
            assertThat(out.expired()).as("only the keyed unreferenced row").isEqualTo(1);
            assertThat(out.protectedCount()).as("only the keyed referenced row: the keyless ones are excluded from the count").isEqualTo(1);
            assertThat(held(w, keyedUnreferenced)).isFalse();
            assertThat(held(w, keyedReferenced)).isTrue();
            assertThat(held(w, keylessUnreferenced)).as("the leak: a keyless unreferenced row is kept after the latch").isTrue();
            assertThat(held(w, keylessReferenced)).isTrue();
        });
    }

    /** Before the latch a keyless row is selected by the label tag as before: an unreferenced one expires, a referenced one is kept and counted. */
    @ParameterizedTest(name = "engine={0}")
    @ValueSource(booleans = {true, false})
    void beforeTheLatch_aRowWithNoKey_expiresAsBefore(boolean engine) throws Exception {
        String a = label("a", seq.incrementAndGet());
        World w = world("p" + engine, engine, a);
        String keylessUnreferenced = row(w, a, "keyless-unreferenced", null, false);
        String keylessReferenced = row(w, a, "keyless-referenced", null, false);
        manifest(w, a, keylessReferenced);
        whileGate3Latched(false, () -> {
            var out = call(w, a);
            assertThat(out.expired()).isEqualTo(1);
            assertThat(out.protectedCount()).isEqualTo(1);
            assertThat(held(w, keylessUnreferenced)).isFalse();
            assertThat(held(w, keylessReferenced)).isTrue();
        });
    }

    // ---- a key that names another collection -----------------------------------------------------------------------------------------

    /**
     * Label A is registered under its own id, and a row tagged A carries the id of another collection B. When B holds the chash the row is kept and counted
     * (B's re-reference protects it, whatever label the call was made with); when B does not, and A does not, it expires. This is the engine function with A
     * re-registered under a new id, and the client function with A live.
     */
    @ParameterizedTest(name = "engine={0}")
    @ValueSource(booleans = {true, false})
    void aKeyNamingAnotherCollection_protectsTheRowWhenThatCollectionHoldsTheChash_andExpiresItWhenNot(boolean engine) throws Exception {
        String a = label("a", seq.incrementAndGet());
        String b = label("b", seq.get());
        World w = world("o" + engine, engine, a, b);
        long bId = idOf(w, b);
        assertThat(bId).as("A is a different collection from B").isNotEqualTo(idOf(w, a));
        String heldByB = row(w, a, "held-by-b", bId, true);
        String heldByNobody = row(w, a, "held-by-nobody", bId, true);
        manifest(w, b, heldByB);
        whileGate3Latched(true, () -> {
            var out = call(w, a);
            assertThat(out.expired()).as("the row no collection holds").isEqualTo(1);
            assertThat(out.protectedCount()).as("the row B holds").isEqualTo(1);
            assertThat(held(w, heldByNobody)).isFalse();
            assertThat(held(w, heldByB)).as("the row's own origin id protects it from a call made with the label A").isTrue();
        });
    }

    /** The guard is not latch-gated: before the latch a keyed row is protected by its own id just the same. */
    @Test
    void beforeTheLatch_aKeyNamingAnotherCollection_stillProtectsTheRow_engineFunction() throws Exception {
        String a = label("a", seq.incrementAndGet());
        String b = label("b", seq.get());
        World w = world("q", true, a, b);
        String heldByB = row(w, a, "held-by-b", idOf(w, b), true);
        manifest(w, b, heldByB);
        whileGate3Latched(false, () -> {
            var out = call(w, a);
            assertThat(out.expired()).isZero();
            assertThat(out.protectedCount()).isEqualTo(1);
            assertThat(held(w, heldByB)).isTrue();
        });
    }
}
