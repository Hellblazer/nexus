// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.RegistryRenameSupport;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5c (folio's review): the embed-skip existence read of {@code upsertChunks} ({@code resolveNeedEmbedIdx}) selects the chunk rows by
 * the collection id from gate 3. It called the four-argument {@code chunkScope}, which is {@code Gate3.never()}, so after the latch and a
 * one-row rename (the chunk keeps its old stored name) the read found no existing row and every flush embedded the whole batch again. A repeat
 * upsert of the same text under the renamed row's CURRENT label embeds nothing; before the latch the same read is by name and finds nothing
 * (the control: the name read is blind to the rename).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P35cEmbedSkipByIdTest {

    private static final String SVC_ROLE = "svc_p35c_embedskip";
    private static final String SVC_PASS = "svc_p35c_embedskip_pass";
    private static final String TENANT = "tenant-p35c-embedskip";
    private static final String COLL = "code__p35c-embedskip__voyage-code-3__v1";
    private static final String RENAMED = "code__p35c-embedskip-renamed__voyage-code-3__v1";

    private PostgreSQLContainer<?> pg;
    private HikariDataSource svcDs;
    private TenantScope tenantScope;
    private CountingEmbedder embedder;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
            RegistryRenameSupport.dropForeignKeysOntoTheRegistry(DSL.using(su, SQLDialect.POSTGRES));   // a one-row rename cascades nowhere
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        embedder = new CountingEmbedder(1024);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private void renameRegistryRow() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            int n = DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, RENAMED)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(COLL)).execute();
            assertThat(n).as("setup: the registry row is renamed in place").isEqualTo(1);
        }
    }

    @Test
    void aRepeatUpsertUnderTheRenamedLabelEmbedsNothingFromTheLatch_andTheNameReadBeforeItIsBlind() throws Exception {
        tenantScope.withTenant(TENANT, ctx -> {
            PgContainerHelper.insertCollection(ctx, TENANT, COLL);
            return null;
        });
        var unlatched = new PgVectorRepository(tenantScope, embedder, embedder, dev.nexus.service.datamove.Gate3.never());
        String chash = Chash.ofText("p35c embed skip").toHex();
        unlatched.upsertChunks(TENANT, COLL, List.of(chash), List.of("the same text"), List.of(Map.of("v", "1")));
        int afterSeed = embedder.callCount();
        assertThat(afterSeed).as("setup: the seed embedded the chunk").isGreaterThan(0);
        renameRegistryRow();   // the chunk row keeps its old stored name; the id is the same

        var latched = new PgVectorRepository(tenantScope, embedder, embedder, () -> true);
        latched.upsertChunks(TENANT, RENAMED, List.of(chash), List.of("the same text"), List.of(Map.of("v", "2")));
        assertThat(embedder.callCount()).as("from gate 3 the existence read finds the row by its id: nothing is embedded again").isEqualTo(afterSeed);

        var byName = new PgVectorRepository(tenantScope, embedder, embedder, dev.nexus.service.datamove.Gate3.never());
        byName.upsertChunks(TENANT, RENAMED, List.of(chash), List.of("the same text"), List.of(Map.of("v", "3")));
        assertThat(embedder.callCount()).as("control: the name read before the latch finds nothing under the new label and embeds again")
            .isGreaterThan(afterSeed);
    }

    private static final class CountingEmbedder implements dev.nexus.service.vectors.Embedder {
        private final int dim;
        private final AtomicInteger calls = new AtomicInteger();

        CountingEmbedder(int dim) {
            this.dim = dim;
        }

        int callCount() {
            return calls.get();
        }

        @Override
        public List<float[]> embed(List<String> texts) {
            int call = calls.incrementAndGet();
            List<float[]> out = new ArrayList<>(texts.size());
            for (String ignored : texts) {
                float[] v = new float[dim];
                v[0] = call;
                out.add(v);
            }
            return out;
        }
    }
}
