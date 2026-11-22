// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.BootWorkRegistry;
import dev.nexus.service.IdKeyIndexProbe;
import dev.nexus.service.IdKeyIndexProbe.Idx;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.CollectionIds;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneId;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.3b (nexus-p2m1x.20): the fixtures of the per-leaf unique-index pass's integration tests ({@link LeafIndexPassIntegrationTest},
 * {@link LeafIndexPassResumeIntegrationTest}, {@link LeafIndexPassBackoffIntegrationTest}), split out of one class that had reached its size
 * cap. Each subclass starts its own container: a database migrated by the non-superuser owner (production's {@code nexus_admin}), the pass
 * connecting as the owner, over the real two-level partitioned layout in the shape {@link PciReconcilerIntegrationTest} uses.
 *
 * <p><b>Hermetic.</b> Every test starts and ends with no id unique index anywhere in the two trees ({@link IdKeyIndexProbe#reset}): a
 * constraint left on a parent would make PostgreSQL itself index each later leaf.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
abstract class LeafIndexPassTestBase {
    static final String ADMIN_ROLE = PgContainerHelper.TEMPLATE_OWNER;
    static final String ADMIN_PASS = PgContainerHelper.TEMPLATE_OWNER_PASSWORD;
    static final String M384 = "minilm-l6-v2-384";
    static final String NONCE = "p33b0001";
    static final String ENGINE_VERSION = "9.9.9-p33b-test";
    static final String CHUNKS_SWAP = LeafIndexPass.swapLabel("chunks");
    static final String CENTROIDS_SWAP = LeafIndexPass.swapLabel("taxonomy_centroids");
    static final org.jooq.Table<?> MOVES = DSL.table(DSL.name("nexus", "data_moves"));
    static final org.jooq.Field<String> LABEL = DSL.field(DSL.name("label"), String.class);

    PostgreSQLContainer<?> pg;
    HikariDataSource adminDs;
    HikariDataSource svcDs;
    AdminConnection admin;
    final List<String> tenants = new ArrayList<>();
    int generation;

    @BeforeAll
    void startAll() {
        pg = PgContainerHelper.startMigratedByOwner();
        var adminCfg = new HikariConfig();
        adminCfg.setJdbcUrl(pg.getJdbcUrl());
        adminCfg.setUsername(ADMIN_ROLE);
        adminCfg.setPassword(ADMIN_PASS);
        adminCfg.setMaximumPoolSize(2);
        adminDs = new HikariDataSource(adminCfg);
        var svcCfg = new HikariConfig();
        svcCfg.setJdbcUrl(pg.getJdbcUrl());
        svcCfg.setUsername(PgContainerHelper.SVC_USERNAME);
        svcCfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        svcCfg.setMaximumPoolSize(2);
        svcCfg.setAutoCommit(true);
        svcDs = new HikariDataSource(svcCfg);
        admin = new AdminConnection(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (adminDs != null) adminDs.close();
        if (pg != null) pg.stop();
    }

    @BeforeEach
    void startClean() throws Exception {
        clean();
    }

    @AfterEach
    @SuppressWarnings("deprecation")
    void endClean() throws Exception {
        try (Connection owner = adminDs.getConnection()) {
            owner.setAutoCommit(true);
            DSLContext ctx = DSL.using(owner, SQLDialect.POSTGRES);
            for (String tenant : tenants) {
                dev.nexus.service.jooq.nexus.Routines.dropTenantPartitions(ctx.configuration(), tenant);
            }
        }
        tenants.clear();
        clean();
    }

    void clean() throws Exception {
        asSuperuser(su -> {
            su.deleteFrom(MOVES).where(LABEL.like("rdr229-swap-%")).execute();
            IdKeyIndexProbe.reset(su);
            return null;
        });
    }

    // -- fixtures --------------------------------------------------------------------------------------------------

    <T> T asSuperuser(java.util.function.Function<DSLContext, T> work) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            return work.apply(DSL.using(su, SQLDialect.POSTGRES));
        }
    }

    <T> T asOwner(java.util.function.Function<DSLContext, T> work) throws Exception {
        try (Connection owner = adminDs.getConnection()) {
            owner.setAutoCommit(true);
            return work.apply(DSL.using(owner, SQLDialect.POSTGRES));
        }
    }

    LeafIndexPass pass(LeafIndexPass.Gate1 gate) {
        return new LeafIndexPass(admin, new BootWorkRegistry(Clock.systemUTC()), gate, ENGINE_VERSION);
    }

    LeafIndexPass openPass() {
        return pass(() -> true);
    }

    /** A tenant with leaves under every model partition of both tables, created the way production's token trigger does (as the owner). */
    @SuppressWarnings("deprecation")
    String newTenant(String tag) throws Exception {
        return newTenant(tag, true);
    }

    /** As above; {@code withoutHelperIndexes} false keeps what the helper made, for a leaf born under a table that already has its constraint. */
    @SuppressWarnings("deprecation")
    String newTenant(String tag, boolean withoutHelperIndexes) throws Exception {
        String tenant = "p33b-" + tag;
        tenants.add(tenant);
        asOwner(owner -> {
            dev.nexus.service.jooq.nexus.Routines.createTenantPartitions(owner.configuration(), "nexus.chunks", tenant, true, true);
            dev.nexus.service.jooq.nexus.Routines.createTenantPartitions(owner.configuration(), "nexus.taxonomy_centroids", tenant, true, true);
            return null;
        });
        // A leaf made through the helper after the cut carries a unique id index of its own (vectors-073; tested in
        // LeafIdUniqueIndexPartitionFunctionIntegrationTest). The pass is for the leaves that existed before the cut, which have none,
        // so the fixture takes the helper's away: every test starts from leaves with no id index.
        if (withoutHelperIndexes) {
            asSuperuser(su -> {
                IdKeyIndexProbe.reset(su);
                return null;
            });
            assertThat(everyIdIndex()).as("the fixture's leaves start with no id index").isEmpty();
        }
        return tenant;
    }

    static String name(String tenant, String tag) {
        return "knowledge__" + tenant + "-" + tag + "__minilm-l6-v2-384__v1";
    }

    static Vector vector(int i) {
        float[] v = new float[384];
        v[i % 384] = 1f;
        v[(i * 7 + 1) % 384] += 0.5f;
        return Vector.of(v);
    }

    static byte[] chash(String tenant, String seed, int i) {
        return Chash.fromHex(Chash.ofText(tenant + "/" + seed + "/" + i).toHex()).toBytes();
    }

    static void chunk(DSLContext su, String tenant, String collection, org.jooq.Field<Long> collectionId, byte[] chash,
                      String text, Vector embedding) {
        su.insertInto(CHUNKS)
            .set(CHUNKS.TENANT_ID, tenant)
            .set(CHUNKS.COLLECTION, collection)
            .set(CHUNKS.COLLECTION_ID, collectionId)
            .set(CHUNKS.CHASH, chash)
            .set(CHUNKS.EMBEDDING_MODEL, M384)
            .set(CHUNKS.CHUNK_TEXT, text)
            .set(CHUNKS.EMBEDDING_384, embedding)
            .set(CHUNKS.METADATA, JSONB.jsonb("{}"))
            .execute();
    }

    /** A registered collection with {@code rows} chunks and one centroid, every row carrying its collection's id (the stamp's result). */
    void collection(String tenant, String collection, int rows) throws Exception {
        asSuperuser(su -> {
            PgContainerHelper.insertCollection(su, tenant, collection, M384);
            for (int i = 0; i < rows; i++) {
                chunk(su, tenant, collection, CollectionIds.idOf(tenant, collection), chash(tenant, "c", i), "row " + i, vector(i));
            }
            su.insertInto(TAXONOMY_CENTROIDS)
                .set(TAXONOMY_CENTROIDS.TENANT_ID, tenant)
                .set(TAXONOMY_CENTROIDS.COLLECTION, collection)
                .set(TAXONOMY_CENTROIDS.COLLECTION_ID, CollectionIds.idOf(tenant, collection))
                .set(TAXONOMY_CENTROIDS.TOPIC_ID, 1L)
                .set(TAXONOMY_CENTROIDS.LABEL, "label")
                .set(TAXONOMY_CENTROIDS.EMBEDDING_MODEL, M384)
                .set(TAXONOMY_CENTROIDS.EMBEDDING_384, vector(1))
                .execute();
            return null;
        });
    }

    /**
     * A second collection whose row repeats the first collection's chash AND its collection_id: distinct under the name key
     * (the collection name differs), a duplicate under the id key. A unique index build on the leaf cannot finish while it is
     * there (23505), which leaves the index INVALID, the state a pass ended mid-build leaves.
     */
    void duplicateUnderTheIdKey(String tenant, String firstCollection, String secondCollection) throws Exception {
        asSuperuser(su -> {
            PgContainerHelper.insertCollection(su, tenant, secondCollection, M384);
            chunk(su, tenant, secondCollection, CollectionIds.idOf(tenant, firstCollection), chash(tenant, "c", 0), "duplicate", vector(0));
            return null;
        });
    }

    void removeDuplicate(String tenant, String secondCollection) throws Exception {
        asSuperuser(su -> su.deleteFrom(CHUNKS).where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION.eq(secondCollection)).execute());
    }

    static String hash(String value, int hexChars) {
        try {
            byte[] d = java.security.MessageDigest.getInstance("SHA-256").digest(value.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            return java.util.HexFormat.of().formatHex(d).substring(0, hexChars);
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    /** The tenant's chunks leaf under the {@value M384} model partition, as {@code regclass} text. */
    String chunksLeaf(String tenant) throws Exception {
        List<String> found = asSuperuser(su -> IdKeyIndexProbe.leaves(su, IdKeyIndexProbe.CHUNKS)).stream()
            .filter(l -> l.contains("_m" + hash(M384, 8) + "_t_" + hash(tenant, 16))).toList();
        assertThat(found).as("the tenant's leaf of %s", M384).hasSize(1);
        return found.get(0);
    }

    List<Map<String, Object>> swapRows() throws Exception {
        return asSuperuser(su -> su.select(LABEL, DSL.field(DSL.name("driving_schema")), DSL.field(DSL.name("driving_table")),
                DSL.field(DSL.name("template")), DSL.field(DSL.name("verification")), DSL.field(DSL.name("on_complete")),
                DSL.field(DSL.name("changeset_id")), DSL.field(DSL.name("completed_at")), DSL.field(DSL.name("registered_at")))
            .from(MOVES).where(LABEL.like("rdr229-swap-%")).orderBy(LABEL).fetch().intoMaps());
    }

    List<String> chunksLeavesWithoutIndex() throws Exception {
        return asSuperuser(su -> IdKeyIndexProbe.leavesWithoutValidIndex(su, IdKeyIndexProbe.CHUNKS));
    }

    List<Idx> everyIdIndex() throws Exception {
        return asSuperuser(su -> IdKeyIndexProbe.KEYS.stream().flatMap(k -> IdKeyIndexProbe.indexes(su, k).stream()).toList());
    }

    int stampMoves(boolean completedOnly) throws Exception {
        return asSuperuser(su -> su.fetchCount(DSL.table(DSL.name("nexus", "data_moves")),
            completedOnly ? DSL.field(DSL.name("label")).like("rdr229-stamp-%").and(DSL.field(DSL.name("completed_at")).isNotNull())
                : DSL.field(DSL.name("label")).like("rdr229-stamp-%")));
    }

    List<String> swapLabels() throws Exception {
        return swapRows().stream().map(r -> (String) r.get("label")).toList();
    }


    /** True when the chain of every key is whole: a valid constraint-backed index at every level, attached to its parent's. */
    boolean whole(LeafIndexPass.Key... keys) throws Exception {
        return asSuperuser(su -> java.util.Arrays.stream(keys).allMatch(k -> LeafIndexPass.chainComplete(su, k)));
    }

    List<Idx> parentIndexes(IdKeyIndexProbe.Key key) throws Exception {
        return asSuperuser(su -> IdKeyIndexProbe.indexes(su, key)).stream().filter(i -> i.level() == 0).toList();
    }


    /** Raised by a test's {@code beforeDdl} hook to end a pass at a chosen statement: the state before that statement is what the test seeds. */
    static final class StopHere extends RuntimeException {
        StopHere() {
            super("stop here", null, false, false);
        }
    }

    /** Holds an advisory lock on a session of its own until closed: another engine's pass, or the migration walk. */
    final class HeldLock implements AutoCloseable {
        final Connection conn;

        HeldLock(long key) throws Exception {
            conn = pg.createConnection("");
            conn.setAutoCommit(true);
            Boolean got = DSL.using(conn, SQLDialect.POSTGRES)
                .select(DSL.function("pg_try_advisory_lock", org.jooq.impl.SQLDataType.BOOLEAN, DSL.val(key))).fetchOne(0, Boolean.class);
            assertThat(got).as("the test's session took lock %s", key).isTrue();
        }

        @Override
        public void close() throws Exception {
            conn.close();   // the session ends and the lock goes with it
        }
    }


    /** A settable clock: the backoff reads it, and the test moves it. */
    static final class MutableClock extends Clock {
        Instant now = Instant.parse("2026-11-22T10:00:00Z");

        @Override
        public ZoneId getZone() {
            return ZoneId.of("UTC");
        }

        @Override
        public Clock withZone(ZoneId zone) {
            return this;
        }

        @Override
        public Instant instant() {
            return now;
        }
    }

}
