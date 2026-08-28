// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

/**
 * nexus-v3w9n Amendment 2 — tumbler grammar, enforced at the engine's HTTP
 * API boundary rather than by a schema CHECK.
 *
 * <p><strong>Why the boundary, not the schema (yet).</strong> Amendment 1
 * (T1 scratch 4cd73175) already corrected the grammar itself from a
 * numeric-content rule to a pure SEGMENT-COUNT rule: an owner prefix is
 * exactly 2 non-empty, dot-free, dot-separated segments (e.g. {@code "1.7"}
 * or {@code "bt.1"}); a document tumbler is 3 or more. A schema {@code
 * CHECK} enforcing that was built and gated (nexus-v3w9n round 2), but the
 * full engine suite measurement (T1 58945409: 1959 tests, 331 broken
 * across 46+ classes) showed the engine's own test corpus is shaped
 * 1-segment-owner / 2-segment-document throughout — raw-SQL fixtures and
 * shared scaffolding included, not just the handful of files a syntactic
 * census could find. Amendment 2 (T1 6ba1a27d, owner decision) defers the
 * schema {@code CHECK}s to nexus-ia69x (fixture-normalization bead) and
 * enforces the SAME grammar here instead: every EXTERNAL producer enters
 * through {@link CatalogHandler}'s HTTP routes, and the internal minting
 * path ({@code ownerPrefix + "." + seq}) already conforms by construction,
 * so the boundary is where an illegal shape can actually be introduced
 * today.
 *
 * <p>Content need not be numeric — that is the Python client's {@code
 * Tumbler.parse} concern (int-segmented), unchanged and unaffected by this
 * class. A segment must be non-empty AND non-blank: {@code "1. "} (a
 * trailing-space segment) is refused exactly like {@code "1."} (an empty
 * one) — fix round 1 (code-review-expert Important finding), since a bare
 * {@code isEmpty()} check let a whitespace-padded segment through.
 *
 * <h2>Route coverage (fix round 1, substantive-critic ship-blocker)</h2>
 *
 * <p>Every {@link CatalogHandler} route that reads {@code "tumbler"} or
 * {@code "tumbler_prefix"} from a request body or query string, enumerated
 * mechanically (grep {@code CatalogHandler} for both key literals) and
 * classified VALIDATED (the route WRITES a new address — an unchecked
 * shape here is exactly how the two 1.1/1.2 phantom rows this bead exists
 * to fix were created) or LOOKUP-ONLY (the route addresses an EXISTING
 * row for a read, update-by-address, or soft-delete/deactivate — a
 * malformed value there simply matches nothing, 404s, or updates zero
 * rows; it can never CREATE an illegal address). A future route that
 * accepts an explicit tumbler/tumbler_prefix and inserts or upserts a row
 * belongs in the VALIDATED column, not the LOOKUP-ONLY one — the
 * {@code /import/document} gap this round closed was exactly a route that
 * should have been in the first column but was left in neither.
 *
 * <table border="1">
 * <caption>tumbler / tumbler_prefix field usage across CatalogHandler</caption>
 * <tr><th>Route</th><th>Field</th><th>Classification</th></tr>
 * <tr><td>POST /register (legacy, owner half)</td><td>tumbler_prefix</td><td>VALIDATED</td></tr>
 * <tr><td>POST /register (legacy, document half)</td><td>tumbler</td><td>VALIDATED</td></tr>
 * <tr><td>POST /owners/upsert</td><td>tumbler_prefix</td><td>VALIDATED</td></tr>
 * <tr><td>POST /import/owner (per row)</td><td>tumbler_prefix</td><td>VALIDATED</td></tr>
 * <tr><td>POST /import/document (per row)</td><td>tumbler</td><td>VALIDATED (fix round 1 — was the ship-blocker gap)</td></tr>
 * <tr><td>POST /doc/register</td><td>owner_prefix (as tumbler_prefix), tumbler (optional)</td><td>VALIDATED</td></tr>
 * <tr><td>POST /doc/register_many</td><td>owner_prefix (as tumbler_prefix), per-doc tumbler (optional)</td><td>VALIDATED</td></tr>
 * <tr><td>GET /show</td><td>tumbler (query)</td><td>LOOKUP-ONLY</td></tr>
 * <tr><td>POST /update</td><td>tumbler (body)</td><td>LOOKUP-ONLY</td></tr>
 * <tr><td>DELETE /delete</td><td>tumbler (query or body)</td><td>LOOKUP-ONLY</td></tr>
 * <tr><td>GET /links</td><td>tumbler (query)</td><td>LOOKUP-ONLY</td></tr>
 * <tr><td>GET /link_query</td><td>tumbler (query)</td><td>LOOKUP-ONLY</td></tr>
 * <tr><td>GET /resolve_chunk</td><td>tumbler (query)</td><td>LOOKUP-ONLY</td></tr>
 * <tr><td>POST /owners/deactivate</td><td>tumbler_prefix (body)</td><td>LOOKUP-ONLY</td></tr>
 * <tr><td>POST /owners/reactivate</td><td>tumbler_prefix (body)</td><td>LOOKUP-ONLY</td></tr>
 * <tr><td>POST /owners/head_hash</td><td>tumbler_prefix (body)</td><td>LOOKUP-ONLY</td></tr>
 * <tr><td>GET /owners/show</td><td>tumbler_prefix (query)</td><td>LOOKUP-ONLY</td></tr>
 * </table>
 */
final class TumblerGrammar {

    private TumblerGrammar() {
    }

    /** True if {@code s} is exactly 2 non-empty, non-blank, dot-free, dot-separated segments. */
    static boolean isOwnerPrefix(String s) {
        String[] segments = segments(s);
        return segments != null && segments.length == 2;
    }

    /** True if {@code s} is 3 or more non-empty, non-blank, dot-free, dot-separated segments. */
    static boolean isDocumentTumbler(String s) {
        String[] segments = segments(s);
        return segments != null && segments.length >= 3;
    }

    /**
     * {@code s} split on {@code '.'}, or {@code null} if {@code s} is
     * {@code null}/blank or any segment is empty or whitespace-only (a
     * leading, trailing, or doubled dot; or a segment like {@code " "}).
     */
    private static String[] segments(String s) {
        if (s == null || s.isBlank()) {
            return null;
        }
        String[] parts = s.split("\\.", -1);
        for (String part : parts) {
            if (part.isBlank()) {
                return null;
            }
        }
        return parts;
    }
}
