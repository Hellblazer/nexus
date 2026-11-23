// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TupleRepository;
import dev.nexus.service.tuples.TemplateRegistry;
import org.jooq.DSLContext;
import org.jooq.impl.DSL;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.TUPLES;

/** Reads the rename tests share: registry ids, per-table row snapshots, the per-collection index, the name keys, queued aspect tasks. */
final class Rdr229RenameChecks {

    /** The seven registry name foreign keys the name-key drop move drops (RDR-229 item 6, Switch). */
    static final List<String> NAME_KEYS = List.of("chunks_collection_fk", "canary_query_ring_collection_fk",
        "document_aspects_collection_fk", "topics_collection_fk", "taxonomy_meta_collection_fk",
        "document_highlights_collection_fk", "topic_assignments_collection_fk");

    /** table, name column, id column: the tables a one-row rename must leave alone. */
    record Site(String table, String nameColumn, String idColumn) { }

    static final List<Site> CHILD_SITES = List.of(
        new Site("chunks", "collection", "collection_id"),
        new Site("catalog_document_chunks", "collection", "collection_id"),
        new Site("topics", "collection", "collection_id"),
        new Site("taxonomy_meta", "collection", "collection_id"),
        new Site("taxonomy_centroids", "collection", "collection_id"),
        new Site("topic_assignments", "source_collection", "source_collection_id"),
        new Site("document_aspects", "collection", "collection_id"),
        new Site("document_highlights", "collection", "collection_id"),
        new Site("canary_query_ring", "collection", "collection_id"));

    private Rdr229RenameChecks() { /* static */ }

    /** The id of the registry row {@code tenant}/{@code name}, or null. */
    static Long registryId(DSLContext ctx, String tenant, String name) {
        return ctx.select(DSL.field(DSL.name("collection_id"), Long.class)).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(name))
            .fetchOne(0, Long.class);
    }

    /** One line per row of {@code site.table} that names {@code name} in {@code tenant}: name, id and xmin (changes when the row is rewritten). Sorted. */
    static List<String> snapshot(DSLContext ctx, String tenant, Site site, String name) {
        var t = DSL.table(DSL.name("nexus", site.table()));
        var nameF = DSL.field(DSL.name(site.nameColumn()), String.class);
        var idF = DSL.field(DSL.name(site.idColumn()), Long.class);
        var xminF = DSL.field(DSL.name("xmin"), String.class);
        List<String> rows = new ArrayList<>();
        for (var r : ctx.select(nameF, idF, xminF).from(t).where(DSL.field(DSL.name("tenant_id"), String.class).eq(tenant))
                .and(nameF.eq(name)).fetch()) {
            rows.add(r.value1() + "|" + r.value2() + "|" + r.value3());
        }
        rows.sort(null);
        return rows;
    }

    /** Snapshots of every child site, by table, for the rows naming {@code name}. */
    static Map<String, List<String>> snapshots(DSLContext ctx, String tenant, String name) {
        Map<String, List<String>> out = new TreeMap<>();
        for (Site s : CHILD_SITES) {
            out.put(s.table(), snapshot(ctx, tenant, s, name));
        }
        return out;
    }

    /** How many rows of {@code table} name {@code name} in {@code nameColumn}. */
    static long naming(DSLContext ctx, String tenant, String table, String nameColumn, String name) {
        return ctx.fetchCount(DSL.table(DSL.name("nexus", table)),
            DSL.field(DSL.name("tenant_id"), String.class).eq(tenant).and(DSL.field(DSL.name(nameColumn), String.class).eq(name)));
    }

    /** The name foreign keys of {@link #NAME_KEYS} that are still on the schema. */
    static List<String> nameKeysPresent(DSLContext ctx) {
        return ctx.select(DSL.field(DSL.name("conname"), String.class)).from(DSL.table(DSL.name("pg_catalog", "pg_constraint")))
            .where(DSL.field(DSL.name("conname"), String.class).in(NAME_KEYS)).fetch(0, String.class).stream().distinct().sorted().toList();
    }

    /** The parent index {@code indexName} on {@code nexus}: its oid and definition (a rebuild changes the oid, a stranded one keeps the old predicate). Empty when absent. */
    static List<String> index(DSLContext ctx, String indexName) {
        var oid = DSL.field(DSL.name("c", "oid"), Long.class);
        var idx = DSL.table(DSL.name("pg_catalog", "pg_indexes"));
        List<String> out = new ArrayList<>();
        for (var r : ctx.select(DSL.field(DSL.name("indexname"), String.class), DSL.field(DSL.name("indexdef"), String.class))
                .from(idx).where(DSL.field(DSL.name("schemaname"), String.class).eq("nexus"))
                .and(DSL.field(DSL.name("indexname"), String.class).eq(indexName)).fetch()) {
            Long o = ctx.select(oid).from(DSL.table(DSL.name("pg_catalog", "pg_class")).as("c"))
                .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n"))
                .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
                .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
                .and(DSL.field(DSL.name("c", "relname"), String.class).eq(indexName)).fetchOne(0, Long.class);
            out.add(o + "|" + r.value2());
        }
        return out;
    }

    // ---- queued aspect tasks -----------------------------------------------------------------------------------------------

    static TupleRepository tuples(TenantScope scope) {
        return new TupleRepository(scope,
            TemplateRegistry.loadAtBoot(null, null, NexusService.SWEEP_INTERVAL_HOURS * 3600L));
    }

    static String taskBody(String collection, String docId) {
        return "{\"collection\":\"" + collection + "\",\"source_path\":\"p/" + docId + ".md\",\"doc_id\":\"" + docId
            + "\",\"content_hash\":\"h-" + docId + "\"}";
    }

    /** The collection each unconsumed {@code aspects} task of {@code tenant} names, by doc id. */
    static Map<String, String> taskCollections(DSLContext ctx, String tenant) {
        Map<String, String> out = new TreeMap<>();
        for (String body : ctx.select(TUPLES.BODY).from(TUPLES).where(TUPLES.TENANT_ID.eq(tenant))
                .and(TUPLES.SUBSPACE.eq("aspects")).and(TUPLES.CONSUMED_AT.isNull()).fetch(TUPLES.BODY)) {
            try {
                JsonNode n = Rdr229RenameEngine.MAPPER.readTree(body);
                out.put(n.path("doc_id").asText(), n.path("collection").asText());
            } catch (com.fasterxml.jackson.core.JsonProcessingException e) {
                throw new IllegalStateException("a task body that is not JSON: " + body, e);
            }
        }
        return out;
    }
}
