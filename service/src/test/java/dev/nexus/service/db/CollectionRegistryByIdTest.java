// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.datamove.Gate3;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.concurrent.atomic.AtomicBoolean;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 P3.5a write half (nexus-p2m1x.23): {@link CollectionRegistry} keyed by id with a label index.
 *
 * <p>RDR line 2662 (P3.5a): "{@code CollectionRegistry} keyed by id with a label index"; item 6: a label "is still accepted at
 * the route boundary and resolved to the id once, by {@code CollectionRegistry}, which is keyed by id with a label index
 * beside it"; line ~1448: "Until gate 3, {@code CollectionRegistry} hands readers the name, ... From gate 3 ... readers are
 * on the id". The bead's own test list (nexus-p2m1x.23 notes, Tests 1): "The registry resolves a label and an id to the same
 * row. A rename's evict drops both index entries. The evict race still holds for both keys."
 *
 * <p><b>Missing production symbols</b> (the tests need exactly these): the registry takes {@link Gate3} by constructor (linda's
 * ruling, bead comment 2026-11-22), which makes it an instance; the existing static {@code cached/evict/require} keep their
 * names on the static class today and cannot share a signature with an instance method, so the instance API here uses new
 * names: {@code resolveLabel(ctx, tenant, label)}, {@code resolveId(ctx, tenant, id)}, {@code cachedByLabel},
 * {@code cachedById}, {@code evictLabel(tenant, label)}, {@code evictTenant}, {@code readerKey(row)}; {@link CollectionRow}
 * gains {@code collectionId()} and {@code label()}; an id with no row raises {@code UnknownCollectionIdException}. The
 * nexus-hkh1y seam {@code afterMissReadForTests} keeps its static name here; the implementation may make it an instance
 * field and this test follows.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionRegistryByIdTest {

    private static final String TENANT = "cr-byid-tenant";
    private static final String OTHER_TENANT = "cr-byid-other";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope scope;
    CatalogRepository catalog;
    int n;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername("nexus_svc");
        cfg.setPassword("nexus_svc_pass");
        cfg.setMaximumPoolSize(2);
        cfg.setConnectionTimeout(15000);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        scope = new TenantScope(svcDs);
        catalog = new CatalogRepository(scope);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private String seeded(String tenant, String stem) throws Exception {
        String label = "docs__" + stem + "-" + (n++) + "__minilm-l6-v2-384__v1";
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), tenant, label);
        }
        return label;
    }

    private long idOf(String tenant, String label) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(label))
                .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
        }
    }

    private CollectionRow byLabel(CollectionRegistry reg, String tenant, String label) {
        return scope.withTenant(tenant, ctx -> reg.resolveLabel(ctx, tenant, label));
    }

    private CollectionRow byId(CollectionRegistry reg, String tenant, long id) {
        return scope.withTenant(tenant, ctx -> reg.resolveId(ctx, tenant, id));
    }

    // ---- the two keys name one row ------------------------------------------------------------------------------------

    /** Bead test list 1, clause 1: "The registry resolves a label and an id to the same row." */
    @Test
    void aLabelAndAnIdResolveToTheSameRow() throws Exception {
        for (boolean gate : new boolean[] {false, true}) {
            var reg = new CollectionRegistry(() -> gate);
            String label = seeded(TENANT, "same" + gate);
            long id = idOf(TENANT, label);

            CollectionRow viaLabel = byLabel(reg, TENANT, label);
            CollectionRow viaId = byId(new CollectionRegistry(() -> gate), TENANT, id);

            assertThat(viaLabel.collectionId()).as("gate3=%s", gate).isEqualTo(id);
            assertThat(viaLabel.label()).isEqualTo(label);
            assertThat(viaId).as("the same row by the other key").isEqualTo(viaLabel);
        }
    }

    /** One resolution by either key fills both index entries (RDR: "keyed by id with a label index beside it"). */
    @Test
    void oneResolutionFillsTheIdEntryAndTheLabelEntry() throws Exception {
        var reg = new CollectionRegistry(Gate3.never());
        String label = seeded(TENANT, "fill");
        long id = idOf(TENANT, label);
        assertThat(reg.cachedById(TENANT, id)).isEmpty();
        assertThat(reg.cachedByLabel(TENANT, label)).isEmpty();

        byId(reg, TENANT, id);

        assertThat(reg.cachedById(TENANT, id)).as("filled by the id lookup").isPresent();
        assertThat(reg.cachedByLabel(TENANT, label)).as("and the label index with it").isPresent();
        assertThat(reg.cachedByLabel(TENANT, label)).isEqualTo(reg.cachedById(TENANT, id));
    }

    /**
     * A hit never reads the database: after the row's {@code lifecycle_state} changes under the cache, both keys still answer the
     * cached row (the contract {@code CollectionRegistryTest#require_cachesFullRow_andHitAvoidsCatalogRead} pins for the label).
     */
    @Test
    void aHitOnEitherKeyDoesNotReadTheDatabase() throws Exception {
        var reg = new CollectionRegistry(Gate3.never());
        String label = seeded(TENANT, "hit");
        long id = idOf(TENANT, label);
        CollectionRow first = byLabel(reg, TENANT, label);
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_COLLECTIONS)
                .set(CATALOG_COLLECTIONS.LIFECYCLE_STATE, "dormant")
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(label)).execute();
        }
        assertThat(first.lifecycleState()).as("setup").isEqualTo("live");
        assertThat(byLabel(reg, TENANT, label).lifecycleState()).as("label hit").isEqualTo("live");
        assertThat(byId(reg, TENANT, id).lifecycleState()).as("id hit").isEqualTo("live");
    }

    /** The id is per tenant: another tenant's registry rows are not reachable by an id they do not own. */
    @Test
    void anIdOfAnotherTenantDoesNotResolve() throws Exception {
        var reg = new CollectionRegistry(Gate3.never());
        String label = seeded(TENANT, "tenant");
        long id = idOf(TENANT, label);
        seeded(OTHER_TENANT, "tenant");

        assertThatThrownBy(() -> byId(reg, OTHER_TENANT, id)).isInstanceOf(UnknownCollectionIdException.class);
    }

    /** Typed refusal for an id with no row (the registry-level twin of the routes' typed 404, contract point 3). */
    @Test
    void anUnknownIdRaisesTheTypedException_notAnUnregisteredLabel() {
        var reg = new CollectionRegistry(Gate3.never());
        assertThatThrownBy(() -> byId(reg, TENANT, 987_654_321_012L))
            .isInstanceOf(UnknownCollectionIdException.class)
            .isNotInstanceOf(UnregisteredCollectionException.class)
            .hasMessageContaining("987654321012");
    }

    // ---- invalidation -------------------------------------------------------------------------------------------------

    /**
     * Bead test list 1, clause 2: "A rename's evict drops both index entries." The caller of an evict knows the label (a
     * rename or delete has just removed the row, so the id is no longer readable): evicting by label drops the id entry too.
     */
    @Test
    void evictingByLabelDropsBothIndexEntries() throws Exception {
        var reg = new CollectionRegistry(Gate3.never());
        String label = seeded(TENANT, "evict");
        long id = idOf(TENANT, label);
        byLabel(reg, TENANT, label);

        reg.evictLabel(TENANT, label);

        assertThat(reg.cachedByLabel(TENANT, label)).isEmpty();
        assertThat(reg.cachedById(TENANT, id)).as("the id entry went with the label's").isEmpty();
    }

    /** {@code evictTenant} (a profile write, the tenant reset route) clears both indexes for that tenant only. */
    @Test
    void evictingATenantDropsBothIndexesForThatTenantOnly() throws Exception {
        var reg = new CollectionRegistry(Gate3.never());
        String mine = seeded(TENANT, "evt-mine");
        String theirs = seeded(OTHER_TENANT, "evt-theirs");
        long mineId = idOf(TENANT, mine);
        long theirsId = idOf(OTHER_TENANT, theirs);
        byLabel(reg, TENANT, mine);
        byLabel(reg, OTHER_TENANT, theirs);

        int removed = reg.evictTenant(TENANT);

        assertThat(removed).as("one row, however many index entries it has").isEqualTo(1);
        assertThat(reg.cachedById(TENANT, mineId)).isEmpty();
        assertThat(reg.cachedByLabel(TENANT, mine)).isEmpty();
        assertThat(reg.cachedById(OTHER_TENANT, theirsId)).as("the other tenant's entry stays").isPresent();
        assertThat(reg.cachedByLabel(OTHER_TENANT, theirs)).isPresent();
    }

    /**
     * Bead test list 1, clause 3: "The evict race still holds for both keys." nexus-hkh1y: a miss reads the row, an evict
     * lands, the fill must not put the row back. A miss by id, and a miss by label, each followed by an evict between the read
     * and the fill, cache under neither key.
     */
    @Test
    void theEvictRaceGuardHoldsForBothKeys() throws Exception {
        var reg = new CollectionRegistry(Gate3.never());
        String viaId = seeded(TENANT, "race-id");
        long id = idOf(TENANT, viaId);
        CollectionRegistry.afterMissReadForTests = () -> reg.evictLabel(TENANT, viaId);
        try {
            byId(reg, TENANT, id);
        } finally {
            CollectionRegistry.afterMissReadForTests = null;
        }
        assertThat(reg.cachedById(TENANT, id)).as("a fill that raced an evict, by id: dropped").isEmpty();
        assertThat(reg.cachedByLabel(TENANT, viaId)).as("and not through the label index either").isEmpty();

        String viaLabel = seeded(TENANT, "race-label");
        long id2 = idOf(TENANT, viaLabel);
        CollectionRegistry.afterMissReadForTests = () -> reg.evictLabel(TENANT, viaLabel);
        try {
            byLabel(reg, TENANT, viaLabel);
        } finally {
            CollectionRegistry.afterMissReadForTests = null;
        }
        assertThat(reg.cachedByLabel(TENANT, viaLabel)).as("a fill that raced an evict, by label: dropped").isEmpty();
        assertThat(reg.cachedById(TENANT, id2)).as("and not through the id key either").isEmpty();

        // control: with no evict in flight the same miss caches under both
        String control = seeded(TENANT, "race-control");
        byLabel(reg, TENANT, control);
        assertThat(reg.cachedById(TENANT, idOf(TENANT, control))).isPresent();
    }

    /**
     * nexus-mp8ys carried to the second key: "a superseded row is never cached". A retired row resolves (a read hands the
     * tombstone back with its successor) and is cached under neither key.
     */
    @Test
    void aSupersededRowIsNeverCachedUnderEitherKey() throws Exception {
        var reg = new CollectionRegistry(Gate3.never());
        String old = seeded(TENANT, "sup-old");
        String successor = seeded(TENANT, "sup-new");
        long oldId = idOf(TENANT, old);
        catalog.supersedeCollection(TENANT, old, successor, "");

        CollectionRow viaLabel = byLabel(reg, TENANT, old);
        CollectionRow viaId = byId(reg, TENANT, oldId);

        assertThat(viaLabel.isSuperseded()).isTrue();
        assertThat(viaLabel.supersededBy()).isEqualTo(successor);
        assertThat(viaId).isEqualTo(viaLabel);
        assertThat(reg.cachedByLabel(TENANT, old)).isEmpty();
        assertThat(reg.cachedById(TENANT, oldId)).isEmpty();
    }

    /**
     * RDR line 2662: "until then a rename gives the collection a new id". After a name-path rename the old id is the
     * tombstone and the new label has its own id, so a client holding the old id reaches the retired row, never the new one.
     */
    @Test
    void afterANamePathRename_theOldIdIsTheTombstone_andTheNewLabelHasANewId() throws Exception {
        String from = seeded(TENANT, "ren-from");
        String to = "docs__ren-to-" + (n++) + "__minilm-l6-v2-384__v1";
        long oldId = idOf(TENANT, from);
        catalog.renameCollection(TENANT, from, to);

        var reg = new CollectionRegistry(Gate3.never());
        CollectionRow tomb = byId(reg, TENANT, oldId);
        CollectionRow renamed = byLabel(reg, TENANT, to);

        assertThat(tomb.isSuperseded()).as("the old id reaches the tombstone").isTrue();
        assertThat(tomb.supersededBy()).isEqualTo(to);
        assertThat(renamed.collectionId()).as("the new label's own id").isNotEqualTo(oldId);
        assertThat(renamed.isSuperseded()).isFalse();
    }

    // ---- what readers are handed --------------------------------------------------------------------------------------

    /**
     * RDR line ~1448: "Until gate 3, {@code CollectionRegistry} hands readers the name, the jOOQ readers read the name
     * columns"; from gate 3 "readers are on the id". {@code readerKey} is the one value a reader keys its query on: the
     * label while gate 3 does not hold, the id once it does, decided per call from the injected {@link Gate3} so the switch
     * needs no restart, and dropping back when gate 3 does.
     */
    @Test
    void readersAreHandedTheNameUntilGate3_andTheIdFromIt_withNoRestart() throws Exception {
        var gate = new AtomicBoolean(false);
        var reg = new CollectionRegistry(gate::get);
        String label = seeded(TENANT, "reader");
        long id = idOf(TENANT, label);
        CollectionRow row = byLabel(reg, TENANT, label);

        assertThat(reg.readerKey(row)).as("gate 3 does not hold").isEqualTo(label);
        gate.set(true);
        assertThat(reg.readerKey(row)).as("gate 3 holds: the same cached row, no restart").isEqualTo(id);
        gate.set(false);
        assertThat(reg.readerKey(row)).as("gate 3 dropped back (an id index went INVALID)").isEqualTo(label);

        var never = new CollectionRegistry(Gate3.never());
        assertThat(never.readerKey(row)).isEqualTo(label);
        var always = new CollectionRegistry(() -> true);
        assertThat(always.readerKey(row)).isEqualTo(id);
    }
}
