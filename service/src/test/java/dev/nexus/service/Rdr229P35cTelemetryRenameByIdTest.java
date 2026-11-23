// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_TELEMETRY;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.db.RenameGuard;
import dev.nexus.service.db.TelemetryRepository;
import org.jooq.Record2;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25), linda 2026-11-23: {@code TelemetryRepository#renameCollection} retags a row that carries an id by that
 * id, never by its stored name, so a stamped row that carries the old name under ANOTHER collection's id is not re-pointed; a row with no
 * id (telemetry is a history table) is retagged by name, and takes the renamed registry row's id only before gate 3. The new id is the
 * renamed registry row's own, never {@code idOf(newName)}.
 */
class Rdr229P35cTelemetryRenameByIdTest extends AtomicWriteTestBase {

    private final AtomicBoolean latched = new AtomicBoolean();

    private String ts(int i) {
        return String.format("2026-01-%02dT00:00:00Z", i);
    }

    private Long idOf(String name) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(name))
                .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
        }
    }

    private Record2<String, Long> row(String queryHash) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).select(SEARCH_TELEMETRY.COLLECTION, SEARCH_TELEMETRY.COLLECTION_ID)
                .from(SEARCH_TELEMETRY).where(SEARCH_TELEMETRY.TENANT_ID.eq(TENANT)).and(SEARCH_TELEMETRY.QUERY_HASH.eq(queryHash))
                .fetchOne();
        }
    }

    /** Three rows under {@code a}: stamped with a's id, stamped with b's id but named a, and with no id. Returns their query hashes. */
    private String[] seed(TelemetryRepository tel, String a, String b, int day) throws Exception {
        String own = "own-" + seq.incrementAndGet(), foreign = "foreign-" + seq.incrementAndGet(), none = "none-" + seq.incrementAndGet();
        tel.importSearchRow(TENANT, ts(day), own, a, 1, 1, 0.5, 0.9);
        tel.importSearchRow(TENANT, ts(day + 1), foreign, b, 1, 1, 0.5, 0.9);
        tel.importSearchRow(TENANT, ts(day + 2), none, a, 1, 1, 0.5, 0.9);
        try (Connection su = pg.createConnection("")) {
            var d = DSL.using(su, SQLDialect.POSTGRES);
            d.update(SEARCH_TELEMETRY).set(SEARCH_TELEMETRY.COLLECTION, a)
                .where(SEARCH_TELEMETRY.TENANT_ID.eq(TENANT)).and(SEARCH_TELEMETRY.QUERY_HASH.eq(foreign)).execute();
            d.update(SEARCH_TELEMETRY).setNull(SEARCH_TELEMETRY.COLLECTION_ID)
                .where(SEARCH_TELEMETRY.TENANT_ID.eq(TENANT)).and(SEARCH_TELEMETRY.QUERY_HASH.eq(none)).execute();
        }
        assertThat(row(own).value2()).as("fixture: the own row carries a's id").isEqualTo(idOf(a));
        assertThat(row(foreign).value1()).as("fixture: named a").isEqualTo(a);
        assertThat(row(foreign).value2()).as("fixture: carrying b's id").isEqualTo(idOf(b));
        assertThat(row(none).value2()).as("fixture: no id").isNull();
        return new String[] {own, foreign, none};
    }

    @Test
    void aStampedRowNamingTheOldNameUnderAnotherCollectionsId_isNotRepointed_beforeAndFromGate3() throws Exception {
        TelemetryRepository tel = new TelemetryRepository(tenantScope, latched::get, RenameGuard.open());
        for (boolean gate : new boolean[] {false, true}) {
            latched.set(gate);
            String a = "code__p35ctl" + seq.incrementAndGet() + "a__minilm-l6-v2-384__v1";
            String b = "code__p35ctl" + seq.incrementAndGet() + "b__minilm-l6-v2-384__v1";
            String renamed = "code__p35ctl" + seq.incrementAndGet() + "n__minilm-l6-v2-384__v1";
            registerCollection(a);
            registerCollection(b);
            Long idA = idOf(a), idB = idOf(b);
            assertThat(idA).isNotEqualTo(idB);
            String[] h = seed(tel, a, b, gate ? 10 : 1);

            var counts = tel.renameCollection(TENANT, a, renamed);

            assertThat(row(h[0])).as("gate " + gate + ": the row of a's id is retagged by id")
                .satisfies(r -> { assertThat(r.value1()).isEqualTo(renamed); assertThat(r.value2()).isEqualTo(idA); });
            assertThat(row(h[1])).as("gate " + gate + ": a row naming a under b's id is NOT re-pointed: it is b's row")
                .satisfies(r -> { assertThat(r.value1()).isEqualTo(a); assertThat(r.value2()).isEqualTo(idB); });
            assertThat(row(h[2]).value1()).as("gate " + gate + ": a row with no id is retagged by name").isEqualTo(renamed);
            assertThat(row(h[2]).value2()).as(gate ? "from gate 3 a pre-id row is never stamped" : "before gate 3 it takes the renamed row's id")
                .isEqualTo(gate ? null : idA);
            assertThat(counts.get("search_telemetry")).as("two rows retagged").isEqualTo(2);
        }
    }
}
