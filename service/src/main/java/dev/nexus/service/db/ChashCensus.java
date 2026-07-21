/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import org.jooq.DSLContext;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * RDR-180 COLUMN CENSUS (nexus-jxizy.10.5, Hal directive 2026-07-19):
 * the SCHEMA-DERIVED legacy-residue scan — "check every text column",
 * mechanically, so a missed migration leg is IMPOSSIBLE to miss silently.
 *
 * <p>The enumeration comes from {@code information_schema.columns}, never
 * a hand list — a NEW chash-bearing column shows up in the scan
 * automatically. The only hand-maintained part is the ALLOWLIST of
 * deliberate exclusions, each carrying a justification and an existence
 * check (a renamed/deleted column must be pruned, not silently skipped —
 * the {@code tests/test_no_chash_truncation.py} discipline, applied to
 * data).
 *
 * <p>Three scans, all under the caller's {@link TenantScope} (RLS-scoped —
 * a migration verify sees exactly the tenant it migrated):
 * <ol>
 *   <li>TEXT columns in schema {@code nexus}: count values shaped like a
 *       LEGACY chunk id (16- or 32-lowercase-hex, full-string match) —
 *       zero expected outside the allowlist.</li>
 *   <li>BYTEA columns in schema {@code nexus}: count values whose width is
 *       not the canonical 32 bytes — zero expected outside the
 *       allowlist.</li>
 *   <li>Pointer dangling: 64-hex-shaped ids in the hex-keyed pointer
 *       stores that resolve to NO content row (the verify surface
 *       critic-C3 found missing from RekeyOps).</li>
 * </ol>
 *
 * <p>NON-VACUITY: {@link #assertDiscoversKnownInventory} fails when the
 * schema-derived enumeration no longer FINDS the known chash-bearing
 * columns — a census that cannot see its own inventory is broken, and a
 * clean report from a broken census is the exact failure mode this class
 * exists to kill.
 */
// SANCTIONED RAW (nexus-jxizy.10.5, RawSqlGateTest allowlist): the census is
// dynamic-by-construction (columns enumerated from information_schema at
// run time) — no generated jOOQ table can exist for a column the census
// exists to DISCOVER. Read-only counts; never serving-path.
public final class ChashCensus {

    private ChashCensus() {
    }

    /** A deliberate exclusion: column + why it may hold non-canonical values. */
    public record Exclusion(String table, String column, String why) {
    }

    /**
     * The justified exclusions. KEEP SHORT; additions need the same scrutiny
     * a new sqlite3.connect gets.
     */
    public static final List<Exclusion> TEXT_EXCLUSIONS = List.of(
        new Exclusion("chash_alias", "old_ref",
            "THE legacy-reference registry — holding old ids is its purpose"),
        new Exclusion("chash_remap", "old_id",
            "remap facts: old_id is free-form by design (RDR-180 Item6a)"),
        new Exclusion("chash_remap", "new_chash",
            "widened era facts: 32-hex pre-flip rows stay readable (rdr180-13)"),
        new Exclusion("chunks_384", "chunk_text",
            "free content — a note BODY may legitimately be a bare hash string"),
        new Exclusion("chunks_768", "chunk_text", "free content (see chunks_384)"),
        new Exclusion("chunks_1024", "chunk_text", "free content (see chunks_384)"),
        new Exclusion("relevance_log", "query", "free content (user query text)"),
        new Exclusion("aspect_extraction_queue", "content", "free content"),
        new Exclusion("aspect_extraction_queue", "content_hash",
            "sha256 of source CONTENT (a document identity, not a chunk id) — "
            + "legacy-width source hashes are historical facts, not pointers"));

    public static final List<Exclusion> BYTEA_EXCLUSIONS = List.of(
        new Exclusion("chash_alias", "old_bytes",
            "the byte carrier of old_ref — any width by design"));

    /** The known chash-bearing inventory the enumeration MUST rediscover. */
    // chash_index.chash left the inventory WITH the table (RDR-187 DROP,
    // nexus-piwya.9) — the schema-derived enumeration no longer discovers it.
    static final Set<String> KNOWN_INVENTORY = Set.of(
        "catalog_document_chunks.chash",
        "topic_assignments.doc_id", "frecency.chunk_id", "relevance_log.chunk_id",
        "chunks_384.chash", "chunks_768.chash", "chunks_1024.chash");

    private static final String LEGACY_SHAPE = "^([0-9a-f]{16}|[0-9a-f]{32})$";

    /** Enumerate schema-nexus columns of one udt type: {@code table.column}. */
    private static List<String[]> columns(DSLContext ctx, String udt) {
        List<String[]> out = new ArrayList<>();
        ctx.resultQuery(
                "SELECT c.table_name, c.column_name "
                + "FROM information_schema.columns c "
                + "JOIN information_schema.tables t "
                + "  ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
                + "WHERE c.table_schema = 'nexus' AND c.udt_name = ? "
                + "  AND t.table_type = 'BASE TABLE' "
                + "ORDER BY c.table_name, c.column_name", udt)
            .forEach(r -> out.add(new String[] {r.get(0, String.class), r.get(1, String.class)}));
        return out;
    }

    private static boolean excluded(List<Exclusion> exclusions, String table, String column) {
        return exclusions.stream().anyMatch(e -> e.table().equals(table) && e.column().equals(column));
    }

    /**
     * The full census. Returns per-column residue counts for every
     * NON-EXCLUDED column with residue &gt; 0 (empty map = clean) plus the
     * dangling-pointer counts under {@code dangling.*} keys.
     */
    public static Map<String, Integer> scan(DSLContext ctx) {
        Map<String, Integer> residue = new LinkedHashMap<>();
        for (String[] col : columns(ctx, "text")) {
            if (excluded(TEXT_EXCLUSIONS, col[0], col[1])) continue;
            Integer n = ctx.fetchOne(
                "SELECT count(*) FROM nexus.\"" + col[0] + "\""
                + " WHERE \"" + col[1] + "\" ~ '" + LEGACY_SHAPE + "'").get(0, Integer.class);
            if (n != null && n > 0) residue.put(col[0] + "." + col[1], n);
        }
        for (String[] col : columns(ctx, "bytea")) {
            if (excluded(BYTEA_EXCLUSIONS, col[0], col[1])) continue;
            Integer n = ctx.fetchOne(
                "SELECT count(*) FROM nexus.\"" + col[0] + "\""
                + " WHERE \"" + col[1] + "\" IS NOT NULL AND octet_length(\"" + col[1] + "\") <> 32")
                .get(0, Integer.class);
            if (n != null && n > 0) residue.put(col[0] + "." + col[1] + "[bytea]", n);
        }
        // Dangling 64-hex pointers: hex-keyed stores whose id resolves to no
        // content row in any dim (the critic-C3 verify gap, closed).
        residue.putAll(danglingPointers(ctx));
        return residue;
    }

    /**
     * Dangling-pointer legs (nexus-kmd5b).
     *
     * <p>These previously gated on the CONFORMANT width — {@code
     * octet_length = 32} for chash_index, {@code ~ '^[0-9a-f]{64}$'} for the
     * three TEXT debt columns — which excluded exactly the population they
     * exist to find: a pointer the cascade could NOT repoint is, by
     * definition, still at its LEGACY width. Production 2026-07-20 measured
     * the consequence: the chash_index leg reported <strong>1</strong> against
     * <strong>292,230</strong> actual orphans, while the manifest leg (no
     * width precondition) reported 426 against 426. Same structural shape as
     * nexus-vounk — a check that structurally cannot see the thing it checks
     * for, whose "all clear" is evidence of a blind query, not a clean store.
     *
     * <p>DANGLING now means what it says: the pointer resolves to a live chunk
     * by NO route — neither directly nor through the permanent {@code
     * chash_alias} map, which is the whole point of that map (RDR-180: legacy
     * references stay resolvable forever). A legacy-width pointer WITH an
     * alias entry is therefore resolvable and not counted; one without is
     * genuine debt and is.
     *
     * <p>The TEXT columns keep a shape filter, but widened to "a chash of
     * EITHER era" (32- or 64-hex): {@code topic_assignments.doc_id} is a mixed
     * identity space that also holds memory-note titles (RDR-180 Item2), and
     * dropping the filter entirely would flag every title as dangling.
     */
    private static Map<String, Integer> danglingPointers(DSLContext ctx) {
        Map<String, Integer> out = new LinkedHashMap<>();
        // Resolves-by-no-route, for a hex-TEXT pointer of either era.
        String unresolvableHex =
            "NOT EXISTS (SELECT 1 FROM nexus.chunks_384 c WHERE c.chash = decode(%1$s, 'hex')) "
            + "AND NOT EXISTS (SELECT 1 FROM nexus.chunks_768 c WHERE c.chash = decode(%1$s, 'hex')) "
            + "AND NOT EXISTS (SELECT 1 FROM nexus.chunks_1024 c WHERE c.chash = decode(%1$s, 'hex')) "
            + "AND NOT EXISTS (SELECT 1 FROM nexus.chash_alias a "
            + "                 WHERE a.old_ref = %1$s "
            + "                   AND (EXISTS (SELECT 1 FROM nexus.chunks_384 k WHERE k.chash = a.new_chash) "
            + "                     OR EXISTS (SELECT 1 FROM nexus.chunks_768 k WHERE k.chash = a.new_chash) "
            + "                     OR EXISTS (SELECT 1 FROM nexus.chunks_1024 k WHERE k.chash = a.new_chash)))";
        Map<String, String> hexKeyed = Map.of(
            "topic_assignments", "doc_id",
            "frecency", "chunk_id",
            "relevance_log", "chunk_id");
        for (Map.Entry<String, String> e : hexKeyed.entrySet()) {
            String col = e.getValue();
            Integer n = ctx.fetchOne(
                "SELECT count(*) FROM nexus." + e.getKey() + " p "
                // EITHER era's chash shape — the 64-only filter was the blindness.
                + "WHERE p." + col + " ~ '^([0-9a-f]{32}|[0-9a-f]{64})$' AND "
                + String.format(unresolvableHex, "p." + col)).get(0, Integer.class);
            if (n != null && n > 0) out.put("dangling." + e.getKey(), n);
        }
        // RDR-187 (nexus-piwya.5): the dangling.chash_index leg is RETIRED
        // ahead of the table DROP (nexus-piwya.9) — a leg reading
        // nexus.chash_index errors on the missing relation once the router
        // dies, and its orphan population (292,230 measured in production,
        // post-kmd5b) dies AT the DROP rather than being reported forever.
        // The manifest leg below and the TEXT debt-column legs above remain
        // the census's dangling surface. (KNOWN_INVENTORY's chash_index.chash
        // entry left with the table in the same commit as the rdr187-2 DROP —
        // the enumeration is schema-derived, and the two stayed in lockstep
        // exactly as planned at .5.)
        // The manifest (review P1 Critical: the census backstop must cover
        // catalog_document_chunks independently of the finalize call site).
        Integer manifest = ctx.fetchOne(
            ChashSqlIdioms.danglingManifestCount()).get(0, Integer.class);
        if (manifest != null && manifest > 0) out.put("dangling.catalog_document_chunks", manifest);
        return out;
    }

    /**
     * NON-VACUITY: the schema-derived enumeration must rediscover the known
     * chash-bearing inventory, and every allowlist entry must still exist.
     */
    public static void assertDiscoversKnownInventory(DSLContext ctx) {
        List<String> discovered = new ArrayList<>();
        for (String[] c : columns(ctx, "text")) discovered.add(c[0] + "." + c[1]);
        for (String[] c : columns(ctx, "bytea")) discovered.add(c[0] + "." + c[1]);
        List<String> missing = new ArrayList<>();
        for (String known : KNOWN_INVENTORY) {
            if (!discovered.contains(known)) missing.add(known);
        }
        if (!missing.isEmpty()) {
            throw new IllegalStateException(
                "census enumeration no longer discovers the known chash-bearing "
                + "inventory: " + missing + " — a census that cannot see its own "
                + "inventory is broken; a clean report from it proves nothing");
        }
        for (Exclusion e : TEXT_EXCLUSIONS) {
            if (!discovered.contains(e.table() + "." + e.column())) {
                throw new IllegalStateException(
                    "allowlist entry " + e.table() + "." + e.column()
                    + " matches no live column — prune it");
            }
        }
        for (Exclusion e : BYTEA_EXCLUSIONS) {
            if (!discovered.contains(e.table() + "." + e.column())) {
                throw new IllegalStateException(
                    "allowlist entry " + e.table() + "." + e.column()
                    + " matches no live column — prune it");
            }
        }
    }
}
