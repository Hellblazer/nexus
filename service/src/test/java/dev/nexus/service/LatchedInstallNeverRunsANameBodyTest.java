// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import dev.nexus.service.CollectionRouteCensus.StoreRename;
import dev.nexus.service.CollectionRouteFixture.Resp;
import dev.nexus.service.datamove.CompletionCheck;
import dev.nexus.service.datamove.Gate3;
import dev.nexus.service.datamove.RenameReadiness.Missing;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.OneRowRename;
import dev.nexus.service.db.RenameGuard;
import dev.nexus.service.db.RenameOutcome;
import dev.nexus.service.db.RenamePathMismatchException;
import dev.nexus.service.db.RenameRefusedException;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 P3.5a (bead nexus-p2m1x.23), the pin behind P3.5c's census: no LATCHED install (the gate 3 latch row exists) ever reaches a
 * name-path rename body. P3.5c marks those bodies n/a on the strength of this. {@code RenameRunsOnlyThroughRunRenameTest} is the structural
 * half (each primitive opens its transaction only inside {@code runRename}); this is the behavioural half, at three levels:
 * <ul>
 * <li>(a) {@code RenameGuard.runRename} for every state with the row present (readiness complete, and each missing condition), and the
 *     no-row/complete state ({@code latch_row_missing}) as its contrast, with a name body that records that it ran;</li>
 * <li>(b) {@code CatalogRepository.renameCollectionOutcome} with a real latch row: the catalog name-path cascade does not run, shown by
 *     the {@code xmin} of every row of the source's footprint (the registry row and each content table) staying the same;</li>
 * <li>(c) the HTTP rename route of each of the five per-store renames on a latched install answers the no-op (the premise met),
 *     {@code rename_not_in_registry} or {@code rename_refused}, never its name body, with {@code xmin} unchanged.</li>
 * </ul>
 * Every state also has its non-vacuity: the same call with no latch row does run the name body.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class LatchedInstallNeverRunsANameBodyTest {

    private static final org.jooq.Table<?> LATCH = DSL.table(DSL.name("nexus", "rdr229_gate_latch"));

    /** The footprint of a collection: every table that carries its name, and the column that does. */
    private static final Map<String, String> FOOTPRINT = new LinkedHashMap<>();

    static {
        FOOTPRINT.put("catalog_collections", "name");
        FOOTPRINT.put("chunks", "collection");
        FOOTPRINT.put("catalog_document_chunks", "collection");
        FOOTPRINT.put("chunk_orphaned_at", "collection");
        FOOTPRINT.put("topic_assignments", "source_collection");
        FOOTPRINT.put("topics", "collection");
        FOOTPRINT.put("taxonomy_meta", "collection");
        FOOTPRINT.put("taxonomy_centroids", "collection");
        FOOTPRINT.put("document_aspects", "collection");
        FOOTPRINT.put("document_highlights", "collection");
        FOOTPRINT.put("catalog_documents", "physical_collection");
        FOOTPRINT.put("relevance_log", "collection");
        FOOTPRINT.put("search_telemetry", "collection");
        FOOTPRINT.put("hook_failures", "collection");
        FOOTPRINT.put("gc_audit", "collection");
    }

    CollectionRouteFixture fx;
    TenantScope scope;
    final AtomicInteger n = new AtomicInteger();

    @BeforeAll
    void startAll() throws Exception {
        fx = new CollectionRouteFixture();
        scope = new TenantScope(fx.svcDs);
    }

    @AfterAll
    void stopAll() {
        if (fx != null) fx.close();
    }

    @BeforeEach
    void noLatch() {
        fx.gate3.set(false);   // removes the latch row
        fx.renameWindow.set(Missing.NAME_KEY_DROP_MOVE);
    }

    // ---- helpers ------------------------------------------------------------------------------------------------------

    private String label(String stem) {
        return "knowledge__" + stem + "-" + n.incrementAndGet() + "__minilm-l6-v2-384__v1";
    }

    private String seeded(String stem) {
        String coll = label(stem);
        fx.asSuperuser(ctx -> {
            RenameSeeds.seed(ctx, fx.tenant, coll, "p" + n.incrementAndGet());
            return null;
        });
        return coll;
    }

    private String registered(String stem) {
        String coll = label(stem);
        fx.asSuperuser(ctx -> {
            PgContainerHelper.insertCollection(ctx, fx.tenant, coll);
            return null;
        });
        return coll;
    }

    private void latchRow(boolean present) {
        fx.asSuperuser(ctx -> {
            if (present) {
                CompletionCheck.insertLatch(ctx, CompletionCheck.GATE3, "test-engine", false);
            } else {
                ctx.deleteFrom(LATCH).execute();
            }
            return null;
        });
    }

    /** The row version ({@code xmin}) and name of every row of the footprint tables, for the tenant. */
    private Map<String, List<String>> footprint() {
        Map<String, List<String>> out = new LinkedHashMap<>();
        FOOTPRINT.forEach((table, column) -> {
            Field<String> xmin = DSL.field(DSL.name("xmin")).cast(SQLDataType.VARCHAR);
            Field<String> name = DSL.field(DSL.name(column), String.class);
            out.put(table, fx.asSuperuser(ctx -> ctx.select(xmin, name).from(DSL.table(DSL.name("nexus", table)))
                .where(DSL.field(DSL.name("tenant_id"), String.class).eq(fx.tenant))
                .fetch().map(r -> r.value1() + "|" + r.value2()).stream().sorted().toList()));
        });
        return out;
    }

    private static void assertSame(String what, Map<String, List<String>> before, Map<String, List<String>> after) {
        before.forEach((table, rows) -> assertThat(after.get(table)).as("%s: every row of %s is the same row version", what, table)
            .isEqualTo(rows));
    }

    private static RenameRefusedException refusalIn(Throwable t) {
        for (Throwable c = t; c != null; c = c.getCause()) {
            if (c instanceof RenameRefusedException r) return r;
        }
        return null;
    }

    private static boolean has(Throwable t, Class<? extends Throwable> type) {
        for (Throwable c = t; c != null; c = c.getCause()) {
            if (type.isInstance(c)) return true;
        }
        return false;
    }

    /** The name-key drop move as far as the simulation needs it, and the registry row renamed in place (the content keeps the old label). */
    private void oneRowRename(String from, String to) {
        fx.asSuperuser(ctx -> {
            for (var key : CATALOG_COLLECTIONS.getKeys()) {
                if (!key.getFields().contains(CATALOG_COLLECTIONS.NAME)) continue;
                for (var fk : key.getReferences()) {
                    ctx.alterTable(fk.getTable()).dropConstraintIfExists(fk.getName()).execute();
                }
            }
            // the chunk table's own name-keyed keys too (topic_assignments_chunk_fk), so a per-store name path run alone is not refused
            for (var key : dev.nexus.service.jooq.nexus.Tables.CHUNKS.getKeys()) {
                if (!key.getFields().contains(dev.nexus.service.jooq.nexus.Tables.CHUNKS.COLLECTION)) continue;
                for (var fk : key.getReferences()) {
                    ctx.alterTable(fk.getTable()).dropConstraintIfExists(fk.getName()).execute();
                }
            }
            return ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, to)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.NAME.eq(from)).execute();
        });
    }

    // ---- (a) runRename: the name body never runs with the row present --------------------------------------------------

    private record State(String name, boolean row, Missing readiness) { }

    private static final List<State> STATES = List.of(
        new State("row, readiness complete", true, Missing.NONE),
        new State("row, drop move missing", true, Missing.NAME_KEY_DROP_MOVE),
        new State("row, restart missing", true, Missing.P35B_RESTART),
        new State("no row, readiness complete (latch_row_missing)", false, Missing.NONE),
        new State("no row, drop move missing (the name path)", false, Missing.NAME_KEY_DROP_MOVE));

    @Test
    void runRename_neverInvokesTheNameBodyWhenTheLatchRowExists() {
        int nameRuns = 0;
        for (State s : STATES) {
            latchRow(s.row());
            var guard = new RenameGuard(() -> s.readiness(), OneRowRename.failClosed());
            AtomicInteger names = new AtomicInteger(), ones = new AtomicInteger();
            Object result = null;
            Throwable thrown = null;
            try {
                result = guard.runRename(scope, fx.tenant, ctx -> {
                    names.incrementAndGet();
                    return "name";
                }, ctx -> {
                    ones.incrementAndGet();
                    return "one";
                });
            } catch (RuntimeException e) {
                thrown = e;
            }

            if (s.row()) {
                assertThat(names.get()).as("%s: the name body never ran", s.name()).isZero();
            }
            if (s.row() && s.readiness() == Missing.NONE) {
                assertThat(result).as(s.name()).isEqualTo("one");
                assertThat(ones.get()).isEqualTo(1);
            } else if (s.row() || s.readiness() == Missing.NONE) {
                assertThat(refusalIn(thrown)).as("%s: refused: %s", s.name(), thrown).isNotNull();
                assertThat(refusalIn(thrown).missing()).isEqualTo(s.row() ? s.readiness() : Missing.LATCH_ROW_MISSING);
                assertThat(names.get()).as("%s: neither body ran", s.name()).isZero();
                assertThat(ones.get()).isZero();
            } else {
                assertThat(result).as("non-vacuity: with no row and the move missing the name body does run").isEqualTo("name");
                nameRuns += names.get();
            }
        }
        assertThat(nameRuns).as("non-vacuity: exactly the one pre-latch state ran a name body").isEqualTo(1);
    }

    // ---- (b) the catalog name-path cascade does not run on a latched install -------------------------------------------

    @Test
    void renameCollectionOutcome_onALatchedInstall_doesNotRunTheCatalogNamePathCascade() {
        for (State s : STATES.subList(0, 4)) {
            String from = seeded("cat-a");
            String to = label("cat-b");
            Map<String, List<String>> before = footprint();
            latchRow(s.row());
            var repo = new CatalogRepository(scope, Gate3.never(), new RenameGuard(() -> s.readiness(), OneRowRename.failClosed()));

            assertThatThrownBy(() -> repo.renameCollectionOutcome(fx.tenant, from, to, null, null, null, null)).as(s.name())
                .satisfies(t -> {
                    if (s.row() && s.readiness() == Missing.NONE) {
                        assertThat(has(t, RenamePathMismatchException.class)).as("the fail-closed one-row seam: %s", t).isTrue();
                    } else {
                        assertThat(refusalIn(t)).as("rename_refused: %s", t).isNotNull();
                    }
                });

            assertSame(s.name() + ": the source's whole footprint", before, footprint());
            latchRow(false);
        }
    }

    /** With a one-row rename wired (a stub that writes nothing), the cascade still does not run: only P3.7's body does. */
    @Test
    void renameCollectionOutcome_withAOneRowBodyWired_runsThatBodyAndNotTheCascade() {
        String from = seeded("cat-stub-a");
        String to = label("cat-stub-b");
        Map<String, List<String>> before = footprint();
        latchRow(true);
        AtomicInteger ran = new AtomicInteger();
        OneRowRename stub = (ctx, tenant, o, nw) -> {
            ran.incrementAndGet();
            return new RenameOutcome(Map.of("stub_rows", 0), 1L, "e", true);
        };
        var repo = new CatalogRepository(scope, Gate3.never(), new RenameGuard(() -> Missing.NONE, stub));

        RenameOutcome out = repo.renameCollectionOutcome(fx.tenant, from, to, null, null, null, null);

        assertThat(ran.get()).as("the one-row body ran").isEqualTo(1);
        assertThat(out.counts()).as("and the cascade's counts are not in the answer").containsOnlyKeys("stub_rows");
        assertSame("the source's whole footprint", before, footprint());
    }

    // ---- (c) the five per-store HTTP routes on a latched install -------------------------------------------------------

    @Test
    void everyPerStoreRenameRoute_onALatchedInstall_neverRunsItsNameBody() {
        assertThat(CollectionRouteCensus.STORE_RENAMES).as("non-vacuity: the five routes").hasSize(5);
        for (StoreRename route : CollectionRouteCensus.STORE_RENAMES) {
            // 1. latched, a condition missing: rename_refused
            String a1 = seeded("rt-a1");
            String b1 = registered("rt-b1");
            Map<String, List<String>> before = footprint();
            fx.gate3.set(true);
            fx.renameWindow.set(Missing.NAME_KEY_DROP_MOVE);
            Resp refused = rename(route, a1, b1);
            assertThat(refused.status()).as("%s, drop move missing: %s", route.path(), refused.body()).isEqualTo(409);
            assertThat(refused.reason()).as(refused.body()).isEqualTo("rename_refused");
            assertSame(route.path() + " refused", before, footprint());

            // 2. latched and ready, the registry rename did not happen: rename_not_in_registry
            fx.renameWindow.set(Missing.NONE);
            Resp notInRegistry = rename(route, a1, b1);
            assertThat(notInRegistry.status()).as("%s, both labels live: %s", route.path(), notInRegistry.body()).isEqualTo(409);
            assertThat(notInRegistry.reason()).as(notInRegistry.body()).isEqualTo("rename_not_in_registry");
            assertSame(route.path() + " not in registry", before, footprint());

            // 3. latched and ready, the registry rename happened: the no-op, every count 0
            String a3 = seeded("rt-a3");
            String b3 = label("rt-b3");
            oneRowRename(a3, b3);
            Map<String, List<String>> beforeNoop = footprint();
            Resp noop = rename(route, a3, b3);
            assertThat(noop.status()).as("%s, the premise met: %s", route.path(), noop.body()).isEqualTo(200);
            JsonNode body = noop.json();
            assertThat(body.size()).as("a count answer: %s", noop.body()).isPositive();
            body.fields().forEachRemaining(e -> assertThat(e.getValue().asLong(-1)).as("%s: %s is 0", route.path(), e.getKey()).isZero());
            assertSame(route.path() + " no-op", beforeNoop, footprint());

            // non-vacuity: without the latch row the same route (a fresh pair) does run its name body and moves rows
            fx.gate3.set(false);
            fx.renameWindow.set(Missing.NAME_KEY_DROP_MOVE);
            String a4 = seeded("rt-a4");
            String b4 = registered("rt-b4");
            Map<String, List<String>> beforeName = footprint();
            Resp name = rename(route, a4, b4);
            assertThat(name.status()).as("%s with no latch row: %s", route.path(), name.body()).isEqualTo(200);
            assertThat(footprint()).as("%s: the name path rewrites rows, so some row version changed", route.path()).isNotEqualTo(beforeName);
        }
    }

    private Resp rename(StoreRename route, String from, String to) {
        return fx.post(route.path(), "{\"old\":" + json(from) + ",\"new\":" + json(to) + "}");
    }
}
