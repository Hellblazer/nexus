// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CombinedWriteService.MetadataMode;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-223 (bead nexus-z0o2p.13) -- the combined routes' metadata write mode. Default REPLACE
 * (unchanged): an existing chunk's stored metadata becomes the incoming metadata, so a writer
 * that does not send {@code bib_*} clears it. MERGE ({@code metadata_merge}): stored metadata
 * becomes {@code (stored - metadata_delete_keys) || incoming}, the semantics of
 * {@code upsert-chunks}, so an indexer re-writing the keys it owns leaves the enrichment another
 * writer set. Both write branches for a stored chash are covered: the metadata-only refresh
 * (identical text) and the insert branch ({@code force_re_embed}).
 */
class MetadataMergeTest extends AtomicWriteTestBase {

    private static Map<String, Object> chunkM(String chash, String text, Map<String, Object> meta) {
        return Map.of("chash", chash, "text", text, "metadata", meta);
    }

    private static Map<String, Object> meta(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) m.put((String) kv[i], kv[i + 1]);
        return m;
    }

    private Map<String, Object> storedMeta(String collection, String hexChash) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var json = DSL.using(su, SQLDialect.POSTGRES)
                .select(CHUNKS.METADATA).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(TENANT))
                .and(CHUNKS.COLLECTION.eq(collection))
                .and(CHUNKS.CHASH.eq(HexFormat.of().parseHex(hexChash)))
                .fetchOne(CHUNKS.METADATA);
            @SuppressWarnings("unchecked")
            Map<String, Object> m = new com.fasterxml.jackson.databind.ObjectMapper().readValue(json.data(), Map.class);
            return m;
        }
    }

    /** Seeds one chunk with {content_hash: v1, section: "old", stale: "x", bib_year: 2020}. */
    private String seed(Fx f, String tag) {
        String a = ch(tag + "-a");
        svc.writeManyCombined(TENANT, f.collection(),
            List.of(chunkM(a, tag + " a", meta("content_hash", "v1", "section", "old", "stale", "x", "bib_year", 2020))),
            List.of(doc(f.docId(), List.of(row(0, a)))), null, false, false);
        return a;
    }

    private static Map<String, Object> incoming() {
        return meta("content_hash", "v2", "section", "new");
    }

    @Test
    void replaceIsTheDefault_andClearsWhatTheIncomingMetadataOmits() throws Exception {
        Fx f = fixture("rep");
        String a = seed(f, "rep");

        svc.writeManyCombined(TENANT, f.collection(), List.of(chunkM(a, "rep a", incoming())),
            List.of(doc(f.docId(), List.of(row(0, a)))), null, false, false);

        assertThat(storedMeta(f.collection(), a)).isEqualTo(incoming());
    }

    @Test
    void writeMany_merge_metadataOnlyBranch_keepsEnrichmentAndDropsTheNamedKeys() throws Exception {
        Fx f = fixture("wm");
        String a = seed(f, "wm");
        int embedsBefore = embedder.calls.get();

        var resp = svc.writeManyCombined(TENANT, f.collection(), List.of(chunkM(a, "wm a", incoming())),
            List.of(doc(f.docId(), List.of(row(0, a)))), null, false, false, null,
            new MetadataMode(true, List.of("stale"))).response();

        assertThat(storedMeta(f.collection(), a)).isEqualTo(
            meta("content_hash", "v2", "section", "new", "bib_year", 2020));
        assertThat(resp.get("embed_embedded")).as("identical text is not re-embedded").isEqualTo(0);
        assertThat(embedder.calls.get()).isEqualTo(embedsBefore);
    }

    @Test
    void writeMany_merge_insertBranch_underForceReEmbed_keepsEnrichmentAndDropsTheNamedKeys() throws Exception {
        Fx f = fixture("wmf");
        String a = seed(f, "wmf");
        int embedsBefore = embedder.calls.get();

        var resp = svc.writeManyCombined(TENANT, f.collection(), List.of(chunkM(a, "wmf a", incoming())),
            List.of(doc(f.docId(), List.of(row(0, a)))), null, false, true, null,
            new MetadataMode(true, List.of("stale"))).response();

        assertThat(resp.get("embed_embedded")).as("force_re_embed re-embeds").isEqualTo(1);
        assertThat(embedder.calls.get()).isEqualTo(embedsBefore + 1);
        assertThat(storedMeta(f.collection(), a)).isEqualTo(
            meta("content_hash", "v2", "section", "new", "bib_year", 2020));
    }

    @Test
    void merge_withNoDeleteKeys_keepsEveryStoredKeyTheIncomingOmits() throws Exception {
        Fx f = fixture("nodk");
        String a = seed(f, "nodk");

        svc.writeManyCombined(TENANT, f.collection(), List.of(chunkM(a, "nodk a", incoming())),
            List.of(doc(f.docId(), List.of(row(0, a)))), null, false, false, null,
            new MetadataMode(true, List.of()));

        assertThat(storedMeta(f.collection(), a)).isEqualTo(
            meta("content_hash", "v2", "section", "new", "stale", "x", "bib_year", 2020));
    }

    @Test
    void merge_ofAChunkNotStoredYet_takesTheIncomingMetadataAsIs() throws Exception {
        Fx f = fixture("new");
        String a = ch("new-a");

        svc.writeManyCombined(TENANT, f.collection(), List.of(chunkM(a, "new a", incoming())),
            List.of(doc(f.docId(), List.of(row(0, a)))), null, false, false, null,
            new MetadataMode(true, List.of("stale")));

        assertThat(storedMeta(f.collection(), a)).isEqualTo(incoming());
    }

    @Test
    void append_merge_bothBranches() throws Exception {
        Fx f = fixture("app");
        String a = seed(f, "app");
        String b = ch("app-b");
        // b is stored under another document with enrichment, then appended to this one.
        svc.writeManyCombined(TENANT, f.collection(),
            List.of(chunkM(b, "app b", meta("content_hash", "v1", "bib_year", 1999))),
            List.of(doc(freshDoc("app-other", f.collection()), List.of(row(0, b)))), null, false, false);

        svc.appendCombined(TENANT, f.collection(), f.docId(), List.of(row(0, a), row(1, b)),
            List.of(chunkM(a, "app a", incoming()), chunkM(b, "app b", incoming())), false, null, null,
            new MetadataMode(true, List.of("stale")));

        assertThat(storedMeta(f.collection(), a)).isEqualTo(
            meta("content_hash", "v2", "section", "new", "bib_year", 2020));
        assertThat(storedMeta(f.collection(), b)).isEqualTo(
            meta("content_hash", "v2", "section", "new", "bib_year", 1999));

        svc.appendCombined(TENANT, f.collection(), f.docId(), List.of(row(0, a)),
            List.of(chunkM(a, "app a", incoming())), true, null, null,
            new MetadataMode(true, List.of("stale", "bib_year")));
        assertThat(storedMeta(f.collection(), a)).as("insert branch, and a named key is dropped").isEqualTo(
            incoming());
    }

    @Test
    void appendMany_merge_bothBranches() throws Exception {
        Fx f = fixture("apm");
        String a = seed(f, "apm");
        Map<String, Object> d = new LinkedHashMap<>();
        d.put("doc_id", f.docId());
        d.put("rows", List.of(row(0, a)));

        svc.appendManyCombined(TENANT, f.collection(), List.of(d), List.of(chunkM(a, "apm a", incoming())),
            false, null, new MetadataMode(true, List.of("stale")));
        assertThat(storedMeta(f.collection(), a)).isEqualTo(
            meta("content_hash", "v2", "section", "new", "bib_year", 2020));

        svc.appendManyCombined(TENANT, f.collection(), List.of(d), List.of(chunkM(a, "apm a", meta("content_hash", "v3"))),
            true, null, new MetadataMode(true, List.of()));
        assertThat(storedMeta(f.collection(), a)).isEqualTo(
            meta("content_hash", "v3", "section", "new", "bib_year", 2020));
    }

    @Test
    void merge_isEchoedOnAllThreeRoutes_andReplaceIsNot() throws Exception {
        Fx f = fixture("echo");
        String a = seed(f, "echo");
        var mode = new MetadataMode(true, List.of());
        Map<String, Object> d = new LinkedHashMap<>();
        d.put("doc_id", f.docId());
        d.put("rows", List.of(row(0, a)));

        assertThat(svc.writeManyCombined(TENANT, f.collection(), List.of(chunkM(a, "echo a", incoming())),
            List.of(doc(f.docId(), List.of(row(0, a)))), null, false, false, null, mode).response())
            .containsEntry("metadata_merge", true);
        assertThat(svc.appendCombined(TENANT, f.collection(), f.docId(), List.of(row(0, a)),
            List.of(chunkM(a, "echo a", incoming())), false, null, null, mode).response())
            .containsEntry("metadata_merge", true);
        assertThat(svc.appendManyCombined(TENANT, f.collection(), List.of(d),
            List.of(chunkM(a, "echo a", incoming())), false, null, mode).response())
            .containsEntry("metadata_merge", true);
        assertThat(svc.writeManyCombined(TENANT, f.collection(), List.of(chunkM(a, "echo a", incoming())),
            List.of(doc(f.docId(), List.of(row(0, a)))), null, false, false).response())
            .doesNotContainKey("metadata_merge");
    }

    @Test
    void merge_doesNotOverwriteAMetadataWriteThatLandsBetweenTheExistenceCheckAndTheInsert() throws Exception {
        Fx f = fixture("race");
        String a = seed(f, "race");
        // Between phase 2a (the existence partition) and the per-doc INSERT, another writer
        // (nx enrich, update_chunks) sets bib_year. force_re_embed sends the chunk down the
        // insert branch, whose ON CONFLICT must merge over the row's CURRENT value.
        svc.setAfterNeedEmbedResolvedHookForTests(() -> {
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                DSL.using(su, SQLDialect.POSTGRES).update(CHUNKS)
                    .set(CHUNKS.METADATA, DSL.function("jsonb_concat", org.jooq.JSONB.class, CHUNKS.METADATA,
                        DSL.val(org.jooq.JSONB.jsonb("{\"bib_year\":1999}"))))
                    .where(CHUNKS.TENANT_ID.eq(TENANT))
                    .and(CHUNKS.COLLECTION.eq(f.collection()))
                    .and(CHUNKS.CHASH.eq(HexFormat.of().parseHex(a)))
                    .execute();
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        });
        try {
            svc.writeManyCombined(TENANT, f.collection(), List.of(chunkM(a, "race a", incoming())),
                List.of(doc(f.docId(), List.of(row(0, a)))), null, false, true, null,
                new MetadataMode(true, List.of("stale")));
        } finally {
            svc.setAfterNeedEmbedResolvedHookForTests(null);
        }

        assertThat(storedMeta(f.collection(), a)).isEqualTo(
            meta("content_hash", "v2", "section", "new", "bib_year", 1999));
    }

    @Test
    void deleteKeysWithoutMerge_isRefused() {
        assertThatThrownBy(() -> new MetadataMode(false, List.of("x")))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("metadata_merge");
    }
}
