// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

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
import org.testcontainers.containers.PostgreSQLContainer;
import org.w3c.dom.Document;
import org.w3c.dom.Element;
import org.w3c.dom.Node;
import org.w3c.dom.NodeList;

import javax.xml.parsers.DocumentBuilderFactory;
import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.SQLWarning;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-tk070.p3b (RDR-194 § D1 steps b/c/d) — direct proof of
 * {@code taxonomy-010-source-collection-backfill.xml}'s three-arm remediation
 * SQL, complementing {@link SchemaUpgradeRehearsalIntegrationTest}'s
 * {@code taxonomy-010-1} leg.
 *
 * <p><strong>Why this test exists alongside the rehearsal leg.</strong> The
 * rehearsal seeds its backing chunk content into the OLD (pre-unify)
 * {@code chunks_384}/{@code chunks_768}/{@code chunks_1024} tables, the ONLY
 * route into {@code nexus.chunks} available before {@code vectors-004-1}
 * creates it mid-hop. Those tables carry a {@code length(chash) = 32} CHECK
 * inherited from the OLD leg's tree ({@code catalog-002-hygiene.xml}), so
 * EVERY chash seedable there is legacy-width (16 bytes post-decode) — never
 * the canonical 64-hex/32-byte shape {@code taxonomy-010-1}'s backfill (b)
 * arm actually resolves. That structural constraint makes the positive
 * (b) UNIQUE-RESOLUTION backfill arm impossible to construct via the
 * rehearsal's own seeding mechanism (see that test's own SEED-block comment
 * for the full derivation). This test sidesteps it entirely: it seeds FRESH,
 * genuinely canonical 64-hex content straight into {@code nexus.chunks}
 * AFTER a full HEAD-schema migration (bytea from creation, no legacy-width
 * CHECK in the way), giving the bead's own explicit TDD ask ("seed a fixture
 * with one uniquely-resolvable + one ambiguous + one unresolvable row; assert
 * exactly one survives with the right collection and BOTH delete counts
 * reported") a fixture that can actually hold all three shapes. A FOURTH row
 * was added 2026-08-17 (critical fix round, critic CRITICAL / bead
 * nexus-i3k3e): a shape-invalid doc_id with a NON-NULL source_collection
 * already set (the cc4 census's 1,262-row wedge class), proving the fixed
 * UNRESOLVABLE delete's shape branch is unconditional on source_collection.
 *
 * <p><strong>Re-running a one-shot changeset.</strong>
 * {@code taxonomy-010-1} already executed once (over zero rows) during the
 * {@code @BeforeAll} full migration, so {@code source_collection} is already
 * {@code NOT NULL} and {@code SchemaMigrator} will never run that changeset
 * again (Liquibase records it in {@code databasechangelog}). This test
 * DROPs the {@code NOT NULL} constraint back off (test-only relaxation),
 * seeds NULL-{@code source_collection} rows, then re-executes the changeset's
 * OWN {@code <sql>} text — read directly out of the changelog XML at test
 * time via {@link #extractChangesetSql} rather than duplicated inline —
 * so there is no second copy of the SQL to drift out of sync with the
 * migration file. The extracted text is executed via a plain JDBC
 * {@link Statement#execute(String)} (not Liquibase), which lets this test
 * additionally capture the changeset's {@code RAISE NOTICE} counts off the
 * driver's {@link SQLWarning} chain — direct proof of the bead's "BOTH
 * delete counts reported" requirement that the rehearsal leg's own comment
 * documents as unavailable to it (no warning listener on Liquibase's
 * internal migration connection).
 *
 * <p>Runs entirely as the container superuser (implicit BYPASSRLS): the
 * FORCE-RLS toggle-wrap DISCIPLINE itself (does the DML actually see rows
 * under the non-BYPASSRLS {@code nexus_admin} owner) is already proven by
 * the rehearsal leg's other three arms (ambiguous / unresolvable /
 * legacy-shape-coincidental); this test's sole job is the SQL LOGIC.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Taxonomy010BackfillDirectIntegrationTest {

    private static final String CHANGELOG_FILE = "db/changelog/taxonomy-010-source-collection-backfill.xml";
    private static final String CHANGESET_ID = "taxonomy-010-1";
    private static final String TENANT = "p3b-direct";

    PostgreSQLContainer<?> pg;

    @BeforeAll
    void startAll() throws Exception {
        // Dedicated, not shared: this test DROPs then re-applies a NOT NULL
        // constraint on nexus.topic_assignments.source_collection, a global
        // schema mutation that must not leak into any other test class's
        // shared-cluster assumptions (PgContainerHelper.start()'s own
        // javadoc names exactly this class of test as required to opt out).
        pg = PgContainerHelper.startDedicated();
        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db).update(new Contexts());
        }
    }

    @AfterAll
    void stopAll() {
        if (pg != null) {
            pg.stop();
        }
    }

    @Test
    void backfillDeleteAndSetNotNull_uniqueAmbiguousUnresolvable() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);

            // taxonomy-010-1 already ran once (zero rows) during startAll()'s
            // full migration; SET NOT NULL is already in place. Relax it back
            // off so this test can seed NULL-source_collection input, exactly
            // the pre-remediation shape the real migration walk saw.
            su.createStatement().execute(
                "ALTER TABLE nexus.topic_assignments ALTER COLUMN source_collection DROP NOT NULL");
            // RDR-194 P3c (nexus-tk070.p3c): startAll()'s full migration now
            // ALSO runs taxonomy-011-1, converting doc_id to bytea. This test
            // replays taxonomy-010-1's OWN <sql> text unmodified (extractChangesetSql,
            // see class javadoc) -- that text was written for a TEXT-typed doc_id
            // (`ta.doc_id ~ '^[0-9a-f]{64}$'`, `encode(c.chash,'hex') = ta.doc_id`)
            // and would fail to typecheck against bytea (`~` is not defined for
            // bytea; the TEXT/bytea equality would not typecheck either). Same
            // test-only relaxation technique as source_collection above: revert
            // doc_id to TEXT for the scope of this dedicated, throwaway container
            // so the changeset's own SQL runs exactly as authored.
            //
            // RDR-194 critical fix round (2026-08-17, nexus-i3k3e/Sig-2):
            // startAll()'s full migration now ALSO runs taxonomy-011-8,
            // which Liquibase-owns a LIVE nexus.diag_chash_conformance view
            // depending on doc_id. The ALTER below would now hit the exact
            // "cannot alter type of a column used by a view or rule" error
            // taxonomy-011-1's own forward pass exists to dodge -- drop the
            // view first (same test-only-relaxation shape as the two
            // statements above; a real migration walk never does this,
            // taxonomy-011-8 recreates it going forward every boot).
            su.createStatement().execute(
                "DROP VIEW IF EXISTS nexus.diag_chash_conformance");
            su.createStatement().execute(
                "ALTER TABLE nexus.topic_assignments ALTER COLUMN doc_id TYPE TEXT USING encode(doc_id, 'hex')");

            registerCollection(su, TENANT, "code__x");
            registerCollection(su, TENANT, "code__y");
            long topicId = seedTopic(su, TENANT, "code__x", "direct-p3b-topic");

            // (b) UNIQUE-RESOLUTION arm: fresh canonical 64-hex chash under
            // exactly ONE collection.
            String uniqueChash = "a".repeat(64);
            seedChunk(su, TENANT, "code__x", uniqueChash, 384);
            seedTopicAssignment(su, TENANT, uniqueChash, topicId);

            // (c) AMBIGUOUS arm: the SAME chash under TWO distinct collections.
            String ambiguousChash = "b".repeat(64);
            seedChunk(su, TENANT, "code__x", ambiguousChash, 384);
            seedChunk(su, TENANT, "code__y", ambiguousChash, 768);
            seedTopicAssignment(su, TENANT, ambiguousChash, topicId);

            // (c) UNRESOLVABLE (anti-join) arm: canonical 64-hex SHAPE, no
            // backing chunk anywhere.
            String unresolvableChash = "c".repeat(64);
            seedTopicAssignment(su, TENANT, unresolvableChash, topicId);

            // (c) UNRESOLVABLE (shape-invalid, NON-NULL source_collection)
            // arm — the exact cc4/nexus-i3k3e cloud wedge fixture (critical
            // fix round, 2026-08-17). This is the cc4 census's 1,262-row
            // class: a legacy 32-hex (non-canonical-shape) doc_id whose
            // source_collection was ALREADY set by the pre-P3a
            // projection/cross-collection writer branch, which persisted
            // source_collection unconditionally. Before the 2026-08-17 fix,
            // the UNRESOLVABLE delete's entire predicate (shape check AND
            // anti-join) was scoped to `source_collection IS NULL`, so this
            // row's non-NULL source_collection would have made it SURVIVE
            // this changeset untouched, wedging on taxonomy-011-1's guard
            // downstream. Post-fix, the shape-invalid branch is
            // unconditional on source_collection, so this row must be
            // deleted despite its non-NULL source_collection.
            String legacyNonNullChash = "d".repeat(32);
            seedTopicAssignment(su, TENANT, legacyNonNullChash, topicId, "code__x");

            assertThat(count(su,
                "SELECT count(*) FROM nexus.topic_assignments "
                + "WHERE tenant_id = '" + TENANT + "' AND source_collection IS NULL"))
                .as("ground truth before re-running taxonomy-010-1's SQL: the three "
                    + "NULL-source_collection seeded rows (unique/ambiguous/unresolvable) "
                    + "— the fourth (legacyNonNullChash) is deliberately NOT NULL")
                .isEqualTo(3);
            assertThat(count(su,
                "SELECT count(*) FROM nexus.topic_assignments WHERE tenant_id = '" + TENANT + "'"))
                .as("ground truth before re-running taxonomy-010-1's SQL: four rows total")
                .isEqualTo(4);

            String sql = extractChangesetSql(CHANGELOG_FILE, CHANGESET_ID);
            List<String> notices;
            try (Statement st = su.createStatement()) {
                st.execute(sql);
                notices = collectNotices(st.getWarnings());
            }

            // ── BOTH delete counts (plus the backfill count), read directly
            // off the driver's SQLWarning chain -- the RAISE NOTICE proof the
            // rehearsal leg's own comment documents as unavailable to it. ──
            assertThat(notices)
                .as("all three RAISE NOTICE messages must be captured, in order")
                .hasSize(3);
            assertThat(notices.get(0))
                .as("backfill count must be exactly 1 (the unique-resolution row)")
                .contains("backfilled 1 row(s) with a unique-resolution collection");
            assertThat(notices.get(1))
                .as("ambiguous-delete count must be exactly 1")
                .contains("deleted 1 ambiguous row(s)");
            assertThat(notices.get(2))
                .as("unresolvable-delete count must be exactly 2 (RDR-194 critical fix "
                    + "round, 2026-08-17, nexus-i3k3e) -- the pre-existing canonical-shape "
                    + "anti-join row (unresolvableChash) PLUS the new shape-invalid "
                    + "non-NULL-source_collection row (legacyNonNullChash), the cc4 wedge "
                    + "class -- one DELETE statement, one ROW_COUNT, both rows")
                .contains("deleted 2 unresolvable row(s)");

            // ── Row-state ground truth: exactly the unique-resolution row
            // survives, backfilled with its ONE real collection. ──
            assertThat(count(su,
                "SELECT count(*) FROM nexus.topic_assignments "
                + "WHERE tenant_id = '" + TENANT + "'"))
                .as("exactly one of the four seeded rows survives")
                .isEqualTo(1);
            assertThat(count(su,
                "SELECT count(*) FROM nexus.topic_assignments "
                + "WHERE tenant_id = '" + TENANT + "' AND doc_id = '" + uniqueChash
                + "' AND source_collection = 'code__x'"))
                .as("the surviving row must be the unique-resolution one, backfilled to its "
                    + "real collection -- not merely non-NULL")
                .isEqualTo(1);
            assertThat(count(su,
                "SELECT count(*) FROM nexus.topic_assignments "
                + "WHERE tenant_id = '" + TENANT + "' AND doc_id = '" + ambiguousChash + "'"))
                .as("the ambiguous row must be gone")
                .isEqualTo(0);
            assertThat(count(su,
                "SELECT count(*) FROM nexus.topic_assignments "
                + "WHERE tenant_id = '" + TENANT + "' AND doc_id = '" + unresolvableChash + "'"))
                .as("the unresolvable row must be gone")
                .isEqualTo(0);
            assertThat(count(su,
                "SELECT count(*) FROM nexus.topic_assignments "
                + "WHERE tenant_id = '" + TENANT + "' AND doc_id = '" + legacyNonNullChash + "'"))
                .as("RDR-194 critical fix round (nexus-i3k3e): the shape-invalid row must be "
                    + "gone DESPITE its non-NULL source_collection -- direct proof the "
                    + "2026-08-17 fix's shape branch is unconditional on source_collection, "
                    + "not the pre-fix behavior that would have left this row behind")
                .isEqualTo(0);
            assertThat(count(su,
                "SELECT count(*) FROM information_schema.columns "
                + "WHERE table_schema = 'nexus' AND table_name = 'topic_assignments' "
                + "AND column_name = 'source_collection' AND is_nullable = 'NO'"))
                .as("SET NOT NULL must have re-applied cleanly -- only reachable if the DML "
                    + "ahead of it left no NULL row for this tenant behind")
                .isEqualTo(1);
            // ── The guard taxonomy-011-1 runs next in the real changelog walk
            // (this test replays taxonomy-010-1 alone, not the full walk) --
            // direct proof this tenant's surviving rows would now PASS that
            // guard's exact predicate, closing the loop on the nexus-i3k3e
            // wedge: before the fix, this count would have been 1 (the
            // legacyNonNullChash row survives), which is exactly what would
            // have RAISE EXCEPTIONed taxonomy-011-1's guard on the cloud walk.
            assertThat(count(su,
                "SELECT count(*) FROM nexus.topic_assignments "
                + "WHERE tenant_id = '" + TENANT + "' AND doc_id !~ '^[0-9a-f]{64}$'"))
                .as("taxonomy-011-1's own guard predicate must find zero non-canonical "
                    + "doc_id rows for this tenant after taxonomy-010-1's fixed DELETE -- "
                    + "the direct nexus-i3k3e wedge-closure proof")
                .isEqualTo(0);
        }
    }

    // ── Helpers ───────────────────────────────────────────────────────────

    /**
     * Read the direct {@code <sql>} children of {@code <changeSet id="changesetId">}
     * out of {@code changelogFile} (classpath resource), concatenated in document
     * order. Deliberately NOT recursive ({@code getElementsByTagNameNS} would also
     * pull in the {@code <rollback>} element's own nested {@code <sql>} children,
     * since both are descendants of {@code <changeSet>}) -- only direct children
     * are the changeset's FORWARD SQL.
     */
    private static String extractChangesetSql(String changelogFile, String changesetId) throws Exception {
        Document doc;
        try (var in = Taxonomy010BackfillDirectIntegrationTest.class.getClassLoader()
                .getResourceAsStream(changelogFile)) {
            if (in == null) {
                throw new IllegalStateException("changelog not found on classpath: " + changelogFile);
            }
            var factory = DocumentBuilderFactory.newInstance();
            factory.setNamespaceAware(true);
            doc = factory.newDocumentBuilder().parse(in);
        }
        NodeList changeSets = doc.getElementsByTagNameNS(
            "http://www.liquibase.org/xml/ns/dbchangelog", "changeSet");
        for (int i = 0; i < changeSets.getLength(); i++) {
            Element cs = (Element) changeSets.item(i);
            if (changesetId.equals(cs.getAttribute("id"))) {
                StringBuilder sb = new StringBuilder();
                NodeList children = cs.getChildNodes();
                for (int j = 0; j < children.getLength(); j++) {
                    Node n = children.item(j);
                    if (n.getNodeType() == Node.ELEMENT_NODE && "sql".equals(n.getLocalName())) {
                        sb.append(n.getTextContent()).append('\n');
                    }
                }
                return sb.toString();
            }
        }
        throw new IllegalStateException("changeset not found: " + changesetId + " in " + changelogFile);
    }

    private static List<String> collectNotices(SQLWarning first) {
        List<String> out = new ArrayList<>();
        SQLWarning w = first;
        while (w != null) {
            out.add(w.getMessage());
            w = w.getNextWarning();
        }
        return out;
    }

    private static void registerCollection(Connection c, String tenant, String name) throws Exception {
        try (var ps = c.prepareStatement(
            "INSERT INTO nexus.catalog_collections (tenant_id, name) "
            + "VALUES (?, ?) ON CONFLICT DO NOTHING")) {
            ps.setString(1, tenant);
            ps.setString(2, name);
            ps.executeUpdate();
        }
    }

    private static long seedTopic(Connection c, String tenant, String collection, String label)
            throws Exception {
        try (var ps = c.prepareStatement(
            "INSERT INTO nexus.topics (tenant_id, label, collection, created_at) "
            + "VALUES (?, ?, ?, now()) RETURNING id")) {
            ps.setString(1, tenant);
            ps.setString(2, label);
            ps.setString(3, collection);
            try (ResultSet rs = ps.executeQuery()) {
                rs.next();
                return rs.getLong(1);
            }
        }
    }

    private static void seedTopicAssignment(Connection c, String tenant, String docId, long topicId)
            throws Exception {
        try (var ps = c.prepareStatement(
            "INSERT INTO nexus.topic_assignments (tenant_id, doc_id, topic_id, assigned_by) "
            + "VALUES (?, ?, ?, 'direct-test-seed')")) {
            ps.setString(1, tenant);
            ps.setString(2, docId);
            ps.setLong(3, topicId);
            ps.executeUpdate();
        }
    }

    /**
     * A {@code nexus.topic_assignments} row with an EXPLICIT, non-NULL
     * {@code source_collection} (RDR-194 critical fix round, 2026-08-17,
     * nexus-i3k3e) -- the cc4 census's 1,262-row wedge class: a legacy
     * non-canonical-shape {@code doc_id} whose {@code source_collection}
     * was already set by a pre-P3a writer branch, which the ORIGINAL
     * (pre-fix) NULL-scoped UNRESOLVABLE delete predicate silently skipped.
     */
    private static void seedTopicAssignment(
            Connection c, String tenant, String docId, long topicId, String sourceCollection)
            throws Exception {
        try (var ps = c.prepareStatement(
            "INSERT INTO nexus.topic_assignments "
            + "(tenant_id, doc_id, topic_id, assigned_by, source_collection) "
            + "VALUES (?, ?, ?, 'direct-test-seed', ?)")) {
            ps.setString(1, tenant);
            ps.setString(2, docId);
            ps.setLong(3, topicId);
            ps.setString(4, sourceCollection);
            ps.executeUpdate();
        }
    }

    /** A fresh {@code nexus.chunks} row -- already bytea, no legacy-width CHECK. */
    private static void seedChunk(Connection c, String tenant, String collection, String chashHex, int dim)
            throws Exception {
        String embeddingCol = "embedding_" + dim;
        try (var ps = c.prepareStatement(
            "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, " + embeddingCol + ") "
            + "VALUES (?, ?, decode(?, 'hex'), ?, ?::vector) "
            + "ON CONFLICT (tenant_id, collection, chash) DO NOTHING")) {
            ps.setString(1, tenant);
            ps.setString(2, collection);
            ps.setString(3, chashHex);
            ps.setString(4, "direct test chunk " + chashHex.substring(0, 8));
            ps.setString(5, "[" + "0,".repeat(dim - 1) + "0]");
            ps.executeUpdate();
        }
    }

    private static int count(Connection c, String sql) throws Exception {
        try (Statement st = c.createStatement(); ResultSet rs = st.executeQuery(sql)) {
            rs.next();
            return rs.getInt(1);
        }
    }
}
