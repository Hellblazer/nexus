// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Assumptions;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;

import static org.assertj.core.api.Assertions.fail;

/**
 * nexus-o5xyx.1 — decides whether a model-gated test runs, skips, or FAILS.
 *
 * <p>A bare {@code assumeTrue} lets a model-gated test skip silently on a host
 * that is supposed to have the model (a HOME override, a half-finished
 * provision), which is how a crash regression test turns vacuous. Three rules:
 * <ol>
 *   <li>model complete at the canonical path: run;</li>
 *   <li>{@value #REQUIRE_ENV}{@code =1} and the model is missing or truncated: FAIL.
 *       Set it on every host that provisions the model (hellmini);</li>
 *   <li>the model directory exists but the files are missing or under the size
 *       floor (a partial provision): FAIL, whatever the env says;</li>
 *   <li>otherwise (nothing provisioned, as on CI): skip loudly.</li>
 * </ol>
 */
final class OrtTestModel {

    static final String REQUIRE_ENV = "NX_REQUIRE_ORT_MODEL";

    /** Mirrors the Python provisioner's floor and Bge768Embedder.MIN_MODEL_BYTES. */
    private static final long MIN_MODEL_BYTES = 200_000_000L;

    static void requireBgeOrSkip() {
        Path model = Path.of(Bge768Embedder.DEFAULT_MODEL_PATH);
        Path tokenizer = Path.of(Bge768Embedder.DEFAULT_TOKENIZER_PATH);
        boolean complete = complete(model, tokenizer);
        if (complete) return;

        String why = "bge model not complete at " + model + " (tokenizer " + tokenizer + ")";
        boolean required = "1".equals(System.getenv(REQUIRE_ENV));
        boolean partial = Files.isDirectory(model.getParent());
        if (required) {
            fail("%s=1 but %s; provision it with `nx init --service`", REQUIRE_ENV, why);
        }
        if (partial) {
            fail("the model directory %s exists but is incomplete: %s. A half-provisioned model "
                    + "must not turn a crash regression test into a silent skip", model.getParent(), why);
        }
        Assumptions.assumeTrue(false, "SKIPPED (not passed): " + why
                + ". nexus-o5xyx.1's crash is only reachable with the real model.");
    }

    private static boolean complete(Path model, Path tokenizer) {
        try {
            return Files.isRegularFile(model) && Files.size(model) >= MIN_MODEL_BYTES
                    && Files.isRegularFile(tokenizer) && Files.size(tokenizer) > 0;
        } catch (IOException e) {
            return false;
        }
    }

    private OrtTestModel() {}
}
