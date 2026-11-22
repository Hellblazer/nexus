// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import dev.nexus.service.BootWorkRegistry;
import dev.nexus.service.IdKeyIndexProbe;
import dev.nexus.service.PgCatalogProbes;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.VectorSeeds;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.DrivingTableIsolationCheck;
import dev.nexus.service.vectors.LeafIndexPass;
import dev.nexus.service.vectors.Rdr229SwapMove;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.Record;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.time.Clock;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;

/**
 * RDR-229 P3.3c (nexus-p2m1x.21): the fixtures the swap-move tests share: the seeded scenario tenants, the swap rows in the state P3.3b's pass leaves,
 * the production worker, and the catalog probes that find a key by what it keys and references. See {@link Rdr229SwapMovesIntegrationTest}.
 */
abstract class Rdr229SwapMovesFixtures extends DataMoveIntegrationBase {

    static final String BGE = StampMoveSeeds.BGE;
    static final String CHUNKS_SWAP = LeafIndexPass.swapLabel("chunks");
    static final String CENTROIDS_SWAP = LeafIndexPass.swapLabel("taxonomy_centroids");
    static final String ENGINE_VERSION = "9.9.9-p33c-test";

    /** The three child tables of chunks, and the id column each carries for the chunk's collection (vectors-068). */
    static final Map<String, String> CHILD_ID_COLUMN = Map.of(
        "catalog_document_chunks", "collection_id",
        "chunk_orphaned_at", "collection_id",
        "topic_assignments", "source_collection_id");

    /** What each child's id key keys, then what it references on chunks (the RDR's item 6 shapes). */
    static final Map<String, String> CHILD_ID_KEY = Map.of(
        "catalog_document_chunks", "FOREIGN KEY (tenant_id, collection_id, chash, embedding_model)",
        "chunk_orphaned_at", "FOREIGN KEY (tenant_id, collection_id, chash, embedding_model)",
        "topic_assignments", "FOREIGN KEY (tenant_id, source_collection_id, doc_id, embedding_model)");
    static final String CHUNKS_ID_KEY_TARGET = "REFERENCES chunks(tenant_id, collection_id, chash, embedding_model)";
    static final String COMPOSITE_ON_ID =
        "FOREIGN KEY (tenant_id, collection_id, embedding_model) REFERENCES catalog_collections(tenant_id, collection_id, embedding_model)";

    static final Field<String> LABEL_F = DSL.field(DSL.name("label"), String.class);
    static final Field<String> TENANT_F = DSL.field(DSL.name("tenant_id"), String.class);

    /** The stamp moves' rows as the walk wrote them, by table: captured to assert the walk registered them (P3.1), the precondition of every swap. */
    static final Map<String, Map<String, Object>> stampRows = new LinkedHashMap<>();

    final List<String> tenantsUsed = new ArrayList<>();

    /** The tenants of one scenario: one per child table (each seeds a chunk of its own) and one for the centroids. */
    record Tenants(String catalogDocumentChunks, String chunkOrphanedAt, String topicAssignments, String centroids) {
        List<String> all() {
            return List.of(catalogDocumentChunks, chunkOrphanedAt, topicAssignments, centroids);
        }

        String of(String childTable) {
            return switch (childTable) {
                case "catalog_document_chunks" -> catalogDocumentChunks;
                case "chunk_orphaned_at" -> chunkOrphanedAt;
                case "topic_assignments" -> topicAssignments;
                default -> throw new IllegalArgumentException(childTable);
            };
        }
    }

    @BeforeAll
    void captureStampRows() {
        for (String table : List.of("chunks", "taxonomy_centroids")) {
            Record row = ctx().selectFrom(table("nexus", "data_moves")).where(LABEL.eq(StampLabels.labelFor(table))).fetchOne();
            assertThat(row).as("the walk registers the stamp move of " + table + " (P3.1)").isNotNull();
            stampRows.put(table, new LinkedHashMap<>(row.intoMap()));
        }
    }

    @BeforeEach
    void startFromNoIdKeys() {
        dropIdKeys();
    }

    @AfterEach
    void removeScenario() {
        dropIdKeys();
        for (String t : tenantsUsed) {
            StampMoveSeeds.clean(ctx(), t);
        }
        tenantsUsed.clear();
    }

    // ── fixtures ────────────────────────────────────────────────────────────────────────────────────────────

    Tenants seed(String tag) {
        var t = new Tenants("p33c-" + tag + "-cdc", "p33c-" + tag + "-orph", "p33c-" + tag + "-ta", "p33c-" + tag + "-cen");
        tenantsUsed.addAll(t.all());
        StampMoveSeeds.seed(ctx(), "catalog_document_chunks", t.catalogDocumentChunks(), StampMoveSeeds.Plan.clean(0));
        StampMoveSeeds.seed(ctx(), "chunk_orphaned_at", t.chunkOrphanedAt(), StampMoveSeeds.Plan.clean(0));
        StampMoveSeeds.seed(ctx(), "topic_assignments", t.topicAssignments(), StampMoveSeeds.Plan.clean(0));
        StampMoveSeeds.seed(ctx(), "taxonomy_centroids", t.centroids(), StampMoveSeeds.Plan.clean(0));
        return t;
    }

    /** Every foreign key this scenario's swap adds, and the id unique keys, gone: the state before P3.3b. */
    void dropIdKeys() {
        for (String child : List.of("catalog_document_chunks", "chunk_orphaned_at", "topic_assignments")) {
            for (Fk fk : foreignKeys(child)) {
                if (fk.isIdTwinOnChunks()) {
                    ctx().alterTable(table("nexus", child)).dropConstraint(DSL.name(fk.name())).execute();
                }
            }
        }
        for (Fk fk : foreignKeys("chunks")) {
            if (fk.def().startsWith(COMPOSITE_ON_ID)) {
                ctx().alterTable(table("nexus", "chunks")).dropConstraint(DSL.name(fk.name())).execute();
            }
        }
        IdKeyIndexProbe.reset(ctx());
    }

    /**
     * What P3.3b leaves: the id unique keys, and each table's swap row. The pass registers them; where it does not (it is not built
     * on this branch), the keys are added here by the PostgreSQL route that recurses through the partitions (the same end state: a valid
     * unique constraint on the parent and one attached on every leaf), and the row is built from {@link Rdr229SwapMove#spec} plus the
     * pass's identity fields: real rows in the state the pass leaves, not a mock.
     */
    void registerSwaps() {
        new LeafIndexPass(adminConnection, new BootWorkRegistry(Clock.systemUTC()), () -> true, ENGINE_VERSION).runOnce();
        for (var key : IdKeyIndexProbe.KEYS) {
            if (IdKeyIndexProbe.indexes(ctx(), key).isEmpty()) {
                var columns = List.of(key.columns().split(",\\s*"));
                ctx().alterTable(table("nexus", key.table()))
                    .add(DSL.constraint(DSL.name("p33c_test_" + key.table() + "_id_uq"))
                        .unique(columns.stream().map(c -> DSL.name(c)).toArray(org.jooq.Name[]::new)))
                    .execute();
            }
            String label = LeafIndexPass.swapLabel(key.table());
            if (!ctx().fetchExists(table("nexus", "data_moves"), LABEL.eq(label))) {
                // the row in the state the pass leaves: its identity fields are the pass's (LeafIndexPass#registerSwap), the rest is P3.3c's
                Rdr229SwapMove.Spec spec = Rdr229SwapMove.spec(key.table());
                Map<String, Object> row = new LinkedHashMap<>();
                row.put("label", label);
                row.put("driving_schema", "nexus");
                row.put("driving_table", key.table());
                row.put("changeset_id", LeafIndexPass.CHANGESET_PREFIX + ENGINE_VERSION);
                row.put("supersedes", null);
                row.put("template", spec.template());
                row.put("chunk_size", spec.chunkSize());
                row.put("sleep_ms", spec.sleepMs());
                row.put("statement_timeout_ms", spec.statementTimeoutMs());
                row.put("verify_statement_timeout_ms", spec.verifyStatementTimeoutMs());
                row.put("verification", spec.verification());
                row.put("on_complete", spec.onComplete());
                List<Field<?>> columns = new ArrayList<>();
                List<Object> values = new ArrayList<>();
                row.forEach((k, v) -> {
                    columns.add(DSL.field(DSL.name(k)));
                    values.add(v);
                });
                ctx().insertInto(table("nexus", "data_moves")).columns(columns).values(values).execute();
            }
        }
    }

    DataMoveWorker productionWorker(VerifyingFinisher finisher) {
        return worker(installed(), 0, new DataMoveFailureHandler(adminConnection, WORKER_NAME, Clock.systemUTC()), finisher);
    }

    /** Cycles until both swaps are completed or failed or three cycles have run (the validation moves registered along the way are worked too). */
    void runSwaps(VerifyingFinisher finisher) {
        var w = productionWorker(finisher);
        for (int i = 0; i < 3; i++) {
            w.runCycle();
            if (swapSettled(CHUNKS_SWAP) && swapSettled(CENTROIDS_SWAP)) {
                return;
            }
        }
    }

    boolean swapSettled(String label) {
        return ctx().fetchExists(table("nexus", "data_moves"),
            LABEL.eq(label).and(COMPLETED_AT.isNotNull().or(DSL.field(DSL.name("failed_at")).isNotNull())));
    }

    List<Map<String, Object>> moves(Condition where) {
        return ctx().selectFrom(table("nexus", "data_moves")).where(where).orderBy(LABEL_F).fetch().intoMaps();
    }

    /** Every move except the two swaps: what the swap's on_complete registered. */
    List<Map<String, Object>> registeredBySwap() {
        return moves(LABEL_F.ne(CHUNKS_SWAP).and(LABEL_F.ne(CENTROIDS_SWAP)));
    }

    // ── the catalog, by what a key keys and references ─────────────────────────────────────────────────────

    /** A top-level foreign key (or other constraint): {@code def} is {@code pg_get_constraintdef} with the schema qualifier removed. */
    record Fk(String name, String def, boolean validated, String onUpdate, String onDelete, boolean deferrable,
                      boolean deferred) {
        boolean isIdTwinOnChunks() {
            return def.contains(CHUNKS_ID_KEY_TARGET);
        }
    }

    List<Fk> constraints(String tableName, String contype) {
        var c = DSL.table(DSL.name("pg_catalog", "pg_constraint")).as("c");
        var r = DSL.table(DSL.name("pg_catalog", "pg_class")).as("r");
        var n = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
        Field<String> def = DSL.function(DSL.name("pg_catalog", "pg_get_constraintdef"), String.class, DSL.field(DSL.name("c", "oid")));
        return ctx().select(DSL.field(DSL.name("c", "conname"), String.class), def,
                DSL.field(DSL.name("c", "convalidated"), Boolean.class), DSL.field(DSL.name("c", "confupdtype"), String.class),
                DSL.field(DSL.name("c", "confdeltype"), String.class),
                DSL.field(DSL.name("c", "condeferrable"), Boolean.class), DSL.field(DSL.name("c", "condeferred"), Boolean.class))
            .from(c)
            .join(r).on(DSL.field(DSL.name("r", "oid")).eq(DSL.field(DSL.name("c", "conrelid"))))
            .join(n).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("r", "relnamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
            .and(DSL.field(DSL.name("r", "relname"), String.class).eq(tableName))
            .and(DSL.field(DSL.name("c", "contype"), String.class).eq(contype))
            .and(DSL.field(DSL.name("c", "conparentid"), Long.class).eq(0L))
            .fetch(rec -> new Fk(rec.value1(), rec.value2().replace("nexus.", ""), rec.value3(), rec.value4(), rec.value5(), rec.value6(),
                rec.value7()));
    }

    List<Fk> foreignKeys(String tableName) {
        return constraints(tableName, "f");
    }

    /** The id twin of {@code child}'s name key into chunks: keyed on the id columns, referencing the chunks id key. */
    List<Fk> idTwins(String child) {
        return foreignKeys(child).stream()
            .filter(fk -> fk.def().startsWith(CHILD_ID_KEY.get(child) + " " + CHUNKS_ID_KEY_TARGET)).toList();
    }

    List<Fk> compositeOnId() {
        return foreignKeys("chunks").stream().filter(fk -> fk.def().startsWith(COMPOSITE_ON_ID)).toList();
    }

    String primaryKeyDef(String table) {
        var pks = constraints(table, "p");
        assertThat(pks).as("the name primary key of nexus." + table).hasSize(1);
        return pks.get(0).def();
    }

    static final List<String> NAME_CHILD_KEYS = List.of("fk_catalog_chunks_chunk", "topic_assignments_chunk_fk",
        "chunk_orphaned_at_chunk_fk");

    void assertNameKeysStay() {
        assertThat(primaryKeyDef("chunks")).as("the name primary key of chunks stays")
            .isEqualTo("PRIMARY KEY (tenant_id, collection, chash, embedding_model)");
        assertThat(primaryKeyDef("taxonomy_centroids")).as("the name primary key of the centroids stays")
            .isEqualTo("PRIMARY KEY (tenant_id, collection, topic_id, embedding_model)");
        for (String name : NAME_CHILD_KEYS) {
            assertThat(PgCatalogProbes.foreignKey(ctx(), "nexus", name)).as("the name child key " + name + " stays").isNotNull();
        }
        assertThat(PgCatalogProbes.foreignKey(ctx(), "nexus", "chunks_collection_fk"))
            .as("chunks_collection_fk stays until P3.7's name-key drop move").isNotNull();
    }
}
