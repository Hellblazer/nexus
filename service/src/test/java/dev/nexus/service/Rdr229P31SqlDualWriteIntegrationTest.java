// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.CollectionIds;
import dev.nexus.service.db.SchemaMigrator;
import dev.nexus.service.jooq.nexus.Routines;
import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.types.YearToSecond;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.SQLException;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static dev.nexus.service.jooq.nexus.Tables.ASSIGN_FROM_CHASHES_1024;
import static dev.nexus.service.jooq.nexus.Tables.ASSIGN_FROM_CHASHES_384;
import static dev.nexus.service.jooq.nexus.Tables.ASSIGN_FROM_CHASHES_768;
import static dev.nexus.service.jooq.nexus.Tables.CANARY_QUERY_RING;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_OWNERS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_RECONCILE_ROWS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_RECONCILE_RUNS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNK_ORPHANED_AT;
import static dev.nexus.service.jooq.nexus.Tables.GC_AUDIT;
import static dev.nexus.service.jooq.nexus.Tables.GC_EXPIRE_QUARANTINE;
import static dev.nexus.service.jooq.nexus.Tables.GC_EXPIRE_QUARANTINE_BOUNDED;
import static dev.nexus.service.jooq.nexus.Tables.GC_QUARANTINE_ORPHANS;
import static dev.nexus.service.jooq.nexus.Tables.GC_QUARANTINE_ORPHANS_BOUNDED;
import static dev.nexus.service.jooq.nexus.Tables.GC_QUARANTINE_ORPHANS_FLOORED;
import static dev.nexus.service.jooq.nexus.Tables.GC_RESTORE_REREFERENCED_BOUNDED;
import static dev.nexus.service.jooq.nexus.Tables.QUARANTINE_RESTORE_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.REAPER_EXPIRE_CLIENT_QUARANTINE;
import static dev.nexus.service.jooq.nexus.Tables.REAPER_EXPIRE_QUARANTINE;
import static dev.nexus.service.jooq.nexus.Tables.REAPER_QUARANTINE_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS_RECOUNT_DOC_COUNT;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.1 stage 2b, the SQL family of the dual write (nexus-p2m1x.17): {@code vectors-069-collection-id-dual-write-functions.xml}.
 *
 * <p>Since vectors-068 a row that names a collection must carry the registry's id beside the name (a NOT VALID CHECK that
 * tests every new and every updated row). This class drives the 24 live plpgsql functions that write a guarded table, through
 * three database states of ONE container so each function is shown failing before the changeset and carrying the right id after:
 * <ol>
 *   <li><b>Before vectors-068.</b> Every scenario seeds the older layout (no id columns), so the rows it leaves are the
 *       unstamped rows an install holds when the CHECKs arrive: a document, topic or chunk with a name and no id.</li>
 *   <li><b>vectors-068 applied, vectors-069 not.</b> Each scenario that the CHECK refuses runs its call and fails with
 *       23514 (SQLSTATE of {@code check_violation}; {@code catalog_reconcile_commit} wraps it in its own message, which names
 *       the state). The function definitions are snapshotted (owner, SECURITY mode, SET clauses, ACL, volatility, arguments,
 *       result, body) and the database is tagged.</li>
 *   <li><b>vectors-069 applied.</b> The same calls succeed; each scenario's verification reads the row the function wrote or
 *       updated and compares its id with the registry's (never with a literal). Every function's attributes are unchanged and its
 *       body differs; every function this changeset does not redefine is byte-identical.</li>
 *   <li><b>Rolled back to the tag.</b> The definitions equal the phase-2 snapshot exactly: each rollback restores its
 *       previous body verbatim.</li>
 * </ol>
 * Scenarios that only write a history row ({@code gc_audit}: no CHECK, so the old body does not fail) run in phase 3 alone and
 * assert the id. The unaffected ({@code purge_trash} writes '' for its collection, so its id stays NULL) is asserted too.
 */
class Rdr229P31SqlDualWriteIntegrationTest {

    private static final String ADMIN_ROLE = "nexus_admin_p31_sqlfn";
    private static final String ADMIN_PASS = "nexus_admin_p31_sqlfn_pass";
    private static final String MASTER_CHANGELOG = "db/changelog/db.changelog-master.xml";
    private static final String CHANGELOG = "db/changelog/vectors-069-collection-id-dual-write-functions.xml";
    private static final Path CHANGELOG_FILE = Path.of("src", "main", "resources", CHANGELOG);
    private static final Path CENSUS = Path.of("src", "test", "resources", "rdr229", "p31-writer-census.tsv");
    private static final Path CHECKS = Path.of("src", "main", "resources", "db", "rdr229", "p31-checks.tsv");

    private static final String EXPAND_FIRST = "vectors-068-1";
    private static final String DUAL_FIRST = "vectors-069-1";
    private static final String TAG = "before-p31-sql-dual-write";

    private static final String T = "p31sql";
    private static final String M768 = "bge-base-en-v15-768";
    private static final String TS = "2026-11-21T00:00:00Z";
    private static final String EXPIRES = "2026-12-21T00:00:00Z";

    /** The census function the changeset leaves alone, with the reason (it writes '' for its collection, so its id is NULL). */
    private static final Set<String> NOT_REDEFINED = Set.of("purge_trash");

    // ---- the plumbing ---------------------------------------------------------------------------------------------

    private record Env(Connection conn, DSLContext ctx) {
        Env(Connection conn) {
            this(conn, DSL.using(conn, SQLDialect.POSTGRES));
        }

        Env tenant(String tenant) {
            ctx.select(DSL.function("set_config", String.class, DSL.val("nexus.tenant"), DSL.val(tenant), DSL.val(false))).fetch();
            return this;
        }

        long id(String tenant, String name) {
            return CollectionIds.require(ctx, tenant, name);
        }
    }

    private interface Act {
        void run(Env e) throws Exception;
    }

    /**
     * One function under test. {@code seed} runs before vectors-068 (the older layout, so its rows are unstamped), {@code call}
     * runs twice (refused before vectors-069 when {@code refusedBefore}, then for real), {@code verify} reads what the real call wrote.
     */
    private record Scenario(String name, String tenant, boolean refusedBefore, Act seed, Act call, Act verify) {}

    // ---- names and seeds ------------------------------------------------------------------------------------------

    private static String coll(String contentType, String tag, String model) {
        return contentType + "__p31sql-" + tag + "__" + model + "__v1";
    }

    private static String docs(String tag) {
        return coll("docs", tag, M768);
    }

    private static String quarantine(String origin) {
        return "quarantine-" + origin;
    }

    private static void register(Env e, String tenant, String name) {
        PgContainerHelper.insertCollection(e.ctx(), tenant, name);
    }

    /** A chunk written under the older layout (no id column yet); returns its chash. */
    private static byte[] chunk(Env e, String tenant, String coll, String tag, int dim, String metadataJson, int ageDays) {
        return chunk(e, tenant, coll, Chash.ofText(coll + "/" + tag).toBytes(), tag, dim, metadataJson, ageDays);
    }

    private static byte[] chunk(Env e, String tenant, String coll, byte[] hash, String tag, int dim, String metadataJson, int ageDays) {
        e.ctx().insertInto(CHUNKS)
            .set(CHUNKS.TENANT_ID, tenant)
            .set(CHUNKS.COLLECTION, coll)
            .set(CHUNKS.CHASH, hash)
            .set(CHUNKS.EMBEDDING_MODEL, PgContainerHelper.collectionModel(e.ctx(), tenant, coll))
            .set(CHUNKS.CHUNK_TEXT, tag + " text")
            .set(VectorSeeds.chunkEmbedding(dim), VectorSeeds.vec(dim, 0.1f))
            .set(CHUNKS.METADATA, JSONB.valueOf(metadataJson))
            .set(CHUNKS.LAST_WRITTEN_AT, OffsetDateTime.now().minusDays(ageDays))
            .execute();
        return hash;
    }

    private static String hex(byte[] chash) {
        return Chash.fromSha256Bytes(chash).toHex();
    }

    private static Long chunkId(Env e, String tenant, String coll, byte[] chash) {
        return e.ctx().select(CHUNKS.COLLECTION_ID).from(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION.eq(coll)).and(CHUNKS.CHASH.eq(chash))
            .fetchOne(CHUNKS.COLLECTION_ID);
    }

    private static void assertChunkId(Env e, String tenant, String coll, byte[] chash) {
        assertThat(chunkExists(e, tenant, coll, chash)).as("the chunk is in %s", coll).isTrue();
        assertThat(chunkId(e, tenant, coll, chash)).as("chunk id in %s", coll).isEqualTo(e.id(tenant, coll));
    }

    private static boolean chunkExists(Env e, String tenant, String coll, byte[] chash) {
        return e.ctx().fetchExists(CHUNKS, CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(coll)).and(CHUNKS.CHASH.eq(chash)));
    }

    /** The gc_audit rows of {@code operation} for {@code coll}: each must carry the registry's id of {@code coll}. */
    private static void assertAuditId(Env e, String tenant, String operation, String coll, String idOfName) {
        List<Long> ids = e.ctx().select(GC_AUDIT.COLLECTION_ID).from(GC_AUDIT)
            .where(GC_AUDIT.TENANT_ID.eq(tenant)).and(GC_AUDIT.OPERATION.eq(operation)).and(GC_AUDIT.COLLECTION.eq(coll))
            .fetch(GC_AUDIT.COLLECTION_ID);
        assertThat(ids).as("gc_audit %s rows for %s", operation, coll).hasSize(1);
        assertThat(ids.get(0)).as("gc_audit %s collection_id", operation).isEqualTo(e.id(tenant, idOfName));
    }

    private static void doc(Env e, String tenant, String tumbler, String physical) {
        e.ctx().insertInto(CATALOG_DOCUMENTS)
            .set(CATALOG_DOCUMENTS.TENANT_ID, tenant)
            .set(CATALOG_DOCUMENTS.TUMBLER, tumbler)
            .set(CATALOG_DOCUMENTS.TITLE, "Doc " + tumbler)
            .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, physical)
            .execute();
    }

    private static Long docId(Env e, String tenant, String tumbler) {
        return e.ctx().select(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID).from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler))
            .fetchOne(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID);
    }

    private static void manifest(Env e, String tenant, String docId, int position, byte[] chash, String coll) {
        e.ctx().insertInto(CATALOG_DOCUMENT_CHUNKS)
            .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, tenant)
            .set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, docId)
            .set(CATALOG_DOCUMENT_CHUNKS.POSITION, position)
            .set(CATALOG_DOCUMENT_CHUNKS.CHASH, chash)
            .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, coll)
            .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, PgContainerHelper.collectionModel(e.ctx(), tenant, coll))
            .execute();
    }

    private static long topic(Env e, String tenant, String coll, String label) {
        return e.ctx().insertInto(TOPICS)
            .set(TOPICS.TENANT_ID, tenant).set(TOPICS.LABEL, label).set(TOPICS.COLLECTION, coll)
            .set(TOPICS.CREATED_AT, OffsetDateTime.now())
            .returning(TOPICS.ID).fetchOne().getId();
    }

    private static Long topicId(Env e, String tenant, long topic) {
        return e.ctx().select(TOPICS.COLLECTION_ID).from(TOPICS)
            .where(TOPICS.TENANT_ID.eq(tenant)).and(TOPICS.ID.eq(topic)).fetchOne(TOPICS.COLLECTION_ID);
    }

    private static void centroid(Env e, String tenant, String coll, long topic, int dim, float fill) {
        e.ctx().insertInto(TAXONOMY_CENTROIDS)
            .set(TAXONOMY_CENTROIDS.TENANT_ID, tenant).set(TAXONOMY_CENTROIDS.COLLECTION, coll)
            .set(TAXONOMY_CENTROIDS.TOPIC_ID, topic)
            .set(TAXONOMY_CENTROIDS.EMBEDDING_MODEL, PgContainerHelper.collectionModel(e.ctx(), tenant, coll))
            .set(TAXONOMY_CENTROIDS.LABEL, "centroid")
            .set(VectorSeeds.centroidEmbedding(dim), VectorSeeds.vec(dim, fill))
            .execute();
    }

    /** A legacy assignment (older layout: no id column), as an install holds one before the stamp. */
    private static void assignment(Env e, String tenant, byte[] chash, long topic, String coll, double similarity) {
        e.ctx().insertInto(TOPIC_ASSIGNMENTS)
            .set(TOPIC_ASSIGNMENTS.TENANT_ID, tenant).set(TOPIC_ASSIGNMENTS.DOC_ID, chash).set(TOPIC_ASSIGNMENTS.TOPIC_ID, topic)
            .set(TOPIC_ASSIGNMENTS.ASSIGNED_BY, "seed").set(TOPIC_ASSIGNMENTS.SIMILARITY, similarity)
            .set(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, coll)
            .set(TOPIC_ASSIGNMENTS.EMBEDDING_MODEL, PgContainerHelper.collectionModel(e.ctx(), tenant, coll))
            .execute();
    }

    private static Long assignmentId(Env e, String tenant, byte[] chash, long topic) {
        return e.ctx().select(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID).from(TOPIC_ASSIGNMENTS)
            .where(TOPIC_ASSIGNMENTS.TENANT_ID.eq(tenant)).and(TOPIC_ASSIGNMENTS.DOC_ID.eq(chash)).and(TOPIC_ASSIGNMENTS.TOPIC_ID.eq(topic))
            .fetchOne(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID);
    }

    /** The manifest row of an owner document in a collection is the one place a restore's chunk is named; the FK to the chunk is bypassed for it. */
    private static void manifestWithoutChunk(Env e, String tenant, String docId, int position, byte[] chash, String coll) throws SQLException {
        e.conn().setAutoCommit(true);
        PgContainerHelper.dropCatalogChunksFk(e.conn());
        manifest(e, tenant, docId, position, chash, coll);
        PgContainerHelper.readdCatalogChunksFkNotValid(e.conn());
    }

    // ---- the scenarios ----------------------------------------------------------------------------------------------

    private static List<Scenario> scenarios() {
        List<Scenario> s = new ArrayList<>();
        // quarantine moves: the sibling's chunk rows and the audit row
        s.add(move("gc_quarantine_orphans (sibling already holds a legacy row)", "mv1", "gc_quarantine_orphans", true,
            (e, o, q) -> e.ctx().selectFrom(GC_QUARANTINE_ORPHANS.call(768, T, o, q, TS, 20)).fetch(), null));
        s.add(move("gc_quarantine_orphans_bounded (sibling already holds a legacy row)", "mv2", "gc_quarantine_orphans_bounded", true,
            (e, o, q) -> e.ctx().selectFrom(GC_QUARANTINE_ORPHANS_BOUNDED.call(768, T, o, q, TS, 20, 100)).fetch(), null));
        s.add(move("gc_quarantine_orphans_floored (move with expiry)", "mv3", "gc_quarantine_orphans",
            (e, o, q) -> e.ctx().selectFrom(GC_QUARANTINE_ORPHANS_FLOORED.call(768, T, o, q, TS, 20, null, null, null, false, EXPIRES)).fetch(),
            (e, q, hash) -> assertThat(e.ctx().select(CHUNKS.METADATA).from(CHUNKS)
                    .where(CHUNKS.TENANT_ID.eq(T)).and(CHUNKS.COLLECTION.eq(q)).and(CHUNKS.CHASH.eq(hash)).fetchOne(CHUNKS.METADATA).data())
                .as("the expiry stamp the wrapper writes is still written").contains(EXPIRES)));
        s.add(move("reaper_quarantine_chunks (sibling already holds a legacy row)", "mv4", "reaper_quarantine", true,
            (e, o, q) -> e.ctx().selectFrom(REAPER_QUARANTINE_CHUNKS.call(T, o, q, TS, 100, null, 1.0, 0, false)).fetch(), null));
        // restores
        s.add(restore("gc_restore_rereferenced", "rs1", "gc_restore_rereferenced",
            (e, q, o) -> Routines.gcRestoreRereferenced(e.ctx().configuration(), 768, T, q, o)));
        s.add(restore("gc_restore_rereferenced_bounded", "rs2", "gc_restore_rereferenced_bounded",
            (e, q, o) -> e.ctx().selectFrom(GC_RESTORE_REREFERENCED_BOUNDED.call(768, T, q, o, 100)).fetch()));
        s.add(quarantineRestore());
        // expiries: history rows only
        s.add(expiry("gc_expire_quarantine", "ex1", "gc_expire_quarantine", false, false,
            (e, q, o) -> e.ctx().selectFrom(GC_EXPIRE_QUARANTINE.call(768, T, q, o, "2099-01-01T00:00:00Z", 1.0, 0, true)).fetch()));
        s.add(expiry("gc_expire_quarantine_bounded", "ex2", "gc_expire_quarantine_bounded", false, false,
            (e, q, o) -> e.ctx().selectFrom(GC_EXPIRE_QUARANTINE_BOUNDED.call(768, T, q, o, "2099-01-01T00:00:00Z", 1.0, 0, true, 100, false)).fetch()));
        s.add(expiry("reaper_expire_quarantine", "ex3", "reaper_expire_quarantine", true, false,
            (e, q, o) -> e.ctx().selectFrom(REAPER_EXPIRE_QUARANTINE.call(T, q, o, "2099-01-01T00:00:00Z", 100)).fetch()));
        s.add(expiry("reaper_expire_client_quarantine", "ex4", "reaper_expire_client_quarantine", false, true,
            (e, q, o) -> e.ctx().selectFrom(REAPER_EXPIRE_CLIENT_QUARANTINE.call(T, q, o, "2099-01-01T00:00:00Z", 100)).fetch()));
        s.add(flooredRefusal());
        s.add(purgeTrash());
        // chunk_orphaned_at triggers
        s.add(stampDelete());
        s.add(stampUpdate());
        // taxonomy
        s.add(assign(384, "minilm-l6-v2-384", "as384"));
        s.add(assign(768, M768, "as768"));
        s.add(assign(1024, "voyage-code-3", "as1024"));
        s.add(topicsTrigger("topics_doc_count_recount_ins", "tp1", false));
        s.add(topicsTrigger("topics_doc_count_recount_del", "tp2", true));
        s.add(topicsRecount());
        // catalog
        s.add(reconcile());
        s.add(trash());
        s.add(restoreDoc());
        s.add(ownerToCollection());
        s.add(canary());
        return s;
    }

    private interface MoveCall {
        void run(Env e, String origin, String quarantine) throws Exception;
    }

    private interface RestoreCall {
        void run(Env e, String quarantine, String origin) throws Exception;
    }

    private interface Extra {
        void run(Env e, String quarantine, byte[] chash);
    }

    /** An orphan chunk moves from {@code origin} to its quarantine sibling: the sibling's row and the audit row carry ids. */
    private static Scenario move(String name, String tag, String auditOp, MoveCall call, Extra extra) {
        return move(name, tag, auditOp, false, call, extra);
    }

    /**
     * With {@code siblingExists} the sibling is already registered and already holds a legacy (unstamped) row of the same chunk, so the
     * move takes the two ON CONFLICT DO UPDATE paths: the registry upsert (superseded_by_id) and the chunk upsert (collection_id).
     */
    private static Scenario move(String name, String tag, String auditOp, boolean siblingExists, MoveCall call, Extra extra) {
        String origin = docs(tag);
        String sibling = quarantine(origin);
        byte[][] hash = new byte[1][];
        return new Scenario(name, T, true,
            e -> {
                register(e, T, origin);
                hash[0] = chunk(e, T, origin, "orphan", 768, "{}", 90);
                if (siblingExists) {
                    register(e, T, sibling);
                    chunk(e, T, sibling, hash[0], "stale copy", 768, "{\"origin_collection\": \"" + origin + "\"}", 90);
                }
            },
            e -> call.run(e, origin, sibling),
            e -> {
                assertChunkId(e, T, sibling, hash[0]);
                assertThat(e.ctx().select(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID).from(CATALOG_COLLECTIONS)
                        .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.NAME.eq(sibling))
                        .fetchOne(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID))
                    .as("the sibling is live, so its superseded_by_id is NULL").isNull();
                assertAuditId(e, T, auditOp, origin, origin);
                if (extra != null) extra.run(e, sibling, hash[0]);
            });
    }

    /** A quarantined chunk is named again by a document of the origin and comes back: the origin's chunk row and the audit row carry ids. */
    private static Scenario restore(String name, String tag, String auditOp, RestoreCall call) {
        String origin = docs(tag);
        String sibling = quarantine(origin);
        byte[][] hash = new byte[1][];
        return new Scenario(name, T, true,
            e -> {
                register(e, T, origin);
                register(e, T, sibling);
                hash[0] = chunk(e, T, sibling, "held", 768, "{\"origin_collection\": \"" + origin + "\", \"quarantined_at\": \"" + TS + "\"}", 1);
                PgContainerHelper.insertCatalogDocument(e.ctx(), T, tag + ".1");
                manifestWithoutChunk(e, T, tag + ".1", 0, hash[0], origin);
            },
            e -> call.run(e, sibling, origin),
            e -> {
                assertChunkId(e, T, origin, hash[0]);
                assertAuditId(e, T, auditOp, sibling, sibling);
            });
    }

    /** quarantine_restore_chunks with a reattach: the restored chunk, its reattached manifest row and the audit row. */
    private static Scenario quarantineRestore() {
        String origin = docs("qr");
        String sibling = quarantine(origin);
        byte[][] hash = new byte[1][];
        String doc = "qr.1";
        return new Scenario("quarantine_restore_chunks", T, true,
            e -> {
                register(e, T, origin);
                register(e, T, sibling);
                hash[0] = chunk(e, T, sibling, "held",  768,
                    "{\"origin_collection\": \"" + origin + "\", \"quarantined_at\": \"" + TS + "\", \"catalog_doc_id\": \"" + doc
                        + "\", \"chunk_index\": 0}", 1);
                doc(e, T, doc, origin);
            },
            e -> e.ctx().selectFrom(QUARANTINE_RESTORE_CHUNKS.call(T, origin, sibling, new String[] {hex(hash[0])}, "p31", null, false, true)).fetch(),
            e -> {
                assertChunkId(e, T, origin, hash[0]);
                List<Long> manifestIds = e.ctx().select(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID).from(CATALOG_DOCUMENT_CHUNKS)
                    .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(T)).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(doc))
                    .and(CATALOG_DOCUMENT_CHUNKS.COLLECTION.eq(origin)).fetch(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID);
                assertThat(manifestIds).as("the reattach wrote the manifest row").hasSize(1);
                assertThat(manifestIds.get(0)).as("the manifest row's collection_id").isEqualTo(e.id(T, origin));
                assertAuditId(e, T, "quarantine_restore", origin, origin);
            });
    }

    private interface ExpiryCall {
        void run(Env e, String quarantine, String origin) throws Exception;
    }

    /** An expiry deletes quarantine rows and writes one gc_audit row, a history row with no CHECK: phase 3 only. */
    private static Scenario expiry(String name, String tag, String auditOp, boolean engineOwned, boolean knowledge, ExpiryCall call) {
        String origin = knowledge ? coll("knowledge", tag, M768) : docs(tag);
        String sibling = quarantine(origin);
        String meta = "{\"origin_collection\": \"" + origin + "\", \"quarantined_at\": \"2026-01-01T00:00:00Z\""
            + (engineOwned ? ", \"quarantined_by\": \"engine-reaper\", \"reaper_quarantined_at\": \"2026-01-01T00:00:00Z\"" : "") + "}";
        return new Scenario(name, T, false,
            e -> {
                register(e, T, origin);
                register(e, T, sibling);
                chunk(e, T, sibling, "old", 768, meta, 1);
            },
            e -> call.run(e, sibling, origin),
            e -> assertAuditId(e, T, auditOp, sibling, sibling));
    }

    /** The floor's refusal row is a gc_audit row too (a history row: phase 3 only). */
    private static Scenario flooredRefusal() {
        String origin = docs("fl");
        String sibling = quarantine(origin);
        return new Scenario("gc_quarantine_orphans_floored (refusal row)", T, false,
            e -> {
                register(e, T, origin);
                chunk(e, T, origin, "o1", 768, "{}", 90);
                chunk(e, T, origin, "o2", 768, "{}", 90);
            },
            e -> {
                var rows = e.ctx().selectFrom(GC_QUARANTINE_ORPHANS_FLOORED.call(768, T, origin, sibling, TS, 20, null, 0.1, 0, false, null)).fetch();
                assertThat(rows.get(0).get("refused", Boolean.class)).as("the floor refused the move").isTrue();
            },
            e -> assertAuditId(e, T, "gc_quarantine_orphans_refused", origin, origin));
    }

    /** purge_trash names no collection ('' means unset, so its id stays NULL): not redefined, asserted unchanged. */
    private static Scenario purgeTrash() {
        String tenant = "p31sql-pt";
        return new Scenario("purge_trash (writes '' and NULL)", tenant, false,
            e -> {
                doc(e, tenant, "pt.1", "");
                e.ctx().update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.DELETED_AT, OffsetDateTime.now().minusDays(400))
                    .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).execute();
            },
            e -> Routines.purgeTrash(e.ctx().configuration(), YearToSecond.valueOf(Duration.ofSeconds(1))),
            e -> {
                List<org.jooq.Record2<String, Long>> rows = e.ctx().select(GC_AUDIT.COLLECTION, GC_AUDIT.COLLECTION_ID).from(GC_AUDIT)
                    .where(GC_AUDIT.TENANT_ID.eq(tenant)).and(GC_AUDIT.OPERATION.eq("purge_trash")).fetch();
                assertThat(rows).as("purge_trash wrote its audit row").hasSize(1);
                assertThat(rows.get(0).value1()).isEmpty();
                assertThat(rows.get(0).value2()).as("no collection, no id").isNull();
            });
    }

    /** DELETE of a manifest row: the trigger records the dropped chunk in chunk_orphaned_at with the id. */
    private static Scenario stampDelete() {
        String origin = docs("sd");
        byte[][] hash = new byte[1][];
        return new Scenario("stamp_chunks_on_manifest_delete", T, true,
            e -> {
                register(e, T, origin);
                hash[0] = chunk(e, T, origin, "kept", 768, "{}", 90);
                PgContainerHelper.insertCatalogDocument(e.ctx(), T, "sd.1");
                manifest(e, T, "sd.1", 0, hash[0], origin);
            },
            e -> e.ctx().deleteFrom(CATALOG_DOCUMENT_CHUNKS)
                .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(T)).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("sd.1")).execute(),
            e -> assertOrphanId(e, origin, hash[0]));
    }

    /** UPDATE of a manifest row to another chunk: the old one is recorded in chunk_orphaned_at with the id. */
    private static Scenario stampUpdate() {
        String origin = docs("su");
        byte[][] hash = new byte[2][];
        return new Scenario("stamp_chunks_on_manifest_update", T, true,
            e -> {
                register(e, T, origin);
                hash[0] = chunk(e, T, origin, "dropped", 768, "{}", 90);
                hash[1] = chunk(e, T, origin, "taken", 768, "{}", 90);
                PgContainerHelper.insertCatalogDocument(e.ctx(), T, "su.1");
                manifest(e, T, "su.1", 0, hash[0], origin);
            },
            // the manifest row is itself a guarded row: the statement carries its id, so only the trigger's insert can be refused
            e -> e.ctx().update(CATALOG_DOCUMENT_CHUNKS)
                .set(CATALOG_DOCUMENT_CHUNKS.CHASH, hash[1])
                .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID, CollectionIds.idOf(T, origin))
                .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(T)).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("su.1")).execute(),
            e -> assertOrphanId(e, origin, hash[0]));
    }

    private static void assertOrphanId(Env e, String origin, byte[] chash) {
        List<Long> ids = e.ctx().select(CHUNK_ORPHANED_AT.COLLECTION_ID).from(CHUNK_ORPHANED_AT)
            .where(CHUNK_ORPHANED_AT.TENANT_ID.eq(T)).and(CHUNK_ORPHANED_AT.COLLECTION.eq(origin)).and(CHUNK_ORPHANED_AT.CHASH.eq(chash))
            .fetch(CHUNK_ORPHANED_AT.COLLECTION_ID);
        assertThat(ids).as("the dropped chunk is recorded").hasSize(1);
        assertThat(ids.get(0)).as("chunk_orphaned_at.collection_id").isEqualTo(e.id(T, origin));
    }

    /**
     * assign_from_chashes_{dim}: the same-collection branch inserts a new assignment, the cross branch inserts one and, through
     * ON CONFLICT DO UPDATE, moves the id with the name (a legacy row the new similarity beats) or gives a legacy row the registry's id
     * when the name did not move (a legacy row that keeps its higher similarity).
     */
    private static Scenario assign(int dim, String model, String tag) {
        String tenant = "p31sql-" + tag;
        String own = coll("docs", tag + "-own", model);
        String src = coll("docs", tag + "-src", model);
        String other = coll("docs", tag + "-oth", model);
        byte[][] h = new byte[4][];
        long[] topic = new long[2];
        return new Scenario("assign_from_chashes_" + dim, tenant, true,
            e -> {
                register(e, tenant, own);
                register(e, tenant, src);
                register(e, tenant, other);
                h[0] = chunk(e, tenant, own, "same", dim, "{}", 1);
                h[1] = chunk(e, tenant, src, "fresh", dim, "{}", 1);
                h[2] = chunk(e, tenant, src, "beaten", dim, "{}", 1);
                h[3] = chunk(e, tenant, src, "keeps", dim, "{}", 1);
                topic[0] = topic(e, tenant, own, "own");
                centroid(e, tenant, own, topic[0], dim, -0.1f);   // far from every chunk, so the cross branch never picks it
                topic[1] = topic(e, tenant, other, "other");
                centroid(e, tenant, other, topic[1], dim, 0.1f);
                // legacy assignments of the cross branch: one the new similarity beats, one that keeps a similarity nothing beats
                assignment(e, tenant, h[2], topic[1], src, -0.5);
                assignment(e, tenant, h[3], topic[1], src, 5.0);
            },
            e -> {
                e.tenant(tenant);
                assignFrom(e, dim, own, new byte[][] {h[0]}, false, model, tenant);
                assignFrom(e, dim, src, new byte[][] {h[1], h[2], h[3]}, true, model, tenant);
            },
            e -> {
                assertThat(assignmentId(e, tenant, h[0], topic[0])).as("same-collection insert").isEqualTo(e.id(tenant, own));
                assertThat(assignmentId(e, tenant, h[1], topic[1])).as("cross insert").isEqualTo(e.id(tenant, src));
                assertThat(assignmentId(e, tenant, h[2], topic[1])).as("cross: the name moved, the id moved with it").isEqualTo(e.id(tenant, src));
                assertThat(assignmentId(e, tenant, h[3], topic[1])).as("cross: the name stayed, the legacy row took the registry's id").isEqualTo(e.id(tenant, src));
            });
    }

    private static void assignFrom(Env e, int dim, String coll, byte[][] chashes, boolean cross, String model, String tenant) {
        String[] hexes = new String[chashes.length];
        for (int i = 0; i < chashes.length; i++) hexes[i] = hex(chashes[i]);
        switch (dim) {
            case 384 -> e.ctx().selectFrom(ASSIGN_FROM_CHASHES_384.call(coll, hexes, cross, model, tenant)).fetch();
            case 768 -> e.ctx().selectFrom(ASSIGN_FROM_CHASHES_768.call(coll, hexes, cross, model, tenant)).fetch();
            default -> e.ctx().selectFrom(ASSIGN_FROM_CHASHES_1024.call(coll, hexes, cross, model, tenant)).fetch();
        }
    }

    /** The doc_count triggers' UPDATE of the topics row: a legacy topic row takes the id from the registry. */
    private static Scenario topicsTrigger(String name, String tag, boolean onDelete) {
        String tenant = "p31sql-" + tag;
        String own = docs(tag);
        byte[][] h = new byte[1][];
        long[] topic = new long[1];
        return new Scenario(name, tenant, true,
            e -> {
                register(e, tenant, own);
                h[0] = chunk(e, tenant, own, "member", 768, "{}", 1);
                topic[0] = topic(e, tenant, own, "t");
                if (onDelete) assignment(e, tenant, h[0], topic[0], own, 0.5);
            },
            e -> {
                if (onDelete) {
                    e.ctx().deleteFrom(TOPIC_ASSIGNMENTS).where(TOPIC_ASSIGNMENTS.TENANT_ID.eq(tenant)).and(TOPIC_ASSIGNMENTS.TOPIC_ID.eq(topic[0])).execute();
                } else {
                    e.ctx().insertInto(TOPIC_ASSIGNMENTS)
                        .set(TOPIC_ASSIGNMENTS.TENANT_ID, tenant).set(TOPIC_ASSIGNMENTS.DOC_ID, h[0]).set(TOPIC_ASSIGNMENTS.TOPIC_ID, topic[0])
                        .set(TOPIC_ASSIGNMENTS.ASSIGNED_BY, "seed").set(TOPIC_ASSIGNMENTS.SIMILARITY, 0.5)
                        .set(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, own).set(TOPIC_ASSIGNMENTS.EMBEDDING_MODEL, M768)
                        .set(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID, CollectionIds.idOf(tenant, own))
                        .execute();
                }
            },
            e -> {
                assertThat(topicId(e, tenant, topic[0])).as("topics.collection_id after the trigger's UPDATE").isEqualTo(e.id(tenant, own));
                assertThat(e.ctx().select(TOPICS.DOC_COUNT).from(TOPICS).where(TOPICS.TENANT_ID.eq(tenant)).and(TOPICS.ID.eq(topic[0])).fetchOne(TOPICS.DOC_COUNT))
                    .as("the recount itself is unchanged").isEqualTo(onDelete ? 0 : 1);
            });
    }

    /** topics_recount_doc_count corrects a drifted doc_count: the corrected legacy topic row takes the id. */
    private static Scenario topicsRecount() {
        String tenant = "p31sql-tp3";
        String own = docs("tp3");
        byte[][] h = new byte[1][];
        long[] topic = new long[1];
        return new Scenario("topics_recount_doc_count", tenant, true,
            e -> {
                register(e, tenant, own);
                h[0] = chunk(e, tenant, own, "member", 768, "{}", 1);
                topic[0] = topic(e, tenant, own, "t");
                assignment(e, tenant, h[0], topic[0], own, 0.5);
                e.ctx().update(TOPICS).set(TOPICS.DOC_COUNT, 99).where(TOPICS.TENANT_ID.eq(tenant)).and(TOPICS.ID.eq(topic[0])).execute();
            },
            e -> e.ctx().selectFrom(TOPICS_RECOUNT_DOC_COUNT.call(tenant, false)).fetch(),
            e -> {
                assertThat(topicId(e, tenant, topic[0])).as("topics.collection_id after the recount").isEqualTo(e.id(tenant, own));
                assertThat(e.ctx().select(TOPICS.DOC_COUNT).from(TOPICS).where(TOPICS.TENANT_ID.eq(tenant)).and(TOPICS.ID.eq(topic[0])).fetchOne(TOPICS.DOC_COUNT))
                    .as("the recount corrected the drift").isEqualTo(1);
            });
    }

    /** catalog_reconcile_commit, one complete walk that reaches all six of its guarded statements, plus the trash of the renamed row. */
    private static Scenario reconcile() {
        String tenant = "p31sql-rc";
        String owner = "1.7";
        String run = "run-p31-1";
        String a = coll("code", "rca", "voyage-code-3");
        String b = coll("code", "rcb", "voyage-code-3");
        return new Scenario("catalog_reconcile_commit", tenant, true,
            e -> {
                register(e, tenant, a);
                register(e, tenant, b);
                e.ctx().insertInto(CATALOG_OWNERS)
                    .set(CATALOG_OWNERS.TENANT_ID, tenant).set(CATALOG_OWNERS.TUMBLER_PREFIX, owner).set(CATALOG_OWNERS.NAME, "own-1.7")
                    .set(CATALOG_OWNERS.OWNER_TYPE, "repo").set(CATALOG_OWNERS.REPO_ROOT, "/r").set(CATALOG_OWNERS.NEXT_SEQ, 10L).execute();
                legacyDoc(e, tenant, "1.7.1", "a.py", a, "{}");                                                   // updated (1)
                legacyDoc(e, tenant, "1.7.2", "gone.py", a, "{}");                                                // absent: a miss (5)
                legacyDoc(e, tenant, "1.7.3", "kept.py", a, "{\"miss_count\": 1}");                               // present, miss reset (6)
                legacyDoc(e, tenant, "1.7.4", "old.py", a, "{\"content_hash\": \"h1\", \"non_evidentiary\": true}"); // renamed away (4), trashed
                legacyDoc(e, tenant, "1.7.5", "new.py", a, "{\"content_hash\": \"h1\"}");                          // rename target (4)
                e.ctx().update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///r/moved.py")
                    .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).and(CATALOG_DOCUMENTS.TUMBLER.eq("1.7.5")).execute();
                legacyDoc(e, tenant, "1.7.6", "old/r.py", a, "{}");                                               // repointed (2)
                e.ctx().update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///r/r.py")
                    .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).and(CATALOG_DOCUMENTS.TUMBLER.eq("1.7.6")).execute();
                e.ctx().insertInto(CATALOG_RECONCILE_RUNS)
                    .set(CATALOG_RECONCILE_RUNS.TENANT_ID, tenant).set(CATALOG_RECONCILE_RUNS.RUN_ID, run)
                    .set(CATALOG_RECONCILE_RUNS.OWNER_TUMBLER, owner).set(CATALOG_RECONCILE_RUNS.WALK, "complete")
                    .set(CATALOG_RECONCILE_RUNS.EXPECTED_ROWS, 5).execute();
                staged(e, tenant, run, 1, "a.py", b, "H1", "");          // moved to b with a new head: update
                staged(e, tenant, run, 2, "kept.py", a, "H0", "");       // unchanged
                staged(e, tenant, run, 3, "new.py", a, "H0", "h1");      // the rename target, unchanged
                staged(e, tenant, run, 4, "r.py", b, "H0", "");          // same uri as 1.7.6, other collection: repoint
                staged(e, tenant, run, 5, "n.py", a, "H0", "");          // a new file: insert
            },
            e -> {
                e.tenant(tenant);
                Routines.catalogReconcileCommit(e.ctx().configuration(), owner, run);
            },
            e -> {
                assertThat(docId(e, tenant, "1.7.1")).as("(1) update moved the name, the id followed").isEqualTo(e.id(tenant, b));
                assertThat(docId(e, tenant, "1.7.6")).as("(2) repoint moved the name, the id followed").isEqualTo(e.id(tenant, b));
                assertThat(docId(e, tenant, "1.7.2")).as("(5) the miss count update set the id").isEqualTo(e.id(tenant, a));
                assertThat(docId(e, tenant, "1.7.3")).as("(6) the miss reset set the id").isEqualTo(e.id(tenant, a));
                assertThat(docId(e, tenant, "1.7.5")).as("(4) the rename flag update set the id").isEqualTo(e.id(tenant, a));
                assertThat(docId(e, tenant, "1.7.4")).as("document_trash of the renamed row set the id").isEqualTo(e.id(tenant, a));
                List<Long> created = e.ctx().select(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID).from(CATALOG_DOCUMENTS)
                    .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).and(CATALOG_DOCUMENTS.FILE_PATH.eq("n.py"))
                    .fetch(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID);
                assertThat(created).as("(3) the insert created the row").hasSize(1);
                assertThat(created.get(0)).as("(3) the insert carries the id").isEqualTo(e.id(tenant, a));
            });
    }

    private static void legacyDoc(Env e, String tenant, String tumbler, String filePath, String physical, String metadataJson) {
        e.ctx().insertInto(CATALOG_DOCUMENTS)
            .set(CATALOG_DOCUMENTS.TENANT_ID, tenant).set(CATALOG_DOCUMENTS.TUMBLER, tumbler)
            .set(CATALOG_DOCUMENTS.TITLE, filePath).set(CATALOG_DOCUMENTS.CONTENT_TYPE, "code").set(CATALOG_DOCUMENTS.FILE_PATH, filePath)
            .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, physical).set(CATALOG_DOCUMENTS.HEAD_HASH, "H0")
            .set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///r/" + filePath)
            .set(CATALOG_DOCUMENTS.METADATA, JSONB.valueOf(metadataJson))
            .execute();
    }

    private static void staged(Env e, String tenant, String run, int seq, String path, String physical, String head, String contentHash) {
        var step = e.ctx().insertInto(CATALOG_RECONCILE_ROWS)
            .set(CATALOG_RECONCILE_ROWS.TENANT_ID, tenant).set(CATALOG_RECONCILE_ROWS.RUN_ID, run).set(CATALOG_RECONCILE_ROWS.SEQ, seq)
            .set(CATALOG_RECONCILE_ROWS.KIND, "present").set(CATALOG_RECONCILE_ROWS.FILE_PATH, path)
            .set(CATALOG_RECONCILE_ROWS.CONTENT_TYPE, "code").set(CATALOG_RECONCILE_ROWS.TITLE, path)
            .set(CATALOG_RECONCILE_ROWS.PHYSICAL_COLLECTION, physical).set(CATALOG_RECONCILE_ROWS.HEAD_HASH, head);
        if (!contentHash.isEmpty()) {
            step = step.set(CATALOG_RECONCILE_ROWS.CONTENT_HASH, contentHash)
                .set(CATALOG_RECONCILE_ROWS.BASE_METADATA, JSONB.valueOf("{\"content_hash\": \"" + contentHash + "\"}"));
        }
        step.execute();
    }

    /** document_trash of an unstamped document: the UPDATE sets deleted_at and the id. */
    private static Scenario trash() {
        String tenant = "p31sql-dt";
        String own = docs("dt");
        return new Scenario("document_trash", tenant, true,
            e -> {
                register(e, tenant, own);
                doc(e, tenant, "dt.1", own);
            },
            e -> {
                e.tenant(tenant);
                Routines.documentTrash(e.ctx().configuration(), "dt.1");
            },
            e -> {
                assertThat(docId(e, tenant, "dt.1")).as("document_trash set the id").isEqualTo(e.id(tenant, own));
                assertThat(e.ctx().fetchExists(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq("dt.1"))
                    .and(CATALOG_DOCUMENTS.DELETED_AT.isNotNull()))).as("the document is trashed").isTrue();
            });
    }

    /** document_restore of an unstamped, trashed document. */
    private static Scenario restoreDoc() {
        String tenant = "p31sql-dr";
        String own = docs("dr");
        return new Scenario("document_restore", tenant, true,
            e -> {
                register(e, tenant, own);
                doc(e, tenant, "dr.1", own);
                e.ctx().update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.DELETED_AT, OffsetDateTime.now())
                    .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).execute();
            },
            e -> {
                e.tenant(tenant);
                Routines.documentRestore(e.ctx().configuration(), "dr.1");
            },
            e -> {
                assertThat(docId(e, tenant, "dr.1")).as("document_restore set the id").isEqualTo(e.id(tenant, own));
                assertThat(e.ctx().fetchExists(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq("dr.1"))
                    .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))).as("the document is restored").isTrue();
            });
    }

    /**
     * The owner trigger of catalog-044 repairs a collection's owner_id from its documents with an UPDATE of the registry row. When
     * that row is an unstamped tombstone, the UPDATE must set superseded_by_id: the successor's id, or, residual R7, the row's own id
     * when the successor is gone.
     */
    private static Scenario ownerToCollection() {
        String tenant = "p31sql-ow";
        String tomb = coll("code", "owtomb", "voyage-code-3").replace("p31sql-owtomb", "arcane-owtomb");
        String orphan = coll("code", "oworph", "voyage-code-3").replace("p31sql-oworph", "arcane-oworph");
        String successor = coll("code", "owsucc", "voyage-code-3");
        return new Scenario("catalog_documents_owner_to_collection", tenant, true,
            e -> {
                register(e, tenant, tomb);
                register(e, tenant, orphan);
                register(e, tenant, successor);
                e.ctx().update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, successor)
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(tomb)).execute();
                e.ctx().update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, "code__gone__voyage-code-3__v1")
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(orphan)).execute();
            },
            e -> {
                // a live document of the collection with a tumbler-shaped owner: the trigger moves the registry row's owner
                e.ctx().insertInto(CATALOG_DOCUMENTS)
                    .set(CATALOG_DOCUMENTS.TENANT_ID, tenant).set(CATALOG_DOCUMENTS.TUMBLER, "1.9.1").set(CATALOG_DOCUMENTS.TITLE, "d1")
                    .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, tomb)
                    .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, CollectionIds.idOf(tenant, tomb)).execute();
                e.ctx().insertInto(CATALOG_DOCUMENTS)
                    .set(CATALOG_DOCUMENTS.TENANT_ID, tenant).set(CATALOG_DOCUMENTS.TUMBLER, "1.9.2").set(CATALOG_DOCUMENTS.TITLE, "d2")
                    .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, orphan)
                    .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, CollectionIds.idOf(tenant, orphan)).execute();
            },
            e -> {
                for (String name : List.of(tomb, orphan)) {
                    assertThat(e.ctx().select(CATALOG_COLLECTIONS.OWNER_ID).from(CATALOG_COLLECTIONS)
                            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(name)).fetchOne(CATALOG_COLLECTIONS.OWNER_ID))
                        .as("non-vacuity: the trigger ran and repaired the owner of %s", name).isEqualTo("1-9");
                }
                assertThat(supersededId(e, tenant, tomb)).as("a tombstone takes its successor's id").isEqualTo(e.id(tenant, successor));
                assertThat(supersededId(e, tenant, orphan)).as("R7: a tombstone whose successor is gone takes its own id").isEqualTo(e.id(tenant, orphan));
            });
    }

    private static Long supersededId(Env e, String tenant, String name) {
        return e.ctx().select(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(name)).fetchOne(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID);
    }

    /** canary_query_ring_upsert: the ring row carries the id; an entry whose collection has no registry row is skipped, not refused. */
    private static Scenario canary() {
        String tenant = "p31sql-cy";
        String own = docs("cy");
        String vec = "[" + "0.1,".repeat(767) + "0.1]";
        return new Scenario("canary_query_ring_upsert", tenant, true,
            e -> register(e, tenant, own),
            e -> {
                Integer written = Routines.canaryQueryRingUpsert(e.ctx().configuration(), tenant,
                    new String[] {own, "docs__p31sql-never-registered__" + M768 + "__v1"}, new String[] {M768, M768},
                    new String[] {vec, vec}, new byte[][] {Chash.ofText("digest one").toBytes(), Chash.ofText("digest two").toBytes()});
                assertThat(written).as("one entry written, the unregistered collection's skipped").isEqualTo(1);
            },
            e -> {
                List<Long> ids = e.ctx().select(CANARY_QUERY_RING.COLLECTION_ID).from(CANARY_QUERY_RING)
                    .where(CANARY_QUERY_RING.TENANT_ID.eq(tenant)).fetch(CANARY_QUERY_RING.COLLECTION_ID);
                assertThat(ids).as("one ring row").hasSize(1);
                assertThat(ids.get(0)).isEqualTo(e.id(tenant, own));
            });
    }

    // ---- the test ----------------------------------------------------------------------------------------------------

    @Test
    void everyFunctionIsRefusedBeforeAndCarriesTheIdAfter_keepsItsAttributes_andItsRollbackRestoresTheBody() throws Exception {
        List<Scenario> scenarios = scenarios();
        assertThat(scenarios).as("non-vacuity: the scenario list").hasSizeGreaterThanOrEqualTo(26);

        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
            }
            try (HikariDataSource ds = adminPool(pg)) {
                // phase 1: the older layout, so the seeded rows are the unstamped rows of an install the CHECKs reach
                migrateUpTo(ds, EXPAND_FIRST);
                List<String> failures = new ArrayList<>();
                try (Connection su = pg.createConnection("")) {
                    su.setAutoCommit(true);
                    PgContainerHelper.installTestObjectsAfterProductSchema(su); // the restore seeds drop and re-add the manifest's chunk foreign key
                    Env env = new Env(su);
                    assertThat(CollectionIdSeeds.hasIdColumns(env.ctx())).as("the seeds run on the layout before vectors-068").isFalse();
                    for (Scenario sc : scenarios) {
                        guard(failures, "seed " + sc.name(), () -> sc.seed().run(env));
                    }
                }
                assertThat(failures).as("the seeds").isEmpty();

                // phase 2: vectors-068 applied, vectors-069 not: the CHECKs refuse each call
                migrateUpTo(ds, DUAL_FIRST);
                tag(ds, TAG);
                Map<String, PgCatalogProbes.FunctionDefinition> before;
                int refused = 0;
                try (Connection su = pg.createConnection("")) {
                    su.setAutoCommit(true);
                    Env env = new Env(su);
                    assertThat(CollectionIdSeeds.hasIdColumns(env.ctx())).isTrue();
                    before = PgCatalogProbes.functionDefinitionsIn(env.ctx(), "nexus");
                    for (Scenario sc : scenarios) {
                        if (!sc.refusedBefore()) continue;
                        refused++;
                        env.tenant(sc.tenant());
                        String state = refusal(() -> sc.call().run(env));
                        if (state == null || !state.contains("23514") || !(state.contains("_id_check") || state.contains("collection_id_not_null"))) {
                            failures.add("before vectors-069, " + sc.name() + " should be refused with 23514 by a collection id CHECK, was: " + state);
                        }
                    }
                }
                assertThat(refused).as("non-vacuity: scenarios refused before the changeset").isGreaterThanOrEqualTo(20);
                assertThat(failures).as("the refusals before vectors-069").isEmpty();

                // phase 3: vectors-069 applied: the same calls succeed and carry the ids
                SchemaMigrator.migrate(ds);
                try (Connection su = pg.createConnection("")) {
                    su.setAutoCommit(true);
                    Env env = new Env(su);
                    for (Scenario sc : scenarios) {
                        env.tenant(sc.tenant());
                        guard(failures, "call " + sc.name(), () -> sc.call().run(env));
                        guard(failures, "verify " + sc.name(), () -> sc.verify().run(env));
                    }
                    assertThat(failures).as("the calls and the ids after vectors-069").isEmpty();

                    var after = PgCatalogProbes.functionDefinitionsIn(env.ctx(), "nexus");
                    assertThat(after.keySet()).as("no function appeared or went").isEqualTo(before.keySet());
                    Set<String> redefined = redefinedFunctions();
                    int same = 0;
                    int changed = 0;
                    for (var entry : before.entrySet()) {
                        var b = entry.getValue();
                        var a = after.get(entry.getKey());
                        String fn = entry.getKey().substring(0, entry.getKey().indexOf('('));
                        if (redefined.contains(fn)) {
                            changed++;
                            assertThat(a.body()).as("%s has a new body", entry.getKey()).isNotEqualTo(b.body());
                            // attributes: everything the definition says other than the body
                            assertThat(a.arguments()).as("%s arguments", fn).isEqualTo(b.arguments());
                            assertThat(a.result()).as("%s result", fn).isEqualTo(b.result());
                            assertThat(a.volatility()).as("%s volatility", fn).isEqualTo(b.volatility());
                            assertThat(a.language()).as("%s language", fn).isEqualTo(b.language());
                            assertThat(a.securityDefiner()).as("%s SECURITY mode", fn).isEqualTo(b.securityDefiner());
                            assertThat(a.config()).as("%s SET clauses (search_path and friends)", fn).isEqualTo(b.config());
                            assertThat(a.owner()).as("%s owner", fn).isEqualTo(b.owner());
                            assertThat(a.acl()).as("%s grants", fn).isEqualTo(b.acl());
                        } else {
                            same++;
                            assertThat(a.definition()).as("%s is not redefined by this changeset", entry.getKey()).isEqualTo(b.definition());
                            assertThat(a.acl()).as("%s grants", fn).isEqualTo(b.acl());
                        }
                    }
                    assertThat(changed).as("non-vacuity: functions redefined").isEqualTo(24);
                    assertThat(same).as("non-vacuity: functions compared and unchanged").isGreaterThan(100);
                }

                // phase 4: the rollback restores every previous body verbatim
                rollbackTo(ds, TAG);
                try (Connection su = pg.createConnection("")) {
                    var restored = PgCatalogProbes.functionDefinitionsIn(DSL.using(su, SQLDialect.POSTGRES), "nexus");
                    for (var entry : before.entrySet()) {
                        assertThat(restored.get(entry.getKey()).definition()).as("%s after the rollback", entry.getKey())
                            .isEqualTo(entry.getValue().definition());
                        assertThat(restored.get(entry.getKey()).acl()).as("%s grants after the rollback", entry.getKey())
                            .isEqualTo(entry.getValue().acl());
                    }
                }
                assertThat(recorded(pg, DUAL_FIRST)).as("the changeset is no longer recorded after the rollback").isFalse();
                SchemaMigrator.migrate(ds); // and the walk goes forward again
            }
        } finally {
            pg.stop();
        }
    }

    // ---- the census ----------------------------------------------------------------------------------------------------

    @Test
    void theChangelogRedefinesEveryCensusFunction_andNoOtherFunctionWritesAGuardedTable() throws Exception {
        Set<String> census = censusFunctions();
        Set<String> redefined = redefinedFunctions();
        assertThat(census).as("non-vacuity: the census names functions").hasSizeGreaterThanOrEqualTo(25);
        Set<String> expected = new TreeSet<>(census);
        expected.removeAll(NOT_REDEFINED);
        assertThat(redefined).as("the changelog redefines exactly the census functions less the ones left alone").isEqualTo(expected);
        assertThat(NOT_REDEFINED).as("every function left alone is in the census").isSubsetOf(census);

        // the live scan: a function body outside the changeset that inserts into or updates a guarded table is a writer the census missed
        Set<String> guarded = guardedTables();
        assertThat(guarded).as("non-vacuity: the guarded tables").hasSizeGreaterThanOrEqualTo(20);
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE + "_c", ADMIN_PASS);
            }
            try (HikariDataSource ds = adminPool(pg, ADMIN_ROLE + "_c")) {
                SchemaMigrator.migrate(ds);
            }
            try (Connection su = pg.createConnection("")) {
                var defs = PgCatalogProbes.functionDefinitionsIn(DSL.using(su, SQLDialect.POSTGRES), "nexus");
                Pattern writer = Pattern.compile("(?is)\\b(insert\\s+into|update)\\s+(?:nexus\\.)?(" + String.join("|", guarded) + ")\\b");
                Set<String> found = new TreeSet<>();
                int examined = 0;
                for (var d : defs.values()) {
                    if (!"plpgsql".equals(d.language()) && !"sql".equals(d.language())) continue;
                    examined++;
                    Matcher m = writer.matcher(stripComments(d.body()));
                    if (m.find()) found.add(d.identity().substring(0, d.identity().indexOf('(')));
                }
                assertThat(examined).as("non-vacuity: function bodies scanned").isGreaterThan(100);
                assertThat(found).as("non-vacuity: the scan sees the writers the changeset redefines (a scan that found none is a broken scan)")
                    .containsAll(Set.of("gc_quarantine_orphans", "catalog_reconcile_commit", "document_trash", "topics_recount_doc_count",
                        "stamp_chunks_on_manifest_delete", "canary_query_ring_upsert", "assign_from_chashes_768"));
                found.removeAll(redefined);
                found.removeAll(NOT_REDEFINED);
                assertThat(found).as("functions outside vectors-069 that write a guarded table (each needs the id, or a recorded reason)")
                    .isEqualTo(KNOWN_OTHER_WRITERS);
            }
        } finally {
            pg.stop();
        }
    }

    /**
     * Functions that match the writer scan and need nothing from this changeset, each with the reason. The reader of the list can
     * check it against the scan: an addition here is a decision, not a default.
     */
    private static final Set<String> KNOWN_OTHER_WRITERS = new TreeSet<>(Set.of());

    private static String stripComments(String body) {
        return body.replaceAll("(?m)--.*$", "").replaceAll("(?s)/\\*.*?\\*/", "");
    }

    private static Set<String> censusFunctions() throws Exception {
        Set<String> out = new TreeSet<>();
        Pattern p = Pattern.compile("^nexus\\.(\\w+) \\[");
        for (String line : Files.readAllLines(CENSUS)) {
            if (line.startsWith("#") || line.isBlank()) continue;
            String[] f = line.split("\t");
            if (f.length > 5 && f[5].equals("sql_function")) {
                Matcher m = p.matcher(f[2]);
                assertThat(m.find()).as("census row names its function: %s", line).isTrue();
                out.add(m.group(1));
            }
        }
        return out;
    }

    private static Set<String> redefinedFunctions() throws Exception {
        Set<String> out = new LinkedHashSet<>();
        Matcher m = Pattern.compile("(?i)CREATE\\s+OR\\s+REPLACE\\s+FUNCTION\\s+nexus\\.(\\w+)\\(").matcher(stripXmlComments(Files.readString(CHANGELOG_FILE)));
        while (m.find()) out.add(m.group(1));
        return new TreeSet<>(out);
    }

    private static String stripXmlComments(String xml) {
        return xml.replaceAll("(?s)<!--.*?-->", "");
    }

    private static Set<String> guardedTables() throws Exception {
        Set<String> out = new TreeSet<>();
        for (String line : Files.readAllLines(CHECKS)) {
            if (line.isBlank() || line.startsWith("#") || line.startsWith("table\t")) continue;
            out.add(line.split("\t")[0]);
        }
        return out;
    }

    // ---- helpers -----------------------------------------------------------------------------------------------------------

    private interface Body {
        void run() throws Exception;
    }

    private static void guard(List<String> failures, String what, Body b) {
        try {
            b.run();
        } catch (Throwable t) {
            failures.add(what + ": " + describe(t));
        }
    }

    private static String describe(Throwable t) {
        StringBuilder sb = new StringBuilder(t.getClass().getSimpleName()).append(": ").append(t.getMessage());
        for (Throwable c = t.getCause(); c != null && c != c.getCause(); c = c.getCause()) {
            sb.append(" <- ").append(c.getClass().getSimpleName()).append(": ").append(c.getMessage());
            if (c.getCause() == c) break;
        }
        return sb.toString();
    }

    /** The SQLSTATE and message of the failure, "state: message" (the message of a wrapped SQLSTATE is in the text); null when the call succeeded. */
    private static String refusal(Body b) {
        try {
            b.run();
            return null;
        } catch (Throwable t) {
            String state = null;
            for (Throwable c = t; c != null; c = c.getCause()) {
                if (c instanceof SQLException se && se.getSQLState() != null) {
                    state = se.getSQLState();
                    break;
                }
                if (c.getCause() == c) break;
            }
            return state + ": " + describe(t);
        }
    }

    private static HikariDataSource adminPool(PostgreSQLContainer<?> pg) {
        return adminPool(pg, ADMIN_ROLE);
    }

    private static HikariDataSource adminPool(PostgreSQLContainer<?> pg, String role) {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(role);
        cfg.setPassword(ADMIN_PASS);
        cfg.setMaximumPoolSize(2);
        cfg.setPoolName("nexus-admin-p31-sqlfn");
        return new HikariDataSource(cfg);
    }

    /** Applies the HEAD changelog up to, but not including, {@code changesetId}. */
    private static void migrateUpTo(HikariDataSource ds, String changesetId) throws Exception {
        try (Connection conn = ds.getConnection()) {
            Database database = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                List<liquibase.changelog.ChangeSet> unrun = liquibase.listUnrunChangeSets(new Contexts(), new LabelExpression());
                int idx = -1;
                for (int i = 0; i < unrun.size(); i++) {
                    if (changesetId.equals(unrun.get(i).getId())) {
                        idx = i;
                        break;
                    }
                }
                assertThat(idx).as("%s must be pending in the HEAD changelog", changesetId).isGreaterThanOrEqualTo(0);
                liquibase.update(idx, new Contexts(), new LabelExpression());
            }
        }
    }

    private static void tag(HikariDataSource ds, String tag) throws Exception {
        try (Connection conn = ds.getConnection()) {
            Database database = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                liquibase.tag(tag);
            }
        }
    }

    private static void rollbackTo(HikariDataSource ds, String tag) throws Exception {
        try (Connection conn = ds.getConnection()) {
            Database database = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                liquibase.rollback(tag, new Contexts(), new LabelExpression());
            }
        }
    }

    private static boolean recorded(PostgreSQLContainer<?> pg, String id) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).fetchExists(DSL.selectOne().from(DSL.table(DSL.name("public", "databasechangelog")))
                .where(DSL.field(DSL.name("id"), String.class).eq(id)));
        }
    }
}
