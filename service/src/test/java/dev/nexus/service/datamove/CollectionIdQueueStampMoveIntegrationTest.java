// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.P31CheckReplay;
import dev.nexus.service.PgCatalogProbes;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.RetiredAspectQueueTable;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.w3c.dom.Element;
import org.w3c.dom.Node;

import javax.xml.XMLConstants;
import javax.xml.parsers.DocumentBuilderFactory;
import java.io.InputStream;
import java.nio.file.Files;
import java.sql.Connection;
import java.time.Clock;
import java.util.ArrayList;
import java.util.List;

/**
 * RDR-229 P3.1 residual R3 (nexus-p2m1x.42), acceptance (a) and (b), for the retired queue's stamp move
 * {@code rdr229-stamp-aspect_extraction_queue}: the completion check reads sixteen labels, and the queue's is the one the file's other
 * fifteen cannot stand in for. Where the queue table is kept with its {@code collection_id} column (vectors-068-18) the move must be
 * registered, or its label stays "no row" for ever and gate 1 can never hold. Where the table is gone no row is registered and the label
 * is not applicable.
 *
 * <p>The changesets are replayed the way an install that kept the table runs them: the table is restored as the migrating role (it owns
 * the kept table, which is what vectors-068-18's precondition requires), the old-shape rows are seeded before the column exists, and the
 * registering changeset and its owner-read policy are found by what they contain, so a changeset id is not retyped here.
 */
class CollectionIdQueueStampMoveIntegrationTest extends DataMoveIntegrationBase {

    private static final String QUEUE = "aspect_extraction_queue";
    private static final String LABEL_TEXT = StampMoveSpecs.LABEL_PREFIX + QUEUE;
    private static final String CHECK = "aspect_extraction_queue_collection_id_check";
    private static final String T1 = "p31-queue-a";
    private static final String T2 = "p31-queue-b";
    private static final Field<Long> COLLECTION_ID = DSL.field(DSL.name("collection_id"), Long.class);
    private static final Table<?> QUEUE_TABLE = DSL.table(DSL.name("nexus", QUEUE));

    @AfterEach
    void dropTheQueueAndTheSeeds() {
        ctx().dropTableIfExists(QUEUE_TABLE).execute();
        for (String t : List.of(T1, T2)) {
            StampMoveSeeds.clean(ctx(), t);
        }
        for (String run : new ArrayList<>(activeRuns(LABEL_TEXT))) {
            ctx().select(DSL.function(DSL.name("dml_utils", "archive_migration_run"), SQLDataType.BIGINT, DSL.val(run))).fetch();
        }
    }

    // ── helpers ─────────────────────────────────────────────────────────────────────────────────────────────

    private interface OnAdmin {
        void run(DSLContext admin, Connection conn) throws Exception;
    }

    /** Runs {@code work} as the migrating role, then gives the pool's one connection back. */
    private void asMigratingRole(OnAdmin work) throws Exception {
        try (Connection c = adminPool.getConnection()) {
            work.run(DSL.using(c, SQLDialect.POSTGRES), c);
        }
    }

    /** The id of the one changeset of the stamp-moves file whose SQL contains {@code marker}. */
    private static String changesetContaining(String marker, String why) throws Exception {
        var dbf = DocumentBuilderFactory.newInstance();
        dbf.setNamespaceAware(true);
        dbf.setFeature(XMLConstants.FEATURE_SECURE_PROCESSING, true);
        dbf.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true);
        List<String> found = new ArrayList<>();
        try (InputStream in = Files.newInputStream(StampMoveSpecs.CHANGELOG_FILE)) {
            Element root = dbf.newDocumentBuilder().parse(in).getDocumentElement();
            for (Node cs = root.getFirstChild(); cs != null; cs = cs.getNextSibling()) {
                if (!(cs instanceof Element e) || !"changeSet".equals(e.getLocalName())) continue;
                StringBuilder sql = new StringBuilder();
                for (Node n = e.getFirstChild(); n != null; n = n.getNextSibling()) {
                    if (n instanceof Element s && "sql".equals(s.getLocalName())) sql.append(s.getTextContent());
                }
                if (sql.toString().contains(marker)) found.add(e.getAttribute("id"));
            }
        }
        assertThat(found).as("%s: exactly one changeset of %s contains %s", why, StampMoveSpecs.CHANGELOG, marker).hasSize(1);
        return found.get(0);
    }

    private static String registeringChangeset() throws Exception {
        return changesetContaining("'" + LABEL_TEXT + "'", "R3: P3.1 registers the queue's stamp while the table and its column exist");
    }

    private static String policyChangeset() throws Exception {
        return changesetContaining("POLICY " + QUEUE + "_owner_read ON nexus." + QUEUE, "the move over a row-level-security table needs the migrating role to read every tenant's rows");
    }

    private int registeredRows() {
        return ctx().fetchCount(table("nexus", "data_moves"), LABEL.eq(LABEL_TEXT));
    }

    /** As an install that kept the table has it: the old-shape rows first (no id column yet), then vectors-068-18 adds the column and the NOT VALID CHECK. */
    private void keptQueueWithOldShapeRows() throws Exception {
        for (String t : List.of(T1, T2)) {
            String collection = StampMoveSeeds.collection(t, 0);
            PgContainerHelper.insertCollection(ctx(), t, collection, StampMoveSeeds.BGE);
            ctx().insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.TITLE,
                    CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID)
                .values(t, "q-doc", "Doc", collection, StampMoveSeeds.id(ctx(), t, collection)).execute();
        }
        asMigratingRole((admin, c) -> RetiredAspectQueueTable.restore(admin));
        for (String t : List.of(T1, T2)) {
            RetiredAspectQueueTable.seed(ctx(), t, StampMoveSeeds.collection(t, 0), "q-doc", "/p/" + t + ".md", "pending");
        }
        asMigratingRole((admin, c) -> PgContainerHelper.replayChangeset(c, P31CheckReplay.CHANGELOG, "vectors-068-18"));
        assertThat(PgCatalogProbes.constraintExists(ctx(), CHECK)).as("non-vacuity: the NOT VALID CHECK is on the kept table").isTrue();
        assertThat(PgCatalogProbes.constraintValidated(ctx(), CHECK)).isFalse();
        assertThat(ctx().fetchCount(QUEUE_TABLE, COLLECTION_ID.isNull())).as("non-vacuity: two rows with a name and no id").isEqualTo(2);
    }

    private CompletionCheck.Result gate1() {
        return CompletionCheck.evaluate(ctx(), new CompletionCheck.ReadSet(List.of(LABEL_TEXT), List.of()),
            CompletionCheck.Gate3Source.NOT_INSTALLED, CompletionCheck.DIRECT);
    }

    // ── R3 (b): the table kept with its column ──────────────────────────────────────────────────────────────

    @Test
    void aKeptQueueWithItsColumn_theMoveRegisters_stamps_verifies_andCompletes_soGate1CanHold() throws Exception {
        keptQueueWithOldShapeRows();
        String register = registeringChangeset();
        String policy = policyChangeset();

        assertThat(registeredRows()).as("non-vacuity: the walk registered nothing for the queue (the table was absent)").isZero();
        assertThat(gate1().gate1()).as("the check reads the label, the table and its column are there, and no row is registered").isFalse();
        asMigratingRole((admin, c) -> PgContainerHelper.replayChangeset(c, StampMoveSpecs.CHANGELOG, register));
        assertThat(registeredRows()).as("the changeset registers the move while the table and its column exist").isEqualTo(1);
        asMigratingRole((admin, c) -> PgContainerHelper.replayChangeset(c, StampMoveSpecs.CHANGELOG, policy));

        var row = ctx().selectFrom(table("nexus", "data_moves")).where(LABEL.eq(LABEL_TEXT)).fetchOne().intoMap();
        assertThat(row).containsEntry("driving_schema", "nexus").containsEntry("driving_table", QUEUE)
            .containsEntry("chunk_size", 1000).containsEntry("sleep_ms", 4000).containsEntry("statement_timeout_ms", 60_000)
            .containsEntry("completed_at", null).containsEntry("failed_at", null);
        String verification = row.get("verification").toString();
        long[] seen = new long[1];
        asMigratingRole((admin, c) -> seen[0] = MoveSql.countRows(c, verification, null, VerifyingFinisher.COUNT_CAP));
        assertThat(seen[0]).as("the migrating role reads both tenants' old rows table-wide: the owner-read policy is there").isEqualTo(2);
        assertThat(gate1().gate1()).as("registered and open: not complete").isFalse();

        assertThat(worker(installed(), 0, new DataMoveFailureHandler(adminConnection, WORKER_NAME, Clock.systemUTC()), new VerifyingFinisher())
            .runCycle()).isEqualTo(1);

        assertThat(completedAt(LABEL_TEXT)).as("the move completes").isNotNull();
        for (String t : List.of(T1, T2)) {
            Long expected = StampMoveSeeds.id(ctx(), t, StampMoveSeeds.collection(t, 0));
            assertThat(ctx().select(COLLECTION_ID).from(QUEUE_TABLE).where(DSL.field(DSL.name("tenant_id"), String.class).eq(t)).fetch(COLLECTION_ID))
                .as("%s: the row takes the id of the registry row of its tenant and name", t).containsExactly(expected);
        }
        asMigratingRole((admin, c) -> seen[0] = MoveSql.countRows(c, verification, null, VerifyingFinisher.COUNT_CAP));
        assertThat(seen[0]).as("the verification finds nothing left, table-wide").isZero();
        assertThat(PgCatalogProbes.constraintValidated(ctx(), CHECK)).as("on_complete validated the queue's CHECK").isTrue();
        CompletionCheck.Result done = gate1();
        assertThat(done.labels().get(0).status()).isEqualTo(CompletionCheck.Status.COMPLETE);
        assertThat(done.gate1()).as("gate 1 can hold on an install that kept the queue").isTrue();
    }

    // ── R3 (a): the table gone ──────────────────────────────────────────────────────────────────────────────

    @Test
    void anAbsentQueue_theChangesetsRegisterNothingAndCreateNoPolicy_theLabelIsNotApplicable_andGate1Holds() throws Exception {
        assertThat(PgCatalogProbes.tableExists(ctx(), "nexus", QUEUE)).as("non-vacuity: the table is absent").isFalse();
        String register = registeringChangeset();
        String policy = policyChangeset();

        asMigratingRole((admin, c) -> PgContainerHelper.replayChangeset(c, StampMoveSpecs.CHANGELOG, register));
        asMigratingRole((admin, c) -> PgContainerHelper.replayChangeset(c, StampMoveSpecs.CHANGELOG, policy));

        assertThat(registeredRows()).as("no row is registered for a table that is not there").isZero();
        assertThat(PgCatalogProbes.tableExists(ctx(), "nexus", QUEUE)).as("and the changesets do not bring the table back").isFalse();
        CompletionCheck.Result r = gate1();
        assertThat(r.labels().get(0).status()).isEqualTo(CompletionCheck.Status.NOT_APPLICABLE);
        assertThat(r.gate1()).as("gate 1 still holds").isTrue();
    }
}
