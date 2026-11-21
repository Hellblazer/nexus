// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.Chash;
import org.jooq.DSLContext;
import org.jooq.Record;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.QUARANTINE_RESTORE_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.REAPER_EXPIRE_CLIENT_QUARANTINE;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 P2.6 (nexus-p2m1x.11): {@code nexus.quarantine_restore_chunks} and {@code nexus.reaper_expire_client_quarantine}
 * (vectors-063) judge a collection by its catalog row's {@code lifecycle_state}, not by whether its name starts with
 * {@code quarantine-}. Each test names the row's state and the name's shape independently, so a function that read the
 * name would fail the cell where the two disagree.
 *
 * <p>The functions are called directly as the schema owner (SECURITY INVOKER, tenant passed as an argument): this pins
 * the SQL, not the Java guards in front of it, which are RDR-229 P2.5.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class QuarantineStateFromRowFunctionsIntegrationTest {

    private static final String TENANT = "t-qstate-row";
    private static final String CUTOFF = "2026-06-01T00:00:00Z";
    private static final String MODEL = "bge-base-en-v15-768";

    private PostgreSQLContainer<?> pg;
    private final AtomicInteger seq = new AtomicInteger();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
    }

    @AfterAll
    void stopAll() {
        if (pg != null) pg.stop();
    }

    private <T> T su(java.util.function.Function<DSLContext, T> work) throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        }
    }

    /** Registers {@code name} (as the seeding helper does) and then sets its row's state and content type. */
    private void register(String name, String state) throws Exception {
        su(d -> {
            PgContainerHelper.insertCollection(d, TENANT, name, MODEL);
            d.update(CATALOG_COLLECTIONS)
                .set(CATALOG_COLLECTIONS.LIFECYCLE_STATE, state)
                .set(CATALOG_COLLECTIONS.CONTENT_TYPE, "knowledge")
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT).and(CATALOG_COLLECTIONS.NAME.eq(name)))
                .execute();
            return null;
        });
    }

    private String chunk(String collection, String seed, Map<String, Object> metadata) throws Exception {
        String hex = Chash.ofText(collection + "/" + seed).toHex();
        su(d -> {
            PgContainerHelper.insertChunks(d, TENANT, collection, List.of(hex), List.of(seed),
                List.of(new float[768]), List.of(metadata));
            return null;
        });
        return hex;
    }

    private boolean present(String collection, String hex) throws Exception {
        return su(d -> d.fetchExists(CHUNKS, CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(collection))
            .and(CHUNKS.CHASH.eq(Chash.fromHex(hex).toBytes()))));
    }

    private String label(String prefix) {
        return prefix + seq.incrementAndGet();
    }

    private String liveName() {
        return "knowledge__qs" + seq.incrementAndGet() + "__" + MODEL + "__v1";
    }

    // ── quarantine_restore_chunks ────────────────────────────────────────────

    private org.jooq.Result<? extends Record> restore(String origin, String quarantine, String hex, boolean dryRun) throws Exception {
        return su(d -> d.selectFrom(QUARANTINE_RESTORE_CHUNKS.call(
                TENANT, origin, quarantine, new String[] {hex}, "test", null, dryRun, false))
            .fetch());
    }

    @Test
    void a_prefixed_origin_name_whose_row_is_live_is_a_live_origin() throws Exception {
        String origin = label("quarantine-liveorigin-");
        String sibling = liveName();
        register(origin, "live");
        register(sibling, "quarantine");
        String hex = chunk(sibling, "restore me", Map.of("origin_collection", origin));

        var dry = restore(origin, sibling, hex, true);

        assertThat(dry).as("the origin's row is live: the name's prefix does not refuse it").hasSize(1);
        assertThat(dry.get(0).get(QUARANTINE_RESTORE_CHUNKS.R_OUTCOME)).isEqualTo("would_restore");

        var real = restore(origin, sibling, hex, false);

        assertThat(real.get(0).get(QUARANTINE_RESTORE_CHUNKS.R_OUTCOME)).isEqualTo("restored");
        assertThat(present(origin, hex)).isTrue();
        assertThat(present(sibling, hex)).isFalse();
    }

    @Test
    void a_quarantine_collection_whose_row_says_quarantine_needs_no_prefix() throws Exception {
        String origin = liveName();
        String sibling = liveName();
        register(origin, "live");
        register(sibling, "quarantine");
        String hex = chunk(sibling, "restore me too", Map.of("origin_collection", origin));

        var real = restore(origin, sibling, hex, false);

        assertThat(real.get(0).get(QUARANTINE_RESTORE_CHUNKS.R_OUTCOME)).isEqualTo("restored");
        assertThat(present(origin, hex)).isTrue();
    }

    @Test
    void an_origin_whose_row_is_quarantine_is_refused_whatever_its_name() throws Exception {
        String origin = liveName();
        String sibling = liveName();
        register(origin, "quarantine");
        register(sibling, "quarantine");
        String hex = chunk(sibling, "stay put", Map.of("origin_collection", origin));

        assertThatThrownBy(() -> restore(origin, sibling, hex, true))
            .isInstanceOf(DataAccessException.class)
            .hasMessageContaining("is not a registered live collection");
        assertThat(present(sibling, hex)).isTrue();
    }

    @Test
    void a_prefixed_name_whose_row_is_live_is_not_a_quarantine_collection() throws Exception {
        String origin = liveName();
        String sibling = label("quarantine-notquarantine-");
        register(origin, "live");
        register(sibling, "live");
        String hex = chunk(sibling, "not quarantined", Map.of("origin_collection", origin));

        assertThatThrownBy(() -> restore(origin, sibling, hex, true))
            .isInstanceOf(DataAccessException.class)
            .hasMessageContaining("is not a registered quarantine collection");
    }

    // ── reaper_expire_client_quarantine ──────────────────────────────────────

    private long[] expire(String sibling, String origin) throws Exception {
        Record r = su(d -> d.selectFrom(REAPER_EXPIRE_CLIENT_QUARANTINE.call(TENANT, sibling, origin, CUTOFF, 100))
            .fetchOne());
        return new long[] {r.get(REAPER_EXPIRE_CLIENT_QUARANTINE.EXPIRED),
            r.get(REAPER_EXPIRE_CLIENT_QUARANTINE.PROTECTED_COUNT)};
    }

    private Map<String, Object> clientMoved(String origin) {
        return Map.of("quarantined_at", "2026-01-01T00:00:00Z", "origin_collection", origin);
    }

    @Test
    void a_client_moved_row_in_a_sibling_whose_row_says_quarantine_expires_with_no_prefix_on_its_name() throws Exception {
        String origin = liveName();
        String sibling = liveName();
        register(origin, "live");
        register(sibling, "quarantine");
        String hex = chunk(sibling, "old client move", clientMoved(origin));

        long[] out = expire(sibling, origin);

        assertThat(out[0]).as("the sibling's row is quarantine and knowledge: its aged row expires").isEqualTo(1L);
        assertThat(present(sibling, hex)).isFalse();
    }

    @Test
    void a_prefixed_name_whose_row_is_live_is_outside_the_expiry_scope() throws Exception {
        String origin = liveName();
        String sibling = label("quarantine-liverow-");
        register(origin, "live");
        register(sibling, "live");
        String hex = chunk(sibling, "old but live", clientMoved(origin));

        long[] out = expire(sibling, origin);

        assertThat(out).as("the row says live: nothing is expired or counted").containsExactly(0L, 0L);
        assertThat(present(sibling, hex)).isTrue();
    }

    @Test
    void an_origin_whose_row_is_not_live_keeps_the_scope_closed() throws Exception {
        String origin = liveName();
        String sibling = liveName();
        register(origin, "quarantine");
        register(sibling, "quarantine");
        String hex = chunk(sibling, "origin not live", clientMoved(origin));

        assertThat(expire(sibling, origin)).containsExactly(0L, 0L);
        assertThat(present(sibling, hex)).isTrue();
    }
}
