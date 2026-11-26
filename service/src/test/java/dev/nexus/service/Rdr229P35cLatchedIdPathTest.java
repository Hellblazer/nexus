// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.CombinedWriteService;
import dev.nexus.service.db.RenameGuard;
import dev.nexus.service.vectors.EmbedderRouter;
import org.jooq.DSLContext;
import org.jooq.Record;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25), the test-strength follow-ups of the census evidence pass (bead comment of 2026-11-23 21:09, items 1 and
 * 2): two readers whose verdict "reads the id from the latch" held by reading, with no test that fails when the id path is reverted to the
 * name.
 *
 * <p>Both start from the state P3.7's one-row rename leaves: the registry row bears a new label and keeps its id, while the chunk and the
 * manifest rows keep the OLD name and the same id. A reader that matches by the name the caller gives (the new label) then finds nothing, or
 * sees a difference that is not one; a reader that matches by id finds the collection's own rows. Every test latches in process on the same
 * repository, and each carries the unlatched run as its control, so the state is proved to be one the name path misreads.
 *
 * <p>The combined write's own gate is the four-argument constructor's: the three-argument one stands for {@code Gate3.never()}.
 */
class Rdr229P35cLatchedIdPathTest extends AtomicWriteTestBase {

    private static final ObjectMapper JSON = new ObjectMapper();

    private final AtomicBoolean latched = new AtomicBoolean();
    private CatalogRepository gated;
    private CombinedWriteService gatedSvc;
    private CombinedWriteService latchedSvc;

    @BeforeAll
    void gatedRepository() throws Exception {
        gated = new CatalogRepository(tenantScope, latched::get, RenameGuard.open());
        gatedSvc = new CombinedWriteService(tenantScope, gated, new EmbedderRouter(embedder, "document"));
        latchedSvc = new CombinedWriteService(tenantScope, gated, new EmbedderRouter(embedder, "document"), latched::get);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext dsl = DSL.using(su, SQLDialect.POSTGRES);
            RegistryRenameSupport.dropForeignKeysOntoTheRegistry(dsl);
            // the manifest's key onto its chunk's NAME goes too (P3.7's name-key drop): the unlatched controls below write the new label
            PgContainerHelper.dropCatalogChunksFk(su);
            // the id arbiter P3.3c builds, over the partitioned parent: a latched chunk upsert needs it
            dsl.createUniqueIndexIfNotExists("p35c_latched_chunks_id_key")
                .on(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION_ID, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL).execute();
        }
    }

    /** A collection with one document holding one chunk and a manifest row, then renamed in place: the old name is on every child row. */
    private record Renamed(String oldLabel, String newLabel, String doc, String chash, String text, long id) {}

    private Renamed renamedCollection() throws Exception {
        latched.set(false);   // the fixture is written before the latch; each test then latches
        Fx fx = fixture("lip");
        String chash = ch("p35c latched id path " + seq.incrementAndGet());
        String text = "latched id path text " + chash;
        var written = gatedSvc.writeManyCombined(TENANT, fx.collection(), List.of(chunk(chash, text)),
            List.of(doc(fx.docId(), List.of(row(0, chash)))), null, false, true);
        assertThat(chunkExists(fx.collection(), chash)).as("fixture: the chunk is stored; " + written.response()).isTrue();
        assertThat(manifestChashes(fx.docId())).as("fixture: the manifest row is stored").contains(chash);
        String newLabel = "code__awlipnew" + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
        long id;
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext dsl = DSL.using(su, SQLDialect.POSTGRES);
            id = dsl.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(fx.collection()))
                .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
            RegistryRenameSupport.rename(dsl, TENANT, fx.collection(), newLabel);
        }
        return new Renamed(fx.collection(), newLabel, fx.docId(), chash, text, id);
    }

    // ---- item 1: ManifestReplace.same() compares the collection by id from the latch -------------------------------------

    /** The physical tuple (ctid and xmin) of every manifest row of the document, by position: it changes when a row is rewritten. */
    private Map<Integer, String> tuples(String docId) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ctid = DSL.field(DSL.name("ctid")).cast(SQLDataType.VARCHAR);
            var xmin = DSL.field(DSL.name("xmin")).cast(SQLDataType.VARCHAR);
            Map<Integer, String> out = new LinkedHashMap<>();
            for (Record r : DSL.using(su, SQLDialect.POSTGRES).select(CATALOG_DOCUMENT_CHUNKS.POSITION).select(ctid, xmin)
                    .from(CATALOG_DOCUMENT_CHUNKS)
                    .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(docId)))
                    .orderBy(CATALOG_DOCUMENT_CHUNKS.POSITION).fetch()) {
                out.put(r.get(CATALOG_DOCUMENT_CHUNKS.POSITION), r.get(ctid) + "/" + r.get(xmin));
            }
            return out;
        }
    }

    /**
     * Guards {@code ManifestReplace.same}: {@code (gate3.holds() || Objects.equals(old.collection(), collection))}. A manifest replace
     * with the rows the document already holds writes nothing. After a one-row rename the stored rows still carry the old name, so a
     * comparison by name (the new label against the old stored name) calls every row changed and rewrites it, though the id, the only
     * identity from the latch, is unchanged.
     */
    @Test
    void aLatchedManifestReplaceWithTheSameRowsOverARenamedCollectionRewritesNothing() throws Exception {
        Renamed control = renamedCollection();
        latched.set(false);
        var controlBefore = tuples(control.doc());
        gated.writeManifest(TENANT, control.doc(), control.newLabel(), List.of(row(0, control.chash())));
        assertThat(tuples(control.doc())).as("control, unlatched: the name path sees the old stored name as a difference and rewrites the row")
            .isNotEqualTo(controlBefore);

        Renamed r = renamedCollection();
        var before = tuples(r.doc());
        assertThat(before).as("fixture: one manifest row").hasSize(1);
        latched.set(true);

        gated.writeManifest(TENANT, r.doc(), r.newLabel(), List.of(row(0, r.chash())));

        assertThat(tuples(r.doc())).as("latched: the stored row names the same collection id, so it is the same row, left in place")
            .isEqualTo(before);
    }

    // ---- item 2: CombinedWriteService finds the existing chunk by id, so a rewrite of its metadata embeds nothing ---------

    private String metadataKey(long collectionId, String hex, String key) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var data = DSL.using(su, SQLDialect.POSTGRES).select(CHUNKS.METADATA).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(TENANT)).and(CHUNKS.COLLECTION_ID.eq(collectionId))
                .and(CHUNKS.CHASH.eq(HexFormat.of().parseHex(hex)))
                .fetchOne(CHUNKS.METADATA);
            return data == null ? null : JSON.readTree(data.data()).path(key).asText(null);
        }
    }

    /**
     * Guards {@code CombinedWriteService.selectExistingText} and the metadata refresh behind it ({@code batchUpdateMetadata}): the same chunk
     * written again with the same text and new metadata is an existing chunk whose metadata is refreshed. A read by the new label finds no
     * chunk (its stored name is the old one), counts the chunk as absent and embeds its text again; read by id, it finds it, so nothing is
     * embedded and the metadata lands on the same row.
     */
    @Test
    void aLatchedCombinedWriteOfAnExistingChunkOverARenamedCollectionRefreshesItsMetadataAndEmbedsNothing() throws Exception {
        Renamed control = renamedCollection();
        latched.set(false);
        int controlBefore = embedder.calls.get();
        gatedSvc.writeManyCombined(TENANT, control.newLabel(), List.of(withMetadata(control, "refreshed")),
            List.of(doc(freshDoc("lipc", control.newLabel()), List.of(row(0, control.chash())))), null, false, false);
        assertThat(embedder.calls.get() - controlBefore)
            .as("control, unlatched: the chunk is not found under the new label, so its text is embedded again").isPositive();

        Renamed r = renamedCollection();
        latched.set(true);
        int before = embedder.calls.get();

        var result = latchedSvc.writeManyCombined(TENANT, r.newLabel(), List.of(withMetadata(r, "refreshed")),
            List.of(doc(freshDoc("lip", r.newLabel()), List.of(row(0, r.chash())))), null, false, false);

        assertThat(result.response()).containsEntry("failed_doc_ids", List.of());
        assertThat(embedder.calls.get() - before).as("latched: the existing chunk is found by id; nothing is embedded").isZero();
        assertThat(metadataKey(r.id(), r.chash(), "p35c_marker")).as("the metadata landed on the collection's own chunk row")
            .isEqualTo("refreshed");
        try (Connection su = pg.createConnection("")) {
            assertThat(DSL.using(su, SQLDialect.POSTGRES).fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(TENANT)
                .and(CHUNKS.COLLECTION_ID.eq(r.id())))).as("and no second row appeared").isEqualTo(1);
        }
    }

    private static Map<String, Object> withMetadata(Renamed r, String marker) {
        Map<String, Object> c = new LinkedHashMap<>(chunk(r.chash(), r.text()));
        c.put("metadata", Map.of("p35c_marker", marker));
        return c;
    }
}
