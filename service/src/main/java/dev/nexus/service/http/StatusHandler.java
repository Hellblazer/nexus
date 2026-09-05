// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.vectors.EmbedActivitySnapshot;
import dev.nexus.service.vectors.EmbedderRouter;

import java.io.IOException;
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
 *    "last_activity_age_ms":230,"queue_depth":0,"thread_width":4}}</pre>
 *
 * <p>{@code embedding_mode} mirrors {@code /version}'s field (via the SAME
 * {@link EmbedderRouter#modeName()}) so a caller does not need a second probe
 * to know which posture it is reading. {@code local_embed_activity} is
 * {@code null} in cloud/voyage mode (nexus-s71lr, code-review-expert pass 2
 * finding a, only ADDS a cloud-mode progress LOG line — the wire counters
 * themselves stay scoped to the local bge path this deliverable named) or when
 * no local embedder is wired for any other reason — never a fabricated value.
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
     *                                    VersionHandler}. Null -> "unknown".
     * @param localEmbedActivitySupplier reads the live snapshot from the
     *                                    process's {@code Bge768Embedder}
     *                                    (local mode only). Null in cloud/
     *                                    voyage mode, where there is no local
     *                                    embedder to read from -> {@code
     *                                    local_embed_activity} is omitted as
     *                                    JSON {@code null}, never fabricated.
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
        StringBuilder body = new StringBuilder(256);
        body.append("{\"embedding_mode\":")
            .append(HttpUtil.jsonString(embedderRouter != null ? embedderRouter.modeName() : "unknown"));

        EmbedActivitySnapshot snap = localEmbedActivitySupplier != null
                ? localEmbedActivitySupplier.get() : null;
        if (snap != null) {
            body.append(",\"local_embed_activity\":{")
                .append("\"active\":").append(snap.active())
                .append(",\"chunks_done_total\":").append(snap.chunksDoneTotal())
                .append(",\"sub_batches_total\":").append(snap.subBatchesTotal())
                .append(",\"last_chunks_per_sec\":").append(snap.lastChunksPerSec())
                .append(",\"last_activity_age_ms\":").append(snap.lastActivityAgeMs())
                .append(",\"queue_depth\":").append(snap.queueDepth())
                .append(",\"thread_width\":").append(snap.threadWidth())
                .append('}');
        } else {
            body.append(",\"local_embed_activity\":null");
        }
        body.append('}');
        HttpUtil.send(exchange, 200, body.toString());
    }
}
