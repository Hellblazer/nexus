// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.ChunkReaper.Settings;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.CollectionIds;
import dev.nexus.service.db.LadderRepository;
import dev.nexus.service.db.Rdr192BackfillGate;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.ReaperRepository;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

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
 * RDR-229 P3.7 (nexus-p2m1x.27; linda 2026-11-23): quarantine rows whose {@code origin_collection_id} is the JSON null marker (the origin had no registry row
 * when they were quarantined) are neither restorable by id nor, before this change, expirable. Two rules, run on a real database:
 * <ol>
 *   <li>The client-moved path ({@code ReaperRepository#clientMovedOrigins}, {@code nexus.reaper_expire_client_quarantine}) lists an origin that only a marker row
 *       names, registered or not, and for an origin that is not a live registered collection expires its marker rows past retention, by tag and the null
 *       id, never a row that carries an id (a live collection's rows, even under the same label). A registered live origin behaves as before.</li>
 *   <li>From the gate 3 latch {@code nexus.gc_expire_quarantine} and {@code gc_expire_quarantine_bounded} select the rows to expire by the origin's id, so
 *       expiring a re-registered label A does not delete a former A's marker rows in the same sibling. Before the latch the label match is unchanged.</li>
 * </ol>
 * The engine-moved population is pinned: {@code nexus.reaper_expire_quarantine} already expires a marker row by its tag.
 */
class Rdr229P37MarkerExpiryTest extends AtomicWriteTestBase {

    private static final Clock CLOCK = Clock.fixed(Instant.parse("2026-10-01T12:00:00Z"), ZoneOffset.UTC);
    private static final String MODEL = "minilm-l6-v2-384";
    private static final org.jooq.Table<?> LATCH = DSL.table(DSL.name("nexus", "rdr229_gate_latch"));
    private static final org.jooq.Field<String> LATCH_GATE = DSL.field(DSL.name("nexus", "rdr229_gate_latch", "gate"), String.class);

    private <T> T su(Function<DSLContext, T> work) throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        }
    }

    private void whileGate3Latched(Runnable work) throws Exception {
        su(ctx -> ctx.insertInto(LATCH).columns(LATCH_GATE, DSL.field(DSL.name("latched_at"), OffsetDateTime.class), DSL.field(DSL.name("engine_version"), String.class))
            .values("gate3", OffsetDateTime.now(), "p37-test").onConflictDoNothing().execute());
        try {
            work.run();
        } finally {
            su(ctx -> ctx.deleteFrom(LATCH).where(LATCH_GATE.eq("gate3")).execute());
        }
    }

    private static String label(String tag, int n) {
        return "knowledge__p37mx" + tag + n + "__" + MODEL + "__v1";
    }

    private static String stamp(int ageDays) {
        return CLOCK.instant().minus(Duration.ofDays(ageDays)).truncatedTo(ChronoUnit.SECONDS).toString();
    }

    private String cutoff() {
        return stamp(14);
    }

    private void register(String tenant, String... names) throws Exception {
        su(ctx -> {
            for (String n : names) {
                PgContainerHelper.insertCollection(ctx, tenant, n);
            }
            return null;
        });
    }

    /** One row of {@code sibling} tagged for {@code origin}, 400 days old: the id key absent, a number, or the JSON null marker; engine-moved or client-moved. */
    private String row(String tenant, String sibling, String origin, String seed, Object idKey, boolean idKeyPresent, boolean engineMoved) throws Exception {
        String hex = Chash.ofText(sibling + "/" + seed).toHex();
        Map<String, Object> meta = new LinkedHashMap<>();
        meta.put("quarantined_at", stamp(400));
        meta.put("origin_collection", origin);
        if (idKeyPresent) {
            meta.put("origin_collection_id", idKey);   // a Long, or null: the JSON null marker
        }
        if (engineMoved) {
            meta.put("quarantined_by", "engine-reaper");
            meta.put("reaper_quarantined_at", stamp(400));
        }
        su(ctx -> {
            PgContainerHelper.insertChunks(ctx, tenant, sibling, List.of(hex), List.of(seed), List.of(new float[384]), List.of(meta));
            return null;
        });
        return hex;
    }

    private boolean held(String tenant, String sibling, String hex) throws Exception {
        return su(ctx -> ctx.fetchExists(CHUNKS, CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.CHASH.eq(Chash.fromHex(hex).toBytes()))
            .and(CHUNKS.COLLECTION_ID.eq(CollectionIds.require(ctx, tenant, sibling)))));
    }

    private ChunkReaper reaper(String tenant) {
        var ladder = new LadderRepository(tenantScope);
        ladder.record(tenant, Rdr192BackfillGate.RUNG_NAME, "7.99.0", "");
        return new ChunkReaper(new ReaperRepository(tenantScope), new PgVectorRepository(tenantScope, embedder, embedder), repo, new Rdr192BackfillGate(ladder),
            () -> List.of(tenant), Settings.defaults(), CLOCK);
    }

    // ---- (A) the client-moved path ---------------------------------------------------------------------------------------------

    /** A client-moved marker row past retention, its origin label registered nowhere: listed, and expired by the reaper. */
    @Test
    void aClientMovedMarkerRowOfAnUnregisteredLabel_isListedAndReapedPastRetention() throws Exception {
        String t = "p37mx-a1-" + seq.incrementAndGet();
        String dead = label("dead", seq.get());
        String sibling = "quarantine-" + label("sib", seq.get());
        register(t, sibling);
        String marker = row(t, sibling, dead, "marker", null, true, false);

        assertThat(new ReaperRepository(tenantScope).clientMovedOrigins(t, sibling, "knowledge", 25_000))
            .as("an origin only a marker row names is listed, registered or not").containsExactly(dead);
        var result = reaper(t).runOnce(null).tenant(t);

        assertThat(result.clientExpiry(sibling)).as("the sibling was expired").isNotNull();
        assertThat(result.clientExpiry(sibling).expired()).as("the marker row was reaped").isEqualTo(1);
        assertThat(held(t, sibling, marker)).isFalse();
    }

    /**
     * The marker path touches marker rows only. A row of the same tag that carries an id (a live collection renamed away from the label, its client-moved row
     * still tagged with the old one) is not reaped by it, and a row with no id key at all is not either.
     */
    @Test
    void theMarkerPathNeverTouchesARowThatCarriesAnId_oneOfALiveCollectionUnderTheSameLabel() throws Exception {
        String t = "p37mx-a2-" + seq.incrementAndGet();
        String oldLabel = label("old", seq.get());
        String live = label("live", seq.get());
        String sibling = "quarantine-" + label("sib", seq.get());
        register(t, live, sibling);
        long liveId = su(ctx -> CollectionIds.require(ctx, t, live));
        String marker = row(t, sibling, oldLabel, "marker", null, true, false);
        String ofLive = row(t, sibling, oldLabel, "of-live", liveId, true, false);
        String noKey = row(t, sibling, oldLabel, "no-key", null, false, false);

        var result = reaper(t).runOnce(null).tenant(t);

        assertThat(result.clientExpiry(sibling).expired()).as("only the marker row goes").isEqualTo(1);
        assertThat(held(t, sibling, marker)).isFalse();
        assertThat(held(t, sibling, ofLive)).as("the live collection's row, tagged with the label it had, stays").isTrue();
        assertThat(held(t, sibling, noKey)).as("a row with no id key is not a marker row").isTrue();
    }

    /**
     * Label L is registered and live. A client-moved marker row tagged L sits beside L's own client-moved row (L's id), both past retention. One pass reaps
     * both, once each: the lister names L once, so one call of the function takes both rows, and writes one audit row naming both.
     */
    @Test
    void aMarkerRowBesideTheLiveCollectionsOwnRow_bothAreReapedOnce_inOnePass() throws Exception {
        String t = "p37mx-a4-" + seq.incrementAndGet();
        String l = label("l", seq.get());
        String sibling = "quarantine-" + label("sib", seq.get());
        register(t, l, sibling);
        long lId = su(ctx -> CollectionIds.require(ctx, t, l));
        String marker = row(t, sibling, l, "marker", null, true, false);
        String own = row(t, sibling, l, "own", lId, true, false);

        var result = reaper(t).runOnce(null).tenant(t);

        assertThat(result.clientExpiry(sibling)).isNotNull();
        assertThat(result.clientExpiry(sibling).expired()).as("both rows, counted once each").isEqualTo(2);
        assertThat(result.clientExpiry(sibling).refusal()).as("no refusal").isNull();
        assertThat(result.clientExpiry(sibling).error()).as("no error").isNull();
        assertThat(held(t, sibling, marker)).isFalse();
        assertThat(held(t, sibling, own)).isFalse();
        var audit = su(ctx -> ctx.select(org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("chash_count"), Integer.class))
            .from(org.jooq.impl.DSL.table(org.jooq.impl.DSL.name("nexus", "gc_audit")))
            .where(org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("tenant_id"), String.class).eq(t)
                .and(org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("operation"), String.class).eq(ChunkReaper.AUDIT_EXPIRED_CLIENT)))
            .fetch(0, Integer.class));
        assertThat(audit).as("one audit row per function call, naming both chunks").containsExactly(2);
    }

    /** Pin of existing behaviour: the engine-moved population's expiry takes a marker row by its tag. */
    @Test
    void anEngineMovedMarkerRowPastItsAge_isReapedByReaperExpireQuarantine() throws Exception {
        String t = "p37mx-a3-" + seq.incrementAndGet();
        String dead = label("dead", seq.get());
        String sibling = "quarantine-" + label("sib", seq.get());
        register(t, sibling);
        String marker = row(t, sibling, dead, "marker", null, true, true);

        var out = new ReaperRepository(tenantScope).expire(t, sibling, dead, cutoff(), 5000, 25_000, 2_000);

        assertThat(out.expired()).isEqualTo(1);
        assertThat(held(t, sibling, marker)).isFalse();
    }

    // ---- (B) gc_expire_quarantine from the latch -----------------------------------------------------------------------------------

    /** A re-registered label A, and a former A's marker row in the same sibling beside A's own row, both old and unowned. */
    private record Relabelled(String tenant, String a, String sibling, String ownRow, String formerRow) { }

    private Relabelled relabelled(String tag) throws Exception {
        int n = seq.incrementAndGet();
        String t = "p37mx-" + tag + n;
        String a = label("a", n);
        String sibling = "quarantine-" + label("sib", n);
        register(t, a, sibling);
        long aId = su(ctx -> CollectionIds.require(ctx, t, a));
        String own = row(t, sibling, a, "own", aId, true, false);
        String former = row(t, sibling, a, "former", null, true, false);
        return new Relabelled(t, a, sibling, own, former);
    }

    @Test
    void gcExpireQuarantine_afterTheLatch_doesNotDeleteAFormerLabelsMarkerRow() throws Exception {
        var w = relabelled("b1");
        whileGate3Latched(() -> {
            var out = new PgVectorRepository(tenantScope, embedder, embedder, () -> true).expireQuarantine(w.tenant(), w.sibling(), w.a(), cutoff(), 1.0, 1000, false);
            assertThat(out.expired()).as("A's own row goes").isEqualTo(1);
            assertHeld(w, w.ownRow(), false);
            assertHeld(w, w.formerRow(), true);
        });
    }

    @Test
    void gcExpireQuarantineBounded_afterTheLatch_doesNotDeleteAFormerLabelsMarkerRow() throws Exception {
        var w = relabelled("b2");
        whileGate3Latched(() -> {
            var out = new PgVectorRepository(tenantScope, embedder, embedder, () -> true)
                .expireQuarantineBounded(w.tenant(), w.sibling(), w.a(), cutoff(), 1.0, 1000, false, 100, false);
            assertThat(out.expired()).as("A's own row goes").isEqualTo(1);
            assertHeld(w, w.ownRow(), false);
            assertHeld(w, w.formerRow(), true);
        });
    }

    /** Before the latch the label match is the rule: both rows carry the tag, and both go. */
    @Test
    void gcExpireQuarantine_beforeTheLatch_matchesByLabel_asBefore() throws Exception {
        var w = relabelled("b3");

        var out = new PgVectorRepository(tenantScope, embedder, embedder).expireQuarantine(w.tenant(), w.sibling(), w.a(), cutoff(), 1.0, 1000, false);

        assertThat(out.expired()).isEqualTo(2);
        assertHeld(w, w.ownRow(), false);
        assertHeld(w, w.formerRow(), false);
    }

    private void assertHeld(Relabelled w, String hex, boolean expected) {
        try {
            assertThat(held(w.tenant(), w.sibling(), hex)).isEqualTo(expected);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }
}
