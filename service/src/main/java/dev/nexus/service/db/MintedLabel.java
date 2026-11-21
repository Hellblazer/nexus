// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.util.function.LongSupplier;
import java.util.function.Predicate;

/**
 * The id and label of a collection the engine mints because it was registered with no label (RDR-229 item 8, P3.8,
 * bead nexus-p2m1x.28), chosen before the one insert that writes both.
 *
 * <p>A pure seam, so the choice is tested without the schema: the caller passes the column's own sequence as
 * {@code nextId} (P3.1's {@code collection_id}, read with {@code nextval(pg_get_serial_sequence(...))}; the insert
 * then writes that id explicitly, which the column's nullable-with-default form and its final {@code BY DEFAULT}
 * identity both accept with no {@code OVERRIDING SYSTEM VALUE}, marlowe 2026-11-21) and
 * {@code labelTaken} as "a live row of this tenant already has this label". When the label of the drawn id is taken
 * (a pre-existing label of the minted form; P3.6 refuses new ones), the next id is drawn and tried, so
 * {@code label == "c" + collectionId} holds for every minted row and a skipped id is never used (design-gate
 * proposal, T2 {@code nexus_rdr/229-p3.8-design-gate-proposal-2026-11-21}, pending Sam).
 *
 * <p>The label rendering itself belongs to P3.6's {@code CollectionLabel.mintedLabel} (elsinore), which also holds the
 * minted-form predicate; {@link #render} delegates to it once that class lands.
 */
public final class MintedLabel {

    /** How many ids a mint draws before it refuses: each extra draw means a live label of the minted form. */
    public static final int MAX_DRAWS = 16;

    /** The id and label a mint inserts together. */
    public record Choice(long collectionId, String label) {
    }

    private MintedLabel() {
    }

    /**
     * Draw ids from {@code nextId} until one's label is free, at most {@link #MAX_DRAWS} times.
     *
     * @throws IllegalStateException when every draw's label is taken (a 409 on the route)
     */
    public static Choice choose(LongSupplier nextId, Predicate<String> labelTaken) {
        throw new UnsupportedOperationException("RDR-229 P3.8: not yet implemented");
    }

    /** Placeholder for {@code CollectionLabel.mintedLabel} (P3.6): {@code "c"} and the id in decimal. */
    static String render(long collectionId) {
        return "c" + collectionId;
    }
}
