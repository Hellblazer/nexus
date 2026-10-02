// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.vectors.EmbedActivitySnapshot;
import dev.nexus.service.vectors.EmbedderRouter;
import dev.nexus.service.vectors.OwnerlessWriteActivity;
import dev.nexus.service.vectors.OwnerlessWritePolicy;
import dev.nexus.service.vectors.RacedEmbedActivity;
import dev.nexus.service.vectors.SuppliedVectorMismatchActivity;

import java.io.IOException;
import java.util.Map;
import java.util.function.Supplier;

/**
 * GET /v1/status — bead nexus-s71lr, deliverable 2. Live embed-activity counters
 * so a client can poll "is the engine still embedding, or has it hung?" without
 * tailing logs — the wire-level twin of the {@code event=bge768_embed_progress}
 * / {@code event=embed_progress} log lines. No authentication required, same
 * posture as {@link HealthHandler} / {@link VersionHandler}: this is operational
 * telemetry, not user data, and crosses no trust boundary the log lines
 * themselves don't already cross.
 *
 * <p>Additive: a NEW route, no existing endpoint's shape changed. See
 * {@code docs/wire-contract-pending.md}'s nexus-s71lr entry.
 *
 * <p>Returns 200:
 * <pre>{"embedding_mode":"onnx-local",
 *  "local_embed_activity":{"active":true,"chunks_done_total":1024,
 *    "sub_batches_total":64,"last_chunks_per_sec":7.7,
 *    "last_activity_age_ms":230,"queue_depth":0,"thread_width":4,
 *    "deadline_aborts_total":0,"admission_refusals_total":0},
 *  "embedder_activity":{"bge-base-en-v15-768":{...same shape...}},
 *  "raced_embeds_total":0,
 *  "supplied_vector_mismatches_total":0,
 *  "process_start_time":"2026-09-12T09:00:00Z"}</pre>
 *
 * <p>{@code reaper} (RDR-192 Phase 3 gate S5, bead nexus-wbfpw.56, ADDITIVE) is the engine reaper's liveness:
 * {@code {"enabled":true,"interval_seconds":3600,"last_completed_pass_at":"2026-10-02T07:00:00Z"|null,
 * "failed_passes_total":0}}, or {@code {"enabled":false}} when no reaper is scheduled in this process. See
 * {@link ReaperStatus}.
 *
 * <p>{@code supplied_vector_mismatches_total} (RDR-223 P1.5, bead nexus-z0o2p.6, ADDITIVE) is
 * a process-wide, lifetime counter (see {@link SuppliedVectorMismatchActivity}) of client-supplied
 * vectors the combined write routes did not store because the chash already had a stored vector
 * for the same text and the two differed. Top-level for the same reason as {@code
 * raced_embeds_total}: it has no embedder dimension.
 *
 * <p>{@code raced_embeds_total} (RDR-222 Phase 0, bead nexus-ulrjq, ADDITIVE) is a
 * process-wide, lifetime counter (see {@link RacedEmbedActivity}) of chashes a
 * write's existence partition found ABSENT that another concurrent writer had
 * already committed by the time this write's own INSERT ran — a duplicate embed
 * (RDR-181's existence-check-then-embed window). Deliberately a TOP-LEVEL field,
 * not folded into {@code local_embed_activity}/{@code embedder_activity}: unlike
 * every field in those two shapes, it has no embedder dimension — it is a
 * DB-write-layer count, identical across every embedder, so nesting it per
 * embedder would misrepresent it as per-embedder data.
 *
 * <p>{@code process_start_time} (RDR-222 Phase 0 fix round, bead nexus-ulrjq,
 * critic #2, ADDITIVE) is the SAME value {@code /version}'s field of the same
 * name reports ({@link VersionHandler#appendProcessUptimeFields}, nexus-904y8) —
 * reused verbatim, never a second {@code System.currentTimeMillis()} sample at
 * this handler's own construction, so the two routes never disagree about when
 * the process started. Answers the restart-fragility gap a window reader of
 * {@code raced_embeds_total} otherwise has no way to close: a counter that reads
 * lower on a later poll, or resets to a small number, is indistinguishable from
 * "nothing raced in this window" unless the reader can also see that the process
 * restarted between the two reads.
 *
 * <p>{@code deadline_aborts_total} (nexus-8hdg9 phases 3/4, ADDITIVE, in
 * every entry of both shapes) counts embed calls the embedder aborted at a
 * cooperative request-deadline check point. The throughput A/B gate reads it
 * and requires ZERO on a healthy run.
 *
 * <p>{@code admission_refusals_total} (nexus-u2mlh.2, ADDITIVE, in every entry
 * of both shapes) counts embed calls refused before queueing because the
 * batches already waiting made the request's deadline unreachable. Only the
 * CCE embedder refuses; every other embedder reports 0.
 *
 * <p>{@code embedding_mode} mirrors {@code /version}'s field (via the SAME
 * {@link EmbedderRouter#modeName()}) so a caller does not need a second probe
 * to know which posture it is reading.
 *
 * <p>{@code local_embed_activity} is the ORIGINAL (deliverable 2) field:
 * {@code null} in cloud/voyage mode or when no local admission-gate-wired
 * embedder is supplied — never a fabricated value. Kept unchanged for any
 * caller already reading it.
 *
 * <p>{@code embedder_activity} (bead nexus-s71lr pass 3, ADDITIVE — see the
 * updated docs/wire-contract-pending.md entry) is a map keyed by each
 * embedder's own {@link dev.nexus.service.vectors.Embedder#modelToken()},
 * covering EVERY embedder {@code embedderRouter} dispatches to — local mode's
 * bge768 (redundant with {@code local_embed_activity} but included for
 * uniformity) AND, the majority posture this pass closes, cloud mode's
 * voyage-code-3 / voyage-context-3 / voyage-3. An embedder that does not
 * track activity (the MiniLM ONNX fallback, test fakes) is simply absent
 * from the map, never reported with a fabricated value. Empty {@code {}}
 * when {@code embedderRouter} is null or reports nothing.
 */
public final class StatusHandler implements HttpHandler {

    private final EmbedderRouter embedderRouter;   // nullable — mode "unknown"
    private final Supplier<EmbedActivitySnapshot> localEmbedActivitySupplier; // nullable
    private final long processStartMillis;
    private final OwnerlessWritePolicy ownerlessWritePolicy;   // nullable — mode field omitted
    private final Supplier<ReaperStatus> reaperStatus;          // nullable — "reaper" key omitted

    public StatusHandler(EmbedderRouter embedderRouter) {
        this(embedderRouter, null);
    }

    /**
     * @param embedderRouter             the doc-side router; supplies {@code
     *                                    embedding_mode} exactly like {@link
     *                                    VersionHandler}, and (pass 3) {@code
     *                                    embedder_activity} via {@link
     *                                    EmbedderRouter#embedActivitySnapshots()}.
     *                                    Null -> "unknown" mode, empty activity map.
     * @param localEmbedActivitySupplier reads the live snapshot from the
     *                                    process's {@code Bge768Embedder}
     *                                    (local mode only) for the ORIGINAL
     *                                    {@code local_embed_activity} field.
     *                                    Null in cloud/voyage mode, where there
     *                                    is no local embedder to read from ->
     *                                    {@code local_embed_activity} is
     *                                    omitted as JSON {@code null}, never
     *                                    fabricated.
     */
    public StatusHandler(
            EmbedderRouter embedderRouter,
            Supplier<EmbedActivitySnapshot> localEmbedActivitySupplier) {
        // No real deploy correlates against this handler's own construction time
        // (this 2-arg ctor predates process_start_time and every caller of it,
        // production included through NexusService's OWN 3-arg wiring, either
        // passes VersionHandler's real value below or is a test that only checks
        // the field's presence/format) — a fresh sample here is a reasonable
        // fallback, never presented as if it were VersionHandler's own value.
        this(embedderRouter, localEmbedActivitySupplier, System.currentTimeMillis());
    }

    /**
     * @param processStartMillis RDR-222 Phase 0 fix round (bead nexus-ulrjq,
     *                                    critic #2): the SAME instant {@link
     *                                    VersionHandler#processStartMillis()}
     *                                    reports — production wiring MUST pass
     *                                    that handler's own value here, never a
     *                                    fresh {@code System.currentTimeMillis()}
     *                                    (a second clock source the two routes
     *                                    could then disagree on).
     */
    public StatusHandler(
            EmbedderRouter embedderRouter,
            Supplier<EmbedActivitySnapshot> localEmbedActivitySupplier,
            long processStartMillis) {
        this(embedderRouter, localEmbedActivitySupplier, processStartMillis, null);
    }

    /**
     * @param ownerlessWritePolicy RDR-223 Phase 3 Step 2 (nexus-z0o2p.24): the policy
     *                             {@code VectorHandler} applies to ownerless chunk writes, so
     *                             {@code ownerless_write_mode} can say which mode this process runs.
     *                             Null omits that one field; the two counters are always present.
     */
    public StatusHandler(
            EmbedderRouter embedderRouter,
            Supplier<EmbedActivitySnapshot> localEmbedActivitySupplier,
            long processStartMillis,
            OwnerlessWritePolicy ownerlessWritePolicy) {
        this(embedderRouter, localEmbedActivitySupplier, processStartMillis, ownerlessWritePolicy, null);
    }

    /**
     * What the engine reaper reports on this route (RDR-192 Phase 3 gate S5, bead nexus-wbfpw.56, [additive]):
     * the one durable sign that it is alive, since a pass with nothing to move writes no {@code gc_audit} row and a
     * cloud operator has no engine log. {@code lastCompletedPassAt} is null before the first pass and is NOT moved by
     * a pass that died; a client flags it when it is older than a few {@code intervalSeconds}.
     *
     * @param enabled              the reaper is scheduled in this process
     * @param intervalSeconds      the delay between the end of one pass and the start of the next
     * @param lastCompletedPassAt  when the last pass that ran to the end finished; null before the first
     * @param failedPassesTotal    passes since boot that died or could not list their tenants
     */
    public record ReaperStatus(boolean enabled, long intervalSeconds, java.time.Instant lastCompletedPassAt,
                               long failedPassesTotal) {}

    /**
     * @param reaperStatus RDR-192 Phase 3 gate S5 (nexus-wbfpw.56): the reaper's liveness. Null omits the
     *                     {@code reaper} key altogether (what an engine predating the field answers in, so tests
     *                     and older wirings read as "cannot tell"); a supplier that returns null reports
     *                     {@code {"enabled":false}} (no reaper is scheduled in this process).
     */
    public StatusHandler(
            EmbedderRouter embedderRouter,
            Supplier<EmbedActivitySnapshot> localEmbedActivitySupplier,
            long processStartMillis,
            OwnerlessWritePolicy ownerlessWritePolicy,
            Supplier<ReaperStatus> reaperStatus) {
        this.embedderRouter = embedderRouter;
        this.localEmbedActivitySupplier = localEmbedActivitySupplier;
        this.processStartMillis = processStartMillis;
        this.ownerlessWritePolicy = ownerlessWritePolicy;
        this.reaperStatus = reaperStatus;
    }

    @Override
    public void handle(HttpExchange exchange) throws IOException {
        if (!"GET".equalsIgnoreCase(exchange.getRequestMethod())) {
            HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}");
            return;
        }
        StringBuilder body = new StringBuilder(384);
        body.append("{\"embedding_mode\":")
            .append(HttpUtil.jsonString(embedderRouter != null ? embedderRouter.modeName() : "unknown"));

        EmbedActivitySnapshot snap = localEmbedActivitySupplier != null
                ? localEmbedActivitySupplier.get() : null;
        body.append(",\"local_embed_activity\":");
        if (snap != null) {
            appendSnapshot(body, snap);
        } else {
            body.append("null");
        }

        body.append(",\"embedder_activity\":{");
        Map<String, EmbedActivitySnapshot> perEmbedder = embedderRouter != null
                ? embedderRouter.embedActivitySnapshots() : Map.of();
        boolean first = true;
        for (Map.Entry<String, EmbedActivitySnapshot> e : perEmbedder.entrySet()) {
            if (!first) body.append(',');
            first = false;
            body.append(HttpUtil.jsonString(e.getKey())).append(':');
            appendSnapshot(body, e.getValue());
        }
        body.append('}');

        // RDR-222 Phase 0 (bead nexus-ulrjq), [additive]: process-wide lifetime
        // counter, not per-embedder — see this class's own javadoc.
        body.append(",\"raced_embeds_total\":").append(RacedEmbedActivity.total());

        // RDR-223 P1.5 (bead nexus-z0o2p.6), [additive]: same shape and lifetime as the
        // raced-embed counter above.
        body.append(",\"supplied_vector_mismatches_total\":").append(SuppliedVectorMismatchActivity.total());

        // RDR-223 Phase 3 Step 2 (bead nexus-z0o2p.24), [additive]: requests the ownerless-write
        // check refused (enforce) or let through and counted (log-only), since boot, plus the mode
        // this process runs. A log-only run's reader asks "did anything write ownerless?" here.
        body.append(",\"ownerless_writes_refused_total\":").append(OwnerlessWriteActivity.refusedTotal());
        body.append(",\"ownerless_writes_would_refuse_total\":").append(OwnerlessWriteActivity.wouldRefuseTotal());
        if (ownerlessWritePolicy != null) {
            body.append(",\"ownerless_write_mode\":")
                .append(HttpUtil.jsonString(ownerlessWritePolicy.mode().wire()));
        }

        // RDR-192 Phase 3 gate S5 (bead nexus-wbfpw.56), [additive]: the engine reaper's liveness. Absent key =
        // an engine (or wiring) that predates it; {"enabled":false} = no reaper in this process.
        if (reaperStatus != null) {
            ReaperStatus r = reaperStatus.get();
            body.append(",\"reaper\":");
            if (r == null) {
                body.append("{\"enabled\":false}");
            } else {
                body.append("{\"enabled\":").append(r.enabled())
                    .append(",\"interval_seconds\":").append(r.intervalSeconds())
                    .append(",\"last_completed_pass_at\":");
                if (r.lastCompletedPassAt() == null) {
                    body.append("null");
                } else {
                    body.append(HttpUtil.jsonString(
                        r.lastCompletedPassAt().truncatedTo(java.time.temporal.ChronoUnit.SECONDS).toString()));
                }
                body.append(",\"failed_passes_total\":").append(r.failedPassesTotal()).append('}');
            }
        }

        // RDR-222 Phase 0 fix round (bead nexus-ulrjq, critic #2), [additive]:
        // VersionHandler.startTimeIso is the SAME rendering /version's field of
        // the same name uses — no second format, no second clock read here.
        body.append(",\"process_start_time\":")
            .append(HttpUtil.jsonString(VersionHandler.startTimeIso(processStartMillis)));

        body.append('}');
        HttpUtil.send(exchange, 200, body.toString());
    }

    private static void appendSnapshot(StringBuilder body, EmbedActivitySnapshot snap) {
        body.append('{')
            .append("\"active\":").append(snap.active())
            .append(",\"chunks_done_total\":").append(snap.chunksDoneTotal())
            .append(",\"sub_batches_total\":").append(snap.subBatchesTotal())
            .append(",\"last_chunks_per_sec\":").append(snap.lastChunksPerSec())
            .append(",\"last_activity_age_ms\":").append(snap.lastActivityAgeMs())
            .append(",\"queue_depth\":").append(snap.queueDepth())
            .append(",\"thread_width\":").append(snap.threadWidth())
            // nexus-8hdg9 phases 3/4, [additive]: the A/B gate asserts this is 0 on a
            // healthy run. Present in every entry, local_embed_activity included.
            .append(",\"deadline_aborts_total\":").append(snap.deadlineAbortsTotal())
            // nexus-u2mlh.2, [additive]: requests refused before queueing because the
            // batches already waiting made their deadline unreachable.
            .append(",\"admission_refusals_total\":").append(snap.admissionRefusalsTotal())
            .append('}');
    }
}
