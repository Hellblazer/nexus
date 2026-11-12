// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.IndexRunCensusReads;
import dev.nexus.service.db.TenantScope;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Pattern;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd: the chunk read of the index-run census's failed_hidden bucket on a leaf of 200,000 chunk rows. Opt-in: tag
 * {@code measurement}, which the default run leaves out (the seed takes about two minutes, almost all HNSW maintenance on
 * insert). Run: {@code scripts/mvnw-leased.sh -B test -Dtest.excluded.groups= -Dgroups=measurement -Dtest=IndexRunCensusChunkReadMeasurementTest}.
 * The leaf-pruning plan assertion is {@link IndexRunCensusPlanTest}'s, on a small seed.
 */
@Tag("measurement")
class IndexRunCensusChunkReadMeasurementTest extends IndexRunCensusTestBase {

    /**
     * ONE measurement on a leaf of 200,000 chunk rows (a large legacy collection): {@code content_hash} is an unindexed
     * metadata key, so the statement reads the whole leaf. The seed is server-side ({@code generate_series}); the figures are
     * printed (EXPLAIN ANALYZE, BUFFERS) for the record, and the statement must finish inside the 10 s bucket bound.
     */
    @Test
    void aLeafOfTwoHundredThousandChunkRowsIsReadWithinTheBound_andTheTimingIsRecorded() throws Exception {
        collection(TENANT, C1);
        final int rows = 200_000;
        long seedStart = System.nanoTime();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            String model = PgContainerHelper.collectionModel(ctx, TENANT, C1);
            var series = DSL.generateSeries(1, rows).as("g", "n");
            org.jooq.Field<Integer> n = series.field(0, Integer.class);
            org.jooq.Field<byte[]> chash = DSL.function(DSL.name("decode"), org.jooq.impl.SQLDataType.VARBINARY,
                DSL.lpad(DSL.function(DSL.name("to_hex"), org.jooq.impl.SQLDataType.VARCHAR, n), 64, "0"), DSL.inline("hex"));
            org.jooq.Field<org.jooq.JSONB> meta = DSL.jsonbObject(DSL.jsonEntry("content_hash",
                DSL.concat(DSL.inline("legacy"), n.cast(org.jooq.impl.SQLDataType.VARCHAR))));
            ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL, CHUNKS.CHUNK_TEXT,
                    CHUNKS.EMBEDDING_768, CHUNKS.METADATA)
                .select(ctx.select(DSL.inline(TENANT), DSL.inline(C1), chash, DSL.inline(model), DSL.inline("t"),
                    DSL.val(VectorSeeds.vec(768, 0.1f), CHUNKS.EMBEDDING_768), meta).from(series))
                .execute();
            PgContainerHelper.analyzeTable(su, CHUNKS);
        }
        System.out.println("CHUNK-READ seeded " + rows + " chunk rows in one leaf in " + (System.nanoTime() - seedStart) / 1_000_000 + " ms");
        // 500 hashes (one batch): 3 name rows of the leaf, the rest name none (the worst case reads everything either way)
        var hashes = new ArrayList<String>(List.of("legacy1", "legacy77777", "legacy200000"));
        for (int i = 0; i < 497; i++) hashes.add("absent" + i);
        var scope = new TenantScope(svcDs);
        var execution = Pattern.compile("Execution Time: ([0-9.]+) ms");
        for (int run = 1; run <= 3; run++) {
            long t0 = System.nanoTime();
            var got = scope.withTenant(TENANT, ctx -> {
                dev.nexus.service.db.PgSession.setLocal(ctx, "statement_timeout", "10000");   // the route's bound
                return IndexRunCensusReads.storedByHashQuery(ctx, TENANT, C1, PgContainerHelper.collectionModel(ctx, TENANT, C1), hashes).fetch();
            });
            long ms = (System.nanoTime() - t0) / 1_000_000;
            System.out.println("CHUNK-READ run " + run + ": " + ms + " ms, " + got.size() + " hashes found");
            assertThat(got).hasSize(3);
            assertThat(ms).as("one 500-hash statement over 200k rows, ms").isLessThan(10_000);
        }
        String plan = scope.withTenant(TENANT, ctx -> PgContainerHelper.explainPlanWith(ctx,
            IndexRunCensusReads.storedByHashQuery(ctx, TENANT, C1, PgContainerHelper.collectionModel(ctx, TENANT, C1), hashes),
            PgContainerHelper.ExplainOptions.ANALYZE_BUFFERS));
        System.out.println("CHUNK-READ plan:\n" + plan);
        var m = execution.matcher(plan);
        assertThat(m.find()).as(plan).isTrue();
        System.out.println("CHUNK-READ EXPLAIN ANALYZE execution " + m.group(1) + " ms");
    }
}
