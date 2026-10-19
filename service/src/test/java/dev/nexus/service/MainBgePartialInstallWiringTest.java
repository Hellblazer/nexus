// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;

import java.nio.file.Files;
import java.nio.file.Path;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-210 TD1, bead nexus-ngvxb (Sam, 2026-10-18): the three-way boot decision on the bge install state.
 * The DECISION (build bge / Voyage-only degraded / fail loud) is made by value in {@code BootPlan} since
 * RDR-210 Phase 2 Step 4 (nexus-un7vj) and asserted in {@code BootPlanTest}; {@code Main.main} cannot run in a
 * test, so this class only pins that Main ACTS on each answer, in order, and builds nothing local on the paths
 * that must not:
 * <ul>
 *   <li>both files present: build bge ({@code plan.buildsBge()});</li>
 *   <li>partial + Voyage key: WARN {@code bge_partial_install_degraded} naming the files and the remedy,
 *       build Voyage-only, no {@code Bge768Embedder};</li>
 *   <li>partial + no key: fail the boot loudly, no ORT.</li>
 * </ul>
 */
class MainBgePartialInstallWiringTest {

    private static final String FAIL_PARTIAL =
            "if (plan.bge() == dev.nexus.service.vectors.BootPlan.Bge.FAIL_PARTIAL_NO_KEY) {";
    private static final String DEGRADED_PARTIAL =
            "if (plan.bge() == dev.nexus.service.vectors.BootPlan.Bge.PARTIAL_DEGRADED) {";
    private static final String FAIL_NONE =
            "if (plan.bge() == dev.nexus.service.vectors.BootPlan.Bge.FAIL_NO_EMBEDDER) {";

    private static String main() throws Exception {
        // Maven runs a module's tests with the module directory as cwd.
        return strip(Files.readString(Path.of("src/main/java/dev/nexus/service/Main.java")));
    }

    /** Comments removed; string and char literals are scanned so a {@code //} in a URL survives. */
    private static String strip(String src) {
        StringBuilder out = new StringBuilder(src.length());
        int i = 0;
        int n = src.length();
        while (i < n) {
            char c = src.charAt(i);
            if (c == '/' && i + 1 < n && src.charAt(i + 1) == '/') {
                while (i < n && src.charAt(i) != '\n') {
                    i++;
                }
            } else if (c == '/' && i + 1 < n && src.charAt(i + 1) == '*') {
                int end = src.indexOf("*/", i + 2);
                i = end < 0 ? n : end + 2;
            } else if (c == '"' || c == '\'') {
                int end = literalEnd(src, i);
                out.append(src, i, end);
                i = end;
            } else {
                out.append(c);
                i++;
            }
        }
        return out.toString();
    }

    private static int literalEnd(String src, int start) {
        char quote = src.charAt(start);
        int i = start + 1;
        while (i < src.length() && src.charAt(i) != quote && src.charAt(i) != '\n') {
            i += src.charAt(i) == '\\' ? 2 : 1;
        }
        return Math.min(i + 1, src.length());
    }

    private static int matchingClose(String code, int open) {
        int depth = 0;
        int i = open;
        while (i < code.length()) {
            char c = code.charAt(i);
            if (c == '"' || c == '\'') {
                i = literalEnd(code, i);
                continue;
            }
            if (c == '{') {
                depth++;
            } else if (c == '}' && --depth == 0) {
                return i;
            }
            i++;
        }
        throw new AssertionError("unbalanced braces from " + open);
    }

    /** The body (between the braces) of the block opened by the first {@code header} at or after {@code from}. */
    private static String block(String code, String header, int from) {
        int at = code.indexOf(header, from);
        assertThat(at).as("missing: " + header).isPositive();
        int open = code.indexOf('{', at);
        return code.substring(open + 1, matchingClose(code, open));
    }

    @Test
    void theKeylessPartialInstallFailsTheBoot_withoutTouchingOrtOrTheEmbedder() throws Exception {
        String src = main();
        int decide = src.indexOf("BootPlan.decide(");
        String body = block(src, FAIL_PARTIAL, decide);
        assertThat(body).contains("event=bge_partial_install_no_voyage_key").contains("BootPlan.PROVISION_COMMAND");
        assertThat(body.strip()).as("the branch ends the process as its last statement").endsWith("System.exit(1);");
        assertThat(body).doesNotContain("Bge768Embedder").doesNotContain("applyAtBoot");
    }

    @Test
    void theKeyedPartialInstallDegradesLoudly_namingTheFilesAndTheRemedy_andBuildsNothingLocal() throws Exception {
        String src = main();
        int decide = src.indexOf("BootPlan.decide(");
        String whole = block(src, DEGRADED_PARTIAL, decide);
        assertThat(whole).as("the degrade is a WARN").contains("log.warn(\"event=bge_partial_install_degraded");
        assertThat(whole).contains("presence.bgeDetail()").contains("presence.bgeMissing()")
                .contains("BootPlan.PROVISION_COMMAND");
        assertThat(whole).as("no ORT use, no embedder and no exit on the partial-with-a-key path")
                .doesNotContain("Bge768Embedder").doesNotContain("applyAtBoot").doesNotContain("EmbedderRouter(")
                .doesNotContain("System.exit");
    }

    @Test
    void theDecisionRunsBeforeTheNoEmbedderCheck_soAPartialKeylessBoxGetsItsOwnMessage() throws Exception {
        String src = main();
        int decide = src.indexOf("BootPlan.decide(");
        assertThat(src.indexOf("BootPlan.decide(presence, embedProfile)")).as("fed the probe and the resolved profile")
                .isEqualTo(decide);
        int partialFail = src.indexOf(FAIL_PARTIAL, decide);
        int partialDegraded = src.indexOf(DEGRADED_PARTIAL, decide);
        int none = src.indexOf(FAIL_NONE, decide);
        int build = src.indexOf("if (plan.buildsBge()) {", decide);
        assertThat(decide).isPositive();
        assertThat(partialFail).isGreaterThan(decide);
        assertThat(partialDegraded).isGreaterThan(partialFail);
        assertThat(none).isGreaterThan(partialDegraded);
        assertThat(build).isGreaterThan(none);
    }

    @Test
    void theBgeEmbedderIsBuiltOnlyInsideThePlansBuildGuard_andOrtOnlyInsideTheFilesPresentGuard() throws Exception {
        String src = main();
        int guard = src.indexOf("if (plan.buildsBge()) {");
        assertThat(guard).as("one guard").isPositive();
        assertThat(src.indexOf("if (plan.buildsBge()) {", guard + 1)).isNegative();
        int open = src.indexOf('{', guard);
        int close = matchingClose(src, open);
        int bge = src.indexOf("new Bge768Embedder()");
        assertThat(bge).isPositive();
        assertThat(src.indexOf("new Bge768Embedder()", bge + 1)).as("built once").isNegative();
        assertThat(bge).as("sits inside the build guard").isBetween(open, close);

        int ortGuard = src.indexOf("if (plan.loadsOrtNativeDir()) {");
        assertThat(ortGuard).isPositive();
        int ortOpen = src.indexOf('{', ortGuard);
        int ortClose = matchingClose(src, ortOpen);
        int apply = src.indexOf("OrtNativeDir.applyAtBoot();");
        assertThat(apply).isPositive();
        assertThat(src.indexOf("OrtNativeDir.applyAtBoot();", apply + 1)).as("applied once").isNegative();
        assertThat(apply).as("applyAtBoot sits inside the files-present guard").isBetween(ortOpen, ortClose);
        assertThat(ortClose).as("and the ORT directory is set before the first embedder is built").isLessThan(bge);
        assertThat(src).as("the guards are plan answers, not a re-derivation from the probe in Main")
                .doesNotContain("bgePresent() || presence.bgePartial()")
                .doesNotContain("bgePartial() || presence.bgePresent()")
                .doesNotContain("if (presence.bgePresent()) {");
    }
}
