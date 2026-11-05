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
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-rfvyy.6.4 (RDR-243 Phase 1 Step 2b): every engine write to {@code catalog_documents} keeps
 * {@code sensitive} one-way. Upsert and both import paths OR the stored and the incoming value; a
 * re-registration of a live document raises the flag and never lowers it; a new tumbler inherits the flag
 * from a sensitive tombstoned predecessor; a merge leaves the canonical sensitive when either side was;
 * rename keeps the row; the general update route still refuses the column.
 *
 * <p>Runs on PostgreSQL as the NOBYPASSRLS service role, so row-level security applies. The flag and the
 * audit rows are read back over a superuser connection with plain SQL, so the test does not depend on the
 * generated jOOQ symbols for the column it is pinning.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogDocumentSensitivityWriteRulesTest {

    private static final String SVC_ROLE = "svc_sens_write_rules";
    private static final String SVC_PASS = "svc_sens_write_rules_pass";
    private static final String OWNER = "48";
    private static final String COLL_OLD = "knowledge__sensrules__minilm-l6-v2-384__v1";
    private static final String COLL_NEW = "knowledge__sensrules__minilm-l6-v2-384__v2";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    CatalogRepository repo;
    int tenantSeq = 0;

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

    /** A fresh tenant per test, so no test reads another's rows. */
    private String tenant() {
        return "sensrules-" + (++tenantSeq);
    }

    private Map<String, Object> fields(String title, String sourceUri, String filePath, Boolean sensitive) {
        var f = new LinkedHashMap<String, Object>();
        f.put("title", title);
        f.put("file_path", filePath);
        f.put("source_uri", sourceUri);
        f.put("physical_collection", COLL_OLD);
        if (sensitive != null) f.put("sensitive", sensitive);
        return f;
    }

    // ── reading back ────────────────────────────────────────────────────

    private boolean flag(String tenant, String tumbler) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var r = DSL.using(su, SQLDialect.POSTGRES).fetchOne(
                "SELECT sensitive FROM nexus.catalog_documents WHERE tenant_id = ? AND tumbler = ?", tenant, tumbler);
            assertThat(r).as("row %s/%s exists", tenant, tumbler).isNotNull();
            return r.get(0, Boolean.class);
        }
    }

    /** Audit rows of one tenant as {@code tumbler|old|new|reason|actor}, oldest first. */
    private List<String> audit(String tenant) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var out = new ArrayList<String>();
            for (var r : DSL.using(su, SQLDialect.POSTGRES).fetch(
                    "SELECT tumbler, old, new, reason, actor FROM nexus.catalog_sensitivity_changes "
                        + "WHERE tenant_id = ? ORDER BY id", tenant)) {
                out.add(r.get(0) + "|" + r.get(1) + "|" + r.get(2) + "|" + r.get(3) + "|" + r.get(4));
            }
            return out;
        }
    }

    private void setFlagDirect(String tenant, String tumbler, boolean v) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).execute(
                "UPDATE nexus.catalog_documents SET sensitive = ? WHERE tenant_id = ? AND tumbler = ?",
                v, tenant, tumbler);
        }
    }

    private Map<String, Object> importRow(String tumbler, String title, Boolean sensitive) {
        var d = new LinkedHashMap<String, Object>();
        d.put("tumbler", tumbler);
        d.put("title", title);
        d.put("physical_collection", COLL_OLD);
        d.put("content_type", "prose");
        d.put("corpus", "knowledge");
        if (sensitive != null) d.put("sensitive", sensitive);
        return d;
    }

    // ── register (single) ───────────────────────────────────────────────

    @Test
    void register_withoutTheFlag_keepsItTrue_onTheSourceUriMatch_andOnTheFilePathMatch() throws Exception {
        String t = tenant();
        String byUri = repo.registerDocument(t, OWNER, fields("a", "file:///r/a.md", "a.md", true));
        assertThat(flag(t, byUri)).as("registered with the flag").isTrue();
        // same source_uri, no flag
        assertThat(repo.registerDocument(t, OWNER, fields("a", "file:///r/a.md", "a.md", null))).isEqualTo(byUri);
        assertThat(flag(t, byUri)).as("source_uri match, request without the flag").isTrue();
        // same source_uri, an explicit false must not lower it either
        assertThat(repo.registerDocument(t, OWNER, fields("a", "file:///r/a.md", "a.md", false))).isEqualTo(byUri);
        assertThat(flag(t, byUri)).as("source_uri match, request with sensitive=false").isTrue();

        // the file_path + owner-prefix match (no source_uri on the request, none derived for a bare name)
        String byPath = repo.registerDocument(t, OWNER, fields("p", "", "only-by-path.md", true));
        assertThat(flag(t, byPath)).isTrue();
        assertThat(repo.registerDocument(t, OWNER, fields("p", "", "only-by-path.md", null))).isEqualTo(byPath);
        assertThat(flag(t, byPath)).as("file_path match, request without the flag").isTrue();
        assertThat(audit(t)).as("neither lowering attempt wrote an audit row").isEmpty();
    }

    @Test
    void register_liveDocumentWithTheFlag_raisesItOnce_andWritesOneAuditRow() throws Exception {
        String t = tenant();
        String byUri = repo.registerDocument(t, OWNER, fields("u", "file:///r/u.md", "u.md", null));
        String byPath = repo.registerDocument(t, OWNER, fields("p", "", "p-only.md", null));
        assertThat(flag(t, byUri)).isFalse();
        assertThat(flag(t, byPath)).isFalse();

        assertThat(repo.registerDocument(t, OWNER, fields("u", "file:///r/u.md", "u.md", true))).isEqualTo(byUri);
        assertThat(repo.registerDocument(t, OWNER, fields("p", "", "p-only.md", true))).isEqualTo(byPath);
        assertThat(flag(t, byUri)).as("raised by the source_uri match").isTrue();
        assertThat(flag(t, byPath)).as("raised by the file_path match").isTrue();
        assertThat(audit(t)).hasSize(2)
            .containsExactlyInAnyOrder(byUri + "|false|true|register_raise|engine",
                byPath + "|false|true|register_raise|engine");

        // a second flagged registration is a no-op: no further audit row
        repo.registerDocument(t, OWNER, fields("u", "file:///r/u.md", "u.md", true));
        assertThat(audit(t)).as("WHERE NOT sensitive: the second raise changes nothing").hasSize(2);
    }

    // ── register (batch) ────────────────────────────────────────────────

    @Test
    void registerMany_keepsTheFlag_raisesIt_andInheritsInOneBatch() throws Exception {
        String t = tenant();
        String keep = repo.registerDocument(t, OWNER, fields("k", "file:///r/k.md", "k.md", true));
        String raise = repo.registerDocument(t, OWNER, fields("r", "file:///r/r.md", "r.md", null));
        String dead = repo.registerDocument(t, OWNER, fields("d", "file:///r/d.md", "d.md", true));
        repo.deleteDocument(t, dead);

        List<Map<String, Object>> docs = List.of(
            fields("k", "file:///r/k.md", "k.md", null),      // live and flagged, request without the flag
            fields("r", "file:///r/r.md", "r.md", true),      // live and plain, request raises
            fields("d", "file:///r/d.md", "d.md", null),      // tombstoned sensitive predecessor: new tumbler inherits
            fields("n", "file:///r/n.md", "n.md", null),      // brand new and plain
            fields("f", "file:///r/f.md", "f.md", true));     // brand new and flagged
        List<String> out = repo.registerDocumentMany(t, OWNER, docs);

        assertThat(out.get(0)).isEqualTo(keep);
        assertThat(out.get(1)).isEqualTo(raise);
        assertThat(out.get(2)).as("a new tumbler for the tombstoned source").isNotEqualTo(dead);
        assertThat(flag(t, out.get(0))).as("kept").isTrue();
        assertThat(flag(t, out.get(1))).as("raised").isTrue();
        assertThat(flag(t, out.get(2))).as("inherited from the tombstoned predecessor").isTrue();
        assertThat(flag(t, out.get(3))).as("new and plain").isFalse();
        assertThat(flag(t, out.get(4))).as("new and flagged").isTrue();
        assertThat(audit(t))
            .anyMatch(r -> r.equals(raise + "|false|true|register_raise|engine"))
            .anyMatch(r -> r.startsWith(out.get(2) + "|false|true|register_inherit") && r.contains(dead)
                && r.endsWith("|engine"))
            .hasSize(2);

        // the same batch again: nothing new to raise or inherit
        repo.registerDocumentMany(t, OWNER, docs);
        assertThat(audit(t)).as("a repeated batch writes no audit row").hasSize(2);
    }

    // ── tombstone then re-index ─────────────────────────────────────────

    @Test
    void tombstoneThenReindexWithoutTheFlag_theNewTumblerIsSensitive_andTheAuditRowNamesThePredecessor() throws Exception {
        String t = tenant();
        String first = repo.registerDocument(t, OWNER, fields("x", "file:///r/x.md", "x.md", true));
        repo.deleteDocument(t, first);

        var outcome = repo.registerDocumentWithOutcome(t, OWNER, fields("x", "file:///r/x.md", "x.md", null));
        assertThat(outcome.created()).isTrue();
        assertThat(outcome.tumbler()).isNotEqualTo(first);
        assertThat(flag(t, outcome.tumbler())).as("delete-then-re-index keeps the protection").isTrue();
        assertThat(audit(t)).hasSize(1);
        assertThat(audit(t).get(0)).startsWith(outcome.tumbler() + "|false|true|register_inherit")
            .contains(first).endsWith("|engine");
    }

    @Test
    void tombstoneThenReindex_aPlainPredecessorLeavesTheNewTumblerPlain_andTheMostRecentOneDecides() throws Exception {
        String t = tenant();
        String plain = repo.registerDocument(t, OWNER, fields("y", "file:///r/y.md", "y.md", null));
        repo.deleteDocument(t, plain);
        String again = repo.registerDocument(t, OWNER, fields("y", "file:///r/y.md", "y.md", null));
        assertThat(flag(t, again)).as("plain predecessor").isFalse();

        // a sensitive tombstone is older than a plain one: the most recent tombstoned row decides
        String t2 = tenant();
        String old = repo.registerDocument(t2, OWNER, fields("z", "file:///r/z.md", "z.md", true));
        repo.deleteDocument(t2, old);
        String mid = repo.registerDocument(t2, OWNER, fields("z", "file:///r/z.md", "z.md", null));
        assertThat(flag(t2, mid)).as("inherited from the first tombstone").isTrue();
        setFlagDirect(t2, mid, false);               // as if the audited route cleared it
        repo.deleteDocument(t2, mid);
        String last = repo.registerDocument(t2, OWNER, fields("z", "file:///r/z.md", "z.md", null));
        assertThat(flag(t2, last)).as("the most recent tombstoned row (cleared) is the predecessor").isFalse();
    }

    // ── upsert and import ───────────────────────────────────────────────

    @Test
    void upsertDocument_orsTheFlag_includingThroughTheUnTombstoneArm() throws Exception {
        String t = tenant();
        repo.upsertDocument(t, importRow("48.1", "u1", true));
        assertThat(audit(t)).as("a fresh row born flagged has no old value to audit here").isEmpty();
        repo.upsertDocument(t, importRow("48.1", "u1 again", false));
        assertThat(flag(t, "48.1")).as("false over true stays true").isTrue();
        repo.upsertDocument(t, importRow("48.1", "u1 once more", null));
        assertThat(flag(t, "48.1")).as("absent over true stays true").isTrue();

        repo.deleteDocument(t, "48.1");
        repo.upsertDocument(t, importRow("48.1", "u1 resurrected", false));
        assertThat(flag(t, "48.1")).as("the un-tombstone arm keeps the old row's flag").isTrue();
        repo.upsertDocument(t, importRow("48.1", "u1 flagged again", true));
        assertThat(audit(t)).as("true over true changes nothing, so nothing is audited").isEmpty();

        repo.upsertDocument(t, importRow("48.2", "u2", false));
        assertThat(flag(t, "48.2")).isFalse();
        assertThat(audit(t)).as("a plain upsert raises nothing").isEmpty();
        repo.upsertDocument(t, importRow("48.2", "u2", true));
        assertThat(flag(t, "48.2")).as("true over false raises").isTrue();
        assertThat(audit(t)).as("the raise is audited (false to true, reason upsert_raise, actor engine)")
            .containsExactly("48.2|false|true|upsert_raise|engine");
        repo.upsertDocument(t, importRow("48.2", "u2", true));
        assertThat(audit(t)).as("raising an already-raised row writes no second row").hasSize(1);

        // a tombstoned plain row raised through the un-tombstone arm is a false-to-true change too
        repo.upsertDocument(t, importRow("48.3", "u3", false));
        repo.deleteDocument(t, "48.3");
        repo.upsertDocument(t, importRow("48.3", "u3 back", true));
        assertThat(flag(t, "48.3")).isTrue();
        assertThat(audit(t)).containsExactly("48.2|false|true|upsert_raise|engine", "48.3|false|true|upsert_raise|engine");
    }

    @Test
    void importDocument_andImportDocumentsBatch_orTheFlag_andAuditTheRaise() throws Exception {
        String t = tenant();
        repo.importDocument(t, importRow("48.10", "i1", true));
        repo.importDocument(t, importRow("48.10", "i1", false));
        assertThat(flag(t, "48.10")).as("importDocument: false over true").isTrue();
        assertThat(audit(t)).as("fresh flagged row and a lowering attempt write nothing").isEmpty();
        repo.importDocument(t, importRow("48.11", "i2", false));
        repo.importDocument(t, importRow("48.11", "i2", true));
        assertThat(flag(t, "48.11")).as("importDocument: true over false").isTrue();
        assertThat(audit(t)).containsExactly("48.11|false|true|import_raise|engine");

        repo.importDocumentsBatch(t, List.of(
            importRow("48.20", "b1", true),
            importRow("48.21", "b2", false),
            importRow("48.22", "b3", null)));
        assertThat(audit(t)).as("a first batch creates rows; nothing to audit").hasSize(1);
        repo.importDocumentsBatch(t, List.of(
            importRow("48.20", "b1", false),
            importRow("48.21", "b2", true),
            importRow("48.22", "b3", null)));
        assertThat(flag(t, "48.20")).as("batch: false over true").isTrue();
        assertThat(flag(t, "48.21")).as("batch: true over false").isTrue();
        assertThat(flag(t, "48.22")).as("batch: absent over plain").isFalse();
        assertThat(audit(t)).as("only 48.21 changed in the batch")
            .containsExactly("48.11|false|true|import_raise|engine", "48.21|false|true|import_raise|engine");
        repo.importDocumentsBatch(t, List.of(importRow("48.21", "b2", true)));
        assertThat(audit(t)).as("a repeat raise writes nothing").hasSize(2);
    }

    // ── the intra-batch alias and the race-lost raises ──────────────────

    @Test
    void registerMany_anIntraBatchAlias_foldsItsFlagIntoTheRowThatInserts() throws Exception {
        String t = tenant();
        // the FIRST entry for the uri is plain, the LATER duplicate asks for the flag
        List<String> laterFlagged = repo.registerDocumentMany(t, OWNER, List.of(
            fields("a", "file:///r/alias1.md", "alias1.md", null),
            fields("a2", "file:///r/alias1.md", "alias1-dup.md", true)));
        assertThat(laterFlagged.get(1)).as("the duplicate aliases to the first entry's tumbler").isEqualTo(laterFlagged.get(0));
        assertThat(flag(t, laterFlagged.get(0))).as("the alias's request reaches the row that was inserted").isTrue();

        // the FIRST entry asks, the later duplicate is plain: the row stays flagged
        List<String> firstFlagged = repo.registerDocumentMany(t, OWNER, List.of(
            fields("b", "file:///r/alias2.md", "alias2.md", true),
            fields("b2", "file:///r/alias2.md", "alias2-dup.md", null)));
        assertThat(flag(t, firstFlagged.get(0))).isTrue();
        assertThat(audit(t)).as("both rows were born flagged: nothing changed on an existing row").isEmpty();
    }

    @Test
    void register_singlePath_raceLost_raisesTheWinnerAndAuditsIt() throws Exception {
        String t = tenant();
        String uri = "file:///r/race-single.md";
        String winner = "48.9001";
        String got = withRacingWinner(t, winner, uri, () ->
            repo.registerDocument(t, OWNER, fields("loser", uri, "race-single.md", true)));
        assertThat(got).as("the loser returns the winner's tumbler").isEqualTo(winner);
        assertThat(flag(t, winner)).as("the flagged request raised the live row it lost to").isTrue();
        assertThat(audit(t)).containsExactly(winner + "|false|true|register_raise|engine");
    }

    @Test
    void registerMany_raceLost_raisesTheWinnerAndAuditsIt() throws Exception {
        String t = tenant();
        String uri = "file:///r/race-batch.md";
        String winner = "48.9002";
        List<String> got = withRacingWinner(t, winner, uri, () -> repo.registerDocumentMany(t, OWNER, List.of(
            fields("loser", uri, "race-batch.md", true),
            fields("other", "file:///r/race-batch-other.md", "race-batch-other.md", null))));
        assertThat(got.get(0)).as("the batch's racing entry resolves to the winner").isEqualTo(winner);
        assertThat(flag(t, winner)).as("the flagged request raised the live row it lost to").isTrue();
        assertThat(flag(t, got.get(1))).as("the other, plain, entry was inserted plain").isFalse();
        assertThat(audit(t)).containsExactly(winner + "|false|true|register_raise|engine");
    }

    /**
     * Makes {@code loser} lose the cross-transaction race on {@code uri} deterministically: a second
     * transaction inserts a live row for the uri and stays open, {@code loser} runs on its own thread (its
     * existence SELECT sees nothing, its INSERT then waits on the open transaction's xid), and once the wait
     * is visible in pg_locks the winner commits. The loser's INSERT ... ON CONFLICT DO NOTHING then does
     * nothing and takes the race-lost path.
     */
    private <T> T withRacingWinner(String tenant, String winnerTumbler, String uri,
                                   java.util.concurrent.Callable<T> loser) throws Exception {
        var pool = java.util.concurrent.Executors.newSingleThreadExecutor();
        try (Connection win = pg.createConnection(""); Connection probe = pg.createConnection("")) {
            win.setAutoCommit(false);
            DSL.using(win, SQLDialect.POSTGRES).execute(
                "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, source_uri) VALUES (?, ?, 'winner', ?)",
                tenant, winnerTumbler, uri);
            var future = pool.submit(loser);
            long deadline = System.nanoTime() + java.util.concurrent.TimeUnit.SECONDS.toNanos(60);
            while (true) {
                Integer waiting = DSL.using(probe, SQLDialect.POSTGRES).fetchOne(
                    "SELECT count(*)::int FROM pg_locks WHERE locktype = 'transactionid' AND NOT granted")
                    .get(0, Integer.class);
                if (waiting != null && waiting > 0) break;
                assertThat(future.isDone()).as("the loser must be blocked on the winner, not finished").isFalse();
                assertThat(System.nanoTime()).as("timed out waiting for the loser to block").isLessThan(deadline);
                Thread.sleep(20);
            }
            win.commit();
            return future.get(60, java.util.concurrent.TimeUnit.SECONDS);
        } finally {
            pool.shutdownNow();
        }
    }

    // ── the batch dedupe ORs a repeated tumbler ─────────────────────────

    @Test
    void importDocumentsBatch_aTumblerRepeatedInOneBatch_isFlaggedWhicheverCopyIsLast() throws Exception {
        String t = tenant();
        // fresh tumblers: the dedupe keeps the last copy, so the flag must be the OR over all copies
        repo.importDocumentsBatch(t, List.of(importRow("48.30", "d1", true), importRow("48.30", "d1", false)));
        repo.importDocumentsBatch(t, List.of(importRow("48.31", "d2", false), importRow("48.31", "d2", true)));
        repo.importDocumentsBatch(t, List.of(importRow("48.32", "d3", false), importRow("48.32", "d3", null)));
        assertThat(flag(t, "48.30")).as("[true, false] for a fresh tumbler").isTrue();
        assertThat(flag(t, "48.31")).as("[false, true] for a fresh tumbler").isTrue();
        assertThat(flag(t, "48.32")).as("[false, null] stays plain").isFalse();

        // an existing plain row: one raise, audited once, whichever copy is last
        repo.importDocumentsBatch(t, List.of(importRow("48.33", "e", false)));
        repo.importDocumentsBatch(t, List.of(importRow("48.33", "e", true), importRow("48.33", "e", false)));
        assertThat(flag(t, "48.33")).isTrue();
        assertThat(audit(t)).containsExactly("48.33|false|true|import_raise|engine");
    }

    // ── a non-boolean flag is a 400, never read as false ────────────────

    @Test
    void aNonBooleanSensitive_isRefused_onRegisterUpsertAndBothImports_andWritesNothing() throws Exception {
        String t = tenant();
        String live = repo.registerDocument(t, OWNER, fields("live", "file:///r/strict.md", "strict.md", null));
        for (Object bad : new Object[] {1, 0, "yes", "true", "false", "", List.of(true)}) {
            assertThatThrownBy(() -> repo.registerDocument(t, OWNER, with(fields("x", "file:///r/x1.md", "x1.md", null), bad)))
                .as("registerDocument sensitive=%s", bad)
                .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("'sensitive' must be a boolean");
            assertThatThrownBy(() -> repo.registerDocument(t, OWNER, with(fields("live", "file:///r/strict.md", "strict.md", null), bad)))
                .as("registerDocument on a live row, sensitive=%s", bad).isInstanceOf(IllegalArgumentException.class);
            assertThatThrownBy(() -> repo.registerDocumentMany(t, OWNER, List.of(
                    fields("ok", "file:///r/x2.md", "x2.md", null),
                    with(fields("x", "file:///r/x3.md", "x3.md", null), bad))))
                .as("registerDocumentMany sensitive=%s", bad).isInstanceOf(IllegalArgumentException.class);
            assertThatThrownBy(() -> repo.upsertDocument(t, with(importRow("48.40", "u", null), bad)))
                .as("upsertDocument sensitive=%s", bad).isInstanceOf(IllegalArgumentException.class);
            assertThatThrownBy(() -> repo.importDocument(t, with(importRow("48.41", "i", null), bad)))
                .as("importDocument sensitive=%s", bad).isInstanceOf(IllegalArgumentException.class);
            assertThatThrownBy(() -> repo.importDocumentsBatch(t, List.of(
                    importRow("48.42", "b0", null), with(importRow("48.43", "b", null), bad))))
                .as("importDocumentsBatch sensitive=%s", bad).isInstanceOf(IllegalArgumentException.class);
        }
        assertThat(flag(t, live)).as("a refused request raised nothing").isFalse();
        try (Connection su = pg.createConnection("")) {
            assertThat(DSL.using(su, SQLDialect.POSTGRES).fetchOne(
                "SELECT count(*)::int FROM nexus.catalog_documents WHERE tenant_id = ?", t).get(0, Integer.class))
                .as("nothing but the one seeded row exists: every refusal rolled back").isEqualTo(1);
        }
        assertThat(audit(t)).isEmpty();
        // an explicit JSON null is the same as absent
        repo.upsertDocument(t, with(importRow("48.44", "n", null), null));
        assertThat(flag(t, "48.44")).isFalse();
    }

    private static Map<String, Object> with(Map<String, Object> m, Object sensitive) {
        var c = new LinkedHashMap<>(m);
        c.put("sensitive", sensitive);
        return c;
    }

    // ── merge and rename ────────────────────────────────────────────────

    @Test
    void merge_leavesTheCanonicalSensitiveWhenEitherSideWas() throws Exception {
        String t = tenant();
        String dupFlagged = repo.registerDocument(t, OWNER, fields("d1", "file:///r/d1.md", "d1.md", true));
        String canonPlain = repo.registerDocument(t, OWNER, fields("c1", "file:///r/c1.md", "c1.md", null));
        repo.mergeDocuments(t, dupFlagged, canonPlain);
        assertThat(flag(t, canonPlain)).as("duplicate sensitive, canonical plain").isTrue();
        assertThat(flag(t, dupFlagged)).as("the duplicate keeps its own flag").isTrue();
        assertThat(audit(t)).hasSize(1);
        assertThat(audit(t).get(0)).startsWith(canonPlain + "|false|true|merge").contains(dupFlagged)
            .endsWith("|engine");

        String dupPlain = repo.registerDocument(t, OWNER, fields("d2", "file:///r/d2.md", "d2.md", null));
        String canonFlagged = repo.registerDocument(t, OWNER, fields("c2", "file:///r/c2.md", "c2.md", true));
        repo.mergeDocuments(t, dupPlain, canonFlagged);
        assertThat(flag(t, canonFlagged)).as("canonical sensitive, duplicate plain: stays sensitive").isTrue();
        assertThat(flag(t, dupPlain)).isFalse();

        String dupPlain3 = repo.registerDocument(t, OWNER, fields("d3", "file:///r/d3.md", "d3.md", null));
        String canonPlain3 = repo.registerDocument(t, OWNER, fields("c3", "file:///r/c3.md", "c3.md", null));
        repo.mergeDocuments(t, dupPlain3, canonPlain3);
        assertThat(flag(t, canonPlain3)).as("neither side sensitive").isFalse();
        assertThat(audit(t)).as("only the first merge changed a flag").hasSize(1);
    }

    @Test
    void renameCollection_keepsTheFlag() throws Exception {
        String t = tenant();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), t, COLL_OLD);
        }
        String doc = repo.registerDocument(t, OWNER, fields("rn", "file:///r/rn.md", "rn.md", true));
        repo.renameCollection(t, COLL_OLD, COLL_NEW);
        assertThat(flag(t, doc)).as("rename rewrites physical_collection in place").isTrue();
        try (Connection su = pg.createConnection("")) {
            var r = DSL.using(su, SQLDialect.POSTGRES).fetchOne(
                "SELECT physical_collection FROM nexus.catalog_documents WHERE tenant_id = ? AND tumbler = ?", t, doc);
            assertThat(r.get(0, String.class)).isEqualTo(COLL_NEW);
        }
    }

    // ── the general update route ────────────────────────────────────────

    @Test
    void generalUpdate_refusesTheColumn_singleAndMany() throws Exception {
        String t = tenant();
        String doc = repo.registerDocument(t, OWNER, fields("g", "file:///r/g.md", "g.md", null));
        assertThatThrownBy(() -> repo.updateDocument(t, doc, Map.of("sensitive", true)))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("sensitive");
        var update = new LinkedHashMap<String, Object>();
        update.put("tumbler", doc);
        update.put("sensitive", true);
        assertThat(repo.updateDocumentsMany(t, List.of(update)))
            .as("the batch route marks an entry carrying a non-updatable column malformed (-1) and writes nothing")
            .containsExactly(-1);
        assertThat(flag(t, doc)).as("nothing moved the flag").isFalse();
    }
}
