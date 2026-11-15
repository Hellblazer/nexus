// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/** Test-only switch of {@link ManifestReplace}: lets a measurement run the old delete-and-insert in the same JVM. */
public final class ManifestReplaceTestHooks {

    private ManifestReplaceTestHooks() {
    }

    /** True makes every manifest replace take the delete-and-insert path. */
    public static void decline(boolean decline) {
        ManifestReplace.declineForTests = decline;
    }
}
