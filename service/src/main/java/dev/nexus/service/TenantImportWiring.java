// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.sun.net.httpserver.Filter;
import com.sun.net.httpserver.HttpServer;
import dev.nexus.service.db.CollectionRegistry;
import dev.nexus.service.db.TenantExportSessions;
import dev.nexus.service.db.TenantImportSessions;
import dev.nexus.service.http.TenantImportHandler;

import javax.sql.DataSource;
import java.util.List;
import java.util.function.Supplier;

/**
 * The engine's tenant import sessions and what {@link NexusService} does with them (RDR-248 Step 5, bead
 * nexus-csayy.6), kept out of that file: the sessions, the "this engine holds the one-engine-per-database lock" fact the
 * routes refuse without ({@code Main} declares it after it took the lock; an in-process test service declares it for
 * its own reasons), the route registration, the tenant reset evictor, and the test seam that replaces the sessions.
 */
public final class TenantImportWiring {

    private volatile TenantImportSessions sessions;
    private volatile boolean singletonLockHeld;

    /** Builds the sessions over {@code exports} (the cap they share) and registers the routes behind {@code authFilter}. */
    void register(HttpServer server, List<? extends Filter> authFilter, DataSource dataSource, Supplier<TenantExportSessions> exports,
                  Supplier<Long> schemaChangesetCount) {
        this.sessions = new TenantImportSessions(exports, dataSource, TenantImportSessions.Config.fromEnv(System::getenv),
            CollectionRegistry::evictTenant, this::singletonLockHeld, schemaChangesetCount);
        server.createContext(TenantImportHandler.PATH, new TenantImportHandler(this::sessions, exports))
            .getFilters().addAll(authFilter);
    }

    /** Declares that this engine holds the one-engine-per-database lock. */
    public void declareSingletonLockHeld(boolean held) {
        this.singletonLockHeld = held;
    }

    /** What {@link #declareSingletonLockHeld} last declared. */
    public boolean singletonLockHeld() {
        return singletonLockHeld;
    }

    /** The sessions the routes use now. */
    public TenantImportSessions sessions() {
        return sessions;
    }

    /** Test seam: replace the sessions (a test's own bounds and retired list); the old ones close. */
    public void sessions(TenantImportSessions replacement) {
        var old = this.sessions;
        this.sessions = replacement;
        old.close();
    }

    /** The tenant reset evictor: ends the tenant's import session; returns how many. */
    int evictTenant(String tenant) {
        return sessions.evictTenant(tenant);
    }

    /** Engine stop: ends every session, so no held transaction outlives the pool. */
    void close() {
        sessions.close();
    }
}
