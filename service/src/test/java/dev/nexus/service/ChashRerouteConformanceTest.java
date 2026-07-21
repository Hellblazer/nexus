package dev.nexus.service;

import dev.nexus.service.db.Chash;
import dev.nexus.service.db.ChashRepository;
import dev.nexus.service.db.TenantScope;
import org.testcontainers.containers.PostgreSQLContainer;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.sql.Connection;
import java.sql.ResultSet;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-187 bead nexus-piwya.2 — the superset-conformance harness that GATES the
 * step-2 reroute (nexus-piwya.3): before /v1/chash/* lookups may be
 * reimplemented over the chunks tables, the 3-table probe must be proven to
 * agree with the router on everything the router knows that is REAL, and to
 * strictly improve on what the router never knew.
 *
 * <p>The contract, row-level (NOT chash-level — production carries
 * partial-orphan chashes that are live in one collection with a dangling
 * router row in another):
 * <ol>
 *   <li><b>Exact agreement on live rows</b>: for every chash the router
 *       returns, the probe's {@code (collection, created_at)} rows equal the
 *       router's rows restricted to collections where the chunk actually
 *       exists.</li>
 *   <li><b>Expected divergence on orphan rows</b>: router rows with no
 *       backing chunk (the 292,230-row class that dies at the DROP,
 *       nexus-piwya.9) are NOT resolved by the probe — asserted explicitly,
 *       with exact counts, so the divergence is a documented property rather
 *       than a silent one.</li>
 *   <li><b>Strict superset on reference-only chunks</b>: RDR-169
 *       reference-only chunks land in {@code chunks_<dim>} via the engine
 *       bridge with no dual-write router row. The probe resolves them, the
 *       router cannot — asserted as an improvement, which is why the reroute
 *       conformance claim is SUPERSET, deliberately not identity.</li>
 *   <li><b>created_at agreement</b>: chunks.created_at is
 *       first-insert-per-(tenant,collection,chash) — both upsert ON CONFLICT
 *       set-lists exclude it — so it carries the identical "when this chash
 *       entered this collection" semantics as router.created_at
 *       (RDR-187 research finding 1).</li>
 *   <li><b>Tenant isolation</b>: the probe under RLS sees only the probing
 *       tenant's rows.</li>
 * </ol>
 *
 * <p>PROBE_SQL below is the contract shape nexus-piwya.3 implements
 * engine-side. This whole class necessarily references {@code chash_index}
 * (it compares against the router), so it is retired WITH the router at
 * nexus-piwya.9 — the .9 inverse-grep gate will catch it; that is by design.
 *
 * <p>Hermetic: Testcontainers pgvector, requires Docker.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ChashRerouteConformanceTest {

    /**
     * The reroute probe: which collections hold this chash for the current
     * tenant, with the first-insert timestamp. Three PK-disjoint tables, so
     * UNION ALL (a collection lives in exactly one dim table; no duplicate
     * (collection, chash) pairs are possible across legs). Runs under
     * TenantScope RLS; the explicit tenant_id predicate binds the leading
     * column of idx_chunks_<dim>_tenant_chash (nexus-piwya.1).
     */
    private static final String PROBE_SQL =
        "SELECT collection, created_at FROM nexus.chunks_384 " +
        " WHERE tenant_id = current_setting('nexus.tenant', true) AND chash = ? " +
        "UNION ALL " +
        "SELECT collection, created_at FROM nexus.chunks_768 " +
        " WHERE tenant_id = current_setting('nexus.tenant', true) AND chash = ? " +
        "UNION ALL " +
        "SELECT collection, created_at FROM nexus.chunks_1024 " +
        " WHERE tenant_id = current_setting('nexus.tenant', true) AND chash = ?";

    private static final String TENANT_A = "conf-tenant-a";
    private static final String TENANT_B = "conf-tenant-b";

    private static final String COLL_384_A  = "conf-coll-384-a";
    private static final String COLL_768_A  = "conf-coll-768-a";
    private static final String COLL_1024_A = "conf-coll-1024-a";
    private static final String COLL_1024_A2 = "conf-coll-1024-a2";
    private static final String COLL_384_B  = "conf-coll-384-b";

    // Live in router AND chunks (exact-agreement class).
    private static final String H1 = hex("live-384");
    private static final String H2 = hex("live-768");
    private static final String H3 = hex("live-1024");
    /** Multi-collection: live in COLL_384_A and COLL_1024_A2. */
    private static final String H4 = hex("live-multi");
    // Router-only rows, no chunk anywhere (full-orphan class, dies at .9).
    private static final String H5 = hex("orphan-1");
    private static final String H6 = hex("orphan-2");
    // Chunks-only (RDR-169 reference-only class; superset side).
    private static final String H7 = hex("ref-only-768");
    private static final String H8 = hex("ref-only-1024");
    /** Partial orphan: live in COLL_384_A, dangling router row in COLL_768_A. */
    private static final String H9 = hex("partial-orphan");

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    ChashRepository router;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; " +
                "  END IF; " +
                "END $$");
        }

        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db)
                .update(new Contexts());
        }

        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        router = new ChashRepository(tenantScope);

        seedFixtures();
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null)    pg.stop();
    }

    private void seedFixtures() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            for (String[] tc : new String[][] {
                    {TENANT_A, COLL_384_A}, {TENANT_A, COLL_768_A},
                    {TENANT_A, COLL_1024_A}, {TENANT_A, COLL_1024_A2},
                    {TENANT_B, COLL_384_B}}) {
                su.createStatement().execute(
                    "INSERT INTO nexus.catalog_collections (tenant_id, name) " +
                    "VALUES ('" + tc[0] + "', '" + tc[1] + "') " +
                    "ON CONFLICT (tenant_id, name) DO NOTHING");
            }

            // Live rows: chunk + router row with the SAME created_at (the
            // dual-write hook wrote both sides of one index pass; research
            // finding 1 pins the semantics as identical).
            chunk(su, TENANT_A, 384,  COLL_384_A,  H1, ts(1));
            routerRow(su, TENANT_A,   COLL_384_A,  H1, ts(1));
            chunk(su, TENANT_A, 768,  COLL_768_A,  H2, ts(2));
            routerRow(su, TENANT_A,   COLL_768_A,  H2, ts(2));
            chunk(su, TENANT_A, 1024, COLL_1024_A, H3, ts(3));
            routerRow(su, TENANT_A,   COLL_1024_A, H3, ts(3));

            // Multi-collection membership across two dim tables.
            chunk(su, TENANT_A, 384,  COLL_384_A,   H4, ts(4));
            routerRow(su, TENANT_A,   COLL_384_A,   H4, ts(4));
            chunk(su, TENANT_A, 1024, COLL_1024_A2, H4, ts(5));
            routerRow(su, TENANT_A,   COLL_1024_A2, H4, ts(5));

            // Full orphans: router rows with no chunk anywhere.
            routerRow(su, TENANT_A, COLL_384_A, H5, ts(6));
            routerRow(su, TENANT_A, COLL_768_A, H6, ts(7));

            // Reference-only chunks: no router row (RDR-169 bridge path).
            chunk(su, TENANT_A, 768,  COLL_768_A,  H7, ts(8));
            chunk(su, TENANT_A, 1024, COLL_1024_A, H8, ts(9));

            // Partial orphan: live in 384, dangling router row in 768.
            chunk(su, TENANT_A, 384, COLL_384_A, H9, ts(10));
            routerRow(su, TENANT_A,  COLL_384_A, H9, ts(10));
            routerRow(su, TENANT_A,  COLL_768_A, H9, ts(11));

            // Tenant B: same chash H1 in its own collection — isolation.
            chunk(su, TENANT_B, 384, COLL_384_B, H1, ts(12));
            routerRow(su, TENANT_B,  COLL_384_B, H1, ts(12));
        }
    }

    // ── 1 + 2. Row-level agreement over the router's whole universe ─────────

    @Test
    void probeAgreesExactlyOnLiveRowsAndDropsExactlyTheOrphanRows() {
        // Enumerate EVERY router row for tenant A (superuser side), probe each
        // distinct chash, and classify row-by-row. No sampling: the whole
        // seeded universe is walked and the counts are exact.
        List<String[]> routerRows = allRouterRows(TENANT_A);
        assertThat(routerRows).hasSize(9); // 6 live + 2 full-orphan + 1 partial-orphan leg

        Set<String> liveRows = new TreeSet<>();
        Set<String> orphanRows = new TreeSet<>();
        Set<String> probedChashes = new HashSet<>();
        for (String[] row : routerRows) {
            String chashHex = row[0];
            if (!probedChashes.add(chashHex)) continue;
            Set<String> probe = probeRows(TENANT_A, chashHex);
            List<Map<String, String>> viaRouter =
                router.lookup(TENANT_A, Chash.fromHex(chashHex));
            for (Map<String, String> r : viaRouter) {
                String key = chashHex + "|" + r.get("collection") + "|" + r.get("created_at");
                if (probe.contains(r.get("collection") + "|" + r.get("created_at"))) {
                    liveRows.add(key);
                } else {
                    orphanRows.add(key);
                }
            }
        }

        assertThat(liveRows)
            .as("live router rows (chunk exists) — probe must return every one")
            .hasSize(6);
        assertThat(orphanRows)
            .as("orphan router rows — probe must NOT resolve them; they die at nexus-piwya.9")
            .containsExactlyInAnyOrder(
                H5 + "|" + COLL_384_A + "|" + fmt(ts(6)),
                H6 + "|" + COLL_768_A + "|" + fmt(ts(7)),
                H9 + "|" + COLL_768_A + "|" + fmt(ts(11)));
    }

    @Test
    void probeMatchesRouterExactlyForFullyLiveChashes() {
        for (String chashHex : List.of(H1, H2, H3, H4)) {
            Set<String> viaRouter = new TreeSet<>();
            for (Map<String, String> r : router.lookup(TENANT_A, Chash.fromHex(chashHex))) {
                viaRouter.add(r.get("collection") + "|" + r.get("created_at"));
            }
            assertThat(probeRows(TENANT_A, chashHex))
                .as("probe == router for fully-live chash %s", chashHex)
                .isEqualTo(viaRouter);
        }
    }

    @Test
    void partialOrphanChashResolvesOnlyItsLiveCollection() {
        assertThat(probeRows(TENANT_A, H9))
            .as("partial-orphan chash: live 384 row kept, dangling 768 row dropped")
            .containsExactly(COLL_384_A + "|" + fmt(ts(10)));
        assertThat(router.lookup(TENANT_A, Chash.fromHex(H9)))
            .as("router still returns both rows (the dangling one included)")
            .hasSize(2);
    }

    // ── 3. Strict superset: reference-only chunks resolve post-reroute ──────

    @Test
    void probeResolvesReferenceOnlyChunksTheRouterNeverKnew() {
        assertThat(router.lookup(TENANT_A, Chash.fromHex(H7))).isEmpty();
        assertThat(router.lookup(TENANT_A, Chash.fromHex(H8))).isEmpty();
        assertThat(probeRows(TENANT_A, H7))
            .containsExactly(COLL_768_A + "|" + fmt(ts(8)));
        assertThat(probeRows(TENANT_A, H8))
            .containsExactly(COLL_1024_A + "|" + fmt(ts(9)));
    }

    @Test
    void probeCompletenessOverEveryChunkRow() {
        // Every chunk row for tenant A must be reachable through the probe —
        // this is the assertion that fails if a UNION leg goes missing.
        int walked = 0;
        for (int dim : new int[] {384, 768, 1024}) {
            for (String[] row : allChunkRows(TENANT_A, dim)) {
                assertThat(probeRows(TENANT_A, row[0]))
                    .as("chunks_%d row (%s, %s) must be probe-reachable", dim, row[0], row[1])
                    .contains(row[1] + "|" + row[2]);
                walked++;
            }
        }
        assertThat(walked).as("seeded chunk rows walked").isEqualTo(8);
    }

    // ── 5. Tenant isolation under RLS ───────────────────────────────────────

    @Test
    void probeIsTenantIsolated() {
        assertThat(probeRows(TENANT_A, H1))
            .containsExactly(COLL_384_A + "|" + fmt(ts(1)));
        assertThat(probeRows(TENANT_B, H1))
            .containsExactly(COLL_384_B + "|" + fmt(ts(12)));
    }

    // ── helpers ─────────────────────────────────────────────────────────────

    /** Probe under RLS via TenantScope; rows as "collection|created_at". */
    private Set<String> probeRows(String tenant, String chashHex) {
        byte[] bytes = Chash.fromHex(chashHex).toBytes();
        return tenantScope.withTenant(tenant, ctx -> {
            Set<String> out = new TreeSet<>();
            for (var r : ctx.resultQuery(PROBE_SQL, bytes, bytes, bytes).fetch()) {
                OffsetDateTime t = r.get("created_at", OffsetDateTime.class);
                out.add(r.get("collection", String.class) + "|"
                        + ChashRepository.UTC_SECOND.format(t));
            }
            return out;
        });
    }

    /** All router rows for a tenant, superuser-side: [chashHex, collection, created_at]. */
    private List<String[]> allRouterRows(String tenant) {
        return rows("SELECT encode(chash, 'hex'), physical_collection, created_at " +
                    "FROM nexus.chash_index WHERE tenant_id = '" + tenant + "'");
    }

    /** All chunk rows for a tenant in one dim table: [chashHex, collection, created_at]. */
    private List<String[]> allChunkRows(String tenant, int dim) {
        return rows("SELECT encode(chash, 'hex'), collection, created_at " +
                    "FROM nexus.chunks_" + dim + " WHERE tenant_id = '" + tenant + "'");
    }

    private List<String[]> rows(String sql) {
        try (Connection su = pg.createConnection("")) {
            List<String[]> out = new ArrayList<>();
            try (ResultSet rs = su.createStatement().executeQuery(sql)) {
                while (rs.next()) {
                    OffsetDateTime t = rs.getObject(3, OffsetDateTime.class);
                    out.add(new String[] {rs.getString(1), rs.getString(2),
                                          ChashRepository.UTC_SECOND.format(t)});
                }
            }
            return out;
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }

    private void chunk(Connection su, String tenant, int dim, String collection,
                       String chashHex, String createdAt) throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.chunks_" + dim +
            " (tenant_id, collection, chash, chunk_text, embedding, created_at) VALUES " +
            "('" + tenant + "', '" + collection + "', decode('" + chashHex + "', 'hex'), " +
            "'conf chunk " + chashHex.substring(0, 8) + "', " + unitVec(dim) + "::vector, " +
            "TIMESTAMPTZ '" + createdAt + "')");
    }

    private void routerRow(Connection su, String tenant, String collection,
                           String chashHex, String createdAt) throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.chash_index (tenant_id, chash, physical_collection, created_at) " +
            "VALUES ('" + tenant + "', decode('" + chashHex + "', 'hex'), " +
            "'" + collection + "', TIMESTAMPTZ '" + createdAt + "')");
    }

    private static String unitVec(int dim) {
        StringBuilder sb = new StringBuilder("'[1");
        for (int i = 1; i < dim; i++) sb.append(",0");
        return sb.append("]'").toString();
    }

    /** Deterministic 64-hex chash from a label (sha256 of the label text). */
    private static String hex(String label) {
        return Chash.ofText(label).toHex();
    }

    private static String ts(int second) {
        return String.format("2026-07-01 00:00:%02d+00", second);
    }

    private static String fmt(String seededTs) {
        // "2026-07-01 00:00:NN+00" -> "2026-07-01T00:00:NNZ" (UTC_SECOND form)
        return seededTs.substring(0, 10) + "T" + seededTs.substring(11, 19) + "Z";
    }
}
