// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.sun.net.httpserver.Filter;
import com.sun.net.httpserver.HttpServer;
import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.ManifestCheck;
import dev.nexus.service.db.ManifestCheckAdmin;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.http.ManifestCheckHandler;

import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.function.Supplier;

/**
 * The manifest CHECK operator steps of RDR-236 (nexus-6pfgb.42) and what {@link NexusService} does with them, kept out of
 * that file: one route family per {@link ManifestCheck} ({@code /v1/admin/manifest-chunk-index-check/*},
 * {@code /v1/admin/manifest-labels-check/*}), each behind the same auth filter, and the late binding of each check's
 * admin-connection DDL ({@link ManifestCheckAdmin}). Nothing in the engine calls a route: {@code Main} binds the admin
 * connection once ({@link #bind}); until then every route answers 503, and an operator's request does the rest.
 */
public final class ManifestCheckWiring {

    private final Map<ManifestCheck, ManifestCheckAdmin> admins = new ConcurrentHashMap<>();

    /** Mounts one route family per check behind {@code authFilter}; each reads its admin at request time. */
    void register(HttpServer server, List<? extends Filter> authFilter, Supplier<List<String>> tenants, TenantScope scope) {
        for (ManifestCheck check : ManifestCheck.values()) {
            server.createContext(check.path(), new ManifestCheckHandler(check, () -> admins.get(check), tenants, scope))
                .getFilters().addAll(authFilter);
        }
    }

    /** Binds every check to the admin connection; {@code Main} calls it once. */
    public void bind(AdminConnection connection) {
        for (ManifestCheck check : ManifestCheck.values()) {
            admins.put(check, new ManifestCheckAdmin(connection, check));
        }
    }

    /** Test seam: replaces one check's admin; null unbinds it (its routes answer 503). */
    public void admin(ManifestCheck check, ManifestCheckAdmin admin) {
        if (admin == null) {
            admins.remove(check);
        } else {
            admins.put(check, admin);
        }
    }
}
