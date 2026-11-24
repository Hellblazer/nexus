// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import dev.nexus.service.AtomicWriteTestBase;
import dev.nexus.service.db.CollectionRegistry;
import dev.nexus.service.db.TenantScope;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.concurrent.atomic.AtomicInteger;
import javax.sql.DataSource;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 R7, linda's round (b): the retired-label guard reads the registry ONCE per request for all the labels its cache lacks, not once
 * per label. A DataSource proxy counts the connections the guard opens (a {@code TenantScope.withTenant} call is one connection, one
 * transaction); the semantics, a refusal for a retired label with no successor, live first, are the same as the per-label loop's.
 */
class RetiredLabelReadGuardReadCountTest extends AtomicWriteTestBase {

    private final AtomicInteger connections = new AtomicInteger();

    private DataSource counting() {
        ClassLoader cl = getClass().getClassLoader();
        return (DataSource) Proxy.newProxyInstance(cl, new Class<?>[] {DataSource.class}, (p, m, a) -> {
            if (m.getName().equals("getConnection")) {
                connections.incrementAndGet();
            }
            try {
                return m.invoke(svcDs, a);
            } catch (InvocationTargetException e) {
                throw e.getCause();
            }
        });
    }

    private <T> T su(java.util.function.Function<org.jooq.DSLContext, T> f) throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            return f.apply(DSL.using(c, SQLDialect.POSTGRES));
        }
    }

    private String liveLabel() {
        String l = "code__rc" + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
        registerCollection(l);
        return l;
    }

    /** A retired label that names no live successor: its own id in superseded_by_id, a dead label in superseded_by. */
    private String retiredNoSuccessor() throws Exception {
        String l = liveLabel();
        su(ctx -> ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, "code__dead" + seq.incrementAndGet() + "__minilm-l6-v2-384__v1")
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, CATALOG_COLLECTIONS.COLLECTION_ID)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(l)).execute());
        return l;
    }

    @Test
    void nColdLabels_costOneRegistryRead_notN() throws Exception {
        List<String> labels = new ArrayList<>();
        for (int i = 0; i < 6; i++) {
            labels.add(liveLabel());
        }
        labels.add("code__never-registered__minilm-l6-v2-384__v1");
        var rows = CollectionSelectorFilter.registryRows(new CollectionRegistry(), new TenantScope(counting()));
        connections.set(0);

        var refusal = RetiredLabelReadGuard.refusal(rows, TENANT, Set.of(), labels);

        assertThat(refusal).as("live and unregistered labels are not refused").isEmpty();
        assertThat(connections.get()).as("one read for %d cold labels", labels.size()).isEqualTo(1);
    }

    @Test
    void aRetiredLabelAmongColdOnes_isRefusedWithTheNoSuccessorBody_afterOneRead() throws Exception {
        String retired = retiredNoSuccessor();
        List<String> labels = List.of(liveLabel(), liveLabel(), retired, liveLabel());
        var rows = CollectionSelectorFilter.registryRows(new CollectionRegistry(), new TenantScope(counting()));
        connections.set(0);

        var refusal = RetiredLabelReadGuard.refusal(rows, TENANT, Set.of(), labels);

        assertThat(refusal).isPresent();
        assertThat(refusal.get()).contains("\"reason\":\"collection_superseded\"").contains("\"collection\":\"" + retired + "\"")
            .doesNotContain("superseded_by").doesNotContain("successor_collection_id");
        // one read for the labels, one for the retired row's successor lookup (a tombstone only)
        assertThat(connections.get()).isEqualTo(2);
    }

    @Test
    void labelsTheCacheHoldsCostNoRead() throws Exception {
        String a = liveLabel();
        String b = liveLabel();
        var registry = new CollectionRegistry();
        var scope = new TenantScope(counting());
        scope.withTenant(TENANT, ctx -> {   // the handlers' own reads fill the cache
            registry.resolveLabel(ctx, TENANT, a);
            registry.resolveLabel(ctx, TENANT, b);
            return null;
        });
        var rows = CollectionSelectorFilter.registryRows(registry, scope);
        connections.set(0);

        assertThat(RetiredLabelReadGuard.refusal(rows, TENANT, Set.of(), List.of(a, b))).isEmpty();
        assertThat(connections.get()).as("both labels are cached live rows").isZero();
    }
}
