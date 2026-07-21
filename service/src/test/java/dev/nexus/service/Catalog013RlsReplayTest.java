package dev.nexus.service;

import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.Test;

import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.sql.Statement;
import java.util.HexFormat;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-1wjmq lineage — the FORCE-RLS Liquibase-DML blindness class, in the
 * RDR-180 era.
 *
 * <p>HISTORY: the 2026-07-08 v0.1.33 cloud incident (conexus-ml1z) was
 * catalog-013-0's normalization DML silently no-op'ing — the Liquibase role
 * (nexus_admin) is the table OWNER but has NO BYPASSRLS, and FORCE ROW LEVEL
 * SECURITY hid every row from its DELETE/UPDATE while 013-2's VALIDATE (DDL,
 * never row-filtered) saw them all and crash-looped. The original form of
 * this test replayed that incident against the TEXT-era
 * {@code chash_index_chash_len_check} lifecycle (that table was dropped by
 * RDR-187/nexus-piwya.9; the vehicle is now the manifest,
 * {@code catalog_document_chunks} — same FORCE-RLS + NOT-VALID-octet shape).
 *
 * <p>RDR-180 (rdr180-001-bytea-chash.xml) RETIRES that incident class BY
 * DESIGN rather than by fix: the cohort changesets contain NO DML at all —
 * ALTER TABLE type conversions are DDL (RLS-exempt, rewrite every row), and
 * the real row rekey runs as nexus_svc under withTenant via
 * {@code /v1/remap/rekey}. The old length-CHECK lifecycle this test replayed
 * no longer exists (dropped by rdr180-2; the octet CHECKs are validated by
 * the client rung, never at boot). What this test now guards:
 *
 * <ol>
 *   <li>The no-DML design invariant itself — a static scan proving the
 *       rdr180 changelogs contain no data-modifying statements (the exact
 *       hazard class the incident taught; a future edit adding DML to these
 *       files would silently reintroduce it).</li>
 *   <li>The RLS-blindness ground truth that motivated the design: FORCE RLS
 *       hides every row from the non-BYPASSRLS owner (superuser sees N,
 *       owner sees 0).</li>
 *   <li>The changelog replays cleanly as the production-shaped role WITH
 *       not-yet-rekeyed legacy rows present (16-byte decoded pre-RDR-180
 *       values — the mid-migration state every upgraded store passes
 *       through), and the replay leaves those rows byte-untouched (no
 *       hidden DML, no boot-time VALIDATE of the NOT VALID octet CHECKs).</li>
 * </ol>
 */
class Catalog013RlsReplayTest {

    private static final String ADMIN_ROLE = "nexus_admin_replay";
    private static final String ADMIN_PASS = "nexus_admin_replay_pw";

    /** Mutating statement scan — mirrors the client-side diagnostic lint's
     *  keyword class, scoped to real SQL (XML comments stripped). */
    private static final Pattern DML_RE = Pattern.compile(
        "\\b(INSERT\\s+INTO|UPDATE\\s+nexus\\.|DELETE\\s+FROM|MERGE\\s+INTO|TRUNCATE)\\b",
        Pattern.CASE_INSENSITIVE);

    @Test
    void rdr180Changelogs_containNoDml_theIncidentClassIsRetiredByDesign() throws Exception {
        for (String name : new String[] {
            "rdr180-001-bytea-chash.xml", "rdr180-002-hex-boundary-functions.xml"}) {
            Path p = Path.of("src/main/resources/db/changelog/" + name);
            String xml = Files.readString(p);
            // Strip XML comments and the function bodies' SELECTs are fine —
            // only data-modifying keywords are hazardous under FORCE RLS.
            String noComments = xml.replaceAll("(?s)<!--.*?-->", "");
            Matcher m = DML_RE.matcher(noComments);
            assertThat(m.find())
                .as("%s must contain NO DML (the nexus-1wjmq FORCE-RLS "
                    + "blindness class: Liquibase runs as the non-BYPASSRLS "
                    + "owner and silently sees zero rows). Found: %s",
                    name, m.find(0) ? m.group() : "")
                .isFalse();
        }
    }

    @Test
    void changelogReplaysCleanly_asNonBypassRlsOwner_withLegacyByteaRows() throws Exception {
        try (var pg = PgContainerHelper.start();
             Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);

            // DBA bootstrap: the grants changeset (runAlways) requires nexus_svc.
            exec(su,
                "DO $$ BEGIN "
                + "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN "
                + "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; "
                + "  END IF; "
                + "END $$");
            exec(su, "CREATE ROLE " + ADMIN_ROLE + " LOGIN PASSWORD '"
                + ADMIN_PASS + "' NOSUPERUSER NOBYPASSRLS");

            // 1. Full changelog as superuser — post-RDR-180 schema (bytea).
            liquibaseUpdate(pg.getJdbcUrl(),
                PgContainerHelper.USERNAME, PgContainerHelper.PASSWORD);

            // 2. Seed the REAL mid-migration legacy state: 16-byte decoded
            //    pre-RDR-180 values (what the type conversion leaves before
            //    the per-tenant rekey runs). FK parents first.
            for (String[] tc : new String[][] {
                {"t1", "code__x"}, {"t2", "code__z"}}) {
                try (var ps = su.prepareStatement(
                    "INSERT INTO nexus.catalog_collections (tenant_id, name) "
                    + "VALUES (?, ?) ON CONFLICT DO NOTHING")) {
                    ps.setString(1, tc[0]);
                    ps.setString(2, tc[1]);
                    ps.executeUpdate();
                }
            }
            // The octet CHECK is NOT VALID — which ENFORCES NEW WRITES by
            // design (only pre-existing rows escape validation). The real
            // mid-migration state arises from the type conversion of rows
            // that predate rdr180; reconstruct it the same way the pre-flip
            // incident tests did — drop, seed, re-add NOT VALID.
            exec(su, "ALTER TABLE nexus.catalog_document_chunks "
                + "DROP CONSTRAINT catalog_document_chunks_chash_octet_check");
            byte[] legacyA = HexFormat.of().parseHex("a".repeat(32));  // 16 bytes
            byte[] legacyC = HexFormat.of().parseHex("c".repeat(32));
            seedRow(su, "t1", legacyA, "code__x");
            seedRow(su, "t2", legacyC, "code__z");
            exec(su, "ALTER TABLE nexus.catalog_document_chunks "
                + "ADD CONSTRAINT catalog_document_chunks_chash_octet_check "
                + "CHECK (octet_length(chash) = 32) NOT VALID");

            // 3. Production-shaped ownership for the replay role.
            exec(su, "GRANT USAGE, CREATE ON SCHEMA nexus, t1, public TO " + ADMIN_ROLE);
            exec(su,
                "DO $$ DECLARE r record; BEGIN "
                + "  FOR r IN SELECT schemaname, tablename FROM pg_tables "
                + "           WHERE schemaname IN ('nexus', 't1') LOOP "
                + "    EXECUTE format('ALTER TABLE %I.%I OWNER TO " + ADMIN_ROLE + "', "
                + "                   r.schemaname, r.tablename); "
                + "  END LOOP; "
                + "  FOR r IN SELECT schemaname, sequencename FROM pg_sequences "
                + "           WHERE schemaname IN ('nexus', 't1') LOOP "
                + "    EXECUTE format('ALTER SEQUENCE %I.%I OWNER TO " + ADMIN_ROLE + "', "
                + "                   r.schemaname, r.sequencename); "
                + "  END LOOP; "
                + "END $$");
            exec(su, "GRANT ALL ON TABLE public.databasechangelog, "
                + "public.databasechangeloglock TO " + ADMIN_ROLE);

            String url = pg.getJdbcUrl();
            try (Connection admin = DriverManager.getConnection(url, ADMIN_ROLE, ADMIN_PASS)) {
                admin.setAutoCommit(true);
                // 4. The RLS-blindness ground truth that motivated the
                //    no-DML design: FORCE RLS + no tenant GUC hides every
                //    row from the non-BYPASSRLS owner.
                assertThat(count(su, "SELECT count(*) FROM nexus.catalog_document_chunks"))
                    .as("superuser ground truth").isEqualTo(2);
                assertThat(count(admin, "SELECT count(*) FROM nexus.catalog_document_chunks"))
                    .as("FORCE RLS hides every row from the non-BYPASSRLS owner "
                        + "— any DML in a changeset would silently no-op here")
                    .isEqualTo(0);
            }

            // 5. THE REPLAY: re-run Liquibase as the production-shaped role
            //    (runAlways changesets re-execute). Must complete: the
            //    octet CHECKs are NOT VALID (no boot-time VALIDATE exists to
            //    crash-loop on the legacy rows), and no changeset carries
            //    DML that RLS could silently blind.
            liquibaseUpdate(url, ADMIN_ROLE, ADMIN_PASS);

            // 6. The legacy rows are byte-untouched (rekey belongs to
            //    /v1/remap/rekey under withTenant, never to Liquibase).
            assertThat(count(su,
                "SELECT count(*) FROM nexus.catalog_document_chunks WHERE octet_length(chash) = 16"))
                .as("mid-migration legacy rows survive the replay unmodified")
                .isEqualTo(2);
            // The octet CHECK exists and remains NOT VALID (validated only
            // by the client rung's admin connection, post-rekey).
            assertThat(count(su,
                "SELECT count(*) FROM pg_constraint "
                + "WHERE conname = 'catalog_document_chunks_chash_octet_check' AND NOT convalidated"))
                .isEqualTo(1);
        }
    }

    /**
     * Run the changelog on a DEDICATED connection. Liquibase flips its
     * connection to autoCommit=false and leaves the transaction/session
     * state behind — reusing the caller's connection afterwards makes every
     * subsequent statement invisible to other sessions (this test's first
     * two failures: an "uncommitted" CREATE ROLE failing password auth, and
     * uncommitted GRANTs reading as permission-denied). Peer tests use the
     * same dedicated-connection pattern.
     */
    private static void liquibaseUpdate(String url, String user, String pass) throws Exception {
        try (Connection conn = DriverManager.getConnection(url, user, pass)) {
            var lb = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(
                    new JdbcConnection(conn)));
            lb.update(new Contexts());
        }
    }

    private static void seedRow(Connection c, String tenant, byte[] chash,
                                String collection) throws Exception {
        // Manifest vehicle (RDR-187: the chash_index original was dropped):
        // parent doc first, then the chunk-pointer row carrying the legacy
        // 16-byte chash.
        try (var ps = c.prepareStatement(
            "INSERT INTO nexus.catalog_documents "
            + "(tenant_id, tumbler, title, author, year, content_type, corpus, physical_collection) "
            + "VALUES (?, ?, 'replay doc', 'a', 2026, 'paper', 'research', ?) "
            + "ON CONFLICT DO NOTHING")) {
            ps.setString(1, tenant);
            ps.setString(2, "replay-" + tenant);
            ps.setString(3, collection);
            ps.executeUpdate();
        }
        try (var ps = c.prepareStatement(
            "INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash, collection) "
            + "VALUES (?, ?, 0, ?, ?)")) {
            ps.setString(1, tenant);
            ps.setString(2, "replay-" + tenant);
            ps.setBytes(3, chash);
            ps.setString(4, collection);
            ps.executeUpdate();
        }
    }

    private static int count(Connection c, String sql) throws Exception {
        try (Statement st = c.createStatement(); ResultSet rs = st.executeQuery(sql)) {
            rs.next();
            return rs.getInt(1);
        }
    }

    private static void exec(Connection c, String sql) throws Exception {
        try (Statement st = c.createStatement()) {
            st.execute(sql);
        }
    }
}
