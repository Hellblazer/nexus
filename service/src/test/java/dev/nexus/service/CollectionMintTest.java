// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

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
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Callable;
import java.util.concurrent.CyclicBarrier;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 P3.8 (nexus-p2m1x.28), the engine mint: a registration with no name takes the next {@code collection_id}
 * and the label {@code "c" + id} in one insert, returns the row, and mints nothing for a live tuple that already has
 * one. Over the real schema (P3.1's {@code collection_id}, {@code BY DEFAULT AS IDENTITY}) as a NOBYPASSRLS service
 * role, as the engine runs. The two index states of {@code collections-001-2} are both exercised: the partial UNIQUE
 * index present (the mint is {@code INSERT ... ON CONFLICT ... DO NOTHING} and a re-read) and absent (the engine boots
 * before the re-own move has completed, so {@code ON CONFLICT} on that index would raise 42P10, and the mint takes a
 * per-tuple advisory lock instead). The heal of a sequence behind the data is {@link CollectionMintHealTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionMintTest {

    private static final String TENANT = "mint-tenant-a";
    private static final String SVC_ROLE = "svc_mint_test";
    private static final String SVC_PASS = "svc_mint_test_pass";
    private static final String MODEL = "bge-base-en-v15-768";
    private static final String INDEX = "catalog_collections_live_tuple_uq";

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

    /** Every test starts with the index absent, which is the state of a fresh install until the re-own move completes. */
    @BeforeEach
    void indexAbsent() {
        su(ctx -> ctx.execute("DROP INDEX IF EXISTS nexus." + INDEX));
    }

    private <T> T su(Function<DSLContext, T> work) {
        try (Connection c = pg.createConnection("")) {
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    private boolean indexPresent() {
        return su(ctx -> ctx.resultQuery("SELECT to_regclass('nexus." + INDEX + "') IS NOT NULL").fetchOne(0, Boolean.class));
    }

    private void createIndex() {
        su(ctx -> ctx.execute("CREATE UNIQUE INDEX IF NOT EXISTS " + INDEX + " ON nexus.catalog_collections "
            + "(tenant_id, content_type, owner_id, embedding_model, model_version) "
            + "WHERE superseded_by = '' AND lifecycle_state <> 'quarantine'"));
    }

    private static Map<String, Object> attrs(String owner) {
        return attrs(owner, "v1");
    }

    private static Map<String, Object> attrs(String owner, String version) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("content_type", "knowledge");
        m.put("owner_id", owner);
        m.put("embedding_model", MODEL);
        m.put("model_version", version);
        return m;
    }

    private static long id(Map<String, Object> row) {
        return ((Number) row.get("collection_id")).longValue();
    }

    private long rowsOfTuple(String owner) {
        return su(ctx -> (long) ctx.fetchCount(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)
            .and(CATALOG_COLLECTIONS.CONTENT_TYPE.eq("knowledge")).and(CATALOG_COLLECTIONS.OWNER_ID.eq(owner))));
    }

    /** The value the sequence hands out next. */
    private long nextSequenceValue() {
        return su(ctx -> {
            String seq = ctx.resultQuery("SELECT pg_get_serial_sequence('nexus.catalog_collections', 'collection_id')").fetchOne(0, String.class);
            var r = ctx.fetchOne("SELECT last_value, is_called FROM " + seq);
            return r.get(1, Boolean.class) ? r.get(0, Long.class) + 1 : r.get(0, Long.class);
        });
    }

    /** A live row of {@code TENANT} with this name, as a superuser writes it (the sequence assigns its id). */
    private void seedLiveRow(String name, String owner) {
        su(ctx -> ctx.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME,
                CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID, CATALOG_COLLECTIONS.EMBEDDING_MODEL,
                CATALOG_COLLECTIONS.MODEL_VERSION, CATALOG_COLLECTIONS.LIFECYCLE_STATE)
            .values(TENANT, name, "knowledge", owner, MODEL, "v1", "live").execute());
    }

    // ── the mint ────────────────────────────────────────────────────────────────────────────────

    @Test
    void aRegistrationWithNoNameMintsTheLabelFromTheCollectionIdAndStampsTheAttributes() {
        var row = repo.mintCollection(TENANT, attrs("subject-mint-1"), true);

        assertThat(row.get("name")).isEqualTo("c" + id(row));
        assertThat(row.get("content_type")).isEqualTo("knowledge");
        assertThat(row.get("owner_id")).isEqualTo("subject-mint-1");
        assertThat(row.get("embedding_model")).isEqualTo(MODEL);
        assertThat(row.get("model_version")).isEqualTo("v1");
        assertThat(row.get("lifecycle_state")).isEqualTo("live");
        // the row is stored, with the id the response names, under that label
        Long stored = su(ctx -> ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq((String) row.get("name")))
            .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID));
        assertThat(stored).isEqualTo(id(row));
        assertThat(repo.getCollection(TENANT, (String) row.get("name"))).as("the registry answers the minted label").isNotNull();
    }

    @Test
    void aSecondRegistrationForTheSameTupleReturnsTheSameRowAndMintsNothing() {
        var first = repo.mintCollection(TENANT, attrs("subject-mint-2"), true);
        long before = nextSequenceValue();

        var second = repo.mintCollection(TENANT, attrs("subject-mint-2"), true);

        assertThat(id(second)).isEqualTo(id(first));
        assertThat(second.get("name")).isEqualTo(first.get("name"));
        assertThat(rowsOfTuple("subject-mint-2")).isEqualTo(1);
        assertThat(nextSequenceValue()).as("a found row draws no id").isEqualTo(before);
    }

    @Test
    void anotherModelVersionIsAnotherTuple() {
        var v1 = repo.mintCollection(TENANT, attrs("subject-mint-3", "v1"), true);
        var v2 = repo.mintCollection(TENANT, attrs("subject-mint-3", "v2"), true);

        assertThat(id(v2)).isNotEqualTo(id(v1));
        assertThat(v2.get("model_version")).isEqualTo("v2");
        assertThat(rowsOfTuple("subject-mint-3")).isEqualTo(2);
    }

    @Test
    void theRequiredAttributesAreRequiredAndNothingIsWrittenWithoutThem() {
        var noVersion = attrs("subject-mint-4");
        noVersion.remove("model_version");
        assertThatThrownBy(() -> repo.mintCollection(TENANT, noVersion, true))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("model_version");
        var blankVersion = attrs("subject-mint-4", "  ");
        assertThatThrownBy(() -> repo.mintCollection(TENANT, blankVersion, true))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("model_version");
        var noOwner = attrs("subject-mint-4");
        noOwner.remove("owner_id");
        assertThatThrownBy(() -> repo.mintCollection(TENANT, noOwner, true))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("owner_id");
        var noModel = attrs("subject-mint-4");
        noModel.remove("embedding_model");
        assertThatThrownBy(() -> repo.mintCollection(TENANT, noModel, true))
            .isInstanceOf(CatalogRepository.EmbeddingProfileConflictException.class).hasMessageContaining("embedding_model");
        var noType = attrs("subject-mint-4");
        noType.remove("content_type");
        assertThatThrownBy(() -> repo.mintCollection(TENANT, noType, true))
            .isInstanceOf(CatalogRepository.EmbeddingProfileConflictException.class).hasMessageContaining("content_type");

        assertThat(rowsOfTuple("subject-mint-4")).isZero();
    }

    @Test
    void theClientRouteMintsLiveOnlyAndNeverAQuarantineLabel() {
        var quarantine = attrs("subject-mint-5");
        quarantine.put("lifecycle_state", "quarantine");
        assertThatThrownBy(() -> repo.mintCollection(TENANT, quarantine, true)).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> repo.mintCollection(TENANT, quarantine, false)).isInstanceOf(IllegalArgumentException.class);
        assertThat(rowsOfTuple("subject-mint-5")).isZero();
    }

    @Test
    void aBodyCarryingACollectionIdIsRefusedAndNothingIsWritten() {
        var body = attrs("subject-mint-6");
        body.put("collection_id", 424242L);
        assertThatThrownBy(() -> repo.mintCollection(TENANT, body, true))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("collection_id");
        assertThatThrownBy(() -> repo.mintCollection(TENANT, body, false))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("collection_id");
        assertThat(rowsOfTuple("subject-mint-6")).isZero();
    }

    // ── a live label of the minted form ─────────────────────────────────────────────────────────

    @Test
    void aLiveLabelOfTheMintedFormMakesTheMintDrawTheNextId() {
        long next = nextSequenceValue();
        // this row takes id `next` from the sequence and the label of the id after it
        seedLiveRow("c" + (next + 1), "subject-mint-7-existing");

        var row = repo.mintCollection(TENANT, attrs("subject-mint-7"), true);

        assertThat(id(row)).as("c%d was taken, so the id after it is drawn", next + 1).isEqualTo(next + 2);
        assertThat(row.get("name")).isEqualTo("c" + (next + 2));
    }

    @Test
    void whenEveryDrawnLabelIsLiveTheMintRefuses() {
        long next = nextSequenceValue();
        int draws = MintedLabel.MAX_DRAWS;
        // the seeds take ids next .. next+draws-1, and name the labels of the ids the mint then draws
        for (int k = 0; k < draws; k++) {
            seedLiveRow("c" + (next + draws + k), "subject-mint-8-existing-" + k);
        }

        assertThatThrownBy(() -> repo.mintCollection(TENANT, attrs("subject-mint-8"), true))
            .isInstanceOf(MintedLabel.LabelsTakenException.class);
        assertThat(rowsOfTuple("subject-mint-8")).isZero();
    }

    // ── concurrent first mints ──────────────────────────────────────────────────────────────────

    @Test
    void concurrentFirstMintsOfOneTupleMakeOneRow_withTheIndexPresent() throws Exception {
        createIndex();
        assertThat(indexPresent()).as("guard: the ON CONFLICT path runs").isTrue();
        assertConcurrentMintsMakeOneRow("subject-race-on-");
    }

    @Test
    void concurrentFirstMintsOfOneTupleMakeOneRow_withTheIndexAbsent() throws Exception {
        assertThat(indexPresent()).as("guard: ON CONFLICT on the index would raise 42P10 here, so the advisory-lock path runs").isFalse();
        assertConcurrentMintsMakeOneRow("subject-race-off-");
    }

    private void assertConcurrentMintsMakeOneRow(String ownerPrefix) throws Exception {
        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            for (int i = 0; i < 12; i++) {
                String owner = ownerPrefix + i;
                var gate = new CyclicBarrier(2);
                Callable<Map<String, Object>> mint = () -> {
                    gate.await(30, TimeUnit.SECONDS);
                    return repo.mintCollection(TENANT, attrs(owner), true);
                };
                var a = pool.submit(mint);
                var b = pool.submit(mint);
                var ra = a.get(90, TimeUnit.SECONDS);
                var rb = b.get(90, TimeUnit.SECONDS);
                assertThat(id(rb)).as("both callers got the one row, tuple %s", owner).isEqualTo(id(ra));
                assertThat(rb.get("name")).isEqualTo(ra.get("name"));
                assertThat(rowsOfTuple(owner)).as("one row for %s", owner).isEqualTo(1);
            }
        } finally {
            pool.shutdownNow();
        }
    }

    // ── the label is the only name the mint knows ───────────────────────────────────────────────

    @Test
    void theMintedRowsAreFoundByTheirAttributes() {
        var row = repo.mintCollection(TENANT, attrs("subject-mint-9"), true);
        var found = repo.collectionForTuple(TENANT, "knowledge", "subject-mint-9", MODEL);
        assertThat(found).isNotNull();
        assertThat(found.get("name")).isEqualTo(row.get("name"));
        List<String> names = new ArrayList<>();
        names.add((String) found.get("name"));
        assertThat(names).allMatch(n -> n.matches("c[0-9]+"));
    }
}
