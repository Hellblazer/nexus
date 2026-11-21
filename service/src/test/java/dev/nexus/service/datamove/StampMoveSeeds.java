// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import dev.nexus.service.CollectionIdSeeds;
import dev.nexus.service.P31CheckReplay;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.VectorSeeds;
import dev.nexus.service.db.CollectionIds;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.impl.DSL;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;

import static dev.nexus.service.jooq.nexus.Tables.CANARY_QUERY_RING;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;

/**
 * RDR-229 P3.1 stage 3 (nexus-p2m1x.17): the rows a stamp move finds on an upgraded install, one table at a time. A row is written as an
 * upgrade leaves it: its name set and its {@code collection_id} NULL, which the table's NOT VALID CHECK refuses on a new row, so the CHECK
 * is lifted for the insert and added back NOT VALID ({@link P31CheckReplay}, the changeset's own text, so no DDL string lives here).
 *
 * <p>Every row {@code n} of a table sits under its own collection, {@link #collection}: the tables key by it ({@code taxonomy_meta}'s primary key
 * is the collection), and a test can name a row by its collection. Rows are:
 * <ul>
 *   <li><b>old</b> rows 0..a-1: name set, id NULL; the stamp's work;</li>
 *   <li>one <b>stamped</b> row: name and id set, as the dual write leaves a new row; the stamp must not write it;</li>
 *   <li>one <b>empty</b> row, on the tables whose name has no registry foreign key: name {@code ''}, id NULL, which the CHECK treats as unset;</li>
 *   <li>one <b>orphan</b> row, on the same tables: a name no registry row carries.</li>
 * </ul>
 * Parents the table's own foreign keys need (chunks, topics, documents) are written whole, id included.
 */
final class StampMoveSeeds {

    private StampMoveSeeds() {}

    static final String BGE = "bge-base-en-v15-768";
    private static final OffsetDateTime TS = OffsetDateTime.parse("2026-11-12T10:15:30.123456Z");

    /** The changeset of vectors-068 that adds each table's CHECKs, which {@link P31CheckReplay} lifts and restores. */
    static final Map<String, String> CHECK_CHANGESET = Map.ofEntries(
        Map.entry("catalog_collections", P31CheckReplay.COLLECTIONS_SUPERSEDED),
        Map.entry("chunks", "vectors-068-3"),
        Map.entry("taxonomy_centroids", "vectors-068-4"),
        Map.entry("catalog_document_chunks", P31CheckReplay.MANIFEST),
        Map.entry("chunk_orphaned_at", "vectors-068-6"),
        Map.entry("topic_assignments", "vectors-068-7"),
        Map.entry("document_aspects", P31CheckReplay.DOCUMENT_ASPECTS),
        Map.entry("topics", "vectors-068-9"),
        Map.entry("taxonomy_meta", "vectors-068-10"),
        Map.entry("document_highlights", P31CheckReplay.DOCUMENT_HIGHLIGHTS),
        Map.entry("canary_query_ring", "vectors-068-12"),
        Map.entry("catalog_documents", P31CheckReplay.CATALOG_DOCUMENTS),
        Map.entry("chash_remap", "vectors-068-14"),
        Map.entry("pdf_pipeline", "vectors-068-15"),
        Map.entry("reembed_jobs", "vectors-068-16"));

    /** Tables whose name column is no foreign key to the registry: a row there can hold {@code ''} or a name nothing registers. */
    static final List<String> FREE_NAME_TABLES = List.of("catalog_documents", "chash_remap", "pdf_pipeline", "reembed_jobs");

    /** Tables every row of which needs a chunk row under the same collection (a foreign key into nexus.chunks). */
    /** Tables with a foreign key to a catalog document by {@code doc_id}. */
    private static final List<String> NEEDS_DOCUMENT = List.of("catalog_document_chunks", "document_aspects", "document_highlights");

    private static final List<String> NEEDS_CHUNK = List.of("catalog_document_chunks", "chunk_orphaned_at", "topic_assignments");

    // ── names, ids, cleanup ────────────────────────────────────────────────────────────────────────────────

    /** The collection of row {@code n} of {@code tenant}: one per row, in the shape the engine registers. */
    static String collection(String tenant, int n) {
        return "knowledge__" + tenant.replace("-", "") + "x" + n + "__" + BGE + "__v1";
    }

    /** The second name column of a table that has two ({@code chash_remap}, {@code reembed_jobs}): the row's target. */
    static String target(String tenant, int n) {
        return collection(tenant, n).replace("__v1", "t__v1");
    }

    static String orphan(String tenant, int n) {
        return "orphan-" + tenant + "-" + n;
    }

    static Long id(DSLContext ctx, String tenant, String name) {
        return CollectionIds.require(ctx, tenant, name);
    }

    /** What a seeded table holds: the old rows' keys (the row's first name), the stamped row's, the empty row's and the orphan row's, null where not seeded. */
    record Seeded(List<String> old, String stamped, String empty, String orphan) {}

    /** What to seed: {@code old} old rows, plus the optional rows. */
    record Plan(int old, boolean stamped, boolean empty, boolean orphan) {
        static Plan clean(int old) {
            return new Plan(old, true, false, false);
        }
    }

    private static boolean twoNames(String table) {
        return table.equals("chash_remap") || table.equals("reembed_jobs");
    }

    /** Removes everything a scenario seeded for {@code tenant}, children before parents. */
    static void clean(DSLContext ctx, String tenant) {
        for (String t : List.of("topic_assignments", "chunk_orphaned_at", "catalog_document_chunks", "taxonomy_centroids", "topics",
                "taxonomy_meta", "document_aspects", "document_highlights", "canary_query_ring", "chunks", "catalog_documents",
                "chash_remap", "pdf_pipeline", "reembed_jobs", "catalog_collections")) {
            ctx.deleteFrom(DSL.table(DSL.name("nexus", t))).where(DSL.field(DSL.name("tenant_id"), String.class).eq(tenant)).execute();
        }
    }

    // ── seeding ─────────────────────────────────────────────────────────────────────────────────────────────

    static Seeded seed(DSLContext ctx, String table, String tenant, Plan plan) {
        int stampedN = plan.old();
        int emptyN = plan.old() + 1;
        int orphanN = plan.old() + 2;
        boolean free = FREE_NAME_TABLES.contains(table);
        if (plan.empty() || plan.orphan()) {
            if (!free) throw new IllegalArgumentException(table + " has a registry foreign key on its name: no empty or orphan row");
        }
        // parents and registrations first, whole
        int registered = plan.old() + (plan.stamped() ? 1 : 0);
        for (int n = 0; n < registered; n++) {
            PgContainerHelper.insertCollection(ctx, tenant, collection(tenant, n), BGE);
            if (twoNames(table)) PgContainerHelper.insertCollection(ctx, tenant, target(tenant, n), BGE);
        }
        for (int n = 0; n < registered; n++) {
            if (NEEDS_DOCUMENT.contains(table)) {
                CollectionIdSeeds.ownerDocument(ctx, tenant, doc(n), collection(tenant, n));
            }
            if (table.equals("taxonomy_centroids") || table.equals("topic_assignments")) {
                topic(ctx, tenant, n);
            }
            if (NEEDS_CHUNK.contains(table)) {
                chunk(ctx, tenant, n);
            }
        }
        String lift = CHECK_CHANGESET.get(table);
        P31CheckReplay.lift(ctx, lift);
        try {
            for (int n = 0; n < plan.old(); n++) {
                write(ctx, table, tenant, n, collection(tenant, n), twoNames(table) ? target(tenant, n) : null, false);
            }
            if (plan.empty()) write(ctx, table, tenant, emptyN, "", twoNames(table) ? "" : null, false);
            if (plan.orphan()) {
                // chash_remap and reembed_jobs: the source resolves and the target does not (a row with any orphan name is left whole)
                if (twoNames(table)) {
                    PgContainerHelper.insertCollection(ctx, tenant, collection(tenant, orphanN), BGE);
                }
                write(ctx, table, tenant, orphanN, twoNames(table) ? collection(tenant, orphanN) : orphan(tenant, orphanN),
                    twoNames(table) ? orphan(tenant, orphanN) : null, false);
            }
        } finally {
            P31CheckReplay.restore(ctx, lift);
        }
        if (plan.stamped()) {
            write(ctx, table, tenant, stampedN, collection(tenant, stampedN), twoNames(table) ? target(tenant, stampedN) : null, true);
        }
        List<String> old = new ArrayList<>();
        for (int n = 0; n < plan.old(); n++) old.add(collection(tenant, n));
        return new Seeded(old, plan.stamped() ? collection(tenant, stampedN) : null, plan.empty() ? "" : null,
            plan.orphan() ? (twoNames(table) ? collection(tenant, orphanN) : orphan(tenant, orphanN)) : null);
    }

    private static String doc(int n) {
        return "1.9." + n;
    }

    private static byte[] chash(String tenant, int n) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(("p31-stamp-" + collection(tenant, n)).getBytes(StandardCharsets.UTF_8));
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    /** One chunk of {@code collection(tenant, n)}, whole (id included), the parent of a manifest, orphan-clock or assignment row. */
    private static void chunk(DSLContext ctx, String tenant, int n) {
        PgContainerHelper.insertChunks(ctx, tenant, collection(tenant, n), List.of(HexFormat.of().formatHex(chash(tenant, n))),
            List.of("text " + n), List.of(new float[768]), List.of(Map.of()));
    }

    /** The id of the topic row {@code n} of {@code tenant}, written whole if it is not there. */
    private static long topic(DSLContext ctx, String tenant, int n) {
        var id = DSL.field(DSL.name("id"), Long.class);
        var label = DSL.field(DSL.name("label"), String.class);
        var found = ctx.select(id).from(TOPICS).where(TOPICS.TENANT_ID.eq(tenant)).and(TOPICS.LABEL.eq("topic " + n)).fetchOne(id);
        if (found != null) return found;
        return ctx.insertInto(TOPICS).set(TOPICS.TENANT_ID, tenant).set(TOPICS.LABEL, "topic " + n)
            .set(TOPICS.COLLECTION, collection(tenant, n)).set(TOPICS.COLLECTION_ID, id(ctx, tenant, collection(tenant, n)))
            .set(TOPICS.CREATED_AT, TS).returning(TOPICS.ID).fetchOne(TOPICS.ID);
    }

    private static Field<String> s(String c) {
        return DSL.field(DSL.name(c), String.class);
    }

    private static Field<Long> l(String c) {
        return DSL.field(DSL.name(c), Long.class);
    }

    private static Field<byte[]> b(String c) {
        return DSL.field(DSL.name(c), byte[].class);
    }

    private static Field<OffsetDateTime> t(String c) {
        return DSL.field(DSL.name(c), OffsetDateTime.class);
    }

    /**
     * One row of {@code table} for row {@code n}, with {@code name} (and {@code name2} on a table that has two). {@code withIds} sets the id
     * columns from the registry (a stamped row); otherwise they are NULL, as an upgrade leaves them. A name that is {@code ''} or no
     * registry row's has no id either way.
     */
    private static void write(DSLContext ctx, String table, String tenant, int n, String name, String name2, boolean withIds) {
        Long id = withIds && !name.isEmpty() ? id(ctx, tenant, name) : null;
        Long id2 = withIds && name2 != null && !name2.isEmpty() ? id(ctx, tenant, name2) : null;
        Map<Field<?>, Object> row = new LinkedHashMap<>();
        row.put(s("tenant_id"), tenant);
        switch (table) {
            case "catalog_documents" -> {
                row.put(s("tumbler"), doc(n));
                row.put(s("title"), "doc " + n);
                row.put(s("physical_collection"), name);
                row.put(l("physical_collection_id"), id);
            }
            case "chash_remap" -> {
                row.put(s("source_collection"), name);
                row.put(l("source_collection_id"), id);
                row.put(s("old_id"), "old-" + n);
                row.put(b("new_chash"), chash(tenant, n));
                row.put(s("target_collection"), name2);
                row.put(l("target_collection_id"), id2);
                row.put(t("created_at"), TS);
                row.put(s("provenance"), "p31-test");
            }
            case "pdf_pipeline" -> {
                row.put(s("content_hash"), "h-" + n);
                row.put(s("pdf_path"), "/p/" + n + ".pdf");
                row.put(s("collection"), name);
                row.put(l("collection_id"), id);
                row.put(t("started_at"), TS);
                row.put(t("updated_at"), TS);
            }
            case "reembed_jobs" -> {
                row.put(DSL.field(DSL.name("job_id"), UUID.class), UUID.nameUUIDFromBytes(("job-" + tenant + n).getBytes(StandardCharsets.UTF_8)));
                row.put(s("source_collection"), name);
                row.put(l("source_collection_id"), id);
                row.put(s("target_collection"), name2);
                row.put(l("target_collection_id"), id2);
                row.put(s("state"), "planned");
                row.put(t("started_at"), TS);
                row.put(t("updated_at"), TS);
            }
            case "canary_query_ring" -> {
                row.put(s("collection"), name);
                row.put(l("collection_id"), id);
                row.put(s("embedding_model"), BGE);
                row.put(CANARY_QUERY_RING.EMBEDDING_768, VectorSeeds.vec(768, 0.25f));
                row.put(b("vector_digest"), chash(tenant, n));
                row.put(t("added_at"), TS);
            }
            case "document_aspects" -> {
                row.put(s("collection"), name);
                row.put(l("collection_id"), id);
                row.put(s("source_path"), "p" + n);
                row.put(t("extracted_at"), TS);
                row.put(s("model_version"), "m");
                row.put(s("extractor_name"), "e");
                row.put(s("source_uri"), "u" + n);
                row.put(s("doc_id"), doc(n));
            }
            case "document_highlights" -> {
                row.put(s("doc_id"), doc(n));
                row.put(s("source_uri"), "u" + n);
                row.put(s("collection"), name);
                row.put(l("collection_id"), id);
                row.put(t("ingested_at"), TS);
            }
            case "taxonomy_meta" -> {
                row.put(s("collection"), name);
                row.put(l("collection_id"), id);
            }
            case "topics" -> {
                row.put(s("label"), "label " + n);
                row.put(s("collection"), name);
                row.put(l("collection_id"), id);
                row.put(t("created_at"), TS);
            }
            case "chunk_orphaned_at" -> {
                row.put(s("collection"), name);
                row.put(l("collection_id"), id);
                row.put(b("chash"), chash(tenant, n));
                row.put(t("orphaned_at"), TS);
                row.put(s("embedding_model"), BGE);
            }
            case "taxonomy_centroids" -> {
                row.put(s("collection"), name);
                row.put(l("collection_id"), id);
                row.put(l("topic_id"), topic(ctx, tenant, n));
                row.put(s("label"), "centroid " + n);
                row.put(s("embedding_model"), BGE);
                row.put(VectorSeeds.centroidEmbedding(768), VectorSeeds.vec(768, 0.5f));
            }
            case "catalog_document_chunks" -> {
                row.put(s("doc_id"), doc(n));
                row.put(DSL.field(DSL.name("position"), Integer.class), n);
                row.put(b("chash"), chash(tenant, n));
                row.put(DSL.field(DSL.name("chunk_index"), Integer.class), n);
                row.put(s("collection"), name);
                row.put(l("collection_id"), id);
                row.put(s("embedding_model"), BGE);
            }
            case "topic_assignments" -> {
                row.put(b("doc_id"), chash(tenant, n));
                row.put(l("topic_id"), topic(ctx, tenant, n));
                row.put(s("source_collection"), name);
                row.put(l("source_collection_id"), id);
                row.put(s("embedding_model"), BGE);
            }
            case "chunks" -> {
                row.put(s("collection"), name);
                row.put(l("collection_id"), id);
                row.put(b("chash"), chash(tenant, n));
                row.put(s("embedding_model"), BGE);
                row.put(s("chunk_text"), "text " + n);
                row.put(VectorSeeds.chunkEmbedding(768), VectorSeeds.vec(768, 0.125f));
            }
            default -> throw new IllegalArgumentException("no seed for " + table);
        }
        ctx.insertInto(DSL.table(DSL.name("nexus", table))).set(row).execute();
    }

    // ── what a table holds now ──────────────────────────────────────────────────────────────────────────────

    /** One row as the test reads it: the first name column's value (the row's key), its id columns by name and the transaction that last wrote it. */
    record RowState(String key, Map<String, Long> ids, String xmin) {}

    /** The rows of {@code tenant} in {@code table}, by key. */
    static Map<String, RowState> rows(DSLContext ctx, String table, String tenant) {
        List<String> names = StampMoveSpecs.nameColumns(table);
        List<String> idCols = StampMoveSpecs.idColumns(table);
        List<Field<?>> select = new ArrayList<>();
        select.add(s(names.get(0)));
        for (String c : idCols) select.add(l(c));
        Field<String> xmin = DSL.field(DSL.name("xmin")).cast(org.jooq.impl.SQLDataType.VARCHAR);
        select.add(xmin);
        Map<String, RowState> out = new LinkedHashMap<>();
        for (var r : ctx.select(select).from(DSL.table(DSL.name("nexus", table))).where(s("tenant_id").eq(tenant)).fetch()) {
            Map<String, Long> ids = new LinkedHashMap<>();
            for (int i = 0; i < idCols.size(); i++) ids.put(idCols.get(i), (Long) r.get(i + 1));
            String key = (String) r.get(0);
            out.put(key == null ? "" : key, new RowState(key, ids, (String) r.get(select.size() - 1)));
        }
        return out;
    }
}
