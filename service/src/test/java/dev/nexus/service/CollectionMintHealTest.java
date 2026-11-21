// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.MintedLabel;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.TestMethodOrder;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 P3.8 (nexus-p2m1x.28): the mint when the {@code collection_id} sequence is behind the data. A restore that
 * writes explicit ids and runs no {@code setval} leaves the sequence below the rows it wrote; the next mint draws an id
 * that is already a row. The mint heals ONCE (through {@code nexus.collection_id_seq_heal}, the definer function of
 * {@code collections-002}: the engine role holds no UPDATE on the shared sequence, and the function only raises it), logs a WARN naming the old and new values, and draws again;
 * a second taken id is a {@link MintedLabel.SequenceBehindDataException} (lead ruling T2
 * {@code nexus_rdr/229-lead-ruling-collection-id-by-default-2026-11-21}). Its own database: it moves the sequence.
 *
 * <p>The heal draws FORWARD (linda's ruling on the heal race, bead nexus-p2m1x.28): it calls {@code nextval} until it has
 * drawn the floor, never {@code setval}, so it cannot move the shared sequence back past an id another session (another
 * tenant, a plain DEFAULT-identity insert) has already drawn. At most {@value #BOUND} draws: a floor further above the
 * sequence than that is refused with the gap and the operator remedy.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class CollectionMintHealTest {

    private static final String TENANT = "mint-heal-tenant";
    private static final String SVC_ROLE = "svc_mint_heal";
    private static final String SVC_PASS = "svc_mint_heal_pass";
    private static final String MODEL = "bge-base-en-v15-768";
    /** The most draws one heal makes (collections-002). */
    static final int BOUND = 100_000;
    /** The rows tenant B inserts in the race test: few, because each heal call scans the whole table. */
    static final int B_ROWS = 2_000;

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
        config.setMaximumPoolSize(4);
        config.setAutoCommit(true);
        svcDs = new HikariDataSource(config);
        repo = new CatalogRepository(new TenantScope(svcDs));
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private <T> T su(Function<DSLContext, T> work) {
        try (Connection c = pg.createConnection("")) {
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    private static Map<String, Object> attrs(String owner) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("content_type", "knowledge");
        m.put("owner_id", owner);
        m.put("embedding_model", MODEL);
        m.put("model_version", "v1");
        return m;
    }

    private static long id(Map<String, Object> row) {
        return ((Number) row.get("collection_id")).longValue();
    }

    private long nextSequenceValue() {
        return su(CollectionMintSupport::nextSequenceValue);
    }

    /** A live row with an explicit id, as a restore writes it (no sequence involved). */
    private void seedWithExplicitId(String name, long collectionId) {
        su(ctx -> {
            CollectionMintSupport.insertLiveRow(ctx, TENANT, name, "restored-" + name, MODEL, collectionId);
            return null;
        });
    }

    private <T> T svc(Function<DSLContext, T> work) {
        try (Connection c = svcDs.getConnection()) {
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    @Test @Order(0)
    void theEngineRoleHealsThroughADefinerFunctionAndHoldsNoUpdateOnTheSequence() {
        // nexus_svc is the production role; svc_mint_heal is this class's own login role, which the test changelog grants
        // exactly what nexus_svc holds. Neither may setval the shared sequence directly (a tenant must not be able to move
        // the identity every tenant draws from); both may call the one function that moves it forward.
        boolean prodExecute = su(ctx -> CollectionMintSupport.holdsExecuteOnHeal(ctx, "nexus_svc"));
        boolean prodUpdate = su(ctx -> CollectionMintSupport.holdsUpdateOnSequence(ctx, "nexus_svc"));
        boolean classExecute = su(ctx -> CollectionMintSupport.holdsExecuteOnHeal(ctx, SVC_ROLE));
        boolean classUpdate = su(ctx -> CollectionMintSupport.holdsUpdateOnSequence(ctx, SVC_ROLE));
        assertThat(prodExecute).as("nexus_svc may EXECUTE " + CollectionMintSupport.HEAL_FUNCTION).isTrue();
        assertThat(prodUpdate).as("nexus_svc holds no UPDATE on the collection_id sequence").isFalse();
        assertThat(classExecute).as("the test's service role may EXECUTE the heal function").isTrue();
        assertThat(classUpdate).as("the test's service role holds no UPDATE on the sequence").isFalse();
        String definition = su(CollectionMintSupport::healFunctionDefinition);
        assertThat(definition).as("a definer function with its search_path pinned")
            .contains("SECURITY DEFINER").containsIgnoringCase("search_path");
        assertThat(definition).as("the heal draws forward with nextval and never calls setval: a setval can move the sequence "
                + "back past an id another session has just drawn")
            .containsIgnoringCase("nextval").doesNotContainPattern("(?i)setval\\s*\\(");
    }

    @Test @Order(1)
    void anIdAtOrAboveTheSequenceIsHealedOnceAndTheMintDrawsPastIt() {
        long next = nextSequenceValue();
        seedWithExplicitId("restored-at-next", next);
        seedWithExplicitId("restored-far-above", next + 5);
        var logs = new ListAppender<ILoggingEvent>();
        logs.start();
        var root = (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME);
        root.addAppender(logs);
        Map<String, Object> row;
        try {
            row = repo.mintCollection(TENANT, attrs("subject-heal-1"), true);
        } finally {
            root.detachAppender(logs);
        }

        assertThat(id(row)).as("the sequence was set to the largest id, so the next draw is past every restored row")
            .isEqualTo(next + 6);
        assertThat(row.get("name")).isEqualTo("c" + (next + 6));
        assertThat(logs.list.stream().map(ILoggingEvent::getFormattedMessage))
            .anyMatch(m -> m.contains("event=collection_id_sequence_behind_data"));
        assertThat(nextSequenceValue()).as("the heal left the sequence after the minted id").isEqualTo(next + 7);
    }

    @Test @Order(0)
    void theTakenFunctionIsADefinerWithAPinnedPathAndOnlyTheEngineRolesMayExecuteIt() {
        // the id-taken check answers a boolean over every tenant's rows: one gated entry point, never PUBLIC
        String definition = su(ctx -> CollectionMintSupport.functionDefinition(ctx, CollectionMintSupport.TAKEN_FUNCTION));
        assertThat(definition).as("a definer function with its search_path pinned")
            .contains("SECURITY DEFINER").containsIgnoringCase("search_path");
        boolean prod = su(ctx -> CollectionMintSupport.holdsExecute(ctx, "nexus_svc", CollectionMintSupport.TAKEN_FUNCTION));
        boolean cls = su(ctx -> CollectionMintSupport.holdsExecute(ctx, SVC_ROLE, CollectionMintSupport.TAKEN_FUNCTION));
        boolean pub = su(ctx -> CollectionMintSupport.holdsExecute(ctx, "public", CollectionMintSupport.TAKEN_FUNCTION));
        boolean pubHeal = su(ctx -> CollectionMintSupport.holdsExecute(ctx, "public", CollectionMintSupport.HEAL_FUNCTION));
        assertThat(prod).as("nexus_svc may EXECUTE it").isTrue();
        assertThat(cls).as("the test's service role may EXECUTE it").isTrue();
        assertThat(pub).as("PUBLIC may not").isFalse();
        assertThat(pubHeal).as("nor the heal").isFalse();
    }

    @Test @Order(2)
    void aMintNeverTakesAnIdOnlyAnotherTenantHolds() {
        // B holds the id the sequence hands out next and A holds nothing at it. A tenant-scoped check of A's own rows says
        // "free", and A's row would repeat B's id across tenants (the table's UNIQUE is per tenant).
        String other = "mint-heal-tenant-b";
        long n = nextSequenceValue();
        su(ctx -> {
            CollectionMintSupport.insertLiveRow(ctx, other, "only-b-holds", "restored-only-b", MODEL, n);
            return null;
        });
        boolean heldByB = svc(ctx -> CollectionMintSupport.callTaken(ctx, n));
        boolean freeId = svc(ctx -> CollectionMintSupport.callTaken(ctx, n + 1_000_000_000L));
        assertThat(heldByB).as("the id-taken function sees another tenant's row: a boolean, no tenant's data").isTrue();
        assertThat(freeId).as("and a free id is free").isFalse();

        var a = repo.mintCollection(TENANT, attrs("subject-only-b"), true);

        assertThat(id(a)).as("A drew n, found it held by B, healed and drew the next").isGreaterThan(n);
        assertThat(a.get("name")).isEqualTo("c" + id(a));
        assertNoIdIsHeldTwiceAcrossTenants();
    }

    @Test @Order(3)
    void theHealFunctionOnlyMovesTheSequenceForward() {
        long next = nextSequenceValue();

        Long raised = healAs(next + 100);
        assertThat(raised).as("a floor above the sequence raises it").isEqualTo(next + 100);
        assertThat(nextSequenceValue()).isEqualTo(next + 101);

        Long lower = healAs(1L);
        Long middle = healAs(next + 50);
        Long none = healAs(null);
        assertThat(lower).as("a lower floor leaves it").isEqualTo(next + 100);
        assertThat(middle).isEqualTo(next + 100);
        assertThat(none).as("no floor leaves it").isEqualTo(next + 100);
        assertThat(nextSequenceValue()).as("nothing moved it back").isEqualTo(next + 101);
    }

    private Long healAs(Long floor) {
        return svc(ctx -> CollectionMintSupport.callHeal(ctx, floor));
    }

    @Test @Order(4)
    void aHealInOneTenantNeverMovesTheSharedSequenceBackAndNoTenantsMintTakesAnIdAnotherTenantHolds() {
        // An id is unique across the installation (RDR-229 item 6; linda's ruling on D-a). The identity sequence is one for
        // every tenant and a tenant sees only its own rows (FORCE RLS), so the heal's floor and the mint's id-taken check
        // read through definer functions that answer a number and a boolean across every tenant. Tenant A holds an id at the
        // sequence's next draw, so A's mint collides and heals; tenant B holds the ids just above. A's heal must clear B's
        // ids too, or A's next draw is an id B holds.
        String other = "mint-heal-tenant-b";
        long n = nextSequenceValue();
        seedWithExplicitId("two-a-held", n);
        su(ctx -> {
            for (long k = 1; k <= 3; k++) {
                CollectionMintSupport.insertLiveRow(ctx, other, "two-b-held-" + k, "restored-two-b-" + k, MODEL, n + k);
            }
            return null;
        });
        long before = n - 1;

        var a = repo.mintCollection(TENANT, attrs("subject-two-a"), true);

        assertThat(id(a)).as("A's heal read B's largest id (n+3) and A drew past it: no id B holds").isEqualTo(n + 4);
        assertThat(a.get("name")).isEqualTo("c" + (n + 4));
        assertThat(nextSequenceValue() - 1).as("A's heal left the sequence at or above where it was").isGreaterThanOrEqualTo(before);

        var b = repo.mintCollection(other, attrs("subject-two-b"), true);

        assertThat(id(b)).as("B's mint drew the next id, past A's").isEqualTo(n + 5);
        assertThat(b.get("name")).isEqualTo("c" + (n + 5));
        assertNoIdIsHeldTwiceAcrossTenants();
    }

    /** No {@code collection_id} is held by two rows, whatever their tenants (the table's own UNIQUE is per tenant). */
    private void assertNoIdIsHeldTwiceAcrossTenants() {
        List<Long> repeated = su(ctx -> ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .groupBy(CATALOG_COLLECTIONS.COLLECTION_ID).having(DSL.count().gt(1)).fetch(CATALOG_COLLECTIONS.COLLECTION_ID));
        assertThat(repeated).as("ids held by more than one row across the tenants").isEmpty();
    }

    @Test @Order(5)
    void aFloorMoreThanTheBoundAboveTheSequenceIsRefusedWithTheGapAndTheRemedyAndMovesNothing() {
        long last = nextSequenceValue() - 1;
        long floor = last + BOUND + 1;

        assertThatThrownBy(() -> healAs(floor))
            .as("a gap above the bound is refused, not drawn")
            .hasMessageContaining(Long.toString(floor))
            .hasMessageContaining(Long.toString(last))
            .hasMessageContaining(Long.toString(BOUND + 1L))
            .hasMessageContaining("setval")
            .hasMessageContaining("engine stopped");
        assertThat(nextSequenceValue()).as("the refusal drew nothing").isEqualTo(last + 1);
    }

    @Test @Order(6)
    void aFloorExactlyTheBoundAboveTheSequenceIsDrawnTo() {
        long last = nextSequenceValue() - 1;

        Long reached = healAs(last + BOUND);

        assertThat(reached).as("the heal drew up to the floor").isEqualTo(last + BOUND);
        assertThat(nextSequenceValue()).isEqualTo(last + BOUND + 1);
    }

    @Test @Order(7)
    void aMintWhoseHealIsRefusedForItsGapAnswersTheRefusalWithTheMessageAndWritesNothing() {
        String third = "mint-heal-tenant-gap";
        long n = nextSequenceValue();
        su(ctx -> {
            CollectionMintSupport.insertLiveRow(ctx, third, "gap-held-at-next", "restored-gap-a", MODEL, n);
            CollectionMintSupport.insertLiveRow(ctx, third, "gap-held-far-above", "restored-gap-b", MODEL, n + BOUND + 50L);
            return null;
        });
        try {
            assertThatThrownBy(() -> repo.mintCollection(third, attrs("subject-gap"), true))
                .isInstanceOf(MintedLabel.SequenceBehindDataException.class)
                .hasMessageContaining(Long.toString(n + BOUND + 50L))
                .hasMessageContaining("setval")
                .hasMessageContaining("engine stopped");
            long written = su(ctx -> (long) ctx.fetchCount(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID.eq(third)
                .and(CATALOG_COLLECTIONS.OWNER_ID.eq("subject-gap"))));
            assertThat(written).as("nothing was written").isZero();
            assertThat(nextSequenceValue()).as("only the mint's own draw moved the sequence").isEqualTo(n + 1);
        } finally {
            su(ctx -> ctx.deleteFrom(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.eq(third)).execute());
        }
    }

    @Test @Order(8)
    void drawersRacingTheHealInTwoTenantsNeverSeeAnIdTwice() throws Exception {
        // Tenant A draws ids as its mints do (a stream of nextval); tenant B inserts rows whose id the column's DEFAULT
        // identity assigns; the heal runs with a floor one above the sequence's value at that instant, the interleaving a
        // concurrent draw has to cross. A setval-based heal sets the sequence back past an id one of them has just drawn,
        // and the next draw repeats it, possibly in the other tenant: UNIQUE is (tenant_id, collection_id), so nothing else
        // refuses the repeat. Every id either side drew or was assigned must be distinct.
        String other = "mint-heal-tenant-b";
        int heals = 100_000;
        var start = new CountDownLatch(1);
        var healed = new AtomicBoolean(false);
        ExecutorService pool = Executors.newFixedThreadPool(3);
        try {
            Future<List<long[]>> tenantA = pool.submit(() -> {
                List<long[]> drawn = new ArrayList<>();
                try (Connection c = pg.createConnection("")) {
                    var ctx = DSL.using(c, SQLDialect.POSTGRES);
                    start.await();
                    do {
                        drawn.add(CollectionMintSupport.drawIds(ctx, 20_000));
                    } while (!healed.get());
                }
                return drawn;
            });
            Future<List<Long>> tenantB = pool.submit(() -> {
                List<Long> assigned = new ArrayList<>();
                try (Connection c = pg.createConnection("")) {
                    var ctx = DSL.using(c, SQLDialect.POSTGRES);
                    start.await();
                    // the heal reads max(collection_id) over every tenant's rows at each call (the installation-wide floor), so
                    // each of its 100000 calls scans the table: B's rows are capped so that scan stays small (20000 rows made the
                    // run take minutes); tenant A's draws overlap every heal on their own
                    for (int k = 0; k < B_ROWS && !healed.get(); k++) {
                        assigned.add(CollectionMintSupport.insertWithDefaultId(ctx, other, "race-b-" + k, "race-owner-" + k, MODEL));
                    }
                }
                return assigned;
            });
            Future<Integer> healer = pool.submit(() -> {
                try (Connection c = svcDs.getConnection()) {
                    start.await();
                    return CollectionMintSupport.healAtTheEdge(DSL.using(c, SQLDialect.POSTGRES), heals);
                } finally {
                    healed.set(true);
                }
            });
            start.countDown();

            assertThat(healer.get(180, TimeUnit.SECONDS)).as("every heal call ran").isEqualTo(heals);
            long[] all = Arrays.stream(new long[][] {
                tenantA.get(60, TimeUnit.SECONDS).stream().flatMapToLong(Arrays::stream).toArray(),
                tenantB.get(60, TimeUnit.SECONDS).stream().mapToLong(Long::longValue).toArray()
            }).flatMapToLong(Arrays::stream).toArray();
            assertThat(all.length).as("the drawers overlapped the heals (non-vacuity)").isGreaterThan(20_000);
            Arrays.sort(all);
            List<Long> repeated = new ArrayList<>();
            for (int i = 1; i < all.length; i++) {
                if (all[i] == all[i - 1]) {
                    repeated.add(all[i]);
                }
            }
            assertThat(repeated).as("ids drawn or assigned twice across the two tenants, of " + all.length + " drawn").isEmpty();
        } finally {
            pool.shutdownNow();
        }
    }

    @Test @Order(9)
    void aSecondTakenIdAfterTheHealIsRefusedAsSequenceBehindData() {
        // The sequence cycles over [1, max]: the first draw wraps to 1 (a row), the heal draws forward to max, and the next draw
        // wraps to 1 again, which is a row too.
        su(ctx -> {
            // the other tenant of the tests above goes, so this tenant's rows alone fix the cycle's span
            ctx.deleteFrom(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.ne(TENANT)).execute();
            CollectionMintSupport.cycleOverTheRows(ctx, TENANT, "cycle-anchor", MODEL);
            return null;
        });

        assertThatThrownBy(() -> repo.mintCollection(TENANT, attrs("subject-heal-2"), true))
            .isInstanceOf(MintedLabel.SequenceBehindDataException.class);
        long written = su(ctx -> (long) ctx.fetchCount(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)
            .and(CATALOG_COLLECTIONS.OWNER_ID.eq("subject-heal-2"))));
        assertThat(written).as("nothing was written").isZero();
    }
}
