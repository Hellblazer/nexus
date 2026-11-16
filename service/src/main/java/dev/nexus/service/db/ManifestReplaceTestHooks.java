// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/** Test-only switches of {@link ManifestReplace}: run the old delete-and-insert in the same JVM, or lower its cap. */
public final class ManifestReplaceTestHooks {

    private ManifestReplaceTestHooks() {
    }

    /** True makes every manifest replace take the delete-and-insert path. */
    public static void decline(boolean decline) {
        ManifestReplace.declineForTests = decline;
    }

    /** A positive value replaces the cap on changed rows; zero restores it. */
    public static void maxChangedRows(int rows) {
        ManifestReplace.maxChangedRowsForTests = rows;
    }

    /** The payload columns {@link ManifestReplace} compares and updates, for the test that guards them against the table. */
    public static java.util.Set<String> payloadColumns() {
        return ManifestReplace.PAYLOAD_COLUMNS;
    }
}
