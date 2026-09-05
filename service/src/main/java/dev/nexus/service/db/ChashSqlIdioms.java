/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.Table;
import org.jooq.impl.DSL;

import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;

/**
 * RDR-180 shared chash-migration SQL idioms (nexus-jxizy.10.2).
 *
 * <p>Originally the single home for the SQL fragments BOTH in-DB chash
 * movers composed: {@code RekeyOps} (the shipped in-store rekey) and
 * {@link StagingPromoteOps} (the land-then-transform promote). {@code
 * RekeyOps} and every fragment that existed SOLELY to serve it (the
 * {@code chash_alias}-staging idioms — the digest-recovery lemma, the
 * content collapse/rekey DSL, the frecency alias aggregate, the alias-stats
 * refresh) were DELETED at nexus-lgdel.l1 along with {@code
 * nexus.chash_alias}: the rekey mechanism's only caller (the client {@code
 * chash_rekey} upgrade rung) is also deleted in that commit, and every one
 * of those fragments composed a statement that joined the now-dropped
 * table. What remains below is what {@link StagingPromoteOps} and {@link
 * ChashCensus} still share — fragments that never touched {@code
 * chash_alias} at all.
 *
 * <p>nexus-4okz4 increment 2: several fragments below carry a TYPED
 * jOOQ-DSL twin (or, where the string form had no other caller, REPLACING
 * it) — see each method's javadoc for which. The class docstring's
 * "single home" promise extends to the typed forms: a caller building DSL
 * statements should reach for the typed twin here rather than re-deriving
 * the formula locally.
 *
 * <p><b>THE INLINE-VS-BIND RULE (nexus-4okz4 increment 3, read this before
 * adding or reusing a fragment here):</b> {@code DSL.val(literal)} mints an
 * INDEPENDENT bind placeholder (a fresh {@code $N}) per TEXTUAL occurrence
 * of the rendered SQL, even when every occurrence comes from the SAME Java
 * {@code Field} object reused across clauses. PostgreSQL validates {@code
 * SELECT DISTINCT ON (...)} against its leading {@code ORDER BY}
 * expressions, and validates {@code GROUP BY} coverage of any ungrouped
 * SELECT-list expression, by PARSE-TREE STRUCTURAL EQUALITY — evaluated
 * BEFORE parameter binding. Two occurrences of the identical expression
 * that differ only in which {@code $N} they happen to bind are NOT
 * recognized as equal, so PostgreSQL reports a DISTINCT-ON/ORDER-BY
 * mismatch, or "column must appear in the GROUP BY clause," even though
 * both placeholders are bound to the SAME value at execution time. This is
 * invisible until a caller reuses the SAME fragment expression in two
 * clauses that need structural matching (DISTINCT ON + its ORDER BY, or a
 * GROUP BY expression + its own SELECT-list occurrence) — a fragment used
 * exactly once per statement (the common case: a WHERE predicate, a SET
 * target) never hits it, because WHERE/SET don't require matching anything
 * else textually. The fix, applied to {@link #digestField} this increment:
 * use {@code DSL.inline(literal)} instead of {@code DSL.val(literal)} for
 * the fragment's embedded constants ({@code "UTF8"}) — {@code DSL.inline}
 * renders the (properly jOOQ-escaped) literal directly into the SQL text,
 * so every occurrence is byte-for-byte identical and PostgreSQL's
 * structural check passes. Safe here specifically because these are FIXED
 * PROTOCOL CONSTANTS (encoding names), never derived from caller/user
 * input — never inline a value that could originate outside this file's
 * own literals. Before composing a NEW multi-occurrence DSL statement (a
 * DISTINCT ON, a GROUP BY reused in the SELECT list) from fragments in
 * this class, check whether the fragment embeds a {@code DSL.val(...)}
 * literal; if it does and you need the SAME expression object more than
 * once in one statement, either reach for an already-inlined twin or
 * inline the literal locally the same way — do not rediscover this by way
 * of a PostgreSQL error.
 */
public final class ChashSqlIdioms {

    private ChashSqlIdioms() {
    }

    /**
     * The content table(s) backing chunk storage, in canonical order.
     *
     * <p>RDR-191 Phase 4 (repoint-batch lane D5, bead nexus-o8dil.41 item 4):
     * {@code nexus.chunks_384/768/1024} collapsed into ONE unified {@code
     * nexus.chunks} table with three nullable typed embedding columns —
     * dim is now a COLUMN choice ({@link dev.nexus.service.vectors.DimTables
     * #embeddingColumn(int)}), not a TABLE identity, so a "list of dim
     * tables" is a single-element list going forward. Left as a {@code
     * List<String>} (not collapsed to a bare constant) because it remains
     * the schema-drift sentinel {@code RawSqlGateTest}'s canary pins — see
     * that test's rewritten checklist for what actually needs touching on a
     * FUTURE dimension-count change (a column add on the unified table, not
     * a table add).
     */
    public static final List<String> CHUNK_TABLES =
        List.of(dev.nexus.service.vectors.DimTables.CHUNKS_TABLE_NAME);

    /**
     * Typed rendering of the digest formula: full sha256 over the row's
     * chunk_text — {@code sha256(convert_to(chunkText, 'UTF8'))}.
     *
     * <p>{@code DSL.inline("UTF8")}, NOT {@code DSL.val("UTF8")} (nexus-4okz4
     * increment 3 pin, found by StagingPromoteOpsIntegrationTest, not
     * anticipated by review): {@code "UTF8"} is a fixed protocol constant,
     * never user input, so inlining it is injection-safe — and it MUST be
     * inline because callers that reference this SAME returned expression
     * object more than once within one rendered statement (e.g. a
     * {@code SELECT DISTINCT ON (...)} whose {@code ORDER BY} leads with
     * the identical expression) need every textual occurrence to be
     * byte-for-byte identical. PostgreSQL validates DISTINCT-ON/ORDER-BY
     * (and, the sibling case, GROUP-BY coverage) by PARSE-TREE structural
     * equality BEFORE parameter binding: a bind placeholder (`DSL.val`)
     * mints an INDEPENDENT `$N` per textual occurrence even for the same
     * Java object, so two occurrences of {@code sha256(convert_to(chunk_text,
     * $N))} with different `$N` are NOT recognized as the same expression,
     * even though both bind the same literal value at execution time —
     * PostgreSQL then reports the DISTINCT ON / ORDER BY mismatch (or, for
     * GROUP BY, that the ungrouped column "must appear in the GROUP BY
     * clause"). A caller referencing this field only once per statement
     * (the common case) sees no behavior change either way; this is a
     * correctness fix for the multiple-occurrence case, not a
     * representation change.
     */
    public static Field<byte[]> digestField(Field<String> chunkText) {
        return DSL.function("sha256", byte[].class,
            DSL.function("convert_to", byte[].class, chunkText, DSL.inline("UTF8")));
    }

    /**
     * {@code jsonb_build_object(...)} with typed/literal argument pairs
     * (nexus-4okz4 increment 2) — keys are always Java string literals;
     * values are either a {@code Field<?>} (passed through as-is) or a
     * literal (bound via {@link DSL#val(Object)}).
     */
    public static Field<JSONB> jsonbBuildObject(Object... keysAndValues) {
        if (keysAndValues.length % 2 != 0) {
            throw new IllegalArgumentException(
                "jsonbBuildObject needs an even number of key/value arguments");
        }
        Field<?>[] args = new Field<?>[keysAndValues.length];
        for (int i = 0; i < keysAndValues.length; i++) {
            Object v = keysAndValues[i];
            args[i] = (v instanceof Field<?> f) ? f : DSL.val(v);
        }
        return DSL.function("jsonb_build_object", JSONB.class, args);
    }

    /**
     * {@code coalesce(metadata, '{}'::jsonb) || stamp} (nexus-4okz4
     * increment 2) — the RDR-086 metadata-mirror merge every chash-writing
     * UPDATE/INSERT in {@link StagingPromoteOps} applies (critic-1010:
     * producers stamp {@code chunk_text_hash} to mirror the current key;
     * this merges a stamp in without clobbering pre-existing metadata
     * keys). {@code jsonb_concat} is a real PostgreSQL builtin (verified
     * against a live PG17 instance during this conversion — it is NOT the
     * {@code ||} operator spelled differently, it is a genuine function of
     * that name), same idiom {@code CatalogRepository.buildUpdateDocumentQuery}
     * already uses for the {@code catalog_documents.metadata} merge.
     */
    public static Field<JSONB> mergeMetadata(Field<JSONB> metadata, Field<JSONB> stamp) {
        return DSL.function("jsonb_concat", JSONB.class,
            DSL.coalesce(metadata, DSL.val(JSONB.valueOf("{}"))),
            stamp);
    }

    /**
     * Typed rendering of the former string-returning {@code
     * residualMismatchCount} (nexus-4okz4 increment 2) — SHARED ({@link
     * StagingPromoteOps#finalizeTenant} step 7 AND {@code ChashCensus}
     * historically; {@code RekeyOps}' own call site was deleted with that
     * class at nexus-lgdel.l1). In-txn verify: {@code count(*)} of content
     * rows whose stored chash mismatches {@code sha256(chunk_text)}.
     */
    public static Integer residualMismatchCountDsl(DSLContext ctx, Table<?> table,
            Field<byte[]> chash, Field<String> chunkText) {
        Field<byte[]> digest = digestField(chunkText);
        return ctx.selectCount().from(table)
            .where(chunkText.ne(""))
            .and(chash.isDistinctFrom(digest))
            .fetchOne(0, Integer.class);
    }

    /**
     * In-txn verify: {@code count(*)} of manifest rows pointing at no
     * content row. SINGLE-HOMED (nexus-4okz4 increment 1): every caller
     * ({@link StagingPromoteOps#finalizeTenant} step 7, {@link ChashCensus
     * #danglingPointers}) calls this ONE implementation. ({@code RekeyOps}'
     * step 6 call site was deleted with that class at nexus-lgdel.l1.)
     *
     * <p>RDR-191 Phase 4 (repoint-batch lane D5, bead nexus-o8dil.41 item
     * 4): {@code nexus.chunks_384/768/1024} collapsed into ONE unified
     * {@code nexus.chunks} table — the predicate this method renders was
     * always "no content row for this chash IN ANY DIM," and with dim no
     * longer a table identity that collapses from a three-way ANDed {@code
     * NOT EXISTS} (one per former dim table) to a SINGLE {@code NOT EXISTS}
     * against the one unified table; a chash is content-backed or it is
     * not, full stop — {@link #CHUNK_TABLES} / {@link
     * dev.nexus.service.vectors.DimTables#CHUNKS_TABLE_NAME} is the single
     * authority for that one table name.
     *
     * <p><b>Re-keyed to {@code (tenant_id, collection, chash)} (nexus-eanej,
     * the 2026-08-11 2.2x-undercount fix: 2,951 reported vs 6,501 actual).</b>
     * The predicate was CHASH-ONLY before this fix — a manifest row's chash
     * matching {@code nexus.chunks} under ANY tenant/collection read as
     * resolved, even when no content row exists at the manifest row's OWN
     * {@code (tenant_id, collection, chash)} triple. Every other detector in
     * this codebase that answers the same question already keys the full
     * triple — {@code catalog-025-collection-not-null.xml}'s dangling-row
     * cleanup, the {@code fk_catalog_chunks_chunk} FK itself
     * ({@code catalog-029-manifest-chunk-fk.xml}, {@code REFERENCES
     * nexus.chunks (tenant_id, collection, chash)}), and {@code
     * gc_quarantine_orphans}'s anti-join — this method now matches that
     * house pattern instead of being the one chash-only outlier. The {@code
     * tenant_id} conjunct is defense-in-depth rather than an independently
     * observable fix under normal access: every known caller runs inside
     * {@link TenantScope#withTenant}, and both {@code
     * catalog_document_chunks} and {@code chunks} carry FORCE ROW LEVEL
     * SECURITY keyed on {@code nexus.tenant} (RDR-191 GATE-2 census, T2
     * [22383] cell 3's identical reasoning for {@code contentCollapseDelete}:
     * a cross-tenant chash coincidence is already invisible to a
     * {@code nexus_svc} (NOBYPASSRLS) session regardless of this predicate)
     * — but the SQL text should not depend on RLS alone to be correct, and a
     * future BYPASSRLS caller (an admin/diagnostic path) would otherwise
     * silently inherit the same undercount. {@code collection} is the
     * operative half of the fix: RLS carries no per-collection dimension at
     * all, so a chash landing in a DIFFERENT collection under the SAME
     * tenant was the actual undercount driver, unprotected by RLS or
     * anything else.
     */
    public static Integer danglingManifestCountDsl(org.jooq.DSLContext ctx) {
        return ctx.selectCount()
            .from(CATALOG_DOCUMENT_CHUNKS)
            .where(DSL.notExists(ctx.selectOne().from(CHUNKS)
                    .where(CHUNKS.TENANT_ID.eq(CATALOG_DOCUMENT_CHUNKS.TENANT_ID))
                    .and(CHUNKS.COLLECTION.eq(CATALOG_DOCUMENT_CHUNKS.COLLECTION))
                    .and(CHUNKS.CHASH.eq(CATALOG_DOCUMENT_CHUNKS.CHASH))))
            .fetchOne(0, Integer.class);
    }

    /**
     * The {@code EXISTS} predicate proving a chash is CANONICAL (a content
     * row for it exists) — nexus-4okz4 increment 4 shared home for the
     * idiom, with {@code ChashCensus}' dangling-pointer scan as its first
     * caller. Every caller reaches for THIS one rather than re-deriving the
     * predicate locally.
     *
     * <p>RDR-191 Phase 4 (repoint-batch lane D5, bead nexus-o8dil.41 item
     * 4): was a three-way {@code OR} disjunction across
     * {@code chunks_384/768/1024} — with those collapsed into the ONE
     * unified {@code nexus.chunks} table, "exists in any dim" is now a
     * SINGLE {@code EXISTS} against that one table (dim is a column
     * choice, not a table identity a chash could independently exist
     * under).
     */
    public static Condition existsInAnyDim(DSLContext ctx, Field<byte[]> chash) {
        return DSL.exists(ctx.selectOne().from(CHUNKS).where(CHUNKS.CHASH.eq(chash)));
    }
}
