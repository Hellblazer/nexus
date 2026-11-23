// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.AtomicWriteTestBase;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.RegistryRenameSupport;
import dev.nexus.service.datamove.Gate3;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.function.Function;

/**
 * RDR-229 P3.7's prerequisite D1 (nexus-p2m1x.25): from gate 3 {@code DepartedWriterSpans.clearDisplaced} keys the pair a document still
 * holds, and the pair its upsert displaced, by (collection_id, chash), never (label, chash).
 *
 * <p>A document D held chunk H at position 1 and now holds H2 there, and still holds H at position 0. After a one-row rename the
 * manifest rows keep the old label while the displaced pair was read under the new one. Keyed by (label, chash), D's held pair
 * (old, H) does not match the displaced pair (new, H), D looks to have dropped H, and H's spans are cleared though D still holds
 * it. Keyed by (collection_id, chash) the pair matches and nothing is cleared. A second document E holds H with no span, so the clear
 * has work to do whenever it is reached; the control (D holds another chunk at position 0, not H) shows it is.
 */
class Rdr229P37ClearDisplacedByIdTest extends AtomicWriteTestBase {

    private static final String MODEL = "minilm-l6-v2-384";
    private static final Gate3 LATCHED = () -> true;
    private static final Map<String, Object> SPAN =
        Map.of("line_start", 5, "line_end", 9, "chunk_start_char", 50, "chunk_end_char", 90);

    @BeforeAll
    void theNameKeysGo() throws Exception {
        su(d -> {
            RegistryRenameSupport.dropForeignKeysOntoTheRegistry(d);
            return null;
        });
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            PgContainerHelper.dropCatalogChunksFk(c);
        }
    }

    private <T> T su(Function<DSLContext, T> body) throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            return body.apply(DSL.using(c, SQLDialect.POSTGRES));
        }
    }

    @Test
    void aDocumentThatStillHoldsTheChunkUnderAStaleLabelKeepsItsSpans_butNotWhenItDroppedIt() throws Exception {
        for (boolean stillHeld : new boolean[] {true, false}) {
            String old = "code__p37d1" + stillHeld + seq.incrementAndGet() + "__" + MODEL + "__v1";
            String neu = old.replaceFirst("^code__p37d1", "code__p37d1n");
            registerCollection(old);
            long id = su(d -> d.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(old))
                .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID));
            String h1 = Chash.ofText("p37-d1-h1-" + seq.incrementAndGet()).toHex();
            String h2 = Chash.ofText("p37-d1-h2-" + seq.incrementAndGet()).toHex();
            String h3 = Chash.ofText("p37-d1-h3-" + seq.incrementAndGet()).toHex();
            List<String> all = List.of(h1, h2, h3);
            List<String> texts = new ArrayList<>();
            List<float[]> vecs = new ArrayList<>();
            List<Map<String, Object>> metas = new ArrayList<>();
            for (String h : all) {
                texts.add("p37 d1 " + h);
                float[] v = new float[384];
                v[0] = 1f;
                vecs.add(v);
                metas.add(SPAN);
            }
            String doc = "p37-d1-doc-" + seq.incrementAndGet();
            su(d -> {
                PgContainerHelper.insertChunks(d, TENANT, old, all, texts, vecs, metas);
                PgContainerHelper.ownChunks(d, TENANT, old, h1);   // E: a holder of H with no span
                PgContainerHelper.insertCatalogDocument(d, TENANT, doc);
                manifest(d, doc, 0, stillHeld ? h1 : h3, old, id);
                manifest(d, doc, 1, h2, old, id);
                RegistryRenameSupport.rename(d, TENANT, old, neu);
                return null;
            });
            var displaced = new DepartedWriterSpans.Gone(neu, HexFormat.of().parseHex(h1), 5, 9, 50, 90, id);

            int cleared = tenantScope.withTenant(TENANT, ctx ->
                DepartedWriterSpans.clearDisplaced(ctx, TENANT, doc, neu, Map.of(1, displaced), Map.of(1, h2), LATCHED));

            boolean spanLeft = su(d -> d.select(CHUNKS.METADATA).from(CHUNKS).where(CHUNKS.TENANT_ID.eq(TENANT))
                .and(CHUNKS.COLLECTION_ID.eq(id)).and(CHUNKS.CHASH.eq(HexFormat.of().parseHex(h1)))
                .fetchOne(CHUNKS.METADATA).data().contains("\"line_start\""));
            if (stillHeld) {
                assertThat(cleared).as("D still holds H at position 0, by id: nothing is cleared").isZero();
                assertThat(spanLeft).as("H keeps its span").isTrue();
            } else {
                assertThat(cleared).as("control: D no longer holds H, so the displaced span is cleared and the clear is reachable").isEqualTo(1);
                assertThat(spanLeft).as("H lost its span").isFalse();
            }
        }
    }

    private static void manifest(DSLContext d, String doc, int position, String hex, String collection, long id) {
        d.insertInto(CATALOG_DOCUMENT_CHUNKS)
            .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, TENANT).set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, doc)
            .set(CATALOG_DOCUMENT_CHUNKS.POSITION, position).set(CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX, position)
            .set(CATALOG_DOCUMENT_CHUNKS.CHASH, HexFormat.of().parseHex(hex))
            .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, collection).set(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID, id)
            .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, MODEL).execute();
    }
}
