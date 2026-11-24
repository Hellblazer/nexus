// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CHASH_REMAP;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.datamove.Gate3;
import dev.nexus.service.datamove.IdUniqueIndexPass;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.RemapRepository;
import dev.nexus.service.db.RemapRepository.RemapEntry;
import dev.nexus.service.db.TenantScope;
import org.jooq.Field;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.sql.Connection;
import java.util.List;
import java.util.concurrent.atomic.AtomicBoolean;
import javax.sql.DataSource;

/**
 * RDR-229 row 21 (linda): {@code RemapRepository#clearLeg} for a leg with NO registry row finds the leg's remap rows by the name they
 * recorded and an id no live registry row holds. The registry read of a label with no row takes no lock, so the label can be registered by
 * another request between that read and the DELETE; the history rule's {@code id = idOf(label)} arm would then reach the NEW collection's
 * rows without the key share a writer takes. A no-row leg now matches only the dangling rows.
 *
 * <p>The race is made deterministic as in {@link PipelineDeleteNoRowRaceTest}: a DataSource wrapper registers the label and files a remap
 * entry for it, on its own connection, at the first prepare of the DELETE on chash_remap.
 */
class RemapClearLegNoRowRaceTest extends Rdr229P35cRestFixture {

    private static final Gate3 LATCHED = () -> true;

    /** The id arbiter a latched upsert needs: P3.4's chash_remap index, created from its spec (as {@code Rdr229P35cTaxoFixRoundTest} does). */
    @BeforeAll
    void buildArbiterIndex() throws Exception {
        var spec = IdUniqueIndexPass.SPECS.stream().filter(sp -> sp.table().equals("chash_remap") && sp.unique()).findFirst().orElseThrow();
        List<Field<?>> cols = java.util.Arrays.stream(spec.columns().split(",\\s*")).<Field<?>>map(c -> DSL.field(DSL.name(c))).toList();
        su(ctx -> ctx.createUniqueIndexIfNotExists(spec.index()).on(DSL.table(DSL.name("nexus", "chash_remap")), cols).execute());
    }

    private DataSource hookedBeforeTheRemapDelete(Runnable hook) {
        AtomicBoolean fired = new AtomicBoolean();
        ClassLoader cl = getClass().getClassLoader();
        return (DataSource) Proxy.newProxyInstance(cl, new Class<?>[] {DataSource.class}, (p, m, a) -> {
            Object result = call(m, svcDs, a);
            if (!m.getName().equals("getConnection")) {
                return result;
            }
            Connection real = (Connection) result;
            return Proxy.newProxyInstance(cl, new Class<?>[] {Connection.class}, (cp, cm, ca) -> {
                if (cm.getName().equals("prepareStatement") && ca != null && ca.length > 0 && ca[0] instanceof String sql
                        && sql.toLowerCase().startsWith("delete from") && sql.contains("chash_remap") && fired.compareAndSet(false, true)) {
                    hook.run();
                }
                return call(cm, real, ca);
            });
        });
    }

    private static Object call(Method m, Object target, Object[] args) throws Throwable {
        try {
            return m.invoke(target, args);
        } catch (InvocationTargetException e) {
            throw e.getCause();
        }
    }

    private static RemapEntry fact(String source, String oldId, String target) {
        return new RemapEntry(source, oldId, Chash.ofText("r21-" + oldId).toHex(), target, "p35c");
    }

    private int entries(String oldId) throws Exception {
        return su(ctx -> ctx.fetchCount(CHASH_REMAP, CHASH_REMAP.TENANT_ID.eq(TENANT).and(CHASH_REMAP.OLD_ID.eq(oldId))));
    }

    /** The leg that has no row: {@code source} when {@code sourceLeg}, else {@code target}; the other leg is registered throughout. */
    private void raceOnALeg(boolean sourceLeg) throws Exception {
        String other = register(TENANT, "code", "rcl-o");
        String label = register(TENANT, "code", "rcl-l");
        String oldId = "r21-dangling-" + seq.incrementAndGet();
        String newId = "r21-new-" + seq.incrementAndGet();
        var latched = new RemapRepository(tenantScope, LATCHED);
        latched.recordBatch(TENANT, List.of(sourceLeg ? fact(label, oldId, other) : fact(other, oldId, label)));
        deleteRegistryRow(TENANT, label);   // the client deleted the row first: the entry keeps an id no registry row holds
        assertThat(registryId(TENANT, label)).as("setup: no registry row for the label").isNull();

        var racing = new RemapRepository(new TenantScope(hookedBeforeTheRemapDelete(() -> {
            try {
                su(ctx -> {
                    PgContainerHelper.insertCollection(ctx, TENANT, label);   // another request registers the label ...
                    return null;
                });
                latched.recordBatch(TENANT, List.of(sourceLeg ? fact(label, newId, other) : fact(other, newId, label)));   // ... and files an entry
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        })), LATCHED);

        int removed = sourceLeg ? racing.clearLeg(TENANT, label, other) : racing.clearLeg(TENANT, other, label);

        assertThat(registryId(TENANT, label)).as("the hook ran: the label is now a collection").isNotNull();
        assertThat(entries(newId)).as("the new collection's entry is untouched").isEqualTo(1);
        assertThat(entries(oldId)).as("the dangling entry is deleted").isZero();
        assertThat(removed).isEqualTo(1);
    }

    @Test
    void aSourceLegRegisteredBetweenTheReadAndTheDelete_keepsItsNewEntries() throws Exception {
        raceOnALeg(true);
    }

    @Test
    void aTargetLegRegisteredBetweenTheReadAndTheDelete_keepsItsNewEntries() throws Exception {
        raceOnALeg(false);
    }

    /** A leg that HAS a row keeps the history rule: its dangling old-id rows and its current rows both clear. */
    @Test
    void aLegWithARegistryRowStillClearsItsDanglingAndItsCurrentEntries() throws Exception {
        String t = register(TENANT, "code", "rcl-t");
        String label = register(TENANT, "code", "rcl-h");
        String oldId = "r21-old-" + seq.incrementAndGet();
        String newId = "r21-cur-" + seq.incrementAndGet();
        var latched = new RemapRepository(tenantScope, LATCHED);
        latched.recordBatch(TENANT, List.of(fact(label, oldId, t)));
        deleteRegistryRow(TENANT, label);
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, TENANT, label);
            return null;
        });
        latched.recordBatch(TENANT, List.of(fact(label, newId, t)));

        assertThat(latched.clearLeg(TENANT, label, t)).isEqualTo(2);
        assertThat(entries(oldId)).isZero();
        assertThat(entries(newId)).isZero();
    }
}
