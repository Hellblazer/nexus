// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import java.util.LinkedHashSet;
import java.util.List;
import java.util.function.Supplier;

/**
 * The tenants the owner deadline rebuild visits (RDR-252): the tenants that hold a service token (the reaper's list)
 * together with every tenant in {@code nexus.tuple_tenants}, the tenants that hold tuple rows, so a tenant whose tokens
 * are gone but whose {@code board/ci} owner rows remain still has its deadline re-armed. Either read that throws
 * propagates: the rebuild's census is then not full and the rebuild retries ({@link BoardOwnerWiring#start}).
 */
public final class BoardOwnerTenants implements Supplier<List<String>> {

    private final Supplier<List<String>> tokenTenants;
    private final Supplier<List<String>> tupleTenants;

    public BoardOwnerTenants(Supplier<List<String>> tokenTenants, Supplier<List<String>> tupleTenants) {
        this.tokenTenants = tokenTenants;
        this.tupleTenants = tupleTenants;
    }

    /** The union, the token tenants first, each tenant once. */
    @Override
    public List<String> get() {
        var out = new LinkedHashSet<String>(tokenTenants.get());
        out.addAll(tupleTenants.get());
        return List.copyOf(out);
    }
}
