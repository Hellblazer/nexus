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
import java.util.OptionalDouble;
import java.util.OptionalLong;
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

    /** REINDEX on, with a canary that is always green: the leaf size rule runs, and reads the leaf index's anchor. */
    PciReconciler reindexingReconciler(Clock clock) {
        PciSettings s = new PciSettings(true, B, 600, 16);
        return new PciReconciler(catalog::read, builderSession(s), PciIndexSweep.create(svcDs, s), s, clock,
            PciReconciler.SET_LOCAL_TENANT, PciReconciler.COUNT_TIMEOUT, Duration.ofSeconds(60),
            java.util.random.RandomGenerator.getDefault(), tenantsSeen -> 0,
            new PciCanarySchedule(new PciLeafIndexMaintenanceIntegrationTest.ScriptedCanary(), clock, System::nanoTime));
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
        System.out.println("SUBSTRATE-FACT index=" + index + " oid_before=" + before.oid() + " oid_after=" + after.oid()
            + " relfilenode_before=" + before.relfilenode() + " relfilenode_after=" + after.relfilenode()
            + " comment_carried=" + before.comment().equals(after.comment()));
        // The name is what the engine keys on (factsOf found the index by it). The oid is NOT kept: REINDEX CONCURRENTLY
        // builds a new index and swaps the names, so the new one has a new oid as well as new storage.
        assertThat(after.oid()).as("a new index under the same name").isNotEqualTo(before.oid());
        assertThat(after.relfilenode()).as("the rebuild gives it new storage").isNotEqualTo(before.relfilenode());
        assertThat(after.comment()).as("the COMMENT is carried across the swap").isEqualTo(before.comment());
    }

    // -- the engine's anchors are v2 ---------------------------------------------------------------------------

    @Test
    void aBuild_anchorsAtV2_withTheRelfilenodeItWasWrittenAgainst() throws Exception {
        String tenant = newTenant("v2-built");
        collection(tenant, name("c"), 300);

        reconciler(settings(), Clock.systemUTC()).reconcileOnce();

        PciCatalog.Leaf leaf = leaf(tenant);
        Facts facts = factsOf(leaf.schema(), indexOf(tenant, name("c")).name());
        assertThat(facts.comment()).contains("\"nexus_pci\":2");
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
    void aV1Anchor_staysValid_isNeitherReanchoredNorAdopted_andGainsTheFieldAtTheNextEngineWrite() throws Exception {
        String tenant = newTenant("v1");
        collection(tenant, name("c"), 300);
        MutableClock clock = new MutableClock();
        PciReconciler r = reconciler(settings(), clock);
        r.reconcileOnce();
        PciCatalog.Leaf leaf = leaf(tenant);
        String index = indexOf(tenant, name("c")).name();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.commentIndexV1Anchor(su, leaf.schema(), index);
        }
        String v1 = factsOf(leaf.schema(), index).comment();
        assertThat(v1).contains("\"nexus_pci\":1").doesNotContain("relfilenode");

        handReindex(leaf.schema(), index);   // a v1 anchor has no relfilenode to differ from
        clock.advance(Duration.ofHours(1));
        r.reconcileOnce();
        r.reconcileOnce();

        assertThat(factsOf(leaf.schema(), index).comment()).as("left exactly as it was").isEqualTo(v1);
        assertThat(logged("event=pci_anchor_rebuilt_detected")).isZero();
        assertThat(logged("event=pci_anchor_adopted")).isZero();
        assertThat(PciCatalog.parseAnchor(v1)).hasValueSatisfying(a -> assertThat(a.relfilenode()).isNull());

        // The next time the engine writes it, it is v2, stamped on the writing session.
        try (PciBuilderSession.Pass pass = builderSession(settings()).open()) {
            var anchor = new PciCatalog.Anchor(clock.instant(), 300, 1, PciCatalog.ORIGIN_BUILT);
            assertThat(pass.comment(leaf, indexOf(tenant, name("c")), anchor))
                .isEqualTo(PciBuilderSession.DdlOutcome.DONE);
        }
        Facts now = factsOf(leaf.schema(), index);
        assertThat(now.comment()).contains("\"nexus_pci\":2");
        assertThat(PciCatalog.parseAnchor(now.comment())).hasValueSatisfying(
            a -> assertThat(a.relfilenode()).isEqualTo(Long.parseLong(now.relfilenode())));
    }

    // -- what the policies count from the new anchor -----------------------------------------------------------

    static PciReindexPolicy.Candidate candidate(PciCatalog.Anchor anchor, long rows, long writtenSince,
                                                PciCanary.State canary, int queries) {
        return new PciReindexPolicy.Candidate("pci_x", "c", 384, anchor, 1000, rows, writtenSince, OptionalLong.empty(),
            false, canary, queries, null, false, OptionalDouble.empty());
    }

    @Test
    void afterTheReanchor_theDeletesRuleAndTheDayFloor_countFromTheNewAnchor() throws Exception {
        String tenant = newTenant("policy");
        collection(tenant, name("c"), 300);
        MutableClock clock = new MutableClock();
        PciReconciler r = reconciler(settings(), clock);
        r.reconcileOnce();
        PciCatalog.Leaf leaf = leaf(tenant);
        String index = indexOf(tenant, name("c")).name();
        PciCatalog.Anchor old = indexOf(tenant, name("c")).anchor().orElseThrow();
        assertThat(old.rows()).isEqualTo(300);

        deleteRows(tenant, name("c"), 0, 150);
        handReindex(leaf.schema(), index);
        clock.advance(Duration.ofHours(2));
        r.reconcileOnce();   // detects, re-anchors at 150 rows
        r.reconcileOnce();   // reads from the new anchor
        PciReconciler.IndexReading reading = r.reading(leaf, index);
        PciCatalog.Anchor fresh = reading.anchor();
        assertThat(fresh.rows()).isEqualTo(150);
        assertThat(fresh.builtAt()).isEqualTo(clock.instant());

        Instant now = clock.instant();
        // Deletes since build: 300 - 150 from the old anchor is the whole index again; from the new one it is nothing.
        assertThat(PciReindexPolicy.trigger(candidate(old, reading.rows(), reading.writtenSince(),
            PciCanary.State.GREEN, 10), now)).contains(PciReconcilePlanner.Trigger.DELETES_SINCE_BUILD);
        assertThat(PciReindexPolicy.trigger(candidate(fresh, reading.rows(), reading.writtenSince(),
            PciCanary.State.GREEN, 10), now)).isEmpty();
        // The red-canary day floor: 23 h after the new anchor, 25 h after the old one.
        Instant later = fresh.builtAt().plus(Duration.ofHours(23));
        assertThat(later).isAfter(old.builtAt().plus(Duration.ofHours(24)));
        assertThat(PciReindexPolicy.trigger(candidate(old, 150, 0, PciCanary.State.RED, 10), later))
            .contains(PciReconcilePlanner.Trigger.RED_CANARY);
        assertThat(PciReindexPolicy.trigger(candidate(fresh, 150, 0, PciCanary.State.RED, 10), later)).isEmpty();
        assertThat(PciReindexPolicy.trigger(candidate(fresh, 150, 0, PciCanary.State.RED, 10),
            fresh.builtAt().plus(Duration.ofHours(25)))).contains(PciReconcilePlanner.Trigger.RED_CANARY);
    }

    // -- a leaf's own HNSW index -------------------------------------------------------------------------------

    @Test
    void aLeafsOwnIndex_withAV2Anchor_isReanchoredAfterAHandReindex() throws Exception {
        String tenant = newTenant("leaf");
        collection(tenant, name("keep"), 250);
        MutableClock clock = new MutableClock();
        PciReconciler r = reindexingReconciler(clock);
        r.reconcileOnce();   // builds the pci_ index: the install reference the leaf rule needs
        PciCatalog.Leaf leaf = leaf(tenant);
        PciCatalog.LeafIndex own = leaf.hnswIndex(384).orElseThrow();
        try (PciBuilderSession.Pass pass = builderSession(settings()).open()) {
            var anchored = pass.anchorAfterLeafReindex(leaf, own, 250, clock.instant());
            assertThat(anchored.outcome()).isEqualTo(PciBuilderSession.DdlOutcome.DONE);
        }
        Facts v2 = factsOf(leaf.schema(), own.name());
        assertThat(PciCatalog.parseAnchor(v2.comment())).hasValueSatisfying(
            a -> assertThat(a.relfilenode()).isEqualTo(Long.parseLong(v2.relfilenode())));
        clock.advance(Duration.ofHours(1));
        r.reconcileOnce();
        assertThat(factsOf(leaf.schema(), own.name()).comment()).as("unmoved: left alone").isEqualTo(v2.comment());
        assertThat(logged("event=pci_anchor_rebuilt_detected")).isZero();

        handReindex(leaf.schema(), own.name());
        Facts rebuilt = factsOf(leaf.schema(), own.name());
        assertThat(rebuilt.relfilenode()).isNotEqualTo(v2.relfilenode());
        clock.advance(Duration.ofHours(1));
        r.reconcileOnce();

        PciCatalog.Anchor re = PciCatalog.parseAnchor(factsOf(leaf.schema(), own.name()).comment()).orElseThrow();
        assertThat(re.relfilenode()).isEqualTo(Long.parseLong(rebuilt.relfilenode()));
        assertThat(re.builtAt()).isEqualTo(clock.instant());
        assertThat(re.origin()).isEqualTo("built");
        assertThat(logs()).anyMatch(l -> l.contains("event=pci_anchor_rebuilt_detected")
            && l.contains("index=" + own.name()) && l.contains("old_relfilenode=" + v2.relfilenode())
            && l.contains("new_relfilenode=" + rebuilt.relfilenode()));
        assertThat(r.reindexes()).as("a rebuilt index is no REINDEX candidate this pass").isZero();
    }
}
