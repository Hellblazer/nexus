// SPDX-License-Identifier: AGPL-3.0-or-later
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;

import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-ogccs — the onnx_models root must resolve where the Python provisioner
 * writes (env override, then HOME), never bare {@code user.home} while HOME is
 * available: user.home is the passwd entry, and any HOME override (containers,
 * release-sandbox, CI) made the engine look somewhere the provisioner never
 * wrote.
 *
 * <p>Tests the pure resolver {@link OnnxModelPaths#resolve} (no real env
 * mutation), the {@link EgressProxy} testing idiom.
 */
class OnnxModelPathsTest {

    private static final String USER_HOME = "/passwd/entry";

    private static OnnxModelPaths.Resolved resolve(Map<String, String> env) {
        return OnnxModelPaths.resolve(env::get, USER_HOME);
    }

    @Test
    void explicitEnvOverrideWinsOverHome() {
        var r = resolve(Map.of(
                OnnxModelPaths.MODEL_DIR_ENV, "/custom/onnx_models",
                "HOME", "/home/nexus"));
        assertThat(r.root()).isEqualTo("/custom/onnx_models");
        assertThat(r.source()).isEqualTo("NX_ONNX_MODEL_DIR");
    }

    @Test
    void homeRungBeatsUserHome() {
        var r = resolve(Map.of("HOME", "/home/nexus/nexus-sandbox"));
        assertThat(r.root()).isEqualTo("/home/nexus/nexus-sandbox/.cache/nexus/onnx_models");
        assertThat(r.source()).isEqualTo("HOME");
    }

    @Test
    void userHomeIsTheLastResort() {
        var r = resolve(Map.of());
        assertThat(r.root()).isEqualTo(USER_HOME + "/.cache/nexus/onnx_models");
        assertThat(r.source()).isEqualTo("user.home");
    }

    @Test
    void blankValuesFallThrough() {
        // A set-but-blank env var is absence, not an empty root.
        var r = resolve(Map.of(OnnxModelPaths.MODEL_DIR_ENV, "   ", "HOME", ""));
        assertThat(r.source()).isEqualTo("user.home");
    }

    @Test
    void valuesAreTrimmed() {
        var r = resolve(Map.of(OnnxModelPaths.MODEL_DIR_ENV, " /custom/root "));
        assertThat(r.root()).isEqualTo("/custom/root");
    }

    @Test
    void embedderAndRerankerConstantsShareTheResolvedRoot() {
        // The per-model constants must all hang off ONE root so a single env
        // override moves every artifact path together.
        String root = OnnxModelPaths.root();
        assertThat(Bge768Embedder.DEFAULT_MODEL_PATH)
                .isEqualTo(root + "/bge-base-en-v1.5/onnx/model.onnx");
        assertThat(Bge768Embedder.DEFAULT_TOKENIZER_PATH)
                .isEqualTo(root + "/bge-base-en-v1.5/onnx/tokenizer.json");
        assertThat(CrossEncoderReranker.DEFAULT_MODEL_PATH)
                .isEqualTo(root + "/ms-marco-minilm-l6-v2/onnx/model.onnx");
        assertThat(CrossEncoderReranker.DEFAULT_TOKENIZER_PATH)
                .isEqualTo(root + "/ms-marco-minilm-l6-v2/onnx/tokenizer.json");
    }
}
