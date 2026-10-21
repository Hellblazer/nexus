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
 * <p>The local cross-encoder reranker's files (model and tokenizer, apart from the bge files) are
 * probed the same way (RDR-210 Phase 2 Step 4, bead nexus-un7vj): the cross-encoder takes no
 * embedder argument and constructs lazily, so {@code Main} builds it whether or not bge is present,
 * and widens the ONNX native-library setup to {@code bgePresent || crossEncoderPresent}, keyed on
 * the files that are PRESENT, so the cloud shape (key, no model files) still creates nothing.
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
     * @param crossEncoderPresent BOTH the cross-encoder model and tokenizer files exist (presence,
     *                         not validity: its lazy init fails loud per request on a bad file)
     * @param crossEncoderDetail  human-readable file state of the cross-encoder, for the boot log
     * @param crossEncoderMissing the absent cross-encoder file paths (empty when both are present,
     *                         on a probe error, or when the cross-encoder was not probed)
     */
    public record Presence(boolean bgePresent, boolean bgePartial, boolean voyageKeyPresent,
                           String bgeDetail, List<String> bgeMissing,
                           boolean crossEncoderPresent, String crossEncoderDetail,
                           List<String> crossEncoderMissing) {

        /** A presence that says nothing about the cross-encoder (reported absent, "not probed"). */
        public Presence(boolean bgePresent, boolean bgePartial, boolean voyageKeyPresent,
                        String bgeDetail, List<String> bgeMissing) {
            this(bgePresent, bgePartial, voyageKeyPresent, bgeDetail, bgeMissing,
                    false, NOT_PROBED, List.of());
        }

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

    /**
     * Canonical cross-encoder layout, derived from {@link OnnxModelPaths} for the same reason as the
     * bge suffixes: {@link CrossEncoderReranker} imports the runtime's classes, and this probe may not
     * load them. {@code LocalModelProbeTest} pins these equal to the reranker's defaults.
     */
    static final String CROSS_ENCODER_MODEL_SUFFIX = "/ms-marco-minilm-l6-v2/onnx/model.onnx";
    static final String CROSS_ENCODER_TOKENIZER_SUFFIX = "/ms-marco-minilm-l6-v2/onnx/tokenizer.json";

    private static final String NOT_PROBED = "not probed";

    /** Probe the canonical bge and cross-encoder artifact paths. Never throws. */
    public static Presence probe(String voyageKey) {
        String root;
        try {
            root = OnnxModelPaths.root();
        } catch (RuntimeException e) {
            String detail = "probe_error=" + e.getClass().getSimpleName() + ": " + e.getMessage();
            return new Presence(false, false, voyageKey != null && !voyageKey.isBlank(), detail, List.of(),
                    false, detail, List.of());
        }
        return probe(root + BGE_MODEL_SUFFIX, root + BGE_TOKENIZER_SUFFIX,
                root + CROSS_ENCODER_MODEL_SUFFIX, root + CROSS_ENCODER_TOKENIZER_SUFFIX, voyageKey);
    }

    /**
     * Probe explicit bge paths (tests, non-default provisioning). Never throws: an
     * unreadable or malformed path reads as absent, with the cause in {@code bgeDetail}.
     * The cross-encoder is not probed (reported absent): use the five-path form.
     */
    public static Presence probe(String bgeModelPath, String bgeTokenizerPath, String voyageKey) {
        return probe(bgeModelPath, bgeTokenizerPath, null, null, voyageKey);
    }

    /**
     * Probe explicit bge and cross-encoder paths. Never throws. {@code null} cross-encoder paths
     * mean "not probed": absent, with nothing listed as missing.
     */
    public static Presence probe(String bgeModelPath, String bgeTokenizerPath,
                                 String crossEncoderModelPath, String crossEncoderTokenizerPath,
                                 String voyageKey) {
        boolean key = voyageKey != null && !voyageKey.isBlank();
        FileState bge = fileState(bgeModelPath, bgeTokenizerPath);
        FileState ce = crossEncoderModelPath == null && crossEncoderTokenizerPath == null
                ? new FileState(false, false, NOT_PROBED, List.of())
                : fileState(crossEncoderModelPath, crossEncoderTokenizerPath);
        return new Presence(bge.both(), bge.partial(), key, bge.detail(), bge.missing(),
                ce.both(), ce.detail(), ce.missing());
    }

    /** What the two files of one model are on disk. */
    private record FileState(boolean both, boolean partial, String detail, List<String> missing) {}

    private static FileState fileState(String modelPath, String tokenizerPath) {
        try {
            boolean model = Files.isRegularFile(Path.of(modelPath));
            boolean tokenizer = Files.isRegularFile(Path.of(tokenizerPath));
            String detail = "model=" + (model ? "present" : "absent") + " path=" + modelPath
                    + " tokenizer=" + (tokenizer ? "present" : "absent") + " path=" + tokenizerPath;
            List<String> missing = new ArrayList<>(2);
            if (!model) missing.add(modelPath);
            if (!tokenizer) missing.add(tokenizerPath);
            return new FileState(model && tokenizer, model != tokenizer, detail, List.copyOf(missing));
        } catch (RuntimeException e) {
            return new FileState(false, false, "probe_error=" + e.getClass().getSimpleName()
                    + ": " + e.getMessage(), List.of());
        }
    }

    private LocalModelProbe() {}
}
