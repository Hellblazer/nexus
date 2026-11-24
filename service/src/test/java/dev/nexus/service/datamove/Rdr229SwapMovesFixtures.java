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
import dev.nexus.service.vectors.Rdr229ChunksFk;
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
        // the per-leaf keys the swap adds to the leaves of chunks (nexus-p2m1x.21), named <leaf>_cid_fk and not adopted by a parent key
        for (LeafFk f : leafFks()) {
            if (f.name().endsWith(Rdr229ChunksFk.LEAF_CONSTRAINT_SUFFIX)) {
                ctx().alterTable(table("nexus", f.leaf())).dropConstraintIfExists(DSL.name(f.name())).execute();
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

    /**
     * Every move except the two swaps, the per-leaf validation moves and the adoption move: the three child validation moves the swap's
     * on_complete registered. The per-leaf route's moves (nexus-p2m1x.21) are {@link #leafMoves} and {@link #adoptMoves}.
     */
    List<Map<String, Object>> registeredBySwap() {
        return moves(LABEL_F.ne(CHUNKS_SWAP).and(LABEL_F.ne(CENTROIDS_SWAP)).and(LABEL_F.notLike(Rdr229ChunksFk.LEAF_LABEL_PREFIX + "%"))
            .and(LABEL_F.ne(Rdr229ChunksFk.ADOPT_LABEL)));
    }

    /** One validation move per chunks leaf that had no validated key when the swap (or the adoption move) looked. */
    List<Map<String, Object>> leafMoves() {
        return moves(LABEL_F.like(Rdr229ChunksFk.LEAF_LABEL_PREFIX + "%"));
    }

    List<Map<String, Object>> adoptMoves() {
        return moves(LABEL_F.eq(Rdr229ChunksFk.ADOPT_LABEL));
    }

    /**
     * The relation name of a {@code schema.name} the probes return, as {@code regclass} prints it: without the schema, and
     * unquoted when the name needed quotes (a mixed-case or punctuated leaf).
     */
    static String bare(String qualified) {
        String n = qualified.startsWith("nexus.") ? qualified.substring("nexus.".length()) : qualified;
        return n.startsWith("\"") && n.endsWith("\"") ? n.substring(1, n.length() - 1).replace("\"\"", "\"") : n;
    }

    /** The relation names of the leaves of nexus.chunks, sorted. */
    List<String> chunkLeaves() {
        return IdKeyIndexProbe.leaves(ctx(), IdKeyIndexProbe.CHUNKS).stream().map(Rdr229SwapMovesFixtures::bare).toList();
    }

    /** The relation names of the model partitions of nexus.chunks, between the parent and its leaves, sorted. */
    List<String> chunkModelPartitions() {
        return IdKeyIndexProbe.modelPartitions(ctx(), IdKeyIndexProbe.CHUNKS).stream().map(Rdr229SwapMovesFixtures::bare).toList();
    }

    /** The foreign keys of the leaves of chunks to the registry: leaf, name, validated, conparentid (0 when no parent key adopted it). */
    record LeafFk(String leaf, String name, boolean validated, long parentId) { }

    List<LeafFk> leafFks() {
        return compositeKeysOn(chunkLeaves());
    }

    /** The foreign keys of the named nexus relations to the registry that are keyed on the composite's three columns, whatever their actions. */
    List<LeafFk> compositeKeysOn(java.util.Collection<String> relations) {
        var k = DSL.table(DSL.name("pg_catalog", "pg_constraint")).as("k");
        var c = DSL.table(DSL.name("pg_catalog", "pg_class")).as("c");
        var n = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
        var r = DSL.table(DSL.name("pg_catalog", "pg_class")).as("r");
        Field<String> relname = DSL.field(DSL.name("c", "relname"), String.class);
        Field<String> conname = DSL.field(DSL.name("k", "conname"), String.class);
        Field<Boolean> validated = DSL.field(DSL.name("k", "convalidated"), Boolean.class);
        Field<Long> parent = DSL.field(DSL.name("k", "conparentid"), Long.class);
        Field<String> def = DSL.function(DSL.name("pg_catalog", "pg_get_constraintdef"), String.class, DSL.field(DSL.name("k", "oid")));
        return ctx().select(relname, conname, validated, parent, def).from(k)
            .join(c).on(DSL.field(DSL.name("c", "oid")).eq(DSL.field(DSL.name("k", "conrelid"))))
            .join(n).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
            .join(r).on(DSL.field(DSL.name("r", "oid")).eq(DSL.field(DSL.name("k", "confrelid"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
            .and(relname.in(relations))
            .and(DSL.field(DSL.name("k", "contype"), String.class).eq("f"))
            .and(DSL.field(DSL.name("r", "relname"), String.class).eq("catalog_collections"))
            .orderBy(relname, conname).fetch().stream()
            .filter(rec -> rec.value5().replace("nexus.", "").startsWith(COMPOSITE_ON_ID))
            .map(rec -> new LeafFk(rec.value1(), rec.value2(), rec.value3(), rec.value4())).toList();
    }

    void pause(String labelLike) {
        ctx().update(table("nexus", "data_moves")).set(DSL.field(DSL.name("paused_at"), OffsetDateTime.class), OffsetDateTime.now())
            .set(DSL.field(DSL.name("paused_by"), String.class), "p33c-test").where(LABEL_F.like(labelLike)).execute();
    }

    void resume(String labelLike) {
        ctx().update(table("nexus", "data_moves")).set(DSL.field(DSL.name("paused_at"), OffsetDateTime.class), (OffsetDateTime) null)
            .set(DSL.field(DSL.name("paused_by"), String.class), (String) null).where(LABEL_F.like(labelLike)).execute();
    }

    /** The on_complete text of a registered move, as the registry holds it. */
    String onCompleteOf(String label) {
        return ctx().select(DSL.field(DSL.name("on_complete"), String.class)).from(table("nexus", "data_moves")).where(LABEL_F.eq(label))
            .fetchOne(0, String.class);
    }

    /** Runs {@code sql} (DDL, SET, LOCK: no typed jOOQ form) on a session of its own through the engine's one sanctioned sink for SQL text. */
    void runSql(String sql) throws SQLException {
        try (Connection c = pg.createConnection("")) {
            MoveSql.runOnComplete(c, sql);
        }
    }

    /**
     * The swaps, then cycles until the composite exists (the leaf validation moves and the adoption move the swap registered are worked
     * along the way, each a worker cycle apart from the swap that registers them), at most twelve.
     */
    void runUntilComposite(VerifyingFinisher finisher) {
        var w = productionWorker(finisher);
        for (int i = 0; i < 12; i++) {
            w.runCycle();
            if (!compositeOnId().isEmpty()) {
                return;
            }
        }
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
