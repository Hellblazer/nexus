// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;

import java.nio.file.Files;
import java.nio.file.Path;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-210 TD1, bead nexus-ngvxb (Sam, 2026-10-18): the three-way boot decision on the bge install state.
 * {@code Main.main} cannot run in a test, so the decision is pinned on its text:
 * <ul>
 *   <li>both files present: build bge (the only place ORT is touched);</li>
 *   <li>partial + Voyage key: WARN {@code bge_partial_install_degraded} naming the files and {@code nx init},
 *       build Voyage-only, no ORT, no {@code Bge768Embedder};</li>
 *   <li>partial + no key: fail the boot loudly, no ORT.</li>
 * </ul>
 */
class MainBgePartialInstallWiringTest {

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
        int probe = src.indexOf("LocalModelProbe.probe(");
        String body = block(src, "if (!voyagePresent) {", src.indexOf("if (presence.bgePartial()) {", probe));
        assertThat(body).contains("event=bge_partial_install_no_voyage_key").contains("nx init");
        assertThat(body.strip()).as("the branch ends the process as its last statement").endsWith("System.exit(1);");
        assertThat(body).doesNotContain("Bge768Embedder").doesNotContain("applyAtBoot");
    }

    @Test
    void theKeyedPartialInstallDegradesLoudly_namingTheFilesAndTheRemedy_andBuildsNothingLocal() throws Exception {
        String src = main();
        int probe = src.indexOf("LocalModelProbe.probe(");
        int partial = src.indexOf("if (presence.bgePartial()) {", probe);
        assertThat(partial).isGreaterThan(probe);
        String whole = block(src, "if (presence.bgePartial()) {", probe);
        int warn = whole.indexOf("log.warn(\"event=bge_partial_install_degraded");
        assertThat(warn).as("the degrade is a WARN").isPositive();
        String degraded = whole.substring(warn);
        assertThat(degraded).contains("presence.bgeDetail()").contains("presence.bgeMissing()").contains("nx init");
        assertThat(whole).as("no ORT use and no embedder on either partial path")
                .doesNotContain("Bge768Embedder").doesNotContain("applyAtBoot").doesNotContain("EmbedderRouter(");
        assertThat(degraded).as("the keyed path continues the boot").doesNotContain("System.exit");
        assertThat(whole.indexOf("if (!voyagePresent) {")).as("the keyless exit comes first")
                .isPositive().isLessThan(warn);
    }

    @Test
    void theDecisionRunsBeforeTheNoEmbedderCheck_soAPartialKeylessBoxGetsItsOwnMessage() throws Exception {
        String src = main();
        int probe = src.indexOf("LocalModelProbe.probe(");
        int partial = src.indexOf("if (presence.bgePartial()) {", probe);
        int none = src.indexOf("if (!presence.any()) {", probe);
        int build = src.indexOf("if (presence.bgePresent()) {", probe);
        assertThat(probe).isPositive();
        assertThat(partial).isGreaterThan(probe);
        assertThat(none).isGreaterThan(partial);
        assertThat(build).isGreaterThan(none);
    }

    @Test
    void bgeAndOrtAreBuiltOnlyInsideTheBothFilesPresentGuard() throws Exception {
        String src = main();
        int guard = src.indexOf("if (presence.bgePresent()) {");
        assertThat(guard).as("one guard").isPositive();
        assertThat(src.indexOf("if (presence.bgePresent()) {", guard + 1)).isNegative();
        int open = src.indexOf('{', guard);
        int close = matchingClose(src, open);
        for (String needle : new String[] {"new Bge768Embedder()", "OrtNativeDir.applyAtBoot();"}) {
            int at = src.indexOf(needle);
            assertThat(at).as(needle).isPositive();
            assertThat(src.indexOf(needle, at + 1)).as(needle + " appears once").isNegative();
            assertThat(at).as(needle + " sits inside the both-files guard").isBetween(open, close);
        }
        assertThat(src).as("the guard is not widened to partial installs")
                .doesNotContain("bgePresent() || presence.bgePartial()")
                .doesNotContain("bgePartial() || presence.bgePresent()");
    }
}
