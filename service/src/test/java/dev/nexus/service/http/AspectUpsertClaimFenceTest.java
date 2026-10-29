// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.AtomicWriteTestBase;
import dev.nexus.service.NexusService;
import dev.nexus.service.db.AspectRepository;
import dev.nexus.service.db.TupleRepository;
import dev.nexus.service.tuples.TemplateRegistry;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.net.URI;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.LinkedHashMap;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.TUPLES;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * Bead nexus-5r2sc.2.5 (RDR-235 E5): {@code POST /v1/aspects/upsert} with {@code claim_id} and
 * {@code claimant} answers the claim's refusals as HTTP codes, not 500s, and writes nothing when it
 * refuses: 400 for one field of the pair, 403 for another claimant or another document's task, 404 for a
 * claim that is not live; 200 with a live claim. Test Plan scenario 5.
 */
class AspectUpsertClaimFenceTest extends AtomicWriteTestBase {

    private static final ObjectMapper JSON = new ObjectMapper();
    private static final Map<String, String> PATTERN = Map.of("queue", "aspects");

    private AspectHandler handler;
    private AspectRepository aspects;
    private TupleRepository tuples;

    @BeforeAll
    void wire() {
        aspects = new AspectRepository(tenantScope);
        handler = new AspectHandler(aspects);
        tuples = new TupleRepository(tenantScope,
                TemplateRegistry.loadAtBoot(null, null, NexusService.SWEEP_INTERVAL_HOURS * 3600L));
    }

    @BeforeEach
    void emptyQueue() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).deleteFrom(TUPLES)
                    .where(TUPLES.TENANT_ID.eq(TENANT).and(TUPLES.SUBSPACE.eq("aspects"))).execute();
        }
    }

    private record Resp(int status, String body) {
        JsonNode json() throws Exception {
            return JSON.readTree(body);
        }
    }

    private Resp post(Map<String, Object> body) throws Exception {
        CapturingExchange ex = new CapturingExchange("POST", URI.create("/v1/aspects/upsert"),
                JSON.writeValueAsString(body));
        RequestContext.set(new RequestContext.Principal(TENANT, null, false, false, "tenant", "test-credential-hash"));
        try {
            handler.handle(ex);
        } finally {
            RequestContext.clear();
        }
        return new Resp(ex.status, ex.bodyString());
    }

    private TupleRepository.ClaimedTuple seedAndClaim(Fx f, String claimant) throws Exception {
        tuples.out(TENANT, "aspects", PATTERN, Map.of(),
                JSON.writeValueAsString(Map.of("collection", f.collection(), "source_path", "s.pdf",
                        "doc_id", f.docId(), "content_hash", "h")),
                f.docId(), null);
        return tuples.inp(TENANT, "aspects", PATTERN, claimant, (Long) 600L).orElseThrow();
    }

    private Map<String, Object> aspectBody(Fx f, String claimId, String claimant) {
        Map<String, Object> b = new LinkedHashMap<>();
        b.put("collection", f.collection());
        b.put("source_path", "s.pdf");
        b.put("problem_formulation", "p");
        b.put("proposed_method", "m");
        b.put("experimental_results", "r");
        b.put("confidence", 0.9);
        b.put("extracted_at", "2026-10-29T00:00:00.000000Z");
        b.put("model_version", "v1");
        b.put("extractor_name", "scholarly-paper");
        b.put("doc_id", f.docId());
        if (claimId != null) b.put("claim_id", claimId);
        if (claimant != null) b.put("claimant", claimant);
        return b;
    }

    private boolean written(Fx f) {
        return aspects.getAspect(TENANT, f.collection(), "s.pdf").isPresent();
    }

    @Test
    void oneOfThePair_is400NamingBothFields_andWritesNothing() throws Exception {
        Fx f = fixture("h4");
        var a = seedAndClaim(f, "wA:0");

        Resp noClaimant = post(aspectBody(f, a.claimId(), null));
        Resp noClaimId = post(aspectBody(f, null, "wA:0"));

        for (Resp r : new Resp[] {noClaimant, noClaimId}) {
            assertThat(r.status()).as(r.body()).isEqualTo(400);
            assertThat(r.json().get("error").asText()).contains("claim_id").contains("claimant");
        }
        assertThat(written(f)).isFalse();
    }

    @Test
    void anotherClaimant_is403_andWritesNothing() throws Exception {
        Fx f = fixture("h3");
        var a = seedAndClaim(f, "wA:0");

        Resp r = post(aspectBody(f, a.claimId(), "wB:0"));

        assertThat(r.status()).as(r.body()).isEqualTo(403);
        assertThat(r.json().get("error").asText()).isEqualTo("ClaimOwnership");
        assertThat(written(f)).isFalse();
    }

    @Test
    void anotherDocumentsTask_is403TaskMismatch_andWritesNothing() throws Exception {
        Fx held = fixture("hm");
        Fx other = fixture("ho");
        var a = seedAndClaim(held, "wA:0");

        Resp r = post(aspectBody(other, a.claimId(), "wA:0"));

        assertThat(r.status()).as(r.body()).isEqualTo(403);
        assertThat(r.json().get("error").asText()).isEqualTo("ClaimTaskMismatch");
        assertThat(written(other)).isFalse();
    }

    @Test
    void aClaimThatIsNotLive_is404_andWritesNothing() throws Exception {
        Fx f = fixture("h5");
        var a = seedAndClaim(f, "wA:0");
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).update(TUPLES)
                    .set(TUPLES.LEASE_UNTIL, OffsetDateTime.now(ZoneOffset.UTC).minusMinutes(5))
                    .where(TUPLES.TENANT_ID.eq(TENANT).and(TUPLES.CLAIM_ID.eq(a.claimId()))).execute();
        }

        Resp lapsed = post(aspectBody(f, a.claimId(), "wA:0"));
        Resp unknown = post(aspectBody(f, "no-such-claim", "wA:0"));

        for (Resp r : new Resp[] {lapsed, unknown}) {
            assertThat(r.status()).as(r.body()).isEqualTo(404);
            assertThat(r.json().get("error").asText()).isEqualTo("ClaimNotFound");
        }
        assertThat(written(f)).isFalse();
    }

    @Test
    void aLiveClaim_is200AndWrites_andNoClaimFieldsStillWrite() throws Exception {
        Fx f = fixture("h2");
        var a = seedAndClaim(f, "wA:0");

        Resp r = post(aspectBody(f, a.claimId(), "wA:0"));

        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(r.json().get("written").asBoolean()).isTrue();
        assertThat(written(f)).isTrue();

        Fx g = fixture("h1");
        Resp claimless = post(aspectBody(g, null, null));
        assertThat(claimless.status()).as(claimless.body()).isEqualTo(200);
        assertThat(written(g)).isTrue();
    }
}
