// SPDX-License-Identifier: AGPL-3.0-or-later
package dev.nexus.service;

/**
 * A database {@link PgContainerHelper} just created. {@code CollectionRegistry}'s caches are process-wide and carry no database
 * identity (an engine's {@code start()} and {@code stop()} empty them, row 15), so a test that builds a repository on its own database
 * without an engine, after another class cached the same tenant and id from another database, would be answered from that cache.
 * Emptying it here, where every test database is created, closes that for the test JVM; production serves one engine per JVM.
 */
final class FreshDatabases {
    private FreshDatabases() { }

    static <C> C fresh(C container) {
        dev.nexus.service.db.CollectionRegistry.evictAll();
        return container;
    }
}
