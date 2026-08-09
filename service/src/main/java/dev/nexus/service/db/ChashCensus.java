/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Table;
import org.jooq.TableField;
import org.jooq.impl.DSL;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

import static dev.nexus.service.jooq.nexus.Tables.CHASH_ALIAS;
import static dev.nexus.service.jooq.nexus.Tables.FRECENCY;
import static dev.nexus.service.jooq.nexus.Tables.RELEVANCE_LOG;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;

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
 *
 * <p>nexus-4okz4 increment 4: the class carried a file-level SANCTIONED RAW
 * entry ("no generated jOOQ table can exist for a column the census exists
 * to DISCOVER") that conflated two different things — the discovered
 * COLUMNS are genuinely runtime-only (no codegen for a table jOOQ doesn't
 * know exists), but the SQL that queries them does not need to be raw
 * string concatenation to be dynamic. {@code DSL.table(DSL.name(...))} /
 * {@code DSL.field(DSL.name(...), Class)} build typed-shaped, properly
 * identifier-quoted references to a runtime-discovered table/column with
 * no codegen involved at all — the same idiom {@link
 * dev.nexus.service.http.VersionHandler} already uses for {@code
 * public.databasechangelog} (outside jOOQ's {@code nexus}/{@code t1}
 * codegen scope for a different reason: cross-schema, not runtime-unknown).
 * {@code information_schema.columns}/{@code .tables} get the same
 * treatment. The three FIXED hex-keyed pointer tables in {@link
 * #danglingPointers} ({@code topic_assignments}, {@code frecency}, {@code
 * relevance_log}) are not runtime-discovered at all — they are compile-time
 * known and DO have generated {@code Tables} constants, so that leg
 * converts to fully typed DSL via {@link ChashSqlIdioms#existsInAnyDim}.
 * Zero raw string-SQL execute/fetch sites remain in this class; the
 * RawSqlGateTest allowlist entry is REMOVED (see that test's comment for
 * the dead-sanction-entry discipline this follows).
 */
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

    // Typed ad-hoc DSL.table/DSL.field references (same idiom as
    // VersionHandler.DATABASECHANGELOG): information_schema is outside
    // jOOQ codegen's nexus/t1 inputSchema scope, so there is no generated
    // Tables.COLUMNS — that is a codegen-coverage fact, not a reason to
    // fall back to string-concatenated SQL.
    private static final Table<?> INFO_COLUMNS = DSL.table(DSL.name("information_schema", "columns"));
    private static final Table<?> INFO_TABLES  = DSL.table(DSL.name("information_schema", "tables"));

    /** Enumerate schema-nexus columns of one udt type: {@code table.column}. */
    private static List<String[]> columns(DSLContext ctx, String udt) {
        Table<?> c = INFO_COLUMNS.as("c");
        Table<?> t = INFO_TABLES.as("t");
        Field<String> cSchema = DSL.field(DSL.name("c", "table_schema"), String.class);
        Field<String> cTable  = DSL.field(DSL.name("c", "table_name"), String.class);
        Field<String> cColumn = DSL.field(DSL.name("c", "column_name"), String.class);
        Field<String> cUdt    = DSL.field(DSL.name("c", "udt_name"), String.class);
        Field<String> tSchema = DSL.field(DSL.name("t", "table_schema"), String.class);
        Field<String> tTable  = DSL.field(DSL.name("t", "table_name"), String.class);
        Field<String> tType   = DSL.field(DSL.name("t", "table_type"), String.class);

        List<String[]> out = new ArrayList<>();
        ctx.select(cTable, cColumn)
            .from(c)
            .join(t).on(tSchema.eq(cSchema).and(tTable.eq(cTable)))
            .where(cSchema.eq("nexus"))
            .and(cUdt.eq(udt))
            .and(tType.eq("BASE TABLE"))
            .orderBy(cTable, cColumn)
            .forEach(r -> out.add(new String[] {r.get(cTable), r.get(cColumn)}));
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
            Table<?> tbl = DSL.table(DSL.name("nexus", col[0]));
            Field<String> fld = DSL.field(DSL.name(col[1]), String.class);
            Integer n = ctx.selectCount().from(tbl)
                .where(fld.likeRegex(LEGACY_SHAPE))
                .fetchOne(0, Integer.class);
            if (n != null && n > 0) residue.put(col[0] + "." + col[1], n);
        }
        for (String[] col : columns(ctx, "bytea")) {
            if (excluded(BYTEA_EXCLUSIONS, col[0], col[1])) continue;
            Table<?> tbl = DSL.table(DSL.name("nexus", col[0]));
            Field<byte[]> fld = DSL.field(DSL.name(col[1]), byte[].class);
            Integer n = ctx.selectCount().from(tbl)
                .where(fld.isNotNull())
                .and(DSL.function("octet_length", Integer.class, fld).ne(32))
                .fetchOne(0, Integer.class);
            if (n != null && n > 0) residue.put(col[0] + "." + col[1] + "[bytea]", n);
        }
        // Dangling 64-hex pointers: hex-keyed stores whose id resolves to no
        // content row in any dim (the critic-C3 verify gap, closed).
        residue.putAll(danglingPointers(ctx));
        return residue;
    }

    /** EITHER era's chash shape — the 64-only filter was the blindness. */
    private static final String EITHER_ERA_SHAPE = "^([0-9a-f]{32}|[0-9a-f]{64})$";

    /**
     * Count rows of one hex-keyed pointer table (nexus-4okz4 increment 4)
     * whose {@code hexCol} value is shaped like a chash of either era AND
     * resolves to a live chunk by NO route — neither directly nor through
     * {@code chash_alias}. Fully typed DSL: {@code topic_assignments},
     * {@code frecency}, {@code relevance_log} are compile-time-known
     * tables (unlike the runtime-discovered columns above), so this reuses
     * the generated {@code Tables} constants and {@link
     * ChashSqlIdioms#existsInAnyDim} rather than string-formatting a
     * template. {@code decode(hexCol, 'hex')}/{@code 'hex'} occurrences
     * below are each in their OWN independent {@code NOT EXISTS}/{@code
     * EXISTS} clause (no DISTINCT-ON/GROUP-BY structural-match requirement
     * across them), so plain binds (not {@code DSL.inline}) are safe per
     * the INLINE-VS-BIND rule ({@link ChashSqlIdioms} class javadoc).
     */
    private static Integer unresolvableHexCount(DSLContext ctx, TableField<?, String> hexCol) {
        Field<byte[]> decoded = DSL.function("decode", byte[].class, hexCol, DSL.val("hex"));
        return ctx.selectCount().from(hexCol.getTable())
            .where(hexCol.likeRegex(EITHER_ERA_SHAPE))
            .and(DSL.not(ChashSqlIdioms.existsInAnyDim(ctx, decoded)))
            .and(DSL.notExists(ctx.selectOne().from(CHASH_ALIAS)
                    .where(CHASH_ALIAS.OLD_REF.eq(hexCol))
                    .and(ChashSqlIdioms.existsInAnyDim(ctx, CHASH_ALIAS.NEW_CHASH))))
            .fetchOne(0, Integer.class);
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
        Map<String, TableField<?, String>> hexKeyed = Map.of(
            "topic_assignments", TOPIC_ASSIGNMENTS.DOC_ID,
            "frecency", FRECENCY.CHUNK_ID,
            "relevance_log", RELEVANCE_LOG.CHUNK_ID);
        for (Map.Entry<String, TableField<?, String>> e : hexKeyed.entrySet()) {
            Integer n = unresolvableHexCount(ctx, e.getValue());
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
        Integer manifest = ChashSqlIdioms.danglingManifestCountDsl(ctx);
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
