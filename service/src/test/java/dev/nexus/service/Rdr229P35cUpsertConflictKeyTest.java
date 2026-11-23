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
 * name key never: gate 3 is the latched, monotone value (linda 2026-11-23), so there is no return to the name key.
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
 * is never inserted. Gate 3 never stops holding once latched, so no test returns a write to the name key.
 *
 * <p><b>This file does not compile until P3.5c adds the constructors below</b> (accepted on this feature branch: they stay red
 * until the code exists). Every call that takes a {@link Gate3} is in one of the three static factories at the foot of the
 * class, so the compile error is in those three places only. The shapes they expect, each storing the gate and reading
 * {@link Gate3#holds()} on every upsert, never caching it:
 * <pre>{@code
 *   new PgVectorRepository(TenantScope, Embedder docEmbedder, Embedder queryEmbedder, Gate3 gate3)
 *   new TaxonomyCentroidRepository(TenantScope, Gate3 gate3)
 *   new CatalogRepository(TenantScope, Gate3 gate3, RenameGuard)
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
 * row by the id key only. P3.3c's per-leaf id unique indexes exist on {@code chunks} and {@code taxonomy_centroids} (the manifest
 * table has no id key until P3.3c's deferred one), so before the latch the id key is enforced though it is not the arbiter: the
 * id-key test also builds the parent id unique over the partitioned table, and drops it again.
 *
 * <p>Tests: guards (the name key is the arbiter before the latch; a write under another name for the same id is refused by the id
 * unique), the id-key arm (the conflict target is the id from the latch). There is no flip-back case: the gate is monotone.
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
        return new CatalogRepository(scope, gate3, dev.nexus.service.db.RenameGuard.open());
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

        /** True when a write the id unique refuses throws it; false when the path counts it as a failed row and returns. */
        abstract boolean refusalThrows();
    }

    private class Chunks extends Upsert {
        @Override
        boolean refusalThrows() {
            return true;
        }

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
        boolean refusalThrows() {
            return false;   // writeManifestMany reports a refused row as a failure (write_manifest_many_failures), it does not throw
        }

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
        boolean refusalThrows() {
            return true;
        }

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
     * GUARD: before the latch the arbiter is the name key, so a write under a DIFFERENT name for the same collection id finds no
     * row to update and tries to insert a second row for the id; P3.3c's id unique index refuses that (23505). A write that returns
     * the refusal as a failed row (the manifest path) leaves the stored row as it was. Never two rows for one id, never the
     * replacement taking the stored row: that is the id-key arm, and it waits for the latch.
     */
    @ParameterizedTest
    @MethodSource("upserts")
    void beforeGate3AWriteUnderAnotherNameWithTheSameIdIsRefusedByTheIdUnique(Upsert u) throws Exception {
        String a = register("ga");
        String b = register("gb");
        String key = u.newKey();
        u.seedNamedAWithIdOfB(a, b, key, "seed");
        try {
            Throwable thrown = org.assertj.core.api.Assertions.catchThrowable(() -> u.write(Gate3.never(), b, key, "replacement"));

            if (u.refusalThrows()) {
                assertThat(thrown).as("the id unique refuses the second row for the id").isNotNull();
                Throwable root = thrown;
                while (root.getCause() != null) root = root.getCause();
                assertThat(root).isInstanceOf(org.postgresql.util.PSQLException.class);
                assertThat(((org.postgresql.util.PSQLException) root).getSQLState()).as("unique_violation").isEqualTo("23505");
            } else {
                assertThat(thrown).as("the path reports the refused row as a failure and returns").isNull();
            }
            assertThat(u.rowsOfId(registryId(b), key))
                .as("one row for the id, and it is the seed: the name arbiter did not take it, the id unique refused the insert")
                .containsExactly(Map.entry(a, "seed"));
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

    // ---- no flip back: gate 3 is the LATCHED value (linda 2026-11-23) -----------------------------------------------
    //
    // It never returns to false once it holds, so no write returns to the name key. That the latched value stays true while the
    // live gate 3 goes false is pinned by PciIdGate3IntegrationTest#gate3Latches_andStaysLatchedWhileTheLiveGate3GoesFalseOnAnInvalidIdIndex_andANewReporterReadsItAgain.
}
