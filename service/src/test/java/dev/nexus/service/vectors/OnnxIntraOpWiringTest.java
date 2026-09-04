// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-00wsf residual — source-level proof that all three bare
 * {@code new OrtSession.SessionOptions()} sites actually apply
 * {@link OnnxThreadPolicy#intraOpThreads()} before creating the session.
 *
 * <p>{@code OrtSession.SessionOptions} has no getter for the configured
 * intra-op thread count (write-only native API — confirmed against the
 * onnxruntime 1.20.0 sources), so a real constructed session cannot be
 * asked "what bound did you get". This mirrors the repo's existing
 * source-scanning gate pattern (nexus-zrcj7's raw-SQL gate) to lock the
 * wiring: each named file must call {@code setIntraOpNumThreads} with an
 * argument sourced from {@code OnnxThreadPolicy}, not a literal or an
 * unbounded default, between its {@code new OrtSession.SessionOptions()}
 * and its {@code createSession} call.
 */
class OnnxIntraOpWiringTest {

    private static final Path SERVICE_MAIN = Path.of(
            "src", "main", "java", "dev", "nexus", "service", "vectors");

    private static final List<String> SITES = List.of(
            "Bge768Embedder.java", "OnnxEmbedder.java", "CrossEncoderReranker.java");

    @Test
    void everySessionOptionsSite_appliesTheSharedIntraOpBound() throws IOException {
        for (String file : SITES) {
            Path path = SERVICE_MAIN.resolve(file);
            assertThat(Files.isRegularFile(path))
                    .as("expected source file %s to exist", path)
                    .isTrue();
            String source = Files.readString(path);

            int newOptsIdx = source.indexOf("new OrtSession.SessionOptions()");
            assertThat(newOptsIdx)
                    .as("%s must construct an OrtSession.SessionOptions", file)
                    .isNotEqualTo(-1);

            int createSessionIdx = source.indexOf("createSession(", newOptsIdx);
            assertThat(createSessionIdx)
                    .as("%s must call createSession after constructing SessionOptions", file)
                    .isGreaterThan(newOptsIdx);

            String between = source.substring(newOptsIdx, createSessionIdx);
            assertThat(between)
                    .as("%s must bound intra-op threads via the shared OnnxThreadPolicy "
                            + "resolver before createSession — a bare SessionOptions here "
                            + "reopens nexus-00wsf's unbounded-thread residual", file)
                    .contains("setIntraOpNumThreads")
                    .contains("OnnxThreadPolicy.intraOpThreads()");
        }
    }
}
