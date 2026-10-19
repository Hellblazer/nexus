// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import java.util.HexFormat;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
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
import java.sql.SQLWarning;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
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

    // Walk-point shape of nexus.topic_assignments: this test relaxes source_collection to nullable and
    // doc_id to TEXT (the generated TOPIC_ASSIGNMENTS has bytea doc_id), so it reads the table by name.
    private static final org.jooq.Table<?> TA = DSL.table(DSL.name("nexus", "topic_assignments"));
    private static final org.jooq.Field<String> TA_TENANT = DSL.field(DSL.name("nexus", "topic_assignments", "tenant_id"), String.class);
    private static final org.jooq.Field<String> TA_DOC_ID = DSL.field(DSL.name("nexus", "topic_assignments", "doc_id"), String.class);
    private static final org.jooq.Field<String> TA_SOURCE_COLLECTION =
        DSL.field(DSL.name("nexus", "topic_assignments", "source_collection"), String.class);

    PostgreSQLContainer<?> pg;

    @BeforeAll
    void startAll() throws Exception {
        // Its own database (a template clone): this test DROPs then re-applies a NOT NULL
        // constraint on nexus.topic_assignments.source_collection. The mutation is
        // database-local, so no other class sees it (nexus-7xfh7: this used to boot a
        // container of its own on the strength of a rule the per-class clone made stale).
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
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
            DSL.using(su, SQLDialect.POSTGRES).alterTable(TOPIC_ASSIGNMENTS).alterColumn(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION).dropNotNull()
                .execute();
            // RDR-194 P3c (nexus-tk070.p3c): startAll()'s full migration now
            // ALSO runs taxonomy-011-1, converting doc_id to bytea. This test
            // replays taxonomy-010-1's OWN <sql> text unmodified (extractChangesetSql,
            // see class javadoc) -- that text was written for a TEXT-typed doc_id
            // (`ta.doc_id ~ '^[0-9a-f]{64}$'`, `encode(c.chash,'hex') = ta.doc_id`)
            // and would fail to typecheck against bytea (`~` is not defined for
            // bytea; the TEXT/bytea equality would not typecheck either). Same
            // test-only relaxation technique as source_collection above: revert
            // doc_id to TEXT for the scope of this throwaway database
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
            DSL.using(su, SQLDialect.POSTGRES).dropViewIfExists(DSL.name("nexus", "diag_chash_conformance")).execute();
            // RDR-194 P3d (nexus-tk070.p3d): startAll()'s full migration now ALSO
            // runs taxonomy-012, which ADDs topic_assignments_chunk_fk -- a
            // composite FK whose (source_collection, doc_id) leg targets
            // nexus.chunks(collection, chash), bytea. The ALTER below would now
            // hit "foreign key constraint ... cannot be implemented" (a bytea-vs-
            // TEXT type mismatch on the referencing column) the moment doc_id
            // stops being bytea -- same test-only-relaxation shape as the view
            // drop directly above: this throwaway database never
            // re-adds the constraint, because this test replays taxonomy-010-1
            // alone (never touches taxonomy-011/012's own logic) and the whole
            // container is stopped in @AfterAll.
            DSL.using(su, SQLDialect.POSTGRES).alterTable(TOPIC_ASSIGNMENTS).dropConstraintIfExists("topic_assignments_chunk_fk").execute();
            PgContainerHelper.revertTopicAssignmentsDocIdToText(su);

            // RDR-225: a collection holds one embedding model, so one width. code__x holds the 384-wide chunks
            // below and code__y the 768-wide one; the ambiguous chash is one chunk in each of the two.
            registerCollection(su, TENANT, "code__x", "minilm-l6-v2-384");
            registerCollection(su, TENANT, "code__y", "bge-base-en-v15-768");
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

            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(ctx.fetchCount(TA, TA_TENANT.eq(TENANT), TA_SOURCE_COLLECTION.isNull()))
                .as("ground truth before re-running taxonomy-010-1's SQL: the three "
                    + "NULL-source_collection seeded rows (unique/ambiguous/unresolvable) "
                    + "— the fourth (legacyNonNullChash) is deliberately NOT NULL")
                .isEqualTo(3);
            assertThat(ctx.fetchCount(TA, TA_TENANT.eq(TENANT)))
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
            assertThat(ctx.fetchCount(TA, TA_TENANT.eq(TENANT)))
                .as("exactly one of the four seeded rows survives")
                .isEqualTo(1);
            assertThat(ctx.fetchCount(TA, TA_TENANT.eq(TENANT), TA_DOC_ID.eq(uniqueChash),
                    TA_SOURCE_COLLECTION.eq("code__x")))
                .as("the surviving row must be the unique-resolution one, backfilled to its "
                    + "real collection -- not merely non-NULL")
                .isEqualTo(1);
            assertThat(ctx.fetchCount(TA, TA_TENANT.eq(TENANT), TA_DOC_ID.eq(ambiguousChash)))
                .as("the ambiguous row must be gone")
                .isEqualTo(0);
            assertThat(ctx.fetchCount(TA, TA_TENANT.eq(TENANT), TA_DOC_ID.eq(unresolvableChash)))
                .as("the unresolvable row must be gone")
                .isEqualTo(0);
            assertThat(ctx.fetchCount(TA, TA_TENANT.eq(TENANT), TA_DOC_ID.eq(legacyNonNullChash)))
                .as("RDR-194 critical fix round (nexus-i3k3e): the shape-invalid row must be "
                    + "gone DESPITE its non-NULL source_collection -- direct proof the "
                    + "2026-08-17 fix's shape branch is unconditional on source_collection, "
                    + "not the pre-fix behavior that would have left this row behind")
                .isEqualTo(0);
            var sourceCollection = PgCatalogProbes.columnInfo(ctx, "nexus", "topic_assignments", "source_collection");
            assertThat(sourceCollection).as("source_collection column must exist").isNotNull();
            assertThat(sourceCollection.nullable())
                .as("SET NOT NULL must have re-applied cleanly -- only reachable if the DML "
                    + "ahead of it left no NULL row for this tenant behind")
                .isFalse();
            // ── The guard taxonomy-011-1 runs next in the real changelog walk
            // (this test replays taxonomy-010-1 alone, not the full walk) --
            // direct proof this tenant's surviving rows would now PASS that
            // guard's exact predicate, closing the loop on the nexus-i3k3e
            // wedge: before the fix, this count would have been 1 (the
            // legacyNonNullChash row survives), which is exactly what would
            // have RAISE EXCEPTIONed taxonomy-011-1's guard on the cloud walk.
            assertThat(ctx.fetchCount(TA, TA_TENANT.eq(TENANT), TA_DOC_ID.notLikeRegex("^[0-9a-f]{64}$")))
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

    private static void registerCollection(Connection c, String tenant, String name, String model) throws Exception {
        // RDR-204 nexus-ft04v.4/.5: delegates to PgContainerHelper.insertCollection.
        PgContainerHelper.insertCollection(DSL.using(c, SQLDialect.POSTGRES), tenant, name, model);
    }

    private static long seedTopic(Connection c, String tenant, String collection, String label)
            throws Exception {
        return DSL.using(c, SQLDialect.POSTGRES).insertInto(TOPICS)
            .set(TOPICS.TENANT_ID, tenant)
            .set(TOPICS.LABEL, label)
            .set(TOPICS.COLLECTION, collection)
            .set(TOPICS.CREATED_AT, DSL.currentOffsetDateTime())
            .returningResult(TOPICS.ID)
            .fetchOne(TOPICS.ID);
    }

    /**
     * {@code doc_id} as TEXT: the generated {@code TOPIC_ASSIGNMENTS.DOC_ID} is bytea, but this class reverts
     * the column to TEXT before seeding (see the test body), so the seeds bind the hex string by name.
     */
    private static final org.jooq.Field<String> TEXT_DOC_ID = DSL.field(DSL.name("doc_id"), String.class);

    private static void seedTopicAssignment(Connection c, String tenant, String docId, long topicId)
            throws Exception {
        DSL.using(c, SQLDialect.POSTGRES).insertInto(TOPIC_ASSIGNMENTS)
            .set(TOPIC_ASSIGNMENTS.TENANT_ID, tenant)
            .set(TEXT_DOC_ID, docId)
            .set(TOPIC_ASSIGNMENTS.TOPIC_ID, topicId)
            .set(TOPIC_ASSIGNMENTS.ASSIGNED_BY, "direct-test-seed")
            .set(TOPIC_ASSIGNMENTS.EMBEDDING_MODEL, "bge-base-en-v15-768")
            .execute();
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
        DSL.using(c, SQLDialect.POSTGRES).insertInto(TOPIC_ASSIGNMENTS)
            .set(TOPIC_ASSIGNMENTS.TENANT_ID, tenant)
            .set(TEXT_DOC_ID, docId)
            .set(TOPIC_ASSIGNMENTS.TOPIC_ID, topicId)
            .set(TOPIC_ASSIGNMENTS.ASSIGNED_BY, "direct-test-seed")
            .set(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, sourceCollection)
            .set(TOPIC_ASSIGNMENTS.EMBEDDING_MODEL, "bge-base-en-v15-768")
            .execute();
    }

    /** A fresh {@code nexus.chunks} row -- already bytea, no legacy-width CHECK. */
    private static void seedChunk(Connection c, String tenant, String collection, String chashHex, int dim)
            throws Exception {
        DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
        ctx.insertInto(CHUNKS)
            .set(CHUNKS.TENANT_ID, tenant)
            .set(CHUNKS.COLLECTION, collection)
            .set(CHUNKS.CHASH, HexFormat.of().parseHex(chashHex))
            .set(CHUNKS.EMBEDDING_MODEL, PgContainerHelper.collectionModel(ctx, tenant, collection))
            .set(CHUNKS.CHUNK_TEXT, "direct test chunk " + chashHex.substring(0, 8))
            .set(VectorSeeds.chunkEmbedding(dim), VectorSeeds.vec(dim, 0f))
            .onConflict(CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL).doNothing()
            .execute();
    }
}
