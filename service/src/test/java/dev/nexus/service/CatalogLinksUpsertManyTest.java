// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogLinksRepository;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_OWNERS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-193 Part A4 item 1 (nexus-u5m5g.4): {@code nexus.catalog_links_upsert_many} through
 * {@code CatalogRepository.linksUpsertMany}, on a migrated database as the NOBYPASSRLS service role. One test per
 * behaviour the design's Step 3 names: identity resolution finds a stale owner's row of the same file; a path present
 * only in another repository is unresolved; an alias endpoint resolves to its canonical; a dangling endpoint is
 * unresolved; an existing link keeps its fields; RLS; and residual 7 (the endpoint's named owner is the owner of the
 * repository being linked, never the source document's).
 */
class CatalogLinksUpsertManyTest extends CatalogReconcileTestBase {

    private CatalogLinksRepository linksRepo;

    @BeforeAll
    void boot() throws Exception {
        migrate();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, "svc_lum_048", "svc_lum_048_pass");
        }
        connect("svc_lum_048", "svc_lum_048_pass");
        linksRepo = new CatalogLinksRepository(repo);
    }

    // ── edge builders ───────────────────────────────────────────────────

    static Map<String, Object> tum(String tumbler) {
        return Map.of("tumbler", tumbler);
    }

    static Map<String, Object> path(String owner, String filePath) {
        return Map.of("owner", owner, "file_path", filePath);
    }

    static Map<String, Object> edge(Map<String, Object> from, Map<String, Object> to, String type, String by) {
        Map<String, Object> e = new LinkedHashMap<>();
        e.put("from", from);
        e.put("to", to);
        e.put("link_type", type);
        e.put("created_by", by);
        return e;
    }

    @SafeVarargs
    private Map<String, Object> upsert(String tenant, Map<String, Object>... edges) {
        return linksRepo.linksUpsertMany(tenant, List.of(edges));
    }

    /** The unresolved entries as {@code index:side:reason}. */
    @SuppressWarnings("unchecked")
    static List<String> unresolved(Map<String, Object> res) {
        var out = new ArrayList<String>();
        for (Map<String, Object> u : (List<Map<String, Object>>) res.get("unresolved")) {
            out.add(u.get("index") + ":" + u.get("side") + ":" + u.get("reason"));
        }
        return out;
    }

    private String tenantWith(String prefix, String root) {
        String t = newTenant();
        owner(t, prefix, root);
        return t;
    }

    // ── identity resolution ─────────────────────────────────────────────

    @Test
    void identityFindsAStaleOwnersRowOfTheSameFile() {
        String t = tenantWith("1.1", "/r");
        owner(t, "1.2", "/old");
        String src = seed(t, "1.1", "docs/src.md");
        // the same file registered under the stale owner 1.2: its URI is the one 1.1's repo_root derives
        insertDoc(t, "1.2.1", "legacy/a.md", Map.of(CATALOG_DOCUMENTS.SOURCE_URI, "file:///r/docs/a.md"));

        Map<String, Object> res = upsert(t, edge(tum(src), path("1.1", "docs/a.md"), "cites", "gen"));

        assertThat(res.get("created")).isEqualTo(1);
        assertThat(res.get("skipped")).isEqualTo(0);
        assertThat(unresolved(res)).isEmpty();
        assertThat(links(t)).containsExactly(src + ">1.2.1:cites:gen:::");
    }

    @Test
    void anAbsolutePath_andADotSegmentPath_resolveByTheSameDerivedUri() {
        String t = tenantWith("1.1", "/r");
        String src = seed(t, "1.1", "docs/src.md");
        String a = seed(t, "1.1", "docs/a.md");

        Map<String, Object> res = upsert(t,
            edge(tum(src), path("1.1", "/r/docs/a.md"), "cites", "gen"),
            edge(tum(src), path("1.1", "docs/../docs/./a.md"), "implements", "gen"));

        assertThat(res.get("created")).isEqualTo(2);
        assertThat(links(t)).containsExactly(src + ">" + a + ":cites:gen:::", src + ">" + a + ":implements:gen:::");
    }

    @Test
    void identityIsTriedBeforeTheNamedOwnersFilePath() {
        String t = tenantWith("1.1", "/r");
        owner(t, "1.5", "/five");
        String src = seed(t, "1.1", "docs/src.md");
        String byPath = seed(t, "1.1", "x.md");   // the named owner's row with file_path x.md (URI file:///r/x.md)
        // clear its URI so the only identity match is the other row, then give that one the derived URI
        setDoc(t, byPath, CATALOG_DOCUMENTS.SOURCE_URI, "");
        insertDoc(t, "1.5.1", "elsewhere/x.md", Map.of(CATALOG_DOCUMENTS.SOURCE_URI, "file:///r/x.md"));

        upsert(t, edge(tum(src), path("1.1", "x.md"), "cites", "gen"));

        assertThat(links(t)).as("identity wins over the file_path arm").containsExactly(src + ">1.5.1:cites:gen:::");
    }

    @Test
    void severalRowsUnderTheNamedOwner_takeTheOneDocumentsByOwnerListsLast() {
        String t = tenantWith("1.4", "/s");
        owner(t, "1.1", "");          // no repo_root: no identity arm, only the file_path arm
        String src = seed(t, "1.4", "s.md");
        insertDoc(t, "1.1.9", "dup.md", Map.of());
        insertDoc(t, "1.1.10", "dup.md", Map.of());
        List<Map<String, Object>> listed = repo.documentsByOwner(t, "1.1", 0, 0);
        String last = null;
        for (Map<String, Object> d : listed) if ("dup.md".equals(d.get("file_path"))) last = (String) d.get("tumbler");

        upsert(t, edge(tum(src), path("1.1", "dup.md"), "cites", "gen"));

        assertThat(last).isNotNull();
        assertThat(links(t)).as("the last in documentsByOwner's own order (the database collation)")
            .containsExactly(src + ">" + last + ":cites:gen:::");
    }

    @Test
    void aPathPresentOnlyInAnotherRepository_isUnresolvedNoPath() {
        String t = tenantWith("1.1", "/r");
        owner(t, "1.3", "/other");
        String src = seed(t, "1.1", "docs/src.md");
        String other = seed(t, "1.3", "docs/b.md");   // file:///other/docs/b.md

        Map<String, Object> res = upsert(t, edge(tum(src), path("1.1", "docs/b.md"), "cites", "gen"));

        assertThat(res.get("created")).isEqualTo(0);
        assertThat(unresolved(res)).containsExactly("0:to:no_path");
        assertThat(links(t)).as("the cross-repository edge is dropped, not written").isEmpty();

        // the control: naming the repository that holds the file resolves it
        Map<String, Object> ok = upsert(t, edge(tum(src), path("1.3", "docs/b.md"), "cites", "gen"));
        assertThat(ok.get("created")).isEqualTo(1);
        assertThat(links(t)).containsExactly(src + ">" + other + ":cites:gen:::");
    }

    @Test
    void residual7_theNamedOwnerIsTheRepositoryBeingLinked_notTheSourcesOwner() {
        String t = tenantWith("1.1", "/r");          // O_new, the current owner of the repository
        owner(t, "1.2", "");                          // O_old, the stale owner of about 150 RDRs: no repo_root
        String target = seed(t, "1.1", "docs/target.md");                       // file:///r/docs/target.md
        insertDoc(t, "1.2.1", "rdr/rdr-001.md",                                 // the source, under O_old
            Map.of(CATALOG_DOCUMENTS.SOURCE_URI, "file:///r/docs/rdr/rdr-001.md"));

        Map<String, Object> viaNew = upsert(t, edge(tum("1.2.1"), path("1.1", "docs/target.md"), "cites", "gen"));
        assertThat(viaNew.get("created")).as("the edge naming O_new resolves the target").isEqualTo(1);
        assertThat(links(t)).containsExactly("1.2.1>" + target + ":cites:gen:::");

        Map<String, Object> viaOld = upsert(t, edge(tum("1.2.1"), path("1.2", "docs/target.md"), "implements", "gen"));
        assertThat(viaOld.get("created")).as("the same edge naming O_old (no repo_root) does not").isEqualTo(0);
        assertThat(unresolved(viaOld)).containsExactly("0:to:no_path");

        setOwner(t, "1.2", CATALOG_OWNERS.REPO_ROOT, "/elsewhere");               // O_old with another repo_root
        Map<String, Object> viaOtherRoot = upsert(t, edge(tum("1.2.1"), path("1.2", "docs/target.md"), "implements", "gen"));
        assertThat(unresolved(viaOtherRoot)).containsExactly("0:to:no_path");
        assertThat(links(t)).as("nothing else written").hasSize(1);
    }

    @Test
    void anAbsolutePathIntoAnotherRepository_andADotDotPathOutOfTheRoot_areNoPath() {
        String t = tenantWith("1.1", "/r");
        owner(t, "1.3", "/other");
        owner(t, "1.4", "/rx");   // shares the textual prefix "/r" with the named owner's root
        String src = seed(t, "1.1", "docs/src.md");
        seed(t, "1.3", "docs/b.md");   // file:///other/docs/b.md
        seed(t, "1.4", "docs/c.md");   // file:///rx/docs/c.md

        Map<String, Object> res = upsert(t,
            edge(tum(src), path("1.1", "/other/docs/b.md"), "cites", "gen"),            // absolute, into the other repository
            edge(tum(src), path("1.1", "../other/docs/b.md"), "cites", "gen"),          // dot segments climb out of /r
            edge(tum(src), path("1.1", "docs/../../other/docs/b.md"), "cites", "gen"),
            edge(tum(src), path("1.1", "/rx/docs/c.md"), "cites", "gen"));              // "/rx" starts with "/r" but is not inside it

        assertThat(res.get("created")).isEqualTo(0);
        assertThat(unresolved(res)).containsExactly("0:to:no_path", "1:to:no_path", "2:to:no_path", "3:to:no_path");
        assertThat(links(t)).as("no edge reaches another repository's file").isEmpty();
    }

    @Test
    void anAbsolutePath_underAnOwnerWithNoRepoRoot_orNoSuchOwner_isNoPath() {
        String t = tenantWith("1.1", "/r");
        owner(t, "1.2", "");
        String src = seed(t, "1.1", "docs/src.md");
        seed(t, "1.1", "docs/a.md");   // file:///r/docs/a.md

        Map<String, Object> res = upsert(t,
            edge(tum(src), path("1.2", "/r/docs/a.md"), "cites", "gen"),     // an owner with no repo_root has no repository to contain it
            edge(tum(src), path("9.9", "/r/docs/a.md"), "cites", "gen"));    // no such owner

        assertThat(res.get("created")).isEqualTo(0);
        assertThat(unresolved(res)).containsExactly("0:to:no_path", "1:to:no_path");
        assertThat(links(t)).isEmpty();
    }

    // ── tumbler endpoints ───────────────────────────────────────────────

    /** An alias chain {@code first -> first+1 -> ... -> last}: {@code n} live documents under 1.1, the last one not an alias. */
    private void aliasChain(String t, int first, int n) {
        for (int i = 0; i < n; i++) {
            Map<org.jooq.Field<?>, Object> more = new LinkedHashMap<>();
            if (i < n - 1) more.put(CATALOG_DOCUMENTS.ALIAS_OF, "1.1." + (first + i + 1));
            insertDoc(t, "1.1." + (first + i), "chain" + (first + i) + ".md", more);
        }
    }

    @Test
    void anAliasWhoseCanonicalIsTombstonedMissingCyclicOrTooFar_isDangling_andTheRestOfTheBatchStillWrites() {
        String t = tenantWith("1.1", "/r");
        String a = seed(t, "1.1", "a.md");
        String b = seed(t, "1.1", "b.md");
        String gone = seed(t, "1.1", "gone.md");
        tombstone(t, gone, false);
        insertDoc(t, "1.1.50", "toTombstone.md", Map.of(CATALOG_DOCUMENTS.ALIAS_OF, gone));
        insertDoc(t, "1.1.51", "toMissing.md", Map.of(CATALOG_DOCUMENTS.ALIAS_OF, "1.1.999"));
        insertDoc(t, "1.1.52", "cycleA.md", Map.of(CATALOG_DOCUMENTS.ALIAS_OF, "1.1.53"));
        insertDoc(t, "1.1.53", "cycleB.md", Map.of(CATALOG_DOCUMENTS.ALIAS_OF, "1.1.52"));
        aliasChain(t, 100, 20);   // 1.1.100 .. 1.1.119: 19 hops from the first to the live canonical, past the 16-hop limit
        aliasChain(t, 200, 3);    // 1.1.200 -> 1.1.201 -> 1.1.202: within the limit

        Map<String, Object> res = upsert(t,
            edge(tum(a), tum("1.1.50"), "cites", "gen"),     // alias of a tombstoned document
            edge(tum("1.1.51"), tum(a), "cites", "gen"),     // alias of a document that is not there
            edge(tum(a), tum("1.1.52"), "cites", "gen"),     // a cycle
            edge(tum(a), tum("1.1.100"), "cites", "gen"),    // a chain past the hop limit
            edge(tum(a), tum(b), "cites", "gen"),            // fine
            edge(tum(a), tum("1.1.200"), "cites", "gen"));   // a chain within the limit resolves to its end

        assertThat(res.get("created")).as("the batch is not failed by the stuck aliases").isEqualTo(2);
        assertThat(unresolved(res)).containsExactly("0:to:dangling", "1:from:dangling", "2:to:dangling", "3:to:dangling");
        assertThat(links(t)).containsExactlyInAnyOrder(a + ">" + b + ":cites:gen:::", a + ">1.1.202:cites:gen:::");
    }

    @Test
    void aPathWhoseRowIsAnAliasOfATombstonedDocument_isNoPath() {
        String t = tenantWith("1.1", "/r");
        String src = seed(t, "1.1", "src.md");
        String gone = seed(t, "1.1", "gone.md");
        tombstone(t, gone, false);
        insertDoc(t, "1.1.50", "stuck.md", Map.of(CATALOG_DOCUMENTS.ALIAS_OF, gone));

        Map<String, Object> res = upsert(t, edge(tum(src), path("1.1", "stuck.md"), "cites", "gen"));

        assertThat(res.get("created")).isEqualTo(0);
        assertThat(unresolved(res)).containsExactly("0:to:no_path");
    }

    @Test
    void anAliasEndpointResolvesToItsCanonical() {
        String t = tenantWith("1.1", "/r");
        String a = seed(t, "1.1", "a.md");
        String b = seed(t, "1.1", "b.md");
        String c = seed(t, "1.1", "c.md");
        setDoc(t, c, CATALOG_DOCUMENTS.ALIAS_OF, b);

        Map<String, Object> res = upsert(t,
            edge(tum(a), tum(c), "cites", "gen"),     // to side is the alias
            edge(tum(c), tum(a), "relates", "gen"));  // from side is the alias

        assertThat(res.get("created")).isEqualTo(2);
        assertThat(links(t)).containsExactly(a + ">" + b + ":cites:gen:::", b + ">" + a + ":relates:gen:::");
    }

    @Test
    void aDanglingEndpoint_isUnresolvedAndNotWritten() {
        String t = tenantWith("1.1", "/r");
        String a = seed(t, "1.1", "a.md");
        String gone = seed(t, "1.1", "gone.md");
        tombstone(t, gone, false);

        Map<String, Object> res = upsert(t,
            edge(tum(a), tum(gone), "cites", "gen"),
            edge(tum(a), tum("1.1.999"), "cites", "gen"),
            edge(tum(gone), tum(a), "cites", "gen"));

        assertThat(res.get("created")).isEqualTo(0);
        assertThat(unresolved(res)).containsExactly("0:to:dangling", "1:to:dangling", "2:from:dangling");
        assertThat(links(t)).isEmpty();
    }

    @Test
    void anExistingLinkKeepsItsFields_andIsSkipped() {
        String t = tenantWith("1.1", "/r");
        String a = seed(t, "1.1", "a.md");
        String b = seed(t, "1.1", "b.md");
        String c = seed(t, "1.1", "c.md");
        setDoc(t, c, CATALOG_DOCUMENTS.ALIAS_OF, b);
        link(t, a, b, "cites", "orig", "s1", "s2");
        List<String> before = links(t);

        Map<String, Object> same = linksRepo.linksUpsertMany(t, List.of(withSpans(edge(tum(a), tum(b), "cites", "other"), "n1", "n2")));
        // declared difference: an alias endpoint whose canonical edge exists is resolved first and skipped, not merged
        Map<String, Object> viaAlias = linksRepo.linksUpsertMany(t, List.of(withSpans(edge(tum(a), tum(c), "cites", "third"), "m1", "m2")));

        assertThat(same.get("created")).isEqualTo(0);
        assertThat(same.get("skipped")).isEqualTo(1);
        assertThat(viaAlias.get("skipped")).isEqualTo(1);
        assertThat(links(t)).as("creator, spans and metadata untouched, no co_discovered_by folded in").isEqualTo(before);
    }

    private static Map<String, Object> withSpans(Map<String, Object> e, String from, String to) {
        Map<String, Object> m = new LinkedHashMap<>(e);
        m.put("from_span", from);
        m.put("to_span", to);
        return m;
    }

    @Test
    void spansAndCreatorAreStored_onANewLink() {
        String t = tenantWith("1.1", "/r");
        String a = seed(t, "1.1", "a.md");
        String b = seed(t, "1.1", "b.md");

        linksRepo.linksUpsertMany(t, List.of(withSpans(edge(tum(a), tum(b), "cites", "gen"), "chash:aa", "chash:bb")));

        assertThat(links(t)).containsExactly(a + ">" + b + ":cites:gen:chash:aa:chash:bb:");
    }

    // ── the lock pass ───────────────────────────────────────────────────

    @Test
    void aRowLockedByAMerge_isWaitedFor_andTheEdgeThenFollowsItsNewAlias() throws Exception {
        String t = tenantWith("1.1", "/r");
        String a = seed(t, "1.1", "a.md");
        String b = seed(t, "1.1", "b.md");
        String c = seed(t, "1.1", "c.md");
        Map<String, Object> res;
        var pool = java.util.concurrent.Executors.newSingleThreadExecutor();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            // a merge in flight: it holds c FOR UPDATE, as mergeDocuments does, and is about to alias it to b
            suCtx(su).selectOne().from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(t).and(CATALOG_DOCUMENTS.TUMBLER.eq(c))).forUpdate().fetch();
            var call = pool.submit(() -> upsert(t, edge(tum(a), tum(c), "cites", "gen")));
            awaitCommitsBlocked(su, 1, call);   // the batch resolved c unaliased and is now waiting for its FOR KEY SHARE lock
            assertThat(call.isDone()).as("the batch waits for the merge").isFalse();
            suCtx(su).update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.ALIAS_OF, b)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(t).and(CATALOG_DOCUMENTS.TUMBLER.eq(c))).execute();
            su.commit();
            res = call.get(60, java.util.concurrent.TimeUnit.SECONDS);
        } finally {
            pool.shutdownNow();
        }

        assertThat(res.get("created")).isEqualTo(1);
        assertThat(links(t)).as("resolved again after the lock: the edge lands on the canonical, not on the alias it raced")
            .containsExactly(a + ">" + b + ":cites:gen:::");
    }

    @Test
    void aRowDeletedWhileTheBatchWaitsForItsLock_leavesTheEdgeDangling() throws Exception {
        String t = tenantWith("1.1", "/r");
        String a = seed(t, "1.1", "a.md");
        String b = seed(t, "1.1", "b.md");
        String c = seed(t, "1.1", "c.md");
        Map<String, Object> res;
        var pool = java.util.concurrent.Executors.newSingleThreadExecutor();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            suCtx(su).selectOne().from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(t).and(CATALOG_DOCUMENTS.TUMBLER.eq(c))).forUpdate().fetch();
            var call = pool.submit(() -> upsert(t, edge(tum(a), tum(c), "cites", "gen"), edge(tum(a), tum(b), "cites", "gen")));
            awaitCommitsBlocked(su, 1, call);
            suCtx(su).update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.DELETED_AT, java.time.OffsetDateTime.now())
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(t).and(CATALOG_DOCUMENTS.TUMBLER.eq(c))).execute();
            su.commit();
            res = call.get(60, java.util.concurrent.TimeUnit.SECONDS);
        } finally {
            pool.shutdownNow();
        }

        assertThat(res.get("created")).isEqualTo(1);
        assertThat(unresolved(res)).containsExactly("0:to:dangling");
        assertThat(links(t)).containsExactly(a + ">" + b + ":cites:gen:::");
    }

    // ── created_at: the column is NOT NULL DEFAULT now() (hygiene-001-7); these pin that an insert here leaves it stamped ──

    @Test
    void aNewLinkIsStampedCreatedAt_andASkippedOneKeepsItsStamp() {
        String t = tenantWith("1.1", "/r");
        String a = seed(t, "1.1", "a.md");
        String b = seed(t, "1.1", "b.md");

        upsert(t, edge(tum(a), tum(b), "cites", "gen"));
        java.time.OffsetDateTime stamped = linkCreatedAt(t, a, b);
        assertThat(stamped).as("a link inserted by the batch has a created_at (bulk_unlink and created_at_before read it)").isNotNull();
        assertThat(stamped).isBetween(java.time.OffsetDateTime.now().minusMinutes(10), java.time.OffsetDateTime.now().plusMinutes(10));

        java.time.OffsetDateTime old = java.time.OffsetDateTime.parse("2001-02-03T04:05:06Z");
        setLinkCreatedAt(t, a, b, old);
        Map<String, Object> again = upsert(t, edge(tum(a), tum(b), "cites", "other"));

        assertThat(again.get("skipped")).isEqualTo(1);
        assertThat(linkCreatedAt(t, a, b).toInstant()).as("a skipped existing link is not restamped").isEqualTo(old.toInstant());
    }

    // ── the batch ───────────────────────────────────────────────────────

    @Test
    void aMixedBatch_countsEachEdgeOnce_andRepeatsAreSkipped() {
        String t = tenantWith("1.1", "/r");
        String a = seed(t, "1.1", "a.md");
        String b = seed(t, "1.1", "b.md");

        Map<String, Object> res = upsert(t,
            edge(tum(a), tum(b), "cites", "gen"),
            edge(tum(a), tum("1.1.404"), "cites", "gen"),
            edge(tum(a), tum(b), "cites", "gen"),                      // a repeat inside the batch
            edge(tum(a), path("1.1", "nope.md"), "cites", "gen"),
            edge(tum(b), tum(a), "cites", "gen"));

        assertThat(res.get("created")).isEqualTo(2);
        assertThat(res.get("skipped")).isEqualTo(1);
        assertThat(unresolved(res)).containsExactly("1:to:dangling", "3:to:no_path");
        assertThat(links(t)).containsExactly(a + ">" + b + ":cites:gen:::", b + ">" + a + ":cites:gen:::");
    }

    @Test
    void aSelfLinkIsNotRefused() {
        String t = tenantWith("1.1", "/r");
        String a = seed(t, "1.1", "a.md");

        Map<String, Object> res = upsert(t, edge(tum(a), tum(a), "relates", "gen"));

        assertThat(res.get("created")).as("upsertLink accepts a self-link, so the batch does too").isEqualTo(1);
    }

    @Test
    void anEmptyBatch_isZero() {
        String t = tenantWith("1.1", "/r");
        Map<String, Object> res = linksRepo.linksUpsertMany(t, List.of());
        assertThat(res.get("created")).isEqualTo(0);
        assertThat(res.get("skipped")).isEqualTo(0);
        assertThat(unresolved(res)).isEmpty();
    }

    @Test
    void aMalformedEdge_failsTheWholeBatch_andWritesNothing() {
        String t = tenantWith("1.1", "/r");
        String a = seed(t, "1.1", "a.md");
        String b = seed(t, "1.1", "b.md");
        Map<String, Object> noType = new LinkedHashMap<>(edge(tum(a), tum(b), "cites", "gen"));
        noType.remove("link_type");
        Map<String, Object> badEnd = new LinkedHashMap<>(edge(tum(a), tum(b), "cites", "gen"));
        badEnd.put("to", Map.of("owner", "1.1"));   // a path endpoint with no file_path

        assertThatThrownBy(() -> linksRepo.linksUpsertMany(t, List.of(edge(tum(a), tum(b), "cites", "gen"), noType)))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("invalid_edge");
        assertThatThrownBy(() -> linksRepo.linksUpsertMany(t, List.of(badEnd)))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("invalid_endpoint");
        assertThat(links(t)).as("the good edge of the first call was not written").isEmpty();
    }

    // ── RLS ─────────────────────────────────────────────────────────────

    @Test
    void anotherTenantsRowsAreInvisible() {
        String t1 = tenantWith("1.1", "/r");
        String t2 = tenantWith("1.1", "/r");
        String a1 = seed(t1, "1.1", "a.md");
        String b1 = seed(t1, "1.1", "b.md");
        String a2 = seed(t2, "1.1", "a.md");   // the same tumblers in tenant 2: 1.1.1 and no 1.1.2
        assertThat(a2).isEqualTo(a1);

        Map<String, Object> viaTumbler = upsert(t2, edge(tum(a1), tum(b1), "cites", "gen"));
        Map<String, Object> viaPath = upsert(t2, edge(tum(a2), path("1.1", "b.md"), "cites", "gen"));

        assertThat(unresolved(viaTumbler)).as("tenant 1's b is not tenant 2's").containsExactly("0:to:dangling");
        assertThat(unresolved(viaPath)).containsExactly("0:to:no_path");
        assertThat(links(t2)).isEmpty();
        assertThat(links(t1)).isEmpty();

        // and the write lands in the caller's own tenant only
        upsert(t1, edge(tum(a1), tum(b1), "cites", "gen"));
        assertThat(links(t1)).hasSize(1);
        assertThat(links(t2)).isEmpty();
    }
}
