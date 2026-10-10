// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import dev.nexus.service.vectors.EmbedActivitySnapshot;
import dev.nexus.service.vectors.EmbedderRouter;
import dev.nexus.service.vectors.PciBuilderSession;
import dev.nexus.service.vectors.PciIndexSweep;
import dev.nexus.service.vectors.PciReconciler;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Bead nexus-s71lr, deliverable 2 — {@code GET /v1/status}. Hermetic: a bare
 * {@link HttpServer} bound to {@link StatusHandler} directly, no {@code
 * NexusService}/DataSource/Postgres involved at all (this handler has none of
 * those dependencies), so this suite needs no substrate the fast loop lacks.
 */
class StatusHandlerTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private HttpServer server;
    private String baseUrl;
    private final HttpClient http = HttpClient.newHttpClient();

    private void start(StatusHandler handler) throws Exception {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/status", handler);
        server.start();
        baseUrl = "http://127.0.0.1:" + server.getAddress().getPort();
    }

    @AfterEach
    void stop() {
        if (server != null) server.stop(0);
    }

    private JsonNode get() throws Exception {
        HttpResponse<String> resp = http.send(
                HttpRequest.newBuilder(URI.create(baseUrl + "/v1/status")).GET().build(),
                HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).isEqualTo(200);
        return MAPPER.readTree(resp.body());
    }

    @Test
    void noRouterNoSupplier_reportsUnknownModeAndNullActivity() throws Exception {
        start(new StatusHandler(null));
        JsonNode body = get();
        assertThat(body.get("embedding_mode").asText()).isEqualTo("unknown");
        assertThat(body.get("local_embed_activity").isNull()).isTrue();
        assertThat(body.get("embedder_activity").isEmpty()).isTrue();
    }

    @Test
    void withRouterNoSupplier_reportsRealModeAndNullActivity() throws Exception {
        // A trivial local Embedder wired through EmbedderRouter's local-embedder
        // constructor resolves modeName() to "onnx-local" -- exactly the /version
        // handler's own contract, reused here.
        var localEmbedder = new dev.nexus.service.vectors.Embedder() {
            @Override public String modelToken() { return "test-local"; }
            @Override public List<float[]> embed(List<String> texts) { return List.of(); }
        };
        var router = new EmbedderRouter(localEmbedder, "document");
        start(new StatusHandler(router));

        JsonNode body = get();
        assertThat(body.get("embedding_mode").asText()).isEqualTo("onnx-local");
        assertThat(body.get("local_embed_activity").isNull()).isTrue();
        // The fake embedder above does not override activitySnapshot() -> the
        // interface default (null) -> absent from the map, not fabricated.
        assertThat(body.get("embedder_activity").isEmpty()).isTrue();
    }

    @Test
    void embedderActivity_populatesFromRouterWhenAnEmbedderTracksIt() throws Exception {
        // Bead nexus-s71lr pass 3: the majority-posture fix -- cloud embedders
        // (VoyageEmbedder/CceEmbedder) now report through EmbedderRouter's
        // generic modelEmbedders map, not just the local-mode direct supplier.
        // Simulated here with a fake Embedder overriding activitySnapshot(),
        // since a real VoyageEmbedder needs network; the integration proof
        // that VoyageEmbedder/CceEmbedder's OWN activitySnapshot() reads their
        // real tracker lives in VoyageEmbedderBatchSplitTest/
        // CceEmbedderParallelTest.
        EmbedActivitySnapshot fake = new EmbedActivitySnapshot(
                true, 42L, 7L, 3.5, 100L, -1, -1, 0L, 0L);
        var trackedEmbedder = new dev.nexus.service.vectors.Embedder() {
            @Override public String modelToken() { return "voyage-code-3"; }
            @Override public List<float[]> embed(List<String> texts) { return List.of(); }
            @Override public EmbedActivitySnapshot activitySnapshot() { return fake; }
        };
        var router = new EmbedderRouter(trackedEmbedder, "document");
        start(new StatusHandler(router));

        JsonNode body = get();
        // local_embed_activity is unaffected -- no supplier was wired for it.
        assertThat(body.get("local_embed_activity").isNull()).isTrue();
        JsonNode perEmbedder = body.get("embedder_activity").get("voyage-code-3");
        assertThat(perEmbedder).isNotNull();
        assertThat(perEmbedder.get("active").asBoolean()).isTrue();
        assertThat(perEmbedder.get("chunks_done_total").asLong()).isEqualTo(42L);
        assertThat(perEmbedder.get("queue_depth").asInt()).isEqualTo(-1);
        assertThat(perEmbedder.get("thread_width").asInt()).isEqualTo(-1);
        assertThat(perEmbedder.get("deadline_aborts_total").asLong()).isEqualTo(0L);
    }

    @Test
    void deadlineAbortsTotal_isReportedInBothActivityShapes() throws Exception {
        // nexus-8hdg9 phases 3/4 ([additive]): the counter the throughput A/B gate
        // reads. Distinct non-zero values per shape so a swapped source is visible.
        EmbedActivitySnapshot local = new EmbedActivitySnapshot(
                false, 10L, 2L, 1.0, 5_000L, 0, 4, 3L, 5L);
        EmbedActivitySnapshot cloud = new EmbedActivitySnapshot(
                false, 20L, 20L, 2.0, 6_000L, -1, -1, 7L, 11L);
        var trackedEmbedder = new dev.nexus.service.vectors.Embedder() {
            @Override public String modelToken() { return "voyage-context-3"; }
            @Override public List<float[]> embed(List<String> texts) { return List.of(); }
            @Override public EmbedActivitySnapshot activitySnapshot() { return cloud; }
        };
        var router = new EmbedderRouter(trackedEmbedder, "document");
        start(new StatusHandler(router, () -> local));

        JsonNode body = get();
        assertThat(body.get("local_embed_activity").get("deadline_aborts_total").asLong()).isEqualTo(3L);
        assertThat(body.get("embedder_activity").get("voyage-context-3")
                .get("deadline_aborts_total").asLong()).isEqualTo(7L);
        // nexus-u2mlh.2 ([additive]): same two shapes, distinct values again.
        assertThat(body.get("local_embed_activity").get("admission_refusals_total").asLong()).isEqualTo(5L);
        assertThat(body.get("embedder_activity").get("voyage-context-3")
                .get("admission_refusals_total").asLong()).isEqualTo(11L);
    }

    @Test
    void withSupplier_reportsRealSnapshotFields() throws Exception {
        EmbedActivitySnapshot fake = new EmbedActivitySnapshot(
                true, 1024L, 64L, 7.7, 230L, 0, 4, 0L, 0L);
        start(new StatusHandler(null, () -> fake));

        JsonNode body = get();
        JsonNode activity = body.get("local_embed_activity");
        assertThat(activity.isNull()).isFalse();
        assertThat(activity.get("active").asBoolean()).isTrue();
        assertThat(activity.get("chunks_done_total").asLong()).isEqualTo(1024L);
        assertThat(activity.get("sub_batches_total").asLong()).isEqualTo(64L);
        assertThat(activity.get("last_chunks_per_sec").asDouble()).isEqualTo(7.7);
        assertThat(activity.get("last_activity_age_ms").asLong()).isEqualTo(230L);
        assertThat(activity.get("queue_depth").asInt()).isEqualTo(0);
        assertThat(activity.get("thread_width").asInt()).isEqualTo(4);
        assertThat(activity.get("deadline_aborts_total").asLong()).isEqualTo(0L);
    }

    @Test
    void supplierReturningNull_reportsNullActivityNotAFabricatedValue() throws Exception {
        // A supplier is wired, but its own answer is null (e.g. a real
        // Bge768Embedder that has never embedded anything yet still returns a
        // non-null snapshot per EmbedActivityTrackerTest, but this proves the
        // handler itself never fabricates a value when the supplier truly has
        // none to give).
        start(new StatusHandler(null, () -> null));
        JsonNode body = get();
        assertThat(body.get("local_embed_activity").isNull()).isTrue();
    }

    @Test
    void racedEmbedsTotal_topLevelFieldReflectsTheGlobalCounterDelta() throws Exception {
        // RDR-222 Phase 0 (bead nexus-ulrjq), [additive]: raced_embeds_total is a
        // process-wide counter (dev.nexus.service.vectors.RacedEmbedActivity), not
        // per-embedder — asserted here as a DELTA (never an absolute value), since
        // other test classes sharing this JVM/fork may also record against it.
        start(new StatusHandler(null));
        long before = get().get("raced_embeds_total").asLong();

        dev.nexus.service.vectors.RacedEmbedActivity.record(3);

        long after = get().get("raced_embeds_total").asLong();
        assertThat(after - before).isEqualTo(3L);
    }

    @Test
    void suppliedVectorMismatchesTotal_topLevelFieldReflectsTheGlobalCounterDelta() throws Exception {
        // RDR-223 P1.5 (bead nexus-z0o2p.6), [additive]: same delta discipline as
        // raced_embeds_total above.
        start(new StatusHandler(null));
        long before = get().get("supplied_vector_mismatches_total").asLong();

        dev.nexus.service.vectors.SuppliedVectorMismatchActivity.record(2);

        long after = get().get("supplied_vector_mismatches_total").asLong();
        assertThat(after - before).isEqualTo(2L);
    }

    @Test
    void processStartTime_reflectsTheExplicitlyProvidedInstant() throws Exception {
        // RDR-222 Phase 0 fix round (bead nexus-ulrjq, critic #2): production
        // wiring (NexusService) passes VersionHandler's OWN processStartMillis()
        // through the 3-arg constructor — asserted here against VersionHandler's
        // own rendering (startTimeIso), never a second clock source or a second
        // format.
        long fixedMillis = 1_757_500_800_000L; // 2026-09-10T12:00:00Z, arbitrary fixed instant
        start(new StatusHandler(null, null, fixedMillis));

        JsonNode body = get();
        assertThat(body.get("process_start_time").asText())
            .isEqualTo(VersionHandler.startTimeIso(fixedMillis));
    }

    @Test
    void processStartTime_defaultConstructorsStillReportAValidInstant() throws Exception {
        // The 1-arg/2-arg constructors predate process_start_time and are used
        // throughout this file's other tests — they fall back to this handler's
        // own construction time rather than omitting the field, so every
        // existing caller keeps compiling and keeps getting a real value.
        start(new StatusHandler(null));
        JsonNode body = get();
        assertThat(body.get("process_start_time").isTextual()).isTrue();
        // Parses as an ISO instant without throwing.
        java.time.Instant.parse(body.get("process_start_time").asText());
    }

    // ── the reaper's liveness (nexus-wbfpw.56, RDR-192 Phase 3 gate S5), [additive] ─────────────────────────────

    @Test
    void reaper_keyIsAbsentWhenNoReaperSupplierIsWired() throws Exception {
        // The shape an engine that predates the field answers in: a client reads "no key" as "cannot tell".
        start(new StatusHandler(null));
        assertThat(get().has("reaper")).isFalse();
    }

    @Test
    void reaper_reportsTheLastCompletedPassAndTheIntervalItRunsAt() throws Exception {
        var status = new StatusHandler.ReaperStatus(true, 3600L, 600L, java.time.Instant.parse("2026-10-02T07:00:00Z"), 2L);
        start(new StatusHandler(null, null, 0L, null, () -> status));

        JsonNode reaper = get().get("reaper");
        assertThat(reaper.get("enabled").asBoolean()).isTrue();
        assertThat(reaper.get("interval_seconds").asLong()).isEqualTo(3600L);
        assertThat(reaper.get("wall_clock_budget_seconds").asLong()).isEqualTo(600L);
        assertThat(reaper.get("last_completed_pass_at").asText()).isEqualTo("2026-10-02T07:00:00Z");
        assertThat(reaper.get("failed_passes_total").asLong()).isEqualTo(2L);
    }

    @Test
    void reaper_beforeItsFirstPassTheLastCompletedTimeIsNull_notAFabricatedValue() throws Exception {
        var status = new StatusHandler.ReaperStatus(true, 3600L, 600L, null, 0L);
        start(new StatusHandler(null, null, 0L, null, () -> status));

        JsonNode reaper = get().get("reaper");
        assertThat(reaper.get("enabled").asBoolean()).isTrue();
        assertThat(reaper.get("last_completed_pass_at").isNull()).isTrue();
    }

    @Test
    void reaper_reportsWhatTheLastCompletedPassDidWithItsTenants() throws Exception {
        // nexus-wbfpw.55 round 2: a pass completes whatever its tenants did, so the status carries the counts.
        var status = new StatusHandler.ReaperStatus(true, 3600L, 600L, java.time.Instant.parse("2026-10-02T07:00:00Z"),
            0L, new StatusHandler.ReaperStatus.LastPass(3, 1, 2, 1));
        start(new StatusHandler(null, null, 0L, null, () -> status));

        JsonNode lastPass = get().get("reaper").get("last_pass");
        assertThat(lastPass.get("tenants_visited").asInt()).isEqualTo(3);
        assertThat(lastPass.get("tenants_errored").asInt()).isEqualTo(1);
        assertThat(lastPass.get("tenants_refused").asInt()).isEqualTo(2);
        assertThat(lastPass.get("tenants_empty").asInt()).as("appended after the existing three").isEqualTo(1);
        var names = new java.util.ArrayList<String>();
        lastPass.fieldNames().forEachRemaining(names::add);
        assertThat(names).containsExactly("tenants_visited", "tenants_errored", "tenants_refused", "tenants_empty");
    }

    @Test
    void reaper_lastPassIsNullBeforeTheFirstPassAndForAStatusWithNoSummary() throws Exception {
        var status = new StatusHandler.ReaperStatus(true, 3600L, 600L, null, 0L);
        start(new StatusHandler(null, null, 0L, null, () -> status));

        JsonNode reaper = get().get("reaper");
        assertThat(reaper.has("last_pass")).as("the key is always there for an enabled reaper").isTrue();
        assertThat(reaper.get("last_pass").isNull()).isTrue();
    }

    @Test
    void reaper_aSupplierThatReturnsNullSaysTheReaperIsNotRunning() throws Exception {
        start(new StatusHandler(null, null, 0L, null, () -> null));

        JsonNode reaper = get().get("reaper");
        assertThat(reaper.get("enabled").asBoolean()).isFalse();
        assertThat(reaper.has("last_completed_pass_at")).as("nothing to report for a reaper that is not scheduled")
            .isFalse();
    }

    // ── nexus-wbfpw.48, [additive]: chunks_tenant_isolation_intact ──────────────────────────────────────────────

    @Test
    void chunksIsolation_keyIsAbsentWhenNoSupplierIsWired_andWhenTheProbeCouldNotRun() throws Exception {
        start(new StatusHandler(null));
        assertThat(get().has("chunks_tenant_isolation_intact")).as("an engine that predates the field").isFalse();
        stop();
        start(new StatusHandler(null, null, 0L, null, null, () -> null));
        assertThat(get().has("chunks_tenant_isolation_intact")).as("a probe that could not run").isFalse();
    }

    @Test
    void chunksIsolation_reportsTrueAndFalse_asABooleanOnly() throws Exception {
        start(new StatusHandler(null, null, 0L, null, null, () -> Boolean.TRUE));
        assertThat(get().get("chunks_tenant_isolation_intact").isBoolean()).isTrue();
        assertThat(get().get("chunks_tenant_isolation_intact").asBoolean()).isTrue();
        stop();
        start(new StatusHandler(null, null, 0L, null, null, () -> Boolean.FALSE));
        JsonNode body = get();
        assertThat(body.get("chunks_tenant_isolation_intact").asBoolean()).isFalse();
        assertThat(body.toString()).as("an unauthenticated route never names a policy or a role")
            .doesNotContain("chunks_gate_probe_owner_read").doesNotContain("nexus_admin");
    }

    // ── nexus-43ulx.23 (RDR-227 Step 2), [additive]: per_collection_indexes ─────────────────────────────────────

    private static final java.time.Instant READ_AT = java.time.Instant.parse("2026-10-10T08:00:00Z");
    private static final java.time.Instant DDL_AT = java.time.Instant.parse("2026-10-10T07:30:00.987Z");

    private static StatusHandler.PerCollectionIndexes pci(PciIndexSweep.Status sweep, PciReconciler.DdlStatus ddl) {
        return StatusHandler.PerCollectionIndexes.of(sweep, ddl);
    }

    private static PciIndexSweep.Status read(int valid, int invalid, int unparsed, boolean expired) {
        return new PciIndexSweep.Status(true, valid, invalid, unparsed, READ_AT, null, 0, expired);
    }

    private StatusHandler withPci(java.util.function.Supplier<StatusHandler.PerCollectionIndexes> supplier) {
        return new StatusHandler(null, null, 0L, null, null, null, supplier);
    }

    @Test
    void perCollectionIndexes_keyIsAbsentWhenNoSupplierIsWired_andWhenTheSupplierHasNothingYet() throws Exception {
        // An engine that predates the field answers without the key, and so does one whose sweep and reconciler are
        // not built yet (the boot window): a client reads "no key" as "cannot tell", never as zero indexes.
        start(new StatusHandler(null));
        assertThat(get().has("per_collection_indexes")).isFalse();
        stop();
        start(withPci(() -> null));
        assertThat(get().has("per_collection_indexes")).isFalse();
    }

    @Test
    void perCollectionIndexes_theHolderReportsEveryField() throws Exception {
        var holder = pci(read(4, 1, 2, false),
            new PciReconciler.DdlStatus(PciBuilderSession.BuilderState.OK, 1, 0, DDL_AT));
        start(withPci(() -> holder));

        JsonNode o = get().get("per_collection_indexes");
        assertThat(o.get("valid").asInt()).isEqualTo(4);
        assertThat(o.get("invalid").asInt()).isEqualTo(1);
        assertThat(o.get("unparsed").asInt()).isEqualTo(2);
        assertThat(o.get("last_read_at").asText()).isEqualTo("2026-10-10T08:00:00Z");
        assertThat(o.get("expired").asBoolean()).isFalse();
        JsonNode me = o.get("this_engine");
        assertThat(me.get("builder_state").asText()).isEqualTo("ok");
        assertThat(me.get("building").asInt()).isEqualTo(1);
        assertThat(me.get("failing").asInt()).isEqualTo(0);
        assertThat(me.get("last_ddl_pass_at").asText()).as("whole seconds, like the reaper's times")
            .isEqualTo("2026-10-10T07:30:00Z");
    }

    @Test
    void perCollectionIndexes_aNonHolderIsStandbyWithNullBuildingFailingAndPassTime() throws Exception {
        var standby = pci(read(4, 0, 0, false),
            new PciReconciler.DdlStatus(PciBuilderSession.BuilderState.STANDBY, null, null, null));
        start(withPci(() -> standby));

        JsonNode o = get().get("per_collection_indexes");
        assertThat(o.get("valid").asInt()).as("the read half's counts are global, the same on every engine")
            .isEqualTo(4);
        JsonNode me = o.get("this_engine");
        assertThat(me.get("builder_state").asText()).isEqualTo("standby");
        for (String k : new String[] {"building", "failing", "last_ddl_pass_at", "pass_started_at"}) {
            assertThat(me.has(k)).as(k + " is present, not omitted").isTrue();
            assertThat(me.get(k).isNull()).as(k + " is null on this non-holder, which never ran a pass").isTrue();
        }
        assertThat(me.get("pass_in_progress").asBoolean()).isFalse();
    }

    @Test
    void perCollectionIndexes_aStandbyKeepsItsOwnLastPassTime_butNoBuildingFailingOrPassInFlight() throws Exception {
        // This engine completed a pass, then a peer took the lock: its own history stays, the holder-only fields go.
        var standby = pci(read(4, 0, 0, false),
            new PciReconciler.DdlStatus(PciBuilderSession.BuilderState.STANDBY, null, null, DDL_AT));
        start(withPci(() -> standby));

        JsonNode me = get().at("/per_collection_indexes/this_engine");
        assertThat(me.get("builder_state").asText()).isEqualTo("standby");
        assertThat(me.get("last_ddl_pass_at").asText()).isEqualTo("2026-10-10T07:30:00Z");
        assertThat(me.get("building").isNull()).isTrue();
        assertThat(me.get("failing").isNull()).isTrue();
        assertThat(me.get("pass_started_at").isNull()).isTrue();
        assertThat(me.get("pass_in_progress").asBoolean()).isFalse();
    }

    @Test
    void perCollectionIndexes_aPassInFlightShowsItsStartAndTheFlag_whileTheLastPassTimeIsStillNull() throws Exception {
        var running = pci(read(0, 0, 0, false), new PciReconciler.DdlStatus(PciBuilderSession.BuilderState.OK, 1, 0,
            null, java.time.Instant.parse("2026-10-10T07:58:30.500Z"), true));
        start(withPci(() -> running));

        JsonNode me = get().at("/per_collection_indexes/this_engine");
        assertThat(me.get("pass_in_progress").asBoolean()).isTrue();
        assertThat(me.get("pass_started_at").asText()).as("whole seconds").isEqualTo("2026-10-10T07:58:30Z");
        assertThat(me.get("last_ddl_pass_at").isNull()).isTrue();
        assertThat(me.get("building").asInt()).isEqualTo(1);
    }

    @Test
    void perCollectionIndexes_noPrivilegeAndAuthFailedRenderAsThePrivilegeAndAuthenticationFailuresTheyAre()
            throws Exception {
        for (var state : new PciBuilderSession.BuilderState[] {PciBuilderSession.BuilderState.NO_PRIVILEGE,
                PciBuilderSession.BuilderState.AUTH_FAILED}) {
            var s = pci(read(2, 0, 0, false), new PciReconciler.DdlStatus(state, 0, 0, DDL_AT));
            start(withPci(() -> s));
            assertThat(get().at("/per_collection_indexes/this_engine/builder_state").asText())
                .isEqualTo(state.wire());
            stop();
        }
    }

    @Test
    void perCollectionIndexes_everyBuilderStateRendersItsWireName() throws Exception {
        for (var state : PciBuilderSession.BuilderState.values()) {
            var s = pci(read(0, 0, 0, false), new PciReconciler.DdlStatus(state, null, null, null));
            start(withPci(() -> s));
            assertThat(get().at("/per_collection_indexes/this_engine/builder_state").asText())
                .isEqualTo(state.wire());
            stop();
        }
        assertThat(java.util.Arrays.stream(PciBuilderSession.BuilderState.values())
            .map(PciBuilderSession.BuilderState::wire).toList())
            .containsExactlyInAnyOrder("ok", "auth_failed", "no_privilege", "off", "standby");
    }

    @Test
    void perCollectionIndexes_aHolderThatNeverRanAPassHasANullPassTime_andABuildingZero() throws Exception {
        var fresh = pci(read(0, 0, 0, false),
            new PciReconciler.DdlStatus(PciBuilderSession.BuilderState.OK, 0, 0, null));
        start(withPci(() -> fresh));

        JsonNode me = get().at("/per_collection_indexes/this_engine");
        assertThat(me.get("building").asInt()).isEqualTo(0);
        assertThat(me.get("last_ddl_pass_at").isNull()).as("not a fabricated time").isTrue();
    }

    @Test
    void perCollectionIndexes_beforeTheFirstSuccessfulReadTheLastReadTimeIsNull() throws Exception {
        var never = pci(new PciIndexSweep.Status(false, 0, 0, 0, null, null, 0, false),
            new PciReconciler.DdlStatus(PciBuilderSession.BuilderState.OFF, null, null, null));
        start(withPci(() -> never));

        JsonNode o = get().get("per_collection_indexes");
        assertThat(o.has("last_read_at")).isTrue();
        assertThat(o.get("last_read_at").isNull()).isTrue();
        assertThat(o.get("valid").asInt()).isZero();
        assertThat(o.get("expired").asBoolean()).isFalse();
    }

    @Test
    void perCollectionIndexes_aFrozenSetShowsAsExpiredAtTheGlobalLevel() throws Exception {
        var frozen = pci(read(3, 0, 0, true),
            new PciReconciler.DdlStatus(PciBuilderSession.BuilderState.STANDBY, null, null, null));
        start(withPci(() -> frozen));

        JsonNode o = get().get("per_collection_indexes");
        assertThat(o.get("expired").asBoolean()).as("an operator can see the router is answering as empty").isTrue();
        assertThat(o.get("valid").asInt()).as("the counts stay the last read's").isEqualTo(3);
        assertThat(o.get("this_engine").has("expired")).as("a global fact, not a per-engine one").isFalse();
    }

    @Test
    void perCollectionIndexes_hasExactlyTheDocumentedKeysInOrder() throws Exception {
        var holder = pci(read(1, 0, 0, false),
            new PciReconciler.DdlStatus(PciBuilderSession.BuilderState.OK, 0, 0, DDL_AT));
        start(withPci(() -> holder));

        JsonNode o = get().get("per_collection_indexes");
        var top = new java.util.ArrayList<String>();
        o.fieldNames().forEachRemaining(top::add);
        assertThat(top).containsExactly("valid", "invalid", "unparsed", "last_read_at", "expired",
            "sweep_seconds", "this_engine");
        var inner = new java.util.ArrayList<String>();
        o.get("this_engine").fieldNames().forEachRemaining(inner::add);
        assertThat(inner).containsExactly("builder_state", "building", "failing", "last_ddl_pass_at",
            "pass_started_at", "pass_in_progress");
    }

    /**
     * The golden fixture the Python doctor row also reads ({@code tests/fixtures/pci_status_bodies.json}): the bodies
     * the engine emits for a holder, a first pass in flight, a standby, an auth failure, a privilege failure, an
     * expired router set and a switched-off engine, rendered here through the real handler. A field renamed or
     * retyped on this side fails this test until the fixture is updated, and the doctor row's test then reads the
     * updated bodies, so the two halves cannot drift apart unseen.
     */
    @Test
    void perCollectionIndexes_bodiesMatchTheGoldenFixtureThePythonDoctorRowReads() throws Exception {
        JsonNode golden = MAPPER.readTree(java.nio.file.Files.readString(
            java.nio.file.Path.of("..", "tests", "fixtures", "pci_status_bodies.json"))).get("cases");
        var cases = new java.util.LinkedHashMap<String, StatusHandler.PerCollectionIndexes>();
        // A build in flight is a pass in flight: PciReconciler.status() cannot report building 1 without a started pass.
        cases.put("holder", pci(read(4, 1, 2, false), new PciReconciler.DdlStatus(
            PciBuilderSession.BuilderState.OK, 1, 0, DDL_AT, java.time.Instant.parse("2026-10-10T07:58:30.250Z"),
            true)));
        cases.put("first_pass_in_flight", pci(read(0, 1, 0, false), new PciReconciler.DdlStatus(
            PciBuilderSession.BuilderState.OK, 1, 0, null, java.time.Instant.parse("2026-10-10T07:58:30.500Z"), true)));
        cases.put("standby", pci(read(4, 0, 0, false),
            new PciReconciler.DdlStatus(PciBuilderSession.BuilderState.STANDBY, null, null, null)));
        cases.put("auth_failed", pci(read(2, 0, 0, false),
            new PciReconciler.DdlStatus(PciBuilderSession.BuilderState.AUTH_FAILED, null, null, null)));
        cases.put("no_privilege", pci(read(2, 0, 0, false),
            new PciReconciler.DdlStatus(PciBuilderSession.BuilderState.NO_PRIVILEGE, 0, 0, DDL_AT)));
        cases.put("expired", pci(read(3, 0, 0, true),
            new PciReconciler.DdlStatus(PciBuilderSession.BuilderState.STANDBY, null, null, null)));
        cases.put("off", pci(new PciIndexSweep.Status(false, 0, 0, 0, null, null, 0, false),
            new PciReconciler.DdlStatus(PciBuilderSession.BuilderState.OFF, null, null, null)));

        var goldenNames = new java.util.ArrayList<String>();
        golden.fieldNames().forEachRemaining(goldenNames::add);
        assertThat(goldenNames).as("the fixture holds exactly the cases rendered here").containsExactlyElementsOf(
            cases.keySet());
        for (var entry : cases.entrySet()) {
            var supplied = entry.getValue();
            start(withPci(() -> supplied));
            assertThat(get().get("per_collection_indexes")).as(entry.getKey()).isEqualTo(golden.get(entry.getKey()));
            stop();
        }
    }

    @Test
    void perCollectionIndexes_reportsThePeriodOfTheDdlPasses() throws Exception {
        // A client judges how stale last_ddl_pass_at may be from this; it is the setting, not a constant.
        var v = pci(read(1, 0, 0, false), new PciReconciler.DdlStatus(
            PciBuilderSession.BuilderState.OK, 0, 0, DDL_AT, null, false, 900));
        start(withPci(() -> v));

        JsonNode pci = get().get("per_collection_indexes");

        assertThat(pci.get("sweep_seconds").isIntegralNumber()).isTrue();
        assertThat(pci.get("sweep_seconds").asLong()).isEqualTo(900);
        assertThat(pci.has("this_engine")).isTrue();
    }

    @Test
    void perCollectionIndexes_isServedFromTheSuppliedValue_oneCallPerRequest() throws Exception {
        // The route reads a cached value: the handler asks the supplier once per request and does no catalog read of
        // its own (it has no data source to read with).
        var calls = new java.util.concurrent.atomic.AtomicInteger();
        var v = pci(read(1, 0, 0, false),
            new PciReconciler.DdlStatus(PciBuilderSession.BuilderState.OK, 0, 0, DDL_AT));
        start(withPci(() -> {
            calls.incrementAndGet();
            return v;
        }));
        get();
        get();
        assertThat(calls.get()).isEqualTo(2);
    }

    @Test
    void nonGetMethodIsRejected() throws Exception {
        start(new StatusHandler(null));
        HttpResponse<String> resp = http.send(
                HttpRequest.newBuilder(URI.create(baseUrl + "/v1/status"))
                        .method("POST", HttpRequest.BodyPublishers.noBody()).build(),
                HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).isEqualTo(405);
    }
}
