package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantScope;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;
import java.util.Map;

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
            var lb = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(
                    new JdbcConnection(su)));
            lb.update(new Contexts());
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

    // ── nexus-p5qk8: manifest writes refresh indexed_at ──────────────────

    private String indexedAtOf(String tumbler) {
        Map<String, Object> doc = repo.getDocument(TENANT, tumbler);
        return doc == null ? null : String.valueOf(doc.get("indexed_at"));
    }

    @Test
    void manifest_append_refreshes_indexed_at() {
        String doc = repo.registerDocument(TENANT, "9.3", Map.of(
            "title", "ghost.md", "content_type", "rdr",
            "file_path", "docs/ghost.md",
            "indexed_at", "2026-07-09T17:42:10+00:00"));
        assertThat(indexedAtOf(doc)).isEqualTo("2026-07-09T17:42:10+00:00");

        repo.appendManifestChunks(TENANT, doc, List.of(Map.of(
            "position", 0, "chash", "c".repeat(64), "chunk_index", 0,
            "line_start", 1, "line_end", 10, "char_start", 0, "char_end", 100)));
        String after = indexedAtOf(doc);
        assertThat(after)
            .as("--force backfill (appendManifestChunks) must stamp repair time")
            .isNotEqualTo("2026-07-09T17:42:10+00:00");
        assertThat(after).isNotBlank();
    }

    @Test
    void manifest_replace_refreshes_indexed_at_but_empty_replace_does_not() {
        String doc = repo.registerDocument(TENANT, "9.3", Map.of(
            "title", "ghost2.md", "content_type", "rdr",
            "file_path", "docs/ghost2.md",
            "indexed_at", "2026-07-09T17:42:10+00:00"));

        // Empty REPLACE (a clear) is not an indexing event — no stamp.
        repo.writeManifest(TENANT, doc, List.of());
        assertThat(indexedAtOf(doc)).isEqualTo("2026-07-09T17:42:10+00:00");

        repo.writeManifest(TENANT, doc, List.of(Map.of(
            "position", 0, "chash", "d".repeat(64), "chunk_index", 0,
            "line_start", 1, "line_end", 5, "char_start", 0, "char_end", 50)));
        assertThat(indexedAtOf(doc)).isNotEqualTo("2026-07-09T17:42:10+00:00");
    }
}
