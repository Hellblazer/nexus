// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.ChunkReaper.RunResult;
import dev.nexus.service.ChunkReaper.Settings;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.LadderRepository;
import dev.nexus.service.db.Rdr192BackfillGate;
import dev.nexus.service.vectors.ChunkMetadataKeys;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.ReaperRepository;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-6pfgb.40 (RDR-236 Phase 3): {@code nexus.gc_restore_rereferenced} and
 * {@code nexus.gc_restore_rereferenced_bounded} strip all four quarantine keys and restore only the
 * CHUNK keys of the key table ({@link ChunkMetadataKeys#CHUNK}).
 *
 * <p>Until vectors-050 both stripped two of the four ({@code quarantined_at}, {@code origin_collection}) and
 * kept every other key, so a row the engine's reaper had quarantined (which also writes {@code quarantined_by}
 * and {@code reaper_quarantined_at}) came back half stamped, and its retired document keys came back too. The
 * Phase 4 CHECKs (quarantined_by implies quarantined_at; the allow-list) would reject that row at the restore.
 *
 * <p>The fixture is the real round trip: a manifest-less chunk with the retired document keys is moved to its
 * quarantine sibling by the real reaper, the document then names it again (the foreign key bypassed, the one way a
 * manifest row can name a chunk that is only in quarantine), and the restore function runs through
 * {@link PgVectorRepository}.
 */
class GcRestoreChunkKeysOnlyIntegrationTest extends AtomicWriteTestBase {

    private static final Clock CLOCK = Clock.fixed(Instant.parse("2026-10-01T12:00:00Z"), ZoneOffset.UTC);
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final List<String> QUARANTINE_KEYS =
        List.of("quarantined_at", "origin_collection", "quarantined_by", "reaper_quarantined_at");

    private PgVectorRepository vectors;
    private ReaperRepository store;
    private Rdr192BackfillGate gate;

    @BeforeAll
    void wire() {
        vectors = new PgVectorRepository(tenantScope, embedder, embedder);
        store = new ReaperRepository(tenantScope);
        gate = new Rdr192BackfillGate(new LadderRepository(tenantScope));
    }

    private record Fixture(String tenant, String origin, String quarantine, String chash) {}

    /**
     * One chunk with the retired document keys, quarantined by the real reaper, then re-referenced by a live
     * document of the origin. Asserts the fixture is what it claims: all four quarantine keys on the quarantined
     * row (so a stripped result cannot be a fixture that never carried them) and the chunk gone from the origin.
     */
    private Fixture reaperQuarantinedThenReReferenced(String slug) throws Exception {
        int n = seq.incrementAndGet();
        String tenant = "gk" + n;
        String origin = "code__gk" + n + "__minilm-l6-v2-384__v1";
        String quarantine = "quarantine-" + origin;
        String doc = "gk." + n;
        String chash = Chash.ofText(origin + "/" + slug).toHex();
        String text = slug + " text";
        Map<String, Object> legacy = new LinkedHashMap<>();
        legacy.put("title", slug);
        legacy.put("catalog_doc_id", doc);
        legacy.put("doc_id", doc);
        legacy.put("chunk_index", 0);
        legacy.put("content_hash", "h-" + slug);
        legacy.put("tags", "a,b");
        legacy.put("section_title", "s-" + slug);
        legacy.put("indexed_at", "2026-01-01T00:00:00+00:00");
        legacy.put("chunk_text_hash", "stale-value-that-is-not-the-chash");
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, tenant, origin);
            PgContainerHelper.insertChunks(ctx, tenant, origin, List.of(chash), List.of(text),
                embedder.embed(List.of(text)), List.of(legacy));
        }
        new LadderRepository(tenantScope).record(tenant, Rdr192BackfillGate.RUNG_NAME, "7.99.0", "");
        RunResult run = new ChunkReaper(store, vectors, repo, gate, () -> List.of(tenant), Settings.defaults(), CLOCK)
            .runOnce(Duration.ZERO);
        assertThat(run.tenant(tenant).collection(origin).moved()).as("fixture: the reaper moved it").isEqualTo(1);
        assertThat(metadataOf(tenant, quarantine, chash).keySet())
            .as("fixture: the reaper's move wrote all four quarantine keys")
            .containsAll(QUARANTINE_KEYS);
        assertThat(metadataOf(tenant, quarantine, chash)).as("fixture: and kept the retired document keys")
            .containsKeys("title", "catalog_doc_id", "doc_id", "chunk_index", "content_hash", "tags");

        repo.upsertDocument(tenant, Map.of(
            "tumbler", doc, "title", slug, "content_type", "code", "corpus", "code",
            "physical_collection", origin, "chunk_count", 1));
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.dropCatalogChunksFk(su);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                    CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                    CATALOG_DOCUMENT_CHUNKS.COLLECTION, CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL)
               .values(tenant, doc, 0, Chash.fromHex(chash).toBytes(), origin,
                    PgContainerHelper.collectionModel(ctx, tenant, origin)).execute();
            PgContainerHelper.readdCatalogChunksFkNotValid(su);
        }
        assertThat(exists(tenant, origin, chash)).as("fixture: only the quarantine copy exists").isFalse();
        return new Fixture(tenant, origin, quarantine, chash);
    }

    @Test
    void theUnboundedRestoreOfAReaperQuarantinedRowLeavesNoQuarantineKeyAndOnlyTheChunkKeys() throws Exception {
        Fixture f = reaperQuarantinedThenReReferenced("unbounded");

        assertThat(vectors.restoreRereferenced(f.tenant(), f.quarantine(), f.origin())).isEqualTo(1L);

        assertRestoredToChunkKeysOnly(f);
    }

    @Test
    void theBoundedRestoreOfAReaperQuarantinedRowLeavesNoQuarantineKeyAndOnlyTheChunkKeys() throws Exception {
        Fixture f = reaperQuarantinedThenReReferenced("bounded");

        var out = vectors.restoreRereferencedBounded(f.tenant(), f.quarantine(), f.origin(), 10);
        assertThat(out.restored()).isEqualTo(1L);
        assertThat(out.remaining()).isZero();

        assertRestoredToChunkKeysOnly(f);
    }

    /** The row that came back: every quarantine key gone, nothing but the CHUNK keys, both equal to the columns. */
    private void assertRestoredToChunkKeysOnly(Fixture f) throws Exception {
        assertThat(exists(f.tenant(), f.quarantine(), f.chash())).as("the quarantine copy is gone").isFalse();
        Map<String, Object> meta = metadataOf(f.tenant(), f.origin(), f.chash());
        assertThat(meta.keySet()).as("no quarantine key survives the restore").doesNotContainAnyElementsOf(QUARANTINE_KEYS);
        assertThat(meta.keySet()).as("only the CHUNK keys of the key table come back, no retired document key")
            .isEqualTo(ChunkMetadataKeys.CHUNK);
        assertThat(meta.get("chunk_text_hash"))
            .as("chunk_text_hash is the chash column's hex, whatever the quarantined row held")
            .isEqualTo(f.chash());
        assertThat(meta.get("embedding_model"))
            .as("embedding_model is the column's value")
            .isEqualTo(embeddingModelColumn(f.tenant(), f.origin(), f.chash()));
    }

    // ── reads (typed jOOQ over a superuser connection) ───────────────────────────

    @SuppressWarnings("unchecked")
    private Map<String, Object> metadataOf(String tenant, String collection, String hex) throws Exception {
        try (Connection su = pg.createConnection("")) {
            String json = DSL.using(su, SQLDialect.POSTGRES)
                .select(DSL.field(DSL.name("chunks", "metadata"), String.class))
                .from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(collection))
                       .and(CHUNKS.CHASH.eq(Chash.fromHex(hex).toBytes())))
                .fetchOne(0, String.class);
            return MAPPER.readValue(json, Map.class);
        }
    }

    private String embeddingModelColumn(String tenant, String collection, String hex) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES)
                .select(CHUNKS.EMBEDDING_MODEL).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(collection))
                       .and(CHUNKS.CHASH.eq(Chash.fromHex(hex).toBytes())))
                .fetchOne(0, String.class);
        }
    }

    private boolean exists(String tenant, String collection, String hex) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).fetchExists(DSL.selectOne().from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(collection))
                       .and(CHUNKS.CHASH.eq(Chash.fromHex(hex).toBytes()))));
        }
    }
}
