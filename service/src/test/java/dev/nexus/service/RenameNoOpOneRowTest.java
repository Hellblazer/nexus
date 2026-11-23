// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.datamove.CompletionCheck;
import dev.nexus.service.datamove.RenameReadiness.Missing;
import dev.nexus.service.db.AspectRepository;
import dev.nexus.service.db.ChashRepository;
import dev.nexus.service.db.OneRowRename;
import dev.nexus.service.db.RenameGuard;
import dev.nexus.service.db.RenameNoOpActivity;
import dev.nexus.service.db.RenameNotInRegistryException;
import dev.nexus.service.db.TaxonomyRepository;
import dev.nexus.service.db.TelemetryRepository;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.BiFunction;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 P3.5a (bead nexus-p2m1x.23), linda's rulings: in the one-row regime the per-store renames (chash, taxonomy, telemetry,
 * aspects, highlights) are a no-op that answers success, when the registry rename has moved the label (Q-A); the content's name
 * columns stay at the old label (the keys of the one-row regime depend on them), so a rewrite would break them. When the registry did
 * not move it, each is the typed 409 {@code rename_not_in_registry} and changes nothing.
 *
 * <p>P3.7's real one-row rename is not on this base, so the regime is simulated: the latch row is written, readiness is complete, and
 * the registry row is renamed in place as the superuser with its content left at the old label (the name keys' inbound foreign keys
 * dropped first, as {@code CollectionDeleteScopeByIdTest} does). Every table a store's name body would touch is compared by {@code xmin}
 * before and after: an UPDATE of any row, even to the same values, writes a new row version, so an equal {@code xmin} list means no
 * row was written. The quarantine retag rows of the chash rename and the queued aspect task bodies are not seeded here.
 * The control is the same call before the latch, which rewrites as it did before.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class RenameNoOpOneRowTest {

    private static final String TENANT = "rename-noop-tenant";
    private static final String SVC_ROLE = "svc_rename_noop";
    private static final String SVC_PASS = "svc_rename_noop_pass";
    private static final org.jooq.Table<?> LATCH = DSL.table(DSL.name("nexus", "rdr229_gate_latch"));

    /** The key set of each map-answering route's name path, which the one-row answer must have too. */
    private static final Map<String, Set<String>> KEYS = Map.of(
        "taxonomy", Set.of("topics", "assignments", "meta"),
        "telemetry", Set.of("search_telemetry", "hook_failures"));

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    ChashRepository chash;
    TaxonomyRepository taxonomy;
    TelemetryRepository telemetry;
    AspectRepository aspects;
    final AtomicInteger n = new AtomicInteger();
    /** Main's readiness input. */
    final AtomicReference<Missing> readiness = new AtomicReference<>(Missing.NAME_KEY_DROP_MOVE);

    /**
     * One per-store rename: its call, every table (with its name column) its name body would touch, and the ones it rewrites by an
     * explicit UPDATE (the rest follow by foreign key cascade, which the one-row simulation has dropped).
     */
    private record Store(String name, BiFunction<String, String, Object> rename, Map<String, String> touched, List<String> direct) { }

    private List<Store> stores;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        // the chunks table is partitioned per tenant: this class's tenant gets its leaves (the engine does it at a tenant's first token)
        su(ctx -> {
            PgContainerHelper.ensureTenantPartitions(ctx, TENANT);
            return null;
        });
        var config = new HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(4);
        config.setAutoCommit(true);
        svcDs = new HikariDataSource(config);

        var guard = new RenameGuard(readiness::get, OneRowRename.failClosed());
        var scope = new TenantScope(svcDs);
        chash = new ChashRepository(scope, guard);
        taxonomy = new TaxonomyRepository(scope, guard);
        telemetry = new TelemetryRepository(scope, guard);
        aspects = new AspectRepository(scope, guard);
        stores = List.of(
            new Store("chash", (o, nw) -> chash.renameCollection(TENANT, o, nw),
                Map.of("chunks", "collection", "catalog_document_chunks", "collection", "chunk_orphaned_at", "collection",
                    "topic_assignments", "source_collection"),
                List.of("chunks", "catalog_document_chunks")),
            new Store("taxonomy", (o, nw) -> taxonomy.renameCollection(TENANT, o, nw),
                Map.of("topics", "collection", "taxonomy_meta", "collection", "topic_assignments", "source_collection"),
                List.of("topics", "taxonomy_meta", "topic_assignments")),
            new Store("telemetry", (o, nw) -> telemetry.renameCollection(TENANT, o, nw),
                Map.of("search_telemetry", "collection", "hook_failures", "collection"),
                List.of("search_telemetry", "hook_failures")),
            new Store("aspects", (o, nw) -> aspects.renameAspectCollection(TENANT, o, nw),
                Map.of("document_aspects", "collection"), List.of("document_aspects")),
            new Store("highlights", (o, nw) -> aspects.renameHighlightsCollection(TENANT, o, nw),
                Map.of("document_highlights", "collection"), List.of("document_highlights")));
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @BeforeEach
    void beforeTheLatch() {
        readiness.set(Missing.NAME_KEY_DROP_MOVE);
        su(ctx -> ctx.deleteFrom(LATCH).execute());
    }

    // ---- helpers ------------------------------------------------------------------------------------------------------

    private <T> T su(Function<DSLContext, T> work) {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    /** A conformant label no other row of this class has, with the model the seeds use. */
    private String label(String stem) {
        return "knowledge__" + stem + "-" + n.incrementAndGet() + "__minilm-l6-v2-384__v1";
    }

    /** A collection with a row in every table that carries a collection name. */
    private String seeded(String stem) {
        String coll = label(stem);
        su(ctx -> {
            RenameSeeds.seed(ctx, TENANT, coll, "p" + n.incrementAndGet());
            return null;
        });
        return coll;
    }

    /** A bare registry row (the target of a name-path rename). */
    private String registered(String stem) {
        String coll = label(stem);
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, TENANT, coll);
            return null;
        });
        return coll;
    }

    /** Each row of the tenant in {@code table}: its row version ({@code xmin}) and the name column it carries, sorted. */
    private List<String> versions(String table, String nameColumn) {
        Field<String> xmin = DSL.field(DSL.name("xmin")).cast(SQLDataType.VARCHAR);
        Field<String> name = DSL.field(DSL.name(nameColumn), String.class);
        return su(ctx -> ctx.select(xmin, name).from(DSL.table(DSL.name("nexus", table)))
            .where(DSL.field(DSL.name("tenant_id"), String.class).eq(TENANT))
            .fetch().map(r -> r.value1() + "|" + r.value2()).stream().sorted().toList());
    }

    private int rowsNamed(String table, String nameColumn, String label) {
        return su(ctx -> ctx.fetchCount(DSL.table(DSL.name("nexus", table)),
            DSL.field(DSL.name("tenant_id"), String.class).eq(TENANT).and(DSL.field(DSL.name(nameColumn), String.class).eq(label))));
    }

    /** Every touched table's versions, for {@code store}. */
    private Map<String, List<String>> snapshot(Store s) {
        Map<String, List<String>> out = new LinkedHashMap<>();
        s.touched().forEach((table, column) -> {
            out.put(table, versions(table, column));
            assertThat(out.get(table)).as("%s: non-vacuity: %s holds rows", s.name(), table).isNotEmpty();
        });
        return out;
    }

    private static boolean zero(Object counts) {
        if (counts instanceof Integer i) {
            return i == 0;
        }
        if (counts instanceof Map<?, ?> m) {
            return !m.isEmpty() && m.values().stream().allMatch(v -> v instanceof Integer i && i == 0);
        }
        throw new AssertionError("an unexpected answer shape: " + counts);
    }

    private static boolean someChanged(Object counts) {
        if (counts instanceof Integer i) {
            return i > 0;
        }
        return counts instanceof Map<?, ?> m && m.values().stream().anyMatch(v -> v instanceof Integer i && i > 0);
    }

    private static void assertKeySet(Store s, Object answer) {
        if (answer instanceof Map<?, ?> m) {
            assertThat(m.keySet()).as("%s: the answer's keys are the name path's", s.name()).isEqualTo(KEYS.get(s.name()));
        } else {
            assertThat(KEYS).as("%s answers a bare count", s.name()).doesNotContainKey(s.name());
        }
    }

    /**
     * P3.7's state, as far as this base can make it: the latch row, readiness complete, the name keys' inbound foreign keys dropped, and
     * the registry row renamed in place (the content keeps the old label).
     */
    private void oneRowRename(String from, String to) {
        latchAndDropKeys();
        int moved = su(ctx -> ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, to)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(from)).execute());
        assertThat(moved).as("setup: the registry row took its new label").isEqualTo(1);
    }

    private void latchAndDropKeys() {
        dropNameKeys();
        su(ctx -> {
            CompletionCheck.insertLatch(ctx, CompletionCheck.GATE3, "test-engine", false);
            return null;
        });
        readiness.set(Missing.NONE);
    }

    /** The name-key drop move, as far as this class needs it: every inbound foreign key on a registry key that holds {@code name}. */
    private void dropNameKeys() {
        su(ctx -> {
            for (var key : CATALOG_COLLECTIONS.getKeys()) {
                if (!key.getFields().contains(CATALOG_COLLECTIONS.NAME)) continue;
                for (var fk : key.getReferences()) {
                    ctx.alterTable(fk.getTable()).dropConstraintIfExists(fk.getName()).execute();
                }
            }
            return null;
        });
    }

    private static boolean notInRegistry(Throwable t) {
        for (Throwable c = t; c != null; c = c.getCause()) {
            if (c instanceof RenameNotInRegistryException) return true;
        }
        return false;
    }

    // ---- the one-row regime, the registry rename happened: a no-op that answers success -------------------------------

    @Test
    void afterAOneRowRename_aPerStoreRenameOfOldToNewChangesNothingAndAnswersZero() {
        for (Store s : stores) {
            String from = seeded(s.name() + "-a");
            String to = label(s.name() + "-b");
            Map<String, List<String>> before = snapshot(s);
            oneRowRename(from, to);
            long noops = RenameNoOpActivity.total();

            Object answer = s.rename().apply(from, to);
            Object again = s.rename().apply(from, to);

            assertThat(zero(answer)).as("%s: success with zero rows changed, got %s", s.name(), answer).isTrue();
            assertThat(zero(again)).as("%s: and again, got %s", s.name(), again).isTrue();
            assertKeySet(s, answer);
            s.touched().forEach((table, column) -> {
                assertThat(versions(table, column)).as("%s: every row of %s is the same row version, still naming %s", s.name(), table, from)
                    .isEqualTo(before.get(table));
                assertThat(rowsNamed(table, column, to)).as("%s: nothing in %s was filed under the new label", s.name(), table).isZero();
            });
            assertThat(RenameNoOpActivity.total()).as("%s: each no-op is counted, once committed", s.name()).isEqualTo(noops + 2);
            beforeTheLatch();
        }
    }

    // ---- the registry rename did not happen: a typed 409 and nothing changed (linda's Q-A) ----------------------------------

    /** Both labels are live registry rows: the registry rename did not happen. */
    @Test
    void whenTheRegistryRenameDidNotHappen_aPerStoreRenameIsRefusedAndChangesNothing() {
        for (Store s : stores) {
            String from = seeded(s.name() + "-live-a");
            String to = registered(s.name() + "-live-b");
            Map<String, List<String>> before = snapshot(s);
            latchAndDropKeys();
            long noops = RenameNoOpActivity.total();

            assertThatThrownBy(() -> s.rename().apply(from, to)).as("%s: both labels live", s.name())
                .satisfies(t -> assertThat(notInRegistry(t)).as("%s: rename_not_in_registry, got %s", s.name(), t).isTrue());

            s.touched().forEach((table, column) -> assertThat(versions(table, column))
                .as("%s: %s is untouched", s.name(), table).isEqualTo(before.get(table)));
            assertThat(RenameNoOpActivity.total()).as("%s: a refusal is not a no-op", s.name()).isEqualTo(noops);
            beforeTheLatch();
        }
    }

    /** The registry renamed A to B, and the call names a label that is not B: the new label is not a live row. */
    @Test
    void aTypoedNewLabelIsRefused_andChangesNothing() {
        for (Store s : stores) {
            String from = seeded(s.name() + "-typo-a");
            String to = label(s.name() + "-typo-b");
            Map<String, List<String>> before = snapshot(s);
            oneRowRename(from, to);
            String typo = to + "x";

            assertThatThrownBy(() -> s.rename().apply(from, typo)).as("%s: a label no registry row has", s.name())
                .satisfies(t -> assertThat(notInRegistry(t)).as("%s: rename_not_in_registry, got %s", s.name(), t).isTrue());

            s.touched().forEach((table, column) -> assertThat(versions(table, column))
                .as("%s: %s is untouched", s.name(), table).isEqualTo(before.get(table)));
            beforeTheLatch();
        }
    }

    // ---- the control: before the latch the same call rewrites, as it did ------------------------------------------------

    @Test
    void beforeTheLatch_theSameCallRewritesTheNameColumnsAsToday() {
        dropNameKeys();   // whatever order the tests run in, the foreign keys are as the one-row tests leave them
        for (Store s : stores) {
            String from = seeded(s.name() + "-c");
            String to = registered(s.name() + "-d");   // the primitives re-point rows at a registered target, as their callers register it
            long noops = RenameNoOpActivity.total();
            s.direct().forEach(table -> assertThat(rowsNamed(table, s.touched().get(table), from))
                .as("%s: non-vacuity: %s holds a row under the old label", s.name(), table).isPositive());

            Object answer = s.rename().apply(from, to);

            assertThat(someChanged(answer)).as("%s: rows changed, got %s", s.name(), answer).isTrue();
            assertKeySet(s, answer);
            s.direct().forEach(table -> {
                String column = s.touched().get(table);
                assertThat(rowsNamed(table, column, from)).as("%s: %s no longer names the old label", s.name(), table).isZero();
                assertThat(rowsNamed(table, column, to)).as("%s: %s names the new label", s.name(), table).isPositive();
            });
            assertThat(RenameNoOpActivity.total()).as("%s: the name path is not a no-op", s.name()).isEqualTo(noops);
        }
    }
}
