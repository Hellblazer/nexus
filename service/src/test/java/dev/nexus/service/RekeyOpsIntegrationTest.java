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
import java.sql.SQLException;
import java.sql.Statement;
import java.util.HexFormat;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

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
 * manifest pointers CASCADED (no dangling scan hit; the chash_index twin
 * died with the router, RDR-187); (d)
 * orphaned row under {@code synthesize} → surrogate 32-byte key present
 * WITH {@code metadata.chash_origin='synthetic'}, pointer preserved (and
 * repointed to the surrogate — never dangling at the old key). Disposition
 * counts logged and asserted. Plus: two-phase duplicate collapse, the
 * ETL-era 32-byte-ASCII id class, full cascade (manifest,
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
            // Mirror grants-nexus-svc's MAINTAIN grant onto this test's stand-in
            // for nexus_svc, so the rekey's in-transaction ANALYZE is exercised
            // under the SAME privilege it holds in production (rdr180-17 / F2).
            // That the CHANGESET grants this to the real nexus_svc is a separate
            // contract, asserted in SchemaMigratorIntegrationTest — this fixture
            // must not be the only thing standing between the grant and a silent
            // no-op in production.
            su.createStatement().execute(
                "GRANT MAINTAIN ON nexus.chash_alias TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }
        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        // nexus-t76bp: bumped from 3 to 4 -- this class's own peak
        // concurrent-connection usage (code-review-expert T2
        // review-11gh6-gate-2026-08-08 [21797], T76BP SECTION "MINOR" note:
        // the original comment's justification was half-inaccurate -- it
        // cited StagingPromoteOpsIntegrationTest "for the same reason," but
        // that sibling runs the IDENTICAL external-holds-gate-then-blocks-
        // then-proceeds shape at pool=3, unbumped, so "matching the
        // sibling" was not the real driver). The actual driver: the gate
        // tests below (Order 20-22) each hold one EXTERNAL connection open
        // on this same svcDs pool while rekeyOps.rekey blocks on a SECOND,
        // borrowed connection from the same pool -- 2 concurrent, a small
        // margin over this class's own prior peak.
        config.setMaximumPoolSize(4);
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

    // RDR-191 repoint (nexus-o8dil.17): the former per-dim octet-check
    // constraints (chunks_768_chash_octet_check / chunks_384_chash_octet_check
    // on their own physical tables) collapse to ONE constraint,
    // chunks_chash_octet_check, on the single unified nexus.chunks table
    // (vectors-004-unify-chunks.xml). One DROP/ADD pair instead of two.
    private void withChecksDropped(Connection su, Runnable seed) throws Exception {
        su.createStatement().execute(
            "ALTER TABLE nexus.chunks DROP CONSTRAINT chunks_chash_octet_check");
        su.createStatement().execute(
            "ALTER TABLE nexus.catalog_document_chunks DROP CONSTRAINT catalog_document_chunks_chash_octet_check");
        try {
            seed.run();
        } finally {
            su.createStatement().execute(
                "ALTER TABLE nexus.chunks ADD CONSTRAINT chunks_chash_octet_check "
                + "CHECK (octet_length(chash) = 32) NOT VALID");
            su.createStatement().execute(
                "ALTER TABLE nexus.catalog_document_chunks ADD CONSTRAINT catalog_document_chunks_chash_octet_check "
                + "CHECK (octet_length(chash) = 32) NOT VALID");
        }
    }

    // RDR-191 repoint (nexus-o8dil.17): nexus.chunks_384/768/1024 collapsed
    // into ONE table, nexus.chunks, with a per-dim embedding_<dim> column
    // (exactly one non-null). The `table` parameter is gone -- there is
    // only one table to insert into now -- and the fixed "embedding" column
    // name becomes "embedding_" + dim, selecting which column this row's
    // vector lands in.
    private static void insertChunk(Connection su, String tenant, int dim,
                                    String collection, byte[] chash, String text) {
        try (PreparedStatement ps = su.prepareStatement(
            "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_" + dim + ") "
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
                + "('" + TA + "', 'code__k'), ('" + TA + "', 'code__k2') ON CONFLICT DO NOTHING");
            exec(su, "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title) "
                + "VALUES ('" + TA + "', '1.1', 'doc') ON CONFLICT DO NOTHING");
            withChecksDropped(su, () -> {
                // (a) content row, legacy 16-byte key
                insertChunk(su, TA, 768, "code__k", legacyA, TEXT_A);
                // ETL-era 32-byte ASCII id with content (the width-predicate blindspot)
                insertChunk(su, TA, 768, "code__k", etlB, TEXT_B);
                // duplicate-content collapse pair (same collection, same text)
                insertChunk(su, TA, 768, "code__k", legacyDup1, TEXT_DUP);
                insertChunk(su, TA, 768, "code__k", legacyDup2, TEXT_DUP);
                // (b) reference-only row sharing A's old key, in a DIFFERENT
                // collection ('code__k2'). RDR-191 repoint (nexus-o8dil.17):
                // before unification this lived in chunks_384 while A's
                // content lived in chunks_768 -- "another dim, same
                // collection" was schema-legal because each dim had its own
                // table/PK. Under the unified nexus.chunks table the PK is
                // (tenant_id, collection, chash) with NO dim component, so a
                // content row and an empty-text row can no longer coexist at
                // the identical (tenant, collection, chash) key -- that
                // configuration is now a PK violation, not a fixture this
                // schema can express. orphanCond's own scoping was ALWAYS
                // chash-only (no collection filter -- "a content-bearing row
                // ANYWHERE shares this chash"), so a different collection is
                // the faithful, schema-valid re-expression of the exact same
                // scenario the class javadoc's disposition (b) documents,
                // not a weakened substitute.
                insertChunk(su, TA, 384, "code__k2", legacyA, "");
                // (c) orphan: empty text, no content sibling anywhere
                insertChunk(su, TA, 384, "code__k", orphanKey, "");
                // nexus-4okz4 increment 2 REWORK (code-review-expert C1, T2
                // review [21863] / critique [21864]): adjudicating fixture for
                // orphanCond's content-sibling NOT EXISTS clause -- pins the
                // self-shadowing regression class (an unaliased same-table
                // subquery degenerating "chash = c.chash AND chunk_text <>
                // ''" to just "chunk_text <> ''", i.e. "does ANY content
                // exist", independent of which key is being checked).
                //
                // RDR-191 repoint (nexus-o8dil.17): this fixture's ORIGINAL
                // purpose was to make chunks_384 non-empty, because before
                // unification a de-correlated same-dim subquery and the
                // correlated one only disagreed once THAT dim's table held
                // some content. Post-unification there is exactly ONE
                // physical table (nexus.chunks) and it is ALREADY non-empty
                // from TEXT_A/TEXT_B/TEXT_DUP above, so this insert is no
                // longer load-bearing for non-emptiness -- kept anyway as an
                // independent, defense-in-depth pin (a second, differently
                // -chashed content row) so a self-shadowing regression is
                // caught even if a future edit changes which rows precede it
                // in insertion order. Digest-matching (chash =
                // sha256(text)), so it stays inert to rekey's own
                // mismatch/collapse predicates exactly as before.
                String sameDimContentText =
                    "rekey self-shadow orphanCond correlation fixture ta " + System.nanoTime();
                byte[] sameDimChash = sha256(sameDimContentText);
                insertChunk(su, TA, 384, "code__k",
                    sameDimChash, sameDimContentText);
                // this row is digest-matching so rekey never touches it (see
                // above) -- stamp metadata.chunk_text_hash here to mirror
                // what a real producer already does, so it does not trip the
                // "every row mirrors its chash into metadata" parity sweep
                // below (that sweep scans the WHOLE table for the tenant,
                // not just rows rekey actually rewrote).
                try (PreparedStatement mps = su.prepareStatement(
                        "UPDATE nexus.chunks SET metadata = "
                        + "jsonb_build_object('chunk_text_hash', encode(chash, 'hex')) "
                        + "WHERE tenant_id = ? AND chash = ?")) {
                    mps.setString(1, TA);
                    mps.setBytes(2, sameDimChash);
                    mps.executeUpdate();
                } catch (Exception e) {
                    throw new RuntimeException(e);
                }
                try {
                    // pointers at the orphan key (manifest; the chash_index
                    // twin died with the router, RDR-187) — must cascade on drop
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
        assertThat(count("SELECT count(*) FROM nexus.chunks WHERE tenant_id='" + TA
            + "' AND chash = decode('" + newAHex + "', 'hex') AND collection = 'code__k'")).isEqualTo(1);
        // ETL-era 32-byte ASCII id also rekeyed (width-free predicate)
        assertThat(count("SELECT count(*) FROM nexus.chunks WHERE tenant_id='" + TA
            + "' AND chash = sha256(convert_to('" + TEXT_B + "', 'UTF8'))")).isEqualTo(1);
        // duplicate pair collapsed to ONE row at the digest key
        assertThat(count("SELECT count(*) FROM nexus.chunks WHERE tenant_id='" + TA
            + "' AND chunk_text = '" + TEXT_DUP + "'")).isEqualTo(1);
        // (b) reference-only row (seeded in 'code__k2' -- see the fixture
        // comment above for why a DIFFERENT collection now stands in for
        // "another dim, same key") remapped to the sibling's new key, NOT
        // dropped.
        assertThat(count("SELECT count(*) FROM nexus.chunks WHERE tenant_id='" + TA
            + "' AND collection = 'code__k2' AND chash = decode('" + newAHex
            + "', 'hex') AND chunk_text = ''")).isEqualTo(1);
        // (c) orphan row GONE and pointers CASCADED — no dangling scan hits
        assertThat(count("SELECT count(*) FROM nexus.chunks WHERE tenant_id='" + TA
            + "' AND chash = decode('" + "f".repeat(32) + "', 'hex')")).isZero();
        // RDR-086 metadata parity (critic-1010, nexus-jxizy.10.10): every
        // row the rekey touched must carry metadata.chunk_text_hash
        // mirroring its (new) chash — the citation resolver's where-filter
        // reads it; the seeded rows here deliberately carry NO metadata, so
        // this fails unless the rekey statements stamp it (backfill +
        // invariant-by-construction, parity with StagingPromoteOps).
        //
        // RDR-191 repoint (nexus-o8dil.17): collapsed from a loop over two
        // former per-dim tables to ONE query -- nexus.chunks now holds every
        // rekeyed row for this tenant (both collections) in the single
        // table, so a single tenant-scoped scan covers the same row set the
        // two-table loop used to cover.
        assertThat(count("SELECT count(*) FROM nexus.chunks WHERE tenant_id='" + TA
            + "' AND metadata->>'chunk_text_hash' IS DISTINCT FROM encode(chash,'hex')"))
            .as("rekeyed rows in nexus.chunks mirror their chash into metadata chunk_text_hash")
            .isZero();
        assertThat(count("SELECT count(*) FROM nexus.catalog_document_chunks WHERE tenant_id='" + TA
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
                insertChunk(su, TB, 384, "code__s", orphanKey, "");
                // nexus-4okz4 increment 2 REWORK (code-review-expert C1, T2
                // review [21863] / critique [21864]): same adjudicating
                // fixture as rekey_fullPass_dispositionsAtoC_andCascade,
                // digest-matching so it is inert to rekey's own predicates --
                // exercises orphanCond's content-sibling NOT EXISTS
                // self-shadowing pin (see that test's fixture comment,
                // RDR-191 nexus-o8dil.17) for the SYNTHESIZE policy too, not
                // just DROP.
                String sameDimContentText =
                    "rekey self-shadow orphanCond correlation fixture tb " + System.nanoTime();
                insertChunk(su, TB, 384, "code__s",
                    sha256(sameDimContentText), sameDimContentText);
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
        assertThat(scalar("SELECT metadata->>'chash_origin' FROM nexus.chunks "
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

    // ── Test 3c: cascade COLLAPSE branches — two old refs, one new key ───────

    @Test
    @Order(3)
    void rekey_cascadeCollapse_frecencyMerges_assignmentsAndIndexTwoPhase() throws Exception {
        // Two distinct legacy ids carrying the SAME text (they collapse to
        // one digest), each with its own frecency / topic_assignments
        // rows — exercising the GREATEST-merge and the
        // two-phase delete branches of the cascades, which test 1 only
        // reached in their no-pre-existing-target shape.
        String tenant = "t-rekey-collapse";
        String text = "collapse cascade text";
        byte[] old1 = legacyKey(text);
        byte[] old2 = HexFormat.of().parseHex("0".repeat(30) + "99");
        String old1Ref = HexFormat.of().formatHex(old1);
        String old2Ref = HexFormat.of().formatHex(old2);
        String newHex = HexFormat.of().formatHex(sha256(text));

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            exec(su, "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES "
                + "('" + tenant + "', 'code__m') ON CONFLICT DO NOTHING");
            withChecksDropped(su, () -> {
                insertChunk(su, tenant, 768, "code__m", old1, text);
                insertChunk(su, tenant, 768, "code__m", old2, text);
                try {
                    // (chash_index seeds removed — RDR-187/nexus-piwya.9: the
                    // router and its two-phase repoint died; topic_assignments
                    // below carries the two-phase collapse coverage.)
                    exec(su, "INSERT INTO nexus.topics (tenant_id, id, collection, label, created_at) "
                        + "VALUES ('" + tenant + "', 992, 'code__m', 'topic-m', now()) ON CONFLICT DO NOTHING");
                    exec(su, "INSERT INTO nexus.topic_assignments (tenant_id, doc_id, topic_id) VALUES "
                        + "('" + tenant + "', '" + old1Ref + "', 992), "
                        + "('" + tenant + "', '" + old2Ref + "', 992)");
                    // frecency: distinct stats per old id — the survivor must
                    // carry the GREATEST of each column.
                    exec(su, "INSERT INTO nexus.frecency (tenant_id, chunk_id, frecency_score, "
                        + "miss_count, last_hit_at, embedded_at, ttl_days) VALUES "
                        + "('" + tenant + "', '" + old1Ref + "', 5.0, 1, now() - interval '2 days', now(), 10), "
                        + "('" + tenant + "', '" + old2Ref + "', 2.0, 7, now(), now(), 30)");
                } catch (Exception e) {
                    throw new RuntimeException(e);
                }
            });
        }

        Map<String, Object> counts = rekeyOps.rekey(tenant, false);
        assertThat((int) counts.get("collapsed_duplicates")).isEqualTo(1);
        assertThat((int) counts.get("residual_mismatched")).isZero();

        // chunks collapsed to ONE row at the digest key
        assertThat(count("SELECT count(*) FROM nexus.chunks WHERE tenant_id='" + tenant + "'"))
            .isEqualTo(1);
        // topic_assignments two-phase: one surviving assignment at the 64-hex
        assertThat(count("SELECT count(*) FROM nexus.topic_assignments WHERE tenant_id='" + tenant + "'"))
            .isEqualTo(1);
        assertThat(scalar("SELECT doc_id FROM nexus.topic_assignments WHERE tenant_id='" + tenant + "'"))
            .isEqualTo(newHex);
        // frecency GREATEST-merge: one survivor carrying max of each column
        assertThat(count("SELECT count(*) FROM nexus.frecency WHERE tenant_id='" + tenant + "'"))
            .isEqualTo(1);
        assertThat(scalar("SELECT chunk_id FROM nexus.frecency WHERE tenant_id='" + tenant + "'"))
            .isEqualTo(newHex);
        assertThat(scalar("SELECT frecency_score::text FROM nexus.frecency WHERE tenant_id='" + tenant + "'"))
            .isEqualTo("5");
        assertThat(scalar("SELECT miss_count::text FROM nexus.frecency WHERE tenant_id='" + tenant + "'"))
            .isEqualTo("7");
        assertThat(scalar("SELECT ttl_days::text FROM nexus.frecency WHERE tenant_id='" + tenant + "'"))
            .isEqualTo("30");
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
                insertChunk(su, TC, 768, "code__c1", sharedOldKey, "text one");
                insertChunk(su, TC, 768, "code__c2", sharedOldKey, "text two");
            });
        }
        assertThatThrownBy(() -> rekeyOps.rekey(TC, false))
            .isInstanceOf(RekeyOps.RekeyConflictException.class)
            .hasMessageContaining("refusing");
        // nothing mutated (transactional): both rows still hold the old key
        assertThat(count("SELECT count(*) FROM nexus.chunks WHERE tenant_id='" + TC
            + "' AND chash = decode('" + "d".repeat(32) + "', 'hex')")).isEqualTo(2);
        assertThat(count("SELECT count(*) FROM nexus.chash_alias WHERE tenant_id='" + TC + "'"))
            .isZero();
    }

    // ── Test 5: the rekey leaves chash_alias with FRESH planner stats ────────

    /**
     * F2, production 2026-07-20 (bus [20980]): the second tenant's rekey ran
     * 101 MINUTES and had to be cancelled. Root cause was not the work — the
     * same work took 461s once the planner was un-blinded. Autoanalyze fired
     * the instant tenant 1 committed and froze {@code chash_alias} statistics
     * at "this table is 100% tenant 1" ({@code most_common_vals={t1}},
     * {@code freqs=[1.0]}, {@code n_distinct=1}). Tenant 2's ~134k alias rows
     * are inserted INSIDE its own transaction and are therefore invisible to
     * the planner, so {@code tenant_id = 't2'} estimated ONE row and Postgres
     * chose a triple nested loop against ~134k x 466k actual.
     *
     * <p>The fix is one statement — ANALYZE {@code chash_alias} inside the
     * rekey transaction, after the alias INSERT — because an in-transaction
     * ANALYZE samples its own uncommitted rows.
     *
     * <p><strong>The trap this test exists to catch:</strong> the rekey runs as
     * {@code nexus_svc}, which holds DML grants only and does NOT own the
     * table. Postgres does not ERROR when a non-owner analyzes — it emits a
     * WARNING and SKIPS the table. A naive fix therefore looks applied, logs
     * nothing a caller sees, and leaves the planner exactly as blind as before.
     * So this asserts the OBSERVABLE EFFECT (statistics exist and describe the
     * rows this transaction wrote), never the mere presence of the statement.
     *
     * <p>Autovacuum is disabled on the table for the duration so the ONLY
     * possible source of statistics is the rekey itself — without that, a
     * passing assertion could be autoanalyze's work rather than ours.
     */
    @Test
    @Order(5)
    void rekey_leavesFreshPlannerStatsOnChashAlias() throws Exception {
        final String tenant = "t-rekey-stats";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            // ONLY the rekey may produce statistics for this table.
            exec(su, "ALTER TABLE nexus.chash_alias SET (autovacuum_enabled = false)");
            exec(su, "DELETE FROM pg_statistic WHERE starelid = 'nexus.chash_alias'::regclass");
            exec(su, "INSERT INTO nexus.catalog_collections (tenant_id, name) "
                + "VALUES ('" + tenant + "', 'code__stats') ON CONFLICT DO NOTHING");
            withChecksDropped(su, () -> {
                for (int i = 0; i < 40; i++) {
                    String text = "stats fixture chunk " + i;
                    insertChunk(su, tenant, 768, "code__stats",
                        legacyKeyOf(text), text);
                }
            });
        }
        // Precondition: genuinely no statistics before the rekey. Without this
        // the assertion below could pass on stale rows and prove nothing.
        assertThat(count("SELECT count(*) FROM pg_stats WHERE schemaname='nexus' "
            + "AND tablename='chash_alias' AND attname='tenant_id'"))
            .as("fixture must start with NO chash_alias statistics")
            .isZero();

        Map<String, Object> counts = rekeyOps.rekey(tenant, false);
        assertThat(((Number) counts.get("alias_rows")).intValue())
            .as("the fixture must actually write alias rows, or the ANALYZE has nothing to see")
            .isEqualTo(40);

        // THE ASSERTION: the rekey's own transaction left usable statistics.
        // Pre-fix this is 0 (no ANALYZE at all); with a permission-skipped
        // ANALYZE it is ALSO 0 — which is exactly why the effect, not the
        // statement, is what gets asserted.
        assertThat(count("SELECT count(*) FROM pg_stats WHERE schemaname='nexus' "
            + "AND tablename='chash_alias' AND attname='tenant_id'"))
            .as("the rekey must leave FRESH planner statistics on chash_alias — an "
                + "in-transaction ANALYZE that Postgres silently skipped for want of "
                + "ownership/MAINTAIN leaves the planner blind (F2: 101min vs 461s)")
            .isEqualTo(1);

        // And they must DESCRIBE this tenant's rows, not merely exist: the
        // production failure was statistics that were present but described a
        // different tenant entirely.
        assertThat(scalar("SELECT most_common_vals::text FROM pg_stats WHERE schemaname='nexus' "
            + "AND tablename='chash_alias' AND attname='tenant_id'"))
            .as("statistics must describe the rows this transaction wrote")
            .contains(tenant);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            exec(su, "ALTER TABLE nexus.chash_alias RESET (autovacuum_enabled)");
        }
    }

    // ── Test 6: the server-side envelope log is the authoritative record ─────

    /**
     * nexus-b878d contract-to-keep: {@code RekeyOps} logs {@code
     * event=rekey_complete} carrying the FULL envelope, server-side, after the
     * transaction commits.
     *
     * <p>This is not decorative logging. During the RDR-180 production cutover
     * the tls sidecar 504'd at 120.3s while the transaction committed 88s
     * later, so the client never received its envelope — and this log line is
     * what recovered it. nexus-b878d removes the long-held request that caused
     * that, but the log stays the authoritative record of what a rekey did, and
     * degrading it to a summary would re-open the same recovery gap.
     *
     * <p>Asserts the envelope's CONTENT, not merely that a line was emitted: it
     * pins the formatted message against the returned map, so dropping any
     * single count from the log fails this test.
     */
    @Test
    @Order(6)
    void rekeyComplete_logsTheFullEnvelopeServerSide() {
        ch.qos.logback.classic.Logger root =
            (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(
                org.slf4j.Logger.ROOT_LOGGER_NAME);
        ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent> logs =
            new ch.qos.logback.core.read.ListAppender<>();
        logs.start();
        root.addAppender(logs);
        try {
            Map<String, Object> counts = rekeyOps.rekey(TA, false);

            var envelopeLines = logs.list.stream()
                .map(ch.qos.logback.classic.spi.ILoggingEvent::getFormattedMessage)
                .filter(m -> m.startsWith("event=rekey_complete"))
                .toList();

            assertThat(envelopeLines)
                .as("exactly one rekey_complete line per rekey")
                .hasSize(1);
            assertThat(envelopeLines.getFirst())
                .as("the line names the tenant it applies to")
                .contains("tenant=" + TA);
            assertThat(envelopeLines.getFirst())
                .as("the line carries the FULL envelope, not a summary of it")
                .contains("counts=" + counts);
        } finally {
            root.detachAppender(logs);
            logs.stop();
        }
    }

    /** The pre-RDR-180 32-hex half-digest of {@code text}, as raw bytes. */
    private static byte[] legacyKeyOf(String text) {
        try {
            byte[] full = MessageDigest.getInstance("SHA-256")
                .digest(text.getBytes(StandardCharsets.UTF_8));
            byte[] half = new byte[16];
            System.arraycopy(full, 0, half, 0, 16);
            return half;
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }

    // ── nexus-t76bp: RekeyOps' manifest-cascade UPDATE (step 5,
    //    "manifest_repointed") must take the SAME sweep gate every other
    //    catalog_document_chunks writer does — the coordinator's
    //    investigation found no code-enforced exclusivity between RekeyOps
    //    and live serving writers (the only lock RekeyOps takes,
    //    'staging:<tenant>', is shared with StagingPromoteOps ONLY; no
    //    serving-path manifest writer ever acquires or checks it), so this
    //    is a fix, not a documented exemption.
    //
    //    REWORKED (critic-p1 Critical, T2 nexus/critique-t76bp-rekey-gate-
    //    2026-08-08 [21807]): round 1's gate sat immediately before step 5
    //    only, leaving steps 3-4 (content rekey -- the step that makes
    //    rekeyed content physically live) entirely ungated. The gate now
    //    acquires before step 3 begins (RekeyOps.rekey step 2b); Order(20)
    //    below still proves the observable behavior (blocks, then
    //    completes with manifest_repointed=1) is unchanged. Order(21)
    //    discriminates the NEW, earlier block point from round 1's via the
    //    physical row's LOCK state (FOR UPDATE NOWAIT), not its content --
    //    a content/count pin cannot discriminate placement at all, since
    //    PostgreSQL has no dirty reads at any isolation level and an
    //    external connection sees the identical pre-rekey committed
    //    snapshot under EITHER placement (code-review-expert T2 review-
    //    11gh6-gate-2026-08-08 [21797] R1: an earlier, content-pinned
    //    version of this test was vacuous for exactly that reason). Order
    //    (22) pins the combined fix's detection half (critic Option 2): a
    //    pre-existing dangling manifest row now aborts the whole rekey
    //    loud, rather than being silently reported in the envelope. ──

    /** Raw connection to the service role's own pool (test-controlled transaction). */
    private Connection dsConnection() throws SQLException {
        return svcDs.getConnection();
    }

    /** Hand-drives {@code CatalogRepository.acquireSweepGateExclusive}'s exact
     *  SQL shape on a raw connection, for tests needing manual transaction control. */
    private static void acquireGateExclusive(Connection conn, String tenant, String collection, int lockTimeoutMs)
            throws SQLException {
        try (var ps = conn.prepareStatement("SELECT set_config('lock_timeout', ?, true)")) {
            ps.setString(1, String.valueOf(lockTimeoutMs));
            ps.execute();
        }
        try (var ps = conn.prepareStatement("SELECT pg_advisory_xact_lock(hashtext(?))")) {
            ps.setString(1, "sweepgate:" + tenant + "/" + collection);
            ps.execute();
        }
    }

    /** Polls {@code pg_stat_activity} until a backend owned by {@code usename}
     *  is OBSERVABLY waiting on an advisory lock ({@code wait_event='advisory'}) --
     *  proof the rekey has genuinely reached its gate, rather than trusting a
     *  fixed {@code future.get(timeout)} window alone to mean "it got there"
     *  (a race on a loaded CI runner: not-yet-blocked and blocked-forever both
     *  look identical to a bare timeout). Used before Order(21)'s lock probe so
     *  that probe cannot race the executor's own startup. */
    private void waitForAdvisoryLockWait(String usename, java.time.Duration timeout) throws Exception {
        long deadlineNanos = System.nanoTime() + timeout.toNanos();
        while (System.nanoTime() < deadlineNanos) {
            int waiting = count("SELECT count(*) FROM pg_stat_activity WHERE usename = '"
                + usename + "' AND wait_event_type = 'Lock' AND wait_event = 'advisory'");
            if (waiting > 0) {
                return;
            }
            Thread.sleep(20);
        }
        throw new AssertionError(
            "rekey backend never reached wait_event='advisory' within " + timeout);
    }

    @Test
    @Order(20)
    void rekey_manifestRepoint_blocksOnExternalExclusiveGate_thenProceeds() throws Exception {
        String tenant = "t-rekey-gate";
        String col = "gate-rekey-col";
        String text = "rekey gate test content " + System.nanoTime();
        byte[] trueChash = sha256(text);
        // A 32-byte value that is NOT sha256(text) -- deliberately mismatched
        // so the rekey pipeline's own predicate ("chash IS DISTINCT FROM
        // sha256(chunk_text)") picks this row up and drives it through the
        // full alias-build + content-rekey + manifest-repoint sequence,
        // exactly like a real digest-mismatch row. MUST be valid UTF-8 (the
        // ETL-era "32-byte-ASCII id" shape, per OLD_REF_LEMMA's
        // convert_from(chash, 'UTF8') branch for any non-16-byte key) --
        // raw sha256 output is NOT valid UTF-8 and would fail that
        // conversion; a plain 32-char ASCII string sidesteps both that AND
        // the octet_length=32 check constraint with no legacy-16-byte
        // juggling needed.
        String bogusOldSeed = ("gate-old-" + System.nanoTime() + "00000000000000000000000000000000")
            .substring(0, 32);
        byte[] bogusOldChash = bogusOldSeed.getBytes(StandardCharsets.UTF_8);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('"
                + tenant + "', '" + col + "') ON CONFLICT DO NOTHING");
            insertChunk(su, tenant, 768, col, bogusOldChash, text);
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title) VALUES ('"
                + tenant + "', 'gate-doc', 'gate doc') ON CONFLICT DO NOTHING");
            try (PreparedStatement ps = su.prepareStatement(
                    "INSERT INTO nexus.catalog_document_chunks "
                    + "(tenant_id, doc_id, position, chash, collection) VALUES (?, ?, 0, ?, ?)")) {
                ps.setString(1, tenant);
                ps.setString(2, "gate-doc");
                ps.setBytes(3, bogusOldChash);
                ps.setString(4, col);
                ps.executeUpdate();
            }
        }

        try (Connection external = dsConnection()) {
            external.setAutoCommit(false);
            try (var st = external.prepareStatement("SET nexus.tenant = '" + tenant + "'")) {
                st.execute();
            }
            // Generous lock_timeout on the EXTERNAL holder's own acquire --
            // uncontended, returns immediately; never bounds rekey's own
            // wait (writers take the gate SHARED with no timeout at all).
            acquireGateExclusive(external, tenant, col, 60_000);

            ExecutorService executor = Executors.newSingleThreadExecutor();
            try {
                Future<Map<String, Object>> future = executor.submit(() -> rekeyOps.rekey(tenant, false));
                assertThatThrownBy(() -> future.get(750, TimeUnit.MILLISECONDS))
                    .as("rekey's manifest-repoint UPDATE must BLOCK while the target collection's "
                        + "gate is held EXCLUSIVE externally -- a missing/broken gate call would "
                        + "let this complete immediately and this assertion would fail")
                    .isInstanceOf(TimeoutException.class);

                external.rollback();

                Map<String, Object> counts = future.get(15, TimeUnit.SECONDS);
                // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk's ON
                // UPDATE CASCADE (F10a, verified for exactly this rekey case) now
                // re-points the manifest row AUTOMATICALLY the instant step (4)'s
                // content rekey (ChashSqlIdioms.contentRekeyUpdateDsl) UPDATEs
                // nexus.chunks.chash -- by the time step (5)'s own explicit
                // "UPDATE catalog_document_chunks ... WHERE chash = old_bytes"
                // runs, the row already carries the NEW chash, so that
                // statement's own affected-row count is legitimately 0, not 1.
                // The manifest is still correctly repointed -- verified by the
                // chash assertion after this block -- just by the FK's cascade
                // instead of the explicit UPDATE.
                assertThat(counts.get("manifest_repointed"))
                    .as("gate released -- the manifest repoint completes via the FK's own ON "
                        + "UPDATE CASCADE now (F10a) -- the explicit UPDATE that used to do this "
                        + "work finds nothing left to touch").isEqualTo(0);
            } finally {
                executor.shutdownNow();
            }
        }

        assertThat(count("SELECT count(*) FROM nexus.catalog_document_chunks "
            + "WHERE tenant_id = '" + tenant + "' AND doc_id = 'gate-doc' "
            + "AND chash = decode('" + HexFormat.of().formatHex(trueChash) + "', 'hex')"))
            .as("the manifest row now points at the rekeyed (true) digest").isEqualTo(1);
    }

    // ── nexus-t76bp REWORK falsification (critic-p1 Critical, T2
    //    nexus/critique-t76bp-rekey-gate-2026-08-08 [21807]): round 1 gated
    //    ONLY step 5's manifest UPDATE, so a rekey blocked externally had
    //    ALREADY rewritten chunks_768's physical chash via step 4 (content
    //    rekey) by the time it blocked -- Order(20) above cannot see this,
    //    since it only observes step 5's OUTCOME after the gate releases.
    //    THIS test observes state WHILE still blocked.
    //
    //    DISCRIMINATION ARGUMENT (code-review-expert T2 review-11gh6-gate-
    //    2026-08-08 [21797] R1): an earlier version of this test pinned the
    //    physical row's CONTENT (a count() query on a fresh connection) and
    //    claimed round 1's placement would fail that pin. That claim was
    //    FALSE: PostgreSQL has no dirty reads at any isolation level, so a
    //    separate connection can never observe another transaction's
    //    uncommitted writes. Under round 1's placement, step 4's UPDATE
    //    would still be uncommitted at the blocked point, so the OLD tuple
    //    version stays the one an external snapshot sees -- the external
    //    count() reports the SAME pre-rekey state under EITHER placement,
    //    discriminating nothing.
    //
    //    Row LOCKS, unlike row VALUES, ARE externally observable under READ
    //    COMMITTED: an UPDATE sets the old tuple's xmax to the updating
    //    transaction, and any other backend requesting that row {@code FOR
    //    UPDATE} must wait for (or, with NOWAIT, immediately fail on) that
    //    xact to resolve, whether or not it has committed. So this test
    //    probes the row with {@code FOR UPDATE NOWAIT} from a separate
    //    connection instead of reading its content. Under the REWORK (gate
    //    at step 2b, before step 3) the row is untouched while blocked --
    //    unlocked, NOWAIT succeeds, which is what this test runs and
    //    asserts against the actual reworked code. Under round 1's
    //    placement (not re-run here -- reverting the gate call order in a
    //    scratch copy to actually falsify this is out of scope for this
    //    fix-up; this is the reasoned argument in its place), step 4's
    //    contentRekeyUpdate would already hold that exact tuple's row lock
    //    -- NOWAIT would raise 55P03 lock_not_available immediately instead
    //    of returning a row, so the assertion below would fail. The
    //    wait_event poll before the probe confirms the rekey backend has
    //    genuinely reached the gate (not merely "hasn't returned within
    //    750ms yet"), so the probe cannot race the executor's own
    //    startup. ──

    @Test
    @Order(21)
    void rekey_gateBlocksBeforeContentRekey_notJustBeforeManifestUpdate() throws Exception {
        String tenant = "t-rekey-gate-b";
        String col = "gate-rekey-col-b";
        String text = "rekey gate content-rekey test " + System.nanoTime();
        byte[] trueChash = sha256(text);
        String trueHex = HexFormat.of().formatHex(trueChash);
        // Same ETL-era 32-byte-ASCII shape as Order(20) -- valid UTF-8 (the
        // OLD_REF_LEMMA convert_from branch), sidesteps the octet-length
        // check with no legacy-16-byte juggling needed.
        String bogusOldSeed = ("gate-b-old-" + System.nanoTime() + "00000000000000000000000000000000")
            .substring(0, 32);
        byte[] bogusOldChash = bogusOldSeed.getBytes(StandardCharsets.UTF_8);
        String bogusOldHex = HexFormat.of().formatHex(bogusOldChash);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('"
                + tenant + "', '" + col + "') ON CONFLICT DO NOTHING");
            insertChunk(su, tenant, 768, col, bogusOldChash, text);
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title) VALUES ('"
                + tenant + "', 'gate-b-doc', 'gate doc b') ON CONFLICT DO NOTHING");
            try (PreparedStatement ps = su.prepareStatement(
                    "INSERT INTO nexus.catalog_document_chunks "
                    + "(tenant_id, doc_id, position, chash, collection) VALUES (?, ?, 0, ?, ?)")) {
                ps.setString(1, tenant);
                ps.setString(2, "gate-b-doc");
                ps.setBytes(3, bogusOldChash);
                ps.setString(4, col);
                ps.executeUpdate();
            }
        }

        try (Connection external = dsConnection()) {
            external.setAutoCommit(false);
            try (var st = external.prepareStatement("SET nexus.tenant = '" + tenant + "'")) {
                st.execute();
            }
            acquireGateExclusive(external, tenant, col, 60_000);

            ExecutorService executor = Executors.newSingleThreadExecutor();
            try {
                Future<Map<String, Object>> future = executor.submit(() -> rekeyOps.rekey(tenant, false));
                assertThatThrownBy(() -> future.get(750, TimeUnit.MILLISECONDS))
                    .as("rekey must block BEFORE step 4's content rekey (step 2b's gate "
                        + "resolution), not just before step 5's manifest UPDATE")
                    .isInstanceOf(TimeoutException.class);

                // Confirm the rekey backend has genuinely reached the gate --
                // see the class-level comment above this test for why this
                // poll, and the lock probe below, are the discriminators
                // (a row-content pin cannot discriminate placement at all).
                waitForAdvisoryLockWait(SVC_ROLE, java.time.Duration.ofSeconds(5));

                // THE DISCRIMINATOR, taken WHILE still blocked: probe the
                // physical row's LOCK state, not its content, via FOR UPDATE
                // NOWAIT on a separate connection. Under the REWORK (this
                // code) the row is untouched at step 2b's block point --
                // unlocked, NOWAIT succeeds and returns the row.
                try (Connection probe = pg.createConnection("")) {
                    probe.setAutoCommit(false);
                    try {
                        try (PreparedStatement ps = probe.prepareStatement(
                                "SELECT 1 FROM nexus.chunks WHERE tenant_id = ? "
                                + "AND chash = decode(?, 'hex') FOR UPDATE NOWAIT")) {
                            ps.setString(1, tenant);
                            ps.setString(2, bogusOldHex);
                            try (ResultSet rs = ps.executeQuery()) {
                                assertThat(rs.next())
                                    .as("content rekey (step 4) must NOT have run while blocked "
                                        + "at step 2b's gate -- the row must be UNLOCKED (FOR "
                                        + "UPDATE NOWAIT succeeds); under round 1's placement "
                                        + "step 4 would already hold this row's lock, raising "
                                        + "55P03 lock_not_available instead")
                                    .isTrue();
                            }
                        }
                    } finally {
                        probe.rollback();
                    }
                }

                external.rollback();

                Map<String, Object> counts = future.get(15, TimeUnit.SECONDS);
                // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk's ON
                // UPDATE CASCADE (F10a, verified for exactly this rekey case) now
                // re-points the manifest row AUTOMATICALLY the instant step (4)'s
                // content rekey (ChashSqlIdioms.contentRekeyUpdateDsl) UPDATEs
                // nexus.chunks.chash -- by the time step (5)'s own explicit
                // "UPDATE catalog_document_chunks ... WHERE chash = old_bytes"
                // runs, the row already carries the NEW chash, so that
                // statement's own affected-row count is legitimately 0, not 1.
                // The manifest is still correctly repointed -- verified by the
                // chash assertion after this block -- just by the FK's cascade
                // instead of the explicit UPDATE.
                assertThat(counts.get("manifest_repointed"))
                    .as("gate released -- the manifest repoint completes via the FK's own ON "
                        + "UPDATE CASCADE now (F10a) -- the explicit UPDATE that used to do this "
                        + "work finds nothing left to touch").isEqualTo(0);
            } finally {
                executor.shutdownNow();
            }
        }

        assertThat(count("SELECT count(*) FROM nexus.chunks WHERE tenant_id='" + tenant
            + "' AND chash = decode('" + trueHex + "', 'hex')"))
            .as("after the gate releases, content rekey completes too").isEqualTo(1);
        assertThat(count("SELECT count(*) FROM nexus.catalog_document_chunks WHERE tenant_id='" + tenant
            + "' AND doc_id = 'gate-b-doc' AND chash = decode('" + trueHex + "', 'hex')"))
            .as("and the manifest points at the rekeyed digest").isEqualTo(1);
    }

    // ── nexus-t76bp REWORK falsification of the DETECTION half (critic
    //    Option 2): the counts were computed pre-rework too, but never
    //    enforced -- delete the throw in RekeyOps.rekey's step 6 and THIS
    //    test fails. Mirrors StagingPromoteOpsIntegrationTest's
    //    preExistingDanglingManifestRow_abortsFinalizeLoud exactly. ──

    @Test
    @Order(22)
    void preExistingDanglingManifestRow_abortsRekeyLoud() throws Exception {
        String tenant = "t-rekey-dangling";
        byte[] ghost = sha256("rekey pre-existing dangling ghost " + System.nanoTime());
        // nexus-4okz4 increment 2 critic follow-up (critic nit, costless —
        // T2 critique-4okz4-increment2 [21864]): distinct collection name
        // per dim (one-collection-one-dim convention), rather than the same
        // "gate-dangling-col" reused across all three dim inserts.
        String col384 = "gate-dangling-col-384";
        String col768 = "gate-dangling-col-768";
        String col1024 = "gate-dangling-col-1024";
        // nexus-4okz4 increment 1, critic ROUND 3 pin-sensitivity finding
        // (T2 critique-t76bp-rekey-gate-2026-08-08 [21807], "Site 2
        // correlation is unpinned"): ChashSqlIdioms.danglingManifestCountDsl
        // was THREE correlated NOT EXISTS subqueries (m.chash matched
        // against each dim table's own chash) prior to RDR-191. A
        // de-correlated rewrite (e.g. a dropped .where() turning
        // "NOT EXISTS(SELECT 1 FROM <table> WHERE chash = m.chash)" into the
        // unconditional "NOT EXISTS(SELECT 1 FROM <table>)") passed every
        // fixture that predates this row: this tenant previously had ZERO
        // content rows anywhere, so the de-correlated form's "table is
        // empty" check was ALSO true, giving the identical
        // count=1/abort-fires result as the correlated form -- no
        // discrimination. One unrelated, live content row per collection
        // (all schema-valid: same chash, three DIFFERENT collections, so no
        // PK collision under the unified table either) closes that gap
        // regardless of dim.
        //
        // RDR-191 repoint (nexus-o8dil.17): ChashSqlIdioms.java (including
        // danglingManifestCountDsl) is OUT OF THIS FILE'S SCOPE -- it is a
        // separate, as-yet-unrepointed census file (T2
        // nexus/rdr-191-batch-D1-2026-08-13 [22460] flags it "unassigned to
        // a specific D-slot" pending orchestrator triage). This fixture is
        // left schema-valid either way (three distinct collections, so no
        // PK collision regardless of whether that method collapses to one
        // correlated NOT EXISTS against the unified nexus.chunks or stays
        // structurally three) and the assertion below only checks the
        // coarse "dangling manifest" message, so it does not assume that
        // method's post-repoint internal shape. Once ChashSqlIdioms is
        // repointed, re-verify this fixture still exercises real
        // correlation (not vacuously passing because the table is
        // non-empty for unrelated reasons).
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            for (String c : new String[] {col384, col768, col1024}) {
                su.createStatement().execute(
                    "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('"
                    + tenant + "', '" + c + "') ON CONFLICT DO NOTHING");
            }
            String unrelatedText = "rekey dangling-count correlation fixture " + System.nanoTime();
            insertChunk(su, tenant, 384, col384, sha256(unrelatedText), unrelatedText);
            insertChunk(su, tenant, 768, col768, sha256(unrelatedText), unrelatedText);
            insertChunk(su, tenant, 1024, col1024, sha256(unrelatedText), unrelatedText);
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title) VALUES ('"
                + tenant + "', 'ghost-doc', 'ghost') ON CONFLICT DO NOTHING");
            // nexus-7nrvr: catalog_document_chunks.collection is NOT NULL
            // (catalog-025-collection-not-null.xml). The dangling/orphan
            // nature this row exists to prove is about the CHASH — "ghost"
            // above and below names a chash matched by no row in ANY
            // chunks_<dim> table, by construction — never about the
            // collection value, which is orthogonal. col384 is an arbitrary
            // real, already-registered collection (chosen for no reason
            // other than existing); the dangling detection correlates on
            // chash alone, so this does not touch the coverage.
            // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk now
            // requires a matching nexus.chunks row -- a genuinely-dangling row
            // is exactly this test's SUBJECT, so bypass the FK locally: drop
            // the constraint, insert, then re-add it NOT VALID (catalog-029-0's
            // exact shape) so it is live again (unvalidated) afterward.
            su.createStatement().execute(
                "ALTER TABLE nexus.catalog_document_chunks DROP CONSTRAINT IF EXISTS fk_catalog_chunks_chunk");
            try (PreparedStatement ps = su.prepareStatement(
                    "INSERT INTO nexus.catalog_document_chunks "
                    + "(tenant_id, doc_id, position, chash, collection) VALUES (?, 'ghost-doc', 0, ?, ?)")) {
                ps.setString(1, tenant);
                ps.setBytes(2, ghost);
                ps.setString(3, col384);
                ps.executeUpdate();
            }
            su.createStatement().execute(
                "ALTER TABLE nexus.catalog_document_chunks "
                + "ADD CONSTRAINT fk_catalog_chunks_chunk "
                + "FOREIGN KEY (tenant_id, collection, chash) REFERENCES nexus.chunks (tenant_id, collection, chash) "
                + "ON UPDATE CASCADE DEFERRABLE INITIALLY IMMEDIATE NOT VALID");
        }

        assertThatThrownBy(() -> rekeyOps.rekey(tenant, false))
            .as("the step-6 detection must be ENFORCED, not merely computed and returned in the "
                + "envelope -- a caller that never inspects the envelope must not be able to miss "
                + "this, AND the count must be CORRELATED per row (an unrelated content row in "
                + "nexus.chunks, any collection, must not mask the ghost manifest row's dangling "
                + "reference)")
            .isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("dangling manifest");
    }
}
