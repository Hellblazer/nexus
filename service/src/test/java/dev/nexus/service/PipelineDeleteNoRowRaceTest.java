// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.PDF_PIPELINE;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.datamove.Gate3;
import dev.nexus.service.db.PipelineRepository;
import dev.nexus.service.db.TenantScope;
import org.junit.jupiter.api.Test;

import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.sql.Connection;
import java.util.concurrent.atomic.AtomicBoolean;
import javax.sql.DataSource;

/**
 * RDR-229 R7 follow-up (linda, row 21): {@code PipelineRepository#deleteForCollection} for a label with NO registry row finds the
 * pipeline runs by the name they recorded and an id no live registry row holds. Its tolerant registry read takes no lock when there is no
 * row, so the label can be registered by another request between that read and the DELETE; an {@code id = idOf(label)} arm would then
 * reach the NEW collection's runs without the key share a writer takes. The fix drops that arm: only the dangling-id runs match.
 *
 * <p>The race is made deterministic with a DataSource wrapper that, on the first prepare of the DELETE (so after the tolerant read, before
 * the statement runs), registers the label as a new collection and files a run under it, on its own connection.
 */
class PipelineDeleteNoRowRaceTest extends Rdr229P35cRestFixture {

    private static final Gate3 LATCHED = () -> true;

    /** A DataSource over {@link #svcDs} that runs {@code hook} once, just before the first DELETE on pdf_pipeline is prepared. */
    private DataSource hookedBeforeThePipelineDelete(Runnable hook) {
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
                        && sql.toLowerCase().startsWith("delete from") && sql.contains("pdf_pipeline") && fired.compareAndSet(false, true)) {
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

    private int runs(String hash) throws Exception {
        return su(ctx -> ctx.fetchCount(PDF_PIPELINE, PDF_PIPELINE.TENANT_ID.eq(TENANT).and(PDF_PIPELINE.CONTENT_HASH.eq(hash))));
    }

    @Test
    void aLabelRegisteredBetweenTheTolerantReadAndTheDelete_keepsItsNewRuns_andTheDanglingOnesGo() throws Exception {
        String label = register(TENANT, "code", "race");
        String oldHash = "p35c-race-old-" + seq.incrementAndGet();
        String newHash = "p35c-race-new-" + seq.incrementAndGet();
        var latched = new PipelineRepository(tenantScope, LATCHED);
        assertThat(latched.create(TENANT, oldHash, "/p35c/race-old.pdf", label, true).status()).isEqualTo("created");
        deleteRegistryRow(TENANT, label);   // the client deleted the row first: the run keeps an id no registry row holds
        assertThat(registryId(TENANT, label)).as("setup: no registry row for the label").isNull();

        var racing = new PipelineRepository(new TenantScope(hookedBeforeThePipelineDelete(() -> {
            try {
                su(ctx -> {
                    PgContainerHelper.insertCollection(ctx, TENANT, label);   // another request registers the label ...
                    return null;
                });
                // ... and files a run under it, as its own writer does
                assertThat(latched.create(TENANT, newHash, "/p35c/race-new.pdf", label, true).status()).isEqualTo("created");
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        })), LATCHED);

        int removed = racing.deleteForCollection(TENANT, label);

        assertThat(registryId(TENANT, label)).as("the hook ran: the label is now a collection").isNotNull();
        assertThat(runs(newHash)).as("the new collection's run is untouched").isEqualTo(1);
        assertThat(runs(oldHash)).as("the dangling-id run is deleted").isZero();
        assertThat(removed).isEqualTo(1);
    }

    /** The unraced no-row cleanup is as before: the dangling-id runs go, and nothing else is touched. */
    @Test
    void withNoRaceTheDanglingRunsGo_andAnotherLabelsRunsStay() throws Exception {
        String label = register(TENANT, "code", "norace");
        String other = register(TENANT, "code", "other");
        String hash = "p35c-norace-" + seq.incrementAndGet();
        String otherHash = "p35c-norace-other-" + seq.incrementAndGet();
        var latched = new PipelineRepository(tenantScope, LATCHED);
        latched.create(TENANT, hash, "/p35c/n.pdf", label, true);
        latched.create(TENANT, otherHash, "/p35c/o.pdf", other, true);
        deleteRegistryRow(TENANT, label);

        assertThat(latched.deleteForCollection(TENANT, label)).isEqualTo(1);
        assertThat(runs(hash)).isZero();
        assertThat(runs(otherHash)).isEqualTo(1);
    }
}
