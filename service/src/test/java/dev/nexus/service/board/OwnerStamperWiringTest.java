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

    private HttpResponse<String> post(String bearer, Map<String, Object> body) throws Exception {
        return http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/tuples/out")
            .header("Authorization", "Bearer " + bearer).header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body))).build(), HttpResponse.BodyHandlers.ofString());
    }

    @Test
    void aClientWriteThatSaysFromEngineToTheBoardIsRefusedAndTheStampersOwnRowsStillLand() throws Exception {
        se.env.ids.clock.set(Instant.now());
        String t = se.tenant();
        String adapter = "adapter-" + UUID.randomUUID();
        String client = "client-" + UUID.randomUUID();
        seedToken(adapter, t, TokenStore.SCOPE_BOARD_CI);
        seedToken(client, t, TokenStore.SCOPE_TENANT);
        String l = se.env.seat(t, "lead");
        String a = se.env.seat(t, "alpha");
        String c = se.env.seat(t, "carol");
        se.form(t, l, a, c);
        se.push(t, StamperEnv.BASE, "X", "G0", "sess-alpha");
        se.push(t, StamperEnv.BASE, "Y", "X", "sess-carol");
        String forged = "{\"owner\":\"" + c + "\",\"next\":null,\"reason\":\"earliest_suspect\",\"suspects\":[\"Y\"],"
            + "\"suspects_total\":1,\"episode_at\":\"2026-01-01T00:00:00Z\",\"refresh\":false,\"handoffs\":0}";

        for (String kind : List.of("owner", "clear")) {
            for (boolean replace : List.of(false, true)) {
                var body = new java.util.LinkedHashMap<String, Object>(Map.of("subspace", "board/ci/nexus-develop--owner",
                    "keys", Map.of("topic", "nexus-develop--owner"), "dims", Map.of("from", "engine", "kind", kind, "sha", "Y"),
                    "body", forged, "nonce", UUID.randomUUID().toString()));
                if (replace) {
                    body.put("mode", "replace");
                }
                var r = post(client, body);
                assertThat(r.statusCode()).as(kind + " replace=" + replace + ": " + r.body()).isEqualTo(403);
                assertThat(r.body()).contains("reserved_dim");
            }
        }
        var viaAdapter = post(adapter, Map.of("subspace", "board/ci/nexus-develop--done", "keys", Map.of("topic", "nexus-develop--done"),
            "dims", Map.of("from", "engine", "kind", "sha", "sha", "Y", "outcome", "failure"),
            "body", "{\"state\":\"concluded\",\"sha\":\"Y\"}", "nonce", UUID.randomUUID().toString()));
        assertThat(viaAdapter.statusCode()).as("the adapter's credential may not say from=engine either: " + viaAdapter.body()).isEqualTo(403);
        assertThat(se.ownerRows(t, StamperEnv.BASE)).as("no forged row landed").isEmpty();

        // A member's ack carries its own name, not engine's, and still lands; the stamper's own rows land from=engine.
        var ack = post(client, Map.of("subspace", "board/ci/nexus-develop--owner", "keys", Map.of("topic", "nexus-develop--owner"),
            "dims", Map.of("from", a, "kind", "ack", "sha", "Y"), "body", "{}", "nonce", UUID.randomUUID().toString()));
        assertThat(ack.statusCode()).as(ack.body()).isEqualTo(200);
        out(adapter, Map.of("subspace", "board/ci/nexus-develop--done", "keys", Map.of("topic", "nexus-develop--done"),
            "dims", Map.of("from", "github", "kind", "sha", "sha", "Y", "outcome", "failure"),
            "body", "{\"state\":\"concluded\",\"sha\":\"Y\"}", "nonce", UUID.randomUUID().toString()));
        var rows = eventually(() -> se.ownerRows(t, StamperEnv.BASE), r -> !r.isEmpty());
        assertThat(rows).hasSize(1);
        assertThat(rows.getFirst().from()).isEqualTo("engine");
        assertThat(rows.getFirst().body()).containsEntry("owner", a);
    }

    private HttpResponse<String> postTo(String bearer, String path, Map<String, Object> body) throws Exception {
        return http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + path)
            .header("Authorization", "Bearer " + bearer).header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body))).build(), HttpResponse.BodyHandlers.ofString());
    }

    @Test
    void anAckReplyThatSaysFromEngineToTheBoardIsRefusedWithTheClaimStillHeldAndAMembersReplyLands() throws Exception {
        se.env.ids.clock.set(Instant.now());
        String t = se.tenant();
        String client = "client-" + UUID.randomUUID();
        seedToken(client, t, TokenStore.SCOPE_TENANT);
        String l = se.env.seat(t, "lead");
        String a = se.env.seat(t, "alpha");
        se.form(t, l, a);
        String inbox = "ackinbox" + UUID.randomUUID().toString().substring(0, 8);
        var sent = postTo(client, "/v1/tuples/out", Map.of("subspace", "mailbox/" + inbox, "keys", Map.of("to", inbox),
            "dims", Map.of("from", "someone"), "body", "work", "nonce", UUID.randomUUID().toString()));
        assertThat(sent.statusCode()).as(sent.body()).isEqualTo(200);
        var claim = postTo(client, "/v1/tuples/inp", Map.of("subspace", "mailbox/" + inbox, "keys_pattern", Map.of("to", inbox),
            "claimant", "w", "lease_s", 60));
        assertThat(claim.statusCode()).as(claim.body()).isEqualTo(200);
        String claimId = MAPPER.readTree(claim.body()).get("claim_id").asText();
        String forged = "{\"owner\":\"" + a + "\",\"next\":null,\"reason\":\"earliest_suspect\",\"suspects\":[\"Y\"],"
            + "\"suspects_total\":1,\"episode_at\":\"2026-01-01T00:00:00Z\",\"refresh\":false,\"handoffs\":0}";

        var refused = postTo(client, "/v1/tuples/ack", Map.of("claim_id", claimId, "claimant", "w", "reply", Map.of(
            "subspace", "board/ci/nexus-develop--owner", "keys", Map.of("topic", "nexus-develop--owner"),
            "dims", Map.of("from", "engine", "kind", "owner", "sha", "Y"), "body", forged)));

        assertThat(refused.statusCode()).as(refused.body()).isEqualTo(403);
        assertThat(refused.body()).contains("reserved_dim");
        assertThat(se.ownerRows(t, StamperEnv.BASE)).as("no owner row landed").isEmpty();
        assertThat(se.tuples.rdp(t, "board/ci/nexus-develop--owner", Map.of(), 10, null)).as("no row of any kind").isEmpty();

        // The claim is still held by the refused ack: the same claim acks with a member's own name and the reply lands.
        var allowed = postTo(client, "/v1/tuples/ack", Map.of("claim_id", claimId, "claimant", "w", "reply", Map.of(
            "subspace", "board/ci/nexus-develop--owner", "keys", Map.of("topic", "nexus-develop--owner"),
            "dims", Map.of("from", a, "kind", "ack", "sha", "Y"), "body", "{}")));
        assertThat(allowed.statusCode()).as(allowed.body()).isEqualTo(200);
        assertThat(se.tuples.rdp(t, "board/ci/nexus-develop--owner", Map.of(), 10, null))
            .extracting(r -> r.dims().get("from")).containsExactly(a);
    }

    private HttpResponse<String> postAs(String bearer, String identityToken, String path, Map<String, Object> body) throws Exception {
        return postAs(bearer, identityToken, null, path, body);
    }

    private HttpResponse<String> postAs(String bearer, String identityToken, String sessionToken, String path,
                                        Map<String, Object> body) throws Exception {
        var b = TestHttp.request("http://127.0.0.1:" + service.getPort() + path)
            .header("Authorization", "Bearer " + bearer).header("Content-Type", "application/json");
        if (identityToken != null) {
            b.header("X-Nexus-Identity-Token", identityToken);
        }
        if (sessionToken != null) {
            b.header("X-Nexus-T1-Session", sessionToken);
        }
        return http.send(b.POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body))).build(),
            HttpResponse.BodyHandlers.ofString());
    }

    /** How the ack of {@link #ackThenDeadline} is made: whose credential it carries. */
    private enum Via { OWNER_TOKEN, CHILD_TOKEN, NO_CREDENTIAL, MAPPED_MINTED_SESSION, UNMAPPED_MINTED_SESSION }

    /** What an ack over the real route did: its status, the {@code from} the row landed with, the owner's id, and the owner rows after the deadline. */
    private record AckRun(int ackStatus, String ackFrom, String ownerId, List<String> rows) { }

    /**
     * Alpha owns the red on Y; an ack for Y is posted over the real route with {@code from} (null: left out) and
     * the credential {@code via} names (the owner's token, a delegated child's token, none, a minted session the
     * index maps to alpha, a minted session it does not map); the deadline then falls due.
     */
    private AckRun ackThenDeadline(String from, Via via) throws Exception {
        se.env.ids.clock.set(Instant.now());
        String t = se.tenant();
        String client = "client-" + UUID.randomUUID();
        seedToken(client, t, TokenStore.SCOPE_TENANT);
        String l = se.env.seat(t, "lead");
        String a = se.env.seat(t, "alpha");
        String c = se.env.seat(t, "carol");
        se.form(t, l, a, c);
        se.push(t, StamperEnv.BASE, "X", "G0", "sess-alpha");
        se.push(t, StamperEnv.BASE, "Y", "X", "sess-carol");
        se.done(t, StamperEnv.BASE, "Y", "failure");
        assertThat(se.ownerRows(t, StamperEnv.BASE).getFirst().body()).containsEntry("owner", a);
        assertThat(se.env.tokenOf(a)).as("the fixture's bind returned alpha's identity token").isNotBlank();

        String identityToken = null;
        String sessionToken = null;
        switch (via) {
            case OWNER_TOKEN -> identityToken = se.env.tokenOf(a);
            case CHILD_TOKEN -> identityToken = se.env.tokenOf(se.env.delegated(t, a, "alpha-child"));
            case MAPPED_MINTED_SESSION -> sessionToken = mintSession(client, "sess-alpha");
            case UNMAPPED_MINTED_SESSION -> sessionToken = mintSession(client, "sess-nobody-" + UUID.randomUUID());
            case NO_CREDENTIAL -> { }
        }
        Map<String, String> dims = new java.util.LinkedHashMap<>();
        if (from != null) {
            dims.put("from", from);
        }
        dims.put("kind", "ack");
        dims.put("sha", "Y");
        var ack = postAs(client, identityToken, sessionToken, "/v1/tuples/out", Map.of(
            "subspace", "board/ci/nexus-develop--owner", "keys", Map.of("topic", "nexus-develop--owner"), "dims", dims,
            "body", "{}", "nonce", UUID.randomUUID().toString()));
        String landedFrom = se.tuples.rdp(t, "board/ci/nexus-develop--owner", Map.of(), 10, null).stream()
            .filter(r -> "ack".equals(r.dims().get("kind"))).map(r -> r.dims().get("from")).findFirst().orElse(null);

        se.env.ids.clock.advance(java.time.Duration.ofMinutes(21));
        for (String id : new String[] {l, a, c}) {
            se.env.renew(t, id);
        }
        se.stamper.onDeadline(t, StamperEnv.BASE);
        return new AckRun(ack.statusCode(), landedFrom, a,
            se.ownerRows(t, StamperEnv.BASE).stream().map(r -> r.kind() + "/" + r.get("reason")).toList());
    }

    private String mintSession(String bearer, String sessionId) throws Exception {
        var r = postAs(bearer, null, "/v1/sessions/start", Map.of("session_id", sessionId));
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        return MAPPER.readTree(r.body()).get("session_token").asText();
    }

    private static final List<String> HELD = List.of("owner/earliest_suspect");
    private static final List<String> HANDED_OFF = List.of("owner/earliest_suspect", "owner/handoff");

    /**
     * Alpha owns the red on Y. {@code stamp} (alpha's or carol's minted session) posts a {@code disclaim} on Y over the
     * real route; returns the owner rows' kinds and reasons once the stamper has had its turn.
     */
    private List<String> afterADisclaimBy(String stamp) throws Exception {
        se.env.ids.clock.set(Instant.now());
        String t = se.tenant();
        String client = "client-" + UUID.randomUUID();
        seedToken(client, t, TokenStore.SCOPE_TENANT);
        String l = se.env.seat(t, "lead");
        String a = se.env.seat(t, "alpha");
        String c = se.env.seat(t, "carol");
        se.form(t, l, a, c);
        se.push(t, StamperEnv.BASE, "X", "G0", "sess-alpha");
        se.push(t, StamperEnv.BASE, "Y", "X", "sess-carol");
        se.done(t, StamperEnv.BASE, "Y", "failure");
        assertThat(se.ownerRows(t, StamperEnv.BASE).getFirst().body()).containsEntry("owner", a);

        var logger = (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(OwnerStamper.class);
        var logs = new ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent>();
        logs.start();
        logger.addAppender(logs);
        try {
            var disclaim = postAs(client, null, mintSession(client, stamp), "/v1/tuples/out", Map.of(
                "subspace", "board/ci/nexus-develop--owner", "keys", Map.of("topic", "nexus-develop--owner"),
                "dims", Map.of("from", "marginalia", "kind", "disclaim", "sha", "Y"), "body", "{}",
                "nonce", UUID.randomUUID().toString()));
            assertThat(disclaim.statusCode()).as(disclaim.body()).isEqualTo(200);
            // The hook runs after the response, on the request's thread, and logs its outcome when it is done: wait for that
            // line (the observable), whichever way it went, rather than for a time.
            var handled = eventually(
                () -> logs.list.stream().map(e -> e.getFormattedMessage())
                    .filter(m -> m.contains("event=board_owner_disclaim") && m.contains("tenant=" + t)).toList(),
                lines -> !lines.isEmpty());
            assertThat(handled).as("the hook handled the disclaim").hasSize(1);
        } finally {
            logger.detachAppender(logs);
        }
        return se.ownerRows(t, StamperEnv.BASE).stream().map(r -> r.kind() + "/" + r.get("reason")).toList();
    }

    @Test
    void aDisclaimOverTheRealRouteFromTheOwnersSessionHandsTheRedOnAtOnce() throws Exception {
        assertThat(afterADisclaimBy("sess-alpha")).containsExactly("owner/earliest_suspect", "owner/disclaimed");
    }

    @Test
    void aDisclaimOverTheRealRouteFromAnotherSessionChangesNothing() throws Exception {
        assertThat(afterADisclaimBy("sess-carol")).as("control: carol is not the owner").containsExactly("owner/earliest_suspect");
    }

    @Test
    void anAckWithNoFromStampedByTheEngineStopsTheDeadline() throws Exception {
        var run = ackThenDeadline(null, Via.OWNER_TOKEN);

        assertThat(run.ackStatus()).isEqualTo(200);
        assertThat(run.ackFrom()).as("stamped with alpha's identity id, not left out").isEqualTo(run.ownerId());
        assertThat(run.rows()).as("the stamped ack counts: no hand-off row").isEqualTo(HELD);
    }

    @Test
    void anAckThatSaysAnAliasWithATokenIsStampedWithTheIdentityAndStopsTheDeadline() throws Exception {
        var run = ackThenDeadline("marginalia", Via.OWNER_TOKEN);

        assertThat(run.ackStatus()).as("an alias is overwritten, not refused").isEqualTo(200);
        assertThat(run.ackFrom()).as("the label ack-times reads is alpha's identity id").isEqualTo(run.ownerId());
        assertThat(run.rows()).isEqualTo(HELD);
    }

    @Test
    void aDelegatedChildsAckIsLabelledWithTheParentTheDecisionCountsAndStopsTheDeadline() throws Exception {
        var run = ackThenDeadline("marginalia", Via.CHILD_TOKEN);

        assertThat(run.ackStatus()).isEqualTo(200);
        assertThat(run.ackFrom()).as("the parent's id: the engine counts a delegated child's ack for its parent").isEqualTo(run.ownerId());
        assertThat(run.rows()).as("the owner's child acknowledged: no hand-off").isEqualTo(HELD);
    }

    @Test
    void aTokenlessAckFromAMintedSessionTheIndexMapsIsLabelledAndStopsTheDeadline() throws Exception {
        var run = ackThenDeadline("marginalia", Via.MAPPED_MINTED_SESSION);

        assertThat(run.ackStatus()).isEqualTo(200);
        assertThat(run.ackFrom()).isEqualTo(run.ownerId());
        assertThat(run.rows()).isEqualTo(HELD);
    }

    @Test
    void anAckFromASessionTheIndexDoesNotMapKeepsItsFromAndTheDeadlineStillHandsOff() throws Exception {
        var run = ackThenDeadline("marginalia", Via.UNMAPPED_MINTED_SESSION);

        assertThat(run.ackStatus()).isEqualTo(200);
        assertThat(run.ackFrom()).as("nothing maps: from stays as sent").isEqualTo("marginalia");
        assertThat(run.rows()).as("the engine does not count this ack either, so the typed from matches no counted ack")
            .isEqualTo(HANDED_OFF);
    }

    @Test
    void anAckThatSaysAnAliasWithNoCredentialDoesNotStopTheDeadline() throws Exception {
        var run = ackThenDeadline("alpha", Via.NO_CREDENTIAL);

        assertThat(run.ackStatus()).as("no credential: accepted as it was").isEqualTo(200);
        assertThat(run.ackFrom()).isEqualTo("alpha");
        assertThat(run.rows()).as("control: an ack no session maps to the owner does not close the red, so the tests above are not vacuous")
            .isEqualTo(HANDED_OFF);
    }

    private static long lifetimeSeconds(dev.nexus.service.db.TupleRepository.TupleRow r) {
        return java.time.Duration.between(r.createdAt(), r.expiresAt()).toSeconds();
    }

    @Test
    void ownerAckAndClearRowsLiveThirtyFiveDaysWhileTheOtherTopicsKeepThree() throws Exception {
        long thirtyFiveDays = 35L * 24 * 3600;
        long threeDays = 3L * 24 * 3600;
        se.env.ids.clock.set(Instant.now());
        String t = se.tenant();
        String adapter = "adapter-" + UUID.randomUUID();
        String client = "client-" + UUID.randomUUID();
        seedToken(adapter, t, TokenStore.SCOPE_BOARD_CI);
        seedToken(client, t, TokenStore.SCOPE_TENANT);
        String l = se.env.seat(t, "lead");
        String a = se.env.seat(t, "alpha");
        String c = se.env.seat(t, "carol");
        se.form(t, l, a, c);
        se.push(t, StamperEnv.BASE, "X", "G0", "sess-alpha");
        se.push(t, StamperEnv.BASE, "Y", "X", "sess-carol");
        se.done(t, StamperEnv.BASE, "Y", "failure");                                  // the stamper's owner row
        var ack = postAs(client, se.env.tokenOf(a), "/v1/tuples/out", Map.of(           // a session's ack row
            "subspace", "board/ci/nexus-develop--owner", "keys", Map.of("topic", "nexus-develop--owner"),
            "dims", Map.of("kind", "ack", "sha", "Y"), "body", "{}", "nonce", UUID.randomUUID().toString()));
        assertThat(ack.statusCode()).as(ack.body()).isEqualTo(200);
        se.push(t, StamperEnv.BASE, "Z", "Y", "sess-alpha");
        se.done(t, StamperEnv.BASE, "Z", "success");                                  // the stamper's clear row
        out(adapter, Map.of("subspace", "board/ci/nexus-develop", "keys", Map.of("topic", "nexus-develop"),   // the adapter's post
            "dims", Map.of("from", "github", "kind", "job", "sha", "Z"),
            "body", "{\"state\":\"completed\"}", "nonce", UUID.randomUUID().toString()));

        var owner = se.tuples.rdp(t, "board/ci/nexus-develop--owner", Map.of(), 50, null);
        assertThat(owner).extracting(r -> r.dims().get("kind")).as("an owner row, an ack and a clear row were written")
            .contains("owner", "ack", "clear");
        for (var row : owner) {
            assertThat(lifetimeSeconds(row)).as(row.dims().get("kind") + " row").isBetween(thirtyFiveDays - 5, thirtyFiveDays + 5);
        }
        var push = se.tuples.rdp(t, "board/ci/nexus-develop--push", Map.of(), 50, null);
        assertThat(push).isNotEmpty();
        push.forEach(row -> assertThat(lifetimeSeconds(row)).as("push row").isBetween(threeDays - 5, threeDays + 5));
        var base = se.tuples.rdp(t, "board/ci/nexus-develop", Map.of(), 50, null);
        assertThat(base).isNotEmpty();
        base.forEach(row -> assertThat(lifetimeSeconds(row)).as("adapter post").isBetween(threeDays - 5, threeDays + 5));
    }

    @Test
    void anExplicitTtlOnAnOwnerTopicMayReachThirtyFiveDaysAndNoFurther() throws Exception {
        String t = se.tenant();
        var ok = se.tuples.out(t, "board/ci/ttl-check--owner", Map.of("topic", "ttl-check--owner"),
            Map.of("from", "i1", "kind", "ack", "sha", "S"), "{}", UUID.randomUUID().toString(), 35L * 24 * 3600);
        assertThat(ok).isNotNull();
        org.junit.jupiter.api.Assertions.assertThrows(dev.nexus.service.db.TtlTooLongException.class, () ->
            se.tuples.out(t, "board/ci/ttl-check--owner", Map.of("topic", "ttl-check--owner"),
                Map.of("from", "i1", "kind", "ack", "sha", "S2"), "{}", UUID.randomUUID().toString(), 35L * 24 * 3600 + 1));
        org.junit.jupiter.api.Assertions.assertThrows(dev.nexus.service.db.TtlTooLongException.class, () ->
            se.tuples.out(t, "board/ci/ttl-check", Map.of("topic", "ttl-check"),
                Map.of("from", "github", "kind", "job", "sha", "S"), "{}", UUID.randomUUID().toString(), 3L * 24 * 3600 + 1));
    }

    /**
     * Behaviour of the 35-day retention (RDR-252, nexus-igea9.4): after three days the push and done rows have
     * expired while the owner row of the same red is still live. The next green then clears that episode and nothing
     * worse: no stamper error, no hand-off, no escalation, and no second owner row.
     */
    @Test
    void anOwnerRowThatOutlivesItsPushAndDoneRowsIsClearedByTheNextGreen() throws Exception {
        se.env.ids.clock.set(Instant.now());
        String t = se.tenant();
        String l = se.env.seat(t, "lead");
        String a = se.env.seat(t, "alpha");
        String c = se.env.seat(t, "carol");
        se.form(t, l, a, c);
        se.push(t, StamperEnv.BASE, "X", "G0", "sess-alpha");
        se.push(t, StamperEnv.BASE, "Y", "X", "sess-carol");
        se.done(t, StamperEnv.BASE, "Y", "failure");
        assertThat(se.ownerRows(t, StamperEnv.BASE)).extracting(StamperEnv.Row::kind).containsExactly("owner");

        // Three days on: the template's other topics have expired; the owner subspace has not.
        se.env.ids.scope.withTenant(t, ctx -> ctx.update(dev.nexus.service.jooq.nexus.Tables.TUPLES)
            .set(dev.nexus.service.jooq.nexus.Tables.TUPLES.EXPIRES_AT, java.time.OffsetDateTime.now().minusSeconds(1))
            .where(dev.nexus.service.jooq.nexus.Tables.TUPLES.TENANT_ID.eq(t))
            .and(dev.nexus.service.jooq.nexus.Tables.TUPLES.SUBSPACE.in("board/ci/nexus-develop--push", "board/ci/nexus-develop--done"))
            .execute());
        assertThat(se.tuples.rdp(t, "board/ci/nexus-develop--push", Map.of(), 10, null)).as("the push rows are gone").isEmpty();
        assertThat(se.tuples.rdp(t, "board/ci/nexus-develop--owner", Map.of(), 10, null)).as("the owner row is still there").hasSize(1);

        se.push(t, StamperEnv.BASE, "Z", "Y", "sess-alpha");
        se.done(t, StamperEnv.BASE, "Z", "success");

        assertThat(se.ownerRows(t, StamperEnv.BASE).stream().map(r -> r.kind() + "/" + r.get("reason")).toList())
            .as("the green clears the open episode and posts nothing else")
            .containsExactly("owner/earliest_suspect", "clear/null");
    }

    @Test
    void theStampersOwnerAndClearRowsCarryNoSenderSession() throws Exception {
        // Null sender_session with from=engine is what makes a row the engine's: the route refuses a client's
        // from=engine, and the client aura accepts one only with a rendered null session. A change that stamps the
        // engine's writes would silently kill the aura; this fails first.
        se.env.ids.clock.set(Instant.now());
        String t = se.tenant();
        String adapter = "adapter-" + UUID.randomUUID();
        seedToken(adapter, t, TokenStore.SCOPE_BOARD_CI);
        String l = se.env.seat(t, "lead");
        String a = se.env.seat(t, "alpha");
        String c = se.env.seat(t, "carol");
        se.form(t, l, a, c);
        se.push(t, StamperEnv.BASE, "X", "G0", "sess-alpha");
        se.push(t, StamperEnv.BASE, "Y", "X", "sess-carol");
        out(adapter, Map.of("subspace", "board/ci/nexus-develop--done", "keys", Map.of("topic", "nexus-develop--done"),
            "dims", Map.of("from", "github", "kind", "sha", "sha", "Y", "outcome", "failure"),
            "body", "{\"state\":\"concluded\",\"sha\":\"Y\"}", "nonce", UUID.randomUUID().toString()));
        eventually(() -> se.ownerRows(t, StamperEnv.BASE), r -> !r.isEmpty());
        se.push(t, StamperEnv.BASE, "Z", "Y", "sess-alpha");
        out(adapter, Map.of("subspace", "board/ci/nexus-develop--done", "keys", Map.of("topic", "nexus-develop--done"),
            "dims", Map.of("from", "github", "kind", "sha", "sha", "Z", "outcome", "success"),
            "body", "{\"state\":\"concluded\",\"sha\":\"Z\"}", "nonce", UUID.randomUUID().toString()));
        eventually(() -> se.ownerRows(t, StamperEnv.BASE), r -> r.size() >= 2);

        var rows = se.tuples.rdp(t, "board/ci/nexus-develop--owner", Map.of(), 50, null).stream()
            .filter(r -> "engine".equals(r.dims().get("from"))).toList();
        assertThat(rows).extracting(r -> r.dims().get("kind")).as("the stamper wrote an owner row and a clear row")
            .containsExactlyInAnyOrder("owner", "clear");
        assertThat(rows).extracting(dev.nexus.service.db.TupleRepository.TupleRow::senderSession)
            .as("the engine's rows carry no sender session").containsOnlyNulls();
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
