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
import dev.nexus.service.db.PgSession.PciSettings;
import dev.nexus.service.datamove.CompletionCheck;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.vectors.PciBuilderSession.BuilderState;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
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
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;
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
            su.execute("DELETE FROM nexus.data_moves WHERE label LIKE 'rdr229-swap-%'");
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
        return pass(ctx -> true);
    }

    /** A tenant with leaves under every model partition of both tables, created the way production's token trigger does (as the owner). */
    @SuppressWarnings("deprecation")
    String newTenant(String tag) throws Exception {
        String tenant = "p33b-" + tag;
        tenants.add(tenant);
        asOwner(owner -> {
            dev.nexus.service.jooq.nexus.Routines.createTenantPartitions(owner.configuration(), "nexus.chunks", tenant, true, true);
            dev.nexus.service.jooq.nexus.Routines.createTenantPartitions(owner.configuration(), "nexus.taxonomy_centroids", tenant, true, true);
            return null;
        });
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
        asSuperuser(su -> su.execute("DELETE FROM nexus.chunks WHERE tenant_id = ? AND collection = ?", tenant, secondCollection));
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
        return asSuperuser(su -> su.fetch("SELECT label, driving_schema, driving_table, template, verification, on_complete, "
                + "changeset_id, completed_at, registered_at FROM nexus.data_moves WHERE label LIKE 'rdr229-swap-%' ORDER BY label")
            .intoMaps());
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
     * nexus_rdr/229-research-33, the pattern of {@code PciBootWork.ON_STOP}). The leftover is made by a real concurrent build that
     * fails on a duplicate, named as the pass names its leaf index, so the pass finds it. The duplicate is then removed; what the
     * next pass must do is the drop and the rebuild, not build beside it: one unique id index on the leaf, valid, and not the
     * leftover's oid.
     */
    @Test
    void aPassEndedMidBuildLeavesAnInvalidIndexAndTheNextPassDropsAndRebuildsIt() throws Exception {
        String tenant = newTenant("midbuild");
        collection(tenant, name(tenant, "a"), 3);
        duplicateUnderTheIdKey(tenant, name(tenant, "a"), name(tenant, "dup"));
        String leaf = chunksLeaf(tenant);
        String leftover = LeafIndexPass.leafIndexName(leaf.substring(leaf.indexOf('.') + 1));
        assertThatThrownBy(() -> asOwner(owner -> owner.execute("CREATE UNIQUE INDEX CONCURRENTLY \"" + leftover + "\" ON " + leaf
            + " (tenant_id, collection_id, chash, embedding_model)")))
            .as("the concurrent build cannot finish over the duplicate")
            .isInstanceOfSatisfying(DataAccessException.class, e -> assertThat(e.sqlState()).isEqualTo("23505"));
        List<Idx> before = asSuperuser(su -> IdKeyIndexProbe.indexes(su, IdKeyIndexProbe.CHUNKS)).stream()
            .filter(i -> i.relation().equals(leaf)).toList();
        assertThat(before).as("precondition: the failed build left one INVALID index on the leaf").hasSize(1);
        assertThat(before.get(0).valid()).isFalse();
        long leftoverOid = before.get(0).indexOid();
        removeDuplicate(tenant, name(tenant, "dup"));

        openPass().runOnce();

        List<Idx> after = asSuperuser(su -> IdKeyIndexProbe.indexes(su, IdKeyIndexProbe.CHUNKS)).stream()
            .filter(i -> i.relation().equals(leaf)).toList();
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
        LeafIndexPass.Gate1 real = ctx -> CompletionCheck.evaluate(ctx).gate1();
        Boolean realGate = asOwner(real::holds);
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

        asSuperuser(su -> su.execute("DELETE FROM nexus.data_moves WHERE label = ?", CHUNKS_SWAP));
        asSuperuser(su -> su.execute("INSERT INTO nexus.data_moves (label, template, driving_schema, driving_table, chunk_size, "
            + "sleep_ms, statement_timeout_ms, verification, changeset_id) VALUES (?, 'hand', 'nexus', 'chunks', 1, 0, 1000, 'hand', 'hand-registered')",
            CHUNKS_SWAP));
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
}
