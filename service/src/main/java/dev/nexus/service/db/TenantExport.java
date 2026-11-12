// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;

/**
 * The tenant export's table registry (RDR-248 Technical Design "The table registry", bead nexus-csayy.1).
 *
 * <p>One {@link ExportTable} per row-level-security parent table ({@code relrowsecurity AND NOT
 * relispartition}). {@link #TABLES} is in load order: every table comes after each registered table it
 * refers to, by foreign key or by a reference with no foreign key. {@code TenantExportRegistryParityTest}
 * checks the registry against a Liquibase-migrated database, so a schema change that adds an RLS table, a
 * foreign key, a serial primary key, a trigger, or an id-shaped column fails that test until the entry
 * says what the export and the restore do with it.
 *
 * <p>The registry is also the export's closed table whitelist (RDR-248 gate S4): {@link #exportable}
 * answers only a table classed {@link ExportClass#DATA} or {@link ExportClass#DERIVED}.
 */
public final class TenantExport {

    private TenantExport() {
    }

    /** What the export does with a table. */
    public enum ExportClass {
        /** User data: always exported and restored. */
        DATA,
        /** Rebuildable, exported because it is cheap and saves a rebuild. */
        DERIVED,
        /** Operational state, never exported; the entry carries the reason. */
        EXCLUDED
    }

    /** A column and its type as {@code format_type} prints it ({@code bigint}, {@code text}, ...). */
    public record Column(String name, String type) {
    }

    /**
     * Columns of this table that hold a key of {@code parentSchema.parentTable}. {@code foreignKey} is true
     * when a foreign key constraint enforces it and false for a reference the schema does not enforce
     * (RDR-248: {@code nx_answer_runs.plan_id}, {@code taxonomy_centroids.topic_id}). {@code array} is true
     * for an array column whose elements are the parent's keys ({@code memory_summaries.source_ids}).
     * {@code jsonPath} is set when the key sits inside a jsonb column ({@code nx_answer_runs.run_summary}
     * at {@code run.plan_id}), dot-separated, and is null otherwise. A restore rewrites a reference only
     * when the parent's key is reassigned; whether it rewrites the array and jsonb ones or keeps them as
     * provenance is the Phase 2 design's decision (bead nexus-csayy.5).
     */
    public record Reference(List<String> columns, String parentSchema, String parentTable,
                            List<String> parentColumns, boolean foreignKey, boolean array, String jsonPath) {
        public Reference {
            columns = List.copyOf(columns);
            parentColumns = List.copyOf(parentColumns);
        }

        public String parentName() {
            return qualify(parentSchema, parentTable);
        }

        static Reference fk(List<String> columns, String parentTable, List<String> parentColumns) {
            return new Reference(columns, "nexus", parentTable, parentColumns, true, false, null);
        }

        static Reference soft(String column, String parentTable, String parentColumn) {
            return new Reference(List.of(column), "nexus", parentTable, List.of(parentColumn), false, false,
                null);
        }

        static Reference softArray(String column, String parentTable, String parentColumn) {
            return new Reference(List.of(column), "nexus", parentTable, List.of(parentColumn), false, true,
                null);
        }

        static Reference softJson(String column, String jsonPath, String parentTable, String parentColumn) {
            return new Reference(List.of(column), "nexus", parentTable, List.of(parentColumn), false, false,
                jsonPath);
        }
    }

    /**
     * One RLS parent table.
     *
     * @param reassignedId      the primary-key column a restore gives new values from the table's own
     *                          sequence (a global serial or identity key), or {@code null}
     * @param references        every column that holds another table's key, foreign key or not
     * @param triggers          every non-internal trigger on the table, by name
     * @param triggerMaintained columns a trigger rewrites, which a restore's verification skips
     * @param notReferences     id-shaped columns (bigint, bigint[], jsonb: every reassigned id is a
     *                          bigint) that hold no other row's key; with {@code reassignedId} and {@code references} this
     *                          classifies every such column (RDR-248 gate S1, census half)
     */
    public record ExportTable(String schema, String table, ExportClass exportClass, String reason,
                              List<Column> primaryKey, String reassignedId, List<Reference> references,
                              Set<String> triggers, Set<String> triggerMaintained,
                              Set<String> notReferences) {
        public ExportTable {
            primaryKey = List.copyOf(primaryKey);
            references = List.copyOf(references);
            triggers = Set.copyOf(triggers);
            triggerMaintained = Set.copyOf(triggerMaintained);
            notReferences = Set.copyOf(notReferences);
            if ((exportClass == ExportClass.EXCLUDED) == (reason == null || reason.isBlank())) {
                throw new IllegalArgumentException(qualify(schema, table)
                    + ": an excluded table needs a reason, and only an excluded table has one");
            }
        }

        public String name() {
            return qualify(schema, table);
        }

        public boolean exported() {
            return exportClass != ExportClass.EXCLUDED;
        }

        /** Every column this entry names, for the check that each still exists. */
        public Set<String> namedColumns() {
            Set<String> named = new LinkedHashSet<>();
            primaryKey.forEach(c -> named.add(c.name()));
            if (reassignedId != null) named.add(reassignedId);
            references.forEach(r -> named.addAll(r.columns()));
            named.addAll(triggerMaintained);
            named.addAll(notReferences);
            return named;
        }
    }

    /** {@code memory}, or {@code t1.scratch} for a table outside the {@code nexus} schema. */
    static String qualify(String schema, String table) {
        return "nexus".equals(schema) ? table : schema + "." + table;
    }

    /** Builds one entry; a primary-key column is written "name type". */
    private static final class Entry {
        private final String schema;
        private final String table;
        private final ExportClass exportClass;
        private final String reason;
        private final List<Column> pk = new ArrayList<>();
        private String reassigned;
        private final List<Reference> refs = new ArrayList<>();
        private final Set<String> triggers = new LinkedHashSet<>();
        private final Set<String> maintained = new LinkedHashSet<>();
        private final Set<String> notRefs = new LinkedHashSet<>();

        private Entry(String schema, String table, ExportClass exportClass, String reason) {
            this.schema = schema;
            this.table = table;
            this.exportClass = exportClass;
            this.reason = reason;
        }

        Entry pk(String... columns) {
            for (String c : columns) {
                int space = c.indexOf(' ');
                pk.add(new Column(c.substring(0, space), c.substring(space + 1)));
            }
            return this;
        }

        Entry reassigned(String column) {
            reassigned = column;
            return this;
        }

        Entry ref(Reference r) {
            refs.add(r);
            return this;
        }

        Entry fk(String columns, String parent, String parentColumns) {
            return ref(Reference.fk(List.of(columns.split(",")), parent, List.of(parentColumns.split(","))));
        }

        Entry triggers(String... names) {
            triggers.addAll(List.of(names));
            return this;
        }

        Entry maintained(String... columns) {
            maintained.addAll(List.of(columns));
            return this;
        }

        Entry notRefs(String... columns) {
            notRefs.addAll(List.of(columns));
            return this;
        }

        ExportTable build() {
            return new ExportTable(schema, table, exportClass, reason, pk, reassigned, refs, triggers,
                maintained, notRefs);
        }
    }

    private static Entry data(String table) {
        return new Entry("nexus", table, ExportClass.DATA, null);
    }

    private static Entry derived(String table) {
        return new Entry("nexus", table, ExportClass.DERIVED, null);
    }

    private static Entry excluded(String table, String reason) {
        return excluded("nexus", table, reason);
    }

    private static Entry excluded(String schema, String table, String reason) {
        return new Entry(schema, table, ExportClass.EXCLUDED, reason);
    }

    private static final String COLLECTION_KEY = "tenant_id,collection,embedding_model";
    private static final String CHUNK_KEY = "tenant_id,collection,chash,embedding_model";

    private static final String IN_FLIGHT = "in-flight work: an unfinished pipeline or job; the restored tenant "
        + "re-runs it";
    private static final String COORDINATION = "coordination state: its ids hash the tenant, and its claims "
        + "would be stale in a restore";
    private static final String OPERATIONAL = "operational log of this engine's own activity, not user data";

    /**
     * The registry, in load order (RDR-248 Technical Design; classes from the spike, T2
     * {@code nexus/rdr248-local-spike-2026-11-11}). Exported tables first, parents before children.
     */
    public static final List<ExportTable> TABLES = List.of(
        // ── catalog ──
        data("catalog_owners").pk("tenant_id text", "tumbler_prefix text")
            .notRefs("next_seq").build(),
        data("catalog_collections").pk("tenant_id text", "name text")
            .ref(new Reference(List.of("embedding_model"), "nexus", "embedding_models",
                List.of("embedding_model"), true, false, null))
            // catalog-044: owner_id is recomputed from the collection's documents on insert, and again
            // when documents load (catalog_documents_owner_to_collection).
            .triggers("catalog_collections_owner_from_docs").maintained("owner_id").build(),
        data("embedding_profile").pk("tenant_id text", "content_type text")
            .ref(new Reference(List.of("embedding_model"), "nexus", "embedding_models",
                List.of("embedding_model"), true, false, null)).build(),
        data("catalog_documents").pk("tenant_id text", "tumbler text")
            .triggers("catalog_documents_owner_to_collection").notRefs("metadata").build(),
        data("catalog_meta").pk("tenant_id text", "key text").build(),
        data("catalog_sensitivity_changes").pk("id bigint").reassigned("id").build(),
        data("chunks").pk("tenant_id text", "collection text", "chash bytea", "embedding_model text")
            .fk(COLLECTION_KEY, "catalog_collections", "tenant_id,name,embedding_model")
            .notRefs("metadata").build(),
        data("catalog_document_chunks").pk("tenant_id text", "doc_id text", "position integer")
            .fk("tenant_id,doc_id", "catalog_documents", "tenant_id,tumbler")
            .fk(CHUNK_KEY, "chunks", CHUNK_KEY)
            // The stamp triggers fire on delete and update only; the chunk_index default fires only when
            // the inserted value is null, which an exported row never is.
            .triggers("trg_catalog_chunks_stamp_on_delete", "trg_catalog_chunks_stamp_on_update",
                "trg_catalog_document_chunks_chunk_index_default").build(),
        derived("chunk_orphaned_at").pk("tenant_id text", "collection text", "chash bytea")
            .fk(CHUNK_KEY, "chunks", CHUNK_KEY).build(),
        data("catalog_links").pk("tenant_id text", "id bigint").reassigned("id")
            .fk("tenant_id,from_tumbler", "catalog_documents", "tenant_id,tumbler")
            .fk("tenant_id,to_tumbler", "catalog_documents", "tenant_id,tumbler")
            .notRefs("metadata").build(),
        data("document_aspects").pk("id bigint").reassigned("id")
            .fk("tenant_id,collection", "catalog_collections", "tenant_id,name")
            .fk("tenant_id,doc_id", "catalog_documents", "tenant_id,tumbler")
            // BEFORE UPDATE only: a load inserts, so updated_at keeps the exported value.
            .triggers("trg_document_aspects_updated_at").notRefs("extras", "salient_sentences").build(),
        data("document_highlights").pk("id bigint").reassigned("id")
            .fk("tenant_id,collection", "catalog_collections", "tenant_id,name")
            .fk("tenant_id,doc_id", "catalog_documents", "tenant_id,tumbler").build(),
        // ── T2 ──
        data("memory").pk("id bigint").reassigned("id").build(),
        data("memory_summaries").pk("id bigint").reassigned("id")
            .ref(Reference.softArray("source_ids", "memory", "id")).build(),
        data("plans").pk("id bigint").reassigned("id").notRefs("plan_json", "default_bindings").build(),
        derived("nx_answer_runs").pk("id bigint").reassigned("id")
            .ref(Reference.soft("plan_id", "plans", "id"))
            .ref(Reference.softJson("run_summary", "run.plan_id", "plans", "id"))
            .notRefs("duration_ms").build(),
        derived("nx_answer_steps").pk("run_id bigint", "step_index integer")
            .fk("run_id", "nx_answer_runs", "id").build(),
        // ── taxonomy ──
        data("topics").pk("id bigint").reassigned("id")
            .fk("tenant_id,parent_id", "topics", "tenant_id,id")
            .fk("tenant_id,collection", "catalog_collections", "tenant_id,name")
            // doc_count is kept by topic_assignments' triggers, and that update restamps updated_at.
            .triggers("trg_topics_updated_at").maintained("updated_at", "doc_count").build(),
        data("topic_assignments").pk("tenant_id text", "doc_id bytea", "topic_id bigint")
            .fk("tenant_id,topic_id", "topics", "tenant_id,id")
            .fk("tenant_id,source_collection,doc_id,embedding_model", "chunks", CHUNK_KEY)
            .fk("tenant_id,source_collection", "catalog_collections", "tenant_id,name")
            .triggers("trg_topic_assignments_doc_count_del", "trg_topic_assignments_doc_count_ins").build(),
        data("topic_links").pk("tenant_id text", "from_topic_id bigint", "to_topic_id bigint")
            .fk("tenant_id,from_topic_id", "topics", "tenant_id,id")
            .fk("tenant_id,to_topic_id", "topics", "tenant_id,id")
            .notRefs("link_types").build(),
        data("taxonomy_meta").pk("tenant_id text", "collection text")
            .fk("tenant_id,collection", "catalog_collections", "tenant_id,name").build(),
        derived("taxonomy_centroids")
            .pk("tenant_id text", "collection text", "topic_id bigint", "embedding_model text")
            .ref(Reference.soft("topic_id", "topics", "id")).build(),
        // ── telemetry and consent ──
        derived("frecency").pk("tenant_id text", "chunk_id text").build(),
        derived("search_telemetry")
            .pk("tenant_id text", "ts timestamp with time zone", "query_hash text", "collection text").build(),
        derived("relevance_log").pk("id bigint").reassigned("id").build(),
        data("claude_assisted_remediation_consents").pk("id bigint").reassigned("id").build(),
        // ── excluded ──
        excluded("pdf_pipeline", IN_FLIGHT).pk("tenant_id text", "pipeline_id bigint")
            .reassigned("pipeline_id").build(),
        excluded("pdf_pages", IN_FLIGHT).pk("tenant_id text", "pipeline_id bigint", "page_index integer")
            .fk("tenant_id,pipeline_id", "pdf_pipeline", "tenant_id,pipeline_id").build(),
        excluded("pdf_chunks", IN_FLIGHT).pk("tenant_id text", "pipeline_id bigint", "chunk_index integer")
            .fk("tenant_id,pipeline_id", "pdf_pipeline", "tenant_id,pipeline_id").build(),
        excluded("reembed_jobs", IN_FLIGHT).pk("tenant_id text", "job_id uuid").build(),
        excluded("reembed_job_moves", IN_FLIGHT).pk("tenant_id text", "job_id uuid", "doc_id text")
            .fk("tenant_id,job_id", "reembed_jobs", "tenant_id,job_id").build(),
        excluded("tuples", COORDINATION).pk("id bytea").build(),
        excluded("tuple_claim_log", COORDINATION).pk("log_id bigint").reassigned("log_id")
            .fk("tuple_id", "tuples", "id").build(),
        excluded("tuple_deliveries", COORDINATION)
            .pk("tenant_id text", "subspace text", "subscriber text", "tuple_id bytea")
            .fk("tuple_id", "tuples", "id").build(),
        excluded("t1", "scratch", "session scratch: it lives for one session").pk("id text").build(),
        excluded("agent_dispatches", OPERATIONAL).pk("id bigint").reassigned("id").build(),
        excluded("aspect_promotion_log", OPERATIONAL).pk("id bigint").reassigned("id").build(),
        excluded("canary_query_ring", OPERATIONAL)
            .pk("tenant_id text", "collection text", "vector_digest bytea")
            .fk(COLLECTION_KEY, "catalog_collections", "tenant_id,name,embedding_model").build(),
        excluded("capability_census", OPERATIONAL).pk("tenant_id text", "session_id text").build(),
        excluded("catalog_reconcile_runs", OPERATIONAL).pk("tenant_id text", "run_id text").build(),
        excluded("catalog_reconcile_rows", OPERATIONAL)
            .pk("tenant_id text", "run_id text", "file_path text").build(),
        excluded("chash_remap", OPERATIONAL)
            .pk("tenant_id text", "source_collection text", "old_id text").build(),
        excluded("gc_audit", OPERATIONAL).pk("id bigint").reassigned("id").build(),
        excluded("hook_failures", OPERATIONAL).pk("id bigint").reassigned("id").build(),
        excluded("index_failures", OPERATIONAL).pk("id bigint").reassigned("id").build(),
        excluded("ladder_completions", OPERATIONAL).pk("tenant_id text", "rung_name text").build(),
        excluded("request_ledger", OPERATIONAL).pk("tenant_id text", "request_id uuid")
            .triggers("trg_request_ledger_register_tenant").build(),
        excluded("retention_markers", OPERATIONAL).pk("tenant_id text", "relation text").build(),
        excluded("routing_events", OPERATIONAL).pk("id bigint").reassigned("id").build(),
        excluded("tier_writes", OPERATIONAL).pk("id bigint").reassigned("id").build());

    private static final Map<String, ExportTable> BY_NAME = index(TABLES);

    private static Map<String, ExportTable> index(List<ExportTable> tables) {
        Map<String, ExportTable> m = new HashMap<>();
        for (ExportTable t : tables) {
            if (m.put(t.name(), t) != null) {
                throw new IllegalStateException("TenantExport.TABLES names " + t.name() + " twice");
            }
        }
        return Map.copyOf(m);
    }

    /** The entry for {@code name}, any class. */
    public static Optional<ExportTable> entry(String name) {
        return Optional.ofNullable(BY_NAME.get(name));
    }

    /**
     * The entry for {@code name} when the export may read it ({@code data} or {@code derived}); empty for
     * an excluded table and for any name the registry does not hold (gate S4's closed whitelist).
     */
    public static Optional<ExportTable> exportable(String name) {
        return entry(name).filter(ExportTable::exported);
    }

    /**
     * Load-order violations in {@code tables}: one line per reference whose parent is a registered table
     * that does not come before the child. A table's reference to itself ({@code topics.parent_id}) is not
     * an ordering edge; the restore builds the table's id map before it inserts any row.
     */
    public static List<String> loadOrderViolations(List<ExportTable> tables) {
        Map<String, Integer> position = new HashMap<>();
        for (int i = 0; i < tables.size(); i++) position.put(tables.get(i).name(), i);
        List<String> violations = new ArrayList<>();
        for (int i = 0; i < tables.size(); i++) {
            ExportTable child = tables.get(i);
            for (Reference r : child.references()) {
                Integer parent = position.get(r.parentName());
                if (parent == null || r.parentName().equals(child.name())) continue;
                if (parent > i) {
                    violations.add(child.name() + "." + String.join(",", r.columns()) + " refers to "
                        + r.parentName() + ", which loads after it");
                }
            }
        }
        return violations;
    }
}
