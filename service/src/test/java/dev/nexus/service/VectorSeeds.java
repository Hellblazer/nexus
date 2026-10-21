package dev.nexus.service;

import dev.nexus.service.jooq.binding.Vector;
import org.jooq.Field;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;

import java.util.Arrays;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;

/**
 * Typed pgvector values and column picks for test seeds written through the generated jOOQ tables
 * (nexus-cbo4a: no SQL strings in Java, tests included). A {@link Vector} binds through the
 * generated field's {@code VectorBinding}, which is the same bind production uses.
 */
public final class VectorSeeds {

    private VectorSeeds() {}

    /** A {@code dim}-wide vector with every component {@code fill}. */
    public static Vector vec(int dim, float fill) {
        float[] f = new float[dim];
        Arrays.fill(f, fill);
        return Vector.of(f);
    }

    /** A {@code dim}-wide vector: {@code x} then {@code y}, every other component 0 (dim >= 2). */
    public static Vector vecXY(int dim, float x, float y) {
        float[] f = new float[dim];
        f[0] = x;
        f[1] = y;
        return Vector.of(f);
    }

    /**
     * The {@code embedding} column of the OLD per-dim tables ({@code chunks_<dim>},
     * {@code taxonomy_centroids_<dim>}), bound as a {@code real[]}. Before the pgvector extension is
     * relocated into {@code nexus} (search-path-001) the generated {@link Vector} binding, which renders
     * {@code ::nexus.vector}, does not resolve. pgvector's {@code real[]} to {@code vector} cast is an
     * ASSIGNMENT cast ({@code CREATE CAST (real[] AS vector) WITH FUNCTION array_to_vector(real[], integer,
     * boolean) AS ASSIGNMENT}), looked up by type oid rather than by name, so the INSERT converts the bind in
     * whichever schema the extension sits.
     */
    public static final Field<Float[]> LEGACY_EMBEDDING =
        DSL.field(DSL.name("embedding"), SQLDataType.REAL.array());

    /** A {@code dim}-wide {@code real[]} with every component {@code fill}, for {@link #LEGACY_EMBEDDING}. */
    public static Float[] legacyEmbedding(int dim, float fill) {
        Float[] v = new Float[dim];
        Arrays.fill(v, fill);
        return v;
    }

    /** {@code nexus.chunks.embedding_<dim>}. */
    public static Field<Vector> chunkEmbedding(int dim) {
        return switch (dim) {
            case 384 -> CHUNKS.EMBEDDING_384;
            case 768 -> CHUNKS.EMBEDDING_768;
            case 1024 -> CHUNKS.EMBEDDING_1024;
            default -> throw new IllegalArgumentException("no nexus.chunks embedding column for dim " + dim);
        };
    }

    /** {@code nexus.taxonomy_centroids.embedding_<dim>}. */
    public static Field<Vector> centroidEmbedding(int dim) {
        return switch (dim) {
            case 384 -> TAXONOMY_CENTROIDS.EMBEDDING_384;
            case 768 -> TAXONOMY_CENTROIDS.EMBEDDING_768;
            case 1024 -> TAXONOMY_CENTROIDS.EMBEDDING_1024;
            default -> throw new IllegalArgumentException("no nexus.taxonomy_centroids embedding column for dim " + dim);
        };
    }
}
