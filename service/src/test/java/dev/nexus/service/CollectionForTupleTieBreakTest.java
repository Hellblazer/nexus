// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.reembed.TargetSelection;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.8 (nexus-p2m1x.28), the tuple tie-break: among the live candidates of one
 * {@code (content_type, owner_id, embedding_model)} tuple at the same top {@code model_version}, the LOWEST
 * {@code collection_id} wins, whether or not the partial UNIQUE index of the live tuple exists (a D4 duplicate can
 * still be live before it does). Then {@code created_at}, then the name, for a row without an id (the column is an
 * identity, so no row of this schema is without one; the order is total all the same). Both
 * {@code CatalogRepository#collectionForTuple} and the re-embed {@code TargetSelection} order this way, and the
 * parity case here holds them together. Rows are written as a superuser writes them, with explicit ids and stamps, so
 * the order of insertion, the order of the stamps and the order of the names each disagree with the order of the ids.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionForTupleTieBreakTest {

    private static final String TENANT = "tuple-tie-tenant";
    private static final String SVC_ROLE = "svc_tuple_tie";
    private static final String SVC_PASS = "svc_tuple_tie_pass";
    private static final String MODEL = "bge-base-en-v15-768";
    private static final OffsetDateTime T0 = OffsetDateTime.parse("2026-01-01T00:00:00Z");

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
        config.setMaximumPoolSize(3);
        config.setAutoCommit(true);
        svcDs = new HikariDataSource(config);
        repo = new CatalogRepository(new TenantScope(svcDs));
        // two live rows of one tuple are the case: the partial UNIQUE index would refuse the second
        su(ctx -> {
            CollectionMintSupport.dropIndex(ctx);
            return null;
        });
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private <T> T su(Function<DSLContext, T> work) {
        try (Connection c = pg.createConnection("")) {
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    /** A row of the tuple {@code (knowledge, owner, MODEL)} with an explicit id and creation stamp. */
    private void seed(String owner, String name, String version, long id, int createdDays, String lifecycle, String supersededBy) {
        su(ctx -> {
            var insert = ctx.insertInto(CATALOG_COLLECTIONS)
                .set(CATALOG_COLLECTIONS.TENANT_ID, TENANT)
                .set(CATALOG_COLLECTIONS.NAME, name)
                .set(CATALOG_COLLECTIONS.COLLECTION_ID, id)
                .set(CATALOG_COLLECTIONS.CONTENT_TYPE, "knowledge")
                .set(CATALOG_COLLECTIONS.OWNER_ID, owner)
                .set(CATALOG_COLLECTIONS.EMBEDDING_MODEL, MODEL)
                .set(CATALOG_COLLECTIONS.MODEL_VERSION, version)
                .set(CATALOG_COLLECTIONS.LIFECYCLE_STATE, lifecycle)
                .set(CATALOG_COLLECTIONS.CREATED_AT, T0.plusDays(createdDays))
                .set(CATALOG_COLLECTIONS.SUPERSEDED_BY, supersededBy);
            if (!supersededBy.isEmpty()) {
                insert.set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, id);   // a tombstone with no successor row takes its own id
            }
            insert.execute();
            return null;
        });
    }

    private void seedLive(String owner, String name, String version, long id, int createdDays) {
        seed(owner, name, version, id, createdDays, "live", "");
    }

    private String winner(String owner) {
        var row = repo.collectionForTuple(TENANT, "knowledge", owner, MODEL);
        assertThat(row).as("guard: the tuple answers for " + owner).isNotNull();
        return (String) row.get("name");
    }

    @Test
    void atOneVersionTheLowestCollectionIdWinsNotTheOldestNorTheLowestName() {
        String owner = "tie-lowest-id";
        // inserted first, oldest and lowest-named, but the HIGHER id: created_at / name order would pick it
        seedLive(owner, "a-oldest-first-inserted", "v1", 9020L, 0);
        seedLive(owner, "z-newest-low-id", "v1", 9010L, 30);
        seedLive(owner, "m-middle", "v1", 9030L, 10);

        assertThat(winner(owner)).isEqualTo("z-newest-low-id");
    }

    @Test
    void aHigherModelVersionStillBeatsALowerCollectionId() {
        String owner = "tie-version";
        seedLive(owner, "v1-lowest-id", "v1", 9110L, 0);
        seedLive(owner, "v10-high-id", "v10", 9130L, 0);
        seedLive(owner, "v2-mid-id", "v2", 9120L, 0);

        assertThat(winner(owner)).as("v10 numerically, whatever the ids").isEqualTo("v10-high-id");
    }

    @Test
    void aLowerIdThatIsQuarantinedOrSupersededIsNotACandidate() {
        String owner = "tie-excluded";
        seed(owner, "quarantined-lowest-id", "v1", 9210L, 0, "quarantine", "");
        seed(owner, "superseded-low-id", "v1", 9220L, 0, "live", "somewhere-else");
        seedLive(owner, "the-live-one", "v1", 9230L, 0);

        assertThat(winner(owner)).isEqualTo("the-live-one");
    }

    @Test
    void theReembedTargetSelectionNamesTheSameWinnerAsTheTupleLookup() {
        String owner = "tie-parity";
        seedLive(owner, "p-oldest-high-id", "v1", 9320L, 0);
        seedLive(owner, "p-newest-low-id", "v1", 9310L, 20);
        var rows = repo.listCollectionsWithIds(TENANT, "knowledge", null).stream()
            .map(TargetSelection.CollectionRow::fromMap).toList();
        assertThat(rows.stream().filter(r -> owner.equals(r.ownerId())).map(TargetSelection.CollectionRow::collectionId).toList())
            .as("guard: the rows the re-embed selection reads carry their ids").containsExactlyInAnyOrder(9320L, 9310L);

        var selected = TargetSelection.select(rows, "knowledge", owner, MODEL);

        assertThat(selected).isEqualTo(new TargetSelection.Live(winner(owner)));
        assertThat(selected).isEqualTo(new TargetSelection.Live("p-newest-low-id"));
    }

    @Test
    void theListRouteRowsCarryNoCollectionIdKeyAndTheIdListIsAParallelRead() {
        seedLive("tie-list", "list-row", "v1", 9410L, 0);
        var plain = repo.listCollections(TENANT, "knowledge", null).stream()
            .filter(r -> "tie-list".equals(r.get("owner_id"))).toList();
        var withIds = repo.listCollectionsWithIds(TENANT, "knowledge", null).stream()
            .filter(r -> "tie-list".equals(r.get("owner_id"))).toList();

        assertThat(plain).hasSize(1);
        assertThat(plain.get(0)).as("the wire shape of the list route is unchanged").doesNotContainKey("collection_id");
        assertThat(withIds).hasSize(1);
        assertThat(withIds.get(0).get("collection_id")).isEqualTo(9410L);
        assertThat(List.copyOf(withIds.get(0).keySet())).containsAll(plain.get(0).keySet());
    }
}
