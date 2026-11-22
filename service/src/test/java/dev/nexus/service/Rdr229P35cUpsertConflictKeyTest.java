// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.fail;

import dev.nexus.service.db.Chash;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.TaxonomyCentroidRepository;
import dev.nexus.service.vectors.TaxonomyCentroidRepository.CentroidRecord;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.lang.reflect.Method;
import java.sql.Connection;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.function.BooleanSupplier;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25), test first: the engine's two upserts take the id key as their conflict target from gate 3
 * on the install (RDR item 6, "Readers during the stamp"; P3.5c step).
 *
 * <p>{@code PgVectorRepository#upsertChunks} conflicts on {@code (tenant_id, collection, chash, embedding_model)} and
 * {@code TaxonomyCentroidRepository#upsertCentroids} on {@code (tenant_id, collection, topic_id, embedding_model)} (the name
 * keys, the primary keys). From gate 3 each conflicts on the id key, {@code (tenant_id, collection_id, chash | topic_id,
 * embedding_model)}, so a write that matches an existing row by {@code collection_id} under a different name updates that
 * row; a second row for one collection id is never inserted.
 *
 * <p>The state the tests build is the one a name-path rename leaves in the window the RDR names: the registry holds two
 * names A and B, B carries id Y, and a stored row names A (so the name foreign key holds) and id Y. A write for B resolves
 * to id Y and finds that row by the id key only. The id unique key (P3.3b/P3.3c builds it per leaf; this tree has neither)
 * is created by the test over the partitioned parent, and dropped again, so the guard tests keep the name key alone as
 * their arbiter.
 *
 * <p><b>The gate-3 seam does not exist yet.</b> {@code CompletionBootWork#latest()} holds {@code Result#gate3()} and only
 * {@code Main} reaches it; neither repository can read it. This test is written against the smallest seam the P3.5c commit
 * would add, found by reflection so that this test tree compiles without it (the seam's absence is the test's first, and
 * stated, failure):
 * <pre>{@code
 *   public void idKeyConflictTarget(java.util.function.BooleanSupplier gate3Holds)   // on both repositories; default: never
 * }</pre>
 * {@code Main} would pass {@code () -> { var r = completionCheck.latest(); return r != null && r.gate3(); }}, read on every
 * upsert so the switch takes effect with no restart (the RDR's "no restart" rule). If P3.5c names the seam otherwise, change
 * {@link #SEAM} and {@link #gate3(Object, boolean)} only.
 *
 * <p>Guards (pass today): before gate 3 a write matches by name. Id-key tests (fail today): once gate 3 holds, by id.
 */
class Rdr229P35cUpsertConflictKeyTest extends AtomicWriteTestBase {

    private static final String SEAM = "idKeyConflictTarget";
    private static final String MODEL = "minilm-l6-v2-384";
    private static final String CHUNK_ID_KEY = "p35c_chunks_id_key";
    private static final String CENTROID_ID_KEY = "p35c_centroids_id_key";

    private PgVectorRepository vectors;
    private TaxonomyCentroidRepository centroids;

    // ---- fixture -----------------------------------------------------------------------------------------------

    private void ensureRepositories() {
        if (vectors == null) {
            vectors = new PgVectorRepository(tenantScope, embedder, embedder);
            centroids = new TaxonomyCentroidRepository(tenantScope);
        }
    }

    private String register(String tag) {
        String c = "knowledge__p35c" + tag + seq.incrementAndGet() + "__" + MODEL + "__v1";
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

    /**
     * Turns the repository's id-key conflict target on or off by the proposed seam; fails, naming it, while the seam is
     * absent. The supplier is read per upsert (a constant here; Main's reads the completion check).
     */
    private static void gate3(Object repository, boolean holds) {
        Method seam;
        try {
            seam = repository.getClass().getMethod(SEAM, BooleanSupplier.class);
        } catch (NoSuchMethodException e) {
            fail("the gate-3 seam is missing: " + repository.getClass().getSimpleName() + "." + SEAM
                + "(BooleanSupplier) does not exist, so the upsert cannot learn that gate 3 holds on the install "
                + "(CompletionBootWork#latest().gate3() is reachable only from Main)");
            return;
        }
        try {
            BooleanSupplier supplier = () -> holds;
            seam.invoke(repository, supplier);
        } catch (ReflectiveOperationException e) {
            throw new AssertionError("the gate-3 seam threw", e);
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

    /**
     * A guard that leaves two rows for one collection id would make the id unique key impossible to build for the id-key tests that
     * share this database (23505 at CREATE UNIQUE INDEX), so those guards delete what they wrote.
     */
    private void deleteChunksOf(String hex) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).deleteFrom(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(TENANT)).and(CHUNKS.CHASH.eq(HexFormat.of().parseHex(hex))).execute();
        }
    }

    private void deleteCentroidsOf(long topic) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).deleteFrom(TAXONOMY_CENTROIDS)
                .where(TAXONOMY_CENTROIDS.TENANT_ID.eq(TENANT)).and(TAXONOMY_CENTROIDS.TOPIC_ID.eq(topic)).execute();
        }
    }

    // ---- chunks ------------------------------------------------------------------------------------------------

    /** The chunk rows of {@code (tenant, id)} with this chash, as (name, text). */
    private List<Map.Entry<String, String>> chunkRowsOfId(Long id, String hex) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).select(CHUNKS.COLLECTION, CHUNKS.CHUNK_TEXT).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(TENANT)).and(CHUNKS.COLLECTION_ID.eq(id))
                .and(CHUNKS.CHASH.eq(HexFormat.of().parseHex(hex)))
                .fetch(r -> Map.entry(r.value1(), String.valueOf(r.value2())));
        }
    }

    /** Seeds a chunk under {@code a}, then gives it id {@code idOfB}: the stored row names A and carries B's id. */
    private String seedChunkNamedAWithIdOfB(String a, String b, String text) throws Exception {
        String hex = Chash.ofText(text).toHex();
        vectors.upsertChunks(TENANT, a, List.of(hex), List.of(text), List.of(Map.of()), false);
        try (Connection su = pg.createConnection("")) {
            int n = DSL.using(su, SQLDialect.POSTGRES).update(CHUNKS).set(CHUNKS.COLLECTION_ID, registryId(b))
                .where(CHUNKS.TENANT_ID.eq(TENANT)).and(CHUNKS.COLLECTION.eq(a))
                .and(CHUNKS.CHASH.eq(HexFormat.of().parseHex(hex))).execute();
            assertThat(n).as("fixture: the stored chunk carries the other collection's id").isEqualTo(1);
        }
        return hex;
    }

    /** GUARD, passes today: before gate 3 the conflict target is the name key, so a write under the SAME name updates in place. */
    @Test
    void beforeGate3AChunkWriteUnderTheSameNameUpdatesTheRow() throws Exception {
        ensureRepositories();
        String a = register("ga");
        String text = "same name " + seq.incrementAndGet();
        String hex = Chash.ofText(text).toHex();
        vectors.upsertChunks(TENANT, a, List.of(hex), List.of(text), List.of(Map.of()), false);

        vectors.upsertChunks(TENANT, a, List.of(hex), List.of(text), List.of(Map.of()), true);   // force_re_embed: the DO UPDATE arm

        assertThat(chunkRowsOfId(registryId(a), hex)).as("one row for (collection, chash): the name key matched").hasSize(1);
    }

    /**
     * GUARD, passes today: before gate 3 the arbiter is the name key and nothing else, so a write under a DIFFERENT name for the
     * same collection id inserts a second row. (Run without the id unique key, which only exists inside {@link #withIdKeys}.)
     */
    @Test
    void beforeGate3AChunkWriteUnderAnotherNameWithTheSameIdInsertsASecondRow() throws Exception {
        ensureRepositories();
        String a = register("gb1");
        String b = register("gb2");
        String hex = seedChunkNamedAWithIdOfB(a, b, "name key only " + seq.incrementAndGet());

        try {
            vectors.upsertChunks(TENANT, b, List.of(hex), List.of("replacement"), List.of(Map.of()), true);

            assertThat(chunkRowsOfId(registryId(b), hex))
                .as("the name key (B, chash) has no row, so the write inserts; two rows now carry one collection id")
                .hasSize(2);
        } finally {
            deleteChunksOf(hex);
        }
    }

    /**
     * FAILS today (no seam; and once the seam exists, until the conflict target moves): with gate 3 held and the id unique key
     * on the leaf, the write for B finds the row that names A and carries B's id, and updates it. One row, the new text.
     */
    @Test
    void fromGate3AChunkWriteMatchingByIdUpdatesThatRowInsteadOfInsertingASecond() throws Exception {
        ensureRepositories();
        String a = register("ia");
        String b = register("ib");
        String hex = seedChunkNamedAWithIdOfB(a, b, "by id " + seq.incrementAndGet());
        Long idOfB = registryId(b);

        withIdKeys(() -> {
            gate3(vectors, true);
            try {
                vectors.upsertChunks(TENANT, b, List.of(hex), List.of("replacement text"), List.of(Map.of()), true);
            } finally {
                gate3(vectors, false);
            }
        });

        var rows = chunkRowsOfId(idOfB, hex);
        assertThat(rows).as("the id key matched the existing row: still one row for (collection id, chash)").hasSize(1);
        assertThat(rows.get(0).getValue()).as("and that row took the write").isEqualTo("replacement text");
    }

    // ---- centroids ---------------------------------------------------------------------------------------------

    private static final long TOPIC = 7_001L;

    private List<Map.Entry<String, String>> centroidRowsOfId(Long id, long topic) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).select(TAXONOMY_CENTROIDS.COLLECTION, TAXONOMY_CENTROIDS.LABEL)
                .from(TAXONOMY_CENTROIDS)
                .where(TAXONOMY_CENTROIDS.TENANT_ID.eq(TENANT)).and(TAXONOMY_CENTROIDS.COLLECTION_ID.eq(id))
                .and(TAXONOMY_CENTROIDS.TOPIC_ID.eq(topic))
                .fetch(r -> Map.entry(r.value1(), String.valueOf(r.value2())));
        }
    }

    private void seedCentroidNamedAWithIdOfB(String a, String b, long topic) throws Exception {
        centroids.upsertCentroids(TENANT, List.of(new CentroidRecord(a, topic, new float[384], "original", 1)));
        try (Connection su = pg.createConnection("")) {
            int n = DSL.using(su, SQLDialect.POSTGRES).update(TAXONOMY_CENTROIDS)
                .set(TAXONOMY_CENTROIDS.COLLECTION_ID, registryId(b))
                .where(TAXONOMY_CENTROIDS.TENANT_ID.eq(TENANT)).and(TAXONOMY_CENTROIDS.COLLECTION.eq(a))
                .and(TAXONOMY_CENTROIDS.TOPIC_ID.eq(topic)).execute();
            assertThat(n).as("fixture: the stored centroid carries the other collection's id").isEqualTo(1);
        }
    }

    /** GUARD, passes today: before gate 3 a centroid write under the same name updates in place (the name key). */
    @Test
    void beforeGate3ACentroidWriteUnderTheSameNameUpdatesTheRow() throws Exception {
        ensureRepositories();
        String a = register("ca");
        long topic = TOPIC + seq.incrementAndGet();
        centroids.upsertCentroids(TENANT, List.of(new CentroidRecord(a, topic, new float[384], "first", 1)));

        centroids.upsertCentroids(TENANT, List.of(new CentroidRecord(a, topic, new float[384], "second", 2)));

        assertThat(centroidRowsOfId(registryId(a), topic)).as("one row; its label is the second write's")
            .containsExactly(Map.entry(a, "second"));
    }

    /** GUARD, passes today: before gate 3 the arbiter is the name key, so another name with the same id inserts a second centroid. */
    @Test
    void beforeGate3ACentroidWriteUnderAnotherNameWithTheSameIdInsertsASecondRow() throws Exception {
        ensureRepositories();
        String a = register("cb1");
        String b = register("cb2");
        long topic = TOPIC + seq.incrementAndGet();
        seedCentroidNamedAWithIdOfB(a, b, topic);

        try {
            centroids.upsertCentroids(TENANT, List.of(new CentroidRecord(b, topic, new float[384], "replacement", 2)));

            assertThat(centroidRowsOfId(registryId(b), topic)).as("the name key (B, topic) has no row, so the write inserts").hasSize(2);
        } finally {
            deleteCentroidsOf(topic);
        }
    }

    /** FAILS today (no seam; then until the conflict target moves): with gate 3 held the write for B updates the row named A. */
    @Test
    void fromGate3ACentroidWriteMatchingByIdUpdatesThatRowInsteadOfInsertingASecond() throws Exception {
        ensureRepositories();
        String a = register("ca1");
        String b = register("ca2");
        long topic = TOPIC + seq.incrementAndGet();
        seedCentroidNamedAWithIdOfB(a, b, topic);
        Long idOfB = registryId(b);

        withIdKeys(() -> {
            gate3(centroids, true);
            try {
                centroids.upsertCentroids(TENANT, List.of(new CentroidRecord(b, topic, new float[384], "replacement", 2)));
            } finally {
                gate3(centroids, false);
            }
        });

        var rows = centroidRowsOfId(idOfB, topic);
        assertThat(rows).as("the id key matched the existing row: still one row for (collection id, topic)").hasSize(1);
        assertThat(rows.get(0).getValue()).as("and that row took the write").isEqualTo("replacement");
    }
}
