// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_OWNERS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-f3yqd: {@code GET /v1/catalog/owners/next_seq_drift?limit=N}, the read-only form of the next_seq sweep: the
 * envelope, the bounds, tenant scope, that it writes nothing, and that it reports exactly what the sweep then floors. The
 * rule against the client's, case by case, is {@link NextSeqDriftParityTest}'s.
 */
class NextSeqDriftReadRouteTest extends IndexRunCensusTestBase {

    /** An owner whose counter is {@code nextSeq}, with children at each of {@code children} (live). */
    private void owner(String tenant, String prefix, long nextSeq, int... children) throws Exception {
        repo.upsertOwner(tenant, Map.of("tumbler_prefix", prefix, "name", "owner " + prefix, "owner_type", "repo"));
        for (int c : children) insert(tenant, Doc.of(prefix + "." + c));
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_OWNERS).set(CATALOG_OWNERS.NEXT_SEQ, nextSeq)
                .where(CATALOG_OWNERS.TENANT_ID.eq(tenant).and(CATALOG_OWNERS.TUMBLER_PREFIX.eq(prefix))).execute();
        }
    }

    private long nextSeq(String prefix) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).select(CATALOG_OWNERS.NEXT_SEQ).from(CATALOG_OWNERS)
                .where(CATALOG_OWNERS.TENANT_ID.eq(TENANT).and(CATALOG_OWNERS.TUMBLER_PREFIX.eq(prefix))).fetchOne(0, Long.class);
        }
    }

    @Test
    void theAnswerHasTheEnvelope_andAnEmptyTenantReportsNothing() throws Exception {
        var body = drift("");
        assertThat(body.keySet()).containsExactlyInAnyOrder("checked", "drifted", "limit", "capped", "examples");
        assertThat(body).containsEntry("checked", 0).containsEntry("drifted", 0).containsEntry("limit", 1000).containsEntry("capped", false);
        assertThat(list(body, "examples")).isEmpty();
    }

    @Test
    void aDriftedOwnerIsNamedWithItsCounterAndItsHighestChild_aHealthyOneBesideItIsNot() throws Exception {
        owner(TENANT, "7.1", 2, 1, 5);     // drifted: counter 2, highest child 5
        owner(TENANT, "7.2", 3, 1, 3);     // control: equality is healthy
        var body = drift("");
        assertThat(body).containsEntry("checked", 2).containsEntry("drifted", 1);
        var examples = list(body, "examples");
        assertThat(examples).hasSize(1);
        assertThat(examples.get(0)).containsEntry("owner", "7.1").containsEntry("next_seq", 2).containsEntry("highest_child_seq", 5);
        assertThat(examples.get(0).keySet()).containsExactlyInAnyOrder("owner", "next_seq", "highest_child_seq");
    }

    @Test
    void theListStopsAtTheLimit_theCountDoesNot_andCappedSaysSo() throws Exception {
        for (int i = 1; i <= 3; i++) owner(TENANT, "7.3" + i, 0, 4);
        var two = drift("?limit=2");
        assertThat(two).containsEntry("drifted", 3).containsEntry("capped", true).containsEntry("limit", 2);
        assertThat(list(two, "examples")).extracting(e -> (String) e.get("owner")).containsExactly("7.31", "7.32");
        var three = drift("?limit=3");
        assertThat(three).containsEntry("drifted", 3).containsEntry("capped", false);
        assertThat(list(three, "examples")).hasSize(3);
        assertThat(drift("?limit=4")).containsEntry("capped", false);
    }

    @Test
    void anotherTenantsOwnersAreInvisible_andSoAreOurs() throws Exception {
        owner(TENANT, "7.4", 0, 2);
        owner(TENANT_B, "7.4", 0, 2);       // the very same prefix
        owner(TENANT_B, "7.5", 0, 9);
        assertThat(drift("")).containsEntry("checked", 1).containsEntry("drifted", 1);
        var resp = get(TOKEN_B, TENANT_B, "/v1/catalog/owners/next_seq_drift");
        assertThat(mapper.readValue(resp.body(), MAP_T)).containsEntry("checked", 2).containsEntry("drifted", 2);
    }

    @Test
    void aBadLimitIsA400_andOnlyGetIsAllowed() throws Exception {
        for (String bad : List.of("0", "-1", "10001", "abc", "1.5")) {
            assertThat(get(TOKEN, TENANT, "/v1/catalog/owners/next_seq_drift?limit=" + bad).statusCode()).as("limit=" + bad).isEqualTo(400);
        }
        assertThat(post(TOKEN, TENANT, "/v1/catalog/owners/next_seq_drift", "{}").statusCode()).isEqualTo(405);
    }

    /** The read floors nothing; the sweep that follows floors exactly the owners the read named, to exactly the mark it named. */
    @Test
    void theReadWritesNothing_andTheSweepThenFloorsExactlyWhatItNamed() throws Exception {
        owner(TENANT, "7.6", 1, 1, 8);
        owner(TENANT, "7.7", 4, 1, 4);
        owner(TENANT, "7.8", 0, 3);
        var before = drift("");
        assertThat(nextSeq("7.6")).isEqualTo(1);
        assertThat(drift("")).isEqualTo(before);           // reading twice changes nothing
        assertThat(nextSeq("7.8")).isEqualTo(0);

        var resp = post(TOKEN, TENANT, "/v1/catalog/owners/sweep_next_seq_drift", "{}");
        assertThat(resp.statusCode()).isEqualTo(200);
        var swept = mapper.readValue(resp.body(), MAP_T);
        var named = list(before, "examples");
        var healed = list(swept, "owners");
        assertThat(healed).extracting(h -> h.get("tumbler_prefix")).containsExactlyInAnyOrderElementsOf(named.stream().map(n -> n.get("owner")).toList());
        for (var h : healed) {
            var n = named.stream().filter(x -> x.get("owner").equals(h.get("tumbler_prefix"))).findFirst().orElseThrow();
            assertThat(h.get("next_seq")).isEqualTo(n.get("next_seq"));
            assertThat(h.get("high_water")).isEqualTo(n.get("highest_child_seq"));
        }
        assertThat(swept).containsEntry("checked", before.get("checked")).containsEntry("healed", before.get("drifted"));
        assertThat(drift("")).containsEntry("drifted", 0);
    }

    /**
     * The call deadline between owners (one probe per owner): the clock the deadline reads is spent after the first owner, so
     * the second owner's probe is refused with the cancelled error (SQLSTATE 57014), which the route answers as the 503 the
     * statement-timeout test above shows. No partial list leaves the read.
     */
    @Test
    void theDeadlineBetweenOwnersIsTheCancelledError_andNoPartialAnswerLeaves() throws Exception {
        owner(TENANT, "7.10", 0, 2);
        owner(TENANT, "7.11", 0, 2);
        var reads = new java.util.concurrent.atomic.AtomicInteger();
        // read 1 sets the deadline, read 2 is the first owner's check; from the third on the clock is a minute later
        java.util.function.LongSupplier clock = () -> reads.incrementAndGet() <= 2 ? 0L : 60_000_000_000L;
        var reads2 = new dev.nexus.service.db.NextSeqDriftReads(repo, clock);
        assertThatThrownBy(() -> reads2.read(TENANT, 10)).isInstanceOf(org.jooq.exception.DataAccessException.class)
            .satisfies(e -> assertThat(((java.sql.SQLException) e.getCause()).getSQLState()).isEqualTo(CatalogRepository.QUERY_CANCELED));
        // control: the same read with a clock that never moves lists both owners
        var whole = new dev.nexus.service.db.NextSeqDriftReads(repo, () -> 0L).read(TENANT, 10);
        assertThat(whole.drifted()).isEqualTo(2);
    }

    @Test
    void aReadThatOutlastsItsBoundAnswers503WithNoPartialBody_andTheNextReadAnswers() throws Exception {
        owner(TENANT, "7.9", 0, 2);
        System.setProperty(CatalogRepository.FILLABLE_COUNT_TIMEOUT_PROPERTY, "300");
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            PgContainerHelper.lockTable(holder, CATALOG_OWNERS, PgContainerHelper.LockMode.ACCESS_EXCLUSIVE);
            var resp = get(TOKEN, TENANT, "/v1/catalog/owners/next_seq_drift");
            assertThat(resp.statusCode()).as(resp.body()).isEqualTo(503);
            var body = mapper.readValue(resp.body(), MAP_T);
            assertThat(body).containsEntry("reason", "statement_timeout");
            assertThat(body).doesNotContainKeys("checked", "drifted", "examples");
            holder.rollback();
        } finally {
            System.clearProperty(CatalogRepository.FILLABLE_COUNT_TIMEOUT_PROPERTY);
        }
        assertThat(drift("")).containsEntry("drifted", 1);
    }
}
