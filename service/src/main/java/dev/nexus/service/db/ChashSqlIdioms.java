/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.Table;
import org.jooq.impl.DSL;

import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHASH_ALIAS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS_1024;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS_384;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS_768;
import static dev.nexus.service.jooq.nexus.Tables.FRECENCY;

/**
 * RDR-180 shared chash-migration SQL idioms (nexus-jxizy.10.2).
 *
 * <p>The single home for the SQL fragments BOTH in-DB chash movers compose:
 * {@link RekeyOps} (the shipped in-store rekey — in-place UPDATE shape) and
 * {@code StagingPromoteOps} (the land-then-transform promote —
 * INSERT-into-possibly-populated-target shape, nexus-jxizy.10.3). Extracted
 * behavior-preserving from RekeyOps (its integration suite is the
 * regression gate); the two ops classes compose DIFFERENT statements from
 * these SAME fragments — the point is that the digest formula, the
 * reversibility lemma, the collapse keeper selection, the frecency merge
 * aggregate, and the verify scans can never drift between the two writers
 * of the shared tables.
 *
 * <p>Scoping note (reconciliation H2): the string-to-bytes direction is the
 * DB function {@code nexus.chash_old_bytes(text)} (rdr180-20 changeset) —
 * total over any legacy ref, used by every {@code chash_alias.old_bytes}
 * writer. The bytes-to-string recovery lemma ({@link #OLD_REF_LEMMA} /
 * {@link #oldRefField}) stays an in-store-only idiom with its documented
 * CONSTRAINED domain (values that lived under the pre-flip 32-char CHECK):
 * staging always carries the original ref alongside, so the promote path
 * never recovers strings from bytes.
 *
 * <p>nexus-4okz4 increment 2: several fragments below now carry a TYPED
 * jOOQ-DSL twin alongside (or, where the string form had no other caller,
 * REPLACING) their raw-string original — see each method's javadoc for
 * which. The class docstring's "single home" promise extends to the typed
 * forms: a caller building DSL statements should reach for the typed twin
 * here rather than re-deriving the formula locally.
 */
// SANCTIONED RAW (nexus-jxizy.10.2, narrowed nexus-4okz4 increment 2): the
// residue after this increment converted contentRekeyUpdate,
// frecencyAliasAggregate, and residualMismatchCount to typed DSL
// (contentRekeyUpdateDsl / frecencyAliasAggregateDsl / residualMismatchCountDsl
// — no allowlist entry needed, pure DSL, no raw-SQL string executed) and
// deleted the now-dead raw-string forms outright. What remains sanctioned:
// contentCollapseDelete (the ctid/array_agg ORDER BY keeper-selection idiom
// has no jOOQ DSL form — array-subscript of an ordered array_agg), and
// refreshAliasStats (ANALYZE is maintenance DDL with no jOOQ DSL form at
// all, plus a privilege probe over pg_class/has_table_privilege system
// catalogs codegen does not cover). chashOldBytes is a one-line string
// helper with no execute/fetch call of its own — StagingPromoteOps-only,
// out of this increment's scope. Never serving-path.
public final class ChashSqlIdioms {

    private ChashSqlIdioms() {
    }

    /** The three dim-partitioned content tables, in canonical order. */
    public static final List<String> CHUNK_TABLES =
        List.of("nexus.chunks_384", "nexus.chunks_768", "nexus.chunks_1024");

    /** The one digest formula: full sha256 over the row's chunk_text.
     *  Typed jOOQ-DSL twin: {@link #digestField(Field)} (nexus-4okz4
     *  increment 2) — same formula, for callers composing typed DSL. */
    public static final String DIGEST = "sha256(convert_to(chunk_text, 'UTF8'))";

    /**
     * Reversibility-lemma rendering of a CONVERTED key's original string
     * (in-store domain only — see class docstring). {@code %1$s} is the
     * bytea column reference. Typed jOOQ-DSL twin: {@link #oldRefField(Field)}
     * (nexus-4okz4 increment 2).
     */
    public static final String OLD_REF_LEMMA =
        "CASE WHEN octet_length(%1$s) = 16 THEN encode(%1$s, 'hex') "
        + "ELSE convert_from(%1$s, 'UTF8') END";

    /**
     * The canonical string-to-bytes function for
     * {@code chash_alias.old_bytes} (rdr180-20 changeset) — call as
     * {@code chashOldBytes("s.legacy_ref")}.
     */
    public static String chashOldBytes(String refExpr) {
        return "nexus.chash_old_bytes(" + refExpr + ")";
    }

    /**
     * Typed rendering of {@link #DIGEST} (same formula: {@code
     * sha256(convert_to(chunkText, 'UTF8'))}) — added nexus-4okz4 increment
     * 2 so callers composing typed jOOQ DSL statements (RekeyOps' UNION /
     * INSERT / UPDATE sites) do not fall back to string interpolation.
     * Tied to {@link #DIGEST} by formula identity, not derived from it — a
     * formula change must update both. Both renderings are exercised by
     * RekeyOpsIntegrationTest (every rekey assertion depends on the digest
     * being right), so a drift between them fails loud on the next test
     * run rather than silently.
     */
    public static Field<byte[]> digestField(Field<String> chunkText) {
        return DSL.function("sha256", byte[].class,
            DSL.function("convert_to", byte[].class, chunkText, DSL.val("UTF8")));
    }

    /**
     * Typed rendering of {@link #OLD_REF_LEMMA} (same in-store-only domain
     * constraint — see class docstring): {@code CASE WHEN
     * octet_length(chash) = 16 THEN encode(chash, 'hex') ELSE
     * convert_from(chash, 'UTF8') END}. Added nexus-4okz4 increment 2.
     */
    public static Field<String> oldRefField(Field<byte[]> chash) {
        return DSL.when(
                DSL.function("octet_length", Integer.class, chash).eq(16),
                DSL.function("encode", String.class, chash, DSL.val("hex")))
            .otherwise(DSL.function("convert_from", String.class, chash, DSL.val("UTF8")));
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
     * UPDATE in both movers applies (critic-1010: producers stamp {@code
     * chunk_text_hash} to mirror the current key; this merges a stamp in
     * without clobbering pre-existing metadata keys). {@code jsonb_concat}
     * is a real PostgreSQL builtin (verified against a live PG17 instance
     * during this conversion — it is NOT the {@code ||} operator spelled
     * differently, it is a genuine function of that name), same idiom
     * CatalogRepository.buildUpdateDocumentQuery already uses for the
     * {@code catalog_documents.metadata} merge.
     */
    public static Field<JSONB> mergeMetadata(Field<JSONB> metadata, Field<JSONB> stamp) {
        return DSL.function("jsonb_concat", JSONB.class,
            DSL.coalesce(metadata, DSL.val(JSONB.valueOf("{}"))),
            stamp);
    }

    /**
     * Phase-A content collapse for one chunk table: delete collapse-losers
     * per (collection, digest); the keeper is a row already AT the digest
     * key when one exists, else min ctid. Verbatim RekeyOps step (4)
     * phase A.
     */
    public static String contentCollapseDelete(String table) {
        return "DELETE FROM " + table + " c USING ("
            + "  SELECT collection, " + DIGEST + " AS d, "
            + "         (array_agg(ctid ORDER BY (chash = " + DIGEST + ") DESC, ctid))[1] AS keep "
            + "  FROM " + table + " WHERE chunk_text <> '' "
            + "  GROUP BY collection, " + DIGEST + " HAVING count(*) > 1"
            + ") k "
            + "WHERE c.collection = k.collection AND c.chunk_text <> '' "
            + "  AND " + DIGEST.replace("chunk_text", "c.chunk_text") + " = k.d "
            + "  AND c.ctid <> k.keep";
    }

    /**
     * Typed rendering of the former string-returning {@code
     * contentRekeyUpdate} (nexus-4okz4 increment 2) — grep-verified
     * RekeyOps-exclusive before this conversion (StagingPromoteOps never
     * called the string form), so this REPLACES it outright rather than
     * forking a variant. Phase B of the two-phase content rekey: UPDATE
     * survivors whose stored chash mismatches {@code sha256(chunk_text)},
     * re-stamping {@code metadata.chunk_text_hash} to mirror the new key
     * (critic-1010, nexus-jxizy.10.10 — same behavior as the string form
     * it replaces).
     */
    public static int contentRekeyUpdateDsl(DSLContext ctx, Table<?> table, Field<byte[]> chash,
            Field<String> chunkText, Field<JSONB> metadata) {
        Field<byte[]> digest = digestField(chunkText);
        Field<JSONB> stamp = jsonbBuildObject("chunk_text_hash",
            DSL.function("encode", String.class, digest, DSL.val("hex")));
        return ctx.update(table)
            .set(chash, digest)
            .set(metadata, mergeMetadata(metadata, stamp))
            .where(chunkText.ne(""))
            .and(chash.isDistinctFrom(digest))
            .execute();
    }

    /**
     * Typed rendering of the former string-returning {@code
     * frecencyAliasAggregate} (nexus-4okz4 increment 2) — grep-verified
     * RekeyOps-exclusive, replaces it outright. Per-target group aggregate
     * over old {@code frecency} rows joined to the alias map (both
     * collapse directions ride this; keeper keyed by {@code min(chunk_id)},
     * NOT ctid — the RekeyOpsIntegrationTest 3c catch: an UPDATE changes
     * ctid). Column aliases ({@code new_chash}/{@code keep_id}/{@code fs}/
     * {@code mc}/{@code lh}/{@code ea}/{@code td}) match the string form's
     * exactly, so a caller reads either rendering identically. Aliased
     * {@code g}, same as the string form.
     */
    public static Table<?> frecencyAliasAggregateDsl(DSLContext ctx) {
        return ctx.select(
                CHASH_ALIAS.NEW_CHASH.as("new_chash"),
                DSL.min(FRECENCY.CHUNK_ID).as("keep_id"),
                DSL.max(FRECENCY.FRECENCY_SCORE).as("fs"),
                DSL.max(FRECENCY.MISS_COUNT).as("mc"),
                DSL.max(FRECENCY.LAST_HIT_AT).as("lh"),
                DSL.max(FRECENCY.EMBEDDED_AT).as("ea"),
                DSL.max(FRECENCY.TTL_DAYS).as("td"))
            .from(FRECENCY)
            .join(CHASH_ALIAS).on(FRECENCY.CHUNK_ID.eq(CHASH_ALIAS.OLD_REF))
            .groupBy(CHASH_ALIAS.NEW_CHASH)
            .asTable("g");
    }

    /**
     * Typed rendering of the former string-returning {@code
     * residualMismatchCount} (nexus-4okz4 increment 2) — SHARED (RekeyOps
     * step 6 AND {@code StagingPromoteOps.finalizeTenant} step 7), so this
     * REPLACES the string form for BOTH callers in the same change
     * (single-homed, same discipline as {@link #danglingManifestCountDsl}'s
     * twin collapse in increment 1 — no forked variant left behind for
     * either caller). In-txn verify: {@code count(*)} of content rows
     * whose stored chash mismatches {@code sha256(chunk_text)}.
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
     * Refresh {@code nexus.chash_alias} statistics INSIDE the caller's
     * transaction, and report whether it actually took effect (rdr180-17 / F2,
     * production 2026-07-20).
     *
     * <p>BOTH chash movers write the alias map and then immediately join it —
     * {@link RekeyOps} for the Item8 disposition and the step-5 cascades,
     * {@code StagingPromoteOps} for its promote/collapse joins. On a
     * multi-tenant store the SECOND tenant onward is planned against
     * statistics autoanalyze froze the instant the FIRST tenant committed
     * ({@code most_common_vals={t1}}, {@code freqs=[1.0]},
     * {@code n_distinct=1}), while this transaction's own alias rows are
     * uncommitted and therefore invisible. The planner estimates ONE row,
     * picks a nested loop, and the cascade degrades from 461 seconds to 101
     * minutes on real data. An in-transaction ANALYZE samples this
     * transaction's own rows, which is the whole reason it cannot be deferred
     * to a post-commit maintenance pass.
     *
     * <p>The return value is NOT ceremony. {@code nexus_svc} holds DML grants
     * only and does not own the table; Postgres does not ERROR when a
     * non-owner analyzes — it WARNs and SKIPS. Without {@code MAINTAIN}
     * (granted by {@code grants-nexus-svc}) this method is a silent no-op, so
     * callers report the outcome in their envelope rather than assuming the
     * planner was un-blinded. Same discipline the RDR-180 window taught twice
     * over: the outcome of the operation, never the issuing of the statement.
     *
     * <p>The server-version test is load-bearing, not defensive clutter:
     * {@code MAINTAIN} does not exist before PostgreSQL 17, and
     * {@code has_table_privilege(..., 'MAINTAIN')} does not return false there
     * — it RAISES "unrecognized privilege type". Probing unguarded would
     * therefore abort the entire rekey transaction on a legacy cluster, which
     * is strictly worse than the stale-statistics slowness this method exists
     * to prevent. Managed/cloud runs on a provider-controlled server (verified
     * PostgreSQL 17.10, 2026-07-20) and local-service runs on our own 17.x
     * bundle; the branch covers clusters an earlier, pre-bundle install
     * created and the data-directory carve-out deliberately keeps.
     *
     * @return {@code true} when the role can actually analyze the table
     */
    // SANCTIONED RAW (rdr180-17): ANALYZE is maintenance DDL with no jOOQ DSL
    // form, and the privilege probe reads system catalogs (pg_class,
    // has_table_privilege) that codegen does not cover. Must execute inside the
    // caller's transaction to see its own uncommitted rows. Never serving-path.
    public static boolean refreshAliasStats(org.jooq.DSLContext ctx) {
        Boolean permitted = ctx.fetchOne(
            "SELECT current_setting('server_version_num')::int >= 170000 "
            + "   AND (pg_catalog.has_table_privilege('nexus.chash_alias', "
            + "          CASE WHEN current_setting('server_version_num')::int >= 170000 "
            + "               THEN 'MAINTAIN' ELSE 'SELECT' END) "
            + "        OR pg_catalog.pg_get_userbyid("
            + "             (SELECT relowner FROM pg_class "
            + "               WHERE oid = 'nexus.chash_alias'::regclass)) = current_user)"
        ).get(0, Boolean.class);
        if (permitted == null || !permitted) {
            return false;
        }
        ctx.execute("ANALYZE nexus.chash_alias");
        return true;
    }

    /**
     * In-txn verify: {@code count(*)} of manifest rows pointing at no
     * content row in any dim. SINGLE-HOMED (nexus-4okz4 increment 1,
     * collapsing the twin renderings the nexus-t76bp jOOQ-DSL pass left
     * split across {@code RekeyOps} (a private DSL copy) and this class's
     * former raw-SQL {@code danglingManifestCount()} string — both callers
     * ({@code RekeyOps.rekey} step 6, {@code StagingPromoteOps.
     * finalizeTenant} step 7, {@code ChashCensus.danglingPointers}) now
     * call this ONE implementation, closing the drift hazard both prior
     * copies independently hardcoding the three dim tables carried (T2
     * critique-t76bp-rekey-gate-2026-08-08 [21807] ROUND 3, condition (3):
     * "neither copy derives from CHUNK_TABLES ... a fourth dim table is
     * the realistic drift trigger"). {@link #CHUNK_TABLES} is a
     * String-typed table-name list built for raw-SQL composition
     * elsewhere in this class (contentCollapseDelete iterates it as a
     * loop variable at call sites); the three generated-Tables constants
     * below are typed jOOQ handles for the SAME three tables and cannot be
     * driven off that String list without a name-to-generated-class lookup
     * that would be its own drift surface — kept as the explicit three,
     * tied to CHUNK_TABLES only by this comment. See RawSqlGateTest's
     * {@code chunkTablesCanary_fourthDimNeedsAllSitesToldChecklistAbove}
     * for the full fourth-dim checklist (nexus-4okz4 increment 2).
     */
    // No SANCTIONED RAW comment: pure DSL, no raw-SQL string executed here.
    public static Integer danglingManifestCountDsl(org.jooq.DSLContext ctx) {
        return ctx.selectCount()
            .from(CATALOG_DOCUMENT_CHUNKS)
            .where(DSL.notExists(ctx.selectOne().from(CHUNKS_384)
                    .where(CHUNKS_384.CHASH.eq(CATALOG_DOCUMENT_CHUNKS.CHASH))))
            .and(DSL.notExists(ctx.selectOne().from(CHUNKS_768)
                    .where(CHUNKS_768.CHASH.eq(CATALOG_DOCUMENT_CHUNKS.CHASH))))
            .and(DSL.notExists(ctx.selectOne().from(CHUNKS_1024)
                    .where(CHUNKS_1024.CHASH.eq(CATALOG_DOCUMENT_CHUNKS.CHASH))))
            .fetchOne(0, Integer.class);
    }
}
