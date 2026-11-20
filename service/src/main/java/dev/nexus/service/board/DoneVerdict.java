// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.util.Optional;
import java.util.Set;

/**
 * The verdict a {@code board/ci/<base>--done} row carries: the tip sha and {@code success}, {@code failure} or
 * {@code cancelled}. It is {@code dims.branch_outcome} when present and not blank (the adapter's carry-forward of a
 * path-filtered workflow's last result on the branch, written once its FOLD_BRANCH_OUTCOME switch is on, so a Service
 * CI red that a later sha never re-ran still stands); else {@code dims.outcome} when present (the per-sha fold,
 * written once its FOLD_RESULT_DIMS switch is on); otherwise it is derived from the {@code runs} conclusions in the body with the template's rules: any run
 * that concluded failure, timed_out, startup_failure or action_required is a failure; else a run with an unknown
 * conclusion (including the old two-element entries), or a body whose {@code n} counts more runs than it lists
 * (trailing entries dropped), leaves no verdict; else a run that concluded cancelled or stale
 * is cancelled; else success. Only {@code success} and {@code failure} are verdicts.
 */
public final class DoneVerdict {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final Set<String> SUCCESS = Set.of("success", "neutral", "skipped");
    private static final Set<String> FAILURE = Set.of("failure", "timed_out", "startup_failure", "action_required");
    private static final Set<String> CANCELLED = Set.of("cancelled", "stale");

    public static final String OK = "success";
    public static final String RED = "failure";

    /** The tip and the outcome; the outcome is {@code success}, {@code failure} or {@code cancelled}. */
    public record Verdict(String sha, String outcome) {
        public boolean counts() {
            return OK.equals(outcome) || RED.equals(outcome);
        }
    }

    private DoneVerdict() {
    }

    /** The verdict of a done row, or empty when it is not a {@code kind=sha} row or its outcome cannot be told. */
    public static Optional<Verdict> of(BoardRow row) {
        if (!"sha".equals(row.kind())) {
            return Optional.empty();
        }
        JsonNode body = parse(row.body());
        String sha = row.sha();
        if ((sha == null || sha.isBlank()) && body != null && body.hasNonNull("sha")) {
            sha = body.get("sha").asText();
        }
        if (sha == null || sha.isBlank()) {
            return Optional.empty();
        }
        String outcome = row.dim("branch_outcome");
        if (outcome == null || outcome.isBlank()) {
            outcome = row.dim("outcome");
        }
        if (outcome == null || outcome.isBlank()) {
            outcome = derive(body);
        }
        return outcome == null ? Optional.empty() : Optional.of(new Verdict(sha, outcome));
    }

    /** The outcome from the body's {@code runs}, or null when none can be told. */
    static String derive(JsonNode body) {
        if (body == null || !body.has("runs") || !body.get("runs").isArray() || body.get("runs").isEmpty()) {
            return null;
        }
        boolean failed = false;
        boolean unknown = false;
        boolean cancelled = false;
        for (JsonNode run : body.get("runs")) {
            String conclusion = run.isArray() && run.size() >= 4 && run.get(3).isTextual() ? run.get(3).asText() : null;
            if (conclusion == null) {
                unknown = true;
            } else if (FAILURE.contains(conclusion)) {
                failed = true;
            } else if (CANCELLED.contains(conclusion)) {
                cancelled = true;
            } else if (!SUCCESS.contains(conclusion)) {
                unknown = true;
            }
        }
        if (failed) {
            return RED;
        }
        // Trailing runs were dropped (n counts the fold): with no failure listed, an unlisted one may have failed.
        boolean truncated = body.path("n").isNumber() && body.get("n").asLong() > body.get("runs").size();
        if (unknown || truncated) {
            return null;
        }
        return cancelled ? "cancelled" : OK;
    }

    private static JsonNode parse(String body) {
        if (body == null || body.isBlank()) {
            return null;
        }
        try {
            return MAPPER.readTree(body);
        } catch (java.io.IOException e) {
            return null;
        }
    }
}
