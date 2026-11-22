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
import dev.nexus.service.db.PciBuilders;
import dev.nexus.service.db.SchemaMigrator;
import dev.nexus.service.db.PgSession.PciSettings;
import dev.nexus.service.datamove.CompletionCheck;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.vectors.PciBuilderSession.BuilderState;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.Timeout;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneId;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.stream.Collectors;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 P3.3b (nexus-p2m1x.20), the failing tests of the per-leaf unique-index pass ({@link LeafIndexPass}), against the real
 * two-level partitioned layout in the shape {@link PciReconcilerIntegrationTest} uses: a database migrated by the non-superuser
 * owner (production's {@code nexus_admin}), the pass connecting as the owner. The pass builds the id unique indexes of
 * {@code nexus.chunks} and {@code nexus.taxonomy_centroids} and, last, registers each table's swap move
 * ({@link LeafIndexPass#swapLabel}).
 *
 * <p><b>Gate 1.</b> Every test but {@link #withMovesBlockedThePassBuildsNoIndexAndRegistersNoSwap} opens the gate with a lambda
 * that returns true: the gate is the completion check's ({@code CompletionCheckIntegrationTest} and its siblings test it) and
 * this class tests what the pass does when it holds. The blocked test reads the REAL gate over a store whose stamp moves are
 * registered and cannot run.
 *
 * <p><b>The key columns.</b> P3.3a (nexus-p2m1x.19) is not on this branch. The id key's columns are not among what it adds
 * ({@code collection_id} is on both tables since P3.1, {@code vectors-068}; P3.3a adds the seven foreign-key twins and their
 * validation moves), so no fixture here adds a column. What the pass waits for from P3.3a is its labels in the gate's read
 * set, which a lambda stands in for.
 *
 * <p><b>Hermetic.</b> Every test starts and ends with no id unique index anywhere in the two trees
 * ({@link IdKeyIndexProbe#reset}): a constraint left on a parent would make PostgreSQL itself index each later leaf.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@Timeout(value = 300, unit = TimeUnit.SECONDS, threadMode = Timeout.ThreadMode.SEPARATE_THREAD)
class LeafIndexPassIntegrationTest {

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

    private void clean() throws Exception {
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

    // -- 1 ---------------------------------------------------------------------------------------------------------

    /**
     * A pass ended mid-build leaves an INVALID leaf index; the next pass drops it and rebuilds it (RDR-229 P3.3b test; T2
     * nexus_rdr/229-research-33, the pattern of {@code PciBootWork.ON_STOP}). The leftover is an index named as the pass names its
     * leaf index, marked invalid the way {@code PgContainerHelper.markIndexInvalid} does for the other interrupted-build tests (the
     * test tree holds no raw SQL, and {@code CREATE INDEX CONCURRENTLY} has no typed form; the failing concurrent build itself is
     * what test 2 does through the pass). What the next pass must do is the drop and the rebuild, not build beside it: one unique id
     * index on the leaf, valid, and not the leftover's oid.
     */
    @Test
    void aPassEndedMidBuildLeavesAnInvalidIndexAndTheNextPassDropsAndRebuildsIt() throws Exception {
        String tenant = newTenant("midbuild");
        collection(tenant, name(tenant, "a"), 3);
        String leaf = chunksLeaf(tenant);
        String schema = leaf.substring(0, leaf.indexOf('.'));
        String relation = leaf.substring(leaf.indexOf('.') + 1);
        String leftover = LeafIndexPass.leafIndexName(relation);
        asSuperuser(su -> {
            var cols = java.util.Arrays.stream(IdKeyIndexProbe.CHUNKS.columns().split(", ")).map(c -> DSL.field(DSL.name(c))).toList();
            su.createUniqueIndex(DSL.name(leftover)).on(DSL.table(DSL.name(schema, relation)), cols).execute();
            return null;
        });
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.markIndexInvalid(su, schema, leftover);   // what a build ended mid-way leaves behind
        }
        List<Idx> before = asSuperuser(su -> IdKeyIndexProbe.indexes(su, IdKeyIndexProbe.CHUNKS)).stream()
            .filter(i -> i.relation().equals(leaf)).toList();
        assertThat(before).as("precondition: one INVALID index on the leaf, under the pass's name").hasSize(1);
        assertThat(before.get(0).valid()).isFalse();
        long leftoverOid = before.get(0).indexOid();

        var result = openPass().runOnce();

        List<Idx> after = asSuperuser(su -> IdKeyIndexProbe.indexes(su, IdKeyIndexProbe.CHUNKS)).stream()
            .filter(i -> i.relation().equals(leaf)).toList();
        assertThat(result.dropped()).as("the pass dropped the leftover").isEqualTo(1);
        assertThat(after).as("one unique id index on the leaf: the leftover dropped, not built beside").hasSize(1);
        assertThat(after.get(0).valid()).as("and it is valid").isTrue();
        assertThat(after.get(0).indexOid()).as("and it is a rebuild, not the leftover").isNotEqualTo(leftoverOid);
        assertThat(asSuperuser(su -> IdKeyIndexProbe.indexes(su, IdKeyIndexProbe.CHUNKS)).stream().filter(i -> !i.valid()).toList())
            .as("no INVALID id index is left anywhere in the tree").isEmpty();
    }

    // -- 2 ---------------------------------------------------------------------------------------------------------

    /**
     * The swap is never registered while any leaf's unique index is missing or INVALID. One chunks leaf cannot be indexed (a
     * duplicate under the id key), so the chunks table has an INVALID leaf and no swap row, however many passes run; the
     * centroids, whose leaves all index, do get theirs (so the test is not a pass that registers nothing at all). Removing the
     * duplicate lets the next pass finish the chunks and register it. The swap is per table: a table's swap waits for its own
     * leaves only.
     */
    @Test
    void theSwapIsNeverRegisteredWhileAnyLeafHasNoValidUniqueIndex() throws Exception {
        String tenant = newTenant("noswap");
        collection(tenant, name(tenant, "a"), 3);
        duplicateUnderTheIdKey(tenant, name(tenant, "a"), name(tenant, "dup"));
        String leaf = chunksLeaf(tenant);

        var first = openPass().runOnce();
        var second = openPass().runOnce();

        assertThat(chunksLeavesWithoutIndex())
            .as("the duplicate keeps exactly its own leaf without a valid index; every other chunks leaf has one")
            .containsExactly(leaf);
        assertThat(swapLabels()).as("chunks: no swap while a leaf has no valid index (two passes); centroids: registered")
            .containsExactly(CENTROIDS_SWAP);
        assertThat(first.registered()).containsExactly(CENTROIDS_SWAP);
        assertThat(second.registered()).as("and the second pass registers nothing more").isEmpty();

        removeDuplicate(tenant, name(tenant, "dup"));
        var third = openPass().runOnce();

        assertThat(chunksLeavesWithoutIndex()).isEmpty();
        assertThat(third.registered()).as("the pass that finishes the last leaf registers the chunks swap").containsExactly(CHUNKS_SWAP);
        assertThat(swapLabels()).containsExactly(CHUNKS_SWAP, CENTROIDS_SWAP);
    }

    // -- 2b --------------------------------------------------------------------------------------------------------

    /**
     * Linda's seam ruling 2026-11-22, item 1: the pass adds a table's PARENT id constraint only once every leaf of that table has
     * a valid index. One chunks leaf cannot be indexed, so the chunks parent has no id constraint (and no index of any kind at the
     * parent) after the pass, while the centroids, whose leaves all index, have theirs: the second assertion is what shows the pass
     * ran and made the parent constraint for the table that was ready. Once the duplicate is gone the next pass adds the chunks one.
     */
    @Test
    void theParentConstraintIsNotAddedWhileAnyLeafOfThatTableLacksAValidIndex() throws Exception {
        String tenant = newTenant("noparent");
        collection(tenant, name(tenant, "a"), 3);
        duplicateUnderTheIdKey(tenant, name(tenant, "a"), name(tenant, "dup"));

        openPass().runOnce();

        List<Idx> centroidsParent = parentIndexes(IdKeyIndexProbe.CENTROIDS);
        assertThat(centroidsParent).as("centroids: every leaf valid, so the parent constraint is there").hasSize(1);
        assertThat(centroidsParent.get(0).constraint()).isNotNull();
        assertThat(parentIndexes(IdKeyIndexProbe.CHUNKS)).as("chunks: a leaf has no valid index, so no parent constraint and no parent index")
            .isEmpty();

        removeDuplicate(tenant, name(tenant, "dup"));
        openPass().runOnce();

        List<Idx> chunksParent = parentIndexes(IdKeyIndexProbe.CHUNKS);
        assertThat(chunksParent).as("chunks: the pass that finds every leaf valid adds the parent constraint").hasSize(1);
        assertThat(chunksParent.get(0).constraint()).isNotNull();
        assertThat(chunksParent.get(0).valid()).isTrue();
    }

    /** True when the chain of every key is whole: a valid constraint-backed index at every level, attached to its parent's. */
    boolean whole(LeafIndexPass.Key... keys) throws Exception {
        return asSuperuser(su -> java.util.Arrays.stream(keys).allMatch(k -> LeafIndexPass.chainComplete(su, k)));
    }

    List<Idx> parentIndexes(IdKeyIndexProbe.Key key) throws Exception {
        return asSuperuser(su -> IdKeyIndexProbe.indexes(su, key)).stream().filter(i -> i.level() == 0).toList();
    }

    // -- 3 ---------------------------------------------------------------------------------------------------------

    /**
     * Gate 1 governs (residual S-5, nexus-p2m1x.43): on an install whose moves are blocked (BYO or --no-pg-bundle PostgreSQL with
     * no pg_background) the stamp never completes, so the pass starts nothing: no index, no swap. This store is such an install:
     * the migration registered the stamp moves and nothing can run them, so the REAL gate does not hold. A pass on that gate
     * builds no index and registers no swap. The same pass on an open gate then builds and registers on the same store, which is
     * what makes the first half mean "the gate held it back" and not "the pass cannot build here".
     */
    @Test
    void withMovesBlockedThePassBuildsNoIndexAndRegistersNoSwap() throws Exception {
        String tenant = newTenant("blocked");
        collection(tenant, name(tenant, "a"), 3);
        assertThat(stampMoves(false)).as("precondition: the stamp moves are registered").isPositive();
        assertThat(stampMoves(true)).as("precondition: no stamp move can run here, so none completed").isZero();
    LeafIndexPass.Gate1 real = () -> {
            try {
                return asOwner(ctx -> CompletionCheck.evaluate(ctx).gate1());
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        };
        Boolean realGate = real.holds();
        assertThat(realGate).as("precondition: the real gate 1 does not hold").isFalse();

        var blocked = pass(real).runOnce();

        assertThat(blocked.gateHeld()).isFalse();
        assertThat(blocked.built()).isZero();
        assertThat(everyIdIndex())
            .as("no index of any kind on either table's tree").isEmpty();
        assertThat(swapLabels()).as("no swap registered").isEmpty();

        var open = openPass().runOnce();   // control: the same store, the gate open

        assertThat(open.gateHeld()).isTrue();
        assertThat(chunksLeavesWithoutIndex())
            .as("with the gate open the pass builds on this very store").isEmpty();
        assertThat(swapLabels()).containsExactly(CHUNKS_SWAP, CENTROIDS_SWAP);
    }

    // -- 5 ---------------------------------------------------------------------------------------------------------

    /**
     * Registration is idempotent: no second insert when the swap label row exists. Two passes leave one row each and the second
     * reports no registration. A row that is already there (here a hand row with its own changeset_id, as a restored or
     * hand-registered store could carry) is neither inserted over nor replaced, and the pass does not fail on it.
     */
    @Test
    void registrationIsIdempotentAndNeverInsertsASecondRowForALabel() throws Exception {
        String tenant = newTenant("idem");
        collection(tenant, name(tenant, "a"), 3);

        var first = openPass().runOnce();
        var registeredAt = swapRows().stream().collect(Collectors.toMap(r -> (String) r.get("label"), r -> r.get("registered_at")));
        var second = openPass().runOnce();

        assertThat(first.registered()).containsExactlyInAnyOrder(CHUNKS_SWAP, CENTROIDS_SWAP);
        assertThat(second.registered()).as("nothing to register the second time").isEmpty();
        var rows = swapRows();
        assertThat(rows).as("one row per label").hasSize(2);
        assertThat(rows.stream().collect(Collectors.toMap(r -> (String) r.get("label"), r -> r.get("registered_at"))))
            .as("the rows are the first pass's, untouched").isEqualTo(registeredAt);

        asSuperuser(su -> su.deleteFrom(MOVES).where(LABEL.eq(CHUNKS_SWAP)).execute());
        asSuperuser(su -> su.insertInto(MOVES, LABEL, DSL.field(DSL.name("template")), DSL.field(DSL.name("driving_schema")),
                DSL.field(DSL.name("driving_table")), DSL.field(DSL.name("chunk_size")), DSL.field(DSL.name("sleep_ms")),
                DSL.field(DSL.name("statement_timeout_ms")), DSL.field(DSL.name("verification")), DSL.field(DSL.name("changeset_id")))
            .values(CHUNKS_SWAP, "hand", "nexus", "chunks", 1, 0, 1000, "hand", "hand-registered").execute());
        var third = openPass().runOnce();

        assertThat(third.registered()).as("the label has a row: no insert").isEmpty();
        assertThat(swapRows().stream().filter(r -> CHUNKS_SWAP.equals(r.get("label"))).map(r -> r.get("changeset_id")).toList())
            .as("the existing row is not replaced").containsExactly("hand-registered");
    }

    // -- 6 ---------------------------------------------------------------------------------------------------------

    /**
     * With NX_SEARCH_PCI=0 the pass builds the indexes on every leaf and both partition levels and registers the swap (S-3 points
     * 1 and 2, nexus-p2m1x.41). The reconciler is built with PCI off, as NX_SEARCH_PCI=0 makes it, and does nothing; the pass,
     * which takes no PciSettings, does the S-3 route on two tenants: per leaf a valid unique index backing a UNIQUE constraint,
     * attached to its model partition's, which is attached to the parent's, each level valid. The layout must have two model
     * partitions at least, or the model level is not exercised. The registered rows name the table, carry the run-time
     * changeset_id {@value LeafIndexPass#CHANGESET_PREFIX}{@code <engine version>} (S-3 point 3, rule 2: the engine inserts, there
     * is no on_complete text) and a template the data-move registry accepts ({@code <driving_table>} and
     * {@code <chunking_clause>} once each, assert_chunking_template.sql).
     */
    @Test
    void withPciOffThePassBuildsEveryLeafAndBothPartitionLevelsAndRegistersTheSwap() throws Exception {
        String one = newTenant("pcioff-1");
        String two = newTenant("pcioff-2");
        collection(one, name(one, "a"), 3);
        collection(two, name(two, "a"), 3);
        PciSettings off = new PciSettings(false, 200, 600, 16);
        var reconciler = new PciReconciler(new PciCatalog(svcDs),
            new PciBuilderSession(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS, NONCE, off), PciIndexSweep.create(svcDs, off), off,
            Clock.systemUTC(), PciReconciler.SET_LOCAL_TENANT, PciReconciler.COUNT_TIMEOUT, Duration.ofSeconds(60));
        var pciReport = reconciler.reconcileOnce();
        assertThat(pciReport.state()).as("precondition: PCI is off").isEqualTo(BuilderState.OFF);
        assertThat(pciReport.builds()).isZero();

        var result = openPass().runOnce();

        assertThat(result.gateHeld()).isTrue();
        for (var key : IdKeyIndexProbe.KEYS) {
            List<Idx> all = asSuperuser(su -> IdKeyIndexProbe.indexes(su, key));
            List<String> leaves = asSuperuser(su -> IdKeyIndexProbe.leaves(su, key));
            List<String> models = asSuperuser(su -> IdKeyIndexProbe.modelPartitions(su, key));
            assertThat(models).as("%s: two model partitions at least, so the model level is a level", key.table()).hasSizeGreaterThanOrEqualTo(2);

            List<Idx> top = all.stream().filter(i -> i.level() == 0).toList();
            List<Idx> mid = all.stream().filter(i -> i.level() == 1).toList();
            List<Idx> bottom = all.stream().filter(Idx::leaf).toList();
            assertThat(top).as("%s: the parent has its unique index", key.table()).hasSize(1);
            assertThat(mid.stream().map(Idx::relation).toList()).as("%s: every model partition has one", key.table())
                .containsExactlyInAnyOrderElementsOf(models);
            assertThat(bottom.stream().map(Idx::relation).toList()).as("%s: every leaf has one", key.table())
                .containsExactlyInAnyOrderElementsOf(leaves);
            assertThat(all).as("%s: every one valid", key.table()).allMatch(Idx::valid);
            assertThat(all).as("%s: every one backs a UNIQUE constraint (ADD ... USING INDEX at the leaf, ONLY above)", key.table())
                .allMatch(i -> i.constraint() != null);
            assertThat(mid).as("%s: each model index is attached to the parent's", key.table())
                .allMatch(i -> top.get(0).index().equals(i.parentIndex()));
            List<String> modelIndexes = mid.stream().map(Idx::index).toList();
            assertThat(bottom).as("%s: each leaf index is attached to a model partition's", key.table())
                .allMatch(i -> modelIndexes.contains(i.parentIndex()));
        }

        assertThat(result.registered()).containsExactlyInAnyOrder(CHUNKS_SWAP, CENTROIDS_SWAP);
        for (var row : swapRows()) {
            String table = ((String) row.get("label")).substring("rdr229-swap-".length());
            assertThat(row.get("driving_schema")).isEqualTo("nexus");
            assertThat(row.get("driving_table")).isEqualTo(table);
            assertThat(row.get("changeset_id")).as("the run-time changeset_id convention, rule 2")
                .isEqualTo(LeafIndexPass.CHANGESET_PREFIX + ENGINE_VERSION);
            assertThat(row.get("completed_at")).as("registered open, for the worker").isNull();
            String template = (String) row.get("template");
            assertThat(template.split("<driving_table>", -1)).as("<driving_table> exactly once").hasSize(2);
            assertThat(template.split("<chunking_clause>", -1)).as("<chunking_clause> exactly once").hasSize(2);
            assertThat((String) row.get("verification")).isNotBlank();
        }
    }

    // -- 7: visible to the migration walk ----------------------------------------------------------------------------

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

    /** The pass's backend carries the builders' {@code application_name} prefix, the one the walk's termination and the on_complete's match. */
    @Test
    void thePassNamesItsBackendLikeAPciBuilder_soTheWalkAndTheOnCompleteEndIt() {
        assertThat(LeafIndexPass.APPLICATION_NAME).startsWith(PciBuilderSession.APPLICATION_NAME_PREFIX);
    }

    /**
     * CR-S1: with the migration advisory lock held by another session, a pass sends no statement and builds nothing, whether the lock is
     * held when the pass starts or is taken between two of its statements (the hook takes it at the first statement the pass is about
     * to send); released, the next pass does the whole job.
     */
    @Test
    void withTheMigrationLockHeld_theRunSendsNoDdl_atTheStartAndBetweenStatements() throws Exception {
        String tenant = newTenant("migration");
        collection(tenant, name(tenant, "a"), 3);
        List<LeafIndexPass.Ddl> sent = new ArrayList<>();

        try (HeldLock migration = new HeldLock(SchemaMigrator.MIGRATION_ADVISORY_LOCK_KEY)) {
            var pass = openPass();
            pass.beforeDdl(sent::add);
            var result = pass.runOnce();
            assertThat(result.gateHeld()).as("a migration walks: the pass stands down").isFalse();
            assertThat(result.built()).isZero();
            assertThat(sent).as("no statement was even prepared to send").isEmpty();
            assertThat(everyIdIndex()).isEmpty();
            assertThat(swapLabels()).isEmpty();
        }

        HeldLock[] late = new HeldLock[1];
        try {
            var pass = openPass();
            pass.beforeDdl(statement -> {
                if (late[0] == null) {
                    try {
                        late[0] = new HeldLock(SchemaMigrator.MIGRATION_ADVISORY_LOCK_KEY);   // the walk starts after the pass did
                    } catch (Exception e) {
                        throw new IllegalStateException(e);
                    }
                }
                sent.add(statement);
            });
            var cut = pass.runOnce();
            assertThat(late[0]).as("the hook ran: the pass was about to send its first statement").isNotNull();
            assertThat(cut.built()).as("and the statement was not sent").isZero();
            assertThat(sent).as("the hook saw one statement and the pass ended there").hasSize(1);
            assertThat(everyIdIndex()).as("no index, valid or not").isEmpty();
            assertThat(swapLabels()).isEmpty();
        } finally {
            if (late[0] != null) {
                late[0].close();
            }
        }

        var after = openPass().runOnce();
        assertThat(after.gateHeld()).isTrue();
        assertThat(chunksLeavesWithoutIndex()).isEmpty();
        assertThat(swapLabels()).containsExactly(CHUNKS_SWAP, CENTROIDS_SWAP);
    }

    /** CR-S5: a second session holding the pass's own lock (another engine's pass) makes {@code runOnce} send nothing. */
    @Test
    void aSecondSessionHoldingThePassLockMakesRunOnceSendNothing() throws Exception {
        String tenant = newTenant("peer");
        collection(tenant, name(tenant, "a"), 3);
        List<LeafIndexPass.Ddl> sent = new ArrayList<>();
        try (HeldLock peer = new HeldLock(LeafIndexPass.LOCK_KEY)) {
            var pass = openPass();
            pass.beforeDdl(sent::add);
            var result = pass.runOnce();
            assertThat(result.gateHeld()).as("standby: not a pass").isFalse();
            assertThat(result.built() + result.dropped()).isZero();
            assertThat(result.registered()).isEmpty();
            assertThat(sent).isEmpty();
            assertThat(everyIdIndex()).isEmpty();
            assertThat(swapLabels()).isEmpty();
        }
        assertThat(openPass().runOnce().registered()).as("the peer gone, the pass runs").containsExactlyInAnyOrder(CHUNKS_SWAP, CENTROIDS_SWAP);
    }

    /**
     * Linda's requirement (b), and the walk's termination finding the pass: a build in flight is held on a lock of the test's, the pass's
     * backend is ended with the one call the migration walk and the on_complete make ({@code PciBuilders.terminate}; it finds the pass by
     * the builders' name prefix), the CREATE INDEX CONCURRENTLY dies leaving an INVALID index and the pass ends without throwing; the next
     * pass drops that index and rebuilds, and no INVALID index remains.
     */
    @Test
    void aBuildKilledByTheWalksTerminationLeavesAnInvalidIndexTheNextPassDropsAndRebuilds() throws Exception {
        String tenant = newTenant("killed");
        collection(tenant, name(tenant, "a"), 3);
        long leftoverOid;
        try (Connection blocker = pg.createConnection("")) {
            blocker.setAutoCommit(false);
            try {
                // a write in flight on the tenant's rows: its ROW EXCLUSIVE lock on every leaf of the tenant makes a CREATE INDEX
                // CONCURRENTLY there wait (after it has made its INVALID catalog entry) for the transaction to end
                DSL.using(blocker, SQLDialect.POSTGRES).update(CHUNKS).set(CHUNKS.CHUNK_TEXT, "held").where(CHUNKS.TENANT_ID.eq(tenant)).execute();
                CompletableFuture<LeafIndexPass.Result> running = CompletableFuture.supplyAsync(() -> openPass().runOnce());
                long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(120);
                List<Idx> invalid = List.of();
                while (invalid.isEmpty() && System.nanoTime() < deadline) {
                    Thread.sleep(100);
                    invalid = asSuperuser(su -> IdKeyIndexProbe.indexes(su, IdKeyIndexProbe.CHUNKS)).stream().filter(i -> !i.valid()).toList();
                }
                assertThat(invalid).as("a build is in flight: its index is INVALID and the pass waits on the test's lock").isNotEmpty();
                leftoverOid = invalid.get(0).indexOid();

                int ended = asSuperuser(su -> PciBuilders.terminate(su, "reason=test_walk", org.slf4j.LoggerFactory.getLogger("leaf-index-test")));
                assertThat(ended).as("the walk's termination found the pass's backend by its name").isEqualTo(1);
                running.get(60, TimeUnit.SECONDS);   // the pass ends; it does not throw into its caller
            } finally {
                blocker.rollback();
            }
        }
        List<Idx> killed = asSuperuser(su -> IdKeyIndexProbe.indexes(su, IdKeyIndexProbe.CHUNKS)).stream().filter(i -> !i.valid()).toList();
        assertThat(killed).as("the killed build left its INVALID index").isNotEmpty();
        assertThat(killed.stream().map(Idx::indexOid)).contains(leftoverOid);
        assertThat(swapLabels()).as("and the table is not registered").isEmpty();

        var next = openPass().runOnce();

        assertThat(next.dropped()).as("the next pass drops the leftover").isGreaterThanOrEqualTo(1);
        assertThat(asSuperuser(su -> IdKeyIndexProbe.indexes(su, IdKeyIndexProbe.CHUNKS)).stream().map(Idx::indexOid))
            .as("and rebuilds: the leftover's oid is gone").doesNotContain(leftoverOid);
        assertThat(asSuperuser(su -> IdKeyIndexProbe.indexes(su, IdKeyIndexProbe.CHUNKS)).stream().filter(i -> !i.valid()).toList())
            .as("no INVALID id index is left").isEmpty();
        assertThat(swapLabels()).containsExactly(CHUNKS_SWAP, CENTROIDS_SWAP);
    }

    // -- 8: boot_work, resume, a finished table ----------------------------------------------------------------------

    /** {@code boot_work}: the entry is present while the pass builds, its {@code done} counts the indexes built so far of a constant {@code total}, and it is gone after. */
    @Test
    void theBootWorkEntryCountsTheBuildsWhileRunning_andIsGoneAfter() throws Exception {
        String tenant = newTenant("bootwork");
        collection(tenant, name(tenant, "a"), 3);
        var registry = new BootWorkRegistry(Clock.systemUTC());
        var pass = new LeafIndexPass(admin, registry, () -> true, ENGINE_VERSION);
        List<String> seen = new ArrayList<>();
        pass.beforeDdl(statement -> {
            if (statement.kind() == LeafIndexPass.Kind.BUILD) {
                registry.snapshot().stream().filter(e -> LeafIndexPass.BOOT_WORK_ID.equals(e.id())).findFirst()
                    .ifPresentOrElse(e -> seen.add(e.done() + "/" + e.total()), () -> seen.add("absent"));
            }
        });
        assertThat(registry.snapshot()).as("none before").noneMatch(e -> LeafIndexPass.BOOT_WORK_ID.equals(e.id()));

        var result = pass.runOnce();

        int total = result.built();
        assertThat(total).as("every leaf of both tables had a build").isPositive();
        assertThat(seen).as("at the start of build i the entry says i of total")
            .containsExactlyElementsOf(java.util.stream.IntStream.range(0, total).mapToObj(i -> i + "/" + total).toList());
        assertThat(registry.snapshot()).as("gone once the pass ends").noneMatch(e -> LeafIndexPass.BOOT_WORK_ID.equals(e.id()));
    }

    /**
     * A pass cut at a chosen statement leaves the state before it, and the next pass resumes from there, completes the chain and registers
     * the swaps once. Three cuts: after the parent's ONLY constraint and before any model partition's; after a leaf's constraint and
     * before its attach; after every model partition's constraint and before the first model attach. In each state {@code chainComplete}
     * is false and no swap is registered (an unattached model index, a missing leaf constraint, a missing model constraint: no swap).
     */
    @Test
    void aPassCutAtEachStepOfTheRouteResumes_chainCompleteIsFalseUntilTheChainIsWhole_andTheSwapsRegisterOnce() throws Exception {
        String tenant = newTenant("resume");
        collection(tenant, name(tenant, "a"), 3);
        var chunks = LeafIndexPass.KEYS.get(0);
        var centroids = LeafIndexPass.KEYS.get(1);
        // the first statement that is not sent: the first model partition's constraint, the first leaf's attach, the first model's attach
        var stops = List.<java.util.function.Predicate<LeafIndexPass.Ddl>>of(
            st -> st.kind() == LeafIndexPass.Kind.PARENT_CONSTRAINT && st.target().startsWith("chunks_m"),
            st -> st.kind() == LeafIndexPass.Kind.ATTACH && st.target().startsWith("chunks_m"),
            st -> st.kind() == LeafIndexPass.Kind.ATTACH && st.target().equals("chunks_cid_uq"));
        for (int stop = 0; stop < stops.size(); stop++) {
            var at = stops.get(stop);
            int cutAt = stop;
            clean();
            var cut = openPass();
            cut.beforeDdl(st -> {
                if (at.test(st)) {
                    throw new StopHere();
                }
            });
            assertThatThrownBy(cut::runOnce).as("cut %s", cutAt).isInstanceOf(StopHere.class);
            assertThat(whole(chunks)).as("cut %s: the chain is not whole", cutAt).isFalse();
            assertThat(swapLabels()).as("cut %s: nothing registered", cutAt).isEmpty();

            var resumed = openPass().runOnce();

            assertThat(whole(chunks, centroids))
                .as("resumed from cut %s: both chains whole", cutAt).isTrue();
            assertThat(resumed.registered()).as("resumed from cut %s", cutAt).containsExactlyInAnyOrder(CHUNKS_SWAP, CENTROIDS_SWAP);
            assertThat(everyIdIndex()).as("resumed from cut %s: every index valid, and backing a constraint", cutAt)
                .allMatch(Idx::valid).allMatch(i -> i.constraint() != null);
            assertThat(swapRows()).as("one row per label").hasSize(2);
        }
    }

    /**
     * M3, the helper window: the parent has its id constraint and a model partition does not yet, so a leaf made now has no index (the
     * helper stands down for a constrained parent; the model has nothing to clone). The next pass builds that leaf's index and adopts it:
     * every leaf ends with one valid, constraint-backed, attached index, and the swap registers.
     */
    @Test
    void aLeafMadeWhileTheParentIsConstrainedAndTheModelIsNot_getsItsIndexFromTheNextPass() throws Exception {
        String old = newTenant("window-old");
        collection(old, name(old, "a"), 3);
        var cut = openPass();
        cut.beforeDdl(st -> {
            if (st.kind() == LeafIndexPass.Kind.PARENT_CONSTRAINT && st.target().startsWith("chunks_m")) {
                throw new StopHere();
            }
        });
        assertThatThrownBy(cut::runOnce).isInstanceOf(StopHere.class);
        assertThat(parentIndexes(IdKeyIndexProbe.CHUNKS)).as("the parent has its constraint").hasSize(1);

        String fresh = newTenant("window-new", false);
        collection(fresh, name(fresh, "a"), 2);
        String leaf = chunksLeaf(fresh);
        assertThat(chunksLeavesWithoutIndex()).as("the new leaf has no index: the window").contains(leaf);

        var result = openPass().runOnce();

        assertThat(chunksLeavesWithoutIndex()).isEmpty();
        assertThat(result.built()).as("the pass built the new leaf's index").isGreaterThanOrEqualTo(1);
        assertThat(asSuperuser(su -> IdKeyIndexProbe.indexes(su, IdKeyIndexProbe.CHUNKS)).stream().filter(i -> i.relation().equals(leaf)).toList())
            .as("exactly one index on the leaf: valid, constraint-backed, attached").hasSize(1)
            .allMatch(Idx::valid).allMatch(i -> i.constraint() != null).allMatch(i -> i.parentIndex() != null);
        assertThat(whole(LeafIndexPass.KEYS.get(0))).isTrue();
        assertThat(swapLabels()).containsExactly(CHUNKS_SWAP, CENTROIDS_SWAP);
    }

    /** A second pass over a finished store does nothing: no statement, nothing built or dropped or registered, and the same index oids. */
    @Test
    void aSecondPassOverAFinishedStoreSendsNothing() throws Exception {
        String tenant = newTenant("twice");
        collection(tenant, name(tenant, "a"), 3);
        var first = openPass().runOnce();
        assertThat(first.registered()).containsExactlyInAnyOrder(CHUNKS_SWAP, CENTROIDS_SWAP);
        List<Long> oids = everyIdIndex().stream().map(Idx::indexOid).sorted().toList();
        var registeredAt = swapRows().stream().collect(Collectors.toMap(r -> (String) r.get("label"), r -> r.get("registered_at")));
        List<LeafIndexPass.Ddl> sent = new ArrayList<>();
        var pass = openPass();
        pass.beforeDdl(sent::add);

        var second = pass.runOnce();

        assertThat(second.gateHeld()).isTrue();
        assertThat(second.built()).isZero();
        assertThat(second.dropped()).isZero();
        assertThat(second.registered()).isEmpty();
        assertThat(sent).as("no statement").isEmpty();
        assertThat(everyIdIndex().stream().map(Idx::indexOid).sorted().toList()).as("the same indexes, not rebuilt").isEqualTo(oids);
        assertThat(swapRows().stream().collect(Collectors.toMap(r -> (String) r.get("label"), r -> r.get("registered_at"))))
            .isEqualTo(registeredAt);
    }

    // -- 9: the leftover re-read, the backoff ------------------------------------------------------------------------

    /**
     * The leftover's {@code indisvalid} is read again immediately before the DROP (as {@code PciBuilderSession.Pass#drop} does): an index
     * that turned valid since the pass's first read (here, at the DROP statement) is not dropped, and the leaf is not built beside it.
     */
    @Test
    void aLeftoverThatTurnedValidBeforeTheDropIsNotDropped() throws Exception {
        String tenant = newTenant("stale");
        collection(tenant, name(tenant, "a"), 3);
        String leaf = chunksLeaf(tenant);
        String schema = leaf.substring(0, leaf.indexOf('.'));
        String relation = leaf.substring(leaf.indexOf('.') + 1);
        String leftover = LeafIndexPass.leafIndexName(relation);
        asSuperuser(su -> {
            var cols = java.util.Arrays.stream(IdKeyIndexProbe.CHUNKS.columns().split(", ")).map(c -> DSL.field(DSL.name(c))).toList();
            su.createUniqueIndex(DSL.name(leftover)).on(DSL.table(DSL.name(schema, relation)), cols).execute();
            return null;
        });
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.markIndexInvalid(su, schema, leftover);
        }
        long oid = asSuperuser(su -> IdKeyIndexProbe.indexes(su, IdKeyIndexProbe.CHUNKS)).stream()
            .filter(i -> i.relation().equals(leaf)).findFirst().orElseThrow().indexOid();
        List<LeafIndexPass.Ddl> drops = new ArrayList<>();
        var pass = openPass();
        pass.beforeDdl(statement -> {
            if (statement.kind() == LeafIndexPass.Kind.DROP) {
                drops.add(statement);
                try (Connection su = pg.createConnection("")) {
                    PgContainerHelper.markIndexValid(su, schema, leftover);   // an operator's build finished after the pass's first read
                } catch (java.sql.SQLException e) {
                    throw new IllegalStateException(e);
                }
            }
        });

        var result = pass.runOnce();

        assertThat(drops).as("the pass was about to drop it").hasSize(1);
        assertThat(result.dropped()).as("the second read found it valid: no DROP was sent").isZero();
        List<Idx> after = asSuperuser(su -> IdKeyIndexProbe.indexes(su, IdKeyIndexProbe.CHUNKS)).stream().filter(i -> i.relation().equals(leaf)).toList();
        assertThat(after).as("the index is still there, once, valid, the same one").hasSize(1);
        assertThat(after.get(0).valid()).isTrue();
        assertThat(after.get(0).indexOid()).isEqualTo(oid);
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

    /**
     * CR-S3: a leaf whose build fails (a duplicate under the id key) is not dropped and rebuilt on every tick: it waits 5 minutes after the
     * first failure, 10 after the second, 20 after the third, and the wait is in memory on a clock the test sets (no sleeps). {@code done}
     * of the boot_work entry counts the builds that succeeded only. Once the duplicate is gone the leaf builds and the table registers.
     */
    @Test
    void aLeafWhoseBuildFailedWaitsOutAnExponentialBackoff_andOnlySuccessesCountInBootWork() throws Exception {
        String tenant = newTenant("backoff");
        collection(tenant, name(tenant, "a"), 3);
        duplicateUnderTheIdKey(tenant, name(tenant, "a"), name(tenant, "dup"));
        String leaf = chunksLeaf(tenant);
        String relation = leaf.substring(leaf.indexOf('.') + 1);
        var clock = new MutableClock();
        var registry = new BootWorkRegistry(clock);
        var pass = new LeafIndexPass(admin, registry, () -> true, ENGINE_VERSION, clock);
        List<LeafIndexPass.Ddl> sent = new ArrayList<>();
        List<String> progress = new ArrayList<>();
        AtomicBoolean failedSeen = new AtomicBoolean();
        AtomicInteger builds = new AtomicInteger();
        pass.beforeDdl(statement -> {
            sent.add(statement);
            if (statement.kind() == LeafIndexPass.Kind.BUILD) {
                int succeeded = builds.getAndIncrement() - (failedSeen.get() ? 1 : 0);
                registry.snapshot().stream().filter(e -> LeafIndexPass.BOOT_WORK_ID.equals(e.id())).findFirst()
                    .ifPresent(e -> progress.add(e.done() + "/" + succeeded));
                if (statement.target().equals(relation)) {
                    failedSeen.set(true);
                }
            }
        });

        Runnable tick = () -> {
            builds.set(0);
            failedSeen.set(false);
            pass.runOnce();
        };
        java.util.function.Function<LeafIndexPass.Kind, Long> count = kind ->
            sent.stream().filter(q -> q.kind() == kind && (kind == LeafIndexPass.Kind.DROP
                ? q.target().equals(LeafIndexPass.leafIndexName(relation)) : q.target().equals(relation))).count();

        tick.run();   // t0: the first attempt fails
        assertThat(count.apply(LeafIndexPass.Kind.BUILD)).isEqualTo(1);
        assertThat(progress).as("done at the start of each build is the number of builds that succeeded before it, a failure not counted")
            .allSatisfy(p -> assertThat(p.substring(0, p.indexOf('/'))).isEqualTo(p.substring(p.indexOf('/') + 1)));

        clock.now = clock.now.plus(java.time.Duration.ofMinutes(1));
        tick.run();   // inside the first 5 minutes: not touched
        assertThat(count.apply(LeafIndexPass.Kind.BUILD)).as("no second attempt inside 5 minutes").isEqualTo(1);
        assertThat(count.apply(LeafIndexPass.Kind.DROP)).as("and no drop").isZero();

        clock.now = clock.now.plus(java.time.Duration.ofMinutes(5));   // t0 + 6
        tick.run();
        assertThat(count.apply(LeafIndexPass.Kind.DROP)).as("after 5 minutes the leftover is dropped").isEqualTo(1);
        assertThat(count.apply(LeafIndexPass.Kind.BUILD)).as("and rebuilt (and fails again)").isEqualTo(2);

        clock.now = clock.now.plus(java.time.Duration.ofMinutes(9));   // t0 + 15: the second wait is 10 minutes, from t0 + 6
        tick.run();
        assertThat(count.apply(LeafIndexPass.Kind.BUILD)).as("inside the second, doubled wait").isEqualTo(2);

        clock.now = clock.now.plus(java.time.Duration.ofMinutes(2));   // t0 + 17
        tick.run();
        assertThat(count.apply(LeafIndexPass.Kind.BUILD)).as("after it").isEqualTo(3);
        assertThat(swapLabels()).as("the duplicate holds the chunks table back").containsExactly(CENTROIDS_SWAP);

        removeDuplicate(tenant, name(tenant, "dup"));
        clock.now = clock.now.plus(java.time.Duration.ofMinutes(21));   // t0 + 38: past the third wait (20 minutes from t0 + 17)
        var last = pass.runOnce();
        assertThat(last.registered()).as("the leaf builds and the chunks swap registers").containsExactly(CHUNKS_SWAP);
        assertThat(chunksLeavesWithoutIndex()).isEmpty();
    }
}
