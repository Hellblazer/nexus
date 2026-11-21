// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * The minted collection label (RDR-229 item 8): {@code c} followed by decimal digits. It is reserved to
 * the engine, so the label rule (P3.6) refuses a user label of this form, and the minter (P3.8) renders
 * it. Both read this one class, so the form they agree on cannot drift.
 */
public final class CollectionLabel {

    private CollectionLabel() {
    }

    /**
     * Whether {@code label} is of the minted form: exactly {@code c} followed by one or more ASCII digits.
     * Leading zeros count ({@code c007} is reserved too), so a user cannot hold a near-twin of a minted
     * label. Case is not folded and no non-ASCII digit counts.
     */
    public static boolean isMintedForm(String label) {
        if (label == null || label.length() < 2 || label.charAt(0) != 'c') {
            return false;
        }
        for (int i = 1; i < label.length(); i++) {
            char ch = label.charAt(i);
            if (ch < '0' || ch > '9') {
                return false;
            }
        }
        return true;
    }

    /** The minted label for {@code id}: {@code "c" + id}, never padded. {@code id} must be positive. */
    public static String mintedLabel(long id) {
        if (id <= 0) {
            throw new IllegalArgumentException("a minted label's id must be positive, got " + id);
        }
        return "c" + id;
    }
}
