// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.DimTables;
import liquibase.Contexts;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.Liquibase;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.postgresql.util.PSQLException;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.ResultSet;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-5xn3k.1 (RUNFENCE E1) — TDD suite for {@code nexus.manifest_verify(text)}
 * and {@code nexus.manifest_verify_all()} (catalog-020-index-run-fence.xml).
 *
 * <p>Design of record: T2 nexus memory {@code 5xn3k-design-2026-08-02} §3.2.
 * Structurally these are {@code nexus.manifest_orphans(dim)}
 * (catalog-004-manifest-functions.xml, re-declared for bytea in
 * rdr180-002-hex-boundary-functions.xml) narrowed to one document (or every
 * document in the tenant, grouped by collection) and aggregated into
 * referenced/present/missing counts instead of an orphan-row list.
 *
 * <p><strong>The two traps carried forward from manifest_orphans, or the
 * check reads FALSE:</strong>
 * <ul>
 *   <li>Tombstoned parent documents are EXCLUDED (deleted_at IS NULL) — a
 *       soft-deleted document's manifest is not damage.</li>
 *   <li>Manifest rows with {@code collection IS NULL} are excluded from
 *       "referenced" entirely — get this wrong and every such row reads as
 *       damaged. RDR-191 (nexus-71gw2, {@code catalog-025-collection-not-null.xml})
 *       makes that population UNREPRESENTABLE (NOT NULL, no sentinel, no
 *       DEFAULT) rather than merely pre-backfill state — the filter itself is
 *       untouched by that bead (its own OUT-of-scope list) and is now
 *       permanently vacuous rather than load-bearing.</li>
 * </ul>
 *
 * <p>Unlike manifest_orphans/manifest_backfill (admin/superuser, cross-tenant,
 * no GUC scoping), manifest_verify/manifest_verify_all are tenant-scoped via
 * the {@code nexus.tenant} GUC — the {@code nexus.document_text(text)}
 * pattern — because these are per-request engine-service reads (single-doc
 * verify, and the doctor verify-sweep), not a cross-tenant migration tool.
 *
 * <p>Mirrors the PgContainerHelper / TenantScope / Chash conventions of
 * {@link RemapMembershipFunctionTest} (the most recent SQL-function test in
 * this suite) rather than the older raw-string-chash pattern in
 * {@link ManifestFunctionsTest} — chash columns are {@code bytea} since
 * RDR-180, so fixture inserts go through {@code decode(hex, 'hex')} and
 * {@link Chash#ofText} for deterministic hex generation.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ManifestVerifyTest {

    private static final String TENANT       = "mvf-tenant-a";
    private static final String OTHER_TENANT = "mvf-tenant-b";

    // Collection name follows the conformant <type>__<owner>__<model>__<version>
    // shape; model segment maps to embedding_1024 (voyage-context-3 is a
    // 1024-dim token) on the unified nexus.chunks table (RDR-191 Phase 4) —
    // though manifest_verify itself checks presence via a single dim-agnostic
    // EXISTS(nexus.chunks) rather than routing on this token (see the
    // changeset comment), so the exact model name here is not load-bearing
    // to the function.
    private static final String COLLECTION = "knowledge__mvf-owner__voyage-context-3__v1";

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    com.zaxxer.hikari.HikariDataSource svcDs;

    // ══════════════════════════════════════════════════════════════════════════
    // LIFECYCLE
    // ══════════════════════════════════════════════════════════════════════════

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        // Full master changelog — role-001-nexus-svc.xml (first include) creates
        // nexus_svc; catalog-020-index-run-fence.xml (this bead) adds the fence
        // columns/index + manifest_verify/manifest_verify_all; grants-nexus-svc.xml
        // (last include) grants nexus_svc DML on every table it owns. If this
        // changeset fails to apply cleanly, @BeforeAll fails and every test below
        // reports it — the migration-conformance check for this bead.
        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db)
                .update(new Contexts());
        }

        svcDs = buildSvcDataSource();
        tenantScope = new TenantScope(svcDs);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private com.zaxxer.hikari.HikariDataSource buildSvcDataSource() {
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        return new com.zaxxer.hikari.HikariDataSource(cfg);
    }

    // ══════════════════════════════════════════════════════════════════════════
    // migration conformance — the 5 catalog-020 changesets actually landed
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void catalog020Changesets_recordedInDatabaseChangeLog() throws Exception {
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT count(*) FROM databasechangelog WHERE id LIKE 'catalog-020-%'");
            rs.next();
            assertThat(rs.getLong(1))
                .as("all 5 catalog-020 changesets (columns, index, manifest_verify, " +
                    "manifest_verify_all, grants) must be recorded as applied")
                .isEqualTo(5L);
        }
    }

    @Test
    void indexRunFenceColumns_existOnCatalogDocuments() throws Exception {
        try (Connection su = pg.createConnection("")) {
            for (String col : new String[]{
                    "index_state", "index_content_hash", "index_run_id", "index_started_at"}) {
                ResultSet rs = su.createStatement().executeQuery(
                    "SELECT count(*) FROM information_schema.columns " +
                    "WHERE table_schema = 'nexus' AND table_name = 'catalog_documents' " +
                    "AND column_name = '" + col + "'");
                rs.next();
                assertThat(rs.getLong(1))
                    .as("catalog_documents." + col + " must exist (catalog-020-1)")
                    .isEqualTo(1L);
            }
        }
    }

    @Test
    void indexRunFenceColumn_indexStateHasNoDefault_nullMeansUnknown() throws Exception {
        // The design's load-bearing property: index_state has NO backfill, so a
        // freshly-inserted document (no explicit index_state) reads NULL, not
        // 'complete' and not 'indexing'.
        String docId = "mvf-fence-default-doc";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertDoc(su, TENANT, docId);
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT index_state FROM nexus.catalog_documents " +
                "WHERE tenant_id = '" + TENANT + "' AND tumbler = '" + docId + "'");
            assertThat(rs.next()).isTrue();
            assertThat(rs.getString("index_state"))
                .as("index_state must default to NULL (unknown) — no backfill asserts a fact " +
                    "nobody measured")
                .isNull();
        }
    }

    @Test
    void partialIndex_predicateMatchesIndexStateNotComplete() throws Exception {
        // Gate-vs-proxy (critique finding): asserting the index EXISTS is a proxy —
        // it would stay green even if the WHERE predicate were dropped or silently
        // changed. This reads pg_catalog's actual stored index definition and checks
        // the predicate text itself.
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT indexdef FROM pg_indexes " +
                "WHERE schemaname = 'nexus' AND indexname = 'idx_catalog_documents_index_state'");
            assertThat(rs.next())
                .as("idx_catalog_documents_index_state must exist (catalog-020-2)")
                .isTrue();
            String indexdef = rs.getString("indexdef");
            // Postgres normalizes the stored definition — e.g. it renders the
            // predicate as "WHERE (index_state <> 'complete'::text)" (explicit
            // ::text cast, parens around the operand). Strip both so the assertion
            // targets the predicate's actual meaning rather than one specific
            // rendering.
            String normalized = indexdef.toLowerCase()
                .replace("::text", "")
                .replaceAll("[()]", "")
                .replaceAll("\\s+", " ");
            assertThat(normalized)
                .as("indexdef must be produced by CREATE INDEX ... WHERE index_state <> 'complete' " +
                    "— raw definition: " + indexdef)
                .contains("where index_state <> 'complete'");
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 1 — manifest_verify: all chunks present -> missing = 0
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void manifestVerify_allPresent_missingIsZero() throws Exception {
        String docId  = "mvf-allpresent-doc-1";
        String chash0 = chash("mvf-allpresent-c0");
        String chash1 = chash("mvf-allpresent-c1");

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT, COLLECTION);
            insertDoc(su, TENANT, docId);
            insertManifestRow(su, TENANT, docId, 0, chash0, COLLECTION);
            insertManifestRow(su, TENANT, docId, 1, chash1, COLLECTION);
            insertChunk1024(su, TENANT, COLLECTION, chash0);
            insertChunk1024(su, TENANT, COLLECTION, chash1);
        }

        long[] r = verify(docId);
        assertThat(r[0]).as("referenced").isEqualTo(2L);
        assertThat(r[1]).as("present").isEqualTo(2L);
        assertThat(r[2]).as("missing must be 0 when every manifest row has a chunk").isEqualTo(0L);
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 2 — manifest_verify: some missing -> EXACT count, not a boolean
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void manifestVerify_someMissing_exactCount() throws Exception {
        String docId  = "mvf-somemissing-doc-1";
        String chashA = chash("mvf-somemissing-a");
        String chashB = chash("mvf-somemissing-b");
        String chashC = chash("mvf-somemissing-c");

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT, COLLECTION);
            insertDoc(su, TENANT, docId);
            insertManifestRow(su, TENANT, docId, 0, chashA, COLLECTION);
            insertManifestRow(su, TENANT, docId, 1, chashB, COLLECTION);
            insertManifestRow(su, TENANT, docId, 2, chashC, COLLECTION);
            // Only chashA and chashB have chunk rows; chashC is missing.
            insertChunk1024(su, TENANT, COLLECTION, chashA);
            insertChunk1024(su, TENANT, COLLECTION, chashB);
        }

        long[] r = verify(docId);
        assertThat(r[0]).as("referenced").isEqualTo(3L);
        assertThat(r[1]).as("present").isEqualTo(2L);
        assertThat(r[2])
            .as("missing must be the EXACT count (1), not a boolean 'has damage' flag")
            .isEqualTo(1L);
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 3 — manifest_verify: tombstoned parent EXCLUDED
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void manifestVerify_tombstonedParent_excluded() throws Exception {
        String docId  = "mvf-tombstoned-doc-1";
        String chashMissing = chash("mvf-tombstoned-missing");

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT, COLLECTION);
            insertDoc(su, TENANT, docId);
            // A manifest row with NO chunk — would read as 1 missing if the
            // document were live. It must not, once tombstoned.
            insertManifestRow(su, TENANT, docId, 0, chashMissing, COLLECTION);
            su.createStatement().execute(
                "UPDATE nexus.catalog_documents SET deleted_at = NOW() " +
                "WHERE tenant_id = '" + TENANT + "' AND tumbler = '" + docId + "'");
        }

        long[] r = verify(docId);
        assertThat(r[0])
            .as("a tombstoned document's manifest contributes nothing to referenced " +
                "(deleted_at IS NULL join on parent doc excludes it entirely)")
            .isEqualTo(0L);
        assertThat(r[1]).isEqualTo(0L);
        assertThat(r[2])
            .as("a tombstoned document must NEVER read as damaged, even though its " +
                "manifest row has no matching chunk — soft-delete is not damage")
            .isEqualTo(0L);
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 4 — manifest_verify: NULL-collection rows NOT counted as missing
    //
    // The trap the bead calls out explicitly: get this wrong and every
    // un-backfilled document in the corpus reads as damaged.
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void manifestVerify_nullCollectionRows_areUnrepresentable_andExcludedRowNeverExisted() throws Exception {
        // RDR-191 (nexus-71gw2) rebase: the pre-backfill (collection IS NULL)
        // row this test used to seed via insertManifestRowNullCollection is
        // now UNREPRESENTABLE -- catalog_document_chunks.collection is NOT
        // NULL (catalog-025-collection-not-null.xml), so the manifest's own
        // constraint rejects the row before manifest_verify's NULL guard
        // would ever see it. Assert the unrepresentability directly, then
        // confirm the properly-stamped row alone still verifies clean.
        String docId  = "mvf-nullcoll-doc-1";
        String chashBackfilled = chash("mvf-nullcoll-bf");
        String chashPending    = chash("mvf-nullcoll-pending");

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT, COLLECTION);
            insertDoc(su, TENANT, docId);
            // One properly-stamped, present row.
            insertManifestRow(su, TENANT, docId, 0, chashBackfilled, COLLECTION);
            insertChunk1024(su, TENANT, COLLECTION, chashBackfilled);

            PSQLException ex = org.junit.jupiter.api.Assertions.assertThrows(PSQLException.class, () ->
                insertManifestRowNullCollection(su, TENANT, docId, 1, chashPending));
            assertThat(ex.getSQLState())
                .as("the state is unrepresentable -- assert the not-null-violation SQLSTATE "
                    + "directly, not merely a row count of 0")
                .isEqualTo("23502");
        }

        long[] r = verify(docId);
        assertThat(r[0])
            .as("referenced counts only the real, collection-stamped row -- there is no "
                + "pre-backfill row left to (correctly) exclude")
            .isEqualTo(1L);
        assertThat(r[1]).isEqualTo(1L);
        assertThat(r[2])
            .as("missing must be 0")
            .isEqualTo(0L);
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 5 — manifest_verify: shared chash across two docs, counted once
    // per referencing document (not deduped away, not double-counted within
    // either document's own row set)
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void manifestVerify_sharedChashAcrossTwoDocs_countedOncePerReferencingDoc() throws Exception {
        String docA  = "mvf-shared-doc-a";
        String docB  = "mvf-shared-doc-b";
        String shared = chash("mvf-shared-chash");

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT, COLLECTION);
            insertDoc(su, TENANT, docA);
            insertDoc(su, TENANT, docB);
            insertManifestRow(su, TENANT, docA, 0, shared, COLLECTION);
            insertManifestRow(su, TENANT, docB, 0, shared, COLLECTION);
            insertChunk1024(su, TENANT, COLLECTION, shared);
        }

        long[] a = verify(docA);
        long[] b = verify(docB);

        assertThat(a[0]).as("docA referenced").isEqualTo(1L);
        assertThat(a[1]).as("docA present — the shared chunk is present for docA's own verify").isEqualTo(1L);
        assertThat(a[2]).as("docA missing").isEqualTo(0L);

        assertThat(b[0])
            .as("docB referenced — sharing the chash with docA does not zero out docB's own count")
            .isEqualTo(1L);
        assertThat(b[1])
            .as("docB present — the SAME physical chunk row satisfies BOTH documents' " +
                "manifest reference independently")
            .isEqualTo(1L);
        assertThat(b[2]).as("docB missing").isEqualTo(0L);
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 5b — cross-dim false positive: DOCUMENTED TRADEOFF, not a bug.
    //
    // manifest_verify's presence check (catalog-020-3) is a single, dim-agnostic
    // EXISTS(nexus.chunks) keyed on (tenant_id, collection, chash) (RDR-191 Phase
    // 4: was an OR across chunks_384/768/1024, now the unified table with no
    // dim/embedding-column filter at all) — it does NOT verify the match came
    // from the embedding_<dim> column the collection's model token
    // (split_part(collection,'__',3)) actually declares. A manifest row stamped
    // with a voyage-context-3 (1024-dim) collection name whose chash physically
    // exists with embedding_384 populated (same tenant_id + collection string)
    // reads PRESENT here — the shipped function cannot tell "wrong dim" apart
    // from "right dim".
    //
    // This is the SAME tradeoff nexus.remap_membership() makes deliberately
    // (RDR-186 nexus-146xx.5 — see RemapMembershipFunctionTest.dimAgnostic_
    // claimInChunks384_counted, whose own comment reads "membership must probe
    // ALL chunk dims ... without being told which"). nexus.manifest_orphans(dim)
    // (catalog-004-manifest-functions.xml) is the STRICTER tool when dim
    // fidelity matters: it routes via split_part(collection,'__',3) to the ONE
    // dim (embedding column) the collection name declares, so a wrong-dim chash
    // reads as an orphan there instead of a false "present".
    //
    // This test PINS the current, accepted, documented behavior — it is not an
    // aspiration. If manifest_verify is later tightened to add split_part
    // routing (closing this gap), this assertion should flip to missing=1 at
    // that time, not be treated as a regression to silently patch around.
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void manifestVerify_crossDimChash_countsAsPresent_documentedTradeoff() throws Exception {
        String docId = "mvf-crossdim-doc-1";
        String chashWrongDim = chash("mvf-crossdim-chash");

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT, COLLECTION); // voyage-context-3 (1024-dim) collection
            insertDoc(su, TENANT, docId);
            // Manifest row stamped with the 1024-dim collection name...
            insertManifestRow(su, TENANT, docId, 0, chashWrongDim, COLLECTION);
            // ...but the chash physically exists with embedding_384 populated, under
            // the SAME (tenant_id, collection) pair — same tenant + collection string,
            // wrong dim (RDR-191: same unified table, wrong embedding column).
            insertChunk384(su, TENANT, COLLECTION, chashWrongDim);
        }

        long[] r = verify(docId);
        assertThat(r[0]).as("referenced").isEqualTo(1L);
        assertThat(r[1])
            .as("DOCUMENTED TRADEOFF, not a bug: manifest_verify's dim-agnostic " +
                "EXISTS(nexus.chunks) does not check that the match came from the " +
                "embedding column the collection's model token declares. A chash " +
                "physically present with only embedding_384 populated reads PRESENT " +
                "even though the manifest row's collection is a voyage-context-3 " +
                "(1024-dim) name — manifest_orphans(dim) is the stricter " +
                "split_part-routed tool when dim fidelity matters.")
            .isEqualTo(1L);
        assertThat(r[2])
            .as("missing is 0 for this cross-dim match — behavior-pinning assertion, not " +
                "an aspiration; a future split_part-routing tightening should flip this to " +
                "missing=1, not be treated as breaking this test")
            .isEqualTo(0L);
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 6 — manifest_verify_all: per-collection aggregates match per-doc sums
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void manifestVerifyAll_aggregatesMatchPerDocSums() throws Exception {
        // Own collection, distinct from every other test's fixtures: manifest_verify_all()
        // aggregates over EVERY document in the tenant for a collection, so reusing the
        // shared COLLECTION constant here would fold in every other test's rows sharing
        // the same (tenant, collection) and break the exact-sum assertion below.
        String collection = "knowledge__mvf-owner__voyage-context-3__vagg";
        String docA = "mvf-agg-doc-a";
        String docB = "mvf-agg-doc-b";
        String aOk       = chash("mvf-agg-a-ok");
        String aMissing  = chash("mvf-agg-a-missing");
        String bOk1      = chash("mvf-agg-b-ok1");
        String bOk2      = chash("mvf-agg-b-ok2");

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT, collection);
            insertDoc(su, TENANT, docA);
            insertDoc(su, TENANT, docB);
            // docA: 2 referenced, 1 present, 1 missing.
            insertManifestRow(su, TENANT, docA, 0, aOk, collection);
            insertManifestRow(su, TENANT, docA, 1, aMissing, collection);
            insertChunk1024(su, TENANT, collection, aOk);
            // docB: 2 referenced, 2 present, 0 missing.
            insertManifestRow(su, TENANT, docB, 0, bOk1, collection);
            insertManifestRow(su, TENANT, docB, 1, bOk2, collection);
            insertChunk1024(su, TENANT, collection, bOk1);
            insertChunk1024(su, TENANT, collection, bOk2);
        }

        long[] a = verify(docA);
        long[] b = verify(docB);
        long expectedReferenced = a[0] + b[0];
        long expectedPresent    = a[1] + b[1];
        long expectedMissing    = a[2] + b[2];

        long[] agg = verifyAllForCollection(collection);
        assertThat(agg[0])
            .as("manifest_verify_all's referenced for " + collection + " must equal the " +
                "sum of manifest_verify(docA) + manifest_verify(docB)")
            .isEqualTo(expectedReferenced)
            .isEqualTo(4L);
        assertThat(agg[1]).isEqualTo(expectedPresent).isEqualTo(3L);
        assertThat(agg[2]).isEqualTo(expectedMissing).isEqualTo(1L);
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 7 — reader contract: missing GUC returns the empty/zero answer,
    // never a RAISE (diverges from purge_trash deliberately, same as
    // document_text)
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void manifestVerify_missingGuc_returnsZeroRow() throws Exception {
        String docId = "mvf-allpresent-doc-1"; // exists from GROUP 1, has chunks

        try (Connection su = pg.createConnection("")) {
            su.createStatement().execute("RESET nexus.tenant");
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT referenced, present, missing FROM nexus.manifest_verify('" + docId + "')");
            assertThat(rs.next())
                .as("manifest_verify always returns exactly one aggregate row, even with " +
                    "no tenant GUC set")
                .isTrue();
            assertThat(rs.getLong("referenced"))
                .as("no GUC = no tenant = no rows matched -> referenced = 0 " +
                    "(reader contract: never RAISE, unlike purge_trash)")
                .isEqualTo(0L);
            assertThat(rs.getLong("present")).isEqualTo(0L);
            assertThat(rs.getLong("missing")).isEqualTo(0L);
        }
    }

    @Test
    void manifestVerifyAll_missingGuc_returnsEmptySet() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.createStatement().execute("RESET nexus.tenant");
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT count(*) FROM nexus.manifest_verify_all()");
            rs.next();
            assertThat(rs.getLong(1))
                .as("manifest_verify_all is a GROUP BY query: no GUC means no rows matched " +
                    "the manifest CTE, so there are zero groups (not one zeroed row)")
                .isEqualTo(0L);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 8 — RLS isolation: tenant A's call must not see tenant B's manifest
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void manifestVerify_rlsIsolation_tenantACannotSeeTenantBDoc() throws Exception {
        String docB = "mvf-rls-doc-b-1";
        String chashB = chash("mvf-rls-doc-b-c0");

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, OTHER_TENANT, COLLECTION);
            insertDoc(su, OTHER_TENANT, docB);
            insertManifestRow(su, OTHER_TENANT, docB, 0, chashB, COLLECTION);
            // Deliberately NO chunk row — if RLS leaked tenant B's row into an
            // tenant-A-stamped call, it would show up as 1 referenced / 1 missing.
        }

        long[] r = verify(docB); // called with GUC = TENANT (mvf-tenant-a)
        assertThat(r[0])
            .as("RLS isolation: a tenant-A-stamped call must see 0 referenced rows for " +
                "tenant B's document, even though the tumbler exists")
            .isEqualTo(0L);
        assertThat(r[1]).isEqualTo(0L);
        assertThat(r[2]).isEqualTo(0L);
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 9 — SECURITY INVOKER / grants sanity
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void bothFunctions_areSecurityInvoker() throws Exception {
        try (Connection su = pg.createConnection("")) {
            for (String fn : new String[]{"manifest_verify", "manifest_verify_all"}) {
                ResultSet rs = su.createStatement().executeQuery(
                    "SELECT prosecdef FROM pg_proc p " +
                    "JOIN pg_namespace n ON n.oid = p.pronamespace " +
                    "WHERE n.nspname = 'nexus' AND p.proname = '" + fn + "' LIMIT 1");
                assertThat(rs.next()).as("nexus." + fn + " must exist").isTrue();
                assertThat(rs.getBoolean("prosecdef"))
                    .as("nexus." + fn + " must be SECURITY INVOKER (prosecdef=false)")
                    .isFalse();
            }
        }
    }

    @Test
    void nexusSvc_hasExecuteGrantOnBothFunctions() throws Exception {
        try (Connection su = pg.createConnection("")) {
            for (String sig : new String[]{"manifest_verify(text)", "manifest_verify_all()"}) {
                ResultSet rs = su.createStatement().executeQuery(
                    "SELECT has_function_privilege('nexus_svc', 'nexus." + sig + "', 'EXECUTE')");
                rs.next();
                assertThat(rs.getBoolean(1))
                    .as("nexus_svc must have EXECUTE on nexus." + sig + " (catalog-020-5 grants)")
                    .isTrue();
            }
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // HELPERS
    // ══════════════════════════════════════════════════════════════════════════

    /** Call manifest_verify(docId) under the TENANT GUC. Returns [referenced, present, missing]. */
    private long[] verify(String docId) {
        return tenantScope.withTenant(TENANT, ctx -> {
            var row = ctx.fetchOne(
                "SELECT referenced, present, missing FROM nexus.manifest_verify(?)", docId);
            assertThat(row).as("manifest_verify must return exactly one row").isNotNull();
            return new long[]{
                row.get("referenced", Long.class),
                row.get("present", Long.class),
                row.get("missing", Long.class)};
        });
    }

    /** Call manifest_verify_all() under the TENANT GUC and pick out one collection's row. */
    private long[] verifyAllForCollection(String collection) {
        return tenantScope.withTenant(TENANT, ctx -> {
            var row = ctx.fetchOne(
                "SELECT referenced, present, missing FROM nexus.manifest_verify_all() " +
                "WHERE collection = ?", collection);
            assertThat(row)
                .as("manifest_verify_all must return a row for " + collection)
                .isNotNull();
            return new long[]{
                row.get("referenced", Long.class),
                row.get("present", Long.class),
                row.get("missing", Long.class)};
        });
    }

    private static String chash(String seed) {
        return Chash.ofText(seed).toHex();
    }

    private static void insertCollection(Connection su, String tenantId, String name) throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_collections (tenant_id, name) " +
            "VALUES ('" + tenantId + "', '" + name + "') ON CONFLICT (tenant_id, name) DO NOTHING");
    }

    private static void insertDoc(Connection su, String tenantId, String tumbler) throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title) " +
            "VALUES ('" + tenantId + "', '" + tumbler + "', 'Test Doc " + tumbler + "') " +
            "ON CONFLICT (tenant_id, tumbler) DO NOTHING");
    }

    private static void insertManifestRow(Connection su, String tenantId, String docId,
                                           int position, String chashHex, String collection)
            throws Exception {
        // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk now requires a
        // matching nexus.chunks row for every catalog_document_chunks insert. This
        // file's whole purpose is manifest_verify's detection of a chash with NO
        // matching chunk -- a state the FK now prevents in normal operation.
        // manifest_verify/manifest_verify_all are named in RDR-191 Decision item 4
        // as retiring in a LATER phase (out of this bead's scope, nexus-o8dil.29
        // scopes the FK itself only), so this helper bypasses the FK LOCALLY to
        // keep every existing test's semantics unchanged: drop the constraint,
        // insert, then re-add it NOT VALID (catalog-029-0's exact shape) so it is
        // live again (unvalidated) for every subsequent statement in this
        // container, including any other test's real inserts.
        su.createStatement().execute(
            "ALTER TABLE nexus.catalog_document_chunks DROP CONSTRAINT IF EXISTS fk_catalog_chunks_chunk");
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_document_chunks " +
            "  (tenant_id, doc_id, position, chash, collection) " +
            "VALUES ('" + tenantId + "', '" + docId + "', " + position + ", " +
            "decode('" + chashHex + "', 'hex'), '" + collection + "') " +
            "ON CONFLICT (tenant_id, doc_id, position) DO NOTHING");
        su.createStatement().execute(
            "ALTER TABLE nexus.catalog_document_chunks " +
            "ADD CONSTRAINT fk_catalog_chunks_chunk " +
            "FOREIGN KEY (tenant_id, collection, chash) REFERENCES nexus.chunks (tenant_id, collection, chash) " +
            "ON UPDATE CASCADE DEFERRABLE INITIALLY IMMEDIATE NOT VALID");
    }

    private static void insertManifestRowNullCollection(Connection su, String tenantId,
                                                         String docId, int position, String chashHex)
            throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_document_chunks " +
            "  (tenant_id, doc_id, position, chash) " +
            "VALUES ('" + tenantId + "', '" + docId + "', " + position + ", " +
            "decode('" + chashHex + "', 'hex')) " +
            "ON CONFLICT (tenant_id, doc_id, position) DO NOTHING");
    }

    private static void insertChunk1024(Connection su, String tenantId, String collection,
                                         String chashHex) throws Exception {
        su.createStatement().execute(
            "INSERT INTO " + DimTables.CHUNKS_TABLE_NAME + " (tenant_id, collection, chash, chunk_text, " + DimTables.embeddingColumn(1024) + ") " +
            "VALUES ('" + tenantId + "', '" + collection + "', decode('" + chashHex + "', 'hex'), " +
            "'chunk text', ('[1" + ",0".repeat(1023) + "]')::vector) " +
            "ON CONFLICT (tenant_id, collection, chash) DO NOTHING");
    }

    /**
     * Insert a nexus.chunks row with embedding_384 populated under the given
     * (tenant_id, collection) pair (RDR-191 unified; formerly a chunks_384 row).
     * Used ONLY by the cross-dim false-positive pinning test (GROUP 5b) to seed a
     * chash under the WRONG dim (embedding column) for a 1024-dim-declared collection name.
     */
    private static void insertChunk384(Connection su, String tenantId, String collection,
                                        String chashHex) throws Exception {
        su.createStatement().execute(
            "INSERT INTO " + DimTables.CHUNKS_TABLE_NAME + " (tenant_id, collection, chash, chunk_text, " + DimTables.embeddingColumn(384) + ") " +
            "VALUES ('" + tenantId + "', '" + collection + "', decode('" + chashHex + "', 'hex'), " +
            "'chunk text', ('[1" + ",0".repeat(383) + "]')::vector) " +
            "ON CONFLICT (tenant_id, collection, chash) DO NOTHING");
    }
}
