// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.TenantExport;
import dev.nexus.service.db.TenantExport.ExportTable;
import org.jooq.DSLContext;
import org.jooq.Record;

import java.util.ArrayList;
import java.util.List;

/**
 * Where RDR-229 item 6 puts a {@code collection_id} beside a collection name column, as the tenant backup tests
 * (bead nexus-p2m1x.31, P3.11) need it. The tests reach the new columns by name through the live schema, never
 * through a generated jOOQ symbol, so they compile before P3.1 (nexus-p2m1x.17) adds them and fail, with the
 * missing column named, until it does.
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

    /**
     * The id column {@code site} has in the live schema.
     *
     * @throws AssertionError naming the table and the candidates when none exists: P3.1 has not added the column
     */
    public static String idColumn(DSLContext ctx, Site site) {
        List<String> live = ctx.fetch("select column_name from information_schema.columns "
                + "where table_schema = 'nexus' and table_name = ?", site.table())
            .getValues(0, String.class);
        for (String c : site.idCandidates()) if (live.contains(c)) return c;
        throw new AssertionError("nexus." + site.table() + " has no collection id column for its " + site.nameColumn()
            + " (looked for " + site.idCandidates() + "): P3.1 (nexus-p2m1x.17) has not added it");
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
     * Every row of {@code tenant} at each site in {@code sites}, checked against the tenant's own registry. A row
     * whose name names a registry row must carry that row's id (null is a finding); a row whose id is set must point
     * at a registry row of the same tenant, and, for a site that is not a history table, the one with the row's name.
     * A name that is empty or names no registry row is not required to have an id (the CHECK of item 6 allows it).
     */
    public static List<Finding> audit(DSLContext ctx, String tenant, List<Site> sites) {
        List<Finding> out = new ArrayList<>();
        for (Site s : sites) {
            String idName = idColumn(ctx, s);
            String id = "t.\"" + idName + "\"";
            String name = "t.\"" + s.nameColumn() + "\"";
            String byName = "exists (select 1 from nexus.catalog_collections c where c.tenant_id = t.tenant_id and c.name = " + name + ")";
            String pointsRight = s.history()
                ? "exists (select 1 from nexus.catalog_collections c where c.tenant_id = t.tenant_id and c.collection_id = " + id + ")"
                : "exists (select 1 from nexus.catalog_collections c where c.tenant_id = t.tenant_id and c.name = " + name
                    + " and c.collection_id = " + id + ")";
            Record r = ctx.fetchOne("select count(*), "
                    + "count(*) filter (where " + id + " is null and " + byName + "), "
                    + "count(*) filter (where " + id + " is not null and not " + pointsRight + ") "
                    + "from nexus.\"" + s.table() + "\" t where t.tenant_id = ?", tenant);
            out.add(new Finding(s.table() + "." + s.nameColumn(), idName,
                r.get(0, Long.class), r.get(1, Long.class), r.get(2, Long.class)));
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

    /** One line per site with the rows it examined, for a failure message: a count the reader can see is never a silent zero. */
    public static String counts(List<Finding> found) {
        return found.stream().map(f -> f.site() + "=" + f.examined()).toList().toString();
    }

    private static Site siteOf(String table, String nameColumn) {
        return SITES.stream().filter(s -> s.table().equals(table) && s.nameColumn().equals(nameColumn)).findFirst().orElseThrow();
    }

    private static final String REGISTRY_ID = "(select collection_id from nexus.catalog_collections where tenant_id = ? and name = ?)";

    /**
     * One row in each table the backup does not carry (pdf_pipeline, chash_remap, reembed_jobs, canary_query_ring, and the
     * hook_failures and gc_audit logs), naming {@code first} and {@code second} and holding their registry ids, as an engine at P3.1
     * writes them. The battery seed reaches none of these, so a test that audits them seeds them here; they are never exported, so
     * a restore must leave the target without them. Each id column is read from the live schema, so this throws the same
     * "P3.1 has not added it" assertion as {@link #idColumn} before it.
     */
    public static void seedUnexported(DSLContext ctx, String tenant, String first, String second) {
        String pdfId = idColumn(ctx, siteOf("pdf_pipeline", "collection"));
        ctx.execute("insert into nexus.pdf_pipeline (tenant_id, content_hash, pdf_path, collection, \"" + pdfId
                + "\", started_at, updated_at) values (?, 'h1', 'a.pdf', ?, " + REGISTRY_ID + ", now(), now())",
            tenant, first, tenant, first);
        String srcId = idColumn(ctx, siteOf("chash_remap", "source_collection"));
        String tgtId = idColumn(ctx, siteOf("chash_remap", "target_collection"));
        ctx.execute("insert into nexus.chash_remap (tenant_id, source_collection, old_id, new_chash, target_collection, created_at, "
                + "provenance, \"" + srcId + "\", \"" + tgtId + "\") values (?, ?, 'old1', decode(repeat('ab', 32), 'hex'), ?, now(), 'p', "
                + REGISTRY_ID + ", " + REGISTRY_ID + ")",
            tenant, first, second, tenant, first, tenant, second);
        String rsrc = idColumn(ctx, siteOf("reembed_jobs", "source_collection"));
        String rtgt = idColumn(ctx, siteOf("reembed_jobs", "target_collection"));
        ctx.execute("insert into nexus.reembed_jobs (tenant_id, job_id, source_collection, target_collection, state, started_at, "
                + "updated_at, \"" + rsrc + "\", \"" + rtgt + "\") values (?, gen_random_uuid(), ?, ?, 'planned', now(), now(), "
                + REGISTRY_ID + ", " + REGISTRY_ID + ")",
            tenant, first, second, tenant, first, tenant, second);
        String canaryId = idColumn(ctx, siteOf("canary_query_ring", "collection"));
        ctx.execute("insert into nexus.canary_query_ring (tenant_id, collection, \"" + canaryId + "\", embedding_model, embedding_768, "
                + "vector_digest, added_at) select c.tenant_id, c.name, c.collection_id, c.embedding_model, "
                + "('[' || array_to_string(array_fill(0.5::float4, ARRAY[768]), ',') || ']')::nexus.vector(768), "
                + "decode(repeat('ab', 32), 'hex'), now() from nexus.catalog_collections c where c.tenant_id = ? and c.name = ?",
            tenant, first);
        String hookId = idColumn(ctx, siteOf("hook_failures", "collection"));
        ctx.execute("insert into nexus.hook_failures (tenant_id, collection, \"" + hookId + "\", hook_name) values (?, ?, "
                + REGISTRY_ID + ", 'h')", tenant, first, tenant, first);
        String gcId = idColumn(ctx, siteOf("gc_audit", "collection"));
        ctx.execute("insert into nexus.gc_audit (tenant_id, operation, collection, \"" + gcId + "\") values (?, 'op', ?, "
                + REGISTRY_ID + ")", tenant, first, tenant, first);
    }

    /**
     * The registry's own ids for {@code tenant}: rows with no id, rows whose tombstone fields disagree
     * ({@code (superseded_by = '') = (superseded_by_id IS NULL)}, item 6), and tombstones whose
     * {@code superseded_by_id} is not the id of the row named by {@code superseded_by} (a tombstone whose successor is gone
     * takes its own id, P3.1's residual R7, and is not a finding).
     */
    public static Finding registryAudit(DSLContext ctx, String tenant) {
        List<String> live = ctx.fetch("select column_name from information_schema.columns "
                + "where table_schema = 'nexus' and table_name = 'catalog_collections'").getValues(0, String.class);
        for (String c : List.of("collection_id", "superseded_by_id")) {
            if (!live.contains(c)) {
                throw new AssertionError("nexus.catalog_collections has no " + c + ": P3.1 (nexus-p2m1x.17) has not added it");
            }
        }
        Record r = ctx.fetchOne("select count(*), "
                + "count(*) filter (where t.collection_id is null), "
                + "count(*) filter (where (t.superseded_by = '') <> (t.superseded_by_id is null) "
                + "  or (t.superseded_by <> '' and not exists (select 1 from nexus.catalog_collections s "
                + "      where s.tenant_id = t.tenant_id and s.name = t.superseded_by and s.collection_id = t.superseded_by_id) "
                + "      and not (t.superseded_by_id = t.collection_id and not exists (select 1 from nexus.catalog_collections g "
                + "      where g.tenant_id = t.tenant_id and g.name = t.superseded_by)))) "
                + "from nexus.catalog_collections t where t.tenant_id = ?", tenant);
        return new Finding("catalog_collections", "collection_id", r.get(0, Long.class), r.get(1, Long.class), r.get(2, Long.class));
    }
}
