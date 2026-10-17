// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import ai.djl.huggingface.tokenizers.HuggingFaceTokenizer;

import java.io.IOException;
import java.nio.file.Path;

/**
 * nexus-17jvq -- the one place the engine builds a DJL HuggingFace tokenizer, and the reason it is one place.
 *
 * <p>DJL's {@code HuggingFaceTokenizer.newInstance} calls {@code ai.djl.util.Ec2Utils.callHome} on every build.
 * Unless {@code OPT_OUT_TRACKING} is set, that dials {@code http://169.254.169.254/latest/api/token} (an IMDSv2 token
 * request, four connects at start on a box that is no cloud instance), reads the instance id and region, and on a
 * real EC2 host posts them to a DJL telemetry bucket. The engine has no AWS SDK; this was the whole source of the
 * link-local connects. A local install must not probe a cloud metadata service, so every tokenizer the engine
 * builds is built here, after the opt-out is set.
 *
 * <p>Only the local ONNX path builds tokenizers (the Voyage/cloud branch of {@code Main} constructs no embedder or
 * reranker that does), so this does not change cloud behaviour. DJL reads the environment variable before the system
 * property, so an operator who exports {@code OPT_OUT_TRACKING} keeps their value.
 */
public final class LocalTokenizers {

    /** The name DJL looks up, as an environment variable first and a system property second. */
    public static final String OPT_OUT_PROPERTY = "OPT_OUT_TRACKING";

    private LocalTokenizers() { }

    /** Sets DJL's telemetry opt-out for this JVM unless the operator already chose a value. Idempotent. */
    public static void optOutOfTelemetry() {
        if (System.getenv(OPT_OUT_PROPERTY) == null && System.getProperty(OPT_OUT_PROPERTY) == null) {
            System.setProperty(OPT_OUT_PROPERTY, "true");
        }
    }

    /** A truncating, non-padding tokenizer loaded from {@code tokenizerPath}; the callers pad per batch. */
    public static HuggingFaceTokenizer build(String tokenizerPath, int maxLength) throws IOException {
        optOutOfTelemetry();
        return HuggingFaceTokenizer.builder()
                .optTokenizerPath(Path.of(tokenizerPath))
                .optMaxLength(maxLength)
                .optTruncation(true)
                .optPadding(false)
                .build();
    }
}
