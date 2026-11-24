// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.datamove.Gate3;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.PgSession.PciSettings;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.JSONB;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.time.Clock;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.random.RandomGenerator;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.4 (linda, 2026-11-23): the recall canary follows the latch. Before gate 3 latches it measures the name-predicate indexes; once it
 * has, the name family retires and the canary measures the id-predicate indexes, through the id twin of the serving search ({@code _by_id}),
 * so it is not the name path in disguise. The fixtures are {@link PciIdIndexSupport}'s.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PciIdCanaryIntegrationTest extends PciIdIndexSupport {

    private static final int QUERIES = 10;
    /** Enough rows that the planner prefers the HNSW index to a sequential scan of the leaf. */
    private static final int ROWS = 12000;

    private static final int CLUSTERS = 20;
    private static final float[][] CENTRES = centres();

    private static float[][] centres() {
        var rnd = new java.util.Random(229);
        float[][] out = new float[CLUSTERS][384];
        for (float[] c : out) {
            for (int d = 0; d < c.length; d++) {
                c[d] = (float) rnd.nextGaussian();
            }
        }
        return out;
    }

    /** A unit vector near cluster {@code c}: close enough to its rows that the serving search's distance bound admits them. */
    private static float[] near(java.util.Random rnd, int c, double spread) {
        float[] v = new float[384];
        double norm = 0;
        for (int d = 0; d < v.length; d++) {
            v[d] = (float) (CENTRES[c][d] + spread * rnd.nextGaussian());
            norm += (double) v[d] * v[d];
        }
        float scale = (float) (1.0 / Math.sqrt(norm));
        for (int d = 0; d < v.length; d++) {
            v[d] *= scale;
        }
        return v;
    }

    private static float[] query(int i) {
        return near(new java.util.Random(7000L + i), i % CLUSTERS, 0.35);
    }

    /** Registers {@code coll}, gives it {@code rows} clustered, stamped rows, and returns its registry id. */
    private long clustered(String tenant, String coll, int rows) throws Exception {
        asSuperuser(su -> {
            PgContainerHelper.insertCollection(su, tenant, coll, M384);
            return null;
        });
        var rnd = new java.util.Random(31);
        String[] hex = new String[rows];
        asSuperuser(su -> {
            for (int from = 0; from < rows; from += 250) {
                var step = su.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL,
                    CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_384, CHUNKS.METADATA);
                for (int i = from; i < Math.min(rows, from + 250); i++) {
                    hex[i] = Chash.ofText(tenant + "/" + coll + "/" + i).toHex();
                    step = step.values(tenant, coll, Chash.fromHex(hex[i]).toBytes(),
                        M384, "row " + i, Vector.of(near(rnd, i % CLUSTERS, 0.35)), JSONB.jsonb("{}"));
                }
                step.onConflictDoNothing().execute();
            }
            // the search returns only chunks a catalog document owns
            for (int from = 0; from < rows; from += 4000) {
                PgContainerHelper.ownChunks(su, tenant, coll, java.util.Arrays.copyOfRange(hex, from, Math.min(rows, from + 4000)));
            }
            return null;
        });
        return stamp(tenant, coll);
    }

    private static List<PciQueryRingReader.RingEntry> ring() {
        List<PciQueryRingReader.RingEntry> out = new ArrayList<>();
        for (int i = 0; i < QUERIES; i++) {
            out.add(new PciQueryRingReader.RingEntry(M384, query(i), OffsetDateTime.now()));
        }
        return out;
    }

    /** The serving repository over the real sweep with a real EXPLAIN source, as the canary's forced plan check needs. */
    private PgVectorRepository repository(PciIndexSweep sweep) {
        Embedder none = texts -> List.of();
        return new PgVectorRepository(new TenantScope(svcDs), none, none, sweep,
            new PciPlanCheck(Integer.MAX_VALUE, (ctx, q) -> ctx.explain(q).plan()));
    }

    /** A reconciler (sharing the repository's sweep, as production does) with the canary over the serving repository, its id statements following the test's latch ({@code gate3}). */
    private PciReconciler withCanary(PciSettings s, PgVectorRepository repo, PciIndexSweep sweep) {
        Gate3 latched = () -> gate3 && readers;
        var runner = new PciCanary((t, c) -> ring(), repo.canaryStatements(latched));
        var builder = new PciBuilderSession(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS, NONCE, s);
        var r = new PciReconciler(catalog::read, builder, sweep, s, Clock.systemUTC(),
            PciReconciler.SET_LOCAL_TENANT, PciReconciler.COUNT_TIMEOUT, Duration.ofSeconds(60), RandomGenerator.getDefault(),
            leafTenants -> 0, new PciCanarySchedule(runner));
        r.idBuildGate(() -> gate2);
        r.retireNameIndexesWhen(() -> gate3);
        r.buildIdIndexesFirstWhen(() -> pciOffLatch);
        r.idReadersPresent(() -> readers);
        return r;
    }

    @Test
    void beforeTheLatchTheCanaryMeasuresTheNameIndex_andAfterItTheIdIndex_throughTheByIdTwin() throws Exception {
        PgSession.overrideSearchExactMaxRowsForTests(100);   // the router sends the statement down HNSW, where an index can be used
        String tenant = newTenant("canary");
        String coll = name("canary");
        long id = clustered(tenant, coll, ROWS);
        // Statistics, so the planner prices the partial HNSW index against the primary key, and the manifest tables the search's
        // ownership EXISTS joins. Those are left to autoanalyze otherwise: until it runs they read reltuples=0 (the template's
        // empty state), the planner takes the 12000 manifest rows for one per document, and the by-id statement drives a nested
        // loop from catalog_documents and sorts, naming no HNSW index; the canary then reads index_not_used (nexus-p2m1x.22).
        try (java.sql.Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            for (String table : List.of("chunks", "catalog_documents", "catalog_document_chunks", "catalog_collections")) {
                PgContainerHelper.analyzeTable(su, org.jooq.impl.DSL.table(org.jooq.impl.DSL.name("nexus", table)));
            }
        }
        PciSettings s = settings(16);
        PciIndexSweep sweep = PciIndexSweep.create(svcDs, s);
        PgVectorRepository repo = repository(sweep);
        PciReconciler r = withCanary(s, repo, sweep);
        String nameIndex = PciCatalog.indexName(M384, tenant, coll);
        String idIndex = PciCatalog.indexNameById(M384, tenant, id);

        // before the latch: the name index builds and is measured; the id index builds beside it (gate 2) and is not
        r.reconcileOnce();
        gate2 = true;
        r.reconcileOnce();
        sweep.refresh();
        assertThat(validNameIndexed(tenant)).containsExactly(coll);
        assertThat(validIdIndexed(tenant)).containsExactly(id);
        var before = r.canaryResults();
        assertThat(before).as("control, before the latch: the canary ran against the name index").containsKey(nameIndex);
        assertThat(before).as("and not against the id index").doesNotContainKey(idIndex);
        assertThat(before.get(nameIndex).state()).as(PciCanary.line(before.get(nameIndex))).isEqualTo(PciCanary.State.GREEN);
        assertThat(before.get(nameIndex).target().collectionId()).as("a name target carries no id").isNull();

        // after the latch: the name family retires, and the canary measures the id index
        gate3 = true;
        r.reconcileOnce();
        sweep.refresh();
        assertThat(validNameIndexed(tenant)).as("the name index retired").isEmpty();
        var after = r.canaryResults();
        assertThat(after).as("latched: the canary targets the id index").containsKey(idIndex);
        assertThat(after).as("and no longer the retired name index").doesNotContainKey(nameIndex);
        PciCanary.Result result = after.get(idIndex);
        assertThat(result.target().collectionId()).isEqualTo(id);
        assertThat(result.target().collection()).as("the id's current label, for the ring").isEqualTo(coll);
        assertThat(result.state()).as(PciCanary.line(result)).isEqualTo(PciCanary.State.GREEN);
        assertThat(result.queries()).isEqualTo(QUERIES);
        assertThat(result.meanRecall()).as("the recall is recorded").isNotNull().isGreaterThan(0.0);

        // the statement it sends is the id twin: its plan walks the id-predicate index, which a name-predicate statement cannot use
        PciCanary.Leg leg = repo.canaryStatements(() -> gate3 && readers).hnsw(result.target(), query(0), 10, true);
        assertThat(leg.routedExact()).isFalse();
        assertThat(leg.plan()).as("the plan of the id canary's statement").contains(idIndex).doesNotContain(nameIndex);

        // and an id target is refused, not silently sent by name, while the latch does not hold
        gate3 = false;
        assertThatRefused(repo, result.target());
    }

    private void assertThatRefused(PgVectorRepository repo, PciCanary.Target target) {
        org.assertj.core.api.Assertions.assertThatThrownBy(() -> repo.canaryStatements(() -> gate3 && readers).hnsw(target, query(0), 10, false))
            .isInstanceOf(IllegalStateException.class).hasMessageContaining("latch no longer holds");
    }
}
