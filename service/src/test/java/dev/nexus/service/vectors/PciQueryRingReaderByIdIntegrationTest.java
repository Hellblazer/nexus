// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.VecPciRegistryNames;
import dev.nexus.service.datamove.Gate3;
import dev.nexus.service.db.CollectionIds;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicBoolean;

import static dev.nexus.service.jooq.nexus.Tables.CANARY_QUERY_RING;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25): {@link PciQueryRingReader#load} reads the collection's ring by {@code collection_id} from gate 3, so a
 * registry row renamed in place (the ring rows keep their old label) is read under its new label, and by the name before gate 3. A NULL
 * id is no collection: the {@code ''} label never falls back to a {@code ''} name.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PciQueryRingReaderByIdIntegrationTest {

    private static final String MODEL = "minilm-l6-v2-384";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope scope;

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
            assertThat(VecPciRegistryNames.dropRegistryNameKeys(DSL.using(su, SQLDialect.POSTGRES)))
                .as("non-vacuity: the registry's name keys exist and are dropped").isNotEmpty();
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(2);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        scope = new TenantScope(svcDs);
    }

    @AfterAll
    void stop() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private static String newTenant() {
        return "ringid-" + UUID.randomUUID().toString().substring(0, 8);
    }

    private static String label(String tenant, String tag) {
        return "knowledge__" + tenant + "-" + tag + "__minilm-l6-v2-384__v1";
    }

    private static float[] vec(int seed) {
        float[] v = new float[384];
        for (int i = 0; i < v.length; i++) {
            v[i] = (((seed + 1) * 31 + i * 7) % 997) / 1000f + 0.001f;
        }
        return v;
    }

    /** Registers {@code name} and gives it {@code seeds.length} ring rows stamped with its id. */
    private long collectionWithRing(String tenant, String name, int... seeds) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), tenant, name, MODEL);
        }
        long id = scope.withTenant(tenant, ctx -> CollectionIds.require(ctx, tenant, name));
        for (int seed : seeds) {
            insert(tenant, name, id, seed);
        }
        return id;
    }

    private void insert(String tenant, String name, Long id, int seed) {
        scope.withTenant(tenant, ctx -> ctx.insertInto(CANARY_QUERY_RING)
            .set(CANARY_QUERY_RING.TENANT_ID, tenant).set(CANARY_QUERY_RING.COLLECTION, name)
            .set(CANARY_QUERY_RING.COLLECTION_ID, id)
            .set(CANARY_QUERY_RING.EMBEDDING_MODEL, MODEL)
            .set(CANARY_QUERY_RING.EMBEDDING_384, Vector.of(vec(seed)))
            .set(CANARY_QUERY_RING.VECTOR_DIGEST, PciQueryRing.digest(vec(seed)))
            .set(CANARY_QUERY_RING.ADDED_AT, OffsetDateTime.now().minusSeconds(100 - seed))
            .execute());
    }

    private void rename(String tenant, String from, String to) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            int n = DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, to)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(from)).execute();
            assertThat(n).as("the registry row is renamed in place").isEqualTo(1);
        }
        int carrying = scope.withTenant(tenant, ctx -> ctx.fetchCount(CANARY_QUERY_RING, CANARY_QUERY_RING.COLLECTION.eq(to)));
        assertThat(carrying)
            .as("precondition: no ring row carries the new label").isZero();
    }

    @Test
    void aRenamedCollectionsRing_isReadUnderTheNewLabelFromGate3_andTheOldOneBefore() throws Exception {
        String t = newTenant(), old = label(t, "old"), renamed = label(t, "new");
        collectionWithRing(t, old, 1, 2, 3);
        rename(t, old, renamed);

        var byId = new PciQueryRingReader(scope, () -> true);
        assertThat(byId.load(t, renamed)).as("gate 3: the id of the new label").hasSize(3);
        assertThat(byId.load(t, old)).as("gate 3: the old label is no collection").isEmpty();

        var byName = new PciQueryRingReader(scope, Gate3.never());
        assertThat(byName.load(t, old)).as("before gate 3: the rows' own label, as today").hasSize(3);
        assertThat(byName.load(t, renamed)).isEmpty();
        assertThat(new PciQueryRingReader(scope).load(t, old)).as("the one-argument constructor is the name path").hasSize(3);
    }

    @Test
    void anotherCollectionThatTookTheOldLabel_isNotReadForTheRenamedOne() throws Exception {
        String t = newTenant(), old = label(t, "a-old"), renamed = label(t, "a-new");
        collectionWithRing(t, old, 1, 2);
        rename(t, old, renamed);
        collectionWithRing(t, old, 7);   // a new collection takes the freed label

        var byId = new PciQueryRingReader(scope, () -> true);
        assertThat(byId.load(t, old)).as("the label's collection now: one ring row").hasSize(1);
        assertThat(byId.load(t, old).get(0).vector()).containsExactly(vec(7));
        assertThat(byId.load(t, renamed)).as("the renamed collection's two").hasSize(2);
    }

    @Test
    void aNullIdIsNoCollection_theEmptyLabelNeverFallsBackToAnEmptyName() throws Exception {
        String t = newTenant();
        collectionWithRing(t, label(t, "x"), 1);
        insert(t, "", null, 9);   // a ring row whose name is '' keeps a NULL id (NULLIF rule)

        assertThat(new PciQueryRingReader(scope, () -> true).load(t, "")).as("the id arm has no name arm").isEmpty();
        assertThat(new PciQueryRingReader(scope, () -> true).load(t, label(t, "unregistered"))).isEmpty();
        assertThat(new PciQueryRingReader(scope, Gate3.never()).load(t, "")).as("before gate 3 the name path is unchanged").hasSize(1);
    }

    @Test
    void gate3IsReadOnEveryLoad_notCached() throws Exception {
        String t = newTenant(), old = label(t, "f-old"), renamed = label(t, "f-new");
        collectionWithRing(t, old, 1, 2);
        rename(t, old, renamed);
        AtomicBoolean held = new AtomicBoolean();
        var reader = new PciQueryRingReader(scope, held::get);

        assertThat(reader.load(t, old)).hasSize(2);
        held.set(true);
        assertThat(reader.load(t, old)).as("the next load reads the id with no restart").isEmpty();
        assertThat(reader.load(t, renamed)).hasSize(2);
        held.set(false);
        assertThat(reader.load(t, old)).hasSize(2);
    }
}
