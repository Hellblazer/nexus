// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.db.Chash;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.net.http.HttpResponse;
import java.sql.Connection;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;

/**
 * RDR-229 P3.1 stage 2b, chunks family (nexus-p2m1x.17): the production writers of {@code nexus.chunks} in
 * {@code PgVectorRepository} and {@code ChunkKeySweep} set {@code collection_id} beside {@code collection}.
 *
 * <p>{@code chunks} carries two NOT VALID CHECKs ({@code chunks_collection_id_check}, {@code chunks_collection_id_not_null}),
 * and a NOT VALID CHECK is evaluated on every row version an INSERT or UPDATE writes. So an INSERT must name the id, and
 * an UPDATE that names no collection column must set it too: a chunk the P3.1 stamp has not reached (collection set,
 * {@code collection_id} NULL) is refused with 23514 by any UPDATE that leaves the id alone. The unstamped state is built
 * the way an upgraded install has it: the two CHECKs are rolled back with the changeset's own {@code <rollback>}, the id is
 * set NULL, and the changeset's forward text adds them again NOT VALID.
 */
class Rdr229P31ChunkWriterDualWriteTest extends ChunkKeysTestBase {

    private static final String CHANGELOG = "db/changelog/vectors-068-collection-id-expand.xml";
    private static final String CHUNK_CHECKS = "vectors-068-3";
    private static final String T = "p31-chunk-writers";

    // ---- fixture -----------------------------------------------------------------------------------------------

    private String newCollection(String tag) throws Exception {
        String c = conformant("p31w" + tag + seq.incrementAndGet());
        register(T, c);
        return c;
    }

    private Long registryId(String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.NAME.eq(collection))
                .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
        }
    }

    private Long chunkId(String collection, String hex) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).select(CHUNKS.COLLECTION_ID).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(T)).and(CHUNKS.COLLECTION.eq(collection))
                .and(CHUNKS.CHASH.eq(HexFormat.of().parseHex(hex)))
                .fetchOne(CHUNKS.COLLECTION_ID);
        }
    }

    private String chunkTextOf(String collection, String hex) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).select(CHUNKS.CHUNK_TEXT).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(T)).and(CHUNKS.COLLECTION.eq(collection))
                .and(CHUNKS.CHASH.eq(HexFormat.of().parseHex(hex)))
                .fetchOne(CHUNKS.CHUNK_TEXT);
        }
    }

    /** Puts every chunk of {@code collection} in the state a pre-stamp row has: the name set, the id NULL, the CHECKs NOT VALID. */
    private void unstamp(String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.replayChangesetRollback(su, CHANGELOG, CHUNK_CHECKS);
            DSL.using(su, SQLDialect.POSTGRES).update(CHUNKS).setNull(CHUNKS.COLLECTION_ID)
                .where(CHUNKS.TENANT_ID.eq(T)).and(CHUNKS.COLLECTION.eq(collection)).execute();
            PgContainerHelper.replayChangeset(su, CHANGELOG, CHUNK_CHECKS);
        }
    }

    /** The chash of {@code text}; the repository writes the chunk, no manifest row is needed or possible before it. */
    private static String owned(String collection, String text) {
        return Chash.ofText(text).toHex();
    }

    private void upsert(String collection, String hex, String text, boolean forceReEmbed) {
        vectors.upsertChunks(T, collection, List.of(hex), List.of(text), List.of(Map.of()), forceReEmbed);
    }

    // ---- PgVectorRepository ------------------------------------------------------------------------------------

    @Test
    void upsertChunksInsertsAChunkCarryingTheRegistryId() throws Exception {
        String c = newCollection("ins");
        String hex = owned(c, "insert me " + seq.incrementAndGet());

        upsert(c, hex, "insert me " + seq.get(), false);

        assertThat(chunkId(c, hex)).as("the chunk row names its collection's registry id").isNotNull().isEqualTo(registryId(c));
    }

    @Test
    void theConflictArmOfUpsertChunksStampsAnUnstampedChunk() throws Exception {
        String c = newCollection("cfl");
        String text = "conflict " + seq.incrementAndGet();
        String hex = owned(c, text);
        upsert(c, hex, text, false);
        unstamp(c);
        assertThat(chunkId(c, hex)).as("fixture: the chunk is unstamped").isNull();

        upsert(c, hex, text, true);   // force_re_embed: the ON CONFLICT ... DO UPDATE arm

        assertThat(chunkId(c, hex)).as("the DO UPDATE arm writes the id, or the NOT VALID CHECK refuses the row")
            .isEqualTo(registryId(c));
    }

    @Test
    void theHaveVectorBranchOfUpsertChunksStampsAnUnstampedChunk_batchUpdateMetadata() throws Exception {
        String c = newCollection("hv");
        String text = "have vector " + seq.incrementAndGet();
        String hex = owned(c, text);
        upsert(c, hex, text, false);
        unstamp(c);

        upsert(c, hex, text, false);   // same text, stored vector: a metadata-only UPDATE, no embed

        assertThat(chunkId(c, hex)).isEqualTo(registryId(c));
    }

    @Test
    void updateMetadataStampsAnUnstampedChunk() throws Exception {
        String c = newCollection("um");
        String text = "update metadata " + seq.incrementAndGet();
        String hex = owned(c, text);
        upsert(c, hex, text, false);
        unstamp(c);

        int n = vectors.updateMetadata(T, c, List.of(hex), List.of(Map.of("frecency_score", 0.5)));

        assertThat(n).as("the one chunk row was updated").isEqualTo(1);
        assertThat(chunkId(c, hex)).isEqualTo(registryId(c));
    }

    @Test
    void anUpdateOfAStampedChunkKeepsItsId() throws Exception {
        String c = newCollection("keep");
        String text = "stamped " + seq.incrementAndGet();
        String hex = owned(c, text);
        upsert(c, hex, text, false);
        Long id = chunkId(c, hex);

        vectors.updateMetadata(T, c, List.of(hex), List.of(Map.of("frecency_score", 0.25)));

        assertThat(id).isNotNull();
        assertThat(chunkId(c, hex)).isEqualTo(id);
    }

    // ---- ChunkKeySweep -----------------------------------------------------------------------------------------

    @Test
    void theChunkKeySweepRewritesAnUnstampedRowAndStampsItsId() throws Exception {
        String c = conformant("p31sweep" + seq.incrementAndGet());
        Map<String, Object> retired = keyed(T, c, "legacy-1", "title", "a retired key", "tags", "a,b");
        List<String> hex = seed(T, c, Map.of("legacy-1", retired));
        unstamp(c);
        assertThat(chunkId(c, hex.get(0))).as("fixture: unstamped").isNull();
        String token = tenantToken(T);

        HttpResponse<String> r = sweep(token, plan("dry_run", false, "accept", accept("generated_nulls", 1)));

        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        assertThat(json(r).path("rows_swept").asLong()).as("the one retired-key row was rewritten").isEqualTo(1);
        assertThat(metadataOf(T, c, hex.get(0))).as("the retired keys are gone").doesNotContainKeys("title", "tags");
        assertThat(chunkId(c, hex.get(0))).as("the sweep's UPDATE sets the id").isEqualTo(registryId(c));
    }
}
