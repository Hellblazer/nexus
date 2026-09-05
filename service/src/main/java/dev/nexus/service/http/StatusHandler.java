// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.vectors.EmbedActivitySnapshot;
import dev.nexus.service.vectors.EmbedderRouter;

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
 *    "last_activity_age_ms":230,"queue_depth":0,"thread_width":4},
 *  "embedder_activity":{"bge-base-en-v15-768":{...same shape...}}}</pre>
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
        this.embedderRouter = embedderRouter;
        this.localEmbedActivitySupplier = localEmbedActivitySupplier;
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
            .append('}');
    }
}
