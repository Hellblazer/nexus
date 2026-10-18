// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

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
 * good". A truncated or wrong-size model with both files on disk is reported PRESENT,
 * so {@link Bge768Embedder}'s own constructor then fails the boot loudly naming the
 * size floor — it is never quietly dropped as "absent". A half-provisioned install
 * (exactly one of the model and its tokenizer) is its own state, PARTIAL (nexus-ngvxb,
 * Sam 2026-10-18): it is not servable, so {@code bgePresent} is false, and {@code Main}
 * decides — degrade to Voyage-only with a loud WARN on a keyed box, fail the boot on a
 * keyless one. The probe never builds the embedder for either answer.
 *
 * <p>The Phase 2 cross-encoder placement (Step 4) adds its own files to this probe
 * when it needs them; Step 1 builds no cross-encoder on a path without bge.
 */
public final class LocalModelProbe {

    /**
     * What this boot can build.
     *
     * @param bgePresent       BOTH the bge model and tokenizer files exist (what {@code
     *                         Bge768Embedder} needs; validity is its constructor's job)
     * @param bgePartial       exactly ONE of the two exists: a half-provisioned install
     * @param voyageKeyPresent the Voyage key is non-null and non-blank
     * @param bgeDetail        human-readable file state (or the probe error), for the boot log
     * @param bgeMissing       the absent bge file paths ({@code bgePartial}: the one missing;
     *                         neither present: both; both present or a probe error: empty)
     */
    public record Presence(boolean bgePresent, boolean bgePartial, boolean voyageKeyPresent,
                           String bgeDetail, List<String> bgeMissing) {
        /** True when at least one embedder family can be built (a partial bge install is not one). */
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
            return new Presence(false, false, voyageKey != null && !voyageKey.isBlank(),
                    "probe_error=" + e.getClass().getSimpleName() + ": " + e.getMessage(), List.of());
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
            List<String> missing = new ArrayList<>(2);
            if (!model) missing.add(bgeModelPath);
            if (!tokenizer) missing.add(bgeTokenizerPath);
            return new Presence(model && tokenizer, model != tokenizer, key, detail, List.copyOf(missing));
        } catch (RuntimeException e) {
            return new Presence(false, false, key, "probe_error=" + e.getClass().getSimpleName()
                    + ": " + e.getMessage(), List.of());
        }
    }

    private LocalModelProbe() {}
}
