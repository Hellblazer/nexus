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
import static dev.nexus.service.jooq.nexus.Tables.CHASH_ALIAS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
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
 * else textually. The fix, applied to {@link #digestField} and
 * {@link #oldRefField} this increment: use {@code DSL.inline(literal)}
 * instead of {@code DSL.val(literal)} for the fragment's embedded
 * constants ({@code "UTF8"}, {@code "hex"}) — {@code DSL.inline} renders
 * the (properly jOOQ-escaped) literal directly into the SQL text, so every
 * occurrence is byte-for-byte identical and PostgreSQL's structural check
 * passes. Safe here specifically because these are FIXED PROTOCOL
 * CONSTANTS (encoding names), never derived from caller/user input — never
 * inline a value that could originate outside this file's own literals.
 * Before composing a NEW multi-occurrence DSL statement (a DISTINCT ON, a
 * GROUP BY reused in the SELECT list) from fragments in this class, check
 * whether the fragment embeds a {@code DSL.val(...)} literal; if it does
 * and you need the SAME expression object more than once in one statement,
 * either reach for an already-inlined twin or inline the literal locally
 * the same way — do not rediscover this by way of a PostgreSQL error.
 */
// SANCTIONED RAW (nexus-jxizy.10.2, narrowed nexus-4okz4 increment 2,
// further narrowed increment 3): increment 2 converted contentRekeyUpdate,
// frecencyAliasAggregate, and residualMismatchCount to typed DSL
// (contentRekeyUpdateDsl / frecencyAliasAggregateDsl / residualMismatchCountDsl
// — no allowlist entry needed, pure DSL, no raw-SQL string executed) and
// deleted the now-dead raw-string forms outright. Increment 3 did the same
// for chashOldBytes -> chashOldBytesField (StagingPromoteOps' two alias-
// INSERT sites were its only callers) — REMOVED from the allowlist below,
// dead sanction entry avoided. What remains sanctioned: contentCollapseDelete
// (the ctid/array_agg ORDER BY keeper-selection idiom has no jOOQ DSL form —
// array-subscript of an ordered array_agg), and refreshAliasStats (ANALYZE
// is maintenance DDL with no jOOQ DSL form at all, plus a privilege probe
// over pg_class/has_table_privilege system catalogs codegen does not
// cover). Never serving-path.
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
     * a table add). Verified (nexus-4okz4 increment 1 finding, reaffirmed
     * this lane): grep-confirmed ZERO executable main-source consumers of
     * this constant — {@code contentCollapseDelete}'s call sites are fed
     * from {@code RekeyOps.DIMS}' {@code d.name()}, never from here — so
     * this is a checklist artifact, not a live drift surface itself; keep
     * it in lockstep with {@link
     * dev.nexus.service.vectors.DimTables#CHUNKS_TABLE_NAME} by construction
     * (both derive from the same "one unified table" fact) rather than by a
     * runtime cross-check.
     */
    public static final List<String> CHUNK_TABLES =
        List.of(dev.nexus.service.vectors.DimTables.CHUNKS_TABLE_NAME);

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
     * Typed rendering of the canonical string-to-bytes function for
     * {@code chash_alias.old_bytes} (rdr180-20 changeset) — added
     * nexus-4okz4 increment 3, REPLACING the deleted string-returning
     * {@code chashOldBytes(String)} outright: grep-verified zero remaining
     * callers of the string form once StagingPromoteOps' two alias-INSERT
     * sites (the only callers) converted to typed DSL — same single-homed
     * discipline as {@link #contentRekeyUpdateDsl} etc. in increment 2, no
     * forked variant left behind. {@code DSL.function}'s schema-qualified
     * name form (verified: jOOQ's own javadoc documents {@code name} as
     * "possibly qualified") renders the identical {@code
     * nexus.chash_old_bytes(...)} call.
     */
    public static Field<byte[]> chashOldBytesField(Field<String> refExpr) {
        return DSL.function("nexus.chash_old_bytes", byte[].class, refExpr);
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
     *
     * <p>{@code DSL.inline("UTF8")}, NOT {@code DSL.val("UTF8")} (nexus-4okz4
     * increment 3 pin, found by StagingPromoteOpsIntegrationTest, not
     * anticipated by review): {@code "UTF8"} is a fixed protocol constant,
     * never user input, so inlining it is injection-safe — and it MUST be
     * inline because callers that reference this SAME returned expression
     * object more than once within one rendered statement (e.g. a
     * {@code SELECT DISTINCT ON (...)}  whose {@code ORDER BY} leads with
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
     * Typed rendering of {@link #OLD_REF_LEMMA} (same in-store-only domain
     * constraint — see class docstring): {@code CASE WHEN
     * octet_length(chash) = 16 THEN encode(chash, 'hex') ELSE
     * convert_from(chash, 'UTF8') END}. Added nexus-4okz4 increment 2;
     * inlined literals (not bind parameters) added increment 3 — same
     * multiple-occurrence hazard and injection-safety argument as
     * {@link #digestField}'s javadoc.
     */
    public static Field<String> oldRefField(Field<byte[]> chash) {
        return DSL.when(
                DSL.function("octet_length", Integer.class, chash).eq(16),
                DSL.function("encode", String.class, chash, DSL.inline("hex")))
            .otherwise(DSL.function("convert_from", String.class, chash, DSL.inline("UTF8")));
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
     * content row. SINGLE-HOMED (nexus-4okz4 increment 1, collapsing the
     * twin renderings the nexus-t76bp jOOQ-DSL pass left split across
     * {@code RekeyOps} (a private DSL copy) and this class's former raw-SQL
     * {@code danglingManifestCount()} string — both callers ({@code
     * RekeyOps.rekey} step 6, {@code StagingPromoteOps.finalizeTenant} step
     * 7, {@code ChashCensus.danglingPointers}) call this ONE
     * implementation.
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
     * authority for that one table name. Callers ({@link #existsInAnyDim}
     * is the sibling boolean form) are D3-committed against this unified
     * shape already (RekeyOps.orphanCond, StagingPromoteOps' converged
     * {@code canonExistsDsl} call sites) — this method's body is what makes
     * that commitment correct. See RawSqlGateTest's rewritten channel
     * checklist for what a FUTURE dimension-count change (a column add,
     * not a table add) needs to touch instead.
     */
    // No SANCTIONED RAW comment: pure DSL, no raw-SQL string executed here.
    public static Integer danglingManifestCountDsl(org.jooq.DSLContext ctx) {
        return ctx.selectCount()
            .from(CATALOG_DOCUMENT_CHUNKS)
            .where(DSL.notExists(ctx.selectOne().from(CHUNKS)
                    .where(CHUNKS.CHASH.eq(CATALOG_DOCUMENT_CHUNKS.CHASH))))
            .fetchOne(0, Integer.class);
    }

    /**
     * The {@code EXISTS} predicate proving a chash is CANONICAL (a content
     * row for it exists) — nexus-4okz4 increment 4 shared home for the
     * idiom, with {@code ChashCensus}' dangling-pointer scan as its first
     * caller. {@code StagingPromoteOps} privately carried a byte-for-byte
     * identical copy ({@code canonExistsDsl}, increment 3) until increment
     * 5 converged its three call sites onto this method and deleted the
     * private copy outright — SINGLE-HOMED, same discipline as
     * {@link #danglingManifestCountDsl}'s twin collapse in increment 1: no
     * forked variant left behind for any caller. Every caller, old or new,
     * reaches for THIS one rather than re-deriving the predicate locally.
     *
     * <p>RDR-191 Phase 4 (repoint-batch lane D5, bead nexus-o8dil.41 item
     * 4): was a three-way {@code OR} disjunction across
     * {@code chunks_384/768/1024} — with those collapsed into the ONE
     * unified {@code nexus.chunks} table, "exists in any dim" is now a
     * SINGLE {@code EXISTS} against that one table (dim is a column
     * choice, not a table identity a chash could independently exist
     * under). {@code danglingManifestCountDsl} above renders the same
     * fact as a {@code NOT EXISTS} count-query conjunct rather than
     * composing this boolean form — structurally different shape, kept as
     * its own rendering, same as before. See RawSqlGateTest's rewritten
     * channel checklist for what a FUTURE dimension-count change (a
     * column add, not a table add) needs to touch instead of this method.
     */
    public static Condition existsInAnyDim(DSLContext ctx, Field<byte[]> chash) {
        return DSL.exists(ctx.selectOne().from(CHUNKS).where(CHUNKS.CHASH.eq(chash)));
    }
}
