// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.datamove.Gate3;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.CombinedWriteService;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.EmbedderRouter;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.TaxonomyCentroidRepository;
import dev.nexus.service.vectors.TaxonomyCentroidRepository.CentroidRecord;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Named;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.MethodSource;

import java.sql.Connection;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.stream.Stream;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25), test first: the engine's three upserts that conflict on a collection name key take the id key
 * as their conflict target from gate 3 on the install (RDR item 6, "Readers during the stamp"; P3.5c step), and go back to the
 * name key when gate 3 stops holding (linda 2026-11-22).
 *
 * <p>The three upserts, each a subject of every test below:
 * <ul>
 *   <li>chunks: {@code PgVectorRepository#upsertChunks} ({@code PgVectorRepository.java:1323}), conflict {@code (tenant_id,
 *       collection, chash, embedding_model)};</li>
 *   <li>centroids: {@code TaxonomyCentroidRepository#upsertCentroids} ({@code :169}), conflict {@code (tenant_id, collection,
 *       topic_id, embedding_model)};</li>
 *   <li>manifest chunks: {@code CatalogRepository#upsertManifestChunkVectors} ({@code CatalogRepository.java:6753}), a private
 *       static method that the public {@code writeManifestMany} (through {@code writeManifestRows}) and {@code
 *       appendManifestChunks} (through {@code appendOneDocumentTx}) reach; driven here through {@code
 *       CombinedWriteService#writeManyCombined}, which owns the embed and calls {@code writeManifestMany}.</li>
 * </ul>
 * From gate 3 each conflicts on the id key, {@code (tenant_id, collection_id, chash | topic_id, embedding_model)}: a write that
 * matches an existing row by {@code collection_id} under a different name updates that row; a second row for one collection id
 * is never inserted. When gate 3 stops holding (an id index went INVALID) the next write arbitrates on the name key again.
 *
 * <p><b>This file does not compile until P3.5c adds the constructors below</b> (accepted on this feature branch: they stay red
 * until the code exists). Every call that takes a {@link Gate3} is in one of the three static factories at the foot of the
 * class, so the compile error is in those three places only. The shapes they expect, each storing the gate and reading
 * {@link Gate3#holds()} on every upsert, never caching it:
 * <pre>{@code
 *   new PgVectorRepository(TenantScope, Embedder docEmbedder, Embedder queryEmbedder, Gate3 gate3)
 *   new TaxonomyCentroidRepository(TenantScope, Gate3 gate3)
 *   new CatalogRepository(TenantScope, Gate3 gate3)
 * }</pre>
 *
 * <p><b>Design point the manifest subject surfaces.</b> A manifest row references its chunk by the name foreign key {@code
 * fk_catalog_chunks_chunk} until P3.7 drops it. The id-key arm updates the row that names A and carries B's id; the manifest row
 * the same write inserts names B. So the id-key arm's DO UPDATE must also set {@code collection} to the writer's name, or the
 * foreign key refuses the manifest row and the write fails. The id-key test requires the write to succeed, which holds only if
 * the arm does that. (The other two upserts are free to do the same; no test asserts the surviving name.)
 *
 * <p>Fixture. The state is the one a name-path rename leaves in the window the RDR names: the registry holds two names A and B,
 * B carries id Y, and a stored row names A (so the name foreign key holds) and id Y. A write for B resolves to Y and finds that
 * row by the id key only. The id unique key (P3.3b/P3.3c build it per leaf; this tree has neither) is created by the id-key and
 * flip tests over the partitioned parent and dropped again, so the guards keep the name key alone as their arbiter.
 *
 * <p>Tests: guards (pass once the constructors exist, and the name key is today's behaviour), id-key (fail until the conflict
 * target moves), flip (gate 3 true, then false: the second write must arbitrate as the guard does).
 */
class Rdr229P35cUpsertConflictKeyTest extends AtomicWriteTestBase {

    private static final String MODEL = "minilm-l6-v2-384";
    private static final String CHUNK_ID_KEY = "p35c_chunks_id_key";
    private static final String CENTROID_ID_KEY = "p35c_centroids_id_key";

    // ---- the one place the not-yet-existing constructors are called --------------------------------------------

    private static PgVectorRepository pgVectorRepository(TenantScope scope, Embedder embedder, Gate3 gate3) {
        return new PgVectorRepository(scope, embedder, embedder, gate3);
    }

    private static TaxonomyCentroidRepository taxonomyCentroidRepository(TenantScope scope, Gate3 gate3) {
        return new TaxonomyCentroidRepository(scope, gate3);
    }

    private static CatalogRepository catalogRepository(TenantScope scope, Gate3 gate3) {
        return new CatalogRepository(scope, gate3);
    }

    // ---- fixture -----------------------------------------------------------------------------------------------

    private String register(String tag) {
        String c = "code__p35c" + tag + seq.incrementAndGet() + "__" + MODEL + "__v1";
        registerCollection(c);
        return c;
    }

    private Long registryId(String name) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(name))
                .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
        }
    }

    private void withIdKeys(ThrowingRunnable body) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.createUniqueIndexIfNotExists(CHUNK_ID_KEY)
                .on(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION_ID, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL).execute();
            ctx.createUniqueIndexIfNotExists(CENTROID_ID_KEY)
                .on(TAXONOMY_CENTROIDS, TAXONOMY_CENTROIDS.TENANT_ID, TAXONOMY_CENTROIDS.COLLECTION_ID,
                    TAXONOMY_CENTROIDS.TOPIC_ID, TAXONOMY_CENTROIDS.EMBEDDING_MODEL).execute();
            try {
                body.run();
            } finally {
                ctx.dropIndexIfExists(DSL.name("nexus", CHUNK_ID_KEY)).execute();
                ctx.dropIndexIfExists(DSL.name("nexus", CENTROID_ID_KEY)).execute();
            }
        }
    }

    @FunctionalInterface
    private interface ThrowingRunnable {
        void run() throws Exception;
    }

    // ---- the three upserts, behind one shape -------------------------------------------------------------------

    /** One upsert under test. A key names one row (a chash for chunks, a topic for centroids); a text is the written value. */
    private abstract class Upsert {
        /** A key no other test of this class uses. */
        abstract String newKey();

        /** Writes {@code key} into {@code collection} with {@code gate3} on the repository that owns the upsert. */
        abstract void write(Gate3 gate3, String collection, String key, String text);

        /** Stores a row named {@code a} for {@code key}, carrying the registry id of {@code b}. */
        abstract void seedNamedAWithIdOfB(String a, String b, String key, String text) throws Exception;

        /** The rows of (tenant, collection id, key) as (name, text). */
        abstract List<Map.Entry<String, String>> rowsOfId(Long id, String key) throws Exception;

        /** Removes what every write of {@code key} left, so a later id unique key can be built. */
        abstract void delete(String key) throws Exception;
    }

    private class Chunks extends Upsert {
        @Override
        String newKey() {
            return Chash.ofText("p35c chunk " + seq.incrementAndGet()).toHex();
        }

        @Override
        void write(Gate3 gate3, String collection, String key, String text) {
            pgVectorRepository(tenantScope, embedder, gate3)
                .upsertChunks(TENANT, collection, List.of(key), List.of(text), List.of(Map.of()), true);
        }

        @Override
        void seedNamedAWithIdOfB(String a, String b, String key, String text) throws Exception {
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                PgContainerHelper.insertChunks(ctx, TENANT, a, List.of(key), List.of(text), List.of(new float[384]),
                    List.of(Map.of()));
                int n = ctx.update(CHUNKS).set(CHUNKS.COLLECTION_ID, registryId(b))
                    .where(CHUNKS.TENANT_ID.eq(TENANT)).and(CHUNKS.COLLECTION.eq(a))
                    .and(CHUNKS.CHASH.eq(HexFormat.of().parseHex(key))).execute();
                assertThat(n).as("fixture: the stored chunk carries the other collection's id").isEqualTo(1);
            }
        }

        @Override
        List<Map.Entry<String, String>> rowsOfId(Long id, String key) throws Exception {
            try (Connection su = pg.createConnection("")) {
                return DSL.using(su, SQLDialect.POSTGRES).select(CHUNKS.COLLECTION, CHUNKS.CHUNK_TEXT).from(CHUNKS)
                    .where(CHUNKS.TENANT_ID.eq(TENANT)).and(CHUNKS.COLLECTION_ID.eq(id))
                    .and(CHUNKS.CHASH.eq(HexFormat.of().parseHex(key)))
                    .fetch(r -> Map.entry(r.value1(), String.valueOf(r.value2())));
            }
        }

        @Override
        void delete(String key) throws Exception {
            try (Connection su = pg.createConnection("")) {
                DSL.using(su, SQLDialect.POSTGRES).deleteFrom(CHUNKS)
                    .where(CHUNKS.TENANT_ID.eq(TENANT)).and(CHUNKS.CHASH.eq(HexFormat.of().parseHex(key))).execute();
            }
        }
    }

    /** The manifest path: the same chunk rows, written by {@code CatalogRepository} behind a document's manifest rows. */
    private class ManifestChunks extends Chunks {
        @Override
        void write(Gate3 gate3, String collection, String key, String text) {
            var catalog = catalogRepository(tenantScope, gate3);
            var combined = new CombinedWriteService(tenantScope, catalog, new EmbedderRouter(embedder, "document"));
            String docId = freshDoc("p35c", collection);
            combined.writeManyCombined(TENANT, collection, List.of(chunk(key, text)),
                List.of(doc(docId, List.of(row(0, key)))), null, false, true);
        }

        @Override
        void delete(String key) throws Exception {
            try (Connection su = pg.createConnection("")) {
                DSL.using(su, SQLDialect.POSTGRES).deleteFrom(CATALOG_DOCUMENT_CHUNKS)
                    .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT))
                    .and(CATALOG_DOCUMENT_CHUNKS.CHASH.eq(HexFormat.of().parseHex(key))).execute();
            }
            super.delete(key);
        }
    }

    private class Centroids extends Upsert {
        @Override
        String newKey() {
            return Long.toString(7_000L + seq.incrementAndGet());
        }

        @Override
        void write(Gate3 gate3, String collection, String key, String text) {
            taxonomyCentroidRepository(tenantScope, gate3)
                .upsertCentroids(TENANT, List.of(new CentroidRecord(collection, Long.parseLong(key), new float[384], text, 2)));
        }

        @Override
        void seedNamedAWithIdOfB(String a, String b, String key, String text) throws Exception {
            write(Gate3.never(), a, key, text);
            try (Connection su = pg.createConnection("")) {
                int n = DSL.using(su, SQLDialect.POSTGRES).update(TAXONOMY_CENTROIDS)
                    .set(TAXONOMY_CENTROIDS.COLLECTION_ID, registryId(b))
                    .where(TAXONOMY_CENTROIDS.TENANT_ID.eq(TENANT)).and(TAXONOMY_CENTROIDS.COLLECTION.eq(a))
                    .and(TAXONOMY_CENTROIDS.TOPIC_ID.eq(Long.parseLong(key))).execute();
                assertThat(n).as("fixture: the stored centroid carries the other collection's id").isEqualTo(1);
            }
        }

        @Override
        List<Map.Entry<String, String>> rowsOfId(Long id, String key) throws Exception {
            try (Connection su = pg.createConnection("")) {
                return DSL.using(su, SQLDialect.POSTGRES).select(TAXONOMY_CENTROIDS.COLLECTION, TAXONOMY_CENTROIDS.LABEL)
                    .from(TAXONOMY_CENTROIDS)
                    .where(TAXONOMY_CENTROIDS.TENANT_ID.eq(TENANT)).and(TAXONOMY_CENTROIDS.COLLECTION_ID.eq(id))
                    .and(TAXONOMY_CENTROIDS.TOPIC_ID.eq(Long.parseLong(key)))
                    .fetch(r -> Map.entry(r.value1(), String.valueOf(r.value2())));
            }
        }

        @Override
        void delete(String key) throws Exception {
            try (Connection su = pg.createConnection("")) {
                DSL.using(su, SQLDialect.POSTGRES).deleteFrom(TAXONOMY_CENTROIDS)
                    .where(TAXONOMY_CENTROIDS.TENANT_ID.eq(TENANT)).and(TAXONOMY_CENTROIDS.TOPIC_ID.eq(Long.parseLong(key)))
                    .execute();
            }
        }
    }

    Stream<Arguments> upserts() {
        return Stream.of(
            Arguments.of(Named.of("chunks (PgVectorRepository:1323)", new Chunks())),
            Arguments.of(Named.of("centroids (TaxonomyCentroidRepository:169)", new Centroids())),
            Arguments.of(Named.of("manifest chunks (CatalogRepository:6753)", new ManifestChunks())));
    }

    // ---- guards: before gate 3 the key is the name -------------------------------------------------------------

    /** GUARD: with {@link Gate3#never()} a write under the SAME name updates the row in place (the name key). */
    @ParameterizedTest
    @MethodSource("upserts")
    void beforeGate3AWriteUnderTheSameNameUpdatesTheRow(Upsert u) throws Exception {
        String b = register("gb");
        String key = u.newKey();
        u.write(Gate3.never(), b, key, "first");

        u.write(Gate3.never(), b, key, "second");

        assertThat(u.rowsOfId(registryId(b), key)).as("one row for the name; the second write took it")
            .containsExactly(Map.entry(b, "second"));
        u.delete(key);
    }

    /**
     * GUARD: with {@link Gate3#never()} the arbiter is the name key and nothing else, so a write under a DIFFERENT name for the
     * same collection id inserts a second row. (No id unique key exists here: it is built only inside {@link #withIdKeys}.)
     */
    @ParameterizedTest
    @MethodSource("upserts")
    void beforeGate3AWriteUnderAnotherNameWithTheSameIdInsertsASecondRow(Upsert u) throws Exception {
        String a = register("ga");
        String b = register("gb");
        String key = u.newKey();
        u.seedNamedAWithIdOfB(a, b, key, "seed");
        try {
            u.write(Gate3.never(), b, key, "replacement");

            assertThat(u.rowsOfId(registryId(b), key))
                .as("the name key (B, key) has no row, so the write inserts; two rows now carry one collection id")
                .hasSize(2);
        } finally {
            u.delete(key);
        }
    }

    // ---- from gate 3: the key is the id ------------------------------------------------------------------------

    /**
     * FAILS until the conflict target moves: with gate 3 held and the id unique key built, the write for B finds the row that
     * names A and carries B's id, and updates it. One row for the collection id, holding the new text.
     */
    @ParameterizedTest
    @MethodSource("upserts")
    void fromGate3AWriteMatchingByIdUpdatesThatRowInsteadOfInsertingASecond(Upsert u) throws Exception {
        String a = register("ia");
        String b = register("ib");
        String key = u.newKey();
        u.seedNamedAWithIdOfB(a, b, key, "seed");
        Long idOfB = registryId(b);

        withIdKeys(() -> u.write(() -> true, b, key, "replacement text"));

        var rows = u.rowsOfId(idOfB, key);
        assertThat(rows).as("the id key matched the existing row: still one row for (collection id, key)").hasSize(1);
        assertThat(rows.get(0).getValue()).as("and that row took the write").isEqualTo("replacement text");
    }

    // ---- the flip: gate 3 holds, then stops holding ------------------------------------------------------------

    /**
     * FAILS until the conflict target moves and reads the gate on every write. A {@link Gate3} backed by an {@link AtomicBoolean}:
     * with it true a write goes to the id key (as above); set false (an id index went INVALID) the next write arbitrates on the
     * name key again, exactly as the guards do. Both keys exist, so two writes tell the keys apart:
     * <ul>
     *   <li>a write whose name matches a row is a name-key conflict: that row is updated, one row remains;</li>
     *   <li>a write whose id matches a row named otherwise is NOT matched by the id key any more: the row that names A keeps
     *       its seed text (the write inserts a second row, which the live id unique key refuses, or is arbitrated by the name
     *       key alone; either way the id key did not update A's row).</li>
     * </ul>
     */
    @ParameterizedTest
    @MethodSource("upserts")
    void whenGate3StopsHoldingTheNextWriteArbitratesOnTheNameKeyAgain(Upsert u) throws Exception {
        String a = register("fa");
        String b = register("fb");
        Long idOfB = registryId(b);
        var gate = new AtomicBoolean(true);
        Gate3 gate3 = gate::get;
        String byId = u.newKey(), sameName = u.newKey(), idOnlyAfter = u.newKey();
        try {
            u.seedNamedAWithIdOfB(a, b, byId, "seed");
            withIdKeys(() -> {
                // gate 3 holds: the id key
                u.write(gate3, b, byId, "id write");
                assertThat(u.rowsOfId(idOfB, byId)).as("gate 3 true: matched by id, one row").hasSize(1)
                    .allSatisfy(r -> assertThat(r.getValue()).isEqualTo("id write"));

                // gate 3 stops holding: the name key
                gate.set(false);
                u.write(Gate3.never(), b, sameName, "same name v1");   // a row named B, carrying B's id
                u.write(gate3, b, sameName, "same name v2");
                assertThat(u.rowsOfId(idOfB, sameName)).as("gate 3 false: matched by the name, one row, updated")
                    .containsExactly(Map.entry(b, "same name v2"));

                u.seedNamedAWithIdOfB(a, b, idOnlyAfter, "seed after");
                try {
                    u.write(gate3, b, idOnlyAfter, "must not match by id");
                } catch (RuntimeException expectedWhenTheIdKeyRefusesTheSecondRow) {
                    // the name key finds no row, so the INSERT meets the id unique key: 23505, the statement rolls back
                }
                assertThat(u.rowsOfId(idOfB, idOnlyAfter)).as("gate 3 false: the id key no longer updates the row that names A")
                    .contains(Map.entry(a, "seed after"));
            });
        } finally {
            u.delete(byId);
            u.delete(sameName);
            u.delete(idOnlyAfter);
        }
    }
}
