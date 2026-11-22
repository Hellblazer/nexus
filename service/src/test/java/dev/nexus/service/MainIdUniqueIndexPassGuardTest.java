// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.datamove.CompletionBootWork;
import dev.nexus.service.datamove.IdUniqueIndexPass;
import dev.nexus.service.db.AdminConnection;
import org.junit.jupiter.api.Test;

import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Clock;

/**
 * RDR-229 P3.4 (nexus-p2m1x.22): Main's wiring of {@link IdUniqueIndexPass}, which no other test sees. The CompletionCheck tests run the gate 3
 * extras with fakes, so without these pins production could start no pass, or hand gate 3 no extras, and every suite would stay green (review
 * r1, S3/S5). Behaviour where it can be observed without a database; the text of Main where it cannot (comments stripped, so a pin is never
 * satisfied by a comment).
 */
class MainIdUniqueIndexPassGuardTest {

    private static final AdminConnection UNREACHABLE = new AdminConnection("jdbc:postgresql://127.0.0.1:1/none", "u", "p");

    @Test
    void withACompletionCheckThePassStarts_andReportsNothingAsInProgressYet() {
        var registry = new BootWorkRegistry(Clock.systemUTC());
        var check = CompletionBootWork.atBoot(registry, UNREACHABLE);   // built, not started: no connection is made
        IdUniqueIndexPass pass = Main.startIdUniqueIndexPass(registry, UNREACHABLE, check);
        try {
            assertThat(pass).as("the pass starts when there is a check to read gate 2 from").isNotNull();
            assertThat(registry.snapshot()).as("and nothing is reported as in progress before its first tick")
                .noneMatch(e -> IdUniqueIndexPass.BOOT_WORK_ID.equals(e.id()));
        } finally {
            if (pass != null) {
                pass.close();
            }
            check.close();
        }
    }

    @Test
    void withoutACompletionCheckNoPassIsStarted() {
        assertThat(Main.startIdUniqueIndexPass(new BootWorkRegistry(Clock.systemUTC()), UNREACHABLE, null))
            .as("its gate is the check's latest read; there is none to read").isNull();
    }

    @Test
    void mainStartsThePassOnGate2_closesIt_andHandsGate3ItsArbitersAsAnExtra() throws Exception {
        String src = code(Files.readString(Path.of("src/main/java/dev/nexus/service/Main.java")));
        assertThat(src).as("started once, beside the leaf-index pass")
            .containsOnlyOnce("var idUniqueIndexPass = startIdUniqueIndexPass(bootWork, adminConnection, completionCheck);");
        assertThat(src.indexOf("startIdUniqueIndexPass(bootWork, adminConnection, completionCheck)"))
            .as("after the completion check is built").isGreaterThan(src.indexOf("var completionCheck = startCompletionBootWork("));
        assertThat(src).as("its gate is gate 2 of the check's latest read").containsPattern("latest != null && latest\\.gate2\\(\\)");
        assertThat(src).as("closed at shutdown").contains("idUniqueIndexPass.close();");
        assertThat(src).as("gate 3 requires the arbiters (ruling 4)")
            .containsPattern("CompletionBootWork\\.atBoot\\(bootWork, admin, gate3,\\s*java\\.util\\.List\\.of\\(dev\\.nexus\\.service\\.datamove\\.IdUniqueIndexPass::allValid\\)\\)");
    }

    /** {@code src} without its comments. */
    private static String code(String src) {
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
            } else if (c == '"') {
                int j = i + 1;
                while (j < n && src.charAt(j) != '"') {
                    j += src.charAt(j) == '\\' ? 2 : 1;
                }
                out.append(src, i, Math.min(j + 1, n));
                i = j + 1;
            } else {
                out.append(c);
                i++;
            }
        }
        return out.toString();
    }
}
