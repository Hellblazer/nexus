// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import ai.onnxruntime.OrtException;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;

import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-o5xyx.3 — {@link GatedRun}'s contract without a model: it needs only ORT's
 * bundled native library (for {@code RunOptions}), so it runs on CI, where the
 * model-gated {@link OrtRunShutdownSafetyTest} skips. Private gates throughout; the
 * process gate is never closed here.
 */
@Timeout(value = 30, unit = TimeUnit.SECONDS)
class GatedRunTest {

    @Test
    void openOnAClosedGateIsRefusedAndHoldsNothing() {
        OrtInitGate gate = new OrtInitGate();
        gate.quiesce(0);
        assertThatThrownBy(() -> GatedRun.open(gate, "late-run"))
                .isInstanceOf(OrtInitGate.ShutdownInProgressException.class)
                .hasMessageContaining("late-run");
        assertThat(gate.quiesce(0)).as("a refused open leaves nothing in flight").isTrue();
    }

    @Test
    void anOpenRunHoldsTheGateUntilClosed() throws Exception {
        OrtInitGate gate = new OrtInitGate();
        GatedRun run = GatedRun.open(gate, "run");
        assertThat(gate.quiesce(50)).as("an open run is in flight").isFalse();
        run.close();
        assertThat(gate.quiesce(0)).isTrue();
    }

    @Test
    void shutdownCancelsAnOpenRunAndTranslatesItsFailure() throws Exception {
        OrtInitGate gate = new OrtInitGate();
        try (GatedRun run = GatedRun.open(gate, "run")) {
            OrtException failure = new OrtException(OrtException.OrtErrorCode.ORT_FAIL, "Exiting due to terminate flag");
            assertThat(run.cancelledOr(failure))
                    .as("before shutdown, a run failure is reported as itself")
                    .isSameAs(failure);

            gate.quiesce(0);   // runs the canceller: sets the real terminate flag on the real RunOptions

            Exception translated = run.cancelledOr(failure);
            assertThat(translated)
                    .isInstanceOf(OrtInitGate.ShutdownInProgressException.class)
                    .hasMessageContaining("shutdown in progress")
                    .hasCause(failure);
        }
        assertThat(gate.quiesce(0)).isTrue();
    }

    @Test
    void theNativeImageRegistersOrtExceptionForJni() throws Exception {
        // The entry is hand-added to the traced metadata (a happy-path trace never saw ORT
        // fail); a re-trace would drop it silently. Without it a cancelled run is a fatal
        // JNI GetMethodID error in the native binary (measured: exit 99, not 143).
        var root = new com.fasterxml.jackson.databind.ObjectMapper().readTree(java.nio.file.Path.of(
                "src", "main", "resources", "META-INF", "native-image", "traced",
                "reachability-metadata.json").toFile());
        com.fasterxml.jackson.databind.JsonNode entry = null;
        for (var e : root.get("reflection")) {
            if ("ai.onnxruntime.OrtException".equals(e.path("type").asText())) entry = e;
        }
        assertThat(entry).as("OrtException must be registered for JNI").isNotNull();
        assertThat(entry.path("jniAccessible").asBoolean()).isTrue();
        assertThat(entry.path("methods").toString())
                .as("the (int, String) constructor libonnxruntime4j_jni throws through")
                .contains("\"<init>\"").contains("\"int\"").contains("\"java.lang.String\"");
    }

    @Test
    void closeIsIdempotentAndAClosedRunIsNeverCancelled() throws Exception {
        OrtInitGate gate = new OrtInitGate();
        GatedRun run = GatedRun.open(gate, "run");
        run.close();
        run.close();
        assertThat(gate.quiesce(0)).isTrue();
        OrtException failure = new OrtException(OrtException.OrtErrorCode.ORT_FAIL, "x");
        assertThat(run.cancelledOr(failure)).as("closed before shutdown: not cancelled").isSameAs(failure);
    }
}
