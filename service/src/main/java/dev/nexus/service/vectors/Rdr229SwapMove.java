// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

/**
 * RDR-229 P3.3c (nexus-p2m1x.21): the text of each table's swap move, the one data move {@link LeafIndexPass} registers at run time
 * as the last step of P3.3b. PLACEHOLDER: P3.3b (nexus-p2m1x.20) lands this class with the contract the two beads agreed and a body
 * that satisfies the registry's shape, so the pass can register a row that the worker accepts. Bob's P3.3c commit replaces the body of
 * {@link #spec} (the real template, verification and on_complete, with the sizing values of the P3.2 fork run) and keeps the
 * signature.
 *
 * <p>The placeholder move is inert on purpose: its template re-stamps rows whose {@code collection_id} is still NULL from
 * {@code collection_id} itself, so it writes no row, its verification finds none, and it has no on_complete. A build that carries it
 * and not P3.3c's body is not a release candidate: both land in the one engine cut (P3.3), and a store that ran the placeholder to
 * completion would never register the real row (registration is one INSERT per label, {@code WHERE NOT EXISTS}).
 *
 * <p><b>The guard.</b> {@link #PLACEHOLDER} is true while this body is the placeholder, and {@code Main.startLeafIndexPass} starts no pass
 * while it is, so an engine built from this class registers no inert swap row. Bob's P3.3c commit (nexus-p2m1x.21) deletes the constant
 * together with the placeholder body; {@code Main} then stops compiling until the guard is removed in that same commit.
 */
public final class Rdr229SwapMove {

    private Rdr229SwapMove() { /* static */ }

    /** True while {@link #spec} is the inert placeholder. P3.3c (nexus-p2m1x.21) deletes this constant with the placeholder body. */
    public static final boolean PLACEHOLDER = true;

    /**
     * The seven fields of a swap move's row beyond its label, driving table and changeset_id, which {@link LeafIndexPass} supplies.
     * {@code onComplete} may be null (no on_complete).
     */
    public record Spec(String template, int chunkSize, int sleepMs, int statementTimeoutMs, int verifyStatementTimeoutMs,
                       String verification, String onComplete) { }

    /** The bound of the exclusive on_complete statements, in milliseconds; the lock bound is half of it (Sam's ask C, 1400 ms). */
    public static final int STATEMENT_TIMEOUT_MS = 1400;

    /**
     * The spec of {@code table}'s swap move.
     *
     * @throws IllegalArgumentException for a table other than {@code chunks} and {@code taxonomy_centroids}
     */
    public static Spec spec(String table) {
        if (!"chunks".equals(table) && !"taxonomy_centroids".equals(table)) {
            throw new IllegalArgumentException("no swap move for table " + table);
        }
        String template = "UPDATE <driving_table> SET collection_id = t.collection_id WHERE t.collection_id IS NULL AND <chunking_clause>";
        String verification = "SELECT 1 FROM nexus." + table + " t WHERE t.collection_id IS NULL AND (t.tenant_id = $1 OR $1 IS NULL)";
        return new Spec(template, 1000, 2000, STATEMENT_TIMEOUT_MS, 60000, verification, null);
    }
}
