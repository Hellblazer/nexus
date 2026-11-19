// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.NexusService;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.TestHttp;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TokenStore;
import dev.nexus.service.http.GroupTestEnv;
import dev.nexus.service.tuples.TemplateRegistry;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.function.Supplier;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-252 Step 3: the hook is wired. A {@code --done} write through {@code /v1/tuples/out} on a real engine
 * ({@code NexusService}, the adapter's {@code board-ci}-scoped credential) produces the owner row on
 * {@code board/ci/<base>--owner} for each of two bases, with no repository name in the engine.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class OwnerStamperWiringTest {

    private static final String BOOT = "boot-owner-stamper-wiring";
    private static final ObjectMapper MAPPER = new ObjectMapper();

    StamperEnv se;
    NexusService service;
    final HttpClient http = TestHttp.client();

    @BeforeAll
    void up() throws Exception {
        se = new StamperEnv();
        try (Connection su = se.env.ids.pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.seedServiceToken(dsl, BOOT, TenantConstants.DEFAULT_TENANT, TokenStore.ROOT_TOKEN_LABEL,
                TokenStore.SCOPE_ROOT, null, null);
        }
        TemplateRegistry registry = TemplateRegistry.loadAtBoot(null, null, NexusService.SWEEP_INTERVAL_HOURS * 3600L);
        service = new NexusService(0, BOOT, se.env.ids.ds, null, null, null, null, registry);
        service.start();
    }

    @AfterAll
    void down() throws Exception {
        if (service != null) {
            service.stop();
        }
        se.close();
    }

    private void seedToken(String raw, String tenant, String scope) throws Exception {
        try (Connection su = se.env.ids.pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), raw, tenant, "wiring-" + scope, scope, null, null);
        }
    }

    private int out(String bearer, Map<String, Object> body) throws Exception {
        var r = http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/tuples/out")
            .header("Authorization", "Bearer " + bearer).header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body))).build(), HttpResponse.BodyHandlers.ofString());
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        return r.statusCode();
    }

    /** The stamper runs after the response, on the request's thread: wait for the row, bounded. */
    private static <T> T eventually(Supplier<T> read, java.util.function.Predicate<T> ok) throws Exception {
        long end = System.nanoTime() + java.util.concurrent.TimeUnit.SECONDS.toNanos(20);
        T v = read.get();
        while (!ok.test(v) && System.nanoTime() < end) {
            Thread.sleep(50);
            v = read.get();
        }
        return v;
    }

    @Test
    void aDoneWriteThroughTheTupleRouteProducesTheOwnerRowForTwoBases() throws Exception {
        se.env.ids.clock.set(Instant.now());   // the engine reads the system clock: the seats' leases must be fresh by it
        String t = se.tenant();
        String adapter = "adapter-" + UUID.randomUUID();
        seedToken(adapter, t, TokenStore.SCOPE_BOARD_CI);
        String l = se.env.seat(t, "lead");
        String a = se.env.seat(t, "alpha");
        String c = se.env.seat(t, "carol");
        var body = GroupTestEnv.body("push-train", l, List.of("nexus-develop", "nexus-main"));
        assertThat(se.env.form(t, l, body).status()).isEqualTo(200);
        assertThat(se.env.join(t, a, "push-train", "member").status()).isEqualTo(200);
        assertThat(se.env.join(t, c, "push-train", "member").status()).isEqualTo(200);

        for (String base : List.of("nexus-develop", "nexus-main")) {
            se.push(t, base, "X", "G0", "sess-alpha");
            se.push(t, base, "Y", "X", "sess-carol");
            out(adapter, Map.of("subspace", "board/ci/" + base + "--done", "keys", Map.of("topic", base + "--done"),
                "dims", Map.of("from", "github", "kind", "sha", "sha", "Y", "outcome", "failure"),
                "body", "{\"state\":\"concluded\",\"sha\":\"Y\"}", "nonce", UUID.randomUUID().toString()));
        }

        for (String base : List.of("nexus-develop", "nexus-main")) {
            var rows = eventually(() -> se.ownerRows(t, base), r -> !r.isEmpty());
            assertThat(rows).as(base).hasSize(1);
            assertThat(rows.getFirst().kind()).isEqualTo("owner");
            assertThat(rows.getFirst().from()).isEqualTo("engine");
            assertThat(rows.getFirst().sha()).isEqualTo("Y");
            assertThat(rows.getFirst().body()).containsEntry("owner", a).containsEntry("next", c);
        }
    }

    @Test
    void aCancelledOrAnUnrelatedWritePostsNothing() throws Exception {
        se.env.ids.clock.set(Instant.now());
        String t = se.tenant();
        String adapter = "adapter-" + UUID.randomUUID();
        seedToken(adapter, t, TokenStore.SCOPE_BOARD_CI);
        String l = se.env.seat(t, "lead");
        String a = se.env.seat(t, "alpha");
        se.formEnding(t, l, null, a);
        se.push(t, StamperEnv.BASE, "X", "G0", "sess-alpha");
        se.push(t, StamperEnv.BASE, "Y", "X", "sess-alpha");

        out(adapter, Map.of("subspace", "board/ci/nexus-develop--done", "keys", Map.of("topic", "nexus-develop--done"),
            "dims", Map.of("from", "github", "kind", "sha", "sha", "X", "outcome", "cancelled"),
            "body", "{\"state\":\"concluded\",\"sha\":\"X\"}", "nonce", UUID.randomUUID().toString()));
        out(adapter, Map.of("subspace", "board/ci/nexus-develop", "keys", Map.of("topic", "nexus-develop"),
            "dims", Map.of("from", "github", "kind", "job"),
            "body", "{\"state\":\"completed\",\"conclusion\":\"failure\"}", "nonce", UUID.randomUUID().toString()));
        // The hook runs after the response on the request's thread; give a wrongly posted row time to land.
        Thread.sleep(500);
        assertThat(se.ownerRows(t, StamperEnv.BASE)).as("nothing posted for a cancelled or an unrelated write").isEmpty();

        // A red on a different sha: its row is the only one, so the earlier writes posted nothing.
        out(adapter, Map.of("subspace", "board/ci/nexus-develop--done", "keys", Map.of("topic", "nexus-develop--done"),
            "dims", Map.of("from", "github", "kind", "sha", "sha", "Y", "outcome", "failure"),
            "body", "{\"state\":\"concluded\",\"sha\":\"Y\"}", "nonce", UUID.randomUUID().toString()));

        eventually(() -> se.ownerRows(t, StamperEnv.BASE), r -> !r.isEmpty());
        Thread.sleep(300);
        var rows = se.ownerRows(t, StamperEnv.BASE);
        assertThat(rows).hasSize(1);
        assertThat(rows.getFirst().sha()).isEqualTo("Y");
    }

    @Test
    void aFailedAnswerStillStampsTheOwnerRow() throws Exception {
        se.env.ids.clock.set(Instant.now());
        String t = se.tenant();
        String adapter = "adapter-" + UUID.randomUUID();
        seedToken(adapter, t, TokenStore.SCOPE_BOARD_CI);
        String l = se.env.seat(t, "lead");
        String a = se.env.seat(t, "alpha");
        se.formEnding(t, l, null, a);
        se.push(t, StamperEnv.BASE, "X", "G0", "sess-alpha");

        // The first send of the answer fails (a client that went away): the row is committed, so it is stamped anyway.
        java.util.concurrent.atomic.AtomicBoolean failed = new java.util.concurrent.atomic.AtomicBoolean();
        dev.nexus.service.http.HttpUtil.setSendObserverForTests(ex -> {
            if (ex.getRequestURI().getPath().equals("/v1/tuples/out") && failed.compareAndSet(false, true)) {
                throw new java.io.UncheckedIOException(new java.io.IOException("simulated send failure"));
            }
        });
        try {
            var r = http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/tuples/out")
                .header("Authorization", "Bearer " + adapter).header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(Map.of(
                    "subspace", "board/ci/nexus-develop--done", "keys", Map.of("topic", "nexus-develop--done"),
                    "dims", Map.of("from", "github", "kind", "sha", "sha", "X", "outcome", "failure"),
                    "body", "{\"state\":\"concluded\",\"sha\":\"X\"}", "nonce", UUID.randomUUID().toString()))))
                .build(), HttpResponse.BodyHandlers.ofString());
            assertThat(failed.get()).as("the send failure was injected").isTrue();
            assertThat(r.statusCode()).isNotEqualTo(200);
        } finally {
            dev.nexus.service.http.HttpUtil.setSendObserverForTests(null);
        }
        var rows = eventually(() -> se.ownerRows(t, StamperEnv.BASE), r -> !r.isEmpty());
        assertThat(rows).hasSize(1);
        assertThat(rows.getFirst().sha()).isEqualTo("X");
    }

    /** The identity token a client holds: the one a refresh of its binding returns (the generation it holds now). */
    private String identityToken(String tenant, String id) {
        int generation = ((Number) se.env.ids.show(tenant, id).get("generation")).intValue();
        var r = se.env.ids.bind(tenant, id, "refresh", generation, Map.of());
        assertThat(r.status()).as(String.valueOf(r.body())).isEqualTo(200);
        return r.str("identity_token");
    }

    private void pushOverHttp(String bearer, String identityToken, String sha, String prev, String from) throws Exception {
        var body = Map.of("subspace", "board/ci/nexus-develop--push", "keys", Map.of("topic", "nexus-develop--push"),
            "dims", Map.of("from", from, "kind", "push", "sha", sha),
            "body", "{\"base\":\"" + prev + "\",\"n\":1}", "nonce", UUID.randomUUID().toString());
        var r = http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/tuples/out")
            .header("Authorization", "Bearer " + bearer).header("Content-Type", "application/json")
            .header("X-Nexus-Identity-Token", identityToken)
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body))).build(), HttpResponse.BodyHandlers.ofString());
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
    }

    @Test
    void aPushWrittenWithAnIdentityTokenIsStampedWithTheIdentitysSessionAndTheOwnerRowNamesTheIdentity() throws Exception {
        se.env.ids.clock.set(Instant.now());
        String t = se.tenant();
        String adapter = "adapter-" + UUID.randomUUID();
        String pusher = "pusher-" + UUID.randomUUID();
        seedToken(adapter, t, TokenStore.SCOPE_BOARD_CI);
        seedToken(pusher, t, TokenStore.SCOPE_TENANT);
        String l = se.env.seat(t, "lead");
        String a = se.env.seat(t, "alpha");
        String c = se.env.seat(t, "carol");
        se.form(t, l, a, c);

        // No stamp is written by hand: the engine stamps sender_session from the identity token (IdentityFence).
        pushOverHttp(pusher, identityToken(t, a), "X", "G0", "alpha");
        pushOverHttp(pusher, identityToken(t, c), "Y", "X", "carol");
        assertThat(se.tuples.rdp(t, "board/ci/nexus-develop--push", Map.of(), 10, null))
            .extracting(dev.nexus.service.db.TupleRepository.TupleRow::senderSession)
            .as("the engine stamped each push with its identity's session").containsExactly("sess-alpha", "sess-carol");

        out(adapter, Map.of("subspace", "board/ci/nexus-develop--done", "keys", Map.of("topic", "nexus-develop--done"),
            "dims", Map.of("from", "github", "kind", "sha", "sha", "Y", "outcome", "failure"),
            "body", "{\"state\":\"concluded\",\"sha\":\"Y\"}", "nonce", UUID.randomUUID().toString()));

        var rows = eventually(() -> se.ownerRows(t, StamperEnv.BASE), r -> !r.isEmpty());
        assertThat(rows).hasSize(1);
        assertThat(rows.getFirst().body()).as("the owner is the identity whose session stamped the earlier push")
            .containsEntry("owner", a).containsEntry("next", c);
    }
}
