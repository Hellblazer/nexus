// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.VecPciRegistryNames;
import dev.nexus.service.datamove.Gate3;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.time.Duration;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25): {@link PciLeafCounting#count} reads the collection's rows by {@code collection_id} from gate 3, so a
 * registry row renamed in place (the chunk rows keep their old label until P4.1) is counted under its NEW label, and by the name before it.
 * The counting transaction is the reconciler's own (the admin role, the tenant bound by {@link PciLeafCounting#begin}).
 */
@Tag("integration")
class PciLeafCountingByIdIntegrationTest extends PciIdIndexSupport {

    private static final int ROWS = 7;
    private static final int CAP = 100;
    private static final Gate3 HELD = () -> true;

    @BeforeAll
    void dropTheRegistryNameKeys() throws Exception {
        asSuperuser(su -> {
            assertThat(VecPciRegistryNames.dropRegistryNameKeys(su)).as("non-vacuity: the registry's name keys exist and are dropped")
                .isNotEmpty();
            return null;
        });
    }

    /** Runs {@code work} in a counting transaction of the tenant's leaf, as the reconciler opens one. */
    private <T> T counting(String tenant, Function<DSLContext, T> work) throws Exception {
        PciCatalog.Leaf leaf = leaf(tenant);
        try (Connection c = adminDs.getConnection()) {
            c.setAutoCommit(false);
            DSLContext tx = DSL.using(c, SQLDialect.POSTGRES);
            assertThat(PciLeafCounting.begin(tx, leaf, Duration.ofSeconds(30), PciReconciler.SET_LOCAL_TENANT)).isTrue();
            try {
                return work.apply(tx);
            } finally {
                c.rollback();
            }
        }
    }

    private int count(String tenant, String collection, Gate3 gate3) throws Exception {
        Integer n = counting(tenant, tx -> PciLeafCounting.count(tx, 384, collection, M384, tenant, CAP, gate3));
        return n;
    }

    /** The collection renamed in place: the id is kept, the chunk rows keep the old label. */
    private void rename(String tenant, String from, String to) throws Exception {
        int renamed = asSuperuser(su -> su.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, to)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(from)).execute());
        assertThat(renamed).isEqualTo(1);
        int carrying = asSuperuser(su -> su.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(to))));
        assertThat(carrying)
            .as("precondition: no chunk row carries the new label").isZero();
    }

    @Test
    void aRenamedCollection_isCountedByItsNewLabelFromGate3_andByTheOldOneBefore() throws Exception {
        String tenant = newTenant("cnt-r");
        collection(tenant, name("cnt-r-old"), ROWS);
        rename(tenant, name("cnt-r-old"), name("cnt-r-new"));

        assertThat(count(tenant, name("cnt-r-new"), HELD)).as("gate 3: the id of the new label").isEqualTo(ROWS);
        assertThat(count(tenant, name("cnt-r-old"), HELD)).as("gate 3: the old label is no collection").isZero();

        assertThat(count(tenant, name("cnt-r-old"), Gate3.never())).as("before gate 3: the rows' own label, as today").isEqualTo(ROWS);
        assertThat(count(tenant, name("cnt-r-new"), Gate3.never())).isZero();
    }

    @Test
    void theCapStillStopsTheCountOnTheIdPath() throws Exception {
        String tenant = newTenant("cnt-c");
        collection(tenant, name("cnt-c"), ROWS);
        int byId = counting(tenant, tx -> PciLeafCounting.count(tx, 384, name("cnt-c"), M384, tenant, 3, HELD));
        int byName = counting(tenant, tx -> PciLeafCounting.count(tx, 384, name("cnt-c"), M384, tenant, 3, Gate3.never()));
        assertThat(byId).as("limit + 1 rows, as the name path stops").isEqualTo(4);
        assertThat(byName).isEqualTo(4);
    }

    @Test
    void aNullIdIsNoCollection_aLabelThatResolvesToNoIdCountsNothing_andNeverFallsBackToTheName() throws Exception {
        String tenant = newTenant("cnt-n");
        collection(tenant, name("cnt-n"), ROWS);
        unstampOneRow(tenant, name("cnt-n"));   // one row with a NULL id beside its name

        assertThat(count(tenant, name("cnt-n"), HELD)).as("the NULL-id row is not in the collection by id").isEqualTo(ROWS - 1);
        assertThat(count(tenant, name("cnt-n"), Gate3.never())).as("by name it is").isEqualTo(ROWS);
        assertThat(count(tenant, "", HELD)).as("an empty label resolves to no id").isZero();
        assertThat(count(tenant, name("cnt-n-nothing"), HELD)).as("an unregistered label resolves to no id").isZero();
        // a row carrying a '' name with a NULL id is no collection on either path's id arm
        asSuperuser(su -> su.update(CHUNKS).set(CHUNKS.COLLECTION, "")
            .where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION_ID.isNull()).execute());
        assertThat(count(tenant, "", HELD)).as("the '' row is not found by the id arm: no fall back to the '' name").isZero();
    }

    @Test
    void gate3IsReadOnEveryCall_notCached() throws Exception {
        String tenant = newTenant("cnt-f");
        collection(tenant, name("cnt-f-old"), ROWS);
        rename(tenant, name("cnt-f-old"), name("cnt-f-new"));
        AtomicBoolean held = new AtomicBoolean();
        Gate3 gate = held::get;

        assertThat(count(tenant, name("cnt-f-old"), gate)).isEqualTo(ROWS);
        held.set(true);
        assertThat(count(tenant, name("cnt-f-old"), gate)).as("the next call reads the id with no restart").isZero();
        assertThat(count(tenant, name("cnt-f-new"), gate)).isEqualTo(ROWS);
        held.set(false);
        assertThat(count(tenant, name("cnt-f-old"), gate)).as("and back to the name when gate 3 drops").isEqualTo(ROWS);
    }
}
