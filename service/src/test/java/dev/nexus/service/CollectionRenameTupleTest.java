// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.Record;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.8 (nexus-p2m1x.28), review r3 finding B1: a rename whose new row has the tuple of the old one, with the partial
 * UNIQUE index of the live tuple (changeset {@code collections-001-2}) present and absent.
 *
 * <p>A rename copies the source's attributes onto the new row, so unless the caller names another owner or content type the
 * new row Y has the live tuple the source X still holds. The transaction used to insert Y live before it superseded X, which
 * the index refuses (23505, unnamed). And P3.1's {@code catalog_collections_superseded_by_id_check} ties
 * {@code superseded_by = ''} to {@code superseded_by_id IS NULL}, so X cannot be superseded before Y has an id: the
 * transaction draws Y's id first, supersedes X with it, and then writes Y with that id. A rename back onto the tombstone
 * revives it (its own id stays, its {@code superseded_by_id} is cleared) and supersedes the other with the reviver's id.
 *
 * <p>Both index states, and three shapes of name: a user label, a conformant name, and the rename back. As a NOBYPASSRLS
 * service role, as the engine runs.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionRenameTupleTest {

    private static final String TENANT = "rename-tuple-tenant";
    private static final String SVC_ROLE = "svc_rename_tuple";
    private static final String SVC_PASS = "svc_rename_tuple_pass";
    private static final String MODEL = "bge-base-en-v15-768";

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
        config.setMaximumPoolSize(4);
        config.setAutoCommit(true);
        svcDs = new HikariDataSource(config);
        repo = new CatalogRepository(new TenantScope(svcDs));
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @BeforeEach
    void indexAbsent() {
        su(ctx -> {
            CollectionMintSupport.dropIndex(ctx);
            return null;
        });
    }

    private <T> T su(Function<DSLContext, T> work) {
        try (Connection c = pg.createConnection("")) {
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    private void indexState(boolean present) {
        if (present) {
            su(ctx -> {
                CollectionMintSupport.createIndex(ctx);
                return null;
            });
        }
        assertThat(su(CollectionMintSupport::indexPresent)).as("guard: the index state under test").isEqualTo(present);
    }

    private void register(String name, String owner) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("name", name);
        m.put("content_type", "knowledge");
        m.put("owner_id", owner);
        m.put("embedding_model", MODEL);
        m.put("model_version", "v1");
        repo.upsertCollection(TENANT, m, true);
    }

    private Record row(String name) {
        return su(ctx -> ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID, CATALOG_COLLECTIONS.SUPERSEDED_BY,
                CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, CATALOG_COLLECTIONS.OWNER_ID, CATALOG_COLLECTIONS.EMBEDDING_MODEL,
                CATALOG_COLLECTIONS.MODEL_VERSION, CATALOG_COLLECTIONS.CONTENT_TYPE)
            .from(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(name))
            .fetchOne());
    }

    private long liveRowsOfTuple(String owner) {
        return su(ctx -> (long) ctx.fetchCount(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)
            .and(CATALOG_COLLECTIONS.OWNER_ID.eq(owner)).and(CATALOG_COLLECTIONS.SUPERSEDED_BY.eq(""))));
    }

    private void assertLive(String name, String owner) {
        Record r = row(name);
        assertThat(r).as("row %s", name).isNotNull();
        assertThat(r.get(CATALOG_COLLECTIONS.SUPERSEDED_BY)).as("%s is live", name).isEqualTo("");
        assertThat(r.get(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID)).as("%s has no successor id", name).isNull();
        assertThat(r.get(CATALOG_COLLECTIONS.COLLECTION_ID)).as("%s has an id", name).isNotNull();
        assertThat(r.get(CATALOG_COLLECTIONS.OWNER_ID)).isEqualTo(owner);
        assertThat(r.get(CATALOG_COLLECTIONS.EMBEDDING_MODEL)).isEqualTo(MODEL);
        assertThat(r.get(CATALOG_COLLECTIONS.MODEL_VERSION)).isEqualTo("v1");
    }

    private void assertRetiredBy(String name, String successor) {
        Record r = row(name);
        assertThat(r.get(CATALOG_COLLECTIONS.SUPERSEDED_BY)).as("%s is superseded by %s", name, successor).isEqualTo(successor);
        assertThat(r.get(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID)).as("%s names its successor's id", name)
            .isEqualTo(row(successor).get(CATALOG_COLLECTIONS.COLLECTION_ID));
    }

    private void assertRenameAndBack(String x, String y, String owner, boolean conformant) {
        register(x, owner);
        long xId = row(x).get(CATALOG_COLLECTIONS.COLLECTION_ID);

        var counts = conformant
            ? repo.renameCollection(TENANT, x, y, null, "knowledge", owner, MODEL)
            : repo.renameCollection(TENANT, x, y);

        assertThat(counts.get("catalog_collections_inserted")).as("Y registered").isEqualTo(1);
        assertThat(counts.get("catalog_collections_superseded")).as("X retired").isEqualTo(1);
        assertLive(y, owner);
        assertRetiredBy(x, y);
        assertThat(row(x).get(CATALOG_COLLECTIONS.COLLECTION_ID)).as("X keeps its own id").isEqualTo(xId);
        assertThat(row(y).get(CATALOG_COLLECTIONS.COLLECTION_ID)).as("Y has an id of its own").isNotEqualTo(xId);
        assertThat(liveRowsOfTuple(owner)).as("one live row on the tuple").isEqualTo(1);
        long yId = row(y).get(CATALOG_COLLECTIONS.COLLECTION_ID);

        // the way back: Y -> X revives the tombstone (the handler passes the tombstone's superseded_by as it read it)
        repo.renameCollection(TENANT, y, x, y);

        assertLive(x, owner);
        assertRetiredBy(y, x);
        assertThat(row(x).get(CATALOG_COLLECTIONS.COLLECTION_ID)).as("the revived row keeps its id").isEqualTo(xId);
        assertThat(row(y).get(CATALOG_COLLECTIONS.COLLECTION_ID)).isEqualTo(yId);
        assertThat(liveRowsOfTuple(owner)).as("one live row on the tuple after the way back").isEqualTo(1);
    }

    // ── a user label ────────────────────────────────────────────────────────────────────────────

    @Test
    void aPlainRenameOnTheSameTupleSucceeds_andRenamesBack_withTheIndexAbsent() {
        indexState(false);
        assertRenameAndBack("plain-x-off", "plain-y-off", "rn-subject-plain-off", false);
    }

    @Test
    void aPlainRenameOnTheSameTupleSucceeds_andRenamesBack_withTheIndexPresent() {
        indexState(true);
        assertRenameAndBack("plain-x-on", "plain-y-on", "rn-subject-plain-on", false);
    }

    // ── a conformant name ───────────────────────────────────────────────────────────────────────

    @Test
    void aConformantNameRenameOnTheSameTupleSucceeds_andRenamesBack_withTheIndexAbsent() {
        indexState(false);
        assertRenameAndBack("knowledge__rn-conf-off__" + MODEL + "__v1", "knowledge__rn-conf-off-2__" + MODEL + "__v1",
            "rn-subject-conf-off", true);
    }

    @Test
    void aConformantNameRenameOnTheSameTupleSucceeds_andRenamesBack_withTheIndexPresent() {
        indexState(true);
        assertRenameAndBack("knowledge__rn-conf-on__" + MODEL + "__v1", "knowledge__rn-conf-on-2__" + MODEL + "__v1",
            "rn-subject-conf-on", true);
    }

    // ── the rename that changes the tuple, and the refusals, still behave ───────────────────────

    @Test
    void aRenameThatNamesAnotherOwnerLeavesTheTupleItsOwn_withTheIndexPresent() {
        indexState(true);
        register("owner-x-on", "rn-subject-owner-on");

        repo.renameCollection(TENANT, "owner-x-on", "owner-y-on", null, "knowledge", "rn-subject-owner-on-new");

        assertThat(row("owner-y-on").get(CATALOG_COLLECTIONS.OWNER_ID)).isEqualTo("rn-subject-owner-on-new");
        assertRetiredBy("owner-x-on", "owner-y-on");
    }

    @Test
    void aRenameOntoALiveTargetIsStillRefusedAndWritesNothing_withTheIndexPresent() {
        indexState(true);
        register("busy-x-on", "rn-subject-busy-on");
        register("busy-y-on", "rn-subject-busy-on-other");

        org.assertj.core.api.Assertions.assertThatThrownBy(() -> repo.renameCollection(TENANT, "busy-x-on", "busy-y-on"))
            .isInstanceOf(CatalogRepository.CollectionMergeRefused.class).hasMessageContaining("target collection already exists");

        assertLive("busy-x-on", "rn-subject-busy-on");
        assertThat(row("busy-y-on").get(CATALOG_COLLECTIONS.SUPERSEDED_BY)).isEqualTo("");
    }

    @Test
    void aRevivedTombstoneWhoseIdentityChangedIsStillRefused_withTheIndexPresent() {
        indexState(true);
        register("cas-x-on", "rn-subject-cas-on");
        repo.renameCollection(TENANT, "cas-x-on", "cas-y-on");

        org.assertj.core.api.Assertions.assertThatThrownBy(() -> repo.renameCollection(TENANT, "cas-y-on", "cas-x-on", "someone-else"))
            .isInstanceOf(CatalogRepository.CollectionMergeRefused.class).hasMessageContaining("someone-else");

        assertRetiredBy("cas-x-on", "cas-y-on");
        assertLive("cas-y-on", "rn-subject-cas-on");
    }

    // ── Y's tuple is another collection's, or X is one of D4's accepted pair ────────────────────

    private void assertRenameOntoAHeldTupleIsRefusedNamingBoth(String suffix) {
        String heldOwner = "rn-held-owner-" + suffix;
        register("held-holder-" + suffix, heldOwner);
        register("mover-" + suffix, "rn-mover-owner-" + suffix);

        org.assertj.core.api.Assertions.assertThatThrownBy(
                () -> repo.renameCollection(TENANT, "mover-" + suffix, "moved-" + suffix, null, "knowledge", heldOwner))
            .as("a rename whose new owner puts Y on a tuple another collection holds").isInstanceOf(dev.nexus.service.db.CollectionMint.LiveTupleConflict.class)
            .hasMessageContaining("held-holder-" + suffix).hasMessageContaining("moved-" + suffix);

        assertLive("mover-" + suffix, "rn-mover-owner-" + suffix);
        assertThat(row("moved-" + suffix)).as("nothing was written").isNull();
    }

    @Test
    void aRenameOntoATupleAnotherCollectionHoldsIsRefusedNamingBoth_withTheIndexAbsent() {
        indexState(false);
        assertRenameOntoAHeldTupleIsRefusedNamingBoth("off");
    }

    @Test
    void aRenameOntoATupleAnotherCollectionHoldsIsRefusedNamingBoth_withTheIndexPresent() {
        indexState(true);
        assertRenameOntoAHeldTupleIsRefusedNamingBoth("on");
    }

    @Test
    void renamingOneMemberOfAnAcceptedDuplicatePairKeepsThePairAtTwoLiveRows_withTheIndexAbsent() {
        indexState(false);
        su(ctx -> {
            CollectionMintSupport.insertLiveRow(ctx, TENANT, "pair-a", "rn-pair-owner", MODEL, null);
            CollectionMintSupport.insertLiveRow(ctx, TENANT, "pair-b", "rn-pair-owner", MODEL, null);
            return null;
        });

        repo.renameCollection(TENANT, "pair-a", "pair-c");

        assertThat(liveRowsOfTuple("rn-pair-owner")).as("Y replaced X on the tuple: the pair is as it was").isEqualTo(2);
        assertRetiredBy("pair-a", "pair-c");
    }
}
