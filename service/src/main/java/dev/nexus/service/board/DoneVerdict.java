// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.util.Optional;
import java.util.Set;

/**
 * The verdict a {@code board/ci/<base>--done} row carries: the tip sha and {@code success}, {@code failure} or
 * {@code cancelled}. Only {@code success} and {@code failure} are verdicts ({@link Verdict#counts}).
 *
 * <p>The per-sha result is {@code dims.outcome} when present (written once the adapter's FOLD_RESULT_DIMS switch is
 * on), else derived from the {@code runs} conclusions in the body with the template's rules: any run that concluded
 * failure, timed_out, startup_failure or action_required is a failure; else a run with an unknown conclusion
 * (including the old two-element entries), or a body whose {@code n} counts more runs than it lists (trailing entries
 * dropped), leaves no verdict; else a run that concluded cancelled or stale is cancelled; else success.
 *
 * <p>{@code dims.branch_outcome}, when present and not blank, is the per-sha result folded with the latest earlier
 * result of each path-filtered workflow that did not run on the sha (the adapter's FOLD_BRANCH_OUTCOME switch), so a
 * Service CI red that a later sha never re-ran still stands. It decides, with two limits. A per-sha failure is a
 * failure whatever it says (a red is red; a branch_outcome can only be as bad as the per-sha result, so a
 * contradiction is a writer bug and fails toward red). A value other than success, failure or cancelled is no verdict.
 *
 * <p>Without branch_outcome, a per-sha success is judged by the body's {@code carried} and {@code carried_n}: a null
 * {@code carried} (the carry read failed), a list holding an entry whose conclusion is not {@code success} (a carried
 * skipped or neutral does not pass, so a skipped latest run of a workflow cannot clear an earlier red; the sha's own
 * runs keep success, neutral and skipped) or is missing, or a {@code carried_n} above the list's length (entries were
 * trimmed, the hidden ones cannot be checked; an absent list counts as empty) leaves no verdict. The engine fails
 * toward not clearing. A body with neither {@code carried} nor {@code carried_n} reads as before.
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

    public static final String REASON_CARRY_FAILED = "carry_failed";
    public static final String REASON_CARRIED_NOT_GREEN = "carried_not_green";
    public static final String REASON_CANCELLED = "cancelled";
    public static final String REASON_UNKNOWN = "unknown";

    /**
     * A row's verdict, or its absence with the reason: {@code reason} is null when the verdict counts, else
     * {@code carry_failed} (the carry data is null, unreadable or trimmed), {@code carried_not_green} (a carried entry
     * is present and not success: develop holds a red or an unfinished result), {@code cancelled} or {@code unknown}
     * (not a sha row, no sha, an outcome that cannot be told or is not recognised).
     */
    public record Resolution(Optional<Verdict> verdict, String reason) {
    }

    /** The verdict of a done row, or empty when it is not a {@code kind=sha} row or its outcome cannot be told. */
    public static Optional<Verdict> of(BoardRow row) {
        return resolve(row).verdict();
    }

    public static Resolution resolve(BoardRow row) {
        if (!"sha".equals(row.kind())) {
            return none(REASON_UNKNOWN);
        }
        JsonNode body = parse(row.body());
        String sha = row.sha();
        if ((sha == null || sha.isBlank()) && body != null && body.hasNonNull("sha")) {
            sha = body.get("sha").asText();
        }
        if (sha == null || sha.isBlank()) {
            return none(REASON_UNKNOWN);
        }
        String perSha = row.dim("outcome");
        if (perSha == null || perSha.isBlank()) {
            perSha = derive(body);
        }
        String outcome;
        String branch = row.dim("branch_outcome");
        if (branch != null && !branch.isBlank()) {
            if (RED.equals(perSha)) {
                outcome = RED;
            } else if (!OK.equals(branch) && !RED.equals(branch) && !"cancelled".equals(branch)) {
                return none(REASON_UNKNOWN);
            } else {
                outcome = branch;
            }
        } else {
            outcome = perSha;
            if (OK.equals(outcome)) {
                String problem = carryProblem(body);
                if (problem != null) {
                    return none(problem);
                }
            }
        }
        if (outcome == null) {
            return none(REASON_UNKNOWN);
        }
        Verdict v = new Verdict(sha, outcome);
        return new Resolution(Optional.of(v), v.counts() ? null
            : "cancelled".equals(outcome) ? REASON_CANCELLED : REASON_UNKNOWN);
    }

    private static Resolution none(String reason) {
        return new Resolution(Optional.empty(), reason);
    }

    /**
     * Why the body's carry-forward cannot back a per-sha success, or null when it can: a body with neither
     * {@code carried} nor {@code carried_n}, or a list whose every entry is a {@code [workflow, run, conclusion, sha]}
     * with conclusion {@code success} (a carried skipped or neutral does not pass: a skipped latest run of a
     * workflow must not clear an earlier red) and no more entries counted in {@code carried_n} than listed. An absent
     * list counts as empty. {@link #REASON_CARRIED_NOT_GREEN} when an entry holds a conclusion other than success (a
     * visible red outranks an unreadable entry); {@link #REASON_CARRY_FAILED} for a null or non-list {@code carried},
     * an entry that cannot be read, or trimmed entries.
     */
    private static String carryProblem(JsonNode body) {
        if (body == null) {
            return null;
        }
        long counted = body.path("carried_n").isNumber() ? body.get("carried_n").asLong() : 0;
        if (!body.has("carried")) {
            return counted > 0 ? REASON_CARRY_FAILED : null;
        }
        JsonNode carried = body.get("carried");
        if (!carried.isArray()) {
            return REASON_CARRY_FAILED;   // null, or a shape nobody writes
        }
        boolean unreadable = false;
        for (JsonNode entry : carried) {
            JsonNode conclusion = entry.isArray() && entry.size() >= 3 ? entry.get(2) : null;
            if (conclusion == null || !conclusion.isTextual()) {
                unreadable = true;
            } else if (!OK.equals(conclusion.asText())) {
                return REASON_CARRIED_NOT_GREEN;
            }
        }
        return unreadable || counted > carried.size() ? REASON_CARRY_FAILED : null;
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
