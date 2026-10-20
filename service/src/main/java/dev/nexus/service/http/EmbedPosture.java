// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import dev.nexus.service.vectors.BootPlan;
import dev.nexus.service.vectors.EmbeddingProfile;
import dev.nexus.service.vectors.Reranker;

import java.util.function.BooleanSupplier;

/**
 * RDR-210 Phase 2 Step 4 (bead nexus-un7vj) — the part of the boot posture that is not the
 * embedding mode: which profile new collections get, which reranker serves search, and, when the
 * profile cannot be served, what is missing. {@code GET /version} and {@code GET /v1/status} both
 * report it through {@link #appendFields}, so the two routes cannot disagree and an {@code nx
 * doctor} row can compare {@code embedding_models} against the profile's family.
 *
 * <p>Wire shape (ADDITIVE, four keys appended after {@code embedding_models} on {@code /version}
 * and after {@code embedder_activity} on {@code /v1/status}; omitted entirely when the engine has
 * no posture to report, which a client reads as "cannot tell"):
 * <pre>"embedding_profile":"bge",
 * "reranker":"ms-marco-minilm-l6-v2",
 * "reranker_available":true,
 * "embedding_degraded":null</pre>
 * or, for a choice the boot cannot serve:
 * <pre>"embedding_degraded":{"chosen":"bge","serving":"voyage",
 *   "missing":"the bge-768 model and tokenizer files are missing","remedy":"run `nx init --service` ..."}</pre>
 * where only {@code chosen}'s collections get the 422 and {@code serving}'s are served.
 * {@code missing} names the missing piece by role (the key, the model and tokenizer files), never an absolute
 * path or probe text; {@link EmbeddingProfile.Degradation#detail()} carries those and goes to the boot log only.
 *
 * <p>{@code reranker_available} (nexus-yuh4a) is a bare boolean read LIVE on every response, never a boot
 * snapshot: {@code reranker} names the CHOSEN reranker even when its files are absent and every rerank degrades,
 * and {@code reranker_available} says whether the reranker's model files are present. It is a file-presence
 * answer, not a promise that a rerank would succeed. It carries no path or probe text; {@code false} also
 * covers "no reranker configured".
 *
 * @param profile           the wire value of the resolved profile ({@code voyage} or {@code bge})
 * @param reranker          the reranker's model token, or null when none is configured
 * @param rerankerAvailable asked at render time: whether the reranker's files are present ({@link Reranker#available()})
 * @param degradation       the degraded-boot description, or null when the profile's family is served
 */
public record EmbedPosture(String profile, String reranker, BooleanSupplier rerankerAvailable,
                           EmbeddingProfile.Degradation degradation) {

    /** A posture whose reranker, when named, is taken as available (tests and callers with no probe to ask). */
    public EmbedPosture(String profile, String reranker, EmbeddingProfile.Degradation degradation) {
        this(profile, reranker, () -> reranker != null, degradation);
    }

    /** The posture a boot reports: its plan's profile and degradation and the reranker it built. */
    public static EmbedPosture of(BootPlan plan, Reranker reranker) {
        return new EmbedPosture(plan.profile().value(),
                reranker == null ? null : reranker.modelToken(),
                reranker == null ? () -> false : reranker::available,
                plan.degradation().orElse(null));
    }

    /** Append {@code ,"embedding_profile":...,"reranker":...,"reranker_available":...,"embedding_degraded":...} to {@code body}. */
    void appendFields(StringBuilder body) {
        body.append(",\"embedding_profile\":").append(HttpUtil.jsonString(profile))
            .append(",\"reranker\":").append(reranker == null ? "null" : HttpUtil.jsonString(reranker))
            .append(",\"reranker_available\":").append(rerankerAvailableNow())
            .append(",\"embedding_degraded\":");
        if (degradation == null) {
            body.append("null");
            return;
        }
        body.append("{\"chosen\":").append(HttpUtil.jsonString(degradation.chosen().value()))
            .append(",\"serving\":").append(HttpUtil.jsonString(degradation.serving().value()))
            .append(",\"missing\":").append(HttpUtil.jsonString(degradation.missingPiece()))
            .append(",\"remedy\":").append(HttpUtil.jsonString(degradation.remedy()))
            .append('}');
    }

    /** The live probe, as a bare boolean; a probe that throws is "not available", never a failed status poll. */
    private boolean rerankerAvailableNow() {
        try {
            return rerankerAvailable.getAsBoolean();
        } catch (RuntimeException e) {
            return false;
        }
    }
}
