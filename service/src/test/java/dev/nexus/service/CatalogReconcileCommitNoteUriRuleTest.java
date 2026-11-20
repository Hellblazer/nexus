// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P2.10 (nexus-p2m1x.15): the reconcile commit's collection-move rewrite follows {@code NoteUri.moveTo}
 * ({@code catalog-049-1} redefines {@code nexus.catalog_reconcile_commit}). The rest of the commit's behaviour is
 * {@link CatalogReconcileCommitBehaviourTest}'s.
 */
class CatalogReconcileCommitNoteUriRuleTest extends CatalogReconcileTestBase {

    @BeforeAll
    void boot() throws Exception {
        migrate();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, "svc_rc049_noteuri", "svc_rc049_noteuri_pass");
        }
        connect("svc_rc049_noteuri", "svc_rc049_noteuri_pass");
    }

    private static final String OWNER = "1.1";

    private String tenantWithOwner() {
        String t = newTenant();
        owner(t, OWNER, "/r");
        return t;
    }

    /**
     * RDR-229 P2.10: the commit's collection-move rewrite follows {@code NoteUri.moveTo}. A document filed in a
     * file-routed collection that carries {@code chroma://} residue or an {@code nx-note://<file-routed>} value, and
     * is staged under a prefix-free label, keeps both values on their old segment (the document itself moves), and a
     * live holder of the value the old rule would have made is no collision.
     */
    @Test
    void aCollectionMove_leavesResidueAndAFileRoutedNoteValueOnTheirOldSegment() {
        String t = tenantWithOwner();
        String residueUri = "chroma://docs__old/a.md";
        String noteUri = "nx-note://docs__old/b.md";
        String a = seed(t, OWNER, "a.md", Map.of("physical_collection", "docs__old", "content_type", "docs"));
        String b = seed(t, OWNER, "b.md", Map.of("physical_collection", "docs__old", "content_type", "docs"));
        String h = seed(t, OWNER, "held.md", Map.of("physical_collection", "plainlabel", "content_type", "docs"));
        setUri(t, a, residueUri);
        setUri(t, b, noteUri);
        setUri(t, h, "chroma://plainlabel/a.md");

        Map<String, Object> out = reconcile(t, OWNER, "r1", "partial",
            present(0, "a.md").set("physical_collection", "plainlabel").set("content_type", "docs"),
            present(1, "b.md").set("physical_collection", "plainlabel").set("content_type", "docs"));

        assertThat(actionOf(out, "a.md")).isEqualTo("updated");
        assertThat(col(t, a, "physical_collection")).as("the document moved").isEqualTo("plainlabel");
        assertThat(col(t, b, "physical_collection")).isEqualTo("plainlabel");
        assertThat(col(t, a, "source_uri")).as("residue keeps its segment").isEqualTo(residueUri);
        assertThat(col(t, b, "source_uri")).as("a file-routed nx-note:// value keeps its segment").isEqualTo(noteUri);
        assertThat(col(t, h, "source_uri")).isEqualTo("chroma://plainlabel/a.md");
    }

    /** A note of a note collection still follows the move: the rule narrows the rewrite, it does not remove it. */
    @Test
    void aCollectionMove_stillRewritesANoteOfANonFileRoutedCollection() {
        String t = tenantWithOwner();
        String a = seed(t, OWNER, "n.md", Map.of("physical_collection", "knowledge__old", "content_type", "knowledge"));
        String c = seed(t, OWNER, "l.md", Map.of("physical_collection", "knowledge__old", "content_type", "knowledge"));
        setUri(t, a, "nx-note://knowledge__old/n");
        setUri(t, c, "chroma://knowledge__old/l");

        reconcile(t, OWNER, "r1", "partial",
            present(0, "n.md").set("physical_collection", "knowledge__new").set("content_type", "knowledge"),
            present(1, "l.md").set("physical_collection", "knowledge__new").set("content_type", "knowledge"));

        assertThat(col(t, a, "source_uri")).isEqualTo("nx-note://knowledge__new/n");
        assertThat(col(t, c, "source_uri")).as("the legacy spelling is not moved").isEqualTo("chroma://knowledge__old/l");
    }

    private void setUri(String tenant, String tumbler, String uri) {
        scope.withTenant(tenant, ctx -> ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.SOURCE_URI, uri)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler))).execute());
    }
}
