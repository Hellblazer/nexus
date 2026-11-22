// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.PgContainerHelper;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-254 plan item 254-P1-S4: {@code POST /v1/catalog/owners/move}'s transaction, {@link CatalogMoves#moveOwner}.
 * Real Postgres. A repo owner A keeps its tumbler when its repository moves to a new root R with repo hash H: any other
 * owner B holding H (the user indexed at R before moving) is folded into A, A is re-rooted, and A's documents get the
 * {@code source_uri} a fresh registration at R would give them.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogOwnerMoveTest {

    private static final String SVC_ROLE = "svc_owner_move_test";
    private static final String SVC_PASS = "svc_owner_move_pass";
    private static final String COLL_A = "code__ownermove-a__voyage-code-3__v1";
    private static final String COLL_B = "code__ownermove-b__voyage-code-3__v1";

    private static final String OLD_ROOT = "/old/proj";
    private static final String NEW_ROOT = "/new/proj";
    private static final String HASH_A = "aaaa0001";
    private static final String HASH_B = "bbbb0002";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    CatalogRepository repo;
    CatalogMoves moves;
    AspectRepository aspects;
    MoveTestDb db;
    int tenantSeq;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(4);
        config.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(config);
        var scope = new TenantScope(svcDs);
        repo = new CatalogRepository(scope);
        moves = new CatalogMoves(repo);
        aspects = new AspectRepository(scope);
        db = new MoveTestDb(pg);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── fixtures ────────────────────────────────────────────────────────────

    private String tenant() {
        return "owner-move-" + (++tenantSeq);
    }

    private void repoOwner(String t, String prefix, String name, String hash, String root) {
        Map<String, Object> o = new LinkedHashMap<>();
        o.put("tumbler_prefix", prefix);
        o.put("name", name);
        o.put("owner_type", "repo");
        o.put("repo_hash", hash);
        o.put("repo_root", root);
        repo.upsertOwner(t, o);
    }

    private void curatorOwner(String t, String prefix, String name) {
        repo.upsertOwner(t, Map.of("tumbler_prefix", prefix, "name", name, "owner_type", "curator"));
    }

    /** A repository file: relative file_path, source_uri derived from the owner's root. */
    private String file(String t, String prefix, String relPath, String coll) {
        Map<String, Object> f = new LinkedHashMap<>();
        f.put("title", relPath);
        f.put("file_path", relPath);
        f.put("content_type", "code");
        f.put("corpus", "code");
        f.put("physical_collection", coll);
        return repo.registerDocument(t, prefix, f);
    }

    private String registered(String t, String prefix, String title, String sourceUri, String filePath, String coll) {
        Map<String, Object> f = new LinkedHashMap<>();
        f.put("title", title);
        f.put("file_path", filePath);
        f.put("content_type", "code");
        f.put("corpus", "code");
        f.put("physical_collection", coll);
        f.put("source_uri", sourceUri);
        return repo.registerDocument(t, prefix, f);
    }

    private void link(String t, String from, String to, String type) {
        Map<String, Object> l = new LinkedHashMap<>();
        l.put("from_tumbler", from);
        l.put("to_tumbler", to);
        l.put("link_type", type);
        l.put("created_by", "test");
        repo.upsertLink(t, l);
    }

    private void registerCollection(String t, String collection) throws Exception { db.registerCollection(t, collection); }

    private void aspect(String t, String docId, String coll, String sourcePath, String sourceUri, String text) throws Exception {
        registerCollection(t, coll);
        Map<String, Object> b = new LinkedHashMap<>();
        b.put("collection", coll);
        b.put("source_path", sourcePath);
        b.put("source_uri", sourceUri);
        b.put("doc_id", docId);
        b.put("problem_formulation", text);
        b.put("confidence", 0.9);
        b.put("extracted_at", OffsetDateTime.now().toString());
        b.put("model_version", "v1");
        b.put("extractor_name", "test");
        aspects.upsertAspect(t, b);
    }

    private void highlight(String t, String docId, String coll, String sourceUri, String text) throws Exception {
        registerCollection(t, coll);
        Map<String, Object> b = new LinkedHashMap<>();
        b.put("doc_id", docId);
        b.put("collection", coll);
        b.put("source_uri", sourceUri);
        b.put("highlights_md", text);
        b.put("ingested_at", OffsetDateTime.now().toString());
        aspects.upsertHighlight(t, b);
    }

    private Map<String, Object> doc(String t, String tumbler) throws Exception { return db.doc(t, tumbler); }

    private Map<String, Object> owner(String t, String prefix) throws Exception { return db.owner(t, prefix); }

    private List<Map<String, Object>> aspectRows(String t) throws Exception { return db.aspects(t); }

    private List<Map<String, Object>> highlightRows(String t) throws Exception { return db.highlights(t); }

    private String snapshot(String t) throws Exception { return db.snapshot(t); }

    private Map<String, Object> move(String t, String owner, boolean dryRun) {
        return moves.moveOwner(t, owner, NEW_ROOT, HASH_B, dryRun).toMap();
    }

    private static Map<String, Object> countsOf(Map<String, Object> report) {
        Map<String, Object> m = new LinkedHashMap<>(report);
        m.remove("dry_run");
        return m;
    }

    private static List<Object> absorbed(Map<String, Object> report) {
        return new java.util.ArrayList<>((List<?>) report.get("absorbed_tumblers"));
    }

    /** A at the old root with files x and y; B (the repo as registered at the new root) with x, y and z. */
    private record Fold(String x, String y, String bx, String by, String bz) {}

    private Fold foldFixture(String t) {
        repoOwner(t, "1.1", "proj", HASH_A, OLD_ROOT);
        String x = file(t, "1.1", "docs/x.md", COLL_A);
        String y = file(t, "1.1", "docs/y.md", COLL_A);
        repoOwner(t, "1.2", "proj-renamed", HASH_B, NEW_ROOT);
        String bx = file(t, "1.2", "docs/x.md", COLL_B);
        String by = file(t, "1.2", "docs/y.md", COLL_B);
        String bz = file(t, "1.2", "docs/z.md", COLL_B);
        return new Fold(x, y, bx, by, bz);
    }

    // ── the fold ────────────────────────────────────────────────────────────

    @Test
    void foldsLiveOwnerHoldingNewHash() throws Exception {
        String t = tenant();
        Fold f = foldFixture(t);
        assertThat(doc(t, f.x()).get("source_uri")).isEqualTo("file://" + OLD_ROOT + "/docs/x.md");
        assertThat(doc(t, f.bx()).get("source_uri")).isEqualTo("file://" + NEW_ROOT + "/docs/x.md");
        String other = file(t, "1.1", "docs/other.md", COLL_A);
        link(t, f.bx(), other, "cites");
        aspect(t, f.x(), COLL_A, "docs/x.md", doc(t, f.x()).get("source_uri").toString(), "A's own extraction");
        aspect(t, f.bx(), COLL_B, "docs/x.md", doc(t, f.bx()).get("source_uri").toString(), "B's extraction");
        highlight(t, f.bx(), COLL_B, doc(t, f.bx()).get("source_uri").toString(), "B marks on x");
        highlight(t, f.y(), COLL_A, doc(t, f.y()).get("source_uri").toString(), "A marks on y");
        highlight(t, f.by(), COLL_B, doc(t, f.by()).get("source_uri").toString(), "B marks on y");

        var report = move(t, "1.1", false);

        // B's x and y are absorbed into A's: cleared and trashed. B's z has no counterpart: trashed.
        for (String b : List.of(f.bx(), f.by())) {
            assertThat(doc(t, b)).as("absorbed %s", b).containsEntry("source_uri", "");
            assertThat(doc(t, b).get("deleted_at")).isNotNull();
        }
        assertThat(doc(t, f.bz()).get("deleted_at")).as("no counterpart: trashed").isNotNull();
        // A's rows are at the new root, derived as a fresh registration would.
        assertThat(doc(t, f.x()).get("source_uri")).isEqualTo("file://" + NEW_ROOT + "/docs/x.md");
        assertThat(doc(t, f.y()).get("source_uri")).isEqualTo("file://" + NEW_ROOT + "/docs/y.md");
        // B's link followed its document onto A's.
        assertThat(db.links(t))
            .containsExactly(Map.of("from_tumbler", f.x(), "to_tumbler", other, "link_type", "cites"));
        // B's aspect rows are deleted; A's keeps its own, at the new URI, source_path unchanged.
        assertThat(aspectRows(t)).hasSize(1);
        assertThat(aspectRows(t).get(0)).containsEntry("doc_id", f.x()).containsEntry("source_path", "docs/x.md")
            .containsEntry("source_uri", "file://" + NEW_ROOT + "/docs/x.md")
            .containsEntry("problem_formulation", "A's own extraction");
        // B's highlights move to x (which had none) and are deleted for y (which had its own).
        var hl = highlightRows(t);
        assertThat(hl).extracting(h -> h.get("doc_id")).containsExactly(f.x(), f.y());
        assertThat(hl.get(0)).containsEntry("highlights_md", "B marks on x").containsEntry("collection", COLL_A)
            .containsEntry("source_uri", "file://" + NEW_ROOT + "/docs/x.md");
        assertThat(hl.get(1)).containsEntry("highlights_md", "A marks on y")
            .containsEntry("source_uri", "file://" + NEW_ROOT + "/docs/y.md");
        // B is deactivated with its hash cleared; A holds the new root and hash.
        assertThat(owner(t, "1.2").get("deactivated_at")).isNotNull();
        assertThat((String) owner(t, "1.2").get("repo_hash")).isNullOrEmpty();
        assertThat(owner(t, "1.1")).containsEntry("repo_root", NEW_ROOT).containsEntry("repo_hash", HASH_B)
            .containsEntry("name", "proj");
        assertThat(report).containsEntry("owners_folded", 1).containsEntry("absorbed", 2).containsEntry("trashed", 1)
            .containsEntry("links_remapped", 1)
            .containsEntry("aspect_rows_deleted", 1).containsEntry("aspect_rows_rewritten", 1)
            .containsEntry("highlight_rows_deleted", 1);
        assertThat(report.get("repointed")).as("x, y and other are rewritten").isEqualTo(3);
        assertThat(absorbed(report)).containsExactlyInAnyOrder(f.bx(), f.by());
    }

    @Test
    void foldsDeactivatedOwnerHoldingNewHash() throws Exception {
        String t = tenant();
        Fold f = foldFixture(t);
        repo.deactivateOwner(t, "1.2");
        Object deactivatedAt = owner(t, "1.2").get("deactivated_at");
        assertThat(deactivatedAt).isNotNull();

        var report = move(t, "1.1", false);

        assertThat(owner(t, "1.2").get("deactivated_at")).as("an already-deactivated B keeps its original time")
            .isEqualTo(deactivatedAt);

        assertThat(report).containsEntry("owners_folded", 1).containsEntry("absorbed", 2);
        assertThat(doc(t, f.x()).get("source_uri")).isEqualTo("file://" + NEW_ROOT + "/docs/x.md");
        assertThat(owner(t, "1.1")).containsEntry("repo_hash", HASH_B);
    }

    @Test
    void foldTrashesBDocumentsWithoutCounterpart() throws Exception {
        String t = tenant();
        Fold f = foldFixture(t);

        var report = move(t, "1.1", false);

        assertThat(doc(t, f.bz()).get("deleted_at")).isNotNull();
        assertThat(report).containsEntry("trashed", 1);
        assertThat(absorbed(report)).as("a trashed row is not an absorbed one").doesNotContain(f.bz());
    }

    @Test
    void foldDeactivatesBAndClearsHashBeforeAGetsIt() throws Exception {
        String t = tenant();
        foldFixture(t);

        move(t, "1.1", false);

        // idx_catalog_owners_repo_hash allows one owner per hash: the move would have failed had B kept it.
        assertThat(repo.ownerByRepoHash(t, HASH_B).get("tumbler_prefix")).isEqualTo("1.1");
        assertThat(db.ownersHolding(t, HASH_B)).hasSize(1);
    }

    // ── the re-root and the rewrite ─────────────────────────────────────────

    @Test
    void reRootsAWhenAtOldRoot() throws Exception {
        String t = tenant();
        repoOwner(t, "1.1", "proj", HASH_A, OLD_ROOT);
        String x = file(t, "1.1", "src/x.py", COLL_A);

        var report = move(t, "1.1", false);

        assertThat(owner(t, "1.1")).containsEntry("repo_root", NEW_ROOT).containsEntry("repo_hash", HASH_B)
            .containsEntry("name", "proj");
        assertThat(doc(t, x).get("source_uri")).isEqualTo("file://" + NEW_ROOT + "/src/x.py");
        assertThat(report).containsEntry("owners_folded", 0).containsEntry("repointed", 1).containsEntry("absorbed", 0);
    }

    @Test
    void gap3ShapeAlreadyAtRAndHRewritesDocumentUris() throws Exception {
        // Registration at the new root converged on A by name and re-rooted it (the characterization test's
        // same-basename case): A already has R and H, its documents still name the old root.
        String t = tenant();
        repoOwner(t, "1.1", "proj", HASH_A, OLD_ROOT);
        String x = file(t, "1.1", "src/x.py", COLL_A);
        String y = file(t, "1.1", "src/y.py", COLL_A);
        repo.upsertOwner(t, Map.of("name", "proj", "owner_type", "repo", "repo_hash", HASH_B, "repo_root", NEW_ROOT));
        assertThat(owner(t, "1.1")).containsEntry("repo_root", NEW_ROOT).containsEntry("repo_hash", HASH_B);
        assertThat(doc(t, x).get("source_uri")).isEqualTo("file://" + OLD_ROOT + "/src/x.py");

        var report = move(t, "1.1", false);

        assertThat(owner(t, "1.1")).containsEntry("repo_root", NEW_ROOT).containsEntry("repo_hash", HASH_B);
        assertThat(doc(t, x).get("source_uri")).isEqualTo("file://" + NEW_ROOT + "/src/x.py");
        assertThat(doc(t, y).get("source_uri")).isEqualTo("file://" + NEW_ROOT + "/src/y.py");
        assertThat(report).containsEntry("repointed", 2).containsEntry("owners_folded", 0);
    }

    @Test
    void rewritesTrashedRootDerivedRowsToo() throws Exception {
        String t = tenant();
        repoOwner(t, "1.1", "proj", HASH_A, OLD_ROOT);
        String live = file(t, "1.1", "src/live.py", COLL_A);
        String gone = file(t, "1.1", "src/gone.py", COLL_A);
        repo.deleteDocument(t, gone);

        var report = move(t, "1.1", false);

        assertThat(doc(t, live).get("source_uri")).isEqualTo("file://" + NEW_ROOT + "/src/live.py");
        assertThat(doc(t, gone).get("source_uri")).isEqualTo("file://" + NEW_ROOT + "/src/gone.py");
        assertThat(doc(t, gone).get("deleted_at")).isNotNull();
        assertThat(report).containsEntry("repointed", 2);
    }

    @Test
    void skipsOtherSchemeEmptyUriAndAbsoluteFilePathAndCountsThem() throws Exception {
        String t = tenant();
        repoOwner(t, "1.1", "proj", HASH_A, OLD_ROOT);
        String root = file(t, "1.1", "src/x.py", COLL_A);
        String web = registered(t, "1.1", "web", "https://example.org/spec", "spec.html", COLL_A);
        String noUri = registered(t, "1.1", "no-uri", "", "docs/nouri.md", COLL_A);
        db.blankSourceUri(t, noUri);
        String absolute = registered(t, "1.1", "abs", "file://" + OLD_ROOT + "/abs.md", OLD_ROOT + "/abs.md", COLL_A);

        var report = move(t, "1.1", false);

        assertThat(doc(t, root).get("source_uri")).isEqualTo("file://" + NEW_ROOT + "/src/x.py");
        assertThat(doc(t, web).get("source_uri")).isEqualTo("https://example.org/spec");
        assertThat(doc(t, noUri).get("source_uri")).isEqualTo("");
        assertThat(doc(t, absolute).get("source_uri")).isEqualTo("file://" + OLD_ROOT + "/abs.md");
        assertThat(report).containsEntry("repointed", 1).containsEntry("skipped", 3);
    }

    @Test
    void rewritesAspectAndHighlightSourceUriKeepsAspectSourcePath() throws Exception {
        String t = tenant();
        repoOwner(t, "1.1", "proj", HASH_A, OLD_ROOT);
        String x = file(t, "1.1", "docs/x.md", COLL_A);
        String old = "file://" + OLD_ROOT + "/docs/x.md";
        aspect(t, x, COLL_A, "docs/x.md", old, "extraction");
        highlight(t, x, COLL_A, old, "marks");

        var report = move(t, "1.1", false);

        assertThat(aspectRows(t).get(0)).containsEntry("source_path", "docs/x.md")
            .containsEntry("source_uri", "file://" + NEW_ROOT + "/docs/x.md");
        assertThat(highlightRows(t).get(0)).containsEntry("source_uri", "file://" + NEW_ROOT + "/docs/x.md");
        assertThat(report).containsEntry("aspect_rows_rewritten", 1).containsEntry("highlight_rows_rewritten", 1);
    }

    @Test
    void anAspectRowWithNoDocumentCannotExist_soTheDocIdJoinReachesEveryAspectRow() throws Exception {
        // RDR-254 residual (GC-r2 observation 4): a legacy aspect row with an empty doc_id would be missed by the owner
        // move's doc_id join. fk_doc_aspects_catalog_doc plus hygiene-001's NOT NULL make such a row unrepresentable.
        String t = tenant();
        repoOwner(t, "1.1", "proj", HASH_A, OLD_ROOT);
        assertThatThrownBy(() -> db.insertAspectRow(t, COLL_A, ""))
            .hasMessageContaining("fk_doc_aspects_catalog_doc");
    }

    // ── review round: locks, alias graph, rollback, input ────────────────────

    @Test
    void lockOrderHookReportsTheSortedDocumentOrder() throws Exception {
        // The C-collation choice itself is pinned by the code (orderBy ... collate("C")), not by this fixture: every
        // tumbler here shares the 1.1. prefix, so its order is the same in any collation. This pins the hook's report.
        // RDR-254 review B2: lockDocumentRowsSorted sorts tumblers as Java strings (C order); the owner move's lock
        // must take the same order whatever the database collation, or a concurrent link write can deadlock with it.
        String t = tenant();
        repoOwner(t, "1.1", "proj", HASH_A, OLD_ROOT);
        for (int i = 1; i <= 12; i++) file(t, "1.1", "src/f" + i + ".py", COLL_A);
        java.util.concurrent.atomic.AtomicReference<List<String>> seen = new java.util.concurrent.atomic.AtomicReference<>();
        CatalogMoves.lockOrderHookForTests = seen::set;
        try {
            move(t, "1.1", false);
        } finally {
            CatalogMoves.lockOrderHookForTests = null;
        }
        List<String> order = seen.get();
        assertThat(order).hasSize(12);
        assertThat(order).as("sorted as Java strings: 1.1.10 before 1.1.2").isSortedAccordingTo(String::compareTo);
        assertThat(order.indexOf("1.1.10")).isLessThan(order.indexOf("1.1.2"));
    }

    @Test
    void foldRefusesACrossPairAliasCycleWithNothingWritten() throws Exception {
        // RDR-254 review B3: A/x names B/y and A/y names B/x. Each pair alone is fine against the stored graph; both
        // absorbed together leave A/x and A/y naming each other.
        String t = tenant();
        Fold f = foldFixture(t);
        db.setAlias(t, f.x(), f.by());
        db.setAlias(t, f.y(), f.bx());
        String before = snapshot(t);

        assertThatThrownBy(() -> move(t, "1.1", false))
            .isInstanceOf(CatalogRepository.MergeRefused.class)
            .hasMessageContaining("cycle").hasMessageContaining(f.x()).hasMessageContaining(f.y());
        assertThat(snapshot(t)).as("refused before any write").isEqualTo(before);
        assertThatThrownBy(() -> move(t, "1.1", true)).isInstanceOf(CatalogRepository.MergeRefused.class);
    }

    @Test
    void foldRepointsAliasRowsOfAbsorbedRowsIncludingTrashedOnes() throws Exception {
        String t = tenant();
        Fold f = foldFixture(t);
        String other = file(t, "1.1", "docs/other.md", COLL_A);
        String gone = file(t, "1.1", "docs/gone.md", COLL_A);
        repo.deleteDocument(t, gone);
        db.setAlias(t, other, f.bx());     // a live row naming B's x
        db.setAlias(t, gone, f.by());      // a trashed row naming B's y
        db.setAlias(t, f.x(), f.bx());     // A's own x names its twin: cleared, not left pointing at itself

        move(t, "1.1", false);

        assertThat(doc(t, other).get("alias_of")).isEqualTo(f.x());
        assertThat(doc(t, gone).get("alias_of")).as("a trashed row is re-pointed too").isEqualTo(f.y());
        assertThat(doc(t, f.x()).get("alias_of")).isEqualTo("");
    }

    @Test
    void aFailureAfterTheFoldRollsTheWholeMoveBack() throws Exception {
        // RDR-254 review B4: everything the fold and the rewrite wrote must roll back with the failure.
        String t = tenant();
        Fold f = foldFixture(t);
        String other = file(t, "1.1", "docs/other.md", COLL_A);
        link(t, f.bx(), other, "cites");
        aspect(t, f.x(), COLL_A, "docs/x.md", doc(t, f.x()).get("source_uri").toString(), "A's extraction");
        aspect(t, f.bx(), COLL_B, "docs/x.md", doc(t, f.bx()).get("source_uri").toString(), "B's extraction");
        highlight(t, f.bx(), COLL_B, doc(t, f.bx()).get("source_uri").toString(), "B marks");
        String before = snapshot(t);
        CatalogMoves.afterWritesHookForTests = () -> { throw new IllegalStateException("injected after the last write"); };
        try {
            assertThatThrownBy(() -> move(t, "1.1", false)).hasMessageContaining("injected");
        } finally {
            CatalogMoves.afterWritesHookForTests = null;
        }
        assertThat(snapshot(t)).isEqualTo(before);
        assertThat(owner(t, "1.1")).containsEntry("repo_root", OLD_ROOT).containsEntry("repo_hash", HASH_A);
        assertThat(owner(t, "1.2").get("deactivated_at")).isNull();
    }

    @Test
    void aWindowsDriveLetterNewRootPassesValidation_andARelativeOrDriveRelativeOneDoesNot() throws Exception {
        // RDR-254 confirmation: validation is a shape check, independent of the engine host. What the rewrite then does
        // with a root this host cannot resolve (deriveSourceUri skips it) is not asserted here.
        String t = tenant();
        repoOwner(t, "1.1", "proj", HASH_A, OLD_ROOT);
        file(t, "1.1", "src/x.py", COLL_A);
        assertThat(moves.moveOwner(t, "1.1", "C:\\work\\proj", HASH_B, true).toMap()).containsEntry("dry_run", true);
        assertThat(moves.moveOwner(t, "1.1", "D:/work/proj", HASH_B, true).toMap()).containsEntry("dry_run", true);
        assertThatThrownBy(() -> moves.moveOwner(t, "1.1", "C:proj", HASH_B, true))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("absolute");
    }

    @Test
    void refusesARelativeNewRoot() {
        String t = tenant();
        assertThatThrownBy(() -> moves.moveOwner(t, "1.1", "new/proj", HASH_B, false))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("absolute");
    }

    @Test
    void refusesTwoLiveRowsOfAThatWouldTakeOneUri() throws Exception {
        String t = tenant();
        repoOwner(t, "1.1", "proj", HASH_A, OLD_ROOT);
        String x1 = file(t, "1.1", "src/x.py", COLL_A);
        String x2 = file(t, "1.1", "src/other.py", COLL_A);
        // Registration would fold a second live row of one file_path into the first; a legacy row can still be there.
        db.setUriAndPath(t, x2, "file://" + OLD_ROOT + "/a/src/x.py", "src/x.py");
        String before = snapshot(t);

        assertThatThrownBy(() -> move(t, "1.1", false))
            .isInstanceOf(CatalogRepository.MergeRefused.class)
            .hasMessageContaining(x1).hasMessageContaining(x2).hasMessageContaining("file://" + NEW_ROOT + "/src/x.py");
        assertThat(snapshot(t)).isEqualTo(before);
    }

    @Test
    void refusesWhenARowOfAThatIsNotMovingHoldsATargetUri() throws Exception {
        String t = tenant();
        repoOwner(t, "1.1", "proj", HASH_A, OLD_ROOT);
        String mover = file(t, "1.1", "src/y.py", COLL_A);
        String holder = file(t, "1.1", "src/q.py", COLL_A);
        // An absolute file_path is skipped, so this live row stays put and holds the URI the mover would take.
        db.setUriAndPath(t, holder, "file://" + NEW_ROOT + "/src/y.py", NEW_ROOT + "/src/y.py");
        String before = snapshot(t);

        assertThatThrownBy(() -> move(t, "1.1", false))
            .isInstanceOf(CatalogRepository.MergeRefused.class)
            .hasMessageContaining(holder).hasMessageContaining(mover);
        assertThat(snapshot(t)).isEqualTo(before);
    }

    // ── refusals ────────────────────────────────────────────────────────────

    @Test
    void refusesCuratorOwner() throws Exception {
        String t = tenant();
        curatorOwner(t, "1.1", "pdfs");
        String before = snapshot(t);
        assertThatThrownBy(() -> move(t, "1.1", false))
            .isInstanceOf(CatalogRepository.MergeRefused.class).hasMessageContaining("curator");
        assertThat(snapshot(t)).isEqualTo(before);
    }

    @Test
    void refusesDeactivatedA() throws Exception {
        String t = tenant();
        repoOwner(t, "1.1", "proj", HASH_A, OLD_ROOT);
        repo.deactivateOwner(t, "1.1");
        String before = snapshot(t);
        assertThatThrownBy(() -> move(t, "1.1", false))
            .isInstanceOf(CatalogRepository.MergeRefused.class).hasMessageContaining("deactivated");
        assertThat(snapshot(t)).isEqualTo(before);
    }

    @Test
    void refusesMissingOwner() {
        assertThatThrownBy(() -> move(tenant(), "1.9", false))
            .isInstanceOf(CatalogRepository.MergeRefused.class).hasMessageContaining("1.9");
    }

    @Test
    void refusesBlankArguments() {
        String t = tenant();
        assertThatThrownBy(() -> moves.moveOwner(t, "", NEW_ROOT, HASH_B, false)).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> moves.moveOwner(t, "1.1", " ", HASH_B, false)).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> moves.moveOwner(t, "1.1", NEW_ROOT, "", false)).isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void refusesWithNothingWrittenWhenAnotherOwnersLiveRowHoldsNewUri() throws Exception {
        String t = tenant();
        Fold f = foldFixture(t);
        curatorOwner(t, "1.3", "pdfs");
        // A curator document indexed by absolute path under the new root: a live row of a third owner.
        String w = file(t, "1.1", "docs/w.md", COLL_A);   // only A has it: B holds no URI at the new root for it
        String squatter = registered(t, "1.3", "squatter", "file://" + NEW_ROOT + "/docs/w.md", NEW_ROOT + "/docs/w.md", COLL_A);
        String before = snapshot(t);

        assertThatThrownBy(() -> move(t, "1.1", false))
            .isInstanceOf(CatalogRepository.MergeRefused.class)
            .hasMessageContaining(squatter).hasMessageContaining("file://" + NEW_ROOT + "/docs/w.md");
        assertThat(snapshot(t)).as("nothing was written").isEqualTo(before);
        assertThatThrownBy(() -> move(t, "1.1", true))
            .as("the dry run reports the same refusal")
            .isInstanceOf(CatalogRepository.MergeRefused.class).hasMessageContaining(squatter);
        assertThat(doc(t, f.x()).get("source_uri")).isEqualTo("file://" + OLD_ROOT + "/docs/x.md");
    }

    // ── dry run ─────────────────────────────────────────────────────────────

    @Test
    void dryRunWritesNothingAndReportsSameCountsAsWrite() throws Exception {
        String t = tenant();
        Fold f = foldFixture(t);
        String other = file(t, "1.1", "docs/other.md", COLL_A);
        link(t, f.bx(), other, "cites");
        aspect(t, f.x(), COLL_A, "docs/x.md", doc(t, f.x()).get("source_uri").toString(), "A's own extraction");
        aspect(t, f.bx(), COLL_B, "docs/x.md", doc(t, f.bx()).get("source_uri").toString(), "B's extraction");
        highlight(t, f.bx(), COLL_B, doc(t, f.bx()).get("source_uri").toString(), "B marks on x");
        repo.setSensitivity(t, List.of(f.by()), true, "test", "test");
        String before = snapshot(t);

        var dry = move(t, "1.1", true);

        assertThat(snapshot(t)).as("a dry run leaves every row as it was").isEqualTo(before);
        assertThat(dry).containsEntry("dry_run", true);

        var written = move(t, "1.1", false);

        assertThat(snapshot(t)).as("the write changes rows").isNotEqualTo(before);
        assertThat(written).containsEntry("dry_run", false);
        assertThat(countsOf(written)).isEqualTo(countsOf(dry));
        assertThat(written).containsEntry("owners_folded", 1).containsEntry("absorbed", 2).containsEntry("trashed", 1);
        assertThat(doc(t, f.y())).as("a sensitive B row raised its counterpart").containsEntry("sensitive", true);
    }
}
