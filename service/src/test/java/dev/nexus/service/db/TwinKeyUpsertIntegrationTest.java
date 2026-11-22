// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import dev.nexus.service.AtomicWriteTestBase;
import dev.nexus.service.db.TwinKeyUpsert.Arbiter;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.vectors.DimTables;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;
import org.slf4j.LoggerFactory;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.Callable;
import java.util.concurrent.CyclicBarrier;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.Function;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * {@link TwinKeyUpsert} against a real migrated schema (RDR-229, nexus-p2m1x.20 finding #7), for both pairs of
 * {@link TwinKeyUpsert#pairs()}: {@code nexus.chunks} and {@code nexus.taxonomy_centroids}.
 *
 * <p><b>How a row trips only one member of a pair.</b> A leaf has two unique indexes over the same rows: the name key
 * {@code (tenant_id, collection, k, embedding_model)} (the primary key) and the id key
 * {@code (tenant_id, collection_id, k, embedding_model)} ({@code <leaf>_cid_uq}). The only CHECK between a row's name and
 * id is that both are set or both unset ({@code chunks_collection_id_check}); nothing makes them name the same collection.
 * With two collections A and B of one tenant and model, and a row X at (A, id of A, k):
 * <ul>
 *   <li>(B, id of A, k) collides with X on the id key only: the name differs, so the primary key is clear;</li>
 *   <li>(A, id of B, k) collides with X on the name key only: the id differs, so {@code <leaf>_cid_uq} is clear.</li>
 * </ul>
 * Both are plain INSERTs (no {@code ON CONFLICT}), so the violated index is the one that collided.
 * {@link #theTwoCollisionShapesTripOnlyTheirOwnIndex} pins that against the catalog's own names before any other test
 * relies on it. Both rows satisfy every other constraint: the collection and model foreign keys resolve (A and B are
 * registered under one model) and the CHECK sees a set name and a set id.
 *
 * <p>An {@code ON CONFLICT} on the id key is not available on these tables (an {@code ON CONFLICT} on a partitioned
 * table needs a unique index on the parent, and the id key has none until P3.3b's constraint pass), so the
 * {@link Arbiter#ID_KEY} tests use a plain INSERT for the retry's second call.
 *
 * <p>Test-tree gates: no raw SQL, so the catalog reads are typed over {@code pg_catalog} the way
 * {@code IdKeyIndexProbe} does it, and the rows are built through generated jOOQ.
 */
class TwinKeyUpsertIntegrationTest extends AtomicWriteTestBase {

    private static final org.slf4j.Logger LOG = LoggerFactory.getLogger(TwinKeyUpsertIntegrationTest.class);
    private static final String MODEL = "minilm-l6-v2-384";
    private static final String RETRY_EVENT = "event=twin_key_upsert_retry";
    private static final AtomicLong KEYS = new AtomicLong(1_000);
    private static final Table<?> PG_CLASS = DSL.table(DSL.name("pg_catalog", "pg_class"));
    private static final Table<?> PG_NAMESPACE = DSL.table(DSL.name("pg_catalog", "pg_namespace"));
    private static final Table<?> PG_INHERITS = DSL.table(DSL.name("pg_catalog", "pg_inherits"));
    private static final Table<?> PG_INDEX = DSL.table(DSL.name("pg_catalog", "pg_index"));

    // -- the rows of each pair's table ---------------------------------------------------------------------------------

    /** One table of {@link TwinKeyUpsert#pairs()}: how to build, write and count its rows. {@code key} is the {@code chash} or the {@code topic_id}. */
    private interface Subject {
        String table();

        Object freshKey();

        /** A row at {@code (TENANT, nameCollection, key, MODEL)} carrying {@code collectionId} (null: unset). */
        Map<Field<?>, Object> row(String nameCollection, Long collectionId, Object key);

        /** {@code INSERT ... ON CONFLICT (the name key) DO UPDATE}, the shape of the production writers. */
        int upsertOnNameKey(DSLContext ctx, Map<Field<?>, Object> row);

        /** A plain INSERT: every unique index of the leaf is checked and a violation is the leaf's own 23505. */
        int insert(DSLContext ctx, Map<Field<?>, Object> row);

        int count(DSLContext ctx, String nameCollection, Object key);
    }

    private static float[] unit() {
        float[] v = new float[384];
        v[0] = 1f;
        return v;
    }

    private static final Subject CHUNKS_SUBJECT = new Subject() {
        public String table() {
            return "chunks";
        }

        public Object freshKey() {
            return Chash.ofText("twin-key-" + KEYS.incrementAndGet()).toBytes();
        }

        public Map<Field<?>, Object> row(String nameCollection, Long collectionId, Object key) {
            var m = new LinkedHashMap<Field<?>, Object>();
            m.put(CHUNKS.TENANT_ID, TENANT);
            m.put(CHUNKS.COLLECTION, nameCollection);
            m.put(CHUNKS.CHASH, key);
            m.put(CHUNKS.EMBEDDING_MODEL, MODEL);
            m.put(CHUNKS.COLLECTION_ID, collectionId);
            m.put(CHUNKS.CHUNK_TEXT, "twin key text");
            m.put(CHUNKS.EMBEDDING_384, Vector.of(unit()));
            return m;
        }

        public int upsertOnNameKey(DSLContext ctx, Map<Field<?>, Object> row) {
            return ctx.insertInto(CHUNKS).set(row)
                .onConflict(CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL)
                .doUpdate().set(CHUNKS.CHUNK_TEXT, DSL.excluded(CHUNKS.CHUNK_TEXT))
                .execute();
        }

        public int insert(DSLContext ctx, Map<Field<?>, Object> row) {
            return ctx.insertInto(CHUNKS).set(row).execute();
        }

        public int count(DSLContext ctx, String nameCollection, Object key) {
            return ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(nameCollection))
                .and(CHUNKS.CHASH.eq((byte[]) key)));
        }
    };

    private static final Subject CENTROIDS_SUBJECT = new Subject() {
        private final DimTables.CentroidTable ct = DimTables.CENTROIDS.get(384);

        public String table() {
            return "taxonomy_centroids";
        }

        public Object freshKey() {
            return KEYS.incrementAndGet();
        }

        public Map<Field<?>, Object> row(String nameCollection, Long collectionId, Object key) {
            var m = new LinkedHashMap<Field<?>, Object>();
            m.put(ct.tenantId(), TENANT);
            m.put(ct.collection(), nameCollection);
            m.put(ct.topicId(), key);
            m.put(ct.embeddingModel(), MODEL);
            m.put(TAXONOMY_CENTROIDS.COLLECTION_ID, collectionId);
            m.put(ct.embedding(), Vector.of(unit()));
            m.put(ct.label(), "twin key label");
            m.put(ct.docCount(), 1);
            return m;
        }

        public int upsertOnNameKey(DSLContext ctx, Map<Field<?>, Object> row) {
            return ctx.insertInto(ct.table()).set(row)
                .onConflict(ct.tenantId(), ct.collection(), ct.topicId(), ct.embeddingModel())
                .doUpdate().set(ct.label(), DSL.excluded(ct.label()))
                .execute();
        }

        public int insert(DSLContext ctx, Map<Field<?>, Object> row) {
            return ctx.insertInto(ct.table()).set(row).execute();
        }

        public int count(DSLContext ctx, String nameCollection, Object key) {
            return ctx.fetchCount(ct.table(), ct.tenantId().eq(TENANT).and(ct.collection().eq(nameCollection))
                .and(ct.topicId().eq((Long) key)));
        }
    };

    private static final List<Subject> SUBJECTS = List.of(CHUNKS_SUBJECT, CENTROIDS_SUBJECT);

    // -- two registered collections of one tenant and model ---------------------------------------------------------------

    private record Cols(String a, long idA, String b, long idB) { }

    /** Registers A and B (which also creates the tenant's leaves under every model partition) and reads their registry ids. */
    private Cols twoCollections() {
        int n = seq.incrementAndGet();
        String a = "code__tka" + n + "__" + MODEL + "__v1";
        String b = "code__tkb" + n + "__" + MODEL + "__v1";
        registerCollection(a);
        registerCollection(b);
        return tenantScope.withTenant(TENANT, ctx ->
            new Cols(a, CollectionIds.require(ctx, TENANT, a), b, CollectionIds.require(ctx, TENANT, b)));
    }

    /** The rows at the key as a fresh transaction sees them, so only what committed. */
    private int committedCount(Subject s, String nameCollection, Object key) {
        return tenantScope.withTenant(TENANT, ctx -> s.count(ctx, nameCollection, key));
    }

    private void seed(Subject s, Cols c, Object key) {
        tenantScope.withTenant(TENANT, ctx -> s.insert(ctx, s.row(c.a(), c.idA(), key)));
    }

    // -- the catalog's own view of a leaf (typed, independent of the helper's query) ----------------------------------------

    private record LeafIndex(String name, List<String> columns) { }

    private static Field<Object> col(String alias, String column) {
        return DSL.field(DSL.name(alias, column));
    }

    private static Field<String> text(String alias, String column) {
        return DSL.field(DSL.name(alias, column), String.class);
    }

    private static final Pattern INDEX_COLUMNS = Pattern.compile("USING btree \\((.*?)\\)");

    /**
     * Every unique index of the leaf of {@code table} for ({@link #TENANT}, {@link #MODEL}): the relation whose partition bound is the
     * tenant, under the model partition whose bound is the model, under the table. Columns are parsed from {@code pg_get_indexdef}.
     */
    private List<LeafIndex> leafUniqueIndexes(String table) throws Exception {
        Field<String> leafBound = DSL.function(DSL.name("pg_catalog", "pg_get_expr"), String.class,
            col("leaf", "relpartbound"), col("leaf", "oid"));
        Field<String> modelBound = DSL.function(DSL.name("pg_catalog", "pg_get_expr"), String.class,
            col("mp", "relpartbound"), col("mp", "oid"));
        Field<String> def = DSL.function(DSL.name("pg_catalog", "pg_get_indexdef"), String.class, col("i", "indexrelid"));
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var rows = DSL.using(su, SQLDialect.POSTGRES)
                .select(text("ic", "relname"), def)
                .from(PG_CLASS.as("leaf"))
                .join(PG_INHERITS.as("li")).on(col("li", "inhrelid").eq(col("leaf", "oid")))
                .join(PG_CLASS.as("mp")).on(col("mp", "oid").eq(col("li", "inhparent")))
                .join(PG_INHERITS.as("mi")).on(col("mi", "inhrelid").eq(col("mp", "oid")))
                .join(PG_CLASS.as("parent")).on(col("parent", "oid").eq(col("mi", "inhparent")))
                .join(PG_NAMESPACE.as("n")).on(col("n", "oid").eq(col("parent", "relnamespace")))
                .join(PG_INDEX.as("i")).on(col("i", "indrelid").eq(col("leaf", "oid")))
                .join(PG_CLASS.as("ic")).on(col("ic", "oid").eq(col("i", "indexrelid")))
                .where(text("n", "nspname").eq("nexus"))
                .and(text("parent", "relname").eq(table))
                .and(leafBound.eq("FOR VALUES IN ('" + TENANT + "')"))
                .and(modelBound.eq("FOR VALUES IN ('" + MODEL + "')"))
                .and(DSL.field(DSL.name("i", "indisunique"), Boolean.class))
                .orderBy(text("ic", "relname"))
                .fetch();
            List<LeafIndex> out = new ArrayList<>();
            for (var r : rows) {
                Matcher m = INDEX_COLUMNS.matcher(r.value2());
                assertThat(m.find()).as("index definition %s", r.value2()).isTrue();
                out.add(new LeafIndex(r.value1(), List.of(m.group(1).split(",\\s*"))));
            }
            return out;
        }
    }

    /** The leaf's unique index over exactly {@code columns}; exactly one must exist. */
    private LeafIndex indexOver(String table, List<String> columns) throws Exception {
        var all = leafUniqueIndexes(table);
        var hits = all.stream().filter(i -> i.columns().equals(columns)).toList();
        assertThat(hits).as("%s: leaf unique indexes %s, wanted exactly one over %s", table, all, columns).hasSize(1);
        return hits.get(0);
    }

    // -- log capture ---------------------------------------------------------------------------------------------------------

    /** Attaches a list appender to {@link TwinKeyUpsert}'s logger at INFO for the length of a try block. */
    private static final class RetryLog implements AutoCloseable {
        private final Logger logger = (Logger) LoggerFactory.getLogger(TwinKeyUpsert.class);
        private final ListAppender<ILoggingEvent> appender = new ListAppender<>();
        private final Level prior = logger.getLevel();

        RetryLog() {
            logger.setLevel(Level.INFO);
            appender.start();
            logger.addAppender(appender);
        }

        /** The retry events logged so far, formatted. */
        List<String> retries() {
            synchronized (appender) {
                return appender.list.stream().map(ILoggingEvent::getFormattedMessage)
                    .filter(m -> m.contains(RETRY_EVENT)).toList();
            }
        }

        @Override
        public void close() {
            logger.detachAppender(appender);
            appender.stop();
            logger.setLevel(prior);
        }
    }

    private static String sqlState(Throwable t) {
        for (Throwable c = t; c != null; c = c.getCause()) {
            if (c instanceof java.sql.SQLException se) {
                return se.getSQLState();
            }
        }
        return null;
    }

    private static String constraintOf(Throwable t) {
        assertThat(SqlConstraints.isUniqueViolation(t)).as("a 23505: %s", t).isTrue();
        return SqlConstraints.violated(t);
    }

    // -- 1: the pairs against the migrated schema -----------------------------------------------------------------------------

    /**
     * Every {@link TwinKeyUpsert#pairs()} entry against a real leaf: a unique index over exactly the name key and a different one over
     * exactly the id key. The helper's own catalog query ({@code isMember}) is then checked against that independent reading: the right
     * pairing is a member, the swapped pairing, a made-up index and the other table's index are not.
     */
    @Test
    void pairsMatchTheMigratedSchema() throws Exception {
        twoCollections();
        var pairs = TwinKeyUpsert.pairs();
        assertThat(pairs).as("non-vacuity: the helper has pairs").isNotEmpty();
        Set<String> checked = new LinkedHashSet<>();
        Map<String, LeafIndex> nameIdx = new LinkedHashMap<>(), idIdx = new LinkedHashMap<>();
        for (var pair : pairs) {
            var name = indexOver(pair.table(), pair.nameKey());
            var id = indexOver(pair.table(), pair.idKey());
            assertThat(name.name()).as("%s: two different indexes", pair.table()).isNotEqualTo(id.name());
            nameIdx.put(pair.table(), name);
            idIdx.put(pair.table(), id);
            checked.add(pair.table());
        }
        assertThat(checked).as("non-vacuity: both tables were checked")
            .contains("chunks", "taxonomy_centroids")
            .containsExactlyInAnyOrderElementsOf(pairs.stream().map(TwinKeyUpsert.Pair::table).toList());

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (var pair : pairs) {
                String t = pair.table();
                assertThat(TwinKeyUpsert.isMember(ctx, nameIdx.get(t).name(), pair, pair.nameKey()))
                    .as("%s: the name index is a member as the name key", t).isTrue();
                assertThat(TwinKeyUpsert.isMember(ctx, idIdx.get(t).name(), pair, pair.idKey()))
                    .as("%s: the id index is a member as the id key", t).isTrue();
                assertThat(TwinKeyUpsert.isMember(ctx, idIdx.get(t).name(), pair, pair.nameKey()))
                    .as("%s: the id index is not the name key", t).isFalse();
                assertThat(TwinKeyUpsert.isMember(ctx, nameIdx.get(t).name(), pair, pair.idKey()))
                    .as("%s: the name index is not the id key", t).isFalse();
                assertThat(TwinKeyUpsert.isMember(ctx, "no_such_index_" + t, pair, pair.idKey()))
                    .as("%s: an unknown index is not a member", t).isFalse();
                for (var other : pairs) {
                    if (!other.table().equals(t)) {
                        assertThat(TwinKeyUpsert.isMember(ctx, idIdx.get(other.table()).name(), pair, pair.idKey()))
                            .as("%s: %s's id index is not a member", t, other.table()).isFalse();
                    }
                }
            }
        }
    }

    // -- the collision shapes ---------------------------------------------------------------------------------------------------

    /** The fixture premise: (B, id of A, k) trips only the id index and (A, id of B, k) only the name index, each named by the catalog. */
    @Test
    void theTwoCollisionShapesTripOnlyTheirOwnIndex() throws Exception {
        for (Subject s : SUBJECTS) {
            Cols c = twoCollections();
            var pair = TwinKeyUpsert.pair(s.table()).orElseThrow();
            String nameIdx = indexOver(s.table(), pair.nameKey()).name();
            String idIdx = indexOver(s.table(), pair.idKey()).name();
            Object key = s.freshKey();
            seed(s, c, key);

            assertThatThrownBy(() -> tenantScope.withTenant(TENANT, ctx -> s.insert(ctx, s.row(c.b(), c.idA(), key))))
                .as("%s: same id key, other name", s.table()).satisfies(e -> assertThat(constraintOf(e)).isEqualTo(idIdx));
            assertThatThrownBy(() -> tenantScope.withTenant(TENANT, ctx -> s.insert(ctx, s.row(c.a(), c.idB(), key))))
                .as("%s: same name key, other id", s.table()).satisfies(e -> assertThat(constraintOf(e)).isEqualTo(nameIdx));
            assertThat(committedCount(s, c.a(), key)).as("%s: X is the only row", s.table()).isEqualTo(1);
        }
    }

    // -- 2: one retry on the other member (NAME_KEY arbiter) ---------------------------------------------------------------------

    @Test
    void retriesOnceOnTheOtherMember_andTheTransactionStaysUsable() throws Exception {
        for (Subject s : SUBJECTS) {
            Cols c = twoCollections();
            Object key = s.freshKey(), fresh = s.freshKey();
            seed(s, c, key);
            AtomicInteger calls = new AtomicInteger();

            try (RetryLog log = new RetryLog()) {
                String result = tenantScope.withTenant(TENANT, ctx -> {
                    String r = TwinKeyUpsert.run(ctx, s.table(), Arbiter.NAME_KEY, inner -> {
                        if (calls.incrementAndGet() == 1) {
                            s.insert(inner, s.row(c.b(), c.idA(), key));   // trips only the id key: 23505, never returns
                            return "first";
                        }
                        s.upsertOnNameKey(inner, s.row(c.a(), c.idA(), fresh));
                        return "second";
                    });
                    // the same transaction runs on: the retry's row is in, the first attempt left nothing, X is untouched
                    assertThat(s.count(ctx, c.a(), fresh)).as("%s: the second call's row", s.table()).isEqualTo(1);
                    assertThat(s.count(ctx, c.b(), key)).as("%s: the first call's row was rolled back", s.table()).isZero();
                    assertThat(s.count(ctx, c.a(), key)).as("%s: X", s.table()).isEqualTo(1);
                    return r;
                });

                assertThat(result).as("%s: the result is the second call's", s.table()).isEqualTo("second");
                assertThat(calls).as("%s: exactly two calls", s.table()).hasValue(2);
                assertThat(log.retries()).as("%s: one retry event", s.table()).hasSize(1);
                assertThat(log.retries().get(0)).contains("table=" + s.table(), "arbiter=NAME_KEY",
                    "constraint=" + indexOver(s.table(), TwinKeyUpsert.pair(s.table()).orElseThrow().idKey()).name());
            }
            // committed: the retry's row is durable, the first attempt's is not
            assertThat(committedCount(s, c.a(), fresh)).as(s.table()).isEqualTo(1);
            assertThat(committedCount(s, c.b(), key)).as(s.table()).isZero();
        }
    }

    // -- 3: the arbiter's own conflict surfaces ---------------------------------------------------------------------------------

    @Test
    void aConflictOnTheArbiterSurfacesUnchanged() throws Exception {
        for (Subject s : SUBJECTS) {
            Cols c = twoCollections();
            var pair = TwinKeyUpsert.pair(s.table()).orElseThrow();
            String nameIdx = indexOver(s.table(), pair.nameKey()).name();
            Object key = s.freshKey(), fresh = s.freshKey();
            seed(s, c, key);
            AtomicInteger calls = new AtomicInteger();

            try (RetryLog log = new RetryLog()) {
                tenantScope.withTenant(TENANT, ctx -> {
                    assertThatThrownBy(() -> TwinKeyUpsert.run(ctx, s.table(), Arbiter.NAME_KEY, inner -> {
                        calls.incrementAndGet();
                        return s.insert(inner, s.row(c.a(), c.idB(), key));   // duplicates the name key only
                    })).as("%s: NAME_KEY arbiter, name key violated", s.table())
                        .isInstanceOf(DataAccessException.class)
                        .satisfies(e -> assertThat(constraintOf(e)).isEqualTo(nameIdx));
                    // the savepoint was rolled back before the rethrow: the transaction still works
                    assertThat(s.count(ctx, c.a(), key)).as("%s: X", s.table()).isEqualTo(1);
                    s.insert(ctx, s.row(c.a(), c.idA(), fresh));
                    return null;
                });
                assertThat(calls).as("%s: one call, no retry", s.table()).hasValue(1);
                assertThat(log.retries()).as("%s: no retry event", s.table()).isEmpty();
            }
            assertThat(committedCount(s, c.a(), fresh))
                .as("%s: the transaction committed after the surfaced error", s.table()).isEqualTo(1);
        }
    }

    // -- 4: a second violation surfaces -----------------------------------------------------------------------------------------

    @Test
    void aSecondViolationSurfaces() throws Exception {
        for (Subject s : SUBJECTS) {
            Cols c = twoCollections();
            var pair = TwinKeyUpsert.pair(s.table()).orElseThrow();
            String idIdx = indexOver(s.table(), pair.idKey()).name();
            Object key = s.freshKey(), fresh = s.freshKey();
            seed(s, c, key);
            AtomicInteger calls = new AtomicInteger();

            try (RetryLog log = new RetryLog()) {
                tenantScope.withTenant(TENANT, ctx -> {
                    assertThatThrownBy(() -> TwinKeyUpsert.run(ctx, s.table(), Arbiter.NAME_KEY, inner -> {
                        calls.incrementAndGet();
                        return s.insert(inner, s.row(c.b(), c.idA(), key));   // trips the id key on every call
                    })).as("%s: the id key on both calls", s.table())
                        .isInstanceOf(DataAccessException.class)
                        .satisfies(e -> assertThat(constraintOf(e)).isEqualTo(idIdx));
                    assertThat(s.count(ctx, c.b(), key)).as("%s: nothing of either attempt remains", s.table()).isZero();
                    s.insert(ctx, s.row(c.a(), c.idA(), fresh));   // the transaction is still usable
                    return null;
                });
                assertThat(calls).as("%s: exactly two calls", s.table()).hasValue(2);
                assertThat(log.retries()).as("%s: one retry event, not two", s.table()).hasSize(1);
            }
        }
    }

    // -- 5: the ID_KEY arbiter mirrors 2 and 3 ------------------------------------------------------------------------------------

    @Test
    void idKeyArbiter_retriesOnTheNameKey_andSurfacesItsOwnConflict() throws Exception {
        for (Subject s : SUBJECTS) {
            Cols c = twoCollections();
            var pair = TwinKeyUpsert.pair(s.table()).orElseThrow();
            String nameIdx = indexOver(s.table(), pair.nameKey()).name();
            String idIdx = indexOver(s.table(), pair.idKey()).name();
            Object key = s.freshKey(), fresh = s.freshKey(), other = s.freshKey();
            seed(s, c, key);

            // (a) the other member is the name key: (A, id of B, k) trips it alone, the retry's plain insert is valid
            AtomicInteger calls = new AtomicInteger();
            try (RetryLog log = new RetryLog()) {
                String result = tenantScope.withTenant(TENANT, ctx -> {
                    String r = TwinKeyUpsert.run(ctx, s.table(), Arbiter.ID_KEY, inner -> {
                        if (calls.incrementAndGet() == 1) {
                            s.insert(inner, s.row(c.a(), c.idB(), key));
                            return "first";
                        }
                        s.insert(inner, s.row(c.a(), c.idA(), fresh));
                        return "second";
                    });
                    assertThat(s.count(ctx, c.a(), fresh)).as("%s: the second call's row", s.table()).isEqualTo(1);
                    assertThat(s.count(ctx, c.a(), key)).as("%s: X, and no second row at its key", s.table()).isEqualTo(1);
                    return r;
                });
                assertThat(result).as(s.table()).isEqualTo("second");
                assertThat(calls).as("%s: two calls", s.table()).hasValue(2);
                assertThat(log.retries()).as("%s: one retry event", s.table()).hasSize(1);
                assertThat(log.retries().get(0)).contains("table=" + s.table(), "arbiter=ID_KEY", "constraint=" + nameIdx);
            }

            // (b) the arbiter's own key: (B, id of A, k) trips the id key, which this arbiter names, so it surfaces after one call
            AtomicInteger own = new AtomicInteger();
            try (RetryLog log = new RetryLog()) {
                tenantScope.withTenant(TENANT, ctx -> {
                    assertThatThrownBy(() -> TwinKeyUpsert.run(ctx, s.table(), Arbiter.ID_KEY, inner -> {
                        own.incrementAndGet();
                        return s.insert(inner, s.row(c.b(), c.idA(), key));
                    })).as("%s: ID_KEY arbiter, id key violated", s.table())
                        .isInstanceOf(DataAccessException.class)
                        .satisfies(e -> assertThat(constraintOf(e)).isEqualTo(idIdx));
                    s.insert(ctx, s.row(c.a(), c.idA(), other));   // still usable
                    return null;
                });
                assertThat(own).as("%s: one call", s.table()).hasValue(1);
                assertThat(log.retries()).as("%s: no retry event", s.table()).isEmpty();
            }
        }
    }

    // -- the rest of "anything else rethrows" -------------------------------------------------------------------------------------

    /** A 23514 (name set, id unset) is not a unique violation: one call, surfaced, and the savepoint left the transaction usable. */
    @Test
    void anErrorThatIsNotAUniqueViolationSurfacesAfterOneCall() throws Exception {
        for (Subject s : SUBJECTS) {
            Cols c = twoCollections();
            Object key = s.freshKey(), fresh = s.freshKey();
            AtomicInteger calls = new AtomicInteger();
            try (RetryLog log = new RetryLog()) {
                tenantScope.withTenant(TENANT, ctx -> {
                    assertThatThrownBy(() -> TwinKeyUpsert.run(ctx, s.table(), Arbiter.NAME_KEY, inner -> {
                        calls.incrementAndGet();
                        return s.insert(inner, s.row(c.a(), null, key));
                    })).as("%s: name without id", s.table())
                        .isInstanceOfSatisfying(DataAccessException.class, e -> {
                            assertThat(sqlState(e)).isEqualTo("23514");
                            assertThat(SqlConstraints.isUniqueViolation(e)).isFalse();
                        });
                    s.insert(ctx, s.row(c.a(), c.idA(), fresh));
                    return null;
                });
                assertThat(calls).as("%s: one call", s.table()).hasValue(1);
                assertThat(log.retries()).as("%s: no retry event", s.table()).isEmpty();
            }
        }
    }

    @Test
    void aTableWithNoPairIsRefusedBeforeTheStatementRuns() {
        AtomicInteger calls = new AtomicInteger();
        assertThatThrownBy(() -> TwinKeyUpsert.run(DSL.using(SQLDialect.POSTGRES), "catalog_collections", Arbiter.NAME_KEY,
                ctx -> calls.incrementAndGet()))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("catalog_collections");
        assertThat(calls).hasValue(0);
    }

    // -- 6: two writers, one new key ---------------------------------------------------------------------------------------------

    private static final int UNWRAPPED_CAP = 300;
    private static final int WRAPPED_ITERATIONS = 150;

    private static void await(CyclicBarrier barrier) {
        try {
            barrier.await(20, TimeUnit.SECONDS);
        } catch (Exception e) {
            throw new IllegalStateException("the two writers did not meet at the barrier", e);
        }
    }

    /**
     * Two pooled connections, each in its own tenant transaction, run the same
     * {@code INSERT ... ON CONFLICT (name key) DO UPDATE} of one new key, released together by a barrier just before the statement.
     * Returns the throwable of each writer (null when it committed).
     */
    private List<Throwable> raceOnce(ExecutorService pool, Subject s, Cols c, boolean viaHelper) throws Exception {
        Object key = s.freshKey();
        var barrier = new CyclicBarrier(2);
        Callable<Throwable> writer = () -> {
            var first = new AtomicBoolean(true);
            Function<DSLContext, Integer> statement = inner -> {
                if (first.getAndSet(false)) {
                    await(barrier);   // only the first call: a retry must not wait for a peer that is already done
                }
                return s.upsertOnNameKey(inner, s.row(c.a(), c.idA(), key));
            };
            try {
                tenantScope.withTenant(TENANT, ctx -> viaHelper
                    ? TwinKeyUpsert.run(ctx, s.table(), Arbiter.NAME_KEY, statement)
                    : statement.apply(ctx));
                return null;
            } catch (Throwable t) {
                return t;
            }
        };
        Future<Throwable> w1 = pool.submit(writer), w2 = pool.submit(writer);
        var out = new ArrayList<Throwable>();
        out.add(w1.get(60, TimeUnit.SECONDS));
        out.add(w2.get(60, TimeUnit.SECONDS));
        // a writer that committed left the key stored once, whoever lost
        assertThat(committedCount(s, c.a(), key))
            .as("%s: the raced key is stored once when a writer committed (%s)", s.table(), out)
            .isEqualTo(out.contains(null) ? 1 : 0);
        return out;
    }

    /**
     * Per pair: first the race with no helper, until a 23505 on the id index appears (non-vacuity: the race this helper exists for does
     * happen on this schema and this box), then the same race through {@link TwinKeyUpsert#run} with fresh keys, which must not raise in any
     * iteration. A different failure in the unwrapped phase fails the test rather than being skipped.
     */
    @Test
    @Timeout(value = 280, unit = TimeUnit.SECONDS, threadMode = Timeout.ThreadMode.SEPARATE_THREAD)
    void twoWritersOfTheSameNewKey_theHelperAbsorbsTheIdKeyRace() throws Exception {
        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            for (Subject s : SUBJECTS) {
                Cols c = twoCollections();
                var pair = TwinKeyUpsert.pair(s.table()).orElseThrow();
                String idIdx = indexOver(s.table(), pair.idKey()).name();

                int reproducedAt = -1;
                for (int i = 0; i < UNWRAPPED_CAP && reproducedAt < 0; i++) {
                    for (Throwable t : raceOnce(pool, s, c, false)) {
                        if (t == null) {
                            continue;
                        }
                        assertThat(SqlConstraints.isUniqueViolation(t) && idIdx.equals(SqlConstraints.violated(t)))
                            .as("%s: iteration %d, the only failure the race may show is the id index %s, got %s", s.table(), i, idIdx, t)
                            .isTrue();
                        reproducedAt = i;
                    }
                }
                assertThat(reproducedAt).as("%s: non-vacuity: the unwrapped race raised a 23505 on %s within %d iterations",
                    s.table(), idIdx, UNWRAPPED_CAP).isGreaterThanOrEqualTo(0);

                try (RetryLog log = new RetryLog()) {
                    for (int i = 0; i < WRAPPED_ITERATIONS; i++) {
                        assertThat(raceOnce(pool, s, c, true)).as("%s: wrapped iteration %d, neither writer raised "
                            + "(the unwrapped race reproduced at iteration %d)", s.table(), i, reproducedAt).containsOnlyNulls();
                    }
                    LOG.info("event=twin_key_race_measured table={} unwrapped_reproduced_at={} wrapped_iterations={} wrapped_retries={}",
                        s.table(), reproducedAt, WRAPPED_ITERATIONS, log.retries().size());
                }
            }
        } finally {
            pool.shutdownNow();
        }
    }
}
