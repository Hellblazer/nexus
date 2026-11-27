// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.datamove.NameKeyDropMove;
import dev.nexus.service.datamove.RenameReadiness;
import org.junit.jupiter.api.Test;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Arrays;
import java.util.regex.Pattern;

/**
 * RDR-229 residual R8 (nexus-p2m1x.47), step 3: the one-cut rule. P3.5a's rename refusal ends only when the name-key drop move, which P3.7 registers,
 * has {@code completed_at}; a build that holds the refusal without the move's registration refuses every rename for ever, and one that registers the
 * move without the refusal lets a name-path rename run against readers on the id (RDR item 6, T2 {@code 229-research-69}). {@code Main.main} cannot be
 * run in a test, so, like {@link MainPciWiringTest}, the wiring is pinned on Main's code text (comments removed), and the two halves are compared:
 * the test fails when one is present and the other is not, and when both are absent (a sweep that found nothing is a failure).
 *
 * <p>The refusal half is {@code Main}'s rename readiness handed to {@code NexusService}, answered by the completion check whose
 * {@link RenameReadiness.Missing#NAME_KEY_DROP_MOVE} reads {@link NameKeyDropMove#LABEL}. The registration half is the engine pass
 * {@code NameKeyDropPass} that {@code Main} starts from the check's latest read, and that registers the same label.
 */
class Rdr229P37OneCutWiringTest {

    private static String code(String relativeToServiceMain) throws Exception {
        return MainPciWiringTest.code(Files.readString(Path.of("src/main/java/dev/nexus/service/" + relativeToServiceMain)));
    }

    private static boolean hasRefusal(String main) {
        // the readiness lambda answers from the completion check, and the service is built with it as its last argument
        return main.contains("c.renameReadiness().missing()")
            && Pattern.compile("new NexusService\\([^;]*,\\s*gate3,\\s*renameReadiness\\);", Pattern.DOTALL).matcher(main).find();
    }

    private static boolean hasRegistration(String main) {
        int at = main.indexOf("static dev.nexus.service.datamove.NameKeyDropPass startNameKeyDropPass(");
        if (at < 0 || !main.contains("startNameKeyDropPass(adminConnection, completionCheck);")) {
            return false;
        }
        String body = main.substring(at, Math.min(main.length(), at + 1500));
        return body.contains("new dev.nexus.service.datamove.NameKeyDropPass(") && body.contains("pass.start()");
    }

    @Test
    void theBuildHoldsTheRenameRefusalAndTheDropMovesRegistrationTogether_neverOneWithoutTheOther() throws Exception {
        String main = code("Main.java");
        boolean refusal = hasRefusal(main);
        boolean registration = hasRegistration(main);

        assertThat(refusal).as("Main hands the completion check's rename readiness to NexusService (the refusal)").isTrue();
        assertThat(registration).as("Main starts NameKeyDropPass from the completion check's latest read (the registration)").isTrue();
        assertThat(refusal).as("one engine cut: the refusal and the drop move's registration are both present or both absent").isEqualTo(registration);
    }

    @Test
    void theRefusalWaitsForTheLabelTheRegistrationWrites() throws Exception {
        assertThat(code("datamove/CompletionCheck.java")).as("the readiness reads the drop move's completed_at under its label")
            .contains("complete(ctx, NameKeyDropMove.LABEL");
        assertThat(code("datamove/NameKeyDropPass.java")).as("the pass registers that move").contains("NameKeyDropMove.registerIfAbsent(");
        assertThat(NameKeyDropMove.LABEL).as("the label both name").isEqualTo("rdr229-name-key-drop");
    }

    @Test
    void theRefusalNamesOnlyConditionsSomethingInTheBuildCanEnd() {
        // a new Missing value is a new thing a rename can wait for: whoever adds one decides what ends it, and updates this pin with the registration or
        // restart that does
        assertThat(Arrays.stream(RenameReadiness.Missing.values()).map(Enum::name))
            .containsExactlyInAnyOrder("NONE", "NAME_KEY_DROP_MOVE", "P35B_LIST_MISSING", "P35B_RESTART", "READINESS_UNREADABLE",
                "P35C_READERS_ABSENT", "LATCH_ROW_MISSING");
    }
}
