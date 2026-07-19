package dev.nexus.service;

import dev.nexus.service.db.Chash;
import dev.nexus.service.db.RekeyOps;
import dev.nexus.service.db.TenantScope;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.TestMethodOrder;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.Statement;
import java.util.HexFormat;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-180 Item6/Item8 (nexus-jxizy.6, carrying nexus-jxizy.4's TEST list
 * VERBATIM): the per-tenant full-digest rekey against real Postgres.
 *
 * <p>Bead .4's acceptance criteria, proven end-to-end here (the policy half
 * lives client-side in {@code chash_disposition.py}; THIS is the execution
 * half): (a) rehashable row → {@code sha256(chunk_text)}; (b) reference-only
 * row whose old chash has a content sibling → remapped to the sibling's new
 * key, NOT dropped; (c) orphaned row under {@code drop} → row GONE and its
 * manifest/chash_index pointers CASCADED (no dangling scan hit); (d)
 * orphaned row under {@code synthesize} → surrogate 32-byte key present
 * WITH {@code metadata.chash_origin='synthetic'}, pointer preserved (and
 * repointed to the surrogate — never dangling at the old key). Disposition
 * counts logged and asserted. Plus: two-phase duplicate collapse, the
 * ETL-era 32-byte-ASCII id class, full cascade (manifest, chash_index,
 * topic_assignments, frecency, relevance_log), idempotency, and the
 * collision refusal.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class RekeyOpsIntegrationTest {

    private static final String SVC_ROLE = "svc_rekey_test";
    private static final String SVC_PASS = "svc_rekey_pw";
    private static final String TA = "t-rekey-a";
    private static final String TB = "t-rekey-b";
    private static final String TC = "t-rekey-c";

    private static final String TEXT_A = "rekey content alpha";
    private static final String TEXT_B = "rekey content bravo";
    private static final String TEXT_DUP = "rekey duplicated text";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    RekeyOps rekeyOps;

    private static byte[] sha256(String text) {
        try {
            return MessageDigest.getInstance("SHA-256")
                .digest(text.getBytes(StandardCharsets.UTF_8));
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    /** The pre-RDR-180 legacy key: 16 bytes = decode(sha256[:32hex]). */
    private static byte[] legacyKey(String text) {
        byte[] full = sha256(text);
        byte[] half = new byte[16];
        System.arraycopy(full, 0, half, 0, 16);
        return half;
    }

    private static String vec(int dim) {
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < dim; i++) {
            if (i > 0) sb.append(',');
            sb.append('0');
        }
        return sb.append(']').toString();
    }

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            for (String role : new String[] {SVC_ROLE, "nexus_svc"}) {
                su.createStatement().execute(
                    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '"
                    + role + "') THEN CREATE ROLE " + role + " LOGIN PASSWORD '"
                    + (role.equals(SVC_ROLE) ? SVC_PASS : "nexus_svc_pass")
                    + "'; END IF; END $$");
            }
        }
        try (Connection su = pg.createConnection("")) {
            var lb = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(
                    new JdbcConnection(su)));
            lb.update(new Contexts());
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }
        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(3);
        config.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(config);
        rekeyOps = new RekeyOps(new TenantScope(svcDs));
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── seeding (superuser: reconstructs the mid-migration state the type
    //    conversion leaves — the NOT VALID checks enforce new writes, so the
    //    checks are dropped around seeding and re-added NOT VALID, the same
    //    reconstruction the pre-flip incident tests used) ───────────────────

    private void withChecksDropped(Connection su, Runnable seed) throws Exception {
        su.createStatement().execute(
            "ALTER TABLE nexus.chunks_768 DROP CONSTRAINT chunks_768_chash_octet_check");
        su.createStatement().execute(
            "ALTER TABLE nexus.chunks_384 DROP CONSTRAINT chunks_384_chash_octet_check");
        su.createStatement().execute(
            "ALTER TABLE nexus.catalog_document_chunks DROP CONSTRAINT catalog_document_chunks_chash_octet_check");
        su.createStatement().execute(
            "ALTER TABLE nexus.chash_index DROP CONSTRAINT chash_index_chash_octet_check");
        try {
            seed.run();
        } finally {
            su.createStatement().execute(
                "ALTER TABLE nexus.chunks_768 ADD CONSTRAINT chunks_768_chash_octet_check "
                + "CHECK (octet_length(chash) = 32) NOT VALID");
            su.createStatement().execute(
                "ALTER TABLE nexus.chunks_384 ADD CONSTRAINT chunks_384_chash_octet_check "
                + "CHECK (octet_length(chash) = 32) NOT VALID");
            su.createStatement().execute(
                "ALTER TABLE nexus.catalog_document_chunks ADD CONSTRAINT catalog_document_chunks_chash_octet_check "
                + "CHECK (octet_length(chash) = 32) NOT VALID");
            su.createStatement().execute(
                "ALTER TABLE nexus.chash_index ADD CONSTRAINT chash_index_chash_octet_check "
                + "CHECK (octet_length(chash) = 32) NOT VALID");
        }
    }

    private static void insertChunk(Connection su, String tenant, String table, int dim,
                                    String collection, byte[] chash, String text) {
        try (PreparedStatement ps = su.prepareStatement(
            "INSERT INTO " + table + " (tenant_id, collection, chash, chunk_text, embedding) "
            + "VALUES (?, ?, ?, ?, ?::vector)")) {
            ps.setString(1, tenant);
            ps.setString(2, collection);
            ps.setBytes(3, chash);
            ps.setString(4, text);
            ps.setString(5, vec(dim));
            ps.executeUpdate();
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }

    private static void exec(Connection c, String sql) throws Exception {
        try (Statement st = c.createStatement()) {
            st.execute(sql);
        }
    }

    private int count(String sql) throws Exception {
        try (Connection su = pg.createConnection("");
             Statement st = su.createStatement();
             ResultSet rs = st.executeQuery(sql)) {
            rs.next();
            return rs.getInt(1);
        }
    }

    private String scalar(String sql) throws Exception {
        try (Connection su = pg.createConnection("");
             Statement st = su.createStatement();
             ResultSet rs = st.executeQuery(sql)) {
            return rs.next() ? rs.getString(1) : null;
        }
    }

    // ── Test 1: the full pass on tenant TA (drop policy) ─────────────────────

    @Test
    @Order(1)
    void rekey_fullPass_dispositionsAtoC_andCascade() throws Exception {
        byte[] legacyA = legacyKey(TEXT_A);                    // (a) rehashable, 16-byte era
        String legacyAHex = HexFormat.of().formatHex(legacyA); // its 32-hex old_ref
        String etlBRef = "p4a-rekey-etl-id-000000000000032";  // exactly 32 ASCII chars
        assertThat(etlBRef).hasSize(32);
        byte[] etlB = etlBRef.getBytes(StandardCharsets.UTF_8); // 32-byte ASCII ETL-era id
        byte[] legacyDup1 = legacyKey(TEXT_DUP);
        byte[] legacyDup2 = HexFormat.of().parseHex("0".repeat(31) + "1");  // distinct 16-byte id, same text
        byte[] orphanKey = HexFormat.of().parseHex("f".repeat(32));         // 16-byte, no content anywhere

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            exec(su, "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES "
                + "('" + TA + "', 'code__k') ON CONFLICT DO NOTHING");
            exec(su, "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title) "
                + "VALUES ('" + TA + "', '1.1', 'doc') ON CONFLICT DO NOTHING");
            withChecksDropped(su, () -> {
                // (a) content row, legacy 16-byte key
                insertChunk(su, TA, "nexus.chunks_768", 768, "code__k", legacyA, TEXT_A);
                // ETL-era 32-byte ASCII id with content (the width-predicate blindspot)
                insertChunk(su, TA, "nexus.chunks_768", 768, "code__k", etlB, TEXT_B);
                // duplicate-content collapse pair (same collection, same text)
                insertChunk(su, TA, "nexus.chunks_768", 768, "code__k", legacyDup1, TEXT_DUP);
                insertChunk(su, TA, "nexus.chunks_768", 768, "code__k", legacyDup2, TEXT_DUP);
                // (b) reference-only row in ANOTHER dim sharing A's old key
                insertChunk(su, TA, "nexus.chunks_384", 384, "code__k", legacyA, "");
                // (c) orphan: empty text, no content sibling anywhere
                insertChunk(su, TA, "nexus.chunks_384", 384, "code__k", orphanKey, "");
                try {
                    // pointers at the orphan key (manifest + chash_index) — must cascade on drop
                    try (PreparedStatement ps = su.prepareStatement(
                        "INSERT INTO nexus.catalog_document_chunks "
                        + "(tenant_id, doc_id, position, chash, collection) VALUES ('"
                        + TA + "', '1.1', 0, ?, 'code__k')")) {
                        ps.setBytes(1, orphanKey);
                        ps.executeUpdate();
                    }
                    try (PreparedStatement ps = su.prepareStatement(
                        "INSERT INTO nexus.catalog_document_chunks "
                        + "(tenant_id, doc_id, position, chash, collection) VALUES ('"
                        + TA + "', '1.1', 1, ?, 'code__k')")) {
                        ps.setBytes(1, legacyA);
                        ps.executeUpdate();
                    }
                    try (PreparedStatement ps = su.prepareStatement(
                        "INSERT INTO nexus.chash_index "
                        + "(tenant_id, chash, physical_collection, created_at) VALUES ('"
                        + TA + "', ?, 'code__k', now())")) {
                        ps.setBytes(1, orphanKey);
                        ps.executeUpdate();
                    }
                    try (PreparedStatement ps = su.prepareStatement(
                        "INSERT INTO nexus.chash_index "
                        + "(tenant_id, chash, physical_collection, created_at) VALUES ('"
                        + TA + "', ?, 'code__k', now())")) {
                        ps.setBytes(1, legacyA);
                        ps.executeUpdate();
                    }
                    // debt-table references by A's ORIGINAL string forms (TEXT columns)
                    exec(su, "INSERT INTO nexus.topics (tenant_id, id, collection, label, created_at) VALUES "
                        + "('" + TA + "', 991, 'code__k', 'topic-x', now()) ON CONFLICT DO NOTHING");
                    exec(su, "INSERT INTO nexus.topic_assignments (tenant_id, doc_id, topic_id) "
                        + "VALUES ('" + TA + "', '" + legacyAHex + "', 991)");
                    exec(su, "INSERT INTO nexus.frecency (tenant_id, chunk_id, frecency_score, "
                        + "miss_count, last_hit_at, embedded_at, ttl_days) VALUES ('"
                        + TA + "', '" + legacyAHex + "', 1.5, 2, now(), now(), 30)");
                    exec(su, "INSERT INTO nexus.relevance_log (tenant_id, query, chunk_id, action, "
                        + "timestamp) VALUES ('" + TA + "', 'q', '" + legacyAHex + "', 'open', now())");
                } catch (Exception e) {
                    throw new RuntimeException(e);
                }
            });
        }

        Map<String, Object> counts = rekeyOps.rekey(TA, false);

        // envelope
        assertThat((int) counts.get("residual_mismatched")).isZero();
        assertThat((int) counts.get("dangling_manifest")).isZero();
        assertThat((int) counts.get("rehashed")).isEqualTo(3);   // A, B, dup-survivor
        assertThat((int) counts.get("collapsed_duplicates")).isEqualTo(1);
        assertThat((int) counts.get("reference_only_resolved")).isEqualTo(1);
        assertThat((int) counts.get("orphans_dropped")).isEqualTo(1);
        assertThat((int) counts.get("orphans_synthesized")).isZero();

        String newAHex = HexFormat.of().formatHex(sha256(TEXT_A));
        // (a) rehashable → full digest key
        assertThat(count("SELECT count(*) FROM nexus.chunks_768 WHERE tenant_id='" + TA
            + "' AND chash = decode('" + newAHex + "', 'hex')")).isEqualTo(1);
        // ETL-era 32-byte ASCII id also rekeyed (width-free predicate)
        assertThat(count("SELECT count(*) FROM nexus.chunks_768 WHERE tenant_id='" + TA
            + "' AND chash = sha256(convert_to('" + TEXT_B + "', 'UTF8'))")).isEqualTo(1);
        // duplicate pair collapsed to ONE row at the digest key
        assertThat(count("SELECT count(*) FROM nexus.chunks_768 WHERE tenant_id='" + TA
            + "' AND chunk_text = '" + TEXT_DUP + "'")).isEqualTo(1);
        // (b) reference-only row remapped to the sibling's new key, NOT dropped
        assertThat(count("SELECT count(*) FROM nexus.chunks_384 WHERE tenant_id='" + TA
            + "' AND chash = decode('" + newAHex + "', 'hex') AND chunk_text = ''")).isEqualTo(1);
        // (c) orphan row GONE and pointers CASCADED — no dangling scan hits
        assertThat(count("SELECT count(*) FROM nexus.chunks_384 WHERE tenant_id='" + TA
            + "' AND chash = decode('" + "f".repeat(32) + "', 'hex')")).isZero();
        assertThat(count("SELECT count(*) FROM nexus.catalog_document_chunks WHERE tenant_id='" + TA
            + "' AND octet_length(chash) <> 32")).isZero();
        assertThat(count("SELECT count(*) FROM nexus.chash_index WHERE tenant_id='" + TA
            + "' AND octet_length(chash) <> 32")).isZero();
        // alias facts: the 16-byte era row's old_ref is its 32-hex; the
        // ETL-era row's old_ref is its raw ASCII id (reversibility lemma)
        assertThat(scalar("SELECT encode(new_chash, 'hex') FROM nexus.chash_alias "
            + "WHERE tenant_id='" + TA + "' AND old_ref = '" + legacyAHex + "'"))
            .isEqualTo(newAHex);
        assertThat(count("SELECT count(*) FROM nexus.chash_alias WHERE tenant_id='" + TA
            + "' AND old_ref = '" + etlBRef + "'"))
            .as("reversibility lemma: the 32-byte ASCII id's old_ref is its raw string")
            .isEqualTo(1);
        // cascade: debt tables repointed to the 64-hex interchange form
        assertThat(scalar("SELECT doc_id FROM nexus.topic_assignments WHERE tenant_id='" + TA
            + "' AND topic_id = 991")).isEqualTo(newAHex);
        assertThat(scalar("SELECT chunk_id FROM nexus.frecency WHERE tenant_id='" + TA + "'"))
            .isEqualTo(newAHex);
        assertThat(scalar("SELECT chunk_id FROM nexus.relevance_log WHERE tenant_id='" + TA + "'"))
            .isEqualTo(newAHex);
        // manifest repointed to the new bytes for A
        assertThat(count("SELECT count(*) FROM nexus.catalog_document_chunks WHERE tenant_id='" + TA
            + "' AND chash = decode('" + newAHex + "', 'hex')")).isEqualTo(1);
    }

    // ── Test 2: idempotency ──────────────────────────────────────────────────

    @Test
    @Order(2)
    void rekey_secondRun_isAllZero() {
        Map<String, Object> counts = rekeyOps.rekey(TA, false);
        assertThat((int) counts.get("rehashed")).isZero();
        assertThat((int) counts.get("collapsed_duplicates")).isZero();
        assertThat((int) counts.get("reference_only_resolved")).isZero();
        assertThat((int) counts.get("orphans_dropped")).isZero();
        assertThat((int) counts.get("residual_mismatched")).isZero();
    }

    // ── Test 3: (d) synthesize policy on tenant TB ───────────────────────────

    @Test
    @Order(3)
    void rekey_synthesize_mintsFlaggedSurrogate_pointerFollows() throws Exception {
        byte[] orphanKey = HexFormat.of().parseHex("e".repeat(32));  // 16-byte orphan
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            exec(su, "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES "
                + "('" + TB + "', 'code__s') ON CONFLICT DO NOTHING");
            exec(su, "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title) "
                + "VALUES ('" + TB + "', '2.1', 'doc') ON CONFLICT DO NOTHING");
            withChecksDropped(su, () -> {
                insertChunk(su, TB, "nexus.chunks_384", 384, "code__s", orphanKey, "");
                try (PreparedStatement ps = su.prepareStatement(
                    "INSERT INTO nexus.catalog_document_chunks "
                    + "(tenant_id, doc_id, position, chash, collection) VALUES ('"
                    + TB + "', '2.1', 0, ?, 'code__s')")) {
                    ps.setBytes(1, orphanKey);
                    ps.executeUpdate();
                } catch (Exception e) {
                    throw new RuntimeException(e);
                }
            });
        }

        Map<String, Object> counts = rekeyOps.rekey(TB, true);
        assertThat((int) counts.get("orphans_synthesized")).isEqualTo(1);
        assertThat((int) counts.get("orphans_dropped")).isZero();
        assertThat((int) counts.get("dangling_manifest")).isZero();

        // (d) surrogate = sha256("nexus:synthetic-chash:v1|" + tenant + "|" +
        // collection + "|" + old_ref), flagged, pointer repointed to it.
        String oldRef = "e".repeat(32);
        String surrogateHex = HexFormat.of().formatHex(
            sha256("nexus:synthetic-chash:v1|" + TB + "|code__s|" + oldRef));
        assertThat(scalar("SELECT metadata->>'chash_origin' FROM nexus.chunks_384 "
            + "WHERE tenant_id='" + TB + "' AND chash = decode('" + surrogateHex + "', 'hex')"))
            .isEqualTo("synthetic");
        assertThat(count("SELECT count(*) FROM nexus.catalog_document_chunks WHERE tenant_id='" + TB
            + "' AND chash = decode('" + surrogateHex + "', 'hex')"))
            .as("(d): the preserved pointer must FOLLOW the surrogate, never dangle")
            .isEqualTo(1);
        assertThat(scalar("SELECT encode(new_chash, 'hex') FROM nexus.chash_alias "
            + "WHERE tenant_id='" + TB + "' AND old_ref = '" + oldRef + "'"))
            .isEqualTo(surrogateHex);
    }

    // ── Test 3b: the Item3 read seam — legacy refs resolve via the alias ─────

    @Test
    @Order(3)
    void resolveLegacyRef_readsTheAliasMap() {
        var repo = new dev.nexus.service.db.ChashRepository(new TenantScope(svcDs));
        String legacyAHex = HexFormat.of().formatHex(legacyKey(TEXT_A));
        Chash resolved = repo.resolveLegacyRef(TA, legacyAHex);
        assertThat(resolved).isNotNull();
        assertThat(resolved.toHex()).isEqualTo(HexFormat.of().formatHex(sha256(TEXT_A)));
        // unmapped legacy ref: null (caller answers empty rows — dangling,
        // not an error), and cross-tenant facts are RLS-invisible.
        assertThat(repo.resolveLegacyRef(TA, "0".repeat(32))).isNull();
        assertThat(repo.resolveLegacyRef(TC, legacyAHex)).isNull();
    }

    // ── Test 4: collision refusal on tenant TC ───────────────────────────────

    @Test
    @Order(4)
    void rekey_sameOldRef_twoDigests_refusesLoud() throws Exception {
        byte[] sharedOldKey = HexFormat.of().parseHex("d".repeat(32));
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            exec(su, "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES "
                + "('" + TC + "', 'code__c1'), ('" + TC + "', 'code__c2') ON CONFLICT DO NOTHING");
            withChecksDropped(su, () -> {
                insertChunk(su, TC, "nexus.chunks_768", 768, "code__c1", sharedOldKey, "text one");
                insertChunk(su, TC, "nexus.chunks_768", 768, "code__c2", sharedOldKey, "text two");
            });
        }
        assertThatThrownBy(() -> rekeyOps.rekey(TC, false))
            .isInstanceOf(RekeyOps.RekeyConflictException.class)
            .hasMessageContaining("refusing");
        // nothing mutated (transactional): both rows still hold the old key
        assertThat(count("SELECT count(*) FROM nexus.chunks_768 WHERE tenant_id='" + TC
            + "' AND chash = decode('" + "d".repeat(32) + "', 'hex')")).isEqualTo(2);
        assertThat(count("SELECT count(*) FROM nexus.chash_alias WHERE tenant_id='" + TC + "'"))
            .isZero();
    }
}
