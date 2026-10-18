// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Files;
import java.nio.file.Path;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;

/**
 * RDR-210 Phase 2 Step 1 (bead nexus-uaovy) — the presence probe is non-throwing,
 * answers "is it there" (not "is it good"), and never touches the ONNX runtime.
 */
class LocalModelProbeTest {

    @TempDir Path dir;

    private Path touch(String name, int bytes) throws Exception {
        Path p = dir.resolve(name);
        Files.write(p, new byte[bytes]);
        return p;
    }

    @Test
    void bothFilesPresent_isBgePresent() throws Exception {
        Path model = touch("model.onnx", 8);
        Path tok = touch("tokenizer.json", 8);
        var p = LocalModelProbe.probe(model.toString(), tok.toString(), null);
        assertThat(p.bgePresent()).isTrue();
        assertThat(p.voyageKeyPresent()).isFalse();
        assertThat(p.any()).isTrue();
    }

    @Test
    void noFiles_isBgeAbsent() {
        var p = LocalModelProbe.probe(dir.resolve("nope.onnx").toString(),
                dir.resolve("nope.json").toString(), null);
        assertThat(p.bgePresent()).isFalse();
        assertThat(p.any()).as("neither family present").isFalse();
        assertThat(p.bgeDetail()).contains("model=absent").contains("tokenizer=absent");
    }

    @Test
    void aTruncatedModel_isStillPresent_soTheConstructorFailsLoud() throws Exception {
        // 8 bytes is far under the size floor: presence is not validity, and the
        // boot failure naming the floor belongs to Bge768Embedder, not the probe.
        Path model = touch("model.onnx", 8);
        Path tok = touch("tokenizer.json", 8);
        assertThat(LocalModelProbe.probe(model.toString(), tok.toString(), "k").bgePresent()).isTrue();
    }

    @Test
    void aHalfProvisionedInstall_isPresent_notQuietlyAbsent() throws Exception {
        Path model = touch("model.onnx", 8);
        var modelOnly = LocalModelProbe.probe(model.toString(), dir.resolve("none.json").toString(), "k");
        assertThat(modelOnly.bgePresent()).isTrue();
        assertThat(modelOnly.bgeDetail()).contains("model=present").contains("tokenizer=absent");
        Path tok = touch("tokenizer.json", 8);
        var tokOnly = LocalModelProbe.probe(dir.resolve("none.onnx").toString(), tok.toString(), "k");
        assertThat(tokOnly.bgePresent()).isTrue();
    }

    @Test
    void aDirectoryWhereAFileShouldBe_isAbsent() throws Exception {
        Path d = Files.createDirectory(dir.resolve("model.onnx"));
        var p = LocalModelProbe.probe(d.toString(), dir.resolve("none.json").toString(), null);
        assertThat(p.bgePresent()).isFalse();
    }

    @Test
    void keyPresence_nullBlankAndSet() {
        String none = dir.resolve("none").toString();
        assertThat(LocalModelProbe.probe(none, none, null).voyageKeyPresent()).isFalse();
        assertThat(LocalModelProbe.probe(none, none, "").voyageKeyPresent()).isFalse();
        assertThat(LocalModelProbe.probe(none, none, "   ").voyageKeyPresent()).isFalse();
        var p = LocalModelProbe.probe(none, none, "pa-key");
        assertThat(p.voyageKeyPresent()).isTrue();
        assertThat(p.bgePresent()).isFalse();
        assertThat(p.any()).as("the cloud shape: key, no model files").isTrue();
    }

    @Test
    void neverThrows_onNullOrMalformedPaths() {
        assertThatCode(() -> LocalModelProbe.probe(null, null, null)).doesNotThrowAnyException();
        assertThatCode(() -> LocalModelProbe.probe("a\0b", "c\0d", "k")).doesNotThrowAnyException();
        var p = LocalModelProbe.probe("a\0b", "c\0d", "k");
        assertThat(p.bgePresent()).isFalse();
        assertThat(p.voyageKeyPresent()).isTrue();
        assertThat(p.bgeDetail()).isNotBlank();
        assertThatCode(() -> LocalModelProbe.probe("k")).doesNotThrowAnyException();
    }

    @Test
    void theProbeSourceNeverReferencesTheOnnxRuntime() throws Exception {
        // Constructing a session on a missing model segfaults, and the native-library
        // location is fixed once by Main AFTER this probe runs. The probe may not
        // import or name the runtime at all.
        String src = Files.readString(
                Path.of("src/main/java/dev/nexus/service/vectors/LocalModelProbe.java"));
        String code = src.replaceAll("(?s)/\\*.*?\\*/", "").replaceAll("//[^\\n]*", "");
        assertThat(code)
                .doesNotContain("onnxruntime")
                .doesNotContain("OrtEnvironment")
                .doesNotContain("OrtSession")
                .doesNotContain("OrtNativeDir")
                .doesNotContain("OrtInitGate")
                .doesNotContain("new Bge768Embedder")
                .doesNotContain("CrossEncoderReranker");
    }
}
