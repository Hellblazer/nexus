package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.nexus.Routines;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-8gue1 (GH #1397 field report, 2026-07-13) — free-text catalog search
 * was blind to every document whose searchable text is a file basename/path,
 * which is exactly what repo indexing registers (title = basename).
 * Empirically: {@code plainto_tsquery('RDR-021')} = {@code 'rdr' & '-021'},
 * but the pre-catalog-015 fts_vector held the filename as ONE opaque lexeme
 * ({@code 'rdr-021.md'}, {@code 'docs/rdr/rdr-021.md'}) — no query a human
 * would type could ever match it. SQLite FTS5 (local mode) splits on
 * {@code -./_} so the blindness was service-only, falsifying catalog-001's
 * "PG >= FTS5 superset" claim.
 *
 * <p>Also covers nexus-p5qk8 from the same field report: manifest writes
 * (the {@code --force} backfill repair path) must refresh the parent
 * document's {@code indexed_at} instead of leaving it frozen at the original
 * ghost registration date.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogFtsFilenameSearchTest {

    private static final String TENANT = "fts-fname-tenant";
    // RDR-191: manifest writers require an explicit collection now — these
    // two indexed_at tests don't exercise collection semantics, so any
    // valid, non-blank value works.
    private static final String COLLECTION = "knowledge__fts-fname__voyage-context-3__v1";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource ds;
    CatalogRepository repo;

    String basenameDoc;   // title = file basename (the GH #1397 shape)
    String pathOnlyDoc;   // match only via file_path
    String headingDoc;    // prose-titled (pre-existing behavior must survive)

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN "
                + "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN "
                + "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; "
                + "  END IF; "
                + "END $$");
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        ds = PgContainerHelper.superuserDataSource(pg);
        repo = new CatalogRepository(new TenantScope(ds));

        // The exact GH #1397 shape: repo indexing registers title = basename.
        basenameDoc = repo.registerDocument(TENANT, "9.3", Map.of(
            "title", "rdr-021.md", "content_type", "rdr",
            "file_path", "docs/rdr/rdr-021.md"));
        // Title is prose-ish but the query fragment only appears in the path.
        pathOnlyDoc = repo.registerDocument(TENANT, "9.3", Map.of(
            "title", "AbstractOracle.java", "content_type", "code",
            "file_path", "delphinius/src/main/java/AbstractOracle.java"));
        // Prose heading title — matched by the ORIGINAL english leg; proves
        // catalog-015 changed nothing for previously-findable docs.
        headingDoc = repo.registerDocument(TENANT, "9.3", Map.of(
            "title", "RDR-021: Docling PDF Extraction", "content_type", "rdr",
            "file_path", "docs/rdr/rdr-021-heading.md"));
    }

    @AfterAll
    void stopAll() {
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    private List<String> tumblersFor(String query) {
        return repo.searchDocuments(TENANT, query, null, 50).stream()
                   .map(d -> String.valueOf(d.get("tumbler")))
                   .toList();
    }

    @Test
    void basename_titled_doc_is_findable_by_its_human_name() {
        // The literal GH #1397 repro: nx catalog search "RDR-021" -> no results.
        assertThat(tumblersFor("RDR-021"))
            .as("query 'RDR-021' must find title 'rdr-021.md' via the "
                + "separator-normalized segment")
            .contains(basenameDoc);
    }

    @Test
    void camelcase_filename_is_findable_without_extension() {
        assertThat(tumblersFor("AbstractOracle"))
            .as("query 'AbstractOracle' must find title 'AbstractOracle.java'")
            .contains(pathOnlyDoc);
    }

    @Test
    void path_segment_fragments_are_findable() {
        assertThat(tumblersFor("rdr rdr-021"))
            .as("path 'docs/rdr/rdr-021.md' must be findable by its segments")
            .contains(basenameDoc);
    }

    @Test
    void heading_titled_docs_still_match_via_the_original_english_leg() {
        assertThat(tumblersFor("docling extraction"))
            .as("stemmed prose matching must survive catalog-015 unchanged")
            .contains(headingDoc);
        // And the shared fragment now surfaces BOTH shapes.
        assertThat(tumblersFor("RDR-021"))
            .contains(headingDoc, basenameDoc);
    }

    @Test
    void blank_query_still_returns_nothing() {
        assertThat(repo.searchDocuments(TENANT, "  ", null, 10)).isEmpty();
    }

    @Test
    void all_separator_query_matches_nothing_not_everything() {
        // The translated leg turns '---' into spaces -> EMPTY tsquery.
        // PG semantics: an empty tsquery matches NOTHING via @@ — pin that
        // the new leg opens no match-all hole.
        assertThat(repo.searchDocuments(TENANT, "---", null, 10)).isEmpty();
        assertThat(repo.searchDocuments(TENANT, "/._-", null, 10)).isEmpty();
    }

    // ══════════════════════════════════════════════════════════════════════════
    // nexus-zrcj7 code-review finding (T2 [24213]): catalog_fts_match (catalog-035)
    // is a new function with no inlining/EXPLAIN parity test, unlike every other
    // combined-query function in this codebase (CombinedQueryParityTest's GROUP 3).
    // A GIN index exists on catalog_documents.fts_vector (catalog-017), so this is
    // a real perf surface: a non-inlinable (or non-indexable) rewrite would turn
    // every searchDocuments() call into a sequential scan without any functional
    // test here ever noticing, since GROUP 3-style checks assert PLAN SHAPE, not
    // query correctness.
    //
    // Mirrors CombinedQueryParityTest's own GROUP 3 technique (enable_seqscan=off
    // to eliminate the cheap alternative at unit scale, then assert the GIN index
    // name appears and no Function Scan/InitPlan node exists — a plpgsql body or a
    // non-inlined SQL function would show one of those instead of the expanded
    // `fts_vector @@ ...` predicate). Unlike GROUP 3's vector-ANN functions,
    // catalog_fts_match's caller (searchDocuments) is a single-table predicate with
    // no join, so there is no "index survives the join" claim to make here — only
    // the inlining + GIN-index-selected claim.
    //
    // Seeded via repo.registerDocumentMany/registerDocument — the repository, not
    // raw SQL — per the review instruction. UPDATED (nexus-cbo4a batch 3): the
    // EXPLAIN invocation now builds a typed jOOQ Query (ctx.select(...).from(
    // CATALOG_DOCUMENTS).where(DSL.condition(Routines.catalogFtsMatch(...)))) and
    // runs it through DSLContext#explain -- the "no jOOQ API hands back a real
    // PostgreSQL EXPLAIN plan" premise this comment used to state was superseded
    // the same day by PlainSearchTextGatedSearchExplainTest's ctx.explain(Query)
    // discovery; the same function definition still underlies both this
    // diagnostic call and CatalogRepository's own (Routines-mediated,
    // fts_vector.coerce(Object.class) idiom) call.
    // ══════════════════════════════════════════════════════════════════════════

    private static final String TENANT_EXPLAIN = "fts-fname-explain-tenant";

    /**
     * A rare, single-lexeme, separator-free term (so hasSeparator=false and only
     * the english+simple legs of catalog_fts_match apply — the simplest case to
     * reason about) that appears in exactly ONE seeded document out of {@link
     * #EXPLAIN_FILLER_DOCS} + 1, making the predicate genuinely selective.
     */
    private static final String RARE_TERM = "zqxvwunmarker99187";
    // Comparable to CombinedQueryParityTest's own GROUP-3 EXPLAIN_ROWS (500):
    // a real but modest fixture so the predicate's selectivity is genuine
    // (1 of 501) without needing a large seed just to force a cost crossover
    // (see explainRareTermPlan's javadoc -- the diagnostic query is built to
    // remove the one pre-existing competing index from consideration
    // entirely, rather than out-scaling it).
    private static final int EXPLAIN_FILLER_DOCS = 500;

    private String seedExplainFixture() {
        // nexus-zrcj7 review fixture: a DEDICATED owner prefix, disjoint from
        // "9.3" (used by this file's other fixtures under TENANT). This test's
        // `repo` runs over PgContainerHelper.superuserDataSource -- a real
        // Postgres SUPERUSER connection, which unconditionally bypasses RLS
        // (including FORCE ROW LEVEL SECURITY) regardless of TenantScope's GUC.
        // getDocument's tumbler-only WHERE relies on RLS for tenant scoping,
        // which is a safe assumption under production's nexus_svc role but not
        // under this file's superuser pool -- a colliding tumbler VALUE under a
        // different tenant (e.g. reusing "9.3") is visible cross-tenant here and
        // trips TooManyRowsException on ANY OTHER test in this file that looks
        // up a "9.3.N" tumbler. Disjoint prefix avoids the collision outright.
        List<Map<String, Object>> filler = new ArrayList<>(EXPLAIN_FILLER_DOCS);
        for (int i = 0; i < EXPLAIN_FILLER_DOCS; i++) {
            filler.add(Map.of(
                "title", "Quarterly Status Report " + i, "content_type", "rdr",
                "file_path", "docs/filler/report-" + i + ".md"));
        }
        repo.registerDocumentMany(TENANT_EXPLAIN, "zrcj7-explain", filler);
        return repo.registerDocument(TENANT_EXPLAIN, "zrcj7-explain", Map.of(
            "title", RARE_TERM + " Findings", "content_type", "rdr",
            "file_path", "docs/rare/" + RARE_TERM + ".md"));
    }

    /**
     * EXPLAIN (no ANALYZE) a searchDocuments-shaped query over {@link
     * #RARE_TERM}'s predicate alone, with enable_seqscan AND enable_indexscan
     * disabled — mirrors CombinedQueryParityTest's own {@code explain(...)}
     * helper (penalize every non-target access method), scoped to a single-
     * table (no-join) query.
     *
     * <p>Deliberately carries NO {@code tenant_id}/{@code deleted_at} filter,
     * unlike searchDocuments' real WHERE (which adds {@code deleted_at IS
     * NULL} — {@code tenant_id} scoping is RLS, not an explicit predicate,
     * and this diagnostic connection is a superuser that bypasses RLS
     * entirely). Both columns are covered by {@code
     * idx_catalog_documents_collection_live} (catalog-003), a partial index
     * on {@code (tenant_id, physical_collection) WHERE deleted_at IS NULL}
     * — measured directly: with either filter present, that PRE-EXISTING,
     * unrelated index competes for the optimizer's choice and can win on
     * cost even at 5000 fixture rows (tsvector {@code @@} selectivity is
     * planner-estimated via a fixed default fraction, not real per-value
     * statistics, so raising row count alone does not force a crossover).
     * Dropping both filters removes that partial index from consideration
     * altogether (its WHERE clause can no longer be proven satisfied), so
     * the fts predicate's OWN indexability — what this test exists to pin —
     * decides the plan without an unrelated index's presence as a confound.
     */
    private String explainRareTermPlan() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            // Real row-count statistics, mirroring CombinedQueryParityTest's own
            // GROUP-3 fixture comment verbatim: without this the planner
            // under-estimates the bulk-loaded table to rows=1 (default-selectivity
            // guesswork, no ANALYZE ever run).
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENTS);
            su.createStatement().execute("SET enable_seqscan = off");
            su.createStatement().execute("SET enable_indexscan = off");
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // Same idiom CatalogRepository#searchDocuments uses: fts_vector's
            // tsvector type has no jOOQ-recognized mapping, so codegen types the
            // generated catalogFtsMatch overload's parameter Field<Object> and
            // marks it @Deprecated ("Unknown data type") -- expected and harmless,
            // not a defect to work around.
            Condition ftsMatch = DSL.condition(
                Routines.catalogFtsMatch(
                    CATALOG_DOCUMENTS.field("fts_vector").coerce(Object.class),
                    DSL.val(RARE_TERM), DSL.val(false)));
            return ctx.explain(ctx.select(CATALOG_DOCUMENTS.TUMBLER)
                    .from(CATALOG_DOCUMENTS)
                    .where(ftsMatch)
                    .orderBy(CATALOG_DOCUMENTS.TUMBLER)
                    .limit(50))
                .plan();
        }
    }

    @Test
    void catalog_fts_match_inlines_and_uses_gin_index_when_selective() throws Exception {
        String rareDoc = seedExplainFixture();

        // CONTROL: the real repository code path (Routines.catalogFtsMatch, not
        // this test's diagnostic raw call) finds the seeded document — proves the
        // EXPLAIN'd query below is behaviorally representative of production, not
        // just structurally similar.
        assertThat(repo.searchDocuments(TENANT_EXPLAIN, RARE_TERM, null, 50).stream()
                       .map(d -> String.valueOf(d.get("tumbler"))))
            .as("the same rare term must be findable through CatalogRepository"
                + ".searchDocuments, the production caller of catalog_fts_match")
            .contains(rareDoc);

        String plan = explainRareTermPlan();
        assertThat(plan)
            .as("a selective catalog_fts_match predicate must use the GIN index "
                + "idx_catalog_documents_fts (catalog-017) — a non-inlined or "
                + "non-indexable rewrite falls back to a sequential scan invisible "
                + "to every functional test in this file. Plan was:%n%s", plan)
            .contains("idx_catalog_documents_fts");
        assertThat(plan)
            .as("a Function Scan node means catalog_fts_match is not inlinable "
                + "(e.g. a plpgsql body, or SECURITY DEFINER) — EXPLAIN cannot then "
                + "see the index scan; catalog-035 must stay an inlinable LANGUAGE "
                + "sql, non-SECURITY-DEFINER function. Plan was:%n%s", plan)
            .doesNotContain("Function Scan");
        assertThat(plan)
            .as("an InitPlan means catalog_fts_match is evaluated as a separate "
                + "one-shot subplan rather than inlined into the WHERE expression "
                + "the GIN index can see. Plan was:%n%s", plan)
            .doesNotContain("InitPlan");
        assertThat(plan)
            .as("with enable_seqscan=off and a genuinely selective predicate, a "
                + "Seq Scan on catalog_documents means the GIN index was not "
                + "actually usable for this rewrite of the expression. Plan "
                + "was:%n%s", plan)
            .doesNotContain("Seq Scan on catalog_documents");
    }

    // ── nexus-p5qk8: manifest writes refresh indexed_at ──────────────────

    private String indexedAtOf(String tumbler) {
        Map<String, Object> doc = repo.getDocument(TENANT, tumbler);
        return doc == null ? null : String.valueOf(doc.get("indexed_at"));
    }

    /**
     * RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk now requires a
     * matching nexus.chunks row for every catalog_document_chunks insert. Stub a
     * minimal chunk (single embedding_384 vector, arbitrary text) under COLLECTION.
     */
    private void stubChunk(String chashHex) {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            // RDR-191 Phase 5 (nexus-o8dil.49): nexus.chunks now carries
            // chunks_collection_fk (tenant_id, collection) -> catalog_collections
            // (tenant_id, name) — stub-register the collection first, mirroring
            // PgVectorRepository#upsertChunks' own ensure-registered step.
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES "
                + "('" + TENANT + "', '" + COLLECTION + "') ON CONFLICT (tenant_id, name) DO NOTHING");
            su.createStatement().execute(
                "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_384) VALUES ("
                + "'" + TENANT + "', '" + COLLECTION + "', decode('" + chashHex + "', 'hex'), 'stub', "
                + "('[" + "0.1,".repeat(383) + "0.1]')::vector) ON CONFLICT (tenant_id, collection, chash) DO NOTHING");
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }

    @Test
    void manifest_append_refreshes_indexed_at() {
        // nexus-cefa1.2: indexed_at is timestamptz now (catalog-031-1-documents-temporal) —
        // the wire read is CatalogRepository.utcIso's micros+offset rendering (INDEXED_AT_FMT,
        // the catalog convention, kept). The written value below carries an explicit offset
        // but no microseconds, so it gains the accepted ".000000" residual on read (see
        // utcIso's javadoc) rather than echoing verbatim.
        String doc = repo.registerDocument(TENANT, "9.3", Map.of(
            "title", "ghost.md", "content_type", "rdr",
            "file_path", "docs/ghost.md",
            "indexed_at", "2026-07-09T17:42:10+00:00"));
        assertThat(indexedAtOf(doc)).isEqualTo("2026-07-09T17:42:10.000000+00:00");

        // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk now requires a
        // matching nexus.chunks row for the manifest write below.
        stubChunk("c".repeat(64));
        repo.appendManifestChunks(TENANT, doc, COLLECTION, List.of(Map.of(
            "position", 0, "chash", "c".repeat(64), "chunk_index", 0,
            "line_start", 1, "line_end", 10, "char_start", 0, "char_end", 100)));
        String after = indexedAtOf(doc);
        assertThat(after)
            .as("--force backfill (appendManifestChunks) must stamp repair time")
            .isNotEqualTo("2026-07-09T17:42:10.000000+00:00");
        assertThat(after).isNotBlank();
    }

    @Test
    void manifest_replace_refreshes_indexed_at_but_empty_replace_does_not() {
        // nexus-cefa1.2: see manifest_append_refreshes_indexed_at's comment above.
        String doc = repo.registerDocument(TENANT, "9.3", Map.of(
            "title", "ghost2.md", "content_type", "rdr",
            "file_path", "docs/ghost2.md",
            "indexed_at", "2026-07-09T17:42:10+00:00"));

        // Empty REPLACE (a clear) is not an indexing event — no stamp.
        repo.writeManifest(TENANT, doc, COLLECTION, List.of());
        assertThat(indexedAtOf(doc)).isEqualTo("2026-07-09T17:42:10.000000+00:00");

        // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk now requires a
        // matching nexus.chunks row for the manifest write below.
        stubChunk("d".repeat(64));
        repo.writeManifest(TENANT, doc, COLLECTION, List.of(Map.of(
            "position", 0, "chash", "d".repeat(64), "chunk_index", 0,
            "line_start", 1, "line_end", 5, "char_start", 0, "char_end", 50)));
        assertThat(indexedAtOf(doc)).isNotEqualTo("2026-07-09T17:42:10.000000+00:00");
    }
}
