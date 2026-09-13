// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-904y8: {@code /version} carries how long this engine process has been
 * up, so a post-deploy gate can tell a cold-cache sample from a steady-state
 * one by reading a FACT rather than inferring it from a deploy artifact.
 *
 * <p>Why this exists (measured, 2026-09-12, engine-service-v0.1.116). A
 * migration that reads a large table evicts hot index pages, so the first
 * queries after a container swap are slow through no fault of the build. The
 * conexus STEP-6 gate computes its latency bound from a trailing window of
 * recent runs, so that first sample entered its own baseline, raised the bound
 * by 23%, and manufactured a false GREEN on the following run. The gate needs
 * to EXCLUDE a first-traffic sample, which means knowing whether the engine
 * just restarted.
 *
 * <p>Neither available proxy can answer that. The deploy's SSM tag timestamp
 * misses a redeploy that does not change the tag (a rollback re-run, or
 * re-invoking the same doc), so it UNDER-detects. The minimum backend_start
 * over service connections is disturbed by pool recycling, so it
 * OVER-detects and would exclude good runs. Only the engine knows its own
 * uptime.
 *
 * <p>BOTH forms are emitted and they answer different questions
 * (conexus-98's argument, which corrected an earlier absolute-only lean):
 *
 * <ul>
 *   <li>{@code process_uptime_seconds} is the PREDICATE. It is answered
 *       entirely on the engine's own clock and read at a known moment on the
 *       caller's, so comparing it to a threshold requires no agreement
 *       between the two clocks at all. A skew of minutes cannot shift the
 *       exclusion window.</li>
 *   <li>{@code process_start_time} is for CORRELATION — lining engine boot up
 *       against a recorded deploy timestamp, which is what catches the
 *       same-tag redeploy the SSM marker cannot see. This one DOES depend on
 *       clock agreement, which is exactly why it is not the predicate.</li>
 * </ul>
 *
 * <p>Both are additive: no existing field changes shape, so an older client
 * simply does not see them. Note that a NEW /version field is invisible to
 * cloud clients until the public edge allowlists it — deliberate, since the
 * edge trims this body rather than passing it through (the
 * {@code nx_answer_steps_supported} precedent, nexus-04sff).
 */
class VersionHandlerProcessUptimeTest {

    @Test
    void uptimeIsNonNegativeAndGrowsWithElapsedTime() {
        long start = System.currentTimeMillis();
        assertThat(VersionHandler.uptimeSeconds(start, start)).isZero();
        assertThat(VersionHandler.uptimeSeconds(start, start + 5_000L)).isEqualTo(5L);
        assertThat(VersionHandler.uptimeSeconds(start, start + 3_600_000L)).isEqualTo(3600L);
    }

    @Test
    void uptimeTruncatesRatherThanRounding() {
        long start = 1_000_000L;
        // 1900ms of uptime is 1 second up, not 2. A gate thresholding on
        // "at least N seconds" must never be told it has more than it has.
        assertThat(VersionHandler.uptimeSeconds(start, start + 1_900L)).isEqualTo(1L);
    }

    @Test
    void aClockGoingBackwardsReportsZeroRatherThanNegative() {
        /* NTP correction, or a suspended VM resuming. A negative uptime would
         * read as "up for less than no time", and a gate comparing it against
         * a threshold would treat it as freshly booted forever. Clamp to 0,
         * which is the honest worst case: the caller excludes the sample. */
        long start = System.currentTimeMillis();
        assertThat(VersionHandler.uptimeSeconds(start, start - 60_000L)).isZero();
    }

    @Test
    void startTimeRendersAsAnIsoInstantInUtc() {
        // Epoch millis 0 is the unambiguous fixture; a local-timezone
        // rendering would make this assertion machine-dependent, and the
        // whole point of the field is cross-machine correlation.
        assertThat(VersionHandler.startTimeIso(0L)).isEqualTo("1970-01-01T00:00:00Z");
        assertThat(VersionHandler.startTimeIso(1_757_000_000_000L)).endsWith("Z");
    }

    @Test
    void bothFieldsAppearInTheBodyWithTheContractedNames() {
        StringBuilder body = new StringBuilder();
        long start = System.currentTimeMillis() - 42_000L;
        VersionHandler.appendProcessUptimeFields(body, start, start + 42_000L);
        String json = body.toString();

        // The names are the wire contract the conexus gate keys on; a rename
        // is a wire change, not a refactor.
        assertThat(json).contains("\"process_uptime_seconds\":42");
        assertThat(json).contains("\"process_start_time\":\"");
        // Appended to an existing object, so it must lead with a comma and
        // never open a brace of its own.
        assertThat(json).startsWith(",");
        assertThat(json).doesNotContain("{");
    }

    @Test
    void uptimeIsEmittedAsABareNumberNotAString() {
        /* A gate does `uptime >= threshold`. A quoted value turns that into a
         * string comparison that silently succeeds for the wrong reason. */
        StringBuilder body = new StringBuilder();
        long start = System.currentTimeMillis();
        VersionHandler.appendProcessUptimeFields(body, start, start + 7_000L);
        assertThat(body.toString()).contains("\"process_uptime_seconds\":7,");
    }
}
