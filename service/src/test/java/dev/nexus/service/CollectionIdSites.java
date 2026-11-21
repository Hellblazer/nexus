// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CollectionIds;
import dev.nexus.service.db.TenantExport;
import dev.nexus.service.db.TenantExport.ExportTable;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record3;
import org.jooq.Table;
import org.jooq.impl.DSL;

import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;

/**
 * Where RDR-229 item 6 puts a {@code collection_id} beside a collection name column, as the tenant backup tests
 * (bead nexus-p2m1x.31, P3.11) need it. Every statement is typed jOOQ: the registry through its generated table, and the
 * sites, which differ by name, through {@code DSL.table(DSL.name(...))} and {@code DSL.field(DSL.name(...))}, so one loop
 * covers all twenty.
 *
 * <p>The names are P3.1's (vectors-068-collection-id-expand.xml at 354b0d356, nexus-p2m1x.17): {@code collection_id}
 * beside a {@code collection} column, and {@code physical_collection_id}, {@code source_collection_id},
 * {@code target_collection_id} beside the others. {@link Site#idCandidates} keeps a list so a rename in P3.1's final
 * rebase is a one-place edit here.
 */
public final class CollectionIdSites {

    private CollectionIdSites() {
    }

    /**
     * One name column and the id column that comes beside it.
     *
     * @param history true for a history table (RDR-229 item 6: {@code relevance_log}, {@code search_telemetry},
     *                {@code hook_failures}, {@code gc_audit}): its id is nullable, and a row keeps the label the
     *                collection had when the event happened, so its name need not equal the registry row's
     */
    public record Site(String table, String nameColumn, List<String> idCandidates, boolean history) {
        public Site {
            idCandidates = List.copyOf(idCandidates);
        }

        /** True when the tenant export reads this table (data or derived), so a backup file holds it. */
        public boolean exported() {
            return TenantExport.entry(table).map(ExportTable::exported).orElse(false);
        }
    }

    private static Site site(String table, String nameColumn, String... idCandidates) {
        return new Site(table, nameColumn, List.of(idCandidates), false);
    }

    private static Site history(String table) {
        return new Site(table, "collection", List.of("collection_id"), true);
    }

    /** Every table of RDR-229 item 6 that the tenant export registry describes, exported or excluded. */
    public static final List<Site> SITES = List.of(
        site("catalog_documents", "physical_collection", "physical_collection_id"),
        site("catalog_document_chunks", "collection", "collection_id"),
        site("chunks", "collection", "collection_id"),
        site("chunk_orphaned_at", "collection", "collection_id"),
        site("document_aspects", "collection", "collection_id"),
        site("document_highlights", "collection", "collection_id"),
        site("topics", "collection", "collection_id"),
        site("topic_assignments", "source_collection", "source_collection_id"),
        site("taxonomy_meta", "collection", "collection_id"),
        site("taxonomy_centroids", "collection", "collection_id"),
        site("canary_query_ring", "collection", "collection_id"),
        site("pdf_pipeline", "collection", "collection_id"),
        site("chash_remap", "source_collection", "source_collection_id"),
        site("chash_remap", "target_collection", "target_collection_id"),
        site("reembed_jobs", "source_collection", "source_collection_id"),
        site("reembed_jobs", "target_collection", "target_collection_id"),
        history("relevance_log"),
        history("search_telemetry"),
        history("hook_failures"),
        history("gc_audit"));

    /** The sites a backup file carries. */
    public static List<Site> exportedSites() {
        return SITES.stream().filter(Site::exported).toList();
    }

    /** The id columns a restore changes by design in {@code table}, so a contents comparison across tenants skips them. */
    public static List<String> idColumnsOf(String table) {
        List<String> out = new ArrayList<>();
        if (table.equals("catalog_collections")) out.addAll(List.of("collection_id", "superseded_by_id"));
        for (Site s : SITES) if (s.table().equals(table)) out.addAll(s.idCandidates());
        return out;
    }

    private static Table<?> table(String name) {
        return DSL.table(DSL.name("nexus", name));
    }

    private static Field<Object> column(String name) {
        return DSL.field(DSL.name(name));
    }

    /**
     * The id column {@code site} has in the live schema.
     *
     * @throws AssertionError naming the table and the candidates when none exists
     */
    public static String idColumn(DSLContext ctx, Site site) {
        for (String c : site.idCandidates()) {
            if (PgCatalogProbes.columnExists(ctx, "nexus", site.table(), c)) return c;
        }
        throw new AssertionError("nexus." + site.table() + " has no collection id column for its " + site.nameColumn()
            + " (looked for " + site.idCandidates() + ")");
    }

    /** What {@link #audit} found at one site, for one tenant. */
    public record Finding(String site, String idColumn, long examined, long nullIds, long mismatched) {
        public boolean clean() {
            return nullIds == 0 && mismatched == 0;
        }

        @Override
        public String toString() {
            return site + "." + idColumn + ": examined " + examined + ", null ids " + nullIds + ", ids that are not the registry's "
                + mismatched;
        }
    }

    /**
     * Every row of {@code tenant} at each site in {@code sites}, checked against the tenant's own registry. A row whose name
     * names a registry row must carry that row's id (null is a finding, except at a history table, whose id is nullable by
     * design); a row whose id is set must point at a registry row of the same tenant, and, for a site that is not a history
     * table, the one with the row's name. A name that is empty or names no registry row is not required to have an id (the
     * CHECK of item 6 allows it).
     */
    public static List<Finding> audit(DSLContext ctx, String tenant, List<Site> sites) {
        List<Finding> out = new ArrayList<>();
        for (Site s : sites) {
            String idName = idColumn(ctx, s);
            Table<?> t = table(s.table()).as("t");
            Field<Long> id = DSL.field(DSL.name("t", idName), Long.class);
            Field<String> name = DSL.field(DSL.name("t", s.nameColumn()), String.class);
            Field<String> rowTenant = DSL.field(DSL.name("t", "tenant_id"), String.class);
            var c = CATALOG_COLLECTIONS.as("c");
            Condition byName = DSL.exists(DSL.selectOne().from(c).where(c.TENANT_ID.eq(rowTenant)).and(c.NAME.eq(name)));
            Condition pointsRight = DSL.exists(DSL.selectOne().from(c).where(c.TENANT_ID.eq(rowTenant))
                .and(c.COLLECTION_ID.eq(id)).and(s.history() ? DSL.trueCondition() : c.NAME.eq(name)));
            Condition nullId = s.history() ? DSL.falseCondition() : id.isNull().and(byName);
            Record3<Integer, Integer, Integer> r = ctx.select(DSL.count(), DSL.count().filterWhere(nullId),
                    DSL.count().filterWhere(id.isNotNull().and(pointsRight.not())))
                .from(t).where(rowTenant.eq(tenant)).fetchOne();
            out.add(new Finding(s.table() + "." + s.nameColumn(), idName, r.value1(), r.value2(), r.value3()));
        }
        return out;
    }

    /**
     * Sites in {@code found} that examined no row. A table the test never seeded proves nothing, so a zero is a
     * test-setup failure and never a pass (RDR-229 P3.11, linda's ruling): the caller asserts this list is empty.
     */
    public static List<String> unexamined(List<Finding> found) {
        return found.stream().filter(f -> f.examined() == 0)
            .map(f -> "test-setup failure: " + f.site() + " examined 0 rows, so the test never seeded it").toList();
    }

    /** One entry per site with the rows it examined, for a failure message: a count the reader can see is never a silent zero. */
    public static String counts(List<Finding> found) {
        return found.stream().map(f -> f.site() + "=" + f.examined()).toList().toString();
    }

    private static Site siteOf(String table, String nameColumn) {
        return SITES.stream().filter(s -> s.table().equals(table) && s.nameColumn().equals(nameColumn)).findFirst().orElseThrow();
    }

    /**
     * One row in each table the backup does not carry (pdf_pipeline, chash_remap, reembed_jobs, canary_query_ring, and the
     * hook_failures and gc_audit logs), naming {@code first} and {@code second} and holding their registry ids, as an engine at
     * P3.1 writes them. The battery seed reaches none of these, so a test that audits them seeds them here; they are never
     * exported, so a restore must leave the target without them.
     */
    public static void seedUnexported(DSLContext ctx, String tenant, String first, String second) {
        Long firstId = CollectionIds.require(ctx, tenant, first), secondId = CollectionIds.require(ctx, tenant, second);
        OffsetDateTime now = OffsetDateTime.now();
        ctx.insertInto(table("pdf_pipeline"), column("tenant_id"), column("content_hash"), column("pdf_path"), column("collection"),
                column(idColumn(ctx, siteOf("pdf_pipeline", "collection"))), column("started_at"), column("updated_at"))
            .values(DSL.val(tenant), DSL.val("h1"), DSL.val("a.pdf"), DSL.val(first), DSL.val(firstId), DSL.val(now), DSL.val(now))
            .execute();
        ctx.insertInto(table("chash_remap"), column("tenant_id"), column("source_collection"), column("old_id"), column("new_chash"),
                column("target_collection"), column("created_at"), column("provenance"),
                column(idColumn(ctx, siteOf("chash_remap", "source_collection"))),
                column(idColumn(ctx, siteOf("chash_remap", "target_collection"))))
            .values(DSL.val(tenant), DSL.val(first), DSL.val("old1"), DSL.val(new byte[32]), DSL.val(second), DSL.val(now), DSL.val("p"),
                DSL.val(firstId), DSL.val(secondId))
            .execute();
        ctx.insertInto(table("reembed_jobs"), column("tenant_id"), column("job_id"), column("source_collection"),
                column("target_collection"), column("state"), column("started_at"), column("updated_at"),
                column(idColumn(ctx, siteOf("reembed_jobs", "source_collection"))),
                column(idColumn(ctx, siteOf("reembed_jobs", "target_collection"))))
            .values(DSL.val(tenant), DSL.val(UUID.randomUUID()), DSL.val(first), DSL.val(second), DSL.val("planned"), DSL.val(now),
                DSL.val(now), DSL.val(firstId), DSL.val(secondId))
            .execute();
        Field<dev.nexus.service.jooq.binding.Vector> embedding768 =
            DSL.field(DSL.name("embedding_768"), VectorSeeds.chunkEmbedding(768).getDataType());
        byte[] digest = new byte[32];
        digest[0] = 1;
        ctx.insertInto(table("canary_query_ring"))
            .set(column("tenant_id"), tenant)
            .set(column("collection"), first)
            .set(column(idColumn(ctx, siteOf("canary_query_ring", "collection"))), firstId)
            .set(column("embedding_model"), ctx.select(CATALOG_COLLECTIONS.EMBEDDING_MODEL).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(first))
                .fetchOne(CATALOG_COLLECTIONS.EMBEDDING_MODEL))
            .set(embedding768, TenantExportBatteryTest.Seed.vector(768, 1))
            .set(column("vector_digest"), digest)
            .set(column("added_at"), now)
            .execute();
        ctx.insertInto(table("hook_failures"), column("tenant_id"), column("collection"),
                column(idColumn(ctx, siteOf("hook_failures", "collection"))), column("hook_name"))
            .values(DSL.val(tenant), DSL.val(first), DSL.val(firstId), DSL.val("h")).execute();
        ctx.insertInto(table("gc_audit"), column("tenant_id"), column("operation"), column("collection"),
                column(idColumn(ctx, siteOf("gc_audit", "collection"))))
            .values(DSL.val(tenant), DSL.val("op"), DSL.val(first), DSL.val(firstId)).execute();
    }

    /**
     * The registry's own ids for {@code tenant}: rows with no id, rows whose tombstone fields disagree
     * ({@code (superseded_by = '') = (superseded_by_id IS NULL)}, item 6), and tombstones whose {@code superseded_by_id} is not
     * the id of the row named by {@code superseded_by} (a tombstone whose successor is gone takes its own id, P3.1's residual
     * R7, and is not a finding).
     */
    public static Finding registryAudit(DSLContext ctx, String tenant) {
        var t = CATALOG_COLLECTIONS.as("t");
        var s = CATALOG_COLLECTIONS.as("s");
        var g = CATALOG_COLLECTIONS.as("g");
        Condition live = t.SUPERSEDED_BY.eq("");
        Condition namesRow = DSL.exists(DSL.selectOne().from(s).where(s.TENANT_ID.eq(t.TENANT_ID)).and(s.NAME.eq(t.SUPERSEDED_BY))
            .and(s.COLLECTION_ID.eq(t.SUPERSEDED_BY_ID)));
        Condition successorGone = DSL.notExists(DSL.selectOne().from(g).where(g.TENANT_ID.eq(t.TENANT_ID)).and(g.NAME.eq(t.SUPERSEDED_BY)));
        Condition selfForm = t.SUPERSEDED_BY_ID.eq(t.COLLECTION_ID).and(successorGone);
        Condition bad = live.and(t.SUPERSEDED_BY_ID.isNotNull())
            .or(live.not().and(t.SUPERSEDED_BY_ID.isNull()))
            .or(live.not().and(namesRow.not()).and(selfForm.not()));
        Record3<Integer, Integer, Integer> r = ctx.select(DSL.count(), DSL.count().filterWhere(t.COLLECTION_ID.isNull()),
                DSL.count().filterWhere(bad))
            .from(t).where(t.TENANT_ID.eq(tenant)).fetchOne();
        return new Finding("catalog_collections", "collection_id", r.value1(), r.value2(), r.value3());
    }
}
