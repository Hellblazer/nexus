// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.Chash;
import org.jooq.DSLContext;
import org.jooq.ExecuteContext;
import org.jooq.JSONB;
import org.jooq.Query;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.DefaultExecuteListener;
import org.jooq.impl.DefaultExecuteListenerProvider;

import java.sql.Connection;
import java.sql.SQLException;
import java.sql.SQLWarning;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNK_ORPHANED_AT;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;

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
        return chunk(ctx, tenant, coll, id, model, dim, tag, Chash.ofText(coll + "/" + tag).toBytes());
    }

    /** As above with the chash given: for the tests that hold the same chash under another tenant, model or label. */
    static byte[] chunk(DSLContext ctx, String tenant, String coll, long id, String model, int dim, String tag, byte[] hash) {
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

    // ---- child-row statements, as the writers spell them: the NEW label, whatever the chunk is stored under -----------------

    private static final DSLContext DETACHED = DSL.using(SQLDialect.POSTGRES);

    /** A multi-row INSERT into the manifest naming {@code label}. {@code chashes} go to positions {@code from}, {@code from}+1 ... of {@code doc}. */
    Query manifestInsert(String doc, int from, String label, byte[]... chashes) {
        var step = DETACHED.insertInto(CATALOG_DOCUMENT_CHUNKS).columns(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
            CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.COLLECTION,
            CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID);
        for (int i = 0; i < chashes.length; i++) {
            step = step.values(tenant, doc, from + i, chashes[i], label, model, id);
        }
        return step;
    }

    /** As {@link #manifestInsert} with a last row that repeats the first row's position, so the statement fails the primary key after its rows were looked up. */
    Query manifestInsertWithDuplicatePosition(String doc, String label, byte[] first, byte[] second) {
        return DETACHED.insertInto(CATALOG_DOCUMENT_CHUNKS).columns(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.COLLECTION,
                CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID)
            .values(tenant, doc, 0, first, label, model, id)
            .values(tenant, doc, 0, second, label, model, id);
    }

    /** A multi-row INSERT into chunk_orphaned_at naming {@code label}; {@code upsert} adds the writers' ON CONFLICT (tenant_id, collection, chash) DO UPDATE. */
    Query orphanedInsert(String label, boolean upsert, byte[]... chashes) {
        var step = DETACHED.insertInto(CHUNK_ORPHANED_AT).columns(CHUNK_ORPHANED_AT.TENANT_ID, CHUNK_ORPHANED_AT.COLLECTION,
            CHUNK_ORPHANED_AT.CHASH, CHUNK_ORPHANED_AT.ORPHANED_AT, CHUNK_ORPHANED_AT.EMBEDDING_MODEL, CHUNK_ORPHANED_AT.COLLECTION_ID);
        for (byte[] chash : chashes) {
            step = step.values(DSL.val(tenant), DSL.val(label), DSL.val(chash), DSL.currentOffsetDateTime(), DSL.val(model), DSL.val(id));
        }
        return upsert
            ? step.onConflict(CHUNK_ORPHANED_AT.TENANT_ID, CHUNK_ORPHANED_AT.COLLECTION, CHUNK_ORPHANED_AT.CHASH).doUpdate()
                .set(CHUNK_ORPHANED_AT.ORPHANED_AT, DSL.excluded(CHUNK_ORPHANED_AT.ORPHANED_AT))
            : step;
    }

    Query orphanedInsert(String label, byte[]... chashes) {
        return orphanedInsert(label, false, chashes);
    }

    /** A multi-row INSERT into topic_assignments naming {@code label} as the source collection. */
    Query assignmentInsert(String label, byte[]... chashes) {
        var step = DETACHED.insertInto(TOPIC_ASSIGNMENTS).columns(TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
            TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.ASSIGNED_BY, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION,
            TOPIC_ASSIGNMENTS.EMBEDDING_MODEL, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID);
        for (byte[] chash : chashes) {
            step = step.values(tenant, chash, topic, "seed", label, model, id);
        }
        return step;
    }

    // ---- running and reading --------------------------------------------------------------------------------------------

    /** Collects the server notices of each statement as it ends (the RAISE LOG lines the row trigger writes, delivered at client_min_messages = log). */
    private static final class Notices extends DefaultExecuteListener {
        final List<String> lines = new ArrayList<>();

        @Override
        public void executeEnd(ExecuteContext ectx) {
            try {
                for (SQLWarning w = ectx.statement() == null ? null : ectx.statement().getWarnings(); w != null; w = w.getNextWarning()) {
                    if (w.getMessage() != null && w.getMessage().contains(LOG_PREFIX)) {
                        lines.add(w.getMessage());
                    }
                }
            } catch (SQLException e) {
                throw new IllegalStateException(e);
            }
        }
    }

    /** A context over {@code c} whose statements report their notices to {@code notices}, with {@code client_min_messages = log} set on the connection. */
    static DSLContext logging(Connection c, Notices notices) {
        DSLContext d = DSL.using(c, SQLDialect.POSTGRES).configuration().derive(new DefaultExecuteListenerProvider(notices)).dsl();
        d.set(DSL.name("client_min_messages"), DSL.inline("log")).execute();
        notices.lines.clear();
        return d;
    }

    /**
     * Runs the statements one after another, each in its own transaction (autocommit) on one connection with
     * {@code client_min_messages = log}, and returns the lines vectors-079's row trigger logged, in order. A statement the database
     * refuses throws.
     */
    List<String> run(Query... statements) {
        return engine.su(ctx -> ctx.connectionResult(c -> {
            Notices notices = new Notices();
            DSLContext d = logging(c, notices);
            for (Query q : statements) {
                d.execute(q);
            }
            return new ArrayList<>(notices.lines);
        }));
    }

    /**
     * As {@link #run}, but all the statements are ONE transaction (committed at the end). The line is logged once per transaction
     * (vectors-079), so this is the shape that shows it.
     */
    List<String> runInOneTransaction(Query... statements) {
        return engine.su(ctx -> ctx.connectionResult(c -> {
            Notices notices = new Notices();
            DSLContext d = logging(c, notices);
            c.setAutoCommit(false);
            try {
                for (Query q : statements) {
                    d.execute(q);
                }
                c.commit();
            } catch (RuntimeException e) {
                c.rollback();
                throw e;
            } finally {
                c.setAutoCommit(true);
            }
            return new ArrayList<>(notices.lines);
        }));
    }

    /**
     * One transaction in which {@code failing} is expected to fail and be rolled back to a savepoint taken before it, then {@code after}
     * run one after another. Returns the notices of the statements AFTER the rollback, one list per statement.
     */
    List<List<String>> runAfterARolledBackSavepoint(Query failing, Query... after) {
        return engine.su(ctx -> ctx.connectionResult(c -> {
            Notices notices = new Notices();
            DSLContext d = logging(c, notices);
            c.setAutoCommit(false);
            try {
                var sp = c.setSavepoint();
                try {
                    d.execute(failing);
                    throw new AssertionError("non-vacuity: the statement must fail");
                } catch (org.jooq.exception.DataAccessException expected) {
                    c.rollback(sp);
                }
                notices.lines.clear();
                List<List<String>> out = new ArrayList<>();
                for (Query q : after) {
                    d.execute(q);
                    out.add(new ArrayList<>(notices.lines));
                    notices.lines.clear();
                }
                c.commit();
                return out;
            } finally {
                c.setAutoCommit(true);
            }
        }));
    }

    /** The stored collection name of the manifest rows of {@code doc}, sorted. */
    List<String> manifestNames(String doc) {
        return engine.su(ctx -> ctx.select(CATALOG_DOCUMENT_CHUNKS.COLLECTION).from(CATALOG_DOCUMENT_CHUNKS)
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant)).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(doc))
            .fetch(CATALOG_DOCUMENT_CHUNKS.COLLECTION).stream().sorted().toList());
    }

    List<String> orphanedNames() {
        return engine.su(ctx -> ctx.select(CHUNK_ORPHANED_AT.COLLECTION).from(CHUNK_ORPHANED_AT).where(CHUNK_ORPHANED_AT.TENANT_ID.eq(tenant))
            .fetch(CHUNK_ORPHANED_AT.COLLECTION).stream().sorted().toList());
    }

    /** The collection ids of the tenant's chunk_orphaned_at rows. */
    List<Long> orphanedIds() {
        return engine.su(ctx -> ctx.select(CHUNK_ORPHANED_AT.COLLECTION_ID).from(CHUNK_ORPHANED_AT)
            .where(CHUNK_ORPHANED_AT.TENANT_ID.eq(tenant)).fetch(CHUNK_ORPHANED_AT.COLLECTION_ID));
    }

    List<String> assignmentNames() {
        return engine.su(ctx -> ctx.select(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION).from(TOPIC_ASSIGNMENTS)
            .where(TOPIC_ASSIGNMENTS.TENANT_ID.eq(tenant)).fetch(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION).stream().sorted().toList());
    }

    /** The chash (hex) of the tenant's assignments to {@code topicId}, sorted. */
    List<String> assignedChashes(long topicId) {
        return engine.su(ctx -> ctx.select(TOPIC_ASSIGNMENTS.DOC_ID).from(TOPIC_ASSIGNMENTS)
            .where(TOPIC_ASSIGNMENTS.TENANT_ID.eq(tenant)).and(TOPIC_ASSIGNMENTS.TOPIC_ID.eq(topicId))
            .fetch(TOPIC_ASSIGNMENTS.DOC_ID).stream().map(this::hex).sorted().toList());
    }

    /** The stored collection names of the tenant's chunk rows with {@code chash}, sorted. */
    List<String> chunkNames(byte[] chash) {
        return engine.su(ctx -> ctx.select(CHUNKS.COLLECTION).from(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.CHASH.eq(chash)).fetch(CHUNKS.COLLECTION).stream().sorted().toList());
    }

    /**
     * How many child rows of the three tables do NOT carry their chunk's stored name: the chunk name keys' own invariant, read by a join
     * on the name key. Zero when every child row is where the keys want it.
     */
    long childrenNotOnTheirChunksName() {
        return childrenNotOnTheirChunksName(engine, tenant);
    }

    /** As {@link #childrenNotOnTheirChunksName()} for any tenant of {@code engine}. */
    static long childrenNotOnTheirChunksName(Rdr229RenameEngine engine, String tenant) {
        return engine.su(ctx -> {
            var m = CATALOG_DOCUMENT_CHUNKS;
            var o = CHUNK_ORPHANED_AT;
            var a = TOPIC_ASSIGNMENTS;
            long manifest = ctx.fetchCount(m, m.TENANT_ID.eq(tenant).andNotExists(ctx.selectOne().from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(m.TENANT_ID)).and(CHUNKS.COLLECTION.eq(m.COLLECTION)).and(CHUNKS.CHASH.eq(m.CHASH))
                .and(CHUNKS.EMBEDDING_MODEL.eq(m.EMBEDDING_MODEL))));
            long orphaned = ctx.fetchCount(o, o.TENANT_ID.eq(tenant).andNotExists(ctx.selectOne().from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(o.TENANT_ID)).and(CHUNKS.COLLECTION.eq(o.COLLECTION)).and(CHUNKS.CHASH.eq(o.CHASH))
                .and(CHUNKS.EMBEDDING_MODEL.eq(o.EMBEDDING_MODEL))));
            long assigned = ctx.fetchCount(a, a.TENANT_ID.eq(tenant).andNotExists(ctx.selectOne().from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(a.TENANT_ID)).and(CHUNKS.COLLECTION.eq(a.SOURCE_COLLECTION)).and(CHUNKS.CHASH.eq(a.DOC_ID))
                .and(CHUNKS.EMBEDDING_MODEL.eq(a.EMBEDDING_MODEL))));
            return manifest + orphaned + assigned;
        });
    }

    /** Runs {@code work} with {@code trigger} of {@code table} disabled (the control: the same statement without vectors-079), re-enabled after. */
    <T> T withoutTrigger(String table, String trigger, java.util.function.Supplier<T> work) {
        engine.su(ctx -> ctx.connectionResult(c -> {
            try {
                PgContainerHelper.disableTrigger(c, table, trigger);
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
            return null;
        }));
        try {
            return work.get();
        } finally {
            engine.su(ctx -> ctx.connectionResult(c -> {
                try {
                    PgContainerHelper.enableTrigger(c, table, trigger);
                } catch (Exception e) {
                    throw new IllegalStateException(e);
                }
                return null;
            }));
        }
    }
}
