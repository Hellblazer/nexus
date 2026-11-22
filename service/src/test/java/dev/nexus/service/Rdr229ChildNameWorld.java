// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.Chash;
import org.jooq.DSLContext;
import org.jooq.JSONB;

import java.sql.SQLException;
import java.sql.SQLWarning;
import java.sql.Statement;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;

/**
 * The state the P3.7 child-row name trigger tests (vectors-079) start from: one collection renamed one-row through the route, so its
 * {@link #ex existing chunks} keep the old label as their stored name and its {@link #nw new chunks}, written after the rename, carry
 * the new one. Every writer now holds the new label. Not a test.
 */
final class Rdr229ChildNameWorld {

    static final String MODEL_384 = "minilm-l6-v2-384";
    static final String MODEL_768 = "bge-base-en-v15-768";
    static final String MODEL_1024 = "voyage-code-3";
    /** The line vectors-079's row trigger writes, up to the table name. */
    static final String LOG_PREFIX = "RDR-229: nexus.";

    final Rdr229RenameEngine engine;
    final String tenant;
    final String token;
    final String oldName;
    final String newName;
    final String model;
    final int dim;
    final long id;
    /** Chunks written before the rename: stored under {@link #oldName}. */
    final byte[][] ex = new byte[5][];
    /** Chunks written after it: stored under {@link #newName}. */
    final byte[][] nw = new byte[5][];
    /** A topic every assignment of the world points at. */
    final long topic;

    private Rdr229ChildNameWorld(Rdr229RenameEngine engine, String tenant, String token, String oldName, String newName, String model,
                                 int dim, long id, long topic) {
        this.engine = engine;
        this.tenant = tenant;
        this.token = token;
        this.oldName = oldName;
        this.newName = newName;
        this.model = model;
        this.dim = dim;
        this.id = id;
        this.topic = topic;
    }

    static String collection(String owner, String model, int version) {
        return "docs__" + owner + "__" + model + "__v" + version;
    }

    /** Seeds five chunks under the old label, renames through the route (asserting 200), seeds five under the new one. */
    static Rdr229ChildNameWorld renamed(Rdr229RenameEngine engine, String tag, String model, int dim) throws Exception {
        String tenant = "p37-cn-" + tag;
        String token = engine.newTenant(tenant);
        String owner = "p37cn" + tag;
        String oldName = collection(owner, model, 1);
        String newName = collection(owner, model, 2);
        byte[][] ex = new byte[5][];
        engine.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, oldName);
            long id = Rdr229RenameChecks.registryId(ctx, tenant, oldName);
            for (int i = 0; i < ex.length; i++) {
                ex[i] = chunk(ctx, tenant, oldName, id, model, dim, "ex" + i);
            }
            return null;
        });
        long id = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, oldName));
        var r = engine.rename(token, oldName, newName);
        if (r.status() != 200) {
            throw new AssertionError("the one-row rename of the world answered " + r);
        }
        Rdr229ChildNameWorld[] w = new Rdr229ChildNameWorld[1];
        byte[][] nw = new byte[5][];
        engine.su(ctx -> {
            for (int i = 0; i < nw.length; i++) {
                nw[i] = chunk(ctx, tenant, newName, id, model, dim, "nw" + i);
            }
            long topic = ctx.insertInto(TOPICS)
                .set(TOPICS.TENANT_ID, tenant).set(TOPICS.LABEL, "t-" + tag).set(TOPICS.COLLECTION, newName)
                .set(TOPICS.COLLECTION_ID, id).set(TOPICS.CREATED_AT, OffsetDateTime.now())
                .returning(TOPICS.ID).fetchOne().getId();
            for (int i = 0; i < 5; i++) {
                PgContainerHelper.insertCatalogDocument(ctx, tenant, "cn." + tag + ".d" + i);
            }
            w[0] = new Rdr229ChildNameWorld(engine, tenant, token, oldName, newName, model, dim, id, topic);
            return null;
        });
        System.arraycopy(ex, 0, w[0].ex, 0, ex.length);
        System.arraycopy(nw, 0, w[0].nw, 0, nw.length);
        return w[0];
    }

    /** A chunk of {@code coll} (registered under {@code id}); its chash is the hash of its collection and tag, so the world's chunks are all distinct. */
    static byte[] chunk(DSLContext ctx, String tenant, String coll, long id, String model, int dim, String tag) {
        byte[] hash = Chash.ofText(coll + "/" + tag).toBytes();
        ctx.insertInto(CHUNKS)
            .set(CHUNKS.TENANT_ID, tenant).set(CHUNKS.COLLECTION, coll).set(CHUNKS.CHASH, hash)
            .set(CHUNKS.EMBEDDING_MODEL, model).set(CHUNKS.CHUNK_TEXT, tag + " text")
            .set(VectorSeeds.chunkEmbedding(dim), VectorSeeds.vec(dim, 0.1f))
            .set(CHUNKS.METADATA, JSONB.valueOf("{}")).set(CHUNKS.LAST_WRITTEN_AT, OffsetDateTime.now().minusDays(1))
            .set(CHUNKS.COLLECTION_ID, id).execute();
        return hash;
    }

    /** A centroid of {@code coll} for {@code topic}. */
    static void centroid(DSLContext ctx, String tenant, String coll, long id, String model, int dim, long topic, float fill) {
        ctx.insertInto(TAXONOMY_CENTROIDS)
            .set(TAXONOMY_CENTROIDS.TENANT_ID, tenant).set(TAXONOMY_CENTROIDS.COLLECTION, coll)
            .set(TAXONOMY_CENTROIDS.TOPIC_ID, topic).set(TAXONOMY_CENTROIDS.EMBEDDING_MODEL, model)
            .set(TAXONOMY_CENTROIDS.LABEL, "centroid").set(VectorSeeds.centroidEmbedding(dim), VectorSeeds.vec(dim, fill))
            .set(TAXONOMY_CENTROIDS.COLLECTION_ID, id).execute();
    }

    String hex(byte[] chash) {
        return Chash.fromSha256Bytes(chash).toHex();
    }

    private String lit(byte[] chash) {
        return "decode('" + hex(chash) + "','hex')";
    }

    // ---- child-row statements, as the writers spell them: the NEW label, whatever the chunk is stored under -----------------

    /** A multi-row INSERT into the manifest naming {@code newName}. {@code chashes} go to positions {@code from}, {@code from}+1 ... of {@code doc}. */
    String manifestInsert(String doc, int from, String label, byte[]... chashes) {
        StringBuilder b = new StringBuilder("INSERT INTO nexus.catalog_document_chunks "
            + "(tenant_id, doc_id, position, chash, collection, embedding_model, collection_id) VALUES ");
        for (int i = 0; i < chashes.length; i++) {
            b.append(i == 0 ? "" : ", ").append("('").append(tenant).append("','").append(doc).append("',").append(from + i).append(',')
                .append(lit(chashes[i])).append(",'").append(label).append("','").append(model).append("',").append(id).append(')');
        }
        return b.toString();
    }

    /** A multi-row INSERT into chunk_orphaned_at naming {@code label}. */
    String orphanedInsert(String label, byte[]... chashes) {
        StringBuilder b = new StringBuilder("INSERT INTO nexus.chunk_orphaned_at "
            + "(tenant_id, collection, chash, orphaned_at, embedding_model, collection_id) VALUES ");
        for (int i = 0; i < chashes.length; i++) {
            b.append(i == 0 ? "" : ", ").append("('").append(tenant).append("','").append(label).append("',").append(lit(chashes[i]))
                .append(", now(),'").append(model).append("',").append(id).append(')');
        }
        return b.toString();
    }

    /** A multi-row INSERT into topic_assignments naming {@code label} as the source collection. */
    String assignmentInsert(String label, byte[]... chashes) {
        StringBuilder b = new StringBuilder("INSERT INTO nexus.topic_assignments "
            + "(tenant_id, doc_id, topic_id, assigned_by, source_collection, embedding_model, source_collection_id) VALUES ");
        for (int i = 0; i < chashes.length; i++) {
            b.append(i == 0 ? "" : ", ").append("('").append(tenant).append("',").append(lit(chashes[i])).append(',').append(topic)
                .append(",'seed','").append(label).append("','").append(model).append("',").append(id).append(')');
        }
        return b.toString();
    }

    // ---- running and reading --------------------------------------------------------------------------------------------

    /**
     * Runs the statements one after another, each in its own transaction (autocommit) on one connection with
     * {@code client_min_messages = log}, and returns the lines vectors-079's statement trigger logged, in order. A statement the database
     * refuses throws.
     */
    List<String> run(String... statements) {
        return engine.su(ctx -> ctx.connectionResult(c -> {
            List<String> lines = new ArrayList<>();
            try (Statement s = c.createStatement()) {
                s.execute("SET client_min_messages = log");
                for (String sql : statements) {
                    s.execute(sql);
                    lines.addAll(logged(s));
                }
            }
            return lines;
        }));
    }

    /**
     * As {@link #run}, but all the statements are ONE transaction (committed at the end), on a connection with
     * {@code client_min_messages = log}. The line is logged once per transaction (vectors-079), so this is the shape that shows it.
     */
    List<String> runInOneTransaction(String... statements) {
        return engine.su(ctx -> ctx.connectionResult(c -> {
            List<String> lines = new ArrayList<>();
            c.setAutoCommit(false);
            try (Statement s = c.createStatement()) {
                s.execute("SET client_min_messages = log");
                for (String sql : statements) {
                    s.execute(sql);
                    lines.addAll(logged(s));
                }
                c.commit();
            } catch (SQLException e) {
                c.rollback();
                throw e;
            } finally {
                c.setAutoCommit(true);
            }
            return lines;
        }));
    }

    static List<String> logged(Statement s) throws SQLException {
        List<String> lines = new ArrayList<>();
        for (SQLWarning w = s.getWarnings(); w != null; w = w.getNextWarning()) {
            if (w.getMessage() != null && w.getMessage().contains(LOG_PREFIX)) {
                lines.add(w.getMessage());
            }
        }
        return lines;
    }

    /** The values of {@code column} of {@code sql}'s rows, as strings, sorted. */
    List<String> column(String sql) {
        return engine.su(ctx -> ctx.fetch(sql).getValues(0, String.class).stream().sorted().toList());
    }

    /** The stored collection name of the manifest rows of {@code doc}, sorted. */
    List<String> manifestNames(String doc) {
        return column("SELECT collection FROM nexus.catalog_document_chunks WHERE tenant_id = '" + tenant + "' AND doc_id = '" + doc + "'");
    }

    List<String> orphanedNames() {
        return column("SELECT collection FROM nexus.chunk_orphaned_at WHERE tenant_id = '" + tenant + "'");
    }

    List<String> assignmentNames() {
        return column("SELECT source_collection FROM nexus.topic_assignments WHERE tenant_id = '" + tenant + "'");
    }

    /**
     * How many child rows of the three tables do NOT carry their chunk's stored name: the chunk name keys' own invariant, read by a join
     * on the name key. Zero when every child row is where the keys want it.
     */
    long childrenNotOnTheirChunksName() {
        return engine.su(ctx -> ctx.fetchOne(
            "SELECT (SELECT count(*) FROM nexus.catalog_document_chunks m WHERE m.tenant_id = ? AND NOT EXISTS "
                + "(SELECT 1 FROM nexus.chunks c WHERE c.tenant_id = m.tenant_id AND c.collection = m.collection AND c.chash = m.chash "
                + "AND c.embedding_model = m.embedding_model))"
                + " + (SELECT count(*) FROM nexus.chunk_orphaned_at m WHERE m.tenant_id = ? AND NOT EXISTS "
                + "(SELECT 1 FROM nexus.chunks c WHERE c.tenant_id = m.tenant_id AND c.collection = m.collection AND c.chash = m.chash "
                + "AND c.embedding_model = m.embedding_model))"
                + " + (SELECT count(*) FROM nexus.topic_assignments m WHERE m.tenant_id = ? AND NOT EXISTS "
                + "(SELECT 1 FROM nexus.chunks c WHERE c.tenant_id = m.tenant_id AND c.collection = m.source_collection AND c.chash = m.doc_id "
                + "AND c.embedding_model = m.embedding_model))",
            tenant, tenant, tenant).get(0, Long.class));
    }

    /** Runs {@code work} with {@code trigger} of {@code table} disabled (the control: the same statement without vectors-079), re-enabled after. */
    <T> T withoutTrigger(String table, String trigger, java.util.function.Supplier<T> work) {
        engine.su(ctx -> ctx.execute("ALTER TABLE nexus." + table + " DISABLE TRIGGER " + trigger));
        try {
            return work.get();
        } finally {
            engine.su(ctx -> ctx.execute("ALTER TABLE nexus." + table + " ENABLE TRIGGER " + trigger));
        }
    }
}
