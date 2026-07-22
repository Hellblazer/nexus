/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import java.util.List;

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
 * writer. The bytes-to-string recovery lemma ({@link #OLD_REF_LEMMA}) stays
 * an in-store-only idiom with its documented CONSTRAINED domain (values
 * that lived under the pre-flip 32-char CHECK): staging always carries the
 * original ref alongside, so the promote path never recovers strings from
 * bytes.
 */
// SANCTIONED RAW (nexus-jxizy.10.2, RawSqlGateTest allowlist): these are the
// shared fragments of the two one-shot migration ops (RekeyOps sanction
// heritage, nexus-jxizy.6) — sha256() over chunk_text, ctid/array_agg keeper
// selection, reversibility-lemma CASE, GREATEST-merge aggregates and
// verification anti-joins have no jOOQ DSL form and never run on the
// serving path.
public final class ChashSqlIdioms {

    private ChashSqlIdioms() {
    }

    /** The three dim-partitioned content tables, in canonical order. */
    public static final List<String> CHUNK_TABLES =
        List.of("nexus.chunks_384", "nexus.chunks_768", "nexus.chunks_1024");

    /** The one digest formula: full sha256 over the row's chunk_text. */
    public static final String DIGEST = "sha256(convert_to(chunk_text, 'UTF8'))";

    /**
     * Reversibility-lemma rendering of a CONVERTED key's original string
     * (in-store domain only — see class docstring). {@code %1$s} is the
     * bytea column reference.
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
     * Phase-B content rekey for one chunk table: survivors whose key
     * mismatches their digest. Verbatim RekeyOps step (4) phase B.
     *
     * <p>ALSO re-stamps {@code metadata.chunk_text_hash} to mirror the new
     * key (critic-1010, nexus-jxizy.10.10): the citation resolver's
     * where-filter reads that RDR-086 metadata field. Producers have always
     * stamped the FULL 64-hex there, so for serving-path rows this is a
     * value-identical no-op — the stamp's real work is BACKFILLING rows
     * that never carried one (pre-RDR-086 writers) and guaranteeing the
     * mirror invariant by construction, keeping the in-store rekey path
     * indistinguishable from {@link StagingPromoteOps}' promote output.
     */
    public static String contentRekeyUpdate(String table) {
        String digest = DIGEST.replace("chunk_text", "c.chunk_text");
        return "UPDATE " + table + " c SET chash = " + digest + ", "
            + "metadata = coalesce(c.metadata, '{}'::jsonb) "
            + "  || jsonb_build_object('chunk_text_hash', encode(" + digest + ", 'hex')) "
            + "WHERE c.chunk_text <> '' "
            + "  AND c.chash IS DISTINCT FROM " + digest;
    }

    /**
     * Frecency per-target group aggregate over old rows joined to the alias
     * (both collapse directions ride this; keeper keyed by min(chunk_id),
     * NOT ctid — the 3c catch: an UPDATE changes ctid). Verbatim RekeyOps
     * step (5). Aliased {@code g}.
     */
    public static String frecencyAliasAggregate() {
        return "(SELECT a.new_chash, min(o.chunk_id) AS keep_id, "
            + "        max(o.frecency_score) AS fs, max(o.miss_count) AS mc, "
            + "        max(o.last_hit_at) AS lh, max(o.embedded_at) AS ea, "
            + "        max(o.ttl_days) AS td "
            + "   FROM nexus.frecency o JOIN nexus.chash_alias a "
            + "     ON o.chunk_id = a.old_ref GROUP BY a.new_chash) g";
    }

    /** In-txn verify: residual digest-mismatch count for one chunk table. */
    public static String residualMismatchCount(String table) {
        return "SELECT count(*) FROM " + table + " WHERE chunk_text <> '' "
            + "AND chash IS DISTINCT FROM " + DIGEST;
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

    /** In-txn verify: manifest rows pointing at no content row in any dim. */
    public static String danglingManifestCount() {
        return "SELECT count(*) FROM nexus.catalog_document_chunks m "
            + "WHERE NOT EXISTS (SELECT 1 FROM nexus.chunks_384 c WHERE c.chash = m.chash) "
            + "  AND NOT EXISTS (SELECT 1 FROM nexus.chunks_768 c WHERE c.chash = m.chash) "
            + "  AND NOT EXISTS (SELECT 1 FROM nexus.chunks_1024 c WHERE c.chash = m.chash)";
    }
}
