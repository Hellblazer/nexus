// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.CollectionMint;
import dev.nexus.service.db.MintedLabel;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.Record;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Callable;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 P3.8 (nexus-p2m1x.28), the review r3 guards on the registering routes, over the real schema as a NOBYPASSRLS
 * service role:
 * <ul>
 * <li>B2: the named upsert's revive arm (an existing superseded row made live again) is judged by the tuple the row
 *     will hold, as a new row is, in both index states; and it clears {@code superseded_by_id}, which P3.1's CHECK ties
 *     to {@code superseded_by};</li>
 * <li>B3: the mint on the client route registers {@code live} collections only (dormant, disputed and quarantine are the
 *     engine's own states), as the named upsert does;</li>
 * <li>B4: a mint that finds its row applies a non-blank {@code display_name} the way the named upsert does (the
 *     {@code KNOWLEDGE_CORPUS_OPT_IN_MARKER} the knowledge opt-in stamps), and a blank one never clobbers;</li>
 * <li>B6: with the index present, a registration that passes its pre-check and loses the race to a concurrent insert
 *     (the index's own 23505) is the same refusal naming both collections, not a bare unique violation; a minted
 *     label that another row takes between the draw and the insert is a named 409;</li>
 * <li>an insert that reports no row and leaves no live row of the tuple is a named 409, not a 500.</li>
 * </ul>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionMintGuardsTest {

    private static final String TENANT = "mint-guards-tenant";
    private static final String SVC_ROLE = "svc_mint_guards";
    private static final String SVC_PASS = "svc_mint_guards_pass";
    private static final String MODEL = "bge-base-en-v15-768";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    CatalogRepository repo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var config = new HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(6);
        config.setAutoCommit(true);
        svcDs = new HikariDataSource(config);
        repo = new CatalogRepository(new TenantScope(svcDs));
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @BeforeEach
    void indexAbsent() {
        su(ctx -> {
            CollectionMintSupport.dropIndex(ctx);
            return null;
        });
    }

    private <T> T su(Function<DSLContext, T> work) {
        try (Connection c = pg.createConnection("")) {
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    private void indexState(boolean present) {
        if (present) {
            su(ctx -> {
                CollectionMintSupport.createIndex(ctx);
                return null;
            });
        }
        assertThat(su(CollectionMintSupport::indexPresent)).as("guard: the index state under test").isEqualTo(present);
    }

    private static Map<String, Object> attrs(String owner) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("content_type", "knowledge");
        m.put("owner_id", owner);
        m.put("embedding_model", MODEL);
        m.put("model_version", "v1");
        return m;
    }

    private static Map<String, Object> named(String name, String owner) {
        Map<String, Object> m = attrs(owner);
        m.put("name", name);
        return m;
    }

    private long rowsOfOwner(String owner) {
        return su(ctx -> (long) ctx.fetchCount(CATALOG_COLLECTIONS,
            CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT).and(CATALOG_COLLECTIONS.OWNER_ID.eq(owner))));
    }

    private Record row(String name) {
        return su(ctx -> ctx.select(CATALOG_COLLECTIONS.SUPERSEDED_BY, CATALOG_COLLECTIONS.SUPERSEDED_BY_ID,
                CATALOG_COLLECTIONS.DISPLAY_NAME, CATALOG_COLLECTIONS.LIFECYCLE_STATE)
            .from(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(name))
            .fetchOne());
    }

    // ── B2: the revive arm of the named upsert ──────────────────────────────────────────────────

    private void assertReviveOntoAHeldTupleIsRefusedNamingBoth(String suffix) {
        String owner = "guards-revive-" + suffix;
        repo.upsertCollection(TENANT, named("revive-holder-" + suffix, owner), true);
        su(ctx -> {
            CollectionMintSupport.insertTombstone(ctx, TENANT, "revive-tombstone-" + suffix, owner, MODEL, "revive-holder-" + suffix);
            return null;
        });

        assertThatThrownBy(() -> repo.upsertCollection(TENANT, named("revive-tombstone-" + suffix, owner), true))
            .as("reviving a tombstone onto a live tuple another collection holds is a refusal naming both, not a bare unique "
                + "violation (index present) or a second live row (index absent)")
            .isInstanceOf(CollectionMint.LiveTupleConflict.class)
            .hasMessageContaining("revive-holder-" + suffix).hasMessageContaining("revive-tombstone-" + suffix);

        assertThat(row("revive-tombstone-" + suffix).get(CATALOG_COLLECTIONS.SUPERSEDED_BY)).as("still retired")
            .isEqualTo("revive-holder-" + suffix);
        long live = su(ctx -> (long) ctx.fetchCount(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)
            .and(CATALOG_COLLECTIONS.OWNER_ID.eq(owner)).and(CATALOG_COLLECTIONS.SUPERSEDED_BY.eq(""))));
        assertThat(live).as("one live row on the tuple").isEqualTo(1L);
    }

    @Test
    void reviveOntoAHeldLiveTupleIsRefusedNamingBoth_withTheIndexAbsent() {
        indexState(false);
        assertReviveOntoAHeldTupleIsRefusedNamingBoth("off");
    }

    @Test
    void reviveOntoAHeldLiveTupleIsRefusedNamingBoth_withTheIndexPresent() {
        indexState(true);
        assertReviveOntoAHeldTupleIsRefusedNamingBoth("on");
    }

    private void assertReviveOntoAFreeTupleClearsTheSuccessorId(String suffix) {
        String owner = "guards-revive-free-" + suffix;
        su(ctx -> {
            CollectionMintSupport.insertTombstone(ctx, TENANT, "revive-free-" + suffix, owner, MODEL, "gone-" + suffix);
            return null;
        });

        repo.upsertCollection(TENANT, named("revive-free-" + suffix, owner), true);

        Record r = row("revive-free-" + suffix);
        assertThat(r.get(CATALOG_COLLECTIONS.SUPERSEDED_BY)).as("revived").isEqualTo("");
        assertThat(r.get(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID)).as("P3.1's CHECK: no successor id on a live row").isNull();
    }

    @Test
    void reviveOntoAFreeTupleMakesTheRowLiveAndClearsTheSuccessorId_withTheIndexAbsent() {
        indexState(false);
        assertReviveOntoAFreeTupleClearsTheSuccessorId("off");
    }

    @Test
    void reviveOntoAFreeTupleMakesTheRowLiveAndClearsTheSuccessorId_withTheIndexPresent() {
        indexState(true);
        assertReviveOntoAFreeTupleClearsTheSuccessorId("on");
    }

    // ── B3: the client route mints live only ────────────────────────────────────────────────────

    @Test
    void theClientRouteMintRefusesDormantDisputedAndQuarantine_andWritesNothing() {
        indexState(false);
        for (String state : List.of("dormant", "disputed", "quarantine")) {
            String owner = "guards-state-" + state;
            var body = attrs(owner);
            body.put("lifecycle_state", state);

            assertThatThrownBy(() -> repo.mintCollection(TENANT, body, true))
                .as("the client route may not mint a %s collection", state)
                .isInstanceOf(IllegalArgumentException.class).hasMessageContaining(state);
            assertThat(rowsOfOwner(owner)).as("nothing written for %s", state).isZero();
        }
    }

    @Test
    void theEnginesOwnCallersMayStillMintADormantCollection() {
        indexState(false);
        var body = attrs("guards-state-engine");
        body.put("lifecycle_state", "dormant");

        var row = repo.mintCollection(TENANT, body, false);

        assertThat(row.get("lifecycle_state")).isEqualTo("dormant");
    }

    @Test
    void anExplicitLiveStateIsAcceptedOnTheClientRoute() {
        indexState(false);
        var body = attrs("guards-state-live");
        body.put("lifecycle_state", "live");

        assertThat(repo.mintCollection(TENANT, body, true).get("lifecycle_state")).isEqualTo("live");
    }

    // ── B4: a mint that finds its row applies the display_name ──────────────────────────────────

    private void assertDisplayNameIsApplied(String suffix) {
        String owner = "guards-display-" + suffix;
        var first = repo.mintCollection(TENANT, attrs(owner), true);
        String name = (String) first.get("name");
        assertThat(first.get("display_name")).isEqualTo("");

        var withMarker = attrs(owner);
        withMarker.put("display_name", "knowledge-opt-in-marker");
        var second = repo.mintCollection(TENANT, withMarker, true);

        assertThat(second.get("name")).as("the same row").isEqualTo(name);
        assertThat(second.get("display_name")).as("the answer carries the marker").isEqualTo("knowledge-opt-in-marker");
        assertThat(row(name).get(CATALOG_COLLECTIONS.DISPLAY_NAME)).as("and so does the row").isEqualTo("knowledge-opt-in-marker");

        var third = repo.mintCollection(TENANT, attrs(owner), true);
        assertThat(third.get("display_name")).as("a blank display_name never wins").isEqualTo("knowledge-opt-in-marker");
        assertThat(rowsOfOwner(owner)).isEqualTo(1);
    }

    @Test
    void aMintThatFindsItsRowAppliesANonBlankDisplayName_andABlankOneNeverClobbers_withTheIndexAbsent() {
        indexState(false);
        assertDisplayNameIsApplied("off");
    }

    @Test
    void aMintThatFindsItsRowAppliesANonBlankDisplayName_andABlankOneNeverClobbers_withTheIndexPresent() {
        indexState(true);
        assertDisplayNameIsApplied("on");
    }

    @Test
    void aNewRowTakesTheDisplayNameOfItsMint() {
        indexState(false);
        var body = attrs("guards-display-new");
        body.put("display_name", "from-the-first-mint");

        assertThat(repo.mintCollection(TENANT, body, true).get("display_name")).isEqualTo("from-the-first-mint");
    }

    // ── B6: the race the pre-check cannot see ───────────────────────────────────────────────────

    /**
     * An open transaction inserts {@code blockerName} live on the tuple of {@code owner} and does not commit. {@code
     * registration} runs in another thread: its pre-check cannot see the uncommitted row, so it reaches its INSERT, which
     * waits on the index entry; the blocker commits, and the INSERT fails with the index's own 23505. Returns what the
     * registration threw.
     */
    private Throwable registrationLosingTheRace(String blockerName, String owner, Callable<?> registration) throws Exception {
        ExecutorService pool = Executors.newSingleThreadExecutor();
        try (Connection blocker = pg.createConnection("")) {
            blocker.setAutoCommit(false);
            CollectionMintSupport.insertLiveRow(DSL.using(blocker, SQLDialect.POSTGRES), TENANT, blockerName, owner, MODEL, null);
            Future<?> racer = pool.submit(registration);
            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(60);
            while (!su(CollectionMintSupport::someSessionWaitsOnALock)) {
                if (racer.isDone() || System.nanoTime() > deadline) {
                    blocker.rollback();
                    throw new AssertionError("the registration did not wait on the blocker's uncommitted row (done: " + racer.isDone() + ")");
                }
                Thread.sleep(25);
            }
            blocker.commit();
            try {
                racer.get(60, TimeUnit.SECONDS);
                throw new AssertionError("the registration succeeded onto a tuple the blocker holds");
            } catch (ExecutionException e) {
                return e.getCause();
            }
        } finally {
            pool.shutdownNow();
        }
    }

    @Test
    void aNamedUpsertThatLosesTheRaceToAConcurrentInsertIsRefusedNamingBoth_withTheIndexPresent() throws Exception {
        indexState(true);
        String owner = "guards-race-named";

        Throwable thrown = registrationLosingTheRace("race-blocker-named", owner,
            () -> {
                repo.upsertCollection(TENANT, named("race-racer-named", owner), true);
                return null;
            });

        assertThat(thrown).as("not a bare unique violation").isInstanceOf(CollectionMint.LiveTupleConflict.class)
            .hasMessageContaining("race-blocker-named").hasMessageContaining("race-racer-named");
        assertThat(row("race-racer-named")).as("the loser wrote nothing").isNull();
    }

    @Test
    void aBatchImportThatLosesTheRaceToAConcurrentInsertIsRefusedNamingBoth_withTheIndexPresent() throws Exception {
        indexState(true);
        String owner = "guards-race-import";

        Throwable thrown = registrationLosingTheRace("race-blocker-import", owner,
            () -> repo.importCollectionsBatchReport(TENANT, List.of(named("race-racer-import", owner))));

        assertThat(thrown).as("not a bare unique violation").isInstanceOf(CollectionMint.LiveTupleConflict.class)
            .hasMessageContaining("race-blocker-import").hasMessageContaining("race-racer-import");
        assertThat(row("race-racer-import")).as("the loser wrote nothing").isNull();
    }

    @Test
    void aMintedLabelAnotherRowTakesBetweenTheDrawAndTheInsertIsANamedConflict() throws Exception {
        indexState(false);
        long next = su(CollectionMintSupport::nextSequenceValue);
        String label = "c" + next;
        ExecutorService pool = Executors.newSingleThreadExecutor();
        try (Connection blocker = pg.createConnection("")) {
            blocker.setAutoCommit(false);
            // the label the mint will draw, on a row of another tuple, under an explicit id that no draw will reach
            CollectionMintSupport.insertLiveRow(DSL.using(blocker, SQLDialect.POSTGRES), TENANT, label, "guards-label-other", MODEL, -next);
            Future<?> mint = pool.submit(() -> repo.mintCollection(TENANT, attrs("guards-label-mint"), true));
            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(60);
            while (!su(CollectionMintSupport::someSessionWaitsOnALock)) {
                if (mint.isDone() || System.nanoTime() > deadline) {
                    blocker.rollback();
                    throw new AssertionError("the mint did not wait on the blocker's uncommitted label (done: " + mint.isDone() + ")");
                }
                Thread.sleep(25);
            }
            blocker.commit();
            assertThatThrownBy(() -> mint.get(60, TimeUnit.SECONDS)).isInstanceOf(ExecutionException.class)
                .cause().isInstanceOf(MintedLabel.LabelsTakenException.class).hasMessageContaining(label);
            assertThat(rowsOfOwner("guards-label-mint")).as("the mint wrote nothing").isZero();
        } finally {
            pool.shutdownNow();
        }
    }

    // ── an insert that reports no row and leaves no live row ────────────────────────────────────

    @Test
    void aMintWhoseInsertLeftNoLiveRowOfTheTupleIsANamedConflictNotAnInternalError() {
        assertThatThrownBy(() -> CollectionMint.minted(null, 0, "knowledge", "guards-seam", MODEL, "v1"))
            .isInstanceOf(CollectionMint.LiveTupleConflict.class)
            .hasMessageContaining("guards-seam").hasMessageContaining("try again");

        var row = new LinkedHashMap<String, Object>();
        row.put("name", "c1");
        assertThat(CollectionMint.minted(row, 1, "knowledge", "guards-seam", MODEL, "v1")).isSameAs(row);
    }
}
