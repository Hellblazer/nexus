package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TenantScope;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.stream.Collectors;
import java.util.stream.IntStream;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-155 P1.1 (bead nexus-s7crg): RLS behavioral suite for pgvector {@code chunks_<dim>} tables.
 *
 * <p><strong>TDD-RED: these tests are intentionally red until bead nexus-mf447 lands the
 * Liquibase changesets</strong> that create {@code nexus.chunks_384}, {@code nexus.chunks_768},
 * and {@code nexus.chunks_1024}. Every failure before nexus-mf447 ships should report
 * "relation nexus.chunks_NNN does not exist" (or the equivalent permission error when the
 * GRANT runs before the table exists). Compile must be green; runtime failures are expected.
 *
 * <p><strong>Role discipline (bead nexus-5j7pb rationale):</strong> prior auth tests were
 * vacuous because they ran as a superuser or BYPASSRLS role that bypasses all RLS policies.
 * Every behavioral assertion in this suite runs as {@code svc_chunks_test}, a plain LOGIN role
 * that is NOSUPERUSER, is NOT the table owner, and has NO BYPASSRLS attribute. The superuser
 * connection is used only for schema setup and as a control to prove rows exist when RLS
 * should be hiding them.
 *
 * <p>Schema contract tested (P1.2 must implement exactly these columns and constraints):
 * <ul>
 *   <li>Table: {@code nexus.chunks_384}, {@code nexus.chunks_768}, {@code nexus.chunks_1024}
 *   <li>Column {@code tenant_id} TEXT NOT NULL
 *   <li>Column {@code collection} TEXT NOT NULL
 *   <li>Column {@code chash} TEXT NOT NULL
 *   <li>Column {@code chunk_text} TEXT NOT NULL
 *   <li>Column {@code embedding} {@code vector(<dim>)} NOT NULL
 *   <li>Primary key: {@code (tenant_id, collection, chash)}
 *   <li>RLS FORCE, policy: {@code USING (tenant_id = current_setting('nexus.tenant', true))}
 *   <li>WITH CHECK policy: {@code (tenant_id = current_setting('nexus.tenant', true))}
 *   <li>GUC: {@code nexus.tenant} (SET LOCAL via {@link TenantScope#withTenant})
 * </ul>
 *
 * <p>Deliberately NOT exercised here: the generated tsvector column and the HNSW index
 * (both part of the P1.2 schema per RDR-155 Approach, exercised by Phase 3 hybrid-search
 * tests). The P1.G gate (nexus-cj7qu) must verify both are present in the landed schema
 * even though this suite does not touch them.
 *
 * <p>Hermetic: Testcontainers pgvector/pgvector:pg17, PER_CLASS lifecycle.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ChunksRlsBehavioralTest {

    // Dimensions under test (chunks_384, chunks_768, chunks_1024).
    private static final int[] DIMS = {384, 768, 1024};

    // Plain-LOGIN service role: NOSUPERUSER, NOT table owner, NO BYPASSRLS.
    private static final String SVC_ROLE = "svc_chunks_test";
    private static final String SVC_PASS = "svc_chunks_test_pass";

    private static final String TENANT_A = "tenant-a";
    private static final String TENANT_B = "tenant-b";

    // Distinct collection names to avoid cross-test pollution on the PK.
    private static final String COL_A = "code__owner-a__voyage-code-3__v1";
    private static final String COL_B = "knowledge__owner-b__voyage-context-3__v1";

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    HikariDataSource svcDs;

    // Single-connection Hikari pool for the SET-LOCAL-over-pooler leak test.
    HikariDataSource leakDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        // --- Step 1: create roles before Liquibase runs (changeset DO-blocks need them).
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN " +
                // Explicit NOSUPERUSER NOBYPASSRLS: this suite exists to catch vacuous
                // RLS coverage (nexus-5j7pb); relying on PG defaults would let a future
                // copy-paste edit silently hollow out every behavioral assertion.
                "    CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "' NOSUPERUSER NOBYPASSRLS; " +
                "  END IF; " +
                "END $$");
            // nexus_svc required by changeset 5 grant DO-block.
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; " +
                "  END IF; " +
                "END $$");
        }

        // --- Step 2: apply Liquibase changelog via superuser.
        //     After nexus-mf447 lands this will also CREATE the chunks tables and their
        //     RLS policies. Before that bead, the changelog stops before chunks are created,
        //     so everything below that touches chunks_NNN will fail with "relation does not
        //     exist" -- which is the expected RED state for this bead.
        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            Liquibase liquibase = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                db);
            liquibase.update(new Contexts());
        }

        // --- Step 3: grant nexus schema access + chunks-table DML to the svc role.
        //     These GRANTs will throw "relation nexus.chunks_NNN does not exist" until
        //     nexus-mf447 creates the tables. That is the intentional RED boundary: the
        //     Liquibase apply above (which does NOT create chunks tables yet) succeeds;
        //     the chunks-specific grants fail here and propagate through @BeforeAll so
        //     every @Test in this class is marked as failing for the right reason.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            for (int dim : DIMS) {
                su.createStatement().execute(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.chunks_" + dim + " TO " + SVC_ROLE);
            }
            // RDR-156 P0.2: insertChunk now auto-stubs catalog_collections before chunk writes.
            su.createStatement().execute(
                "GRANT SELECT, INSERT ON nexus.catalog_collections TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }

        // --- Step 4: build the svc-role Hikari pool used by TenantScope.
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);

        // --- Step 5: build a single-connection pool for the SET-LOCAL leak test.
        var leakCfg = new HikariConfig();
        leakCfg.setJdbcUrl(pg.getJdbcUrl());
        leakCfg.setUsername(SVC_ROLE);
        leakCfg.setPassword(SVC_PASS);
        leakCfg.setMaximumPoolSize(1);  // forces reuse of the same physical connection
        leakCfg.setAutoCommit(true);
        leakDs = new HikariDataSource(leakCfg);
    }

    @AfterAll
    void stopAll() {
        if (leakDs != null) leakDs.close();
        if (svcDs  != null) svcDs.close();
        if (pg     != null) pg.stop();
    }

    // ---------------------------------------------------------------------------
    // Behavior A: fail-closed default
    //
    // A connection that has never set nexus.tenant (no GUC stamp) must see zero
    // rows even though the rows exist. The superuser control confirms the rows are
    // physically present (not empty table) before we assert the svc-role count.
    // ---------------------------------------------------------------------------

    @ParameterizedTest
    @ValueSource(ints = {384, 768, 1024})
    void failClosed_noGucStamp_seesZeroRows(int dim) throws Exception {
        String table = "nexus.chunks_" + dim;

        // Seed 2 rows as tenant-a via TenantScope (SET LOCAL stamped, txn-local).
        insertChunk(dim, TENANT_A, COL_A, "chash-fc-1-" + dim, "text one", dim);
        insertChunk(dim, TENANT_A, COL_A, "chash-fc-2-" + dim, "text two", dim);

        // Superuser count: prove the rows are physically there (not empty table).
        // Scoped to COL_A so sibling parameterized tests' rows in the same dim
        // table cannot inflate the count (JUnit method ordering is undefined).
        long suCount = superuserCount(table, COL_A);
        assertThat(suCount)
            .as("superuser must see the 2 seeded rows (rows exist, RLS is the guard)")
            .isEqualTo(2L);

        // Svc-role connection WITHOUT any GUC stamp: borrow a raw connection from the
        // pool and query without entering TenantScope (no set_config call).
        long unstampedCount = unstampedSvcCount(table, COL_A);
        assertThat(unstampedCount)
            .as("unstamped svc-role connection must see 0 rows (fail-closed RLS)")
            .isEqualTo(0L);
    }

    // ---------------------------------------------------------------------------
    // Behavior B: cross-tenant SELECT isolation
    //
    // tenant-b's withTenant call must yield 0 rows of tenant-a's data.
    // tenant-a's own count must be exact (not inflated by B's data).
    // ---------------------------------------------------------------------------

    @ParameterizedTest
    @ValueSource(ints = {384, 768, 1024})
    void crossTenantSelect_tenantBSeesOnlyOwnRows(int dim) {
        String table = "nexus.chunks_" + dim;
        String col = "knowledge__isolation__ctx3__v1";  // unique collection per test

        // Seed 2 rows for tenant-a and 1 row for tenant-b (distinct chash values).
        insertChunk(dim, TENANT_A, col, "chash-iso-a1-" + dim, "a content 1", dim);
        insertChunk(dim, TENANT_A, col, "chash-iso-a2-" + dim, "a content 2", dim);
        insertChunk(dim, TENANT_B, col, "chash-iso-b1-" + dim, "b content 1", dim);

        // tenant-b must see exactly its 1 row.
        long bCount = tenantCount(dim, TENANT_B, col);
        assertThat(bCount)
            .as("tenant-b must see exactly its own 1 row in " + table)
            .isEqualTo(1L);

        // tenant-a must see exactly its 2 rows (not 3).
        long aCount = tenantCount(dim, TENANT_A, col);
        assertThat(aCount)
            .as("tenant-a must see exactly its own 2 rows in " + table)
            .isEqualTo(2L);
    }

    // ---------------------------------------------------------------------------
    // Behavior C: cross-tenant INSERT / UPDATE blocked by WITH CHECK
    //
    // Inside a withTenant("tenant-a") scope, an INSERT with tenant_id='tenant-b'
    // must be rejected (SQLState 42501 or message contains "row-level security").
    // An UPDATE that tries to set tenant_id='tenant-b' on an own row must also fail.
    // Post-state counts must be unchanged from both tenants' views.
    // ---------------------------------------------------------------------------

    @ParameterizedTest
    @ValueSource(ints = {384, 768, 1024})
    void crossTenantInsert_blockedByWithCheck(int dim) {
        String table = "nexus.chunks_" + dim;
        String col = "code__withcheck__vc3__v1";
        String vec = vectorLiteral(dim);

        // Seed a legitimate row for tenant-a.
        insertChunk(dim, TENANT_A, col, "chash-wc-own-" + dim, "own row", dim);

        // tenant-a count before the blocked INSERT.
        long aCountBefore = tenantCount(dim, TENANT_A, col);
        assertThat(aCountBefore).as("baseline count for tenant-a before blocked INSERT").isEqualTo(1L);
        long bCountBefore = tenantCount(dim, TENANT_B, col);
        assertThat(bCountBefore).as("baseline count for tenant-b before blocked INSERT").isEqualTo(0L);

        // Attempt cross-tenant INSERT inside tenant-a's scope: must throw.
        assertThatThrownBy(() ->
            tenantScope.withTenant(TENANT_A, ctx -> {
                ctx.execute(
                    "INSERT INTO " + table + " (tenant_id, collection, chash, chunk_text, embedding) " +
                    "VALUES (?, ?, decode(?, 'hex'), ?, ?::vector)",
                    TENANT_B, col, padChash("chash-wc-cross-" + dim), "cross-tenant inject", vec);
                return null;
            })
        ).as("INSERT with tenant_id='tenant-b' inside tenant-a scope must be rejected by RLS WITH CHECK")
         .satisfies(ex -> {
             Throwable root = rootCause(ex);
             assertThat(sqlState(root) + " " + root.getMessage())
                 .as("rejection must be SQLState 42501 or mention row-level security")
                 .satisfiesAnyOf(
                     s -> assertThat(s).startsWith("42501"),
                     s -> assertThat(s).containsIgnoringCase("row-level security")
                 );
         });

        // Counts must be exactly unchanged (no row was committed).
        assertThat(tenantCount(dim, TENANT_A, col))
            .as("tenant-a count must be unchanged after blocked INSERT")
            .isEqualTo(aCountBefore);
        assertThat(tenantCount(dim, TENANT_B, col))
            .as("tenant-b count must be unchanged after blocked INSERT")
            .isEqualTo(bCountBefore);
    }

    @ParameterizedTest
    @ValueSource(ints = {384, 768, 1024})
    void crossTenantUpdate_blockedByWithCheck(int dim) {
        String table = "nexus.chunks_" + dim;
        String col = "code__withcheck-upd__vc3__v1";

        // Seed a legitimate row for tenant-a.
        insertChunk(dim, TENANT_A, col, "chash-wcu-own-" + dim, "own row for update", dim);

        long aCountBefore = tenantCount(dim, TENANT_A, col);
        assertThat(aCountBefore).as("baseline count for tenant-a before blocked UPDATE").isEqualTo(1L);
        long bCountBefore = tenantCount(dim, TENANT_B, col);
        assertThat(bCountBefore).as("baseline count for tenant-b before blocked UPDATE").isEqualTo(0L);

        // Attempt UPDATE that would change tenant_id to tenant-b (still inside tenant-a scope).
        assertThatThrownBy(() ->
            tenantScope.withTenant(TENANT_A, ctx -> {
                ctx.execute(
                    "UPDATE " + table + " SET tenant_id = ? WHERE chash = decode(?, 'hex') AND collection = ?",
                    TENANT_B, padChash("chash-wcu-own-" + dim), col);
                return null;
            })
        ).as("UPDATE setting tenant_id to tenant-b inside tenant-a scope must be rejected by RLS WITH CHECK")
         .satisfies(ex -> {
             Throwable root = rootCause(ex);
             assertThat(sqlState(root) + " " + root.getMessage())
                 .as("rejection must be SQLState 42501 or mention row-level security")
                 .satisfiesAnyOf(
                     s -> assertThat(s).startsWith("42501"),
                     s -> assertThat(s).containsIgnoringCase("row-level security")
                 );
         });

        // Exactly the same number of rows remain for tenant-a.
        assertThat(tenantCount(dim, TENANT_A, col))
            .as("tenant-a count must be unchanged after blocked UPDATE")
            .isEqualTo(aCountBefore);
        // tenant-b still has zero.
        assertThat(tenantCount(dim, TENANT_B, col))
            .as("tenant-b count must remain 0 after blocked UPDATE attempt")
            .isEqualTo(0L);
    }

    // ---------------------------------------------------------------------------
    // Behavior D: SET LOCAL-over-pooler leak case
    //
    // Uses a maximumPoolSize=1 pool so the same physical connection is reused
    // across two sequential borrows. After withTenant("tenant-a") commits and
    // returns the connection to the pool, the next borrower must see:
    //   - current_setting('nexus.tenant', true) IS NULL or empty string
    //   - SELECT count(*) FROM nexus.chunks_<dim> == 0  (fail-closed, no GUC)
    //
    // This proves SET LOCAL (is_local=true in set_config) does not bleed across
    // the pool boundary when the transaction commits and autoCommit is restored.
    // ---------------------------------------------------------------------------

    @ParameterizedTest
    @ValueSource(ints = {384, 768, 1024})
    void setLocalOverPooler_gucDoesNotBleedToNextBorrower(int dim) throws Exception {
        String table = "nexus.chunks_" + dim;
        String col = "code__leak-probe__vc3__v1";
        TenantScope leakScope = new TenantScope(leakDs);

        // Seed a row so we have something that would show if the GUC leaked.
        insertChunk(dim, TENANT_A, col, "chash-leak-" + dim, "leak probe row", dim);

        // First borrow: stamp tenant-a via withTenant (SET LOCAL, commits, returns conn).
        leakScope.withTenant(TENANT_A, ctx -> {
            // Verify the GUC is active during this txn (sanity check).
            var result = ctx.fetch("SELECT current_setting(?, true) AS guc",
                                   TenantConstants.GUC_NAME);
            assertThat(result.get(0).get("guc", String.class))
                .as("GUC must be set to tenant-a inside withTenant")
                .isEqualTo(TENANT_A);
            return null;
        });

        // Second borrow: raw connection from the same pool (maximumPoolSize=1 guarantees
        // it is the same physical connection). No GUC stamping occurs in this block.
        try (Connection conn = leakDs.getConnection()) {
            // GUC must be NULL or empty (SET LOCAL was txn-local; commit cleared it).
            String gucValue;
            try (PreparedStatement ps = conn.prepareStatement(
                    "SELECT current_setting(?, true) AS guc")) {
                ps.setString(1, TenantConstants.GUC_NAME);
                try (ResultSet rs = ps.executeQuery()) {
                    rs.next();
                    gucValue = rs.getString("guc");
                }
            }
            assertThat(gucValue == null || gucValue.isEmpty())
                .as("GUC must be NULL or empty on re-borrowed connection (no SET LOCAL bleed): got '" + gucValue + "'")
                .isTrue();

            // RLS must apply fail-closed on the unstamped connection.
            long leakCount;
            try (PreparedStatement ps = conn.prepareStatement(
                    "SELECT count(*) FROM " + table + " WHERE collection = ?")) {
                ps.setString(1, col);
                try (ResultSet rs = ps.executeQuery()) {
                    rs.next();
                    leakCount = rs.getLong(1);
                }
            }
            assertThat(leakCount)
                .as("Unstamped re-borrowed connection must see 0 rows in " + table + " (fail-closed)")
                .isEqualTo(0L);
        }
    }

    // ---------------------------------------------------------------------------
    // Behavior E: cross-tenant DELETE isolation (P1.G gate item, nexus-cj7qu)
    //
    // The tenant_isolation policy has no FOR clause, so it defaults to FOR ALL.
    // The USING expression makes tenant-a's rows invisible to a DELETE issued
    // inside tenant-b's scope: 0 rows affected, the row survives for its owner.
    // ---------------------------------------------------------------------------

    @ParameterizedTest
    @ValueSource(ints = {384, 768, 1024})
    void crossTenantDelete_deletesZeroRows_rowSurvives(int dim) {
        String table = "nexus.chunks_" + dim;
        String col = "code__delete-iso__vc3__v1";

        insertChunk(dim, TENANT_A, col, "chash-del-own-" + dim, "own row for delete", dim);
        assertThat(tenantCount(dim, TENANT_A, col))
            .as("baseline count for tenant-a before cross-tenant DELETE")
            .isEqualTo(1L);

        // tenant-b attempts to delete tenant-a's row: RLS USING hides it, 0 affected.
        int deleted = tenantScope.withTenant(TENANT_B, ctx ->
            ctx.execute("DELETE FROM " + table + " WHERE chash = decode(?, 'hex') AND collection = ?",
                        padChash("chash-del-own-" + dim), col));
        assertThat(deleted)
            .as("cross-tenant DELETE must affect exactly 0 rows (RLS makes the row invisible)")
            .isEqualTo(0);

        // The row must still exist for its owner.
        assertThat(tenantCount(dim, TENANT_A, col))
            .as("tenant-a's row must survive the cross-tenant DELETE attempt")
            .isEqualTo(1L);

        // The owner can delete its own row: exactly 1 affected.
        int ownDelete = tenantScope.withTenant(TENANT_A, ctx ->
            ctx.execute("DELETE FROM " + table + " WHERE chash = decode(?, 'hex') AND collection = ?",
                        padChash("chash-del-own-" + dim), col));
        assertThat(ownDelete)
            .as("owner DELETE must affect exactly 1 row")
            .isEqualTo(1);
        assertThat(tenantCount(dim, TENANT_A, col))
            .as("tenant-a count must be 0 after deleting its own row")
            .isEqualTo(0L);
    }

    // ---------------------------------------------------------------------------
    // Private helpers
    // ---------------------------------------------------------------------------

    /**
     * Full 64-lowercase-hex chash deterministically derived from a seed (RDR-180:
     * the chunks_&lt;dim&gt; chash column is bytea(32) now, CHECK octet_length=32 —
     * the pre-flip pad-to-32-char TEXT scheme is retired).
     */
    private static String padChash(String raw) {
        return dev.nexus.service.db.Chash.ofText(raw).toHex();
    }

    /**
     * Build a vector literal string of exactly {@code dim} components, all 0.1.
     * Format: {@code '[0.1,0.1,...,0.1]'} (pgvector cast-safe string).
     */
    private static String vectorLiteral(int dim) {
        return IntStream.range(0, dim)
                        .mapToObj(i -> "0.1")
                        .collect(Collectors.joining(",", "[", "]"));
    }

    /**
     * Insert one chunk row for the given tenant via {@link TenantScope#withTenant}.
     * The embedding vector is dim uniform 0.1 components.
     */
    private void insertChunk(int dim, String tenant, String collection, String chash,
                              String chunkText, int embDim) {
        String table = "nexus.chunks_" + dim;
        String vec = vectorLiteral(embDim);
        // RDR-156 P0.2: chash_len_check requires exactly 32 chars — pad test chashes.
        String paddedChash = padChash(chash);
        tenantScope.withTenant(tenant, ctx -> {
            // RDR-156 P0.2: ensure catalog_collections has a stub row before the chunk insert.
            ctx.execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES (?, ?) " +
                "ON CONFLICT (tenant_id, name) DO NOTHING",
                tenant, collection);
            ctx.execute(
                "INSERT INTO " + table +
                " (tenant_id, collection, chash, chunk_text, embedding)" +
                " VALUES (?, ?, decode(?, 'hex'), ?, ?::vector)" +
                " ON CONFLICT (tenant_id, collection, chash) DO NOTHING",
                tenant, collection, paddedChash, chunkText, vec);
            return null;
        });
    }

    /**
     * Count rows in {@code nexus.chunks_<dim>} visible to the given tenant,
     * scoped to {@code collection}.
     */
    private long tenantCount(int dim, String tenant, String collection) {
        String table = "nexus.chunks_" + dim;
        return tenantScope.withTenant(tenant, ctx ->
            ctx.fetchOne("SELECT count(*) FROM " + table + " WHERE collection = ?", collection)
               .get(0, Long.class));
    }

    /**
     * Count rows in {@code table} for one {@code collection} using the superuser
     * connection (bypasses RLS). Used as the control leg to prove rows exist when
     * RLS should be hiding them. Collection-scoped so rows deposited by sibling
     * parameterized tests in the same dim table cannot skew the exact count.
     */
    private long superuserCount(String table, String collection) throws SQLException {
        try (Connection su = pg.createConnection("");
             PreparedStatement ps = su.prepareStatement(
                 "SELECT count(*) FROM " + table + " WHERE collection = ?")) {
            ps.setString(1, collection);
            try (ResultSet rs = ps.executeQuery()) {
                rs.next();
                return rs.getLong(1);
            }
        }
    }

    /**
     * Count rows in {@code table} for one {@code collection} from a svc-role
     * connection that has no GUC stamp. Borrows directly from the pool without
     * entering {@link TenantScope}. RLS zeroes the result regardless of collection;
     * the scope keeps the helper's intent symmetric with {@link #superuserCount}.
     */
    private long unstampedSvcCount(String table, String collection) throws SQLException {
        try (Connection conn = svcDs.getConnection();
             PreparedStatement ps = conn.prepareStatement(
                 "SELECT count(*) FROM " + table + " WHERE collection = ?")) {
            ps.setString(1, collection);
            try (ResultSet rs = ps.executeQuery()) {
                rs.next();
                return rs.getLong(1);
            }
        }
    }

    /**
     * Walk the exception chain to the root cause.
     */
    private static Throwable rootCause(Throwable t) {
        while (t.getCause() != null) {
            t = t.getCause();
        }
        return t;
    }

    /**
     * Extract the five-character SQLState from a {@link java.sql.SQLException} chain,
     * or return an empty string if the exception is not a SQL exception.
     */
    private static String sqlState(Throwable t) {
        if (t instanceof java.sql.SQLException se) {
            return se.getSQLState() != null ? se.getSQLState() : "";
        }
        return "";
    }
}
