// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.SchemaMigrator;
import dev.nexus.service.jooq.binding.Vector;
import org.assertj.core.api.SoftAssertions;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Random;
import java.util.function.LongPredicate;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;

/**
 * RDR-230 P1.5 (nexus-4lnn1.1.6), Critical Assumption A3: the bytes per row of a {@code pci_} HNSW index under ingest
 * and under 25 and 50 percent churn, read before and after {@code VACUUM}, on the engine's own substrate. A
 * measurement, no product code: it checks the local spike {@code nexus_rdr/230-research-11} against a partitioned
 * {@code chunks} leaf migrated by the non-superuser owner, with the index built as {@link PciBuilderSession} builds it
 * (m 16, ef_construction 64, {@code embedding_1024}, one collection's partial predicate), 1024 dimensions
 * ({@code voyage-code-3}), seeded random unit vectors, autovacuum off on the leaf.
 *
 * <p><b>Predictions</b> (research-11, PG 17.11, pgvector 0.8.7; constants below, asserted before any run). At 1024
 * dimensions an element takes one 8 KB page, so index bytes = (element slots + 1 metapage) x 8192 and bytes per row =
 * slots / rows. Each reading is (bytes_now / rows_now) / (bytes_base / rows_base):
 * <pre>
 *   scenario A, vacuumed churn  (base = a fresh build at N rows)
 *     ingest N to 2N                          1.000   (and 1.000 against a fresh build at 2N)
 *     delete 25%, before VACUUM               1.333   1/(1-f)
 *     delete 25%, after VACUUM                1.333   plain VACUUM does not shrink the index
 *     reinsert 25% after VACUUM               1.000   freed slots are reused
 *     delete 50%, before VACUUM               2.000
 *     delete 50%, after VACUUM                2.000
 *     reinsert 50% after VACUUM               1.000
 *   scenario B, churn outrunning VACUUM (base = a fresh build at 2N rows; no VACUUM between delete and reinsert)
 *     delete 25% and reinsert 25%             1.250
 *     then delete 50% and reinsert 50%        1.750
 *     VACUUM                                  1.750
 * </pre>
 * Order decides the result, so each stage is recorded in the order it ran. A reading outside its tolerance is a
 * finding for P2.1, not a bug: the test writes the whole table first ({@code target/a3-measure-results.md}) and fails
 * after, with every departure listed.
 *
 * <p>Slow (tens of minutes at N 20000; VACUUM of a 40k-row leaf took 163 to 268 s on the laptop in research-11), so
 * it carries its own tag, {@code measurement}, which the default run and the {@code integration} group both leave out.
 * Run it on the lab host:
 * {@code scripts/mvnw-leased.sh --lab-run lab-l test -Dtest.excluded.groups= -Dgroups=measurement
 * -Dtest=PciIndexBytesPerRowChurnMeasurementTest -Dnexus.test.fork.timeout=0 -Dnexus.a3.n=20000}.
 */
@Tag("measurement")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PciIndexBytesPerRowChurnMeasurementTest {

    static final String ADMIN_ROLE = PgContainerHelper.TEMPLATE_OWNER;
    static final String ADMIN_PASS = PgContainerHelper.TEMPLATE_OWNER_PASSWORD;
    static final String MODEL = "voyage-code-3";
    static final int DIM = 1024;
    static final String COLUMN = "embedding_1024";
    static final int PAGE = 8192;

    /** N, the built size; the ingest stage takes the index to 2N. Research-11 used 20000. */
    static final int N = Integer.getInteger("nexus.a3.n", 20_000);
    static final long SEED = Long.getLong("nexus.a3.seed", 42L);
    static final int BATCH = 250;

    // -- the prediction under test (research-11); relative tolerance 1% of the predicted ratio -----------------
    static final double P_INGEST = 1.000;
    static final double P_DELETE_25 = 1.0 / (1.0 - 0.25);
    static final double P_DELETE_50 = 1.0 / (1.0 - 0.50);
    static final double P_REINSERT_AFTER_VACUUM = 1.000;
    static final double P_CONTROL_25 = 1.250;
    static final double P_CONTROL_50 = 1.750;
    static final double TOLERANCE = 0.01;
    /** The readings a complete run takes: scenario A ten, scenario B four; the two bases carry no prediction. */
    static final int EXPECTED_READINGS = 14;
    static final long EXPECTED_COMPARED = 12;

    PostgreSQLContainer<?> pg;
    HikariDataSource adminDs;
    HikariDataSource svcDs;
    PciCatalog pciCatalog;
    final List<String> tenants = new ArrayList<>();
    final List<Reading> readings = new ArrayList<>();
    final List<String> notes = new ArrayList<>();

    /** One row of the results table. */
    record Reading(String scenario, String stage, long rows, long bytes, long deadTup, double ratio, double predicted,
                   double seconds) {
        long pages() {
            return bytes / PAGE;
        }

        double bytesPerRow() {
            return rows == 0 ? Double.NaN : (double) bytes / rows;
        }
    }

    @BeforeAll
    void startAll() throws Exception {
        // 40k rows of 1024 dimensions with two HNSW indexes (the leaf's own and the pci_ one), churn that is not
        // vacuumed in scenario B, and WAL: well past the default 2 GB tmpfs. A sized container, owner-migrated the
        // way PgContainerHelper.startMigratedByOwner does it on its dedicated branch.
        pg = PgContainerHelper.startDedicated("8g", 12L * 1024 * 1024 * 1024);
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
            var cfg = new HikariConfig();
            cfg.setJdbcUrl(pg.getJdbcUrl());
            cfg.setUsername(ADMIN_ROLE);
            cfg.setPassword(ADMIN_PASS);
            cfg.setMaximumPoolSize(2);
            try (HikariDataSource owner = new HikariDataSource(cfg)) {
                SchemaMigrator.migrate(owner);
                try (Connection oc = owner.getConnection()) {
                    PgContainerHelper.installTestObjectsAfterProductSchema(oc);
                }
            }
        }
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
        pciCatalog = new PciCatalog(svcDs);
    }

    @AfterAll
    @SuppressWarnings("deprecation")
    void stopAll() throws Exception {
        if (adminDs != null) {
            try (Connection owner = adminDs.getConnection()) {
                owner.setAutoCommit(true);
                DSLContext ctx = DSL.using(owner, SQLDialect.POSTGRES);
                for (String tenant : tenants) {
                    dev.nexus.service.jooq.nexus.Routines.dropTenantPartitions(ctx.configuration(), tenant);
                }
            } catch (Exception e) {
                System.out.println("A3-MEASURE cleanup could not drop the tenants: " + e);
            }
        }
        if (svcDs != null) svcDs.close();
        if (adminDs != null) adminDs.close();
        if (pg != null) pg.stop();
    }

    // -- the measurement -----------------------------------------------------------------------------------

    @Test
    void a3_bytesPerRow_underIngestAndChurn_matchesResearch11() throws Exception {
        long started = System.nanoTime();
        try {
            vacuumedChurn();
            churnOutrunningVacuum();
        } finally {
            writeResults(secondsSince(started));
        }

        SoftAssertions soft = new SoftAssertions();
        // The loop below passes over an empty list, so it proves nothing until the count does: scenario A takes ten
        // readings (base, ingest, the fresh comparison build, their ratio, then three for each of 25% and 50%) and
        // scenario B four; all but the two bases are compared with a prediction.
        soft.assertThat(readings).as("every stage of both scenarios took its reading").hasSize(EXPECTED_READINGS);
        soft.assertThat(readings.stream().filter(r -> !Double.isNaN(r.predicted())).count())
            .as("readings compared with a prediction").isEqualTo(EXPECTED_COMPARED);
        for (Reading r : readings) {
            if (Double.isNaN(r.predicted())) {
                continue;
            }
            soft.assertThat(r.ratio())
                .as("%s / %s: ratio against the fresh build (predicted %.3f, bytes %d, rows %d, dead %d)",
                    r.scenario(), r.stage(), r.predicted(), r.bytes(), r.rows(), r.deadTup())
                .isBetween(r.predicted() * (1 - TOLERANCE), r.predicted() * (1 + TOLERANCE));
        }
        soft.assertAll();
    }

    /** Scenario A: build N, ingest to 2N, then delete, VACUUM and reinsert at 25 and 50 percent. */
    private void vacuumedChurn() throws Exception {
        String tenant = newTenant("vac");
        String collection = collectionName("vac");
        String leaf = prepare(tenant, collection);
        String index = PciCatalog.indexName(MODEL, tenant, collection);
        Rows rows = new Rows(tenant, collection, SEED);

        rows.insert(N);
        long t = System.nanoTime();
        buildIndex(leaf, index, collection);
        Reading base = read("A", "fresh build at N rows (the base)", leaf, index, collection, rows.live(), null,
            Double.NaN, secondsSince(t));
        recordShape(index);

        t = System.nanoTime();
        rows.insert(N);
        Reading ingest = read("A", "ingest N to 2N, no churn", leaf, index, collection, rows.live(), base, P_INGEST,
            secondsSince(t));

        // The same rows, built fresh: a second partial index on the collection, read and dropped at once so it takes
        // no later write. Reading six of the bead: the ingested index against a build made at the same size.
        String fresh = PciCatalog.indexName(MODEL, tenant, collection + "~fresh2n");
        t = System.nanoTime();
        buildIndex(leaf, fresh, collection);
        Reading freshAt2n = read("A", "fresh build at 2N rows (comparison index, dropped after)", leaf, fresh,
            collection, rows.live(), base, P_INGEST, secondsSince(t));
        dropIndex(fresh);
        readings.add(new Reading("A", "ingested index against the fresh build at 2N rows", ingest.rows(), ingest.bytes(),
            ingest.deadTup(), ingest.bytesPerRow() / freshAt2n.bytesPerRow(), P_INGEST, 0));

        for (double fraction : new double[] {0.25, 0.50}) {
            int pct = (int) Math.round(fraction * 100);
            double predictedDeleted = pct == 25 ? P_DELETE_25 : P_DELETE_50;
            int count = (int) Math.round(rows.live() * fraction);

            t = System.nanoTime();
            rows.delete(count);
            read("A", "delete " + pct + "% (" + count + " rows), before VACUUM", leaf, index, collection,
                rows.live(), base, predictedDeleted, secondsSince(t), v -> v > 0);

            t = System.nanoTime();
            vacuum(leaf);
            double vacuumSeconds = secondsSince(t);
            notes.add(String.format(Locale.ROOT, "A: VACUUM after delete %d%% took %.1f s", pct, vacuumSeconds));
            read("A", "delete " + pct + "%, after VACUUM", leaf, index, collection, rows.live(), base, predictedDeleted,
                vacuumSeconds, v -> v == 0);

            t = System.nanoTime();
            rows.insert(count);
            read("A", "reinsert " + pct + "% (" + count + " rows) after VACUUM", leaf, index, collection, rows.live(),
                base, P_REINSERT_AFTER_VACUUM, secondsSince(t));
        }
    }

    /** Scenario B: a fresh build at 2N, then 25 and 50 percent churn with no VACUUM between delete and reinsert. */
    private void churnOutrunningVacuum() throws Exception {
        String tenant = newTenant("ctl");
        String collection = collectionName("ctl");
        String leaf = prepare(tenant, collection);
        String index = PciCatalog.indexName(MODEL, tenant, collection);
        Rows rows = new Rows(tenant, collection, SEED + 1);

        rows.insert(2 * N);
        long t = System.nanoTime();
        buildIndex(leaf, index, collection);
        Reading base = read("B", "fresh build at 2N rows (the base)", leaf, index, collection, rows.live(), null,
            Double.NaN, secondsSince(t));

        int quarter = (int) Math.round(rows.live() * 0.25);
        t = System.nanoTime();
        rows.delete(quarter);
        rows.insert(quarter);
        read("B", "delete and reinsert 25% (" + quarter + " rows), no VACUUM", leaf, index, collection, rows.live(),
            base, P_CONTROL_25, secondsSince(t), v -> v > 0);

        int half = (int) Math.round(rows.live() * 0.50);
        t = System.nanoTime();
        rows.delete(half);
        rows.insert(half);
        read("B", "then delete and reinsert 50% (" + half + " rows), no VACUUM", leaf, index, collection, rows.live(),
            base, P_CONTROL_50, secondsSince(t), v -> v > 0);

        t = System.nanoTime();
        vacuum(leaf);
        double vacuumSeconds = secondsSince(t);
        notes.add(String.format(Locale.ROOT, "B: VACUUM after the unvacuumed churn took %.1f s", vacuumSeconds));
        read("B", "after VACUUM", leaf, index, collection, rows.live(), base, P_CONTROL_50, vacuumSeconds, v -> v == 0);
    }

    // -- the rows ------------------------------------------------------------------------------------------

    /** The live row ids of one collection, the next id to hand out, and the seeded choice of what to delete. */
    final class Rows {
        final String tenant;
        final String collection;
        final long seed;
        final Random choice;
        final List<Integer> live = new ArrayList<>();
        int next;

        Rows(String tenant, String collection, long seed) {
            this.tenant = tenant;
            this.collection = collection;
            this.seed = seed;
            this.choice = new Random(seed ^ 0x5DEECE66DL);
        }

        long live() {
            return live.size();
        }

        void insert(int count) throws Exception {
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                for (int done = 0; done < count; done += BATCH) {
                    int n = Math.min(BATCH, count - done);
                    var step = ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH,
                        CHUNKS.EMBEDDING_MODEL, CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_1024, CHUNKS.METADATA);
                    for (int i = 0; i < n; i++) {
                        int id = next++;
                        step = step.values(tenant, collection, chash(id), MODEL, "row " + id, unitVector(seed, id),
                            JSONB.jsonb("{}"));
                        live.add(id);
                    }
                    step.execute();
                }
            }
        }

        /** Delete {@code count} live rows chosen by the seeded generator. */
        void delete(int count) throws Exception {
            Collections.shuffle(live, choice);
            List<Integer> doomed = new ArrayList<>(live.subList(0, count));
            live.subList(0, count).clear();
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                for (int from = 0; from < doomed.size(); from += 1000) {
                    List<byte[]> batch = new ArrayList<>();
                    for (int id : doomed.subList(from, Math.min(doomed.size(), from + 1000))) {
                        batch.add(chash(id));
                    }
                    ctx.deleteFrom(CHUNKS).where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION.eq(collection))
                        .and(CHUNKS.CHASH.in(batch)).execute();
                }
            }
        }

        byte[] chash(int id) {
            return Chash.ofText(tenant + "/" + collection + "/" + id).toBytes();
        }
    }

    /** A unit vector that depends only on the seed and the id, so a rerun inserts the same data. */
    static Vector unitVector(long seed, int id) {
        Random random = new Random(seed * 1_000_003L + id);
        float[] v = new float[DIM];
        double norm = 0;
        for (int i = 0; i < DIM; i++) {
            double g = random.nextGaussian();
            v[i] = (float) g;
            norm += g * g;
        }
        float scale = (float) (1.0 / Math.sqrt(norm));
        for (int i = 0; i < DIM; i++) {
            v[i] *= scale;
        }
        return Vector.of(v);
    }

    // -- the substrate -------------------------------------------------------------------------------------

    @SuppressWarnings("deprecation")
    String newTenant(String tag) throws Exception {
        String tenant = "a3m-" + tag;
        tenants.add(tenant);
        try (Connection owner = adminDs.getConnection()) {
            owner.setAutoCommit(true);
            dev.nexus.service.jooq.nexus.Routines.createTenantPartitions(
                DSL.using(owner, SQLDialect.POSTGRES).configuration(), "nexus.chunks", tenant, true);
        }
        return tenant;
    }

    static String collectionName(String tag) {
        return "code__a3m-" + tag + "__voyage-code-3__v1";
    }

    /** Register the collection and switch autovacuum off on its leaf; returns the leaf name. */
    private String prepare(String tenant, String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), tenant, collection, MODEL);
        }
        List<String> hits = pciCatalog.read().leaves().stream()
            .filter(l -> MODEL.equals(l.model()) && tenant.equals(l.tenant())).map(PciCatalog.Leaf::name).toList();
        if (hits.size() != 1) {
            throw new IllegalStateException("expected one leaf for (" + MODEL + ", " + tenant + "), got " + hits);
        }
        String leaf = hits.get(0);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.setTableStorageParams(su, DSL.table(DSL.name("nexus", leaf)),
                Map.of("autovacuum_enabled", "false"));
        }
        return leaf;
    }

    private void buildIndex(String leaf, String index, String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.createBuilderHnswIndexConcurrently(su, "nexus", leaf, index, COLUMN, collection,
                PciBuilderSession.BUILD_M, PciBuilderSession.BUILD_EF_CONSTRUCTION);
        }
    }

    private void dropIndex(String index) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // jOOQ renders DROP INDEX without the schema, so the index is found through the connection's search_path.
            ctx.select(DSL.function("set_config", SQLDataType.VARCHAR, DSL.val("search_path"), DSL.val("nexus"),
                DSL.val(false))).fetch();
            ctx.dropIndex(DSL.name("nexus", index)).execute();
        }
    }

    private void vacuum(String leaf) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.vacuumTable(su, "nexus", leaf);
        }
    }

    private Reading read(String scenario, String stage, String leaf, String index, String collection, long expectedRows,
                         Reading base, double predicted, double seconds) throws Exception {
        return read(scenario, stage, leaf, index, collection, expectedRows, base, predicted, seconds, null);
    }

    /**
     * Read the index size, the collection's row count and the leaf's dead-tuple count, record the row and return it.
     * {@code deadExpectation} (when given) is polled for up to 15 s, because {@code n_dead_tup} arrives from the
     * statistics system after the writing session goes idle; the last value read is the one recorded either way.
     */
    private Reading read(String scenario, String stage, String leaf, String index, String collection, long expectedRows,
                         Reading base, double predicted, double seconds, LongPredicate deadExpectation) throws Exception {
        long bytes;
        long rows;
        long dead = -1;
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            Field<Long> size = DSL.function(DSL.name("pg_catalog", "pg_relation_size"), SQLDataType.BIGINT,
                DSL.field(DSL.name("c", "oid")));
            bytes = ctx.select(size)
                .from(DSL.table(DSL.name("pg_catalog", "pg_class")).as("c"))
                .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n"))
                .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
                .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
                .and(DSL.field(DSL.name("c", "relname"), String.class).eq(index))
                .and(DSL.field(DSL.name("c", "relkind"), String.class).eq("i"))
                .fetchOne(0, Long.class);
            rows = ctx.selectCount().from(CHUNKS).where(CHUNKS.COLLECTION.eq(collection))
                .fetchOne(0, Long.class);
        }
        if (rows != expectedRows) {
            throw new IllegalStateException(stage + ": the collection holds " + rows + " rows, the harness expected "
                + expectedRows);
        }
        long deadline = System.nanoTime() + 15_000_000_000L;
        while (true) {
            dead = deadTuples(leaf);
            if (deadExpectation == null || deadExpectation.test(dead) || System.nanoTime() > deadline) {
                break;
            }
            Thread.sleep(500);
        }
        double ratio = base == null ? 1.0 : ((double) bytes / rows) / base.bytesPerRow();
        Reading reading = new Reading(scenario, stage, rows, bytes, dead, ratio, predicted, seconds);
        readings.add(reading);
        System.out.printf(Locale.ROOT, "A3-MEASURE %s | %s | rows %d | bytes %d | pages %d | bpr %.1f | dead %d | ratio %.4f"
            + " | predicted %.3f | %.1f s%n", scenario, stage, rows, bytes, reading.pages(), reading.bytesPerRow(), dead,
            ratio, predicted, seconds);
        return reading;
    }

    private long deadTuples(String leaf) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            Long dead = DSL.using(su, SQLDialect.POSTGRES)
                .select(DSL.field(DSL.name("s", "n_dead_tup"), SQLDataType.BIGINT))
                .from(DSL.table(DSL.name("pg_catalog", "pg_stat_user_tables")).as("s"))
                .where(DSL.field(DSL.name("s", "schemaname"), String.class).eq("nexus"))
                .and(DSL.field(DSL.name("s", "relname"), String.class).eq(leaf))
                .fetchOne(0, Long.class);
            return dead == null ? -1 : dead;
        }
    }

    /** The server and extension versions and the index definition, so the record states what was measured. */
    private void recordShape(String index) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            notes.add("server: " + ctx.select(DSL.function("version", SQLDataType.VARCHAR)).fetchOne(0, String.class));
            notes.add("pgvector: " + ctx.select(DSL.field(DSL.name("extversion"), String.class))
                .from(DSL.table(DSL.name("pg_catalog", "pg_extension")))
                .where(DSL.field(DSL.name("extname"), String.class).eq("vector")).fetchOne(0, String.class));
            notes.add("maintenance_work_mem: " + ctx.select(DSL.function("current_setting", SQLDataType.VARCHAR,
                DSL.val("maintenance_work_mem"))).fetchOne(0, String.class));
            notes.add("index definition: " + ctx.select(DSL.function(DSL.name("pg_catalog", "pg_get_indexdef"),
                    SQLDataType.CLOB, DSL.field(DSL.name("c", "oid"))))
                .from(DSL.table(DSL.name("pg_catalog", "pg_class")).as("c"))
                .where(DSL.field(DSL.name("c", "relname"), String.class).eq(index))
                .fetchOne(0, String.class));
        }
    }

    // -- the record ----------------------------------------------------------------------------------------

    private static double secondsSince(long nanoStart) {
        return (System.nanoTime() - nanoStart) / 1e9;
    }

    private void writeResults(double totalSeconds) throws IOException {
        StringBuilder out = new StringBuilder();
        out.append("# RDR-230 P1.5 (nexus-4lnn1.1.6) A3 measurement\n\n");
        out.append(String.format(Locale.ROOT, "N %d (2N %d), seed %d, dimension %d, model %s, m %d, ef_construction %d,"
                + " autovacuum off on the leaf, tolerance %.0f%% of the predicted ratio, total %.0f s%n%n", N, 2 * N, SEED,
            DIM, MODEL, PciBuilderSession.BUILD_M, PciBuilderSession.BUILD_EF_CONSTRUCTION, TOLERANCE * 100,
            totalSeconds));
        for (String note : notes) {
            out.append("- ").append(note).append('\n');
        }
        out.append("\n| scenario | stage | rows | bytes | pages | bytes/row | n_dead_tup | ratio | predicted | held |"
            + " step s |\n|---|---|---|---|---|---|---|---|---|---|---|\n");
        for (Reading r : readings) {
            boolean has = !Double.isNaN(r.predicted());
            boolean held = has && Math.abs(r.ratio() / r.predicted() - 1) <= TOLERANCE;
            out.append(String.format(Locale.ROOT, "| %s | %s | %d | %d | %d | %.1f | %d | %.4f | %s | %s | %.1f |%n",
                r.scenario(), r.stage(), r.rows(), r.bytes(), r.pages(), r.bytesPerRow(), r.deadTup(), r.ratio(),
                has ? String.format(Locale.ROOT, "%.3f", r.predicted()) : "-", has ? (held ? "yes" : "NO") : "-",
                r.seconds()));
        }
        Path file = Path.of("target", "a3-measure-results.md");
        Files.createDirectories(file.getParent());
        Files.writeString(file, out.toString());
        System.out.println("A3-MEASURE results written to " + file.toAbsolutePath());
        System.out.println(out);
    }
}
