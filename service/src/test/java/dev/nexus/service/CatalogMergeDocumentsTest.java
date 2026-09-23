// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantScope;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.atomic.AtomicReference;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_LINKS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-z4rpi — {@link CatalogRepository#mergeDocuments} collapses a
 * duplicate catalog entry into its canonical one in ONE transaction.
 *
 * <p>Replaces the client-side three-call recipe ({@code update(dup,
 * source_uri='')} + {@code update(canonical, source_uri=<uri>)} +
 * {@code update(dup, alias_of=canonical)}) that {@code
 * ux_catalog_documents_live_source_uri} (catalog-016) forces apart and that
 * a failure between any two calls could tear — either leaving a document
 * with no identity at all, or two live rows both claiming the same document
 * with no alias between them.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogMergeDocumentsTest {

    private static final String TENANT_A = "merge-a";
    private static final String TENANT_B = "merge-b";
    private static final String SVC_ROLE = "svc_merge_test";
    private static final String SVC_PASS = "svc_merge_pass";

    PostgreSQLContainer<?> pg;
    CatalogRepository repo;
    com.zaxxer.hikari.HikariDataSource svcDs;

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
        repo = new CatalogRepository(new TenantScope(svcDs));
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private String register(String tenant, String owner, String title, String sourceUri, String filePath) {
        var fields = new java.util.LinkedHashMap<String, Object>();
        fields.put("title", title);
        fields.put("file_path", filePath);
        if (sourceUri != null) fields.put("source_uri", sourceUri);
        return repo.registerDocument(tenant, owner, fields);
    }

    private Map<String, Object> readRow(String tenant, String tumbler) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var r = DSL.using(su, SQLDialect.POSTGRES)
                .select(CATALOG_DOCUMENTS.SOURCE_URI, CATALOG_DOCUMENTS.ALIAS_OF)
                .from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler)))
                .fetchOne();
            return Map.of("source_uri", r.value1() == null ? "" : r.value1(),
                          "alias_of", r.value2() == null ? "" : r.value2());
        }
    }

    /** Upsert a link via the repository's own {@code upsertLink} (created_by
     *  distinct per call site so co-discovery folding is observable). */
    private void link(String tenant, String from, String to, String type, String createdBy) {
        var lnk = new LinkedHashMap<String, Object>();
        lnk.put("from_tumbler", from);
        lnk.put("to_tumbler", to);
        lnk.put("link_type", type);
        lnk.put("created_by", createdBy);
        repo.upsertLink(tenant, lnk);
    }

    /** Raw read of one link row (created_by, metadata JSON), or empty if absent. */
    private Optional<Map<String, Object>> getLink(String tenant, String from, String to, String type) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var r = DSL.using(su, SQLDialect.POSTGRES)
                .select(CATALOG_LINKS.CREATED_BY, DSL.field(DSL.name("catalog_links", "metadata"), String.class))
                .from(CATALOG_LINKS)
                .where(CATALOG_LINKS.TENANT_ID.eq(tenant)
                       .and(CATALOG_LINKS.FROM_TUMBLER.eq(from))
                       .and(CATALOG_LINKS.TO_TUMBLER.eq(to))
                       .and(CATALOG_LINKS.LINK_TYPE.eq(type)))
                .fetchOne();
            if (r == null) return Optional.empty();
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("created_by", r.value1());
            row.put("metadata", r.value2());
            return Optional.of(row);
        }
    }

    /** Raw count of every link row touching *tumbler* in either direction. */
    private int countLinksTouching(String tenant, String tumbler) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES)
                .fetchCount(CATALOG_LINKS,
                    CATALOG_LINKS.TENANT_ID.eq(tenant)
                        .and(CATALOG_LINKS.FROM_TUMBLER.eq(tumbler).or(CATALOG_LINKS.TO_TUMBLER.eq(tumbler))));
        }
    }

    // ── semantics ───────────────────────────────────────────────────────

    @Test
    void merge_movesSourceUriToCanonical_whenCanonicalLacksOne() throws Exception {
        String duplicate = register(TENANT_A, "30", "dup", "file:///d1/a.md", "a.md");
        String canonical = register(TENANT_A, "30", "canonical", null, "b.md");

        var result = repo.mergeDocuments(TENANT_A, duplicate, canonical);
        assertThat(result.get("source_uri_moved")).isEqualTo(true);

        var dupRow = readRow(TENANT_A, duplicate);
        var canonRow = readRow(TENANT_A, canonical);
        assertThat(dupRow.get("source_uri")).as("duplicate's identity slot is freed").isEqualTo("");
        assertThat(dupRow.get("alias_of")).as("duplicate points at canonical").isEqualTo(canonical);
        assertThat(canonRow.get("source_uri")).as("canonical takes the durable identity")
            .isEqualTo("file:///d1/a.md");
    }

    @Test
    void merge_doesNotMoveSourceUri_whenCanonicalAlreadyHasOne() throws Exception {
        String duplicate = register(TENANT_A, "31", "dup", "file:///d2/a.md", "a.md");
        String canonical = register(TENANT_A, "31", "canonical", "file:///d2/canonical.md", "canonical.md");

        var result = repo.mergeDocuments(TENANT_A, duplicate, canonical);
        assertThat(result.get("source_uri_moved")).isEqualTo(false);

        var dupRow = readRow(TENANT_A, duplicate);
        var canonRow = readRow(TENANT_A, canonical);
        assertThat(dupRow.get("source_uri")).as("duplicate's identity slot is still freed").isEqualTo("");
        assertThat(dupRow.get("alias_of")).isEqualTo(canonical);
        assertThat(canonRow.get("source_uri")).as("canonical's own identity is untouched")
            .isEqualTo("file:///d2/canonical.md");
    }

    @Test
    void merge_isIdempotent_whenDuplicateAlreadyAliasedToSameCanonical() throws Exception {
        String duplicate = register(TENANT_A, "32", "dup", "file:///d3/a.md", "a.md");
        String canonical = register(TENANT_A, "32", "canonical", null, "b.md");

        repo.mergeDocuments(TENANT_A, duplicate, canonical);
        // Second call: the duplicate is now aliased to exactly this canonical
        // already — not "elsewhere" — so this must NOT refuse.
        var result = repo.mergeDocuments(TENANT_A, duplicate, canonical);
        assertThat(result.get("canonical")).isEqualTo(canonical);
        var dupRow = readRow(TENANT_A, duplicate);
        assertThat(dupRow.get("alias_of")).isEqualTo(canonical);
    }

    // ── refusals ────────────────────────────────────────────────────────

    @Test
    void merge_refusesSelfMerge() throws Exception {
        String doc = register(TENANT_A, "33", "solo", "file:///d4/a.md", "a.md");
        assertThatThrownBy(() -> repo.mergeDocuments(TENANT_A, doc, doc))
            .isInstanceOf(CatalogRepository.MergeRefused.class)
            .hasMessageContaining("cannot merge a document with itself");
    }

    @Test
    void merge_refusesWhenDuplicateNotFound() {
        String canonical = register(TENANT_A, "34", "canonical", null, "b.md");
        assertThatThrownBy(() -> repo.mergeDocuments(TENANT_A, "34.9999", canonical))
            .isInstanceOf(CatalogRepository.MergeRefused.class)
            .hasMessageContaining("duplicate not found");
    }

    @Test
    void merge_refusesWhenCanonicalNotFound() {
        String duplicate = register(TENANT_A, "35", "dup", "file:///d5/a.md", "a.md");
        assertThatThrownBy(() -> repo.mergeDocuments(TENANT_A, duplicate, "35.9999"))
            .isInstanceOf(CatalogRepository.MergeRefused.class)
            .hasMessageContaining("canonical not found");
    }

    @Test
    void merge_refusesCrossTenant() {
        // canonical registered under TENANT_B is invisible to a TENANT_A-scoped
        // merge call — RLS makes this structurally identical to "not found",
        // which IS the refusal (see MergeRefused's own javadoc).
        String duplicate = register(TENANT_A, "36", "dup", "file:///d6/a.md", "a.md");
        // A DIFFERENT owner literal than TENANT_A's, so the two registrations
        // cannot mint the same tumbler string and coincidentally read as a
        // self-merge instead of the cross-tenant case this test targets.
        String canonicalOtherTenant = register(TENANT_B, "136", "canonical-b", null, "b.md");
        assertThatThrownBy(() -> repo.mergeDocuments(TENANT_A, duplicate, canonicalOtherTenant))
            .isInstanceOf(CatalogRepository.MergeRefused.class)
            .hasMessageContaining("canonical not found");
    }

    @Test
    void merge_refusesAlreadyAliasedDuplicatePointingElsewhere() throws Exception {
        String duplicate = register(TENANT_A, "37", "dup", "file:///d7/a.md", "a.md");
        String otherCanonical = register(TENANT_A, "37", "other-canonical", null, "b.md");
        String newCanonical = register(TENANT_A, "37", "new-canonical", null, "c.md");
        repo.mergeDocuments(TENANT_A, duplicate, otherCanonical);  // dup -> otherCanonical

        assertThatThrownBy(() -> repo.mergeDocuments(TENANT_A, duplicate, newCanonical))
            .isInstanceOf(CatalogRepository.MergeRefused.class)
            .hasMessageContaining("already aliased to " + otherCanonical);
    }

    @Test
    void merge_refusesCycle() throws Exception {
        // canonical is already (transitively) an alias OF duplicate: pointing
        // duplicate -> canonical would close a loop.
        String duplicate = register(TENANT_A, "38", "dup", "file:///d8/a.md", "a.md");
        String canonical = register(TENANT_A, "38", "canonical", null, "b.md");
        repo.setAlias(TENANT_A, canonical, duplicate);  // canonical -> duplicate

        assertThatThrownBy(() -> repo.mergeDocuments(TENANT_A, duplicate, canonical))
            .isInstanceOf(CatalogRepository.MergeRefused.class)
            .hasMessageContaining("alias cycle");
    }

    // ── links (nexus-z4rpi follow-up) ─────────────────────────────────────

    @Test
    void merge_remapsLinksInBothDirections() throws Exception {
        String duplicate = register(TENANT_A, "40", "dup", "file:///d10/a.md", "a.md");
        String canonical = register(TENANT_A, "40", "canonical", null, "b.md");
        String other1 = register(TENANT_A, "40", "other1", null, "c.md");
        String other2 = register(TENANT_A, "40", "other2", null, "d.md");

        link(TENANT_A, duplicate, other1, "cites", "agent-x");   // duplicate is FROM
        link(TENANT_A, other2, duplicate, "relates", "agent-y"); // duplicate is TO

        var result = repo.mergeDocuments(TENANT_A, duplicate, canonical);
        assertThat(result.get("links_remapped")).isEqualTo(2);
        assertThat(result.get("links_collapsed")).isEqualTo(0);
        assertThat(result.get("links_dropped")).isEqualTo(0);

        assertThat(getLink(TENANT_A, duplicate, other1, "cites")).as("old FROM-side row gone").isEmpty();
        assertThat(getLink(TENANT_A, other2, duplicate, "relates")).as("old TO-side row gone").isEmpty();
        assertThat(getLink(TENANT_A, canonical, other1, "cites")).as("FROM-side remapped").isPresent();
        assertThat(getLink(TENANT_A, other2, canonical, "relates")).as("TO-side remapped").isPresent();
        assertThat(countLinksTouching(TENANT_A, duplicate)).isZero();
    }

    @Test
    void merge_collapsesLinkThatCollidesWithAnExistingCanonicalLink() throws Exception {
        String duplicate = register(TENANT_A, "41", "dup", "file:///d11/a.md", "a.md");
        String canonical = register(TENANT_A, "41", "canonical", null, "b.md");
        String target = register(TENANT_A, "41", "target", null, "c.md");

        // Canonical already links to target; duplicate links to the SAME
        // target with the SAME type -- the rewrite makes these collide.
        link(TENANT_A, canonical, target, "relates", "agent-x");
        link(TENANT_A, duplicate, target, "relates", "agent-y");

        var result = repo.mergeDocuments(TENANT_A, duplicate, canonical);
        assertThat(result.get("links_collapsed")).isEqualTo(1);
        assertThat(result.get("links_remapped")).isEqualTo(0);

        var survivor = getLink(TENANT_A, canonical, target, "relates");
        assertThat(survivor).isPresent();
        assertThat(survivor.get().get("created_by"))
            .as("the SURVIVING link's original creator is preserved, not overwritten")
            .isEqualTo("agent-x");
        assertThat(String.valueOf(survivor.get().get("metadata")))
            .as("the doomed link's creator is folded into co_discovered_by, the same fold upsertLink uses for created=False")
            .contains("agent-y");
        assertThat(countLinksTouching(TENANT_A, duplicate)).isZero();
    }

    @Test
    void merge_dropsTheSelfLinkTheRewriteWouldCreate() throws Exception {
        String duplicate = register(TENANT_A, "42", "dup", "file:///d12/a.md", "a.md");
        String canonical = register(TENANT_A, "42", "canonical", null, "b.md");

        // A direct link between the two documents being merged becomes a
        // self-link once duplicate is rewritten to canonical.
        link(TENANT_A, duplicate, canonical, "cites", "agent-x");

        var result = repo.mergeDocuments(TENANT_A, duplicate, canonical);
        assertThat(result.get("links_dropped")).isEqualTo(1);
        assertThat(result.get("links_remapped")).isEqualTo(0);
        assertThat(result.get("links_collapsed")).isEqualTo(0);

        assertThat(getLink(TENANT_A, canonical, canonical, "cites"))
            .as("no self-link was written").isEmpty();
        assertThat(countLinksTouching(TENANT_A, duplicate)).isZero();
        assertThat(countLinksTouching(TENANT_A, canonical)).isZero();
    }

    // ── atomicity ───────────────────────────────────────────────────────

    @Test
    void merge_midTransactionFailure_rollsBackEverything() throws Exception {
        // Force a failure on the LAST statement (the alias_of write) via a
        // throwaway CHECK constraint naming the canonical's own tumbler —
        // known only after registration, since tumblers are server-assigned.
        // The FIRST two statements (free duplicate's source_uri, move it onto
        // canonical) already executed inside the SAME uncommitted
        // transaction; this proves they roll back together rather than
        // leaving either row half-migrated.
        String duplicate = register(TENANT_A, "39", "dup", "file:///d9/a.md", "a.md");
        String canonical = register(TENANT_A, "39", "canonical", null, "b.md");

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).execute(
                "ALTER TABLE nexus.catalog_documents ADD CONSTRAINT ck_merge_test_poison "
                + "CHECK (alias_of <> '" + canonical + "')");
        }
        try {
            assertThatThrownBy(() -> repo.mergeDocuments(TENANT_A, duplicate, canonical))
                .as("mid-transaction CHECK violation propagates")
                .isInstanceOf(Exception.class);

            var dupRow = readRow(TENANT_A, duplicate);
            var canonRow = readRow(TENANT_A, canonical);
            assertThat(dupRow.get("source_uri")).as("duplicate's source_uri NOT freed (rolled back)")
                .isEqualTo("file:///d9/a.md");
            assertThat(dupRow.get("alias_of")).as("duplicate NOT aliased (rolled back)").isEqualTo("");
            assertThat(canonRow.get("source_uri")).as("canonical did NOT receive the URI (rolled back)")
                .isEqualTo("");
        } finally {
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                DSL.using(su, SQLDialect.POSTGRES).execute(
                    "ALTER TABLE nexus.catalog_documents DROP CONSTRAINT ck_merge_test_poison");
            }
        }
    }

    @Test
    void merge_midTransactionFailure_rollsBackLinkRewritesToo() throws Exception {
        // The sibling test above poisons the LAST document write (alias_of),
        // which fails before remapLinksForMerge ever runs -- it proves
        // document-write atomicity but exercises no link code at all. This
        // one poisons the LINK rewrite itself: two links to remap, ordered
        // by id (remapLinksForMerge's own ORDER BY), the FIRST succeeds
        // inside the transaction, the SECOND hits a throwaway CHECK
        // constraint and fails -- proving the first link's ALREADY-APPLIED
        // rewrite rolls back together with everything else, not just that
        // the second one never landed.
        String duplicate = register(TENANT_A, "43", "dup", "file:///d13/a.md", "a.md");
        String canonical = register(TENANT_A, "43", "canonical", null, "b.md");
        String other1 = register(TENANT_A, "43", "other1", null, "c.md");
        String other2 = register(TENANT_A, "43", "other2", null, "d.md");

        // Lower id (created first) -- the loop processes this one first.
        link(TENANT_A, duplicate, other1, "cites", "agent-x");
        // Higher id -- processed second, and its rewritten target
        // (canonical, other2, "relates") is what the poison forbids.
        link(TENANT_A, duplicate, other2, "relates", "agent-y");

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).execute(
                "ALTER TABLE nexus.catalog_links ADD CONSTRAINT ck_merge_link_test_poison "
                + "CHECK (NOT (from_tumbler = '" + canonical + "' AND link_type = 'relates'))");
        }
        try {
            assertThatThrownBy(() -> repo.mergeDocuments(TENANT_A, duplicate, canonical))
                .as("mid-link-rewrite CHECK violation propagates")
                .isInstanceOf(Exception.class);

            // The SECOND link's rewrite never landed (expected -- it's what
            // the poison forbids)...
            assertThat(getLink(TENANT_A, canonical, other2, "relates")).isEmpty();
            // ...but critically, the FIRST link's rewrite -- which DID
            // execute, inside the same uncommitted transaction -- rolled
            // back too, rather than surviving the later failure.
            assertThat(getLink(TENANT_A, canonical, other1, "cites"))
                .as("the first link's already-applied rewrite must roll back").isEmpty();
            assertThat(getLink(TENANT_A, duplicate, other1, "cites"))
                .as("the original row is restored, not left half-migrated").isPresent();
            assertThat(getLink(TENANT_A, duplicate, other2, "relates")).isPresent();

            // And the document-level writes that preceded the link rewrite
            // (all inside the same transaction) rolled back with it.
            var dupRow = readRow(TENANT_A, duplicate);
            assertThat(dupRow.get("source_uri")).as("duplicate's source_uri NOT freed")
                .isEqualTo("file:///d13/a.md");
            assertThat(dupRow.get("alias_of")).as("duplicate NOT aliased").isEqualTo("");
        } finally {
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                DSL.using(su, SQLDialect.POSTGRES).execute(
                    "ALTER TABLE nexus.catalog_links DROP CONSTRAINT ck_merge_link_test_poison");
            }
        }
    }

    // ── scale (round 2: set-based remapLinksForMerge) ─────────────────────

    @Test
    void merge_fiveHundredLinks_setBasedRemapStaysCorrect() throws Exception {
        // A regression to the old per-row loop would still PASS the small
        // fixtures above -- this exists so it can't hide behind them. 500
        // touching rows, all three outcomes represented at volume: 200 plain
        // renames (100 FROM-side, 100 TO-side), 80 collapses (canonical
        // already holds the matching edge), and 20 self-link drops (20
        // DISTINCT types between duplicate and canonical, each its own row
        // under the (tenant, from, to, type) key).
        String duplicate = register(TENANT_A, "50", "dup", "file:///d50/a.md", "a.md");
        String canonical = register(TENANT_A, "50", "canonical", null, "b.md");

        String[] fromTargets = new String[100];
        for (int i = 0; i < 100; i++) {
            fromTargets[i] = register(TENANT_A, "50", "from-target-" + i, null, "from-" + i + ".md");
            link(TENANT_A, duplicate, fromTargets[i], "cites", "agent-scale");
        }
        String[] toTargets = new String[100];
        for (int i = 0; i < 100; i++) {
            toTargets[i] = register(TENANT_A, "50", "to-target-" + i, null, "to-" + i + ".md");
            link(TENANT_A, toTargets[i], duplicate, "cites", "agent-scale");
        }
        String[] collapseTargets = new String[80];
        for (int i = 0; i < 80; i++) {
            collapseTargets[i] = register(TENANT_A, "50", "collapse-target-" + i, null, "collapse-" + i + ".md");
            link(TENANT_A, canonical, collapseTargets[i], "relates", "agent-canonical-" + i);
            link(TENANT_A, duplicate, collapseTargets[i], "relates", "agent-duplicate-" + i);
        }
        for (int i = 0; i < 20; i++) {
            link(TENANT_A, duplicate, canonical, "self-type-" + i, "agent-self");
        }

        var result = repo.mergeDocuments(TENANT_A, duplicate, canonical);
        assertThat(result.get("links_remapped")).isEqualTo(200);
        assertThat(result.get("links_collapsed")).isEqualTo(80);
        assertThat(result.get("links_dropped")).isEqualTo(20);

        // Spot-check across the range, not just the first/last -- a
        // regression to per-row processing that silently drops middle rows
        // (e.g. a batch-size bug) would still pass a boundary-only check.
        for (int i : new int[]{0, 1, 49, 50, 98, 99}) {
            assertThat(getLink(TENANT_A, canonical, fromTargets[i], "cites"))
                .as("FROM-side remap #" + i).isPresent();
            assertThat(getLink(TENANT_A, toTargets[i], canonical, "cites"))
                .as("TO-side remap #" + i).isPresent();
        }
        for (int i : new int[]{0, 1, 39, 40, 78, 79}) {
            var survivor = getLink(TENANT_A, canonical, collapseTargets[i], "relates");
            assertThat(survivor).as("collapse #" + i).isPresent();
            assertThat(survivor.get().get("created_by"))
                .as("collapse #" + i + " keeps the canonical's original creator")
                .isEqualTo("agent-canonical-" + i);
            assertThat(String.valueOf(survivor.get().get("metadata")))
                .as("collapse #" + i + " folds the duplicate's creator into co_discovered_by")
                .contains("agent-duplicate-" + i);
        }
        assertThat(countLinksTouching(TENANT_A, duplicate))
            .as("nothing touches the duplicate any more").isZero();
    }

    // ── concurrency (round 3: code review 2026-09-23) ──────────────────────

    /** Release two threads at the same instant: each counts down *ready*
     *  then blocks on *go*, so both are parked at the starting line before
     *  either proceeds -- true race timing rather than a head start for
     *  whichever thread's Thread.start() happened to run first. */
    private static void runConcurrently(Runnable a, Runnable b) throws InterruptedException {
        CountDownLatch ready = new CountDownLatch(2);
        CountDownLatch go = new CountDownLatch(1);
        Thread t1 = new Thread(() -> {
            ready.countDown();
            try {
                go.await();
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return;
            }
            a.run();
        });
        Thread t2 = new Thread(() -> {
            ready.countDown();
            try {
                go.await();
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return;
            }
            b.run();
        });
        t1.start();
        t2.start();
        ready.await();
        go.countDown();
        t1.join(30_000);
        t2.join(30_000);
    }

    @Test
    void merge_concurrentMergesOfSameDuplicate_exactlyOneWinsNoLostUpdate() throws Exception {
        // nexus-z4rpi round 3 (code review 2026-09-23): mergeDocuments takes
        // no row lock, so two concurrent merges of the SAME duplicate into
        // DIFFERENT canonicals could both validate against a pre-merge
        // snapshot under READ COMMITTED and silently lose one's update.
        // lockDocumentRowsSorted (FOR UPDATE, sorted-tumbler order) closes
        // this: whichever call acquires the duplicate's lock first runs to
        // completion; the other blocks until that commit, re-reads
        // POST-commit state, and refuses cleanly (already-aliased-
        // elsewhere) instead of corrupting or silently losing anything.
        String duplicate = register(TENANT_A, "51", "dup", "file:///d51/a.md", "a.md");
        String canonicalA = register(TENANT_A, "51", "canonical-a", null, "b.md");
        String canonicalB = register(TENANT_A, "51", "canonical-b", null, "c.md");

        AtomicReference<Object> resultA = new AtomicReference<>();
        AtomicReference<Object> resultB = new AtomicReference<>();
        runConcurrently(
            () -> {
                try {
                    resultA.set(repo.mergeDocuments(TENANT_A, duplicate, canonicalA));
                } catch (Exception e) {
                    resultA.set(e);
                }
            },
            () -> {
                try {
                    resultB.set(repo.mergeDocuments(TENANT_A, duplicate, canonicalB));
                } catch (Exception e) {
                    resultB.set(e);
                }
            }
        );

        boolean aSucceeded = resultA.get() instanceof Map;
        boolean bSucceeded = resultB.get() instanceof Map;
        assertThat(aSucceeded ^ bSucceeded)
            .as("exactly one merge must succeed, the other must cleanly refuse: a=%s b=%s",
                resultA.get(), resultB.get())
            .isTrue();

        Object failure = aSucceeded ? resultB.get() : resultA.get();
        assertThat(failure).isInstanceOf(CatalogRepository.MergeRefused.class);
        assertThat(((Exception) failure).getMessage()).contains("already aliased to");

        // The persisted state matches the WINNER outright -- not a blend,
        // not a value from neither call (the lost-update shape this test
        // exists to rule out).
        String winnerCanonical = aSucceeded ? canonicalA : canonicalB;
        var dupRow = readRow(TENANT_A, duplicate);
        assertThat(dupRow.get("alias_of")).isEqualTo(winnerCanonical);
    }

    @Test
    void merge_linkUpsertRacingAMerge_linkEndsUpOnCanonical() throws Exception {
        // nexus-z4rpi round 3 (code review 2026-09-23): remapLinksForMerge
        // is a one-time snapshot, so a concurrent upsertLink creating a link
        // TO the duplicate mid-merge could survive unremapped. upsertLink
        // now locks both endpoints FOR KEY SHARE (the SAME sorted-tumbler
        // order mergeDocuments locks FOR UPDATE, so the two serialize) and
        // resolves aliases via resolveAliasTarget AFTER the lock -- so
        // whichever call wins the race, the link ends up on the canonical:
        // if the merge commits first, the link write resolves through the
        // now-live alias directly; if the link write commits first, the
        // merge's own remapLinksForMerge read sees it (same transaction,
        // READ COMMITTED) and remaps it in the same pass.
        String duplicate = register(TENANT_A, "52", "dup", "file:///d52/a.md", "a.md");
        String canonical = register(TENANT_A, "52", "canonical", null, "b.md");
        String other = register(TENANT_A, "52", "other", null, "c.md");

        AtomicReference<Object> mergeResult = new AtomicReference<>();
        AtomicReference<Object> linkResult = new AtomicReference<>();
        runConcurrently(
            () -> {
                try {
                    mergeResult.set(repo.mergeDocuments(TENANT_A, duplicate, canonical));
                } catch (Exception e) {
                    mergeResult.set(e);
                }
            },
            () -> {
                try {
                    var lnk = new LinkedHashMap<String, Object>();
                    lnk.put("from_tumbler", duplicate);
                    lnk.put("to_tumbler", other);
                    lnk.put("link_type", "cites");
                    lnk.put("created_by", "race-agent");
                    linkResult.set(repo.upsertLink(TENANT_A, lnk));
                } catch (Exception e) {
                    linkResult.set(e);
                }
            }
        );

        assertThat(mergeResult.get()).as("merge must succeed: %s", mergeResult.get()).isInstanceOf(Map.class);
        assertThat(linkResult.get()).as("link upsert must succeed: %s", linkResult.get()).isInstanceOf(Boolean.class);

        assertThat(getLink(TENANT_A, canonical, other, "cites"))
            .as("the link resolves to the canonical, whichever call won the race").isPresent();
        assertThat(getLink(TENANT_A, duplicate, other, "cites"))
            .as("the link must never survive pointing at the duplicate").isEmpty();
    }

    // ── round 4 (batch-4 code review, 2026-09-23) ──────────────────────────

    @Test
    void upsertLink_allowsSelfLinkWhenBothEndpointsResolveToTheSameCanonical() throws Exception {
        // batch-4 item 1: alias resolution can make from==to even when the
        // RAW request named two different tumblers. Decision: ALLOW, not
        // refuse -- matches upsertLink's own pre-round-4 behavior for an
        // EXPLICIT self-link on UNRESOLVED input (never guarded either) and
        // matches the schema itself (no CHECK constraint forbids
        // from==to; ReadShapeViewsTest already seeds one via raw SQL as a
        // fixture). remapLinksForMerge DROPS this shape when a LATER merge
        // creates it on an EXISTING link row (merge_dropsTheSelfLinkThe...
        // above) -- that is a different call with a different job; this one
        // proves upsertLink itself never refuses it.
        String canonical = register(TENANT_A, "60", "canonical", null, "a60.md");
        String aliasOne = register(TENANT_A, "60", "alias-one", null, "b60.md");
        String aliasTwo = register(TENANT_A, "60", "alias-two", null, "c60.md");
        repo.mergeDocuments(TENANT_A, aliasOne, canonical);
        repo.mergeDocuments(TENANT_A, aliasTwo, canonical);

        var lnk = new LinkedHashMap<String, Object>();
        lnk.put("from_tumbler", aliasOne);
        lnk.put("to_tumbler", aliasTwo);
        lnk.put("link_type", "cites");
        lnk.put("created_by", "agent-y");
        boolean created = repo.upsertLink(TENANT_A, lnk);

        assertThat(created).isTrue();
        assertThat(getLink(TENANT_A, canonical, canonical, "cites"))
            .as("both raw endpoints resolved to the SAME canonical; the self-link is written, not refused")
            .isPresent();
    }

    @Test
    void setAlias_locksTheRowSoConcurrentWritersResolveDeterministically() throws Exception {
        // batch-4 item 2: setAlias took no lock. Two concurrent blind
        // single-statement UPDATEs to the same row are already crash- and
        // corruption-free under Postgres's own implicit per-statement row
        // lock (whichever commits last wins, deterministically) -- there is
        // no multi-statement read-then-write gap here the way mergeDocuments
        // and upsertLink had, since setAlias never reads alias_of before
        // writing it. Locking FOR UPDATE first still matters: it gives
        // setAlias the SAME serialization priority mergeDocuments and
        // upsertLink already hold, so a setAlias call racing either of
        // THOSE (which DO take locks) queues behind them instead of
        // slipping in via an unlocked statement. Proven here against
        // another setAlias call (the narrowest case setAlias itself can
        // race); the result must be exactly one of the two attempted
        // values, never a torn/null state.
        //
        // NOTE (premise correction): setAlias is NOT actually the engine
        // path behind `nx catalog update --alias-of` -- grep of
        // CatalogHandler's route table shows no route bound to
        // repo.setAlias(...) at all; the real --alias-of path is
        // updateDocument/buildUpdateDocumentQuery's alias_of-SET carve-out
        // (nexus-ekaxn), reached via /update. setAlias has zero production
        // callers today (Java-level test callers only). The lock is added
        // here anyway, matching the review's literal ask and keeping intent
        // consistent for any future caller.
        String tumbler = register(TENANT_A, "61", "doc", null, "a61.md");
        String canonicalA = register(TENANT_A, "61", "canonical-a", null, "b61.md");
        String canonicalB = register(TENANT_A, "61", "canonical-b", null, "c61.md");

        runConcurrently(
            () -> repo.setAlias(TENANT_A, tumbler, canonicalA),
            () -> repo.setAlias(TENANT_A, tumbler, canonicalB)
        );

        var row = readRow(TENANT_A, tumbler);
        assertThat(row.get("alias_of"))
            .as("exactly one writer's value persists -- no torn/blended/null state")
            .isIn(canonicalA, canonicalB);
    }

    @Test
    void upsertLink_oneHopStaleAlias_landsOnCanonicalWhenTheAliasTargetIsMergedConcurrently() throws Exception {
        // batch-4 code review refinement (2026-09-23, "lock the resolved
        // target, not the raw one"): oldAlias -> x is settled BEFORE the
        // race starts. resolveAndLockLinkEndpoints must resolve
        // oldAlias -> x, THEN discover x is concurrently being merged into
        // y under its OWN lock, and land the link on y regardless of which
        // call wins -- closing the gap the simpler raw-endpoint lock left
        // open (a raw endpoint that is itself an already-settled alias
        // pointing at a document concurrently being merged elsewhere was
        // not serialized against that merge at all).
        String x = register(TENANT_A, "62", "x", null, "x62.md");
        String y = register(TENANT_A, "62", "y", null, "y62.md");
        String other = register(TENANT_A, "62", "other", null, "other62.md");
        String oldAlias = register(TENANT_A, "62", "old-alias", null, "old62.md");
        repo.mergeDocuments(TENANT_A, oldAlias, x);   // oldAlias -> x, settled BEFORE the race

        AtomicReference<Object> mergeResult = new AtomicReference<>();
        AtomicReference<Object> linkResult = new AtomicReference<>();
        runConcurrently(
            () -> {
                try {
                    mergeResult.set(repo.mergeDocuments(TENANT_A, x, y));
                } catch (Exception e) {
                    mergeResult.set(e);
                }
            },
            () -> {
                try {
                    var lnk = new LinkedHashMap<String, Object>();
                    lnk.put("from_tumbler", oldAlias);
                    lnk.put("to_tumbler", other);
                    lnk.put("link_type", "cites");
                    lnk.put("created_by", "race-agent-2");
                    linkResult.set(repo.upsertLink(TENANT_A, lnk));
                } catch (Exception e) {
                    linkResult.set(e);
                }
            }
        );

        assertThat(mergeResult.get()).as("merge must succeed: %s", mergeResult.get()).isInstanceOf(Map.class);
        assertThat(linkResult.get()).as("link upsert must succeed: %s", linkResult.get()).isInstanceOf(Boolean.class);

        assertThat(getLink(TENANT_A, y, other, "cites"))
            .as("the one-hop stale alias resolved through x to the merge's canonical y")
            .isPresent();
        assertThat(getLink(TENANT_A, x, other, "cites"))
            .as("the link must never survive pointing at x, the mid-race duplicate").isEmpty();
        assertThat(getLink(TENANT_A, oldAlias, other, "cites"))
            .as("the link must never survive pointing at the raw, already-stale alias either").isEmpty();
    }

    @Test
    void upsertLink_1000Writes_timingBenchmark() throws Exception {
        // batch-4 item 3: numbers captured for the commit message, not
        // asserted against a threshold (machine/CI variance) -- just
        // logged and measured before/after against round-3's per-endpoint
        // lock + resolveAliasTarget + liveDocument shape (~3 queries per
        // endpoint, ~6-7 total) versus round-4's fold (lockAndReadDocRow
        // combines lock+alias_of+liveness into ONE query per endpoint per
        // resolution attempt).
        int n = 1000;
        String[] targets = new String[n];
        for (int i = 0; i < n; i++) {
            targets[i] = register(TENANT_A, "63", "bench-target-" + i, null, "bench63-" + i + ".md");
        }
        String hub = register(TENANT_A, "63", "bench-hub", null, "bench63-hub.md");

        long start = System.nanoTime();
        for (int i = 0; i < n; i++) {
            var lnk = new LinkedHashMap<String, Object>();
            lnk.put("from_tumbler", hub);
            lnk.put("to_tumbler", targets[i]);
            lnk.put("link_type", "cites");
            lnk.put("created_by", "bench-agent");
            repo.upsertLink(TENANT_A, lnk);
        }
        long elapsedMs = (System.nanoTime() - start) / 1_000_000;
        System.out.println("upsertLink " + n + "-write benchmark (nexus-z4rpi round 4 item 3): "
            + elapsedMs + " ms total, " + (elapsedMs / (double) n) + " ms/call");
        assertThat(elapsedMs).isGreaterThan(0);
    }
}
