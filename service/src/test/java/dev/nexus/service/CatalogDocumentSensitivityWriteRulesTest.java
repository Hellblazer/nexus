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
            .anyMatch(r -> r.startsWith(byUri + "|false|true|register_raise|"))
            .anyMatch(r -> r.startsWith(byPath + "|false|true|register_raise|"));

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
            .anyMatch(r -> r.startsWith(raise + "|false|true|register_raise|"))
            .anyMatch(r -> r.startsWith(out.get(2) + "|false|true|register_inherit") && r.contains(dead))
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
            .contains(first);
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
        repo.upsertDocument(t, importRow("48.1", "u1 again", false));
        assertThat(flag(t, "48.1")).as("false over true stays true").isTrue();
        repo.upsertDocument(t, importRow("48.1", "u1 once more", null));
        assertThat(flag(t, "48.1")).as("absent over true stays true").isTrue();

        repo.deleteDocument(t, "48.1");
        repo.upsertDocument(t, importRow("48.1", "u1 resurrected", false));
        assertThat(flag(t, "48.1")).as("the un-tombstone arm keeps the old row's flag").isTrue();

        repo.upsertDocument(t, importRow("48.2", "u2", false));
        assertThat(flag(t, "48.2")).isFalse();
        repo.upsertDocument(t, importRow("48.2", "u2", true));
        assertThat(flag(t, "48.2")).as("true over false raises").isTrue();
    }

    @Test
    void importDocument_andImportDocumentsBatch_orTheFlag() throws Exception {
        String t = tenant();
        repo.importDocument(t, importRow("48.10", "i1", true));
        repo.importDocument(t, importRow("48.10", "i1", false));
        assertThat(flag(t, "48.10")).as("importDocument: false over true").isTrue();
        repo.importDocument(t, importRow("48.11", "i2", false));
        repo.importDocument(t, importRow("48.11", "i2", true));
        assertThat(flag(t, "48.11")).as("importDocument: true over false").isTrue();

        repo.importDocumentsBatch(t, List.of(
            importRow("48.20", "b1", true),
            importRow("48.21", "b2", false),
            importRow("48.22", "b3", null)));
        repo.importDocumentsBatch(t, List.of(
            importRow("48.20", "b1", false),
            importRow("48.21", "b2", true),
            importRow("48.22", "b3", null)));
        assertThat(flag(t, "48.20")).as("batch: false over true").isTrue();
        assertThat(flag(t, "48.21")).as("batch: true over false").isTrue();
        assertThat(flag(t, "48.22")).as("batch: absent over plain").isFalse();
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
        assertThat(audit(t).get(0)).startsWith(canonPlain + "|false|true|merge").contains(dupFlagged);

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
