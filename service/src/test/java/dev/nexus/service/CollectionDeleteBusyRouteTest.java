// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.CollectionRouteFixture.Resp;
import dev.nexus.service.db.ReembedJobRepository;
import dev.nexus.service.db.TenantScope;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.CollectionRouteFixture.label;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a row 14 (nexus-p2m1x.23): {@code POST /v1/catalog/collections/delete} answers 409 {@code collection_busy} while a re-embed job in a
 * non-terminal state holds the collection as its source or target, by label and by {@code collection_id}, naming the job; nothing is removed, and
 * the same request is 200 once the job is terminal. The repository's own refusal, the race with a starting job and the runner's exemption are in
 * {@code CollectionDeleteFenceTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionDeleteBusyRouteTest {

    CollectionRouteFixture fx;
    ReembedJobRepository jobs;

    @BeforeAll
    void startAll() throws Exception {
        fx = new CollectionRouteFixture();
        jobs = new ReembedJobRepository(new TenantScope(fx.svcDs));
    }

    @AfterAll
    void stopAll() {
        if (fx != null) fx.close();
    }

    @Test
    void deleteOfACollectionAJobHolds_is409CollectionBusy_byLabelAndById_thenSucceedsOnceTheJobIsTerminal() {
        String src = label("busy-src");
        String tgt = label("busy-tgt");
        fx.register(src, tgt);
        long tgtId = fx.idOf(tgt);
        var job = jobs.start(fx.tenant, src, tgt, null, false, false);

        Resp byLabel = fx.post("/v1/catalog/collections/delete", "{\"name\":" + json(src) + "}");
        Resp byId = fx.post("/v1/catalog/collections/delete", "{\"collection_id\":" + tgtId + "}");

        assertThat(byLabel.status()).as(byLabel.body()).isEqualTo(409);
        assertThat(byLabel.reason()).isEqualTo("collection_busy");
        assertThat(byLabel.json().path("job_id").asText()).isEqualTo(job.jobId().toString());
        assertThat(byLabel.json().path("collection").asText()).isEqualTo(src);
        assertThat(byId.status()).as(byId.body()).isEqualTo(409);
        assertThat(byId.reason()).isEqualTo("collection_busy");
        assertThat(fx.exists(src)).as("nothing was removed").isTrue();
        assertThat(fx.exists(tgt)).isTrue();

        jobs.applyTransition(fx.tenant, job.jobId(), ReembedJobRepository.PLANNED, null, ReembedJobRepository.ABORTED,
            ReembedJobRepository.Transition.NONE);

        Resp after = fx.post("/v1/catalog/collections/delete", "{\"name\":" + json(src) + "}");
        assertThat(after.status()).as(after.body()).isEqualTo(200);
        assertThat(fx.exists(src)).isFalse();
    }
}
