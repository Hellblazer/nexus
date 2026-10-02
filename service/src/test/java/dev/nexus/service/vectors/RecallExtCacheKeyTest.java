// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * The recall test's embedding cache key must change when the model (or tokenizer) file
 * changes, not only when the passages do, or a cache file left by an older model is
 * served to a newer one.
 */
class RecallExtCacheKeyTest {

    @TempDir
    Path dir;

    private Path file(String name, String content) throws Exception {
        Path p = dir.resolve(name);
        Files.write(p, content.getBytes(StandardCharsets.UTF_8));
        return p;
    }

    @Test
    void sameInputsGiveTheSameKey() throws Exception {
        List<String> passages = List.of("alpha", "beta");
        Path model = file("m.onnx", "model-v1");
        Path tok = file("t.json", "tok-v1");
        assertThat(ChunkLiveOwnersRecallExtendedIntegrationTest.cacheKey(passages, model, tok))
            .isEqualTo(ChunkLiveOwnersRecallExtendedIntegrationTest.cacheKey(passages, model, tok));
    }

    @Test
    void aDifferentModelMissesTheCache() throws Exception {
        List<String> passages = List.of("alpha", "beta");
        Path tok = file("t.json", "tok-v1");
        String v1 = ChunkLiveOwnersRecallExtendedIntegrationTest.cacheKey(passages, file("m1.onnx", "model-v1"), tok);
        String v2 = ChunkLiveOwnersRecallExtendedIntegrationTest.cacheKey(passages, file("m2.onnx", "model-v2"), tok);
        assertThat(v2).isNotEqualTo(v1);
    }

    @Test
    void aDifferentTokenizerMissesTheCache() throws Exception {
        List<String> passages = List.of("alpha", "beta");
        Path model = file("m.onnx", "model-v1");
        String t1 = ChunkLiveOwnersRecallExtendedIntegrationTest.cacheKey(passages, model, file("t1.json", "tok-v1"));
        String t2 = ChunkLiveOwnersRecallExtendedIntegrationTest.cacheKey(passages, model, file("t2.json", "tok-v2"));
        assertThat(t2).isNotEqualTo(t1);
    }

    @Test
    void differentPassagesMissTheCache() throws Exception {
        Path model = file("m.onnx", "model-v1");
        Path tok = file("t.json", "tok-v1");
        assertThat(ChunkLiveOwnersRecallExtendedIntegrationTest.cacheKey(List.of("a"), model, tok))
            .isNotEqualTo(ChunkLiveOwnersRecallExtendedIntegrationTest.cacheKey(List.of("b"), model, tok));
    }
}
