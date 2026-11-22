// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.PgSession.PciSettings;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.vectors.PciCanary.Result;
import dev.nexus.service.vectors.PciCanary.Target;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.Timeout;
import org.slf4j.LoggerFactory;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.TimeUnit;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-230 P2 (nexus-4lnn1.1.10): the anchor detects a rebuild the engine did not do. {@code REINDEX INDEX
 * CONCURRENTLY} run by hand keeps the index's name, carries its COMMENT across, and changes its
 * {@code pg_class.relfilenode} (and its oid: it is a new index swapped in); the anchor records the relfilenode it was written against (v2), so a pass that reads a
 * different one re-anchors the index as rebuilt. A v1 anchor (no relfilenode) stays valid and is left alone.
 * Against the real partitioned layout, as {@link PciReconcilerIntegrationTest}.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@Timeout(value = 300, unit = TimeUnit.SECONDS, threadMode = Timeout.ThreadMode.SEPARATE_THREAD)
class PciAnchorRebuildIntegrationTest {

    static final String ADMIN_ROLE = PgContainerHelper.TEMPLATE_OWNER;
    static final String ADMIN_PASS = PgContainerHelper.TEMPLATE_OWNER_PASSWORD;
    static final String M384 = "minilm-l6-v2-384";
    static final String NONCE = "e5f6a7b8";
    static final int B = 200;
    static final List<Class<?>> LOGGERS = List.of(PciReconciler.class, PciBuilderSession.class, PciLeafIndexReads.class);

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    HikariDataSource adminDs;
    PciCatalog catalog;
    final List<String> tenants = new ArrayList<>();
    ListAppender<ILoggingEvent> appender;
    int generation;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.startMigratedByOwner();
        var adminCfg = new HikariConfig();
        adminCfg.setJdbcUrl(pg.getJdbcUrl());
        adminCfg.setUsername(ADMIN_ROLE);
        adminCfg.setPassword(ADMIN_PASS);
        adminCfg.setMaximumPoolSize(2);
        adminDs = new HikariDataSource(adminCfg);
        var svcCfg = new HikariConfig();
        svcCfg.setJdbcUrl(pg.getJdbcUrl());
        svcCfg.setUsername(PgContainerHelper.SVC_USERNAME);
        svcCfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        svcCfg.setMaximumPoolSize(4);
        svcCfg.setAutoCommit(true);
        svcDs = new HikariDataSource(svcCfg);
        catalog = new PciCatalog(svcDs);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (adminDs != null) adminDs.close();
        if (pg != null) pg.stop();
    }

    @BeforeEach
    void captureLogs() {
        appender = new ListAppender<>();
        appender.start();
        for (Class<?> c : LOGGERS) {
            var logger = (ch.qos.logback.classic.Logger) LoggerFactory.getLogger(c);
            logger.setLevel(Level.DEBUG);
            logger.addAppender(appender);
        }
    }

    @AfterEach
    @SuppressWarnings("deprecation")
    void removeTenants() throws Exception {
        for (Class<?> c : LOGGERS) {
            ((ch.qos.logback.classic.Logger) LoggerFactory.getLogger(c)).detachAppender(appender);
        }
        try (Connection owner = adminDs.getConnection()) {
            owner.setAutoCommit(true);
            DSLContext ctx = DSL.using(owner, SQLDialect.POSTGRES);
            for (String tenant : tenants) {
                dev.nexus.service.jooq.nexus.Routines.dropTenantPartitions(ctx.configuration(), tenant);
            }
        }
        tenants.clear();
    }

    // -- fixtures ----------------------------------------------------------------------------------------

    List<String> logs() {
        return appender.list.stream().map(ILoggingEvent::getFormattedMessage).toList();
    }

    long logged(String needle) {
        return logs().stream().filter(l -> l.contains(needle)).count();
    }

    /** REINDEX off: the rebuild is detected by the anchor read every pass, whatever the REINDEX switch says. */
    static PciSettings settings() {
        return new PciSettings(true, B, 600, 16, null, false);
    }

    static final class MutableClock extends Clock {
        /** Microseconds, as PostgreSQL keeps them: an anchor truncates its built_at, and Linux clocks have nanoseconds. */
        private volatile Instant now = Instant.now().truncatedTo(java.time.temporal.ChronoUnit.MICROS);

        void advance(Duration d) {
            now = now.plus(d);
        }

        @Override public java.time.ZoneId getZone() {
            return ZoneOffset.UTC;
        }

        @Override public Clock withZone(java.time.ZoneId zone) {
            return this;
        }

        @Override public Instant instant() {
            return now;
        }
    }

    PciBuilderSession builderSession(PciSettings s) {
        return new PciBuilderSession(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS, NONCE, s);
    }

    PciReconciler reconciler(PciSettings s, Clock clock) {
        return new PciReconciler(catalog, builderSession(s), PciIndexSweep.create(svcDs, s), s, clock,
            PciReconciler.SET_LOCAL_TENANT, PciReconciler.COUNT_TIMEOUT, Duration.ofSeconds(60));
    }

    /**
     * The canary: every run ends in {@link #state} with {@link #queries} queries; a red run's queries are {@link #sample},
     * and the ones of them that read red are {@link #reds} (all of them when null).
     */
    static final class ScriptedCanary implements PciCanarySchedule.Runner {
        volatile PciCanary.State state = PciCanary.State.GREEN;
        volatile int queries = 10;
        volatile List<float[]> sample = List.of(new float[] {1f, 2f, 3f});
        volatile List<float[]> reds;

        private Result result(Target target, String trigger) {
            boolean red = state == PciCanary.State.RED;
            return new Result(target, trigger, state, null, queries, red ? 0.9 : 1.0, red ? 0.95 : 1.0, red ? 1 : 0,
                Instant.now(), red ? sample : null, null, red ? (reds != null ? reds : sample) : null);
        }

        @Override public Result run(Target target, String trigger) {
            return result(target, trigger);
        }

        @Override public Result remeasure(Target target, String trigger, List<float[]> sameQueries) {
            return result(target, trigger);
        }
    }

    /** REINDEX on, with the scripted canary: the leaf size rule and the REINDEX triggers run. */
    PciReconciler reindexingReconciler(Clock clock, ScriptedCanary canary) {
        PciSettings s = new PciSettings(true, B, 600, 16);
        return new PciReconciler(catalog::read, builderSession(s), PciIndexSweep.create(svcDs, s), s, clock,
            PciReconciler.SET_LOCAL_TENANT, PciReconciler.COUNT_TIMEOUT, Duration.ofSeconds(60),
            java.util.random.RandomGenerator.getDefault(), tenantsSeen -> 0,
            new PciCanarySchedule(canary, clock, System::nanoTime));
    }

    @SuppressWarnings("deprecation")
    String newTenant(String tag) throws Exception {
        String tenant = "pcianc-" + tag;
        tenants.add(tenant);
        try (Connection owner = adminDs.getConnection()) {
            owner.setAutoCommit(true);
            dev.nexus.service.jooq.nexus.Routines.createTenantPartitions(
                DSL.using(owner, SQLDialect.POSTGRES).configuration(), "nexus.chunks", tenant, true, true);
        }
        return tenant;
    }

    static String name(String tag) {
        return "knowledge__pcianc-" + tag + "__minilm-l6-v2-384__v1";
    }

    <T> T asSuperuser(java.util.function.Function<DSLContext, T> work) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            return work.apply(DSL.using(su, SQLDialect.POSTGRES));
        }
    }

    static Vector vector(int i) {
        float[] v = new float[384];
        v[i % 384] = 1f;
        v[(i * 7 + 1) % 384] += 0.5f;
        return Vector.of(v);
    }

    /** Register {@code collection} for {@code tenant} and give it {@code rows} rows. */
    void collection(String tenant, String collection, int rows) throws Exception {
        int gen = ++generation;
        asSuperuser(su -> {
            PgContainerHelper.insertCollection(su, tenant, collection, M384);
            for (int from = 0; from < rows; from += 250) {
                var step = su.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH,
                    CHUNKS.EMBEDDING_MODEL, CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_384, CHUNKS.METADATA);
                for (int i = from; i < Math.min(rows, from + 250); i++) {
                    step = step.values(tenant, collection,
                        Chash.fromHex(Chash.ofText(tenant + "/" + collection + "/" + gen + "/" + i).toHex()).toBytes(),
                        M384, "row " + i, vector(i), JSONB.jsonb("{}"));
                }
                step.onConflictDoNothing().execute();
            }
            return null;
        });
    }

    /** Delete the rows whose text is {@code row <from>} to {@code row <to - 1>}. */
    void deleteRows(String tenant, String collection, int from, int to) throws Exception {
        List<String> texts = new ArrayList<>();
        for (int i = from; i < to; i++) {
            texts.add("row " + i);
        }
        asSuperuser(su -> su.deleteFrom(CHUNKS).where(CHUNKS.TENANT_ID.eq(tenant))
            .and(CHUNKS.COLLECTION.eq(collection)).and(CHUNKS.CHUNK_TEXT.in(texts)).execute());
    }

    PciCatalog.Leaf leaf(String tenant) {
        List<PciCatalog.Leaf> hits = catalog.read().leaves().stream()
            .filter(l -> M384.equals(l.model()) && tenant.equals(l.tenant())).toList();
        assertThat(hits).as("exactly one leaf for (%s, %s)", M384, tenant).hasSize(1);
        return hits.get(0);
    }

    PciCatalog.Index indexOf(String tenant, String collection) {
        String index = PciCatalog.indexName(M384, tenant, collection);
        return leaf(tenant).indexes().stream().filter(i -> index.equals(i.name())).findFirst().orElseThrow();
    }

    /** What the catalog says of an index, read independently of the code under test. */
    record Facts(String oid, String relfilenode, String comment) { }

    Facts factsOf(String schema, String name) throws Exception {
        return asSuperuser(su -> {
            var row = su.select(DSL.field(DSL.name("c", "oid")).cast(String.class),
                    DSL.field(DSL.name("c", "relfilenode")).cast(String.class),
                    DSL.function(DSL.name("pg_catalog", "obj_description"), org.jooq.impl.SQLDataType.CLOB,
                        DSL.field(DSL.name("c", "oid")), DSL.inline("pg_class")))
                .from(DSL.table(DSL.name("pg_catalog", "pg_class")).as("c"))
                .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n"))
                .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
                .where(DSL.field(DSL.name("n", "nspname"), String.class).eq(schema))
                .and(DSL.field(DSL.name("c", "relname"), String.class).eq(name))
                .fetchOne();
            return new Facts(row.value1(), row.value2(), row.value3());
        });
    }

    void handReindex(String schema, String index) throws Exception {
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.reindexIndexConcurrently(su, schema, index);
        }
    }

    long countRows(String tenant, String collection) throws Exception {
        return asSuperuser(su -> su.selectCount().from(CHUNKS).where(CHUNKS.TENANT_ID.eq(tenant))
            .and(CHUNKS.COLLECTION.eq(collection)).fetchOne(0, Long.class));
    }

    // -- the substrate fact the design rests on ----------------------------------------------------------------

    @Test
    void substrate_reindexConcurrently_keepsTheName_changesOidAndRelfilenode_andCarriesTheComment() throws Exception {
        String tenant = newTenant("substrate");
        collection(tenant, name("c"), 300);
        reconciler(settings(), Clock.systemUTC()).reconcileOnce();
        PciCatalog.Leaf leaf = leaf(tenant);
        String index = indexOf(tenant, name("c")).name();
        Facts before = factsOf(leaf.schema(), index);
        assertThat(before.comment()).isNotNull();

        handReindex(leaf.schema(), index);

        Facts after = factsOf(leaf.schema(), index);
        // The name is what the engine keys on (factsOf found the index by it). The oid is NOT kept: REINDEX CONCURRENTLY
        // builds a new index and swaps the names, so the new one has a new oid as well as new storage.
        assertThat(after.oid()).as("a new index under the same name").isNotEqualTo(before.oid());
        assertThat(after.relfilenode()).as("the rebuild gives it new storage").isNotEqualTo(before.relfilenode());
        assertThat(after.comment()).as("the COMMENT is carried across the swap").isEqualTo(before.comment());
    }

    // -- the engine's anchors carry the relfilenode ------------------------------------------------------------

    @Test
    void aBuild_anchorsWithTheRelfilenodeItWasWrittenAgainst_underTheSameVersion() throws Exception {
        String tenant = newTenant("rf-built");
        collection(tenant, name("c"), 300);

        reconciler(settings(), Clock.systemUTC()).reconcileOnce();

        PciCatalog.Leaf leaf = leaf(tenant);
        Facts facts = factsOf(leaf.schema(), indexOf(tenant, name("c")).name());
        assertThat(facts.comment()).startsWith("{\"nexus_pci\":1,").contains("\"relfilenode\":" + facts.relfilenode());
        PciCatalog.Anchor anchor = indexOf(tenant, name("c")).anchor().orElseThrow();
        assertThat(anchor.origin()).isEqualTo("built");
        assertThat(anchor.relfilenode()).isEqualTo(Long.parseLong(facts.relfilenode()));
    }

    // -- a rebuild the engine did not do -----------------------------------------------------------------------

    @Test
    void aHandReindex_isDetectedOnTheNextPass_andReanchored_onceAndOnlyOnce() throws Exception {
        String tenant = newTenant("detect");
        collection(tenant, name("c"), 300);
        MutableClock clock = new MutableClock();
        PciReconciler r = reconciler(settings(), clock);
        r.reconcileOnce();
        PciCatalog.Leaf leaf = leaf(tenant);
        String index = indexOf(tenant, name("c")).name();
        PciCatalog.Anchor built = indexOf(tenant, name("c")).anchor().orElseThrow();
        Facts first = factsOf(leaf.schema(), index);

        // An index whose relfilenode has not moved is left alone, pass after pass.
        clock.advance(Duration.ofHours(1));
        r.reconcileOnce();
        assertThat(factsOf(leaf.schema(), index).comment()).isEqualTo(first.comment());
        assertThat(logged("event=pci_anchor_rebuilt_detected")).isZero();

        deleteRows(tenant, name("c"), 0, 100);
        handReindex(leaf.schema(), index);
        Facts rebuilt = factsOf(leaf.schema(), index);
        assertThat(rebuilt.comment()).as("the old anchor is still there").isEqualTo(first.comment());
        clock.advance(Duration.ofHours(1));
        r.reconcileOnce();

        PciCatalog.Anchor re = indexOf(tenant, name("c")).anchor().orElseThrow();
        assertThat(re.relfilenode()).isEqualTo(Long.parseLong(rebuilt.relfilenode()))
            .isNotEqualTo(built.relfilenode());
        assertThat(re.builtAt()).as("built_at is now, the conservative side of the floors").isEqualTo(clock.instant());
        assertThat(re.origin()).isEqualTo("built");
        assertThat(re.rows()).isEqualTo(200).isEqualTo(countRows(tenant, name("c")));
        assertThat(re.bytes()).isPositive();
        assertThat(logged("event=pci_anchor_rebuilt_detected")).isEqualTo(1);
        assertThat(logs()).anyMatch(l -> l.contains("event=pci_anchor_rebuilt_detected")
            && l.contains("index=" + index) && l.contains("old_relfilenode=" + first.relfilenode())
            && l.contains("new_relfilenode=" + rebuilt.relfilenode()));
        assertThat(r.reading(leaf, index).anchor()).as("the engine's reading is the written anchor").isEqualTo(re);

        String text = factsOf(leaf.schema(), index).comment();
        clock.advance(Duration.ofHours(1));
        r.reconcileOnce();
        r.reconcileOnce();
        assertThat(factsOf(leaf.schema(), index).comment()).as("now unchanged").isEqualTo(text);
        assertThat(logged("event=pci_anchor_rebuilt_detected")).as("one line, not one per pass").isEqualTo(1);
    }

    @Test
    void anAnchorWithoutARelfilenode_isStampedOnce_withItsOwnFigures_andALaterHandReindexIsThenDetected()
            throws Exception {
        String tenant = newTenant("stamp");
        collection(tenant, name("c"), 300);
        MutableClock clock = new MutableClock();
        PciReconciler r = reconciler(settings(), clock);
        r.reconcileOnce();
        PciCatalog.Leaf leaf = leaf(tenant);
        String index = indexOf(tenant, name("c")).name();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.commentIndexAnchorWithoutRelfilenode(su, leaf.schema(), index);
        }
        String before = factsOf(leaf.schema(), index).comment();
        assertThat(before).doesNotContain("relfilenode");
        PciCatalog.Anchor figures = PciCatalog.parseAnchor(before).orElseThrow();

        clock.advance(Duration.ofHours(1));
        r.reconcileOnce();

        Facts stamped = factsOf(leaf.schema(), index);
        assertThat(stamped.comment()).as("the same text, byte for byte, plus the field")
            .isEqualTo(before.replace("}", ",\"relfilenode\":" + stamped.relfilenode() + "}"));
        assertThat(PciCatalog.parseAnchor(stamped.comment()).orElseThrow().sameFigures(figures)).isTrue();
        assertThat(logged("event=pci_anchor_relfilenode_stamped")).isEqualTo(1);
        assertThat(logged("event=pci_anchor_rebuilt_detected")).as("a stamp is no rebuild").isZero();
        assertThat(logged("event=pci_anchor_adopted")).isZero();
        assertThat(r.reading(leaf, index).anchor()).as("the reading carries it").isEqualTo(
            PciCatalog.parseAnchor(stamped.comment()).orElseThrow());

        clock.advance(Duration.ofHours(1));
        r.reconcileOnce();
        assertThat(factsOf(leaf.schema(), index).comment()).as("stamped once").isEqualTo(stamped.comment());
        assertThat(logged("event=pci_anchor_relfilenode_stamped")).isEqualTo(1);

        handReindex(leaf.schema(), index);
        clock.advance(Duration.ofHours(1));
        r.reconcileOnce();

        assertThat(logged("event=pci_anchor_rebuilt_detected")).as("now a hand rebuild is seen").isEqualTo(1);
        assertThat(PciCatalog.parseAnchor(factsOf(leaf.schema(), index).comment()).orElseThrow().builtAt())
            .isEqualTo(clock.instant());
    }

    @Test
    void aRebuildBetweenTheReadAndTheWrite_isNotStampedOverOldFigures() throws Exception {
        String tenant = newTenant("veto");
        collection(tenant, name("c"), 300);
        reconciler(settings(), Clock.systemUTC()).reconcileOnce();
        PciCatalog.Leaf leaf = leaf(tenant);
        PciCatalog.Index index = indexOf(tenant, name("c"));
        Facts read = factsOf(leaf.schema(), index.name());
        handReindex(leaf.schema(), index.name());   // lands after the read
        String text = factsOf(leaf.schema(), index.name()).comment();
        var anchor = new PciCatalog.Anchor(Instant.now(), 300, 1, PciCatalog.ORIGIN_BUILT);

        try (PciBuilderSession.Pass pass = builderSession(settings()).open()) {
            var vetoed = pass.write(leaf, index, anchor, Long.parseLong(read.relfilenode()));
            assertThat(vetoed.outcome()).isEqualTo(PciBuilderSession.DdlOutcome.STALE);
            assertThat(factsOf(leaf.schema(), index.name()).comment()).as("nothing was sent").isEqualTo(text);
            var current = Long.parseLong(factsOf(leaf.schema(), index.name()).relfilenode());
            assertThat(pass.write(leaf, index, anchor, current).outcome()).isEqualTo(PciBuilderSession.DdlOutcome.DONE);
        }
        assertThat(logged("event=pci_anchor_skipped reason=rebuilt_since_read")).isEqualTo(1);
    }

    @Test
    void aCommentOnAnIndexThatIsGone_isStale_andSendsNothing() throws Exception {
        String tenant = newTenant("gone");
        collection(tenant, name("c"), 300);
        reconciler(settings(), Clock.systemUTC()).reconcileOnce();
        PciCatalog.Leaf leaf = leaf(tenant);
        PciCatalog.Index index = indexOf(tenant, name("c"));

        try (PciBuilderSession.Pass pass = builderSession(settings()).open()) {
            assertThat(pass.drop(leaf, index)).isEqualTo(PciBuilderSession.DdlOutcome.DONE);
            var gone = pass.write(leaf, index, new PciCatalog.Anchor(Instant.now(), 1, 1, PciCatalog.ORIGIN_BUILT));
            assertThat(gone.outcome()).isEqualTo(PciBuilderSession.DdlOutcome.STALE);
            assertThat(pass.comment(leaf, index, new PciCatalog.Anchor(Instant.now(), 1, 1,
                PciCatalog.ORIGIN_BUILT))).isEqualTo(PciBuilderSession.DdlOutcome.STALE);
        }
        assertThat(logged("event=pci_anchor_skipped reason=gone index=" + index.name())).isEqualTo(2);
        assertThat(logged("event=pci_ddl_failed op=comment")).as("no COMMENT statement was sent").isZero();
    }

    // -- what the engine does with the new anchor --------------------------------------------------------------

    @Test
    void aHandReindexAfterABulkDelete_isNoBulkDelete_noDeletesRebuild_andNoVacuum() throws Exception {
        String tenant = newTenant("bulk");
        collection(tenant, name("c"), 300);
        MutableClock clock = new MutableClock();
        ScriptedCanary canary = new ScriptedCanary();
        PciReconciler r = reindexingReconciler(clock, canary);
        r.reconcileOnce();
        r.reconcileOnce();   // the first look records the 300 rows as the previous count
        PciCatalog.Leaf leaf = leaf(tenant);
        String index = indexOf(tenant, name("c")).name();

        deleteRows(tenant, name("c"), 0, 120);   // 40%
        handReindex(leaf.schema(), index);
        for (int i = 0; i < 3; i++) {
            clock.advance(Duration.ofMinutes(10));
            r.reconcileOnce();
        }

        assertThat(logged("event=pci_anchor_rebuilt_detected")).isEqualTo(1);
        assertThat(logged("trigger=bulk_delete")).as("the fall since the old build is not a bulk delete").isZero();
        assertThat(logged("trigger=deletes_since_build")).as("deletes count from the new anchor").isZero();
        assertThat(logged("event=pci_leaf_vacuumed")).isZero();
        assertThat(r.reading(leaf, index).anchor().rows()).isEqualTo(180);
    }

    @Test
    void aHandReindexOfAnIndexWithARedCanary_holdsTheRedCanaryDayFromTheDetection() throws Exception {
        String tenant = newTenant("redfloor");
        collection(tenant, name("c"), 300);
        MutableClock clock = new MutableClock();
        ScriptedCanary canary = new ScriptedCanary();
        canary.state = PciCanary.State.RED;
        PciReconciler r = reindexingReconciler(clock, canary);
        r.reconcileOnce();   // builds; the canary is red on ten queries after the build
        PciCatalog.Leaf leaf = leaf(tenant);
        String index = indexOf(tenant, name("c")).name();

        clock.advance(Duration.ofHours(30));   // the build's own day is over: a red would REINDEX now
        handReindex(leaf.schema(), index);
        r.reconcileOnce();   // detects, re-anchors at built_at = now
        assertThat(logged("event=pci_anchor_rebuilt_detected")).isEqualTo(1);
        assertThat(r.reindexes()).isZero();

        clock.advance(Duration.ofHours(23));   // 53 h after the build, 23 h after the detection
        r.reconcileOnce();
        assertThat(r.reindexes()).as("23 h after the detection").isZero();

        clock.advance(Duration.ofHours(2));
        r.reconcileOnce();
        assertThat(r.reindexes()).as("25 h after the detection").isEqualTo(1);
        assertThat(logged("trigger=red_canary")).isGreaterThanOrEqualTo(1);
    }

    // -- a leaf's own HNSW index -------------------------------------------------------------------------------

    @Test
    void aLeafsOwnIndex_isStampedOnce_thenAHandReindexIsDetectedAndReanchored() throws Exception {
        String tenant = newTenant("leaf");
        collection(tenant, name("keep"), 250);
        MutableClock clock = new MutableClock();
        PciReconciler r = reindexingReconciler(clock, new ScriptedCanary());
        r.reconcileOnce();   // builds the pci_ index: the install reference the leaf rule needs
        PciCatalog.Leaf leaf = leaf(tenant);
        PciCatalog.LeafIndex own = leaf.hnswIndex(384).orElseThrow();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.commentIndexAnchorWithoutRelfilenode(su, leaf.schema(), own.name());
        }
        String before = factsOf(leaf.schema(), own.name()).comment();

        clock.advance(Duration.ofHours(1));
        r.reconcileOnce();

        Facts stamped = factsOf(leaf.schema(), own.name());
        assertThat(stamped.comment()).as("stamped, nothing else changed")
            .isEqualTo(before.replace("}", ",\"relfilenode\":" + stamped.relfilenode() + "}"));
        assertThat(logged("event=pci_anchor_relfilenode_stamped index=" + own.name())).isEqualTo(1);
        clock.advance(Duration.ofHours(1));
        r.reconcileOnce();
        assertThat(factsOf(leaf.schema(), own.name()).comment()).as("unmoved: left alone").isEqualTo(stamped.comment());
        assertThat(logged("event=pci_anchor_rebuilt_detected")).isZero();

        handReindex(leaf.schema(), own.name());
        Facts rebuilt = factsOf(leaf.schema(), own.name());
        assertThat(rebuilt.relfilenode()).isNotEqualTo(stamped.relfilenode());
        clock.advance(Duration.ofHours(1));
        r.reconcileOnce();

        PciCatalog.Anchor re = PciCatalog.parseAnchor(factsOf(leaf.schema(), own.name()).comment()).orElseThrow();
        assertThat(re.relfilenode()).isEqualTo(Long.parseLong(rebuilt.relfilenode()));
        assertThat(re.builtAt()).isEqualTo(clock.instant());
        assertThat(re.origin()).isEqualTo("built");
        assertThat(logs()).anyMatch(l -> l.contains("event=pci_anchor_rebuilt_detected")
            && l.contains("index=" + own.name()) && l.contains("old_relfilenode=" + stamped.relfilenode())
            && l.contains("new_relfilenode=" + rebuilt.relfilenode()));
    }

    // -- red_unfixed: a red a REINDEX did not cure stops the daily loop ----------------------------------------

    /** An index with a red canary on ten queries whose red-canary REINDEX re-measured still red: one REINDEX done. */
    private PciReconciler stillRedAfterItsReindex(String tenant, String coll, MutableClock clock,
                                                   ScriptedCanary canary) throws Exception {
        collection(tenant, coll, 300);
        canary.state = PciCanary.State.RED;
        PciReconciler r = reindexingReconciler(clock, canary);
        r.reconcileOnce();   // builds; the canary is red after the build
        clock.advance(Duration.ofHours(25));
        r.reconcileOnce();   // the red-canary REINDEX; the re-measure is still red
        assertThat(r.reindexes()).isEqualTo(1);
        assertThat(logged("trigger=red_canary")).isGreaterThanOrEqualTo(1);
        return r;
    }

    @Test
    void aRedThatAReindexDidNotCure_isRecordedInTheAnchor_andIsNotRebuiltOnTheNextDays() throws Exception {
        String tenant = newTenant("unfixed");
        String coll = name("c");
        MutableClock clock = new MutableClock();
        ScriptedCanary canary = new ScriptedCanary();
        PciReconciler r = stillRedAfterItsReindex(tenant, coll, clock, canary);

        PciCatalog.Anchor anchor = indexOf(tenant, coll).anchor().orElseThrow();
        Facts facts = factsOf(leaf(tenant).schema(), indexOf(tenant, coll).name());
        assertThat(anchor.redUnfixed()).isNotNull();
        assertThat(anchor.redUnfixed().relfilenode()).isEqualTo(Long.parseLong(facts.relfilenode()))
            .isEqualTo(anchor.relfilenode());
        assertThat(anchor.redUnfixed().queries()).isEqualTo(PciQueryRing.queryIds(canary.sample));
        assertThat(anchor.redUnfixed().rows()).as("the count at the red, after the REINDEX").isEqualTo(300);
        assertThat(anchor.redUnfixed().at()).isEqualTo(clock.instant());
        assertThat(facts.comment()).contains("\"red_unfixed\":{");
        assertThat(logged("event=pci_red_unfixed_recorded")).isEqualTo(1);

        for (int day = 0; day < 3; day++) {
            clock.advance(Duration.ofHours(25));
            r.reconcileOnce();
            r.reconcileOnce();
        }

        assertThat(r.reindexes()).as("a persistent red is not rebuilt again on the next days").isEqualTo(1);
        assertThat(indexOf(tenant, coll).anchor().orElseThrow().redUnfixed()).as("still recorded").isNotNull();
    }

    @Test
    void aNewRedQuery_liftsIt_soANewCauseIsRebuiltAgain() throws Exception {
        String tenant = newTenant("unfixed-ring");
        String coll = name("c");
        MutableClock clock = new MutableClock();
        ScriptedCanary canary = new ScriptedCanary();
        PciReconciler r = stillRedAfterItsReindex(tenant, coll, clock, canary);
        List<String> oldRed = indexOf(tenant, coll).anchor().orElseThrow().redUnfixed().queries();

        canary.sample = List.of(new float[] {9f, 8f, 7f});
        clock.advance(Duration.ofHours(25));
        r.reconcileOnce();   // judged on the old red; the daily run then reads the new queries
        assertThat(r.reindexes()).isEqualTo(1);
        r.reconcileOnce();

        assertThat(r.reindexes()).as("a new ring can mean a new cause").isEqualTo(2);
        assertThat(indexOf(tenant, coll).anchor().orElseThrow().redUnfixed().queries())
            .as("recorded again, for the new red queries").isEqualTo(PciQueryRing.queryIds(canary.sample))
            .isNotEqualTo(oldRed);
    }

    @Test
    void aHandReindex_liftsIt_andTheRedDayCountsFromTheDetection() throws Exception {
        String tenant = newTenant("unfixed-hand");
        String coll = name("c");
        MutableClock clock = new MutableClock();
        ScriptedCanary canary = new ScriptedCanary();
        PciReconciler r = stillRedAfterItsReindex(tenant, coll, clock, canary);
        handReindex(leaf(tenant).schema(), indexOf(tenant, coll).name());

        clock.advance(Duration.ofHours(25));
        r.reconcileOnce();   // detects the rebuild: a fresh anchor, with no red_unfixed

        assertThat(logged("event=pci_anchor_rebuilt_detected")).isEqualTo(1);
        assertThat(indexOf(tenant, coll).anchor().orElseThrow().redUnfixed()).isNull();
        assertThat(r.reindexes()).isEqualTo(1);
        clock.advance(Duration.ofHours(25));
        r.reconcileOnce();
        assertThat(r.reindexes()).as("a day after the detection the red is judged afresh").isEqualTo(2);
    }

    @Test
    void sizeDeletesAndBulkDeleteStillFire_whileRedUnfixedIsSet() throws Exception {
        String tenant = newTenant("unfixed-bulk");
        String coll = name("c");
        MutableClock clock = new MutableClock();
        ScriptedCanary canary = new ScriptedCanary();
        PciReconciler r = stillRedAfterItsReindex(tenant, coll, clock, canary);
        assertThat(indexOf(tenant, coll).anchor().orElseThrow().redUnfixed()).isNotNull();

        deleteRows(tenant, coll, 0, 120);   // 40%
        clock.advance(Duration.ofMinutes(10));
        r.reconcileOnce();

        assertThat(r.reindexes()).isEqualTo(2);
        assertThat(logs()).filteredOn(l -> l.contains("event=pci_index_reindexed")).hasSize(2).last()
            .asString().contains("trigger=bulk_delete");
        assertThat(logged("event=pci_leaf_vacuumed")).isEqualTo(1);
    }

    @Test
    void redUnfixedIsInTheAnchor_notInMemory_soARestartedEngineKeepsHolding() throws Exception {
        String tenant = newTenant("unfixed-restart");
        String coll = name("c");
        MutableClock clock = new MutableClock();
        ScriptedCanary canary = new ScriptedCanary();
        stillRedAfterItsReindex(tenant, coll, clock, canary);

        clock.advance(Duration.ofHours(25));
        PciReconciler restarted = reindexingReconciler(clock, canary);   // no memory of the REINDEX
        restarted.reconcileOnce();
        restarted.reconcileOnce();
        assertThat(restarted.reindexes()).as("the anchor holds it").isZero();

        canary.sample = List.of(new float[] {9f, 8f, 7f});
        clock.advance(Duration.ofHours(25));
        restarted.reconcileOnce();
        restarted.reconcileOnce();
        assertThat(restarted.reindexes()).as("and a new red query lifts it on the restarted engine too").isEqualTo(1);
    }

    // -- the hold is on the queries that read red, and on the collection's size ----------------------------------

    private static final float[] OUTLIER = {1f, 2f, 3f};

    private static List<float[]> window(float... greens) {
        List<float[]> out = new java.util.ArrayList<>();
        out.add(OUTLIER);
        for (float g : greens) {
            out.add(new float[] {g, g, g});
        }
        return out;
    }

    @Test
    void oneOutlierThatStaysRed_withNewGreenQueriesEnteringTheWindow_isNeverRebuiltAgain() throws Exception {
        String tenant = newTenant("outlier");
        String coll = name("c");
        MutableClock clock = new MutableClock();
        ScriptedCanary canary = new ScriptedCanary();
        canary.sample = window(10f, 11f);
        canary.reds = List.of(OUTLIER);
        PciReconciler r = stillRedAfterItsReindex(tenant, coll, clock, canary);
        List<String> held = indexOf(tenant, coll).anchor().orElseThrow().redUnfixed().queries();
        assertThat(held).isEqualTo(PciQueryRing.queryIds(List.of(OUTLIER)));

        for (int day = 0; day < 4; day++) {
            canary.sample = window(20f + day, 21f + day, 22f + day);   // the newest ten change; the outlier stays red
            clock.advance(Duration.ofHours(25));
            r.reconcileOnce();
            r.reconcileOnce();
        }

        assertThat(r.reindexes()).as("the same outlier is the same red").isEqualTo(1);
        assertThat(indexOf(tenant, coll).anchor().orElseThrow().redUnfixed().queries()).isEqualTo(held);
    }

    @Test
    void aRedSetThatShrinks_asTheOldestRedQueryLeavesTheWindow_keepsTheHold() throws Exception {
        String tenant = newTenant("shrink");
        String coll = name("c");
        MutableClock clock = new MutableClock();
        ScriptedCanary canary = new ScriptedCanary();
        float[] b = {5f, 5f, 5f};
        canary.sample = window(5f, 10f);   // A (the outlier) is the oldest, B the next
        canary.reds = List.of(OUTLIER, b);
        PciReconciler r = stillRedAfterItsReindex(tenant, coll, clock, canary);
        List<String> recorded = indexOf(tenant, coll).anchor().orElseThrow().redUnfixed().queries();
        assertThat(recorded).isEqualTo(PciQueryRing.queryIds(List.of(OUTLIER, b)));

        // A new green query enters the window and A is evicted: B alone reads red, on the same graph.
        canary.sample = List.of(b, new float[] {10f, 10f, 10f}, new float[] {11f, 11f, 11f});
        canary.reds = List.of(b);
        for (int day = 0; day < 2; day++) {
            clock.advance(Duration.ofHours(25));
            r.reconcileOnce();
            r.reconcileOnce();
        }

        assertThat(r.reindexes()).as("B is the same uncured red: no wasted REINDEX").isEqualTo(1);
        assertThat(indexOf(tenant, coll).anchor().orElseThrow().redUnfixed().queries()).isEqualTo(recorded);
    }

    @Test
    void aNewQueryThatReadsRed_liftsTheHold() throws Exception {
        String tenant = newTenant("newred");
        String coll = name("c");
        MutableClock clock = new MutableClock();
        ScriptedCanary canary = new ScriptedCanary();
        canary.sample = window(10f);
        canary.reds = List.of(OUTLIER);
        PciReconciler r = stillRedAfterItsReindex(tenant, coll, clock, canary);

        canary.sample = window(10f, 30f);
        canary.reds = List.of(OUTLIER, new float[] {30f, 30f, 30f});   // a new cause
        clock.advance(Duration.ofHours(25));
        r.reconcileOnce();   // judged on the old red; the daily run then reads the new one
        r.reconcileOnce();

        assertThat(r.reindexes()).isEqualTo(2);
        assertThat(indexOf(tenant, coll).anchor().orElseThrow().redUnfixed().queries())
            .isEqualTo(PciQueryRing.queryIds(canary.reds));
    }

    @Test
    void theOutlierAgingOutOfTheWindow_withEverythingElseGreen_clearsViaTheGreenRun() throws Exception {
        String tenant = newTenant("aged");
        String coll = name("c");
        MutableClock clock = new MutableClock();
        ScriptedCanary canary = new ScriptedCanary();
        canary.sample = window(10f);
        canary.reds = List.of(OUTLIER);
        PciReconciler r = stillRedAfterItsReindex(tenant, coll, clock, canary);

        canary.state = PciCanary.State.GREEN;   // the outlier is out of the newest ten
        clock.advance(Duration.ofHours(25));
        r.reconcileOnce();   // the daily run reads green at the end of this pass
        r.reconcileOnce();

        assertThat(indexOf(tenant, coll).anchor().orElseThrow().redUnfixed()).isNull();
        assertThat(logged("event=pci_red_unfixed_cleared")).isEqualTo(1);
        assertThat(r.reindexes()).isEqualTo(1);
    }

    @Test
    void insertOnlyGrowthOfTenPercentOrMore_liftsTheHold_andLessKeepsIt() throws Exception {
        String tenant = newTenant("growth");
        String coll = name("c");
        MutableClock clock = new MutableClock();
        ScriptedCanary canary = new ScriptedCanary();
        PciReconciler r = stillRedAfterItsReindex(tenant, coll, clock, canary);   // 300 rows at the red

        addRows(tenant, coll, 29);   // +9.67%
        clock.advance(Duration.ofHours(25));
        r.reconcileOnce();
        r.reconcileOnce();
        assertThat(r.reindexes()).as("under 10%: held").isEqualTo(1);

        addRows(tenant, coll, 1);   // 330 rows: +10%
        clock.advance(Duration.ofHours(25));
        r.reconcileOnce();
        r.reconcileOnce();
        assertThat(r.reindexes()).as("10% or more: the red is judged afresh").isEqualTo(2);
        assertThat(indexOf(tenant, coll).anchor().orElseThrow().redUnfixed().rows()).isEqualTo(330);
    }

    // -- a bulk delete owed while the index was backed off ---------------------------------------------------------

    @Test
    void aFallSeenWhileBackedOff_thenAHandReindex_givesNoBulkDeleteReindexAndNoVacuum() throws Exception {
        String tenant = newTenant("owedbulk");
        collection(tenant, name("c"), 300);
        MutableClock clock = new MutableClock();
        PciReconciler r = reindexingReconciler(clock, new ScriptedCanary());
        r.reconcileOnce();
        r.reconcileOnce();
        PciCatalog.Leaf leaf = leaf(tenant);
        String index = indexOf(tenant, name("c")).name();

        r.reindexState().failed(index, clock.instant());   // backed off for ten minutes
        deleteRows(tenant, name("c"), 0, 120);   // 40%
        clock.advance(Duration.ofMinutes(1));
        r.reconcileOnce();   // the fall is seen and owed; the index is backed off
        handReindex(leaf.schema(), index);
        clock.advance(Duration.ofMinutes(1));
        r.reconcileOnce();   // the rebuild is found: the baseline starts over
        clock.advance(Duration.ofMinutes(11));   // the backoff is over
        r.reconcileOnce();
        r.reconcileOnce();

        assertThat(logged("event=pci_anchor_rebuilt_detected")).isEqualTo(1);
        assertThat(logged("trigger=bulk_delete")).as("the owed fall died with the old build").isZero();
        assertThat(logged("event=pci_leaf_vacuumed")).isZero();
        assertThat(logs()).as("the index is not rebuilt by the engine (the leaf's own index may be: another rule)")
            .noneMatch(l -> l.contains("event=pci_index_reindexed") && l.contains("index=" + index));
    }

    /** {@code rows} more rows for an existing collection. */
    void addRows(String tenant, String collection, int rows) throws Exception {
        int gen = ++generation;
        asSuperuser(su -> {
            var step = su.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL,
                CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_384, CHUNKS.METADATA);
            for (int i = 0; i < rows; i++) {
                step = step.values(tenant, collection,
                    Chash.fromHex(Chash.ofText(tenant + "/" + collection + "/add/" + gen + "/" + i).toHex()).toBytes(),
                    M384, "added " + i, vector(1000 + gen * 100 + i), JSONB.jsonb("{}"));
            }
            step.onConflictDoNothing().execute();
            return null;
        });
    }
}
