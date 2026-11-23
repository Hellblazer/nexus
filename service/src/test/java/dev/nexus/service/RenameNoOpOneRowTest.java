// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.datamove.CompletionCheck;
import dev.nexus.service.datamove.RenameReadiness.Missing;
import dev.nexus.service.db.AspectRepository;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.ChashRepository;
import dev.nexus.service.db.OneRowRename;
import dev.nexus.service.db.RenameGuard;
import dev.nexus.service.db.RenameNoOpActivity;
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
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.BiFunction;
import java.util.function.Consumer;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (bead nexus-p2m1x.23), linda's ruling: in the one-row regime the per-store renames (chash, taxonomy, telemetry,
 * aspects, highlights) are a no-op that answers success. The registry rename has already moved the label; the content's name columns
 * stay at the old label (the keys of the one-row regime depend on them), so a rewrite would break them.
 *
 * <p>P3.7's real one-row rename is not on this base, so the regime is simulated: the latch row is written, readiness is complete, and
 * the registry row is renamed in place as the superuser with its content left at the old label (the name keys' inbound foreign keys
 * dropped first, as {@code CollectionDeleteScopeByIdTest} does). The rows each rename would have touched are compared by
 * {@code xmin} before and after: an UPDATE of any row, even to the same values, writes a new row version, so an equal {@code xmin}
 * list means no row was written. The control is the same call before the latch, which rewrites as it did before.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class RenameNoOpOneRowTest {

    private static final String TENANT = "rename-noop-tenant";
    private static final String SVC_ROLE = "svc_rename_noop";
    private static final String SVC_PASS = "svc_rename_noop_pass";
    private static final String MODEL = "bge-base-en-v15-768";
    private static final String TS = "2026-01-01T00:00:00.000000Z";
    private static final org.jooq.Table<?> LATCH = DSL.table(DSL.name("nexus", "rdr229_gate_latch"));

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    CatalogRepository catalog;
    ChashRepository chash;
    TaxonomyRepository taxonomy;
    TelemetryRepository telemetry;
    AspectRepository aspects;
    final AtomicInteger n = new AtomicInteger();
    /** Main's readiness input. */
    final AtomicReference<Missing> readiness = new AtomicReference<>(Missing.NAME_KEY_DROP_MOVE);

    /** One per-store rename: how its content is seeded under a label, its call, and the tables (with their name column) it rewrites. */
    private record Store(String name, Consumer<String> seed, BiFunction<String, String, Object> rename, List<String> tables) { }

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
        // the name-key drop move, as far as this class needs it: every inbound foreign key on a registry key that holds `name`
        // (CollectionDeleteScopeByIdTest), so the registry row can be renamed in place with its content left at the old label
        su(ctx -> {
            for (var key : CATALOG_COLLECTIONS.getKeys()) {
                if (!key.getFields().contains(CATALOG_COLLECTIONS.NAME)) continue;
                for (var fk : key.getReferences()) {
                    ctx.alterTable(fk.getTable()).dropConstraintIfExists(fk.getName()).execute();
                }
            }
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
        catalog = new CatalogRepository(scope, dev.nexus.service.datamove.Gate3.never(), guard);
        chash = new ChashRepository(scope, guard);
        taxonomy = new TaxonomyRepository(scope, guard);
        telemetry = new TelemetryRepository(scope, guard);
        aspects = new AspectRepository(scope, guard);
        stores = List.of(
            new Store("chash", this::seedChash, (o, nw) -> chash.renameCollection(TENANT, o, nw), List.of("chunks")),
            new Store("taxonomy", this::seedTaxonomy, (o, nw) -> taxonomy.renameCollection(TENANT, o, nw),
                List.of("topics", "taxonomy_meta")),
            new Store("telemetry", this::seedTelemetry, (o, nw) -> telemetry.renameCollection(TENANT, o, nw),
                List.of("search_telemetry", "hook_failures")),
            new Store("aspects", this::seedAspect, (o, nw) -> aspects.renameAspectCollection(TENANT, o, nw),
                List.of("document_aspects")),
            new Store("highlights", this::seedHighlight, (o, nw) -> aspects.renameHighlightsCollection(TENANT, o, nw),
                List.of("document_highlights")));
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

    private String register(String stem) {
        String name = "lr-" + stem + "-" + n.incrementAndGet();
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("name", name);
        m.put("content_type", "knowledge");
        m.put("owner_id", "own-" + name);
        m.put("embedding_model", MODEL);
        m.put("model_version", "v1");
        catalog.upsertCollection(TENANT, m, true);
        return name;
    }

    private String document() {
        String tumbler = "9.1." + n.incrementAndGet();
        su(ctx -> ctx.insertInto(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.TENANT_ID, TENANT).set(CATALOG_DOCUMENTS.TUMBLER, tumbler)
            .set(CATALOG_DOCUMENTS.TITLE, "fixture " + tumbler).execute());
        return tumbler;
    }

    private void seedChash(String collection) {
        String hex = Chash.ofText(collection + "/chunk").toHex();
        su(ctx -> {
            PgContainerHelper.insertChunks(ctx, TENANT, collection, List.of(hex), List.of("noop text"), List.of(new float[768]),
                List.of(Map.<String, Object>of()));
            return null;
        });
    }

    private void seedTaxonomy(String collection) {
        taxonomy.insertTopic(TENANT, "noop-topic", null, collection, 1, TS, null);
        taxonomy.recordDiscoverCount(TENANT, collection, 3, TS);
    }

    private void seedTelemetry(String collection) {
        telemetry.importSearchRow(TENANT, TS, "noop-hash-" + n.incrementAndGet(), collection, 2, 1, 0.5, 0.4);
        telemetry.recordHookFailure(TENANT, "noop-doc", collection, "noop-hook", "noop error", TS, null, false, "single");
    }

    private void seedAspect(String collection) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("collection", collection);
        body.put("source_path", "noop/" + n.incrementAndGet() + ".pdf");
        body.put("problem_formulation", "p");
        body.put("proposed_method", "m");
        body.put("experimental_results", "r");
        body.put("confidence", 0.9);
        body.put("extracted_at", TS);
        body.put("model_version", "v1");
        body.put("extractor_name", "noop-extractor");
        body.put("doc_id", document());
        aspects.upsertAspect(TENANT, body);
    }

    private void seedHighlight(String collection) {
        aspects.upsertHighlight(TENANT, Map.of("doc_id", document(), "collection", collection,
            "source_uri", "file:///noop/" + n.incrementAndGet(), "highlights_md", "- a highlight", "ingested_at", TS));
    }

    /** Each row of the tenant in {@code table}: its row version ({@code xmin}) and the name column it carries, sorted. */
    private List<String> versions(String table) {
        Field<String> xmin = DSL.field(DSL.name("xmin")).cast(SQLDataType.VARCHAR);
        Field<String> collection = DSL.field(DSL.name("collection"), String.class);
        return su(ctx -> ctx.select(xmin, collection).from(DSL.table(DSL.name("nexus", table)))
            .where(DSL.field(DSL.name("tenant_id"), String.class).eq(TENANT))
            .fetch().map(r -> r.value1() + "|" + r.value2()).stream().sorted().toList());
    }

    private int rowsNamed(String table, String label) {
        return su(ctx -> ctx.fetchCount(DSL.table(DSL.name("nexus", table)),
            DSL.field(DSL.name("tenant_id"), String.class).eq(TENANT).and(DSL.field(DSL.name("collection"), String.class).eq(label))));
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

    /** P3.7's state, as far as this base can make it: the latch row, readiness complete, the registry row renamed in place. */
    private void oneRowRename(String from, String to) {
        su(ctx -> {
            CompletionCheck.insertLatch(ctx, CompletionCheck.GATE3, "test-engine", false);
            return null;
        });
        readiness.set(Missing.NONE);
        int moved = su(ctx -> ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, to)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(from)).execute());
        assertThat(moved).as("setup: the registry row took its new label").isEqualTo(1);
    }

    // ---- the one-row regime: a no-op that answers success ---------------------------------------------------------------

    @Test
    void afterAOneRowRename_aPerStoreRenameOfOldToNewChangesNothingAndAnswersZero() {
        for (Store s : stores) {
            String from = register(s.name() + "-a");
            String to = "lr-" + s.name() + "-b-" + n.incrementAndGet();
            s.seed().accept(from);
            Map<String, List<String>> before = new LinkedHashMap<>();
            for (String t : s.tables()) {
                before.put(t, versions(t));
                assertThat(before.get(t)).as("%s: non-vacuity: %s holds a row to be left alone", s.name(), t).isNotEmpty();
            }
            oneRowRename(from, to);
            long noops = RenameNoOpActivity.total();

            Object answer = s.rename().apply(from, to);
            Object again = s.rename().apply(from, to);

            assertThat(zero(answer)).as("%s: success with zero rows changed, got %s", s.name(), answer).isTrue();
            assertThat(zero(again)).as("%s: and again, got %s", s.name(), again).isTrue();
            for (String t : s.tables()) {
                assertThat(versions(t)).as("%s: every row of %s is the same row version, still naming %s", s.name(), t, from)
                    .isEqualTo(before.get(t));
                assertThat(rowsNamed(t, to)).as("%s: nothing was filed under the new label", s.name()).isZero();
            }
            assertThat(RenameNoOpActivity.total()).as("%s: each no-op is counted", s.name()).isEqualTo(noops + 2);
            beforeTheLatch();
        }
    }

    // ---- the control: before the latch the same call rewrites, as it did ------------------------------------------------

    @Test
    void beforeTheLatch_theSameCallRewritesTheNameColumnsAsToday() {
        for (Store s : stores) {
            String from = register(s.name() + "-c");
            String to = register(s.name() + "-d");   // the primitives re-point rows at a registered target, as their callers register it
            s.seed().accept(from);
            long noops = RenameNoOpActivity.total();
            for (String t : s.tables()) {
                assertThat(rowsNamed(t, from)).as("%s: non-vacuity: %s holds a row under the old label", s.name(), t).isPositive();
            }

            Object answer = s.rename().apply(from, to);

            assertThat(someChanged(answer)).as("%s: rows changed, got %s", s.name(), answer).isTrue();
            for (String t : s.tables()) {
                assertThat(rowsNamed(t, from)).as("%s: %s no longer names the old label", s.name(), t).isZero();
                assertThat(rowsNamed(t, to)).as("%s: %s names the new label", s.name(), t).isPositive();
            }
            assertThat(RenameNoOpActivity.total()).as("%s: the name path is not a no-op", s.name()).isEqualTo(noops);
        }
    }
}
