// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.CollectionRouteFixture.Resp;
import dev.nexus.service.db.ReembedJobRepository;
import dev.nexus.service.db.ReembedJobRepository.Job;
import dev.nexus.service.db.TenantScope;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.CollectionRouteFixture.label;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23), linda's FK-less ledger rule for the re-embed guard: a rename or a re-home refuses a
 * collection a re-embed job in a lock state holds, matched by the job end's registry id, or, for an end whose id no longer
 * names a live registry row, by the label the job recorded. After a one-row rename (simulated as the superuser does it, as
 * {@code CollectionRehomeByIdTest} does) the job still names the old label and the row's id, so the guard must hold the
 * collection under its NEW label.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ReembedGuardByIdTest {

    CollectionRouteFixture fx;
    ReembedJobRepository jobs;
    int n;

    @BeforeAll
    void startAll() {
        fx = new CollectionRouteFixture();
        fx.gate3.set(true);
        jobs = new ReembedJobRepository(new TenantScope(fx.svcDs));
    }

    @AfterAll
    void stopAll() {
        if (fx != null) fx.close();
    }

    private String registered(String stem) {
        String l = label("rjg-" + stem + "-" + (n++));
        fx.register(l);
        return l;
    }

    private String oneRowRename(String from, String stem) {
        long id = fx.idOf(from);
        String to = label("rjg-" + stem + "-" + (n++));
        fx.asSuperuser(ctx -> ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, to)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(id)).execute());
        assertThat(fx.idOf(to)).as("setup: the renamed row is the same row").isEqualTo(id);
        return to;
    }

    private Job startJob(String source, String target) {
        return jobs.start(fx.tenant, source, target, null, false, false);
    }

    private Resp rehome(String source, String target) {
        return fx.post("/v1/catalog/collections/rehome", "{\"source\":" + json(source) + ",\"target\":" + json(target) + "}");
    }

    private Resp rename(String from, String to) {
        return fx.post("/v1/catalog/collections/rename", "{\"old_name\":" + json(from) + ",\"new_name\":" + json(to) + "}");
    }

    private void assertHeld(Resp r, Job job) {
        assertThat(r.status()).as(r.body()).isEqualTo(409);
        assertThat(r.json().path("job_id").asText()).as("it names the job: %s", r.body()).isEqualTo(job.jobId().toString());
    }

    @Test
    void afterAOneRowRename_aJobHoldsItsSourceUnderTheNewLabel() {
        String src = registered("s");
        String tgt = registered("t");
        String other = registered("o");
        Job job = startJob(src, tgt);
        String src2 = oneRowRename(src, "s2");

        assertHeld(rehome(src2, other), job);
        assertHeld(rehome(other, src2), job);
        assertHeld(rename(src2, label("rjg-fresh-" + (n++))), job);
    }

    @Test
    void afterAOneRowRename_aJobHoldsItsTargetUnderTheNewLabel() {
        String src = registered("ts");
        String tgt = registered("tt");
        String other = registered("to");
        Job job = startJob(src, tgt);
        String tgt2 = oneRowRename(tgt, "tt2");

        assertHeld(rehome(other, tgt2), job);
        assertHeld(rename(tgt2, label("rjg-fresh-" + (n++))), job);
    }

    @Test
    void aCollectionNoJobHoldsIsNotRefused() {
        String src = registered("us");
        String tgt = registered("ut");
        startJob(src, tgt);
        String free = registered("free");
        String free2 = registered("free2");

        Resp r = rehome(free, free2);

        assertThat(r.status()).as("an unrelated re-home is not held: %s", r.body()).isEqualTo(200);
    }
}
