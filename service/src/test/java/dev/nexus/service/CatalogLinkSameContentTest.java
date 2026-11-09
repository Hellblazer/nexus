// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.Field;
import org.jooq.JSONB;
import dev.nexus.service.db.CatalogLinksRepository;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_LINKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-193 Part A4 item 3 (nexus-u5m5g.4): {@code nexus.catalog_link_same_content} through
 * {@code CatalogRepository.linkSameContent}. The rule is {@code generate_pdf_corpus_links} and
 * {@code prune_stale_same_as_links} of {@code link_generator.py}: groups of live pdf and paper documents keyed by
 * {@code index_content_hash}, else the metadata {@code content_hash}, never {@code head_hash}; the anchor is the least
 * tumbler in code-point order; a seeded anchor gets every member; the prune keeps a {@code co_discovered_by} link and
 * an unsettled key, and refuses on an empty fetch.
 */
class CatalogLinkSameContentTest extends CatalogReconcileTestBase {

    private static final String DEDUP = "content_hash_dedup";

    private CatalogLinksRepository linksRepo;

    @BeforeAll
    void boot() throws Exception {
        migrate();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, "svc_lsc_048", "svc_lsc_048_pass");
        }
        connect("svc_lsc_048", "svc_lsc_048_pass");
        linksRepo = new CatalogLinksRepository(repo);
    }

    // ── fixtures ────────────────────────────────────────────────────────

    /** A live document of {@code type} with the given fence key and registration hash (either may be null). */
    private void doc(String t, String tumbler, String type, String indexKey, String metaKey) {
        Map<Field<?>, Object> more = new LinkedHashMap<>();
        more.put(CATALOG_DOCUMENTS.CONTENT_TYPE, type);
        if (indexKey != null) more.put(CATALOG_DOCUMENTS.INDEX_CONTENT_HASH, indexKey);
        if (metaKey != null) more.put(CATALOG_DOCUMENTS.METADATA, JSONB.valueOf("{\"content_hash\":\"" + metaKey + "\"}"));
        insertDoc(t, tumbler, tumbler + ".pdf", more);
    }

    private void pdf(String t, String tumbler, String key) {
        doc(t, tumbler, "pdf", key, null);
    }

    private String dedup(String from, String to) {
        return from + ">" + to + ":same-as:" + DEDUP + ":::";
    }

    private Map<String, Object> run(String t, boolean prune, String... tumblers) {
        return linksRepo.linkSameContent(t, List.of(tumblers), prune);
    }

    /** A link seeded by {@link #dedupLink}: the shared fixture writer stamps metadata {@code {"note": "x"}}. */
    private String seeded(String from, String to) {
        return from + ">" + to + ":same-as:" + DEDUP + ":::{\"note\": \"x\"}";
    }

    private void dedupLink(String t, String from, String to) {
        link(t, from, to, "same-as", DEDUP, "", "");
    }

    private void setLinkMeta(String t, String from, String to, String json) {
        scope.withTenant(t, ctx -> ctx.update(CATALOG_LINKS).set(CATALOG_LINKS.METADATA, JSONB.valueOf(json))
            .where(CATALOG_LINKS.TENANT_ID.eq(t).and(CATALOG_LINKS.FROM_TUMBLER.eq(from))
                .and(CATALOG_LINKS.TO_TUMBLER.eq(to))).execute());
    }

    // ── grouping and the anchor ─────────────────────────────────────────

    @Test
    void theAnchorIsTheLeastTumblerInCodePointOrder_1_2_10_beforeOneDot2Dot9() {
        String t = newTenant();
        pdf(t, "1.2.9", "K");
        pdf(t, "1.2.10", "K");

        Map<String, Object> res = run(t, false, "1.2.9");

        assertThat(res.get("created")).isEqualTo(1);
        assertThat(res.get("retracted")).isEqualTo(0);
        assertThat(links(t)).as("a numeric order would anchor on 1.2.9 and orphan every existing link")
            .containsExactly(dedup("1.2.9", "1.2.10"));
    }

    @Test
    void aSeededAnchorGetsEveryMember_aMemberOnlyLinksItself() {
        String t = newTenant();
        for (String s : List.of("1.3.1", "1.3.2", "1.3.3", "1.4.1", "1.4.2", "1.4.3")) pdf(t, s, s.startsWith("1.3") ? "A" : "B");

        Map<String, Object> seededAnchor = run(t, false, "1.3.1");
        Map<String, Object> seededMember = run(t, false, "1.4.3");

        assertThat(seededAnchor.get("created")).isEqualTo(2);
        assertThat(seededMember.get("created")).isEqualTo(1);
        assertThat(links(t)).containsExactly(dedup("1.3.2", "1.3.1"), dedup("1.3.3", "1.3.1"), dedup("1.4.3", "1.4.1"));
    }

    @Test
    void headHashNeverGroups_andNeitherDoesAMissingKey() {
        String t = newTenant();
        for (String s : List.of("1.5.1", "1.5.2", "1.5.3")) {
            pdf(t, s, null);                                   // no key at all
            setDoc(t, s, CATALOG_DOCUMENTS.HEAD_HASH, "SAMECOMMIT");   // the git commit shared by distinct files
        }
        pdf(t, "1.5.4", "X");
        pdf(t, "1.5.5", "Y");
        setDoc(t, "1.5.4", CATALOG_DOCUMENTS.HEAD_HASH, "SAMECOMMIT");
        setDoc(t, "1.5.5", CATALOG_DOCUMENTS.HEAD_HASH, "SAMECOMMIT");

        Map<String, Object> res = run(t, false, "1.5.1", "1.5.2", "1.5.3", "1.5.4", "1.5.5");

        assertThat(res.get("created")).isEqualTo(0);
        assertThat(links(t)).isEmpty();
    }

    @Test
    void theMetadataHashGroupsADocumentWithNoFenceKey_andPapersCountAsPdfs() {
        String t = newTenant();
        doc(t, "1.6.1", "pdf", null, "M");
        doc(t, "1.6.2", "paper", "", "M");        // an empty fence key reads as absent
        doc(t, "1.6.3", "pdf", "M", "OTHER");     // the fence key wins over its own metadata hash

        Map<String, Object> res = run(t, false, "1.6.2", "1.6.3");

        assertThat(res.get("created")).isEqualTo(2);
        assertThat(links(t)).containsExactly(dedup("1.6.2", "1.6.1"), dedup("1.6.3", "1.6.1"));
    }

    @Test
    void onlyLivePdfAndPaperDocumentsGroup_acrossOwners() {
        String t = newTenant();
        pdf(t, "1.7.1", "K");
        pdf(t, "2.1.1", "K");                                  // another owner: grouping is tenant-wide
        doc(t, "1.7.2", "prose", "K", null);                   // not a pdf
        pdf(t, "1.7.3", "K");
        tombstone(t, "1.7.3", false);                          // not live
        doc(t, "1.7.4", "pdf", "K", null);

        Map<String, Object> res = run(t, false, "1.7.4", "1.7.2", "1.7.3");

        assertThat(res.get("created")).as("only the live pdf given; prose and the tombstone seed nothing").isEqualTo(1);
        assertThat(links(t)).containsExactly(dedup("1.7.4", "1.7.1"));
    }

    @Test
    void anExistingLinkIsLeftAlone_insertOnlyWhenAbsent() {
        String t = newTenant();
        pdf(t, "1.8.1", "K");
        pdf(t, "1.8.2", "K");
        link(t, "1.8.2", "1.8.1", "same-as", "someone-else", "s1", "s2");
        List<String> before = links(t);

        Map<String, Object> first = run(t, false, "1.8.2");
        Map<String, Object> again = run(t, false, "1.8.2", "1.8.1");

        assertThat(first.get("created")).isEqualTo(0);
        assertThat(again.get("created")).isEqualTo(0);
        assertThat(links(t)).as("creator, spans and metadata untouched").isEqualTo(before);
    }

    @Test
    void creating_isIdempotent() {
        String t = newTenant();
        pdf(t, "1.9.1", "K");
        pdf(t, "1.9.2", "K");

        assertThat(run(t, false, "1.9.2").get("created")).isEqualTo(1);
        assertThat(run(t, false, "1.9.2").get("created")).isEqualTo(0);
        assertThat(links(t)).hasSize(1);
    }

    // ── prune ───────────────────────────────────────────────────────────

    @Test
    void prune_retractsALinkWhoseSettledKeysNowDiffer_oneRow() {
        String t = newTenant();
        pdf(t, "3.1.1", "A");
        pdf(t, "3.1.2", "A");
        pdf(t, "3.1.3", "A");
        dedupLink(t, "3.1.2", "3.1.1");
        dedupLink(t, "3.1.3", "3.1.1");
        setDoc(t, "3.1.2", CATALOG_DOCUMENTS.INDEX_CONTENT_HASH, "B");   // re-indexed with new bytes

        Map<String, Object> res = run(t, true, "3.1.2");

        assertThat(res.get("retracted")).isEqualTo(1);
        assertThat(res.get("created")).as("3.1.2 now has no group of its own").isEqualTo(0);
        assertThat(links(t)).as("only the stale pair went; 3.1.3 keeps its link").containsExactly(seeded("3.1.3", "3.1.1"));
    }

    @Test
    void prune_relinksTheFromSideOfARetractedLinkToTheNextAnchor() {
        String t = newTenant();
        pdf(t, "3.2.1", "A");
        pdf(t, "3.2.2", "A");
        pdf(t, "3.2.3", "A");
        dedupLink(t, "3.2.2", "3.2.1");
        dedupLink(t, "3.2.3", "3.2.1");
        setDoc(t, "3.2.1", CATALOG_DOCUMENTS.INDEX_CONTENT_HASH, "B");   // the anchor was re-indexed

        Map<String, Object> res = run(t, true, "3.2.1");

        assertThat(res.get("retracted")).isEqualTo(2);
        assertThat(res.get("created")).as("the two members re-join the next anchor of their group").isEqualTo(1);
        assertThat(links(t)).containsExactly(dedup("3.2.3", "3.2.2"));
    }

    @Test
    void prune_keepsACoDiscoveredByLink() {
        String t = newTenant();
        pdf(t, "3.3.1", "A");
        pdf(t, "3.3.2", "B");
        dedupLink(t, "3.3.2", "3.3.1");
        setLinkMeta(t, "3.3.2", "3.3.1", "{\"co_discovered_by\":[\"someone-else\"]}");

        Map<String, Object> res = run(t, true, "3.3.2");

        assertThat(res.get("retracted")).as("unlink would delete the other creator's assertion with it").isEqualTo(0);
        assertThat(links(t)).hasSize(1);
    }

    @Test
    void prune_keepsAnUnsettledKey_andAKeylessEndpoint() {
        String t = newTenant();
        doc(t, "3.4.1", "pdf", "A", null);
        doc(t, "3.4.2", "pdf", "C", "D");        // index key C, registration hash D: they disagree, so unsettled
        doc(t, "3.4.3", "pdf", null, null);       // no key at all
        dedupLink(t, "3.4.2", "3.4.1");
        dedupLink(t, "3.4.3", "3.4.1");

        Map<String, Object> res = run(t, true, "3.4.1");

        assertThat(res.get("retracted")).as("an unknown key is not a changed one").isEqualTo(0);
        assertThat(links(t)).containsExactly(seeded("3.4.2", "3.4.1"), seeded("3.4.3", "3.4.1"));
    }

    @Test
    void prune_retractsWhenAnEndpointIsNoLongerALivePdf() {
        String t = newTenant();
        pdf(t, "3.5.1", "A");
        pdf(t, "3.5.2", "A");
        pdf(t, "3.5.3", "A");
        pdf(t, "3.5.4", "A");
        dedupLink(t, "3.5.2", "3.5.1");
        dedupLink(t, "3.5.3", "3.5.1");
        dedupLink(t, "3.5.4", "3.5.1");
        setDoc(t, "3.5.2", CATALOG_DOCUMENTS.CONTENT_TYPE, "prose");      // retyped
        tombstone(t, "3.5.3", false);                                      // deleted

        Map<String, Object> res = run(t, true, "3.5.2", "3.5.3");

        assertThat(res.get("retracted")).isEqualTo(2);
        assertThat(links(t)).containsExactly(seeded("3.5.4", "3.5.1"));
    }

    @Test
    void prune_isScopedToTheGivenTumblers_andOffByDefault() {
        String t = newTenant();
        pdf(t, "3.6.1", "A");
        pdf(t, "3.6.2", "B");
        pdf(t, "3.6.3", "C");
        pdf(t, "3.6.4", "D");
        dedupLink(t, "3.6.2", "3.6.1");     // stale, in scope
        dedupLink(t, "3.6.4", "3.6.3");     // stale, out of scope

        Map<String, Object> noPrune = run(t, false, "3.6.2");
        Map<String, Object> prune = run(t, true, "3.6.2");

        assertThat(noPrune.get("retracted")).isEqualTo(0);
        assertThat(prune.get("retracted")).isEqualTo(1);
        assertThat(links(t)).containsExactly(seeded("3.6.4", "3.6.3"));
    }

    @Test
    void prune_refusesOnAnEmptyFetch_whenCandidateLinksExistAndNoLivePdfDoes() {
        String t = newTenant();
        pdf(t, "3.7.1", "A");
        pdf(t, "3.7.2", "A");
        dedupLink(t, "3.7.2", "3.7.1");
        setDocs(t, CATALOG_DOCUMENTS.CONTENT_TYPE, "prose");               // a failed fetch reads exactly like this
        List<String> before = links(t);

        Map<String, Object> res = run(t, true, "3.7.2");

        assertThat(res.get("refused")).isEqualTo("empty_fetch");
        assertThat(res.get("retracted")).isEqualTo(0);
        assertThat(res.get("created")).isEqualTo(0);
        assertThat(links(t)).as("acting on it would retract every link at once").isEqualTo(before);
    }

    @Test
    void prune_withNoCandidateLinks_doesNotRefuse() {
        String t = newTenant();
        doc(t, "3.8.1", "prose", null, null);

        Map<String, Object> res = run(t, true, "3.8.1");

        assertThat(res).doesNotContainKey("refused");
        assertThat(res.get("retracted")).isEqualTo(0);
    }

    // ── RLS ─────────────────────────────────────────────────────────────

    @Test
    void anotherTenantsDocumentsAndLinksAreInvisible() {
        String t1 = newTenant();
        String t2 = newTenant();
        pdf(t1, "1.2.9", "K");
        pdf(t2, "1.2.10", "K");                  // the same key in another tenant is not a group member
        pdf(t2, "5.1.1", "A");
        pdf(t2, "5.1.2", "B");
        dedupLink(t2, "5.1.2", "5.1.1");         // stale, but tenant 2's

        Map<String, Object> alone = run(t1, true, "1.2.9", "5.1.2");

        assertThat(alone.get("created")).isEqualTo(0);
        assertThat(alone.get("retracted")).isEqualTo(0);
        assertThat(links(t1)).isEmpty();
        assertThat(links(t2)).as("tenant 2's stale link was not reached from tenant 1").containsExactly(seeded("5.1.2", "5.1.1"));

        pdf(t1, "1.2.10", "K");
        assertThat(run(t1, false, "1.2.9").get("created")).isEqualTo(1);
        assertThat(links(t2)).hasSize(1);
    }
}
