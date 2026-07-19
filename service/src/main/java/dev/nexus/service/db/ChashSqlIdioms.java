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
     */
    public static String contentRekeyUpdate(String table) {
        return "UPDATE " + table + " c SET chash = "
            + DIGEST.replace("chunk_text", "c.chunk_text") + " "
            + "WHERE c.chunk_text <> '' "
            + "  AND c.chash IS DISTINCT FROM " + DIGEST.replace("chunk_text", "c.chunk_text");
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

    /** In-txn verify: manifest rows pointing at no content row in any dim. */
    public static String danglingManifestCount() {
        return "SELECT count(*) FROM nexus.catalog_document_chunks m "
            + "WHERE NOT EXISTS (SELECT 1 FROM nexus.chunks_384 c WHERE c.chash = m.chash) "
            + "  AND NOT EXISTS (SELECT 1 FROM nexus.chunks_768 c WHERE c.chash = m.chash) "
            + "  AND NOT EXISTS (SELECT 1 FROM nexus.chunks_1024 c WHERE c.chash = m.chash)";
    }
}
