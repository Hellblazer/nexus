// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.nio.file.Files;
import java.nio.file.Path;

/**
 * RDR-210 Phase 2 Step 1 (bead nexus-uaovy) — NON-THROWING presence probe: which
 * embedder families can this boot build?
 *
 * <p>{@code Main} asks this before it constructs anything, so a box with the bge
 * files, a box with the Voyage key, and a box with both each boot what they can
 * serve (Technical Design 1). The cloud container has a key and no model files; it
 * must boot Voyage-only with no ONNX session.
 *
 * <p><b>This class must never touch the ONNX runtime.</b> It only stats files.
 * Building a session on a missing model file segfaults the JVM (conexus STEP-5), and
 * the runtime's native-library location is fixed once by {@code
 * OrtNativeDir.applyAtBoot()}, which {@code Main} runs only when this probe finds
 * bge. The probe runs first, so it must not reference the runtime's classes (pinned
 * by {@code LocalModelProbeTest}).
 *
 * <p><b>Presence is not validity.</b> The probe answers "is it there", not "is it
 * good". A truncated or wrong-size model is reported PRESENT, so {@link
 * Bge768Embedder}'s own constructor then fails the boot loudly naming the size floor
 * — it is never quietly dropped as "absent". A half-provisioned install (the model
 * without its tokenizer, or the reverse) is likewise PRESENT, so the constructor's
 * missing-file error names the remedy rather than the engine silently serving Voyage
 * alone.
 *
 * <p>The Phase 2 cross-encoder placement (Step 4) adds its own files to this probe
 * when it needs them; Step 1 builds no cross-encoder on a path without bge.
 */
public final class LocalModelProbe {

    /**
     * What this boot can build.
     *
     * @param bgePresent       at least one of the bge model / tokenizer files exists
     * @param voyageKeyPresent the Voyage key is non-null and non-blank
     * @param bgeDetail        human-readable file state (or the probe error), for the boot log
     */
    public record Presence(boolean bgePresent, boolean voyageKeyPresent, String bgeDetail) {
        /** True when at least one embedder family can be built. */
        public boolean any() {
            return bgePresent || voyageKeyPresent;
        }
    }

    /**
     * Canonical bge layout under the onnx_models root — the same files {@code
     * Bge768Embedder} checks. Derived from {@link OnnxModelPaths} here, not read from
     * the embedder's constants, so the probe never loads that class (a static ORT
     * field there must not be able to break the probe contract). {@code
     * LocalModelProbeTest} pins these equal to the embedder's defaults.
     */
    static final String BGE_MODEL_SUFFIX = "/bge-base-en-v1.5/onnx/model.onnx";
    static final String BGE_TOKENIZER_SUFFIX = "/bge-base-en-v1.5/onnx/tokenizer.json";

    /** Probe the canonical bge artifact paths. Never throws. */
    public static Presence probe(String voyageKey) {
        String root;
        try {
            root = OnnxModelPaths.root();
        } catch (RuntimeException e) {
            return new Presence(false, voyageKey != null && !voyageKey.isBlank(),
                    "probe_error=" + e.getClass().getSimpleName() + ": " + e.getMessage());
        }
        return probe(root + BGE_MODEL_SUFFIX, root + BGE_TOKENIZER_SUFFIX, voyageKey);
    }

    /**
     * Probe explicit bge paths (tests, non-default provisioning). Never throws: an
     * unreadable or malformed path reads as absent, with the cause in {@code bgeDetail}.
     */
    public static Presence probe(String bgeModelPath, String bgeTokenizerPath, String voyageKey) {
        boolean key = voyageKey != null && !voyageKey.isBlank();
        try {
            boolean model = Files.isRegularFile(Path.of(bgeModelPath));
            boolean tokenizer = Files.isRegularFile(Path.of(bgeTokenizerPath));
            String detail = "model=" + (model ? "present" : "absent") + " path=" + bgeModelPath
                    + " tokenizer=" + (tokenizer ? "present" : "absent") + " path=" + bgeTokenizerPath;
            return new Presence(model || tokenizer, key, detail);
        } catch (RuntimeException e) {
            return new Presence(false, key, "probe_error=" + e.getClass().getSimpleName()
                    + ": " + e.getMessage());
        }
    }

    private LocalModelProbe() {}
}
