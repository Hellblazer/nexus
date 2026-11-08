// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.AspectRepository;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantScope;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.concurrent.atomic.AtomicInteger;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-231 follow-on (nexus-bivz3.24, critique Issue 7): the census and the rewrite of NOTE rows whose
 * {@code source_uri} names a collection the note no longer lives in.
 *
 * <p>Each test plants its own tenant, because the census counts the whole tenant and a number is only
 * exact when nothing else is in it. One planting ({@link #plant}) holds one row of every class the census
 * names, with the expected counts written beside it.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class NoteUriRepairTest {

    private static final String SVC_ROLE = "svc_note_uri_repair_test";
    private static final String SVC_PASS = "svc_note_uri_repair_test_pass";
    private static final String TS = "2026-11-08T00:00:00Z";
    private static final TypeReference<Map<String, Object>> MAP_T = new TypeReference<>() {};
    private static final AtomicInteger SEQ = new AtomicInteger();

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    CatalogRepository repo;
    AspectRepository aspectRepo;
    NexusService service;
    HttpClient http;
    ObjectMapper mapper = new ObjectMapper();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(6);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        repo = new CatalogRepository(tenantScope);
        aspectRepo = new AspectRepository(tenantScope);
        service = new NexusService(0, "note-uri-repair-root-token", svcDs);
        service.start();
        http = HttpClient.newHttpClient();
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @AfterEach
    void clearHook() {
        CatalogRepository.setNoteUriRepairHookForTests(null);
    }

    // ── the planting ──────────────────────────────────────────────────────────

    /**
     * One tenant holding a row of every class. Y is the collection every note lives in; X1 and X2 are collections
     * notes used to live in. {@code expectedRewrite} is the set of tumblers the census must call rewrite candidates.
     */
    final class Fx {
        final String tenant;
        final String token;
        final String y;
        final String x1;
        final String x2;
        final String docsColl;

        Fx(String tag) throws Exception {
            int n = SEQ.incrementAndGet();
            tenant = "repair-" + tag + "-" + n;
            token = "note-uri-repair-token-" + tag + "-" + n;
            y = coll("y" + n);
            x1 = coll("x1-" + n);
            x2 = coll("x2-" + n);
            docsColl = "docs__repair-" + n + "__minilm-l6-v2-384__v1";
            try (Connection su = pg.createConnection("")) {
                var dsl = DSL.using(su, SQLDialect.POSTGRES);
                PgContainerHelper.seedServiceToken(dsl, token, tenant, "repair-test");
                for (String c : List.of(y, x1, x2, docsColl)) PgContainerHelper.insertCollection(dsl, tenant, c);
            }
        }

        String nx(String coll, String title) {
            return "nx-note://" + coll + "/" + title;
        }
    }

    /**
     * Plants the whole census. Expected, tenant-wide: 4 rewrite candidates (a1, a2, c1, l1), 3 merge candidates
     * (m1 with a twin, s1 and s2 which rewrite onto one URI), 2 refused (r1 file-routed collection, m2 malformed),
     * 1 tombstoned stale row (d1), 2 export copies of which 1 has a moved target. Rows that are not counted at
     * all: f1 (not stale), t1/t2 (twins), file/https/residue rows.
     */
    Fx plant(String tag) throws Exception {
        Fx f = new Fx(tag);
        String t = f.tenant;
        // rewrite candidates
        seedNote(t, "a1", f.y, f.nx(f.x1, "alpha"));
        seedAspect(t, "a1", f.y, "notes/alpha.md", f.nx(f.x1, "notes/alpha.md"));
        seedHighlight(t, "a1", f.y, f.nx(f.x1, "alpha"));
        seedNote(t, "a2", f.y, f.nx(f.x2, "beta"));
        seedNote(t, "c1", f.y, f.nx(f.x1, "eps"));
        seedNote(t, "c0", f.y, f.nx(f.y, "eps"));        // the tombstoned holder of c1's target
        tombstone(t, "c0");
        seedNote(t, "l1", f.y, "chroma://" + f.x1 + "/legacy");  // a legacy spelling the backfill never reached
        // merge candidates: m1 has a live twin; s1 and s2 would rewrite onto one URI
        seedNote(t, "m1", f.y, f.nx(f.x1, "gamma"));
        seedNote(t, "t1", f.y, f.nx(f.y, "gamma"));
        seedNote(t, "s1", f.y, f.nx(f.x1, "delta"));
        seedNote(t, "s2", f.y, f.nx(f.x2, "delta"));
        // refused: a note whose physical collection is file-routed, a URI with no title part
        seedNote(t, "r1", f.docsColl, f.nx(f.x1, "odd"));
        seedNote(t, "m2", f.y, f.nx(f.y, "malformed-seed"));
        setUri(t, "m2", "nx-note://" + f.x1);
        // tombstoned stale row
        seedNote(t, "d1", f.y, f.nx(f.x1, "dead"));
        tombstone(t, "d1");
        // not counted: a note in its own collection, residue, other schemes
        seedNote(t, "f1", f.y, f.nx(f.y, "fine"));
        seedNote(t, "res", f.y, "nx-note://docs__elsewhere/residue");
        seedNote(t, "file", f.y, "file:///papers/a.pdf");
        seedNote(t, "web", f.y, "https://example.org/a");
        // export copies: the first targets its own collection, the second names a collection it left
        seedNote(t, "e1", f.y, "nxexp://" + f.y + "/" + f.nx(f.x1, "export"));
        seedNote(t, "e2", f.y, "nxexp://" + f.x2 + "/" + f.nx(f.x1, "export2"));
        seedNote(t, "e3", f.y, "nxexp://" + f.y + "/file:///papers/b.pdf");
        return f;
    }

    // ── census ────────────────────────────────────────────────────────────────

    @Test
    void census_countsEveryClassExactly_andListsTheSamples() throws Exception {
        Fx f = plant("census");

        Map<String, Object> r = repo.repairStaleNoteUris(f.tenant, false, 1000, 50);

        assertThat(r.get("apply")).isEqualTo(false);
        assertThat(r.get("stale_live")).as("live stale note rows = rewrite + merge + refused").isEqualTo(9);
        assertThat(r.get("rewrite")).isEqualTo(4);
        assertThat(r.get("merge")).isEqualTo(3);
        assertThat(r.get("merge_twin")).isEqualTo(1);
        assertThat(r.get("merge_sibling")).isEqualTo(2);
        assertThat(r.get("refused")).isEqualTo(2);
        assertThat(r.get("refused_file_routed_collection")).isEqualTo(1);
        assertThat(r.get("refused_malformed")).isEqualTo(1);
        assertThat(r.get("tombstoned")).isEqualTo(1);
        assertThat(r.get("export_copies")).isEqualTo(2);
        assertThat(r.get("export_copies_target_moved")).isEqualTo(1);
        assertThat(r).as("a dry run reports no rewrite").doesNotContainKey("rewritten");

        Map<String, List<Map<String, Object>>> sample = sampleOf(r);
        assertThat(tumblers(sample.get("rewrite"))).containsExactly("a1", "a2", "c1", "l1");
        assertThat(tumblers(sample.get("merge"))).containsExactly("m1", "s1", "s2");
        assertThat(tumblers(sample.get("refused"))).containsExactly("m2", "r1");
        assertThat(tumblers(sample.get("tombstoned"))).containsExactly("d1");
        assertThat(tumblers(sample.get("export_target_moved"))).containsExactly("e2");
        Map<String, Object> a1 = byTumbler(sample.get("rewrite"), "a1");
        assertThat(a1.get("source_uri")).isEqualTo(f.nx(f.x1, "alpha"));
        assertThat(a1.get("new_uri")).isEqualTo(f.nx(f.y, "alpha"));
        assertThat(a1.get("collection")).isEqualTo(f.y);
        assertThat(byTumbler(sample.get("rewrite"), "l1").get("new_uri"))
            .as("a legacy spelling is rewritten to the stored spelling").isEqualTo(f.nx(f.y, "legacy"));
        assertThat(byTumbler(sample.get("rewrite"), "c1").get("new_uri"))
            .as("a tombstoned holder does not block").isEqualTo(f.nx(f.y, "eps"));
        assertThat(byTumbler(sample.get("merge"), "m1").get("twin")).isEqualTo("t1");
        assertThat(byTumbler(sample.get("merge"), "m1").get("reason")).isEqualTo("twin");
        assertThat(byTumbler(sample.get("merge"), "s1").get("reason")).isEqualTo("sibling");
        assertThat(byTumbler(sample.get("merge"), "s1").get("twin")).isEqualTo("s2");
        assertThat(byTumbler(sample.get("refused"), "r1").get("reason")).isEqualTo("file_routed_collection");
        assertThat(byTumbler(sample.get("refused"), "m2").get("reason")).isEqualTo("malformed");
    }

    @Test
    void census_theSampleCapBoundsTheListsAndNeverTheCounts() throws Exception {
        Fx f = plant("sample");

        Map<String, Object> r = repo.repairStaleNoteUris(f.tenant, false, 1000, 1);

        assertThat(r.get("rewrite")).isEqualTo(4);
        assertThat(r.get("merge")).isEqualTo(3);
        assertThat(sampleOf(r).get("rewrite")).hasSize(1);
        assertThat(sampleOf(r).get("merge")).hasSize(1);
        Map<String, Object> none = repo.repairStaleNoteUris(f.tenant, false, 1000, 0);
        assertThat(none.get("rewrite")).isEqualTo(4);
        assertThat(sampleOf(none).get("rewrite")).isEmpty();
    }

    @Test
    void census_countsOnlyTheCallersTenant() throws Exception {
        Fx mine = plant("mine");
        Fx other = new Fx("other");
        seedNote(other.tenant, "o1", other.y, other.nx(other.x1, "elsewhere"));

        assertThat(repo.repairStaleNoteUris(mine.tenant, false, 1000, 5).get("rewrite")).isEqualTo(4);
        assertThat(repo.repairStaleNoteUris(other.tenant, false, 1000, 5).get("rewrite")).isEqualTo(1);
        repo.repairStaleNoteUris(mine.tenant, true, 1000, 5);
        assertThat(docUri(other.tenant, "o1")).as("another tenant's stale row is not touched")
            .isEqualTo(other.nx(other.x1, "elsewhere"));
    }

    @Test
    void census_anEmptyTenantReportsZeroEverywhere() throws Exception {
        Fx f = new Fx("empty");
        Map<String, Object> r = repo.repairStaleNoteUris(f.tenant, false, 1000, 5);
        for (String k : List.of("stale_live", "rewrite", "merge", "merge_twin", "merge_sibling", "refused",
                "refused_file_routed_collection", "refused_malformed", "tombstoned", "export_copies",
                "export_copies_target_moved")) {
            assertThat(r.get(k)).as(k).isEqualTo(0);
        }
    }

    // ── dry run ───────────────────────────────────────────────────────────────

    @Test
    void dryRun_writesNothingToAnyOfTheThreeTables() throws Exception {
        Fx f = plant("dry");
        Map<String, Map<String, String>> before = snapshot(f.tenant);

        repo.repairStaleNoteUris(f.tenant, false, 1000, 50);
        repo.repairStaleNoteUris(f.tenant, false, 1, 0);

        assertThat(snapshot(f.tenant)).isEqualTo(before);
        assertThat(before.get("aspects")).as("the planting seeded the satellites it checks").isNotEmpty();
        assertThat(before.get("highlights")).isNotEmpty();
    }

    // ── apply ─────────────────────────────────────────────────────────────────

    @Test
    void apply_rewritesTheCandidatesInAllThreeTables_andTouchesNothingElse() throws Exception {
        Fx f = plant("apply");
        Map<String, Map<String, String>> before = snapshot(f.tenant);

        Map<String, Object> r = repo.repairStaleNoteUris(f.tenant, true, 1000, 50);

        assertThat(r.get("apply")).isEqualTo(true);
        assertThat(r.get("rewritten")).isEqualTo(4);
        assertThat(r.get("remaining")).isEqualTo(0);
        assertThat(docUri(f.tenant, "a1")).isEqualTo(f.nx(f.y, "alpha"));
        assertThat(aspectUri(f.tenant, "a1")).as("the aspect row follows by its own derivation, not the catalog's")
            .isEqualTo(f.nx(f.y, "notes/alpha.md"));
        assertThat(highlightUri(f.tenant, "a1")).isEqualTo(f.nx(f.y, "alpha"));
        assertThat(docUri(f.tenant, "a2")).isEqualTo(f.nx(f.y, "beta"));
        assertThat(docUri(f.tenant, "c1")).isEqualTo(f.nx(f.y, "eps"));
        assertThat(docUri(f.tenant, "l1")).isEqualTo(f.nx(f.y, "legacy"));

        Map<String, Map<String, String>> after = snapshot(f.tenant);
        Map<String, String> docsBefore = new TreeMap<>(before.get("docs"));
        Map<String, String> docsAfter = new TreeMap<>(after.get("docs"));
        for (String rewritten : List.of("a1", "a2", "c1", "l1")) {
            assertThat(docsAfter.remove(rewritten)).isNotEqualTo(docsBefore.remove(rewritten));
        }
        assertThat(docsAfter).as("every other catalog row is byte-identical").isEqualTo(docsBefore);
        assertThat(after.get("aspects")).hasSize(1);
        assertThat(after.get("highlights")).hasSize(1);

        // the census now has nothing left to rewrite; the merge, refused and tombstoned classes are unchanged
        Map<String, Object> again = repo.repairStaleNoteUris(f.tenant, false, 1000, 50);
        assertThat(again.get("rewrite")).isEqualTo(0);
        assertThat(again.get("merge")).isEqualTo(3);
        assertThat(again.get("refused")).isEqualTo(2);
        assertThat(again.get("tombstoned")).isEqualTo(1);
    }

    @Test
    void apply_aReputUnderTheNotesCollectionFindsTheRewrittenRow() throws Exception {
        Fx f = plant("reput");

        repo.repairStaleNoteUris(f.tenant, true, 1000, 0);

        var found = repo.documentsBySourceUri(f.tenant, f.nx(f.y, "alpha"), 0, 0);
        assertThat(found).hasSize(1);
        assertThat(found.get(0).get("tumbler")).isEqualTo("a1");
        assertThat(repo.documentsBySourceUri(f.tenant, f.nx(f.x1, "alpha"), 0, 0)).isEmpty();
    }

    @Test
    void apply_isIdempotent() throws Exception {
        Fx f = plant("idem");
        repo.repairStaleNoteUris(f.tenant, true, 1000, 0);
        Map<String, Map<String, String>> once = snapshot(f.tenant);

        Map<String, Object> second = repo.repairStaleNoteUris(f.tenant, true, 1000, 0);

        assertThat(second.get("rewritten")).isEqualTo(0);
        assertThat(snapshot(f.tenant)).isEqualTo(once);
    }

    @Test
    void apply_limitBoundsOneCall_andRemainingSaysHowManyAreLeft() throws Exception {
        Fx f = plant("limit");

        Map<String, Object> first = repo.repairStaleNoteUris(f.tenant, true, 3, 0);
        assertThat(first.get("rewritten")).isEqualTo(3);
        assertThat(first.get("remaining")).isEqualTo(1);
        assertThat(first.get("rewrite")).as("the class count is taken before the call writes").isEqualTo(4);

        Map<String, Object> second = repo.repairStaleNoteUris(f.tenant, true, 3, 0);
        assertThat(second.get("rewritten")).isEqualTo(1);
        assertThat(second.get("remaining")).isEqualTo(0);
    }

    @Test
    void apply_aTwinThatAppearedSinceTheCensusIsSkippedNotClobbered() throws Exception {
        Fx f = plant("since");
        assertThat(repo.repairStaleNoteUris(f.tenant, false, 1000, 0).get("rewrite")).isEqualTo(4);
        seedNote(f.tenant, "late-twin", f.y, f.nx(f.y, "alpha"));   // a re-put under Y, after the census

        Map<String, Object> r = repo.repairStaleNoteUris(f.tenant, true, 1000, 50);

        assertThat(r.get("rewritten")).isEqualTo(3);
        assertThat(r.get("merge")).as("a1 is now a merge candidate").isEqualTo(4);
        assertThat(docUri(f.tenant, "a1")).isEqualTo(f.nx(f.x1, "alpha"));
        assertThat(aspectUri(f.tenant, "a1")).isEqualTo(f.nx(f.x1, "notes/alpha.md"));
        assertThat(docUri(f.tenant, "late-twin")).isEqualTo(f.nx(f.y, "alpha"));
    }

    // ── the concurrent twin ───────────────────────────────────────────────────

    @Test
    void apply_aTwinFoundWhenTheRowsAreLocked_is409AndNothingIsWritten() throws Exception {
        Fx f = plant("race1");
        Map<String, Map<String, String>> before = snapshot(f.tenant);
        List<String> stages = new java.util.concurrent.CopyOnWriteArrayList<>();
        CatalogRepository.setNoteUriRepairHookForTests(stage -> {
            stages.add(stage);
            if (stage.equals("locked")) seedNote(f.tenant, "race-twin", f.y, f.nx(f.y, "alpha"));
        });

        assertThatThrownBy(() -> repo.repairStaleNoteUris(f.tenant, true, 1000, 0))
            .isInstanceOfSatisfying(CatalogRepository.SourceUriCollision.class, e -> {
                assertThat(e.holder()).isEqualTo("race-twin");
                assertThat(e.getMessage()).contains(f.nx(f.y, "alpha"));
            });

        // The index would refuse the same twin at the UPDATE with the same holder and message, so the 409 alone
        // cannot tell the re-check from the index. Only the re-check stops the run BEFORE the "checked" stage.
        assertThat(stages).as("the run stopped at the holder re-check").containsExactly("locked");

        assertThat(withoutRaceTwin(snapshot(f.tenant), "race-twin")).as("all or nothing").isEqualTo(before);
    }

    @Test
    void apply_aTwinCommittedAfterTheCheck_isCaughtByTheIndex_as409AndNothingIsWritten() throws Exception {
        Fx f = plant("race2");
        Map<String, Map<String, String>> before = snapshot(f.tenant);
        List<String> stages = new java.util.concurrent.CopyOnWriteArrayList<>();
        CatalogRepository.setNoteUriRepairHookForTests(stage -> {
            stages.add(stage);
            if (stage.equals("checked")) seedNote(f.tenant, "race-twin", f.y, f.nx(f.y, "beta"));
        });

        assertThatThrownBy(() -> repo.repairStaleNoteUris(f.tenant, true, 1000, 0))
            .isInstanceOfSatisfying(CatalogRepository.SourceUriCollision.class, e -> {
                assertThat(e.holder()).isEqualTo("race-twin");
                assertThat(e.getMessage()).contains(f.nx(f.y, "beta"));
            });

        assertThat(stages).as("the re-check passed; the index caught the twin").containsExactly("locked", "checked");

        assertThat(withoutRaceTwin(snapshot(f.tenant), "race-twin")).as("all or nothing").isEqualTo(before);
    }

    // ── the ledger (the undo record of --apply) ───────────────────────────────

    @Test
    @SuppressWarnings("unchecked")
    void apply_returnsALedgerOfEveryRewrittenRow_inAllThreeTables() throws Exception {
        Fx f = plant("ledger");
        long aspectId = aspectRowId(f.tenant, "a1");
        long highlightId = highlightRowId(f.tenant, "a1");

        Map<String, Object> dry = repo.repairStaleNoteUris(f.tenant, false, 1000, 0);
        Map<String, Object> r = repo.repairStaleNoteUris(f.tenant, true, 1000, 0);

        assertThat(dry).as("a dry run rewrites nothing, so it has no ledger").doesNotContainKey("ledger");
        List<Map<String, Object>> ledger = (List<Map<String, Object>>) r.get("ledger");
        assertThat(ledger).hasSize(6);
        assertThat(ledgerRows(ledger, "catalog_documents")).extracting(m -> (String) m.get("key"))
            .containsExactlyInAnyOrder("a1", "a2", "c1", "l1");
        Map<String, Object> cat = ledgerRows(ledger, "catalog_documents").stream()
            .filter(m -> "a1".equals(m.get("key"))).findFirst().orElseThrow();
        assertThat(cat.get("old_uri")).isEqualTo(f.nx(f.x1, "alpha"));
        assertThat(cat.get("new_uri")).isEqualTo(f.nx(f.y, "alpha"));
        assertThat(ledgerRows(ledger, "catalog_documents").stream().filter(m -> "l1".equals(m.get("key"))).findFirst()
            .orElseThrow().get("old_uri")).as("the old URI is recorded as it was, legacy spelling and all")
            .isEqualTo("chroma://" + f.x1 + "/legacy");
        Map<String, Object> asp = ledgerRows(ledger, "document_aspects").get(0);
        assertThat(asp.get("key")).isEqualTo(Long.toString(aspectId));
        assertThat(asp.get("doc_id")).isEqualTo("a1");
        assertThat(asp.get("old_uri")).isEqualTo(f.nx(f.x1, "notes/alpha.md"));
        assertThat(asp.get("new_uri")).isEqualTo(f.nx(f.y, "notes/alpha.md"));
        Map<String, Object> hl = ledgerRows(ledger, "document_highlights").get(0);
        assertThat(hl.get("key")).isEqualTo(Long.toString(highlightId));
        assertThat(hl.get("old_uri")).isEqualTo(f.nx(f.x1, "alpha"));
        assertThat(hl.get("new_uri")).isEqualTo(f.nx(f.y, "alpha"));
    }

    @Test
    @SuppressWarnings("unchecked")
    void apply_theLedgerIsBoundedByTheBatch() throws Exception {
        Fx f = plant("ledger-limit");

        Map<String, Object> r = repo.repairStaleNoteUris(f.tenant, true, 2, 0);

        List<Map<String, Object>> ledger = (List<Map<String, Object>>) r.get("ledger");
        assertThat(ledgerRows(ledger, "catalog_documents")).as("one entry per rewritten catalog row").hasSize(2);
        assertThat(ledger.size()).isLessThanOrEqualTo(2 * 3);
        assertThat(r.get("rewritten")).isEqualTo(2);
    }

    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> ledgerRows(List<Map<String, Object>> ledger, String table) {
        return ledger.stream().filter(m -> table.equals(m.get("table"))).toList();
    }

    private long aspectRowId(String tenant, String docId) {
        return tenantScope.withTenant(tenant, ctx -> ctx.select(DOCUMENT_ASPECTS.ID)
            .from(DOCUMENT_ASPECTS).where(DOCUMENT_ASPECTS.DOC_ID.eq(docId)).fetchOne(0, Long.class));
    }

    private long highlightRowId(String tenant, String docId) {
        return tenantScope.withTenant(tenant, ctx -> ctx.select(DOCUMENT_HIGHLIGHTS.ID)
            .from(DOCUMENT_HIGHLIGHTS).where(DOCUMENT_HIGHLIGHTS.DOC_ID.eq(docId)).fetchOne(0, Long.class));
    }

    // ── satellites without a document ─────────────────────────────────────────

    @Test
    void aSatelliteWithABlankDocIdCannotExist_soTheRepairNeedsNoFallbackForIt() throws Exception {
        // Gate review r1 asked for a fallback for the legacy knowledge-note aspect rows whose doc_id is blank
        // (aspects-004). hygiene-001-1 deleted every row with a NULL doc_id and set NOT NULL, and fk-001 puts a
        // composite foreign key on (tenant_id, doc_id): '' names no catalog row (23503), NULL is refused (23502).
        // If this ever stops failing, the repair must claim such rows (by the old catalog URI) and count them.
        Fx f = new Fx("blankdoc");
        seedNote(f.tenant, "b1", f.y, f.nx(f.x1, "bt"));
        seedAspect(f.tenant, "b1", f.y, "p-b1", f.nx(f.x1, "bt"));
        seedHighlight(f.tenant, "b1", f.y, f.nx(f.x1, "bt"));

        assertThatThrownBy(() -> blankDocId(f.tenant, "b1")).hasMessageContaining("violates foreign key constraint");
    }

    // ── a blank physical collection ───────────────────────────────────────────

    @Test
    void census_aNoteRowWithABlankPhysicalCollectionIsRefusedAsMalformed_andNeverRewritten() throws Exception {
        Fx f = new Fx("blankcoll");
        seedNote(f.tenant, "nc", f.y, f.nx(f.x1, "nc"));
        setPhysicalCollection(f.tenant, "nc", "");

        Map<String, Object> r = repo.repairStaleNoteUris(f.tenant, true, 1000, 5);

        assertThat(r.get("refused_malformed")).isEqualTo(1);
        assertThat(r.get("rewrite")).isEqualTo(0);
        assertThat(r.get("rewritten")).isEqualTo(0);
        assertThat(docUri(f.tenant, "nc")).isEqualTo(f.nx(f.x1, "nc"));
        assertThat(tumblers(sampleOf(r).get("refused"))).containsExactly("nc");
    }

    // ── chains and the chroma:// twin ─────────────────────────────────────────

    @Test
    void apply_aChainResolvesOnTheNextCall() throws Exception {
        Fx f = new Fx("chain");
        String t = f.tenant;
        seedNote(t, "cb", f.x2, f.nx(f.y, "ct"));   // stale B: lives in x2, names y; its target nx(x2, ct) is free
        seedNote(t, "ca", f.y, f.nx(f.x1, "ct"));   // stale A: its target nx(y, ct) is held by B

        Map<String, Object> first = repo.repairStaleNoteUris(t, false, 1000, 5);
        assertThat(first.get("rewrite")).isEqualTo(1);
        assertThat(tumblers(sampleOf(first).get("rewrite"))).containsExactly("cb");
        assertThat(tumblers(sampleOf(first).get("merge"))).containsExactly("ca");

        assertThat(repo.repairStaleNoteUris(t, true, 1000, 0).get("rewritten")).isEqualTo(1);
        assertThat(docUri(t, "cb")).isEqualTo(f.nx(f.x2, "ct"));
        assertThat(docUri(t, "ca")).isEqualTo(f.nx(f.x1, "ct"));

        Map<String, Object> second = repo.repairStaleNoteUris(t, false, 1000, 5);
        assertThat(second.get("rewrite")).as("B's move freed A's target").isEqualTo(1);
        assertThat(second.get("merge")).isEqualTo(0);
        assertThat(repo.repairStaleNoteUris(t, true, 1000, 0).get("rewritten")).isEqualTo(1);
        assertThat(docUri(t, "ca")).isEqualTo(f.nx(f.y, "ct"));
    }

    @Test
    void census_aLiveChromaSpelledTwinIsATwin_andApplyDoesNotMintTheDuplicate() throws Exception {
        Fx f = new Fx("chromatwin");
        String t = f.tenant;
        seedNote(t, "ch-twin", f.y, "chroma://" + f.y + "/legacy-twin");
        seedNote(t, "ch-stale", f.y, f.nx(f.x1, "legacy-twin"));

        Map<String, Object> r = repo.repairStaleNoteUris(t, true, 1000, 5);

        assertThat(r.get("merge_twin")).isEqualTo(1);
        assertThat(r.get("rewrite")).isEqualTo(0);
        assertThat(r.get("rewritten")).isEqualTo(0);
        assertThat(byTumbler(sampleOf(r).get("merge"), "ch-stale").get("twin")).isEqualTo("ch-twin");
        assertThat(docUri(t, "ch-stale")).isEqualTo(f.nx(f.x1, "legacy-twin"));
    }

    // ── one transaction across the three tables ───────────────────────────────

    @Test
    void apply_aFailureAfterTheCatalogUpdate_leavesAllThreeTablesUnchanged() throws Exception {
        for (String stage : List.of("catalog_updated", "written")) {
            Fx f = plant("rollback-" + stage);
            Map<String, Map<String, String>> before = snapshot(f.tenant);
            List<String> stages = new java.util.concurrent.CopyOnWriteArrayList<>();
            CatalogRepository.setNoteUriRepairHookForTests(s -> {
                stages.add(s);
                if (s.equals(stage)) throw new IllegalStateException("injected failure after " + stage);
            });

            assertThatThrownBy(() -> repo.repairStaleNoteUris(f.tenant, true, 1000, 0))
                .as(stage).hasStackTraceContaining("injected failure after " + stage);

            assertThat(stages).as(stage).endsWith(stage);
            assertThat(snapshot(f.tenant)).as(stage + ": catalog, aspects and highlights all roll back").isEqualTo(before);
            assertThat(before.get("aspects")).isNotEmpty();
            assertThat(before.get("highlights")).isNotEmpty();
        }
    }

    // ── the merge entries carry what the recommended rule needs ───────────────

    @Test
    void census_mergeEntriesCarryChunkCountIndexedAtAndSatellitePresence_forTheRowAndItsTwin() throws Exception {
        Fx f = plant("mergefields");
        String t = f.tenant;
        setChunkCount(t, "m1", 3);
        setIndexedAt(t, "m1", "2026-10-01T00:00:00Z");
        setIndexedAt(t, "t1", "2026-11-01T00:00:00Z");
        seedAspect(t, "m1", f.y, "notes/m1.md", f.nx(f.x1, "notes/m1.md"));
        seedHighlight(t, "t1", f.y, f.nx(f.y, "gamma"));

        Map<String, Object> r = repo.repairStaleNoteUris(t, false, 1000, 10);

        Map<String, Object> m1 = byTumbler(sampleOf(r).get("merge"), "m1");
        assertThat(m1.get("twin")).isEqualTo("t1");
        assertThat(m1.get("chunk_count")).isEqualTo(3);
        assertThat(m1.get("twin_chunk_count")).isEqualTo(0);
        assertThat(m1.get("has_aspects")).isEqualTo(true);
        assertThat(m1.get("twin_has_aspects")).isEqualTo(false);
        assertThat(m1.get("has_highlights")).isEqualTo(false);
        assertThat(m1.get("twin_has_highlights")).isEqualTo(true);
        assertThat(java.time.OffsetDateTime.parse((String) m1.get("indexed_at")).toInstant())
            .isEqualTo(java.time.Instant.parse("2026-10-01T00:00:00Z"));
        assertThat(java.time.OffsetDateTime.parse((String) m1.get("twin_indexed_at")).toInstant())
            .isEqualTo(java.time.Instant.parse("2026-11-01T00:00:00Z"));
        Map<String, Object> s1 = byTumbler(sampleOf(r).get("merge"), "s1");
        assertThat(s1.get("twin")).isEqualTo("s2");
        assertThat(s1).containsKeys("chunk_count", "twin_chunk_count", "indexed_at", "twin_indexed_at",
            "has_aspects", "twin_has_aspects", "has_highlights", "twin_has_highlights");
    }

    // ── HTTP ──────────────────────────────────────────────────────────────────

    @Test
    void http_dryRunReportsTheCensus_andApplyRewrites() throws Exception {
        Fx f = plant("http");

        var dry = post(f, "{}");
        assertThat(dry.statusCode()).as(dry.body()).isEqualTo(200);
        Map<String, Object> body = mapper.readValue(dry.body(), MAP_T);
        assertThat(body.get("apply")).isEqualTo(false);
        assertThat(body.get("rewrite")).isEqualTo(4);
        assertThat(body.get("merge")).isEqualTo(3);
        assertThat(docUri(f.tenant, "a1")).isEqualTo(f.nx(f.x1, "alpha"));

        var applied = post(f, "{\"apply\":true,\"limit\":2,\"sample\":0}");
        assertThat(applied.statusCode()).as(applied.body()).isEqualTo(200);
        Map<String, Object> done = mapper.readValue(applied.body(), MAP_T);
        assertThat(done.get("rewritten")).isEqualTo(2);
        assertThat(done.get("remaining")).isEqualTo(2);
    }

    @Test
    void http_aConcurrentTwinIs409NamingTheHolder_notA500() throws Exception {
        Fx f = plant("http409");
        CatalogRepository.setNoteUriRepairHookForTests(stage -> {
            if (stage.equals("checked")) seedNote(f.tenant, "race-twin", f.y, f.nx(f.y, "alpha"));
        });

        var resp = post(f, "{\"apply\":true}");

        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(409);
        assertThat((String) mapper.readValue(resp.body(), MAP_T).get("error"))
            .contains("would rewrite a document's source_uri to " + f.nx(f.y, "alpha"))
            .contains("live document race-twin ");
        assertThat(docUri(f.tenant, "a1")).isEqualTo(f.nx(f.x1, "alpha"));
    }

    @Test
    void http_badArguments_are400_andGetIs405() throws Exception {
        Fx f = new Fx("http400");
        for (String bad : List.of("{\"apply\":\"yes\"}", "{\"limit\":0}", "{\"limit\":5001}", "{\"limit\":\"9\"}",
                "{\"sample\":-1}", "{\"sample\":501}")) {
            assertThat(post(f, bad).statusCode()).as(bad).isEqualTo(400);
        }
        var get = http.send(TestHttp.request(url("/v1/catalog/note_uri_repair"))
            .header("Authorization", "Bearer " + f.token).GET().build(), HttpResponse.BodyHandlers.ofString());
        assertThat(get.statusCode()).isEqualTo(405);
    }

    // ── helpers ───────────────────────────────────────────────────────────────

    private HttpResponse<String> post(Fx f, String json) throws Exception {
        return http.send(TestHttp.request(url("/v1/catalog/note_uri_repair"))
            .header("Authorization", "Bearer " + f.token)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(json)).build(), HttpResponse.BodyHandlers.ofString());
    }

    private String url(String path) {
        return "http://127.0.0.1:" + service.getPort() + path;
    }

    @SuppressWarnings("unchecked")
    private static Map<String, List<Map<String, Object>>> sampleOf(Map<String, Object> r) {
        return (Map<String, List<Map<String, Object>>>) r.get("sample");
    }

    private static List<String> tumblers(List<Map<String, Object>> rows) {
        return rows.stream().map(m -> (String) m.get("tumbler")).toList();
    }

    private static Map<String, Object> byTumbler(List<Map<String, Object>> rows, String tumbler) {
        return rows.stream().filter(m -> tumbler.equals(m.get("tumbler"))).findFirst().orElseThrow();
    }

    private static String coll(String slug) {
        return "knowledge__" + slug + "__minilm-l6-v2-384__v1";
    }

    private void seedNote(String tenant, String tumbler, String collection, String uri) {
        Map<String, Object> d = new HashMap<>(Map.of(
            "tumbler", tumbler, "title", tumbler, "content_type", "knowledge",
            "corpus", "knowledge", "physical_collection", collection));
        if (uri.startsWith("chroma://")) {
            // As the backfill finds it: written below the repository, which stores the nx-note:// spelling.
            repo.upsertDocument(tenant, d);
            try {
                setUri(tenant, tumbler, uri);
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
            return;
        }
        d.put("source_uri", uri);
        repo.upsertDocument(tenant, d);
    }

    private void seedAspect(String tenant, String docId, String collection, String sourcePath, String uri) {
        Map<String, Object> body = new HashMap<>();
        body.put("doc_id", docId);
        body.put("collection", collection);
        body.put("source_path", sourcePath);
        body.put("source_uri", uri);
        body.put("extracted_at", TS);
        body.put("model_version", "test");
        body.put("extractor_name", "test");
        body.put("confidence", 0.9);
        aspectRepo.upsertAspect(tenant, body);
    }

    private void seedHighlight(String tenant, String docId, String collection, String uri) {
        aspectRepo.upsertHighlight(tenant, Map.of(
            "doc_id", docId, "collection", collection, "source_uri", uri,
            "highlights_md", "- a highlight", "ingested_at", TS));
    }

    private void tombstone(String tenant, String tumbler) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.DELETED_AT, java.time.OffsetDateTime.now())
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler)))
                .execute();
        }
    }

    private void setUri(String tenant, String tumbler, String uri) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.SOURCE_URI, uri)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler)))
                .execute();
        }
    }

    private void blankDocId(String tenant, String... docIds) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            for (String d : docIds) {
                dsl.update(DOCUMENT_ASPECTS).set(DOCUMENT_ASPECTS.DOC_ID, "")
                    .where(DOCUMENT_ASPECTS.TENANT_ID.eq(tenant).and(DOCUMENT_ASPECTS.DOC_ID.eq(d))).execute();
                dsl.update(DOCUMENT_HIGHLIGHTS).set(DOCUMENT_HIGHLIGHTS.DOC_ID, "")
                    .where(DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(tenant).and(DOCUMENT_HIGHLIGHTS.DOC_ID.eq(d))).execute();
            }
        }
    }

    private void setPhysicalCollection(String tenant, String tumbler, String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, collection)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler)))
                .execute();
        }
    }

    private void setIndexedAt(String tenant, String tumbler, String iso) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.INDEXED_AT, java.time.OffsetDateTime.parse(iso))
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler)))
                .execute();
        }
    }

    private void setChunkCount(String tenant, String tumbler, int n) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.CHUNK_COUNT, n)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler)))
                .execute();
        }
    }

    private String docUri(String tenant, String tumbler) {
        return tenantScope.withTenant(tenant, ctx -> ctx.select(CATALOG_DOCUMENTS.SOURCE_URI)
            .from(CATALOG_DOCUMENTS).where(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler)).fetchOne(0, String.class));
    }

    private String aspectUri(String tenant, String docId) {
        return tenantScope.withTenant(tenant, ctx -> ctx.select(DOCUMENT_ASPECTS.SOURCE_URI)
            .from(DOCUMENT_ASPECTS).where(DOCUMENT_ASPECTS.DOC_ID.eq(docId)).fetchOne(0, String.class));
    }

    private String highlightUri(String tenant, String docId) {
        return tenantScope.withTenant(tenant, ctx -> ctx.select(DOCUMENT_HIGHLIGHTS.SOURCE_URI)
            .from(DOCUMENT_HIGHLIGHTS).where(DOCUMENT_HIGHLIGHTS.DOC_ID.eq(docId)).fetchOne(0, String.class));
    }

    /** Every row's (key -> source_uri|deleted) in the three tables, for before/after equality. */
    private Map<String, Map<String, String>> snapshot(String tenant) {
        return tenantScope.withTenant(tenant, ctx -> {
            Map<String, Map<String, String>> out = new LinkedHashMap<>();
            Map<String, String> docs = new TreeMap<>();
            for (var r : ctx.select(CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.SOURCE_URI,
                    CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.DELETED_AT)
                    .from(CATALOG_DOCUMENTS).where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).fetch()) {
                docs.put(r.value1(), r.value2() + "|" + r.value3() + "|" + (r.value4() != null));
            }
            Map<String, String> aspects = new TreeMap<>();
            for (var r : ctx.select(DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.SOURCE_URI, DOCUMENT_ASPECTS.COLLECTION)
                    .from(DOCUMENT_ASPECTS).where(DOCUMENT_ASPECTS.TENANT_ID.eq(tenant)).fetch()) {
                aspects.put(r.value1(), r.value2() + "|" + r.value3());
            }
            Map<String, String> highlights = new TreeMap<>();
            for (var r : ctx.select(DOCUMENT_HIGHLIGHTS.DOC_ID, DOCUMENT_HIGHLIGHTS.SOURCE_URI, DOCUMENT_HIGHLIGHTS.COLLECTION)
                    .from(DOCUMENT_HIGHLIGHTS).where(DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(tenant)).fetch()) {
                highlights.put(r.value1(), r.value2() + "|" + r.value3());
            }
            out.put("docs", docs);
            out.put("aspects", aspects);
            out.put("highlights", highlights);
            return out;
        });
    }

    private static Map<String, Map<String, String>> withoutRaceTwin(Map<String, Map<String, String>> snap, String twin) {
        Map<String, Map<String, String>> out = new LinkedHashMap<>(snap);
        Map<String, String> docs = new TreeMap<>(out.get("docs"));
        docs.remove(twin);
        out.put("docs", docs);
        return out;
    }
}
