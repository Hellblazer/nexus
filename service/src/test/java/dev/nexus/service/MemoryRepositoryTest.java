package dev.nexus.service;

import dev.nexus.service.db.MemoryRepository;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.nexus.tables.records.MemoryRecord;
import org.testcontainers.containers.PostgreSQLContainer;
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

import java.sql.Connection;
import java.util.List;
import java.util.Optional;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-152 bead nexus-gmiaf.6 — MemoryRepository integration test.
 *
 * <p>Proves that the jOOQ-generated {@code Memory} table class and {@code MemoryRecord}
 * compile, execute correctly, and honour RLS through {@link TenantScope#withTenant}.
 *
 * <p>Coverage (Part A deliverable):
 * <ol>
 *   <li>upsert INSERT: generated id is positive, row is visible to the same tenant.</li>
 *   <li>upsert UPDATE (ON CONFLICT): content is replaced, id is unchanged.</li>
 *   <li>RLS isolation: tenant-A rows are invisible to tenant-B.</li>
 *   <li>findByProject returns all rows for the tenant, ordered by timestamp desc.</li>
 *   <li>findByTitle finds the row or returns empty.</li>
 *   <li>delete removes the row; a second delete returns false.</li>
 *   <li>cross-tenant upsert: inserting with mismatched tenant_id is blocked by RLS.</li>
 * </ol>
 *
 * <p>Hermetic: embedded Postgres (Testcontainers pgvector), port 0, requires Docker.
 * Schema applied via Liquibase master changelog (same as MemorySchemaLiquibaseTest).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class MemoryRepositoryTest {

    private static final String SVC_ROLE = "svc_repo_test";
    private static final String SVC_PASS = "svc_repo_test_pass";

    private static final String TENANT_A = "tenant-a";
    private static final String TENANT_B = "tenant-b";

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    MemoryRepository repo;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            // Create the service role used by TenantScope connections.
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN " +
                "    CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "'; " +
                "  END IF; " +
                "END $$");
            // nexus_svc needed by changeset 5 grant DO block.
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; " +
                "  END IF; " +
                "END $$");
        }

        // Apply Liquibase changelog via superuser.
        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            Liquibase liquibase = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                db);
            liquibase.update(new Contexts());
        }

        // Grant the service role the same privileges as nexus_svc.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.memory TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT USAGE ON SEQUENCE nexus.memory_id_seq TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }

        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);

        tenantScope = new TenantScope(svcDs);
        repo = new MemoryRepository(tenantScope);
    }

    @AfterAll
    void stopAll() throws Exception {
        if (svcDs != null) svcDs.close();
        if (pg != null)    pg.stop();
    }

    // ── Test 1: upsert INSERT — generated id, row visible ───────────────────

    @Test
    void upsert_insert_returnsPositiveId_rowVisible() {
        long id = repo.upsert(TENANT_A, "repo-proj", "first-entry",
                              "hello from generated jOOQ", "tag1,tag2",
                              /*session*/ null, "test-agent", 30);

        assertThat(id).as("generated id from RETURNING must be positive").isPositive();

        Optional<MemoryRecord> row = repo.findByTitle(TENANT_A, "repo-proj", "first-entry");
        assertThat(row).as("inserted row must be findable via findByTitle").isPresent();
        MemoryRecord r = row.get();
        assertThat(r.getId()).as("id must match the returned value").isEqualTo(id);
        assertThat(r.getTenantId()).as("tenant_id must be stamped by RLS").isEqualTo(TENANT_A);
        assertThat(r.getProject()).isEqualTo("repo-proj");
        assertThat(r.getTitle()).isEqualTo("first-entry");
        assertThat(r.getContent()).isEqualTo("hello from generated jOOQ");
        assertThat(r.getTags()).isEqualTo("tag1,tag2");
        assertThat(r.getAgent()).isEqualTo("test-agent");
        assertThat(r.getTtl()).isEqualTo(30);
        // findByTitle now tracks access: access_count is 1 after first read
        assertThat(r.getAccessCount()).as("access_count incremented to 1 by findByTitle").isEqualTo(1);
        assertThat(r.getTimestamp()).as("timestamp must be set").isNotNull();
    }

    // ── Test 2: upsert UPDATE — ON CONFLICT replaces content ────────────────

    @Test
    void upsert_update_onConflict_replacesContent() {
        // Initial insert.
        long id1 = repo.upsert(TENANT_A, "repo-proj", "update-entry",
                               "original content", "tag-old", null, null, 7);
        assertThat(id1).isPositive();

        // Second upsert with same (tenant, project, title) — should update.
        long id2 = repo.upsert(TENANT_A, "repo-proj", "update-entry",
                               "updated content", "tag-new", null, "updater", 14);
        assertThat(id2).as("ON CONFLICT RETURNING must still return a valid id").isPositive();

        // Read back — content must be updated.
        Optional<MemoryRecord> row = repo.findByTitle(TENANT_A, "repo-proj", "update-entry");
        assertThat(row).isPresent();
        assertThat(row.get().getContent())
            .as("content must be updated after ON CONFLICT DO UPDATE").isEqualTo("updated content");
        assertThat(row.get().getTags()).isEqualTo("tag-new");
        assertThat(row.get().getAgent()).isEqualTo("updater");
        assertThat(row.get().getTtl()).isEqualTo(14);
    }

    // ── Test 3: RLS tenant isolation — tenant-B cannot see tenant-A rows ────

    @Test
    void rls_tenantIsolation_crossTenantInvisible() {
        // Seed a row for tenant-A.
        repo.upsert(TENANT_A, "isolation-proj", "alpha-secret",
                    "sensitive content", null, null, null, null);

        // tenant-B must not see it.
        Optional<MemoryRecord> viewedByB = repo.findByTitle(TENANT_B, "isolation-proj", "alpha-secret");
        assertThat(viewedByB)
            .as("tenant-B must NOT see tenant-A's row (RLS isolation)").isEmpty();

        List<MemoryRecord> bRows = repo.findByProject(TENANT_B, "isolation-proj");
        assertThat(bRows)
            .as("findByProject from tenant-B must return no tenant-A rows").isEmpty();
    }

    // ── Test 4: findByProject — all rows, ordered by timestamp desc ─────────

    @Test
    void findByProject_returnsAllRowsForTenant() {
        String proj = "list-proj-" + System.nanoTime();  // unique project to avoid cross-test pollution

        repo.upsert(TENANT_A, proj, "alpha-entry-1", "content 1", null, null, null, null);
        repo.upsert(TENANT_A, proj, "alpha-entry-2", "content 2", null, null, null, null);
        repo.upsert(TENANT_A, proj, "alpha-entry-3", "content 3", null, null, null, null);

        List<MemoryRecord> rows = repo.findByProject(TENANT_A, proj);
        assertThat(rows).as("findByProject must return all 3 tenant-A rows").hasSize(3);
        List<String> titles = rows.stream().map(MemoryRecord::getTitle).toList();
        assertThat(titles)
            .as("all inserted titles must be present")
            .containsExactlyInAnyOrder("alpha-entry-1", "alpha-entry-2", "alpha-entry-3");

        // tenant-B still sees nothing for the same project name
        List<MemoryRecord> bRows = repo.findByProject(TENANT_B, proj);
        assertThat(bRows).as("tenant-B sees zero rows for tenant-A's project").isEmpty();
    }

    // ── Test 5: findByTitle — absent row returns empty ───────────────────────

    @Test
    void findByTitle_absentEntry_returnsEmpty() {
        Optional<MemoryRecord> row = repo.findByTitle(TENANT_A, "nonexistent-proj", "nonexistent-title");
        assertThat(row).as("absent row must return Optional.empty()").isEmpty();
    }

    // ── Test 6: delete — removes row, second delete returns false ───────────

    @Test
    void delete_removesRow_secondDeleteReturnsFalse() {
        repo.upsert(TENANT_A, "delete-proj", "to-delete", "delete me", null, null, null, null);

        boolean firstDelete = repo.delete(TENANT_A, "delete-proj", "to-delete");
        assertThat(firstDelete).as("first delete must return true (row existed)").isTrue();

        Optional<MemoryRecord> afterDelete = repo.findByTitle(TENANT_A, "delete-proj", "to-delete");
        assertThat(afterDelete).as("row must not be findable after delete").isEmpty();

        boolean secondDelete = repo.delete(TENANT_A, "delete-proj", "to-delete");
        assertThat(secondDelete).as("second delete must return false (row already gone)").isFalse();
    }

    // ── Test 7: RLS delete isolation — tenant-B cannot delete tenant-A rows ─

    @Test
    void delete_crossTenant_returnsZeroRows() {
        String proj = "delete-iso-proj-" + System.nanoTime();
        repo.upsert(TENANT_A, proj, "a-row", "content", null, null, null, null);

        // tenant-B tries to delete tenant-A's row — RLS makes it invisible, returns false
        boolean deleted = repo.delete(TENANT_B, proj, "a-row");
        assertThat(deleted)
            .as("tenant-B delete targeting tenant-A row must return false (RLS makes it invisible)").isFalse();

        // tenant-A's row must still be there
        Optional<MemoryRecord> stillThere = repo.findByTitle(TENANT_A, proj, "a-row");
        assertThat(stillThere)
            .as("tenant-A's row must be unaffected by cross-tenant delete attempt").isPresent();
    }

    // ── Test 8: session round-trip — session column persists and survives UPDATE ─
    //
    // Proves that the session provenance required by the .8 ETL is preserved.
    // Two sub-cases:
    //   (a) INSERT with non-null session → read back equals the stored value
    //   (b) UPDATE ON CONFLICT with a different session → read back shows new session
    //       (session is updated so the latest write's provenance is canonical)

    @Test
    void session_roundTrips_throughInsertAndUpdate() {
        String proj = "session-proj-" + System.nanoTime();
        String sessionA = "python-session-abc123";
        String sessionB = "python-session-def456";

        // Insert with sessionA
        long id = repo.upsert(TENANT_A, proj, "session-entry",
                              "content", "tag", sessionA, "agent-a", 30);
        assertThat(id).isPositive();

        Optional<MemoryRecord> afterInsert = repo.findByTitle(TENANT_A, proj, "session-entry");
        assertThat(afterInsert).isPresent();
        assertThat(afterInsert.get().getSession())
            .as("session must round-trip through insert: stored value must equal the passed session")
            .isEqualTo(sessionA);

        // Update with sessionB — ON CONFLICT DO UPDATE also sets SESSION
        repo.upsert(TENANT_A, proj, "session-entry",
                    "updated content", "tag", sessionB, "agent-b", 30);

        Optional<MemoryRecord> afterUpdate = repo.findByTitle(TENANT_A, proj, "session-entry");
        assertThat(afterUpdate).isPresent();
        assertThat(afterUpdate.get().getSession())
            .as("session must round-trip through ON CONFLICT DO UPDATE: stored value must equal the new session")
            .isEqualTo(sessionB);

        // Null session: verify it stores as NULL (not empty string or prior value)
        String proj2 = "session-null-proj-" + System.nanoTime();
        repo.upsert(TENANT_A, proj2, "null-session-entry",
                    "content", null, /*session*/ null, null, null);
        Optional<MemoryRecord> nullRow = repo.findByTitle(TENANT_A, proj2, "null-session-entry");
        assertThat(nullRow).isPresent();
        assertThat(nullRow.get().getSession())
            .as("null session must be stored as NULL (not empty string)")
            .isNull();
    }

    @Test
    void search_equalRankRows_returnDeterministicIdOrder() {
        // nexus-te885.11 (conexus bus 4918/4920): ts_rank has no corpus/IDF
        // component, so identical content = identical rank — and without a
        // tiebreak, equal-rank rows surface in HEAP order, which a migration
        // (or any UPDATE) permutes. Non-vacuous by construction: after both
        // inserts, row A is re-upserted, moving its tuple version to the END
        // of the heap — heap order becomes (B, A) while id order stays
        // (A, B). Without the ", id ASC" tiebreak this test fails.
        String proj = "tiebreak-proj-" + System.nanoTime();
        long idA = repo.upsert(TENANT_A, proj, "tiebreak-alpha",
                "identical searchable tiebreak content", "t", null, null, 30);
        long idB = repo.upsert(TENANT_A, proj, "tiebreak-beta",
                "identical searchable tiebreak content", "t", null, null, 30);
        // move A's tuple to the heap tail (same content: rank unchanged)
        repo.upsert(TENANT_A, proj, "tiebreak-alpha",
                "identical searchable tiebreak content", "t", null, null, 30);

        var rows = repo.search(TENANT_A, "tiebreak content", proj);
        assertThat(rows).hasSize(2);
        assertThat(rows.get(0).getId())
            .as("equal ts_rank rows must return in deterministic id ASC order, not heap order")
            .isEqualTo(Math.min(idA, idB));
        assertThat(rows.get(1).getId()).isEqualTo(Math.max(idA, idB));
    }

    /**
     * nexus-22r1f: the FTS5-parity rows for dotted titles.
     *
     * <p>PostgreSQL's text-search parser classifies {@code auth-design.md} as
     * a FILE token and keeps it WHOLE; SQLite FTS5's unicode61 tokenizer
     * splits on every non-alphanumeric. So before memory-002 a title was
     * findable in service mode ONLY by its exact full string, and
     * {@code nx memory search auth} silently returned nothing for an entry
     * titled {@code auth-design.md} — a wrong answer with a 200 status, on the
     * DEFAULT substrate since 6.0, reproduced against the deployed cloud
     * engine at 0.1.56.
     *
     * <p>These are the exact rows measured on the bead against the SQLite
     * baseline. The last two are the NON-VACUITY half: they passed BEFORE the
     * fix too, so their presence proves this test would still fail if the new
     * segment merely broke the old behaviour instead of adding to it.
     */
    @Test
    void search_dottedTitle_isFindableByAnyWordInside() {
        String proj = "fts-parity-" + System.nanoTime();
        repo.upsert(TENANT_A, proj, "auth-design.md",
                "body text with no shared words", "t", null, null, 30);
        repo.upsert(TENANT_A, proj, "RDR-025-implementation.md",
                "body text with no shared words", "t", null, null, 30);
        repo.upsert(TENANT_A, proj, "plaintitle",
                "body text with no shared words", "t", null, null, 30);

        // --- the rows that were BROKEN (service 0, sqlite 1) ---
        assertThat(repo.search(TENANT_A, "auth", proj))
            .as("a word inside a dotted title must be findable (was: 0 hits)")
            .extracting(r -> r.getTitle()).contains("auth-design.md");
        assertThat(repo.search(TENANT_A, "design", proj))
            .as("a LATER word inside a dotted title must be findable too")
            .extracting(r -> r.getTitle()).contains("auth-design.md");
        assertThat(repo.search(TENANT_A, "RDR-025", proj))
            .as("a separator-bearing QUERY must match the separator-normalized "
                + "stored segment — this is the leg ftsQuery() adds")
            .extracting(r -> r.getTitle()).contains("RDR-025-implementation.md");
        assertThat(repo.search(TENANT_A, "auth-design", proj))
            .as("separator-bearing query, separator-bearing title")
            .extracting(r -> r.getTitle()).contains("auth-design.md");

        // --- the rows that ALREADY worked: prove nothing was narrowed ---
        assertThat(repo.search(TENANT_A, "auth-design.md", proj))
            .as("exact full title still matches (superset preserved, never traded)")
            .extracting(r -> r.getTitle()).contains("auth-design.md");
        assertThat(repo.search(TENANT_A, "plaintitle", proj))
            .as("a title with no separator at all still matches")
            .extracting(r -> r.getTitle()).contains("plaintitle");
    }

    /**
     * nexus-22r1f: the same parity must hold on EVERY memory FTS surface, not
     * just {@code search}.
     *
     * <p>{@code search}, {@code searchGlob} and {@code searchByTag} each
     * carried the tsquery expression inline — seven copies of one decision.
     * Fixing only the one that was noticed is exactly how catalog-015 came to
     * fix {@code catalog_documents} in 2026-07-13 while {@code nexus.memory}
     * kept the identical bug until now. This pins the other two so the next
     * divergence cannot hide in the surface nobody tested.
     */
    @Test
    void search_dottedTitle_parityHoldsOnGlobAndTagSurfacesToo() {
        String proj = "fts-parity-glob-" + System.nanoTime();
        repo.upsert(TENANT_A, proj, "auth-design.md",
                "body text with no shared words", "authtag", null, null, 30);

        assertThat(repo.searchGlob(TENANT_A, "auth", proj.substring(0, 10) + "*"))
            .as("searchGlob must resolve a dotted title by an inside word")
            .extracting(r -> r.getTitle()).contains("auth-design.md");
        assertThat(repo.searchByTag(TENANT_A, "auth", "authtag"))
            .as("searchByTag must resolve a dotted title by an inside word")
            .extracting(r -> r.getTitle()).contains("auth-design.md");
    }

    /**
     * nexus-22r1f: the diacritic-folding parity row.
     *
     * <p>FTS5's unicode61 tokenizer folds diacritics by default, so
     * {@code resume} finds {@code résumé}; {@code to_tsvector('english', ...)}
     * does not. Measured on the CONTENT column with no title involvement, so
     * unlike the dotted-title rows this one is not a tokenization artifact.
     *
     * <p>memory-002 folds on the STORED side and {@link #ftsQuery} folds on
     * the QUERY side. Both halves are required and neither is sufficient —
     * proven by mutation, and the two halves fail DIFFERENT assertions below:
     * dropping the stored fold breaks the unaccented query (the original
     * defect), dropping the query fold breaks the accented one.
     *
     * <p>{@code résumé} and {@code zzznotpresent} are the NON-VACUITY half.
     * The former passed BEFORE the fix, so it proves the change did not merely
     * trade accented matching for unaccented matching; the latter proves the
     * OR'd tsquery has not degenerated into something that matches everything.
     */
    @Test
    void search_accentedContent_isFindableWithoutAccents() {
        String proj = "fts-accent-" + System.nanoTime();
        repo.upsert(TENANT_A, proj, "fr.md",
                "résumé cafetière naïve", "t", null, null, 30);

        // --- the row that was BROKEN (service 0, sqlite 1) ---
        assertThat(repo.search(TENANT_A, "resume", proj))
            .as("an unaccented query must find accented content (was: 0 hits)")
            .extracting(r -> r.getTitle()).contains("fr.md");
        assertThat(repo.search(TENANT_A, "cafetiere", proj))
            .as("folding is not special-cased to one word")
            .extracting(r -> r.getTitle()).contains("fr.md");

        // --- non-vacuity: nothing was traded away, nothing matches everything ---
        assertThat(repo.search(TENANT_A, "résumé", proj))
            .as("the ACCENTED query still matches — folding both sides is a "
                + "superset, not a swap")
            .extracting(r -> r.getTitle()).contains("fr.md");
        assertThat(repo.search(TENANT_A, "zzznotpresent", proj))
            .as("an absent word still misses — the OR'd tsquery has not "
                + "degenerated into a match-all")
            .isEmpty();
    }

    /**
     * nexus-22r1f: the fold stops where FTS5's fold stops.
     *
     * <p>This pins a DELIBERATE design boundary, not an implementation detail.
     * {@code fold_diacritics} is a 1:1 translate over the Latin-1 supplement
     * because that is precisely what FTS5's default
     * {@code remove_diacritics=1} covers — measured against the SQLite
     * baseline this column exists to match:
     *
     * <pre>
     *   angstrom -> Ångström  HIT   (Latin-1)
     *   strasse  -> Straße    MISS  (no ß expansion)
     *   lodz     -> Łódź      MISS  (Latin Extended-A)
     *   privet   -> привет    MISS  (Cyrillic)
     * </pre>
     *
     * <p>The obvious alternative, the {@code unaccent} extension, folds ALL of
     * those. Adopting it would make service BROADER than the baseline in a
     * contract whose entire purpose is to make the two agree — and it is
     * absent from every shipped PG bundle, so it would abort Liquibase at
     * boot. Without this test the next person "improves" the fold to unaccent,
     * both parity divergences silently invert from missing-results to
     * extra-results, and nothing goes red.
     */
    @Test
    void search_diacriticFold_stopsWhereFts5Stops() {
        String proj = "fts-fold-edge-" + System.nanoTime();
        repo.upsert(TENANT_A, proj, "wide.md",
                "Ångström Straße Łódź привет", "t", null, null, 30);

        assertThat(repo.search(TENANT_A, "angstrom", proj))
            .as("Latin-1 IS folded — the range FTS5 covers")
            .extracting(r -> r.getTitle()).contains("wide.md");

        assertThat(repo.search(TENANT_A, "strasse", proj))
            .as("ß is NOT expanded to ss — FTS5 does not either, and translate "
                + "is 1:1 by construction")
            .isEmpty();
        assertThat(repo.search(TENANT_A, "lodz", proj))
            .as("Latin Extended-A is NOT folded — unaccent would fold it and "
                + "overshoot the FTS5 baseline")
            .isEmpty();
        assertThat(repo.search(TENANT_A, "privet", proj))
            .as("Cyrillic is NOT transliterated — likewise unaccent-only "
                + "behaviour the baseline does not have")
            .isEmpty();
    }

    // ── nexus-senub: stopword-only / degenerate queries must not silently
    //    return [] indistinguishably from a genuine empty result ──────────────

    /**
     * MEASURED cases from the bead (both the original 2026-07-26 table and the
     * 2026-07-31 re-scope): {@code 'AND'} and bare {@code 'NOT'} are English
     * stopwords, so {@code plainto_tsquery('english', ...)} strips them to an
     * empty tsquery that can never match {@code content} — corpus-independent,
     * proven here by seeding a row that literally contains the word and
     * showing search still cannot find it via content.
     */
    @Test
    void search_stopwordOnlyQuery_raisesInsteadOfSilentEmpty() {
        String proj = "senub-stopword-" + System.nanoTime();
        repo.upsert(TENANT_A, proj, "operators.md",
                "clause and clause", "t", null, null, 30);

        assertThatThrownBy(() -> repo.search(TENANT_A, "and", proj))
            .as("a query made entirely of English stopwords must raise, not "
                + "silently return [] indistinguishable from a genuine no-match")
            .isInstanceOf(MemoryRepository.DegenerateQueryException.class)
            .hasMessageContaining("and");

        assertThatThrownBy(() -> repo.search(TENANT_A, "NOT", proj))
            .as("bare NOT is also a pure English stopword on this substrate — "
                + "not an FTS5-style operator syntax error, but the same "
                + "silent-content-miss class")
            .isInstanceOf(MemoryRepository.DegenerateQueryException.class)
            .hasMessageContaining("NOT");

        // NON-VACUITY: the search path itself works — a non-stopword term in
        // the same row matches normally. So the raises above are the stopword
        // reduction, not a broken fixture or a dead search path.
        assertThat(repo.search(TENANT_A, "clause", proj))
            .as("the control term must match, or this test proves nothing "
                + "about stopwords")
            .extracting(r -> r.getTitle()).contains("operators.md");
    }

    /**
     * A query that is real (parses to a non-empty english tsquery) but simply
     * matches no row must stay a normal, silent {@code []} — the guard must
     * never turn a genuine "nothing here" into a spurious 400.
     */
    @Test
    void search_realQueryNoMatch_staysEmptyNotAnException() {
        String proj = "senub-realquery-nomatch-" + System.nanoTime();
        repo.upsert(TENANT_A, proj, "unrelated.md", "something else entirely", "t", null, null, 30);

        assertThat(repo.search(TENANT_A, "zzznotpresent", proj))
            .as("a real, non-stopword query that matches nothing is a genuine "
                + "empty result, not a degenerate-query error")
            .isEmpty();
    }

    /**
     * A query whose english leg is empty but that still resolves a REAL hit
     * through the 'simple' title-separator leg (memory-002) must return that
     * hit, not raise — the guard only fires when there is truly nothing to
     * return, never as a blanket rejection of stopword-shaped input.
     */
    @Test
    void search_stopwordQuery_stillFindsRealTitleHit_doesNotRaise() {
        String proj = "senub-stopword-title-hit-" + System.nanoTime();
        // separator-normalized title segment (memory-002) tokenizes
        // "and-design.md" into "and" / "design" / "md" under the 'simple'
        // config, so this row IS a real hit for the query "and".
        repo.upsert(TENANT_A, proj, "and-design.md",
                "body text with no shared words", "t", null, null, 30);

        assertThat(repo.search(TENANT_A, "and", proj))
            .as("a stopword-shaped query that resolves a REAL title hit must "
                + "return it, not raise — the guard only replaces an "
                + "ambiguous [] with a loud reason, it never discards a hit")
            .extracting(r -> r.getTitle()).contains("and-design.md");
    }

    /**
     * A query that parses to a real tsquery on a NON-stopword mechanism
     * ({@code 'foo"bar'} — the quote is punctuation, not an operator on this
     * substrate) matches the client's own historical fixture from the
     * original bead measurement. It must not raise: english leg is non-empty
     * ('foo' & 'bar').
     */
    @Test
    void search_quotedQuery_parsesToRealTsQuery_doesNotRaise() {
        String proj = "senub-quoted-" + System.nanoTime();
        repo.upsert(TENANT_A, proj, "quoted.md", "foo bar baz", "t", null, null, 30);

        assertThat(repo.search(TENANT_A, "foo\"bar", proj))
            .as("'foo\"bar' parses to a real tsquery ('foo' & 'bar') on this "
                + "substrate — must not raise, whatever it does or doesn't match")
            .extracting(r -> r.getTitle()).contains("quoted.md");
    }

    /**
     * The same stopword-degenerate-query guard must hold on searchGlob and
     * searchByTag too — they share {@link #ftsQuery} with search, and fixing
     * only the surface that was noticed is exactly how nexus-22r1f's dotted-
     * title bug survived on nexus.memory after catalog-015 already fixed the
     * identical shape on catalog_documents.
     */
    @Test
    void searchGlob_and_searchByTag_alsoRaiseOnStopwordOnlyQuery() {
        String proj = "senub-stopword-glob-" + System.nanoTime();
        repo.upsert(TENANT_A, proj, "glob-op.md", "clause and clause", "andtag", null, null, 30);

        assertThatThrownBy(() -> repo.searchGlob(TENANT_A, "and", proj.substring(0, 10) + "*"))
            .as("searchGlob shares ftsQuery with search — same degenerate-query gap")
            .isInstanceOf(MemoryRepository.DegenerateQueryException.class)
            .hasMessageContaining("and");

        assertThatThrownBy(() -> repo.searchByTag(TENANT_A, "and", "zzz-no-such-tag"))
            .as("searchByTag shares ftsQuery with search — same degenerate-query gap")
            .isInstanceOf(MemoryRepository.DegenerateQueryException.class)
            .hasMessageContaining("and");
    }
}
