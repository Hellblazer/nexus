// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;

import java.nio.file.Files;
import java.nio.file.Path;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-37n79: {@code Main.main} cannot run in a test, so the {@code --trust-anchors} wiring is
 * pinned on its text. The native build's {@code verify-native-trust-anchors} step needs the mode
 * to answer before the engine starts anything; without this pin, moving or dropping the check
 * would show only on a native leg ("printed no trust_anchors line"). {@code TrustAnchorsTest}
 * covers the mode's selection and output by value.
 */
class MainTrustAnchorsWiringTest {

    @Test
    void mainAnswersTrustAnchorsAndReturnsBeforeStartingAnything() throws Exception {
        String src = Files.readString(Path.of("src/main/java/dev/nexus/service/Main.java"));
        String check = "if (TrustAnchors.requested(args)) {";
        int requested = src.indexOf(check);
        int print = src.indexOf("TrustAnchors.print(System.out);");
        int returns = src.indexOf("return;", print);
        int goOffline = src.indexOf("LocalTokenizers.goOffline();");

        assertThat(requested).as("Main checks for the mode").isPositive();
        assertThat(src.indexOf(check, requested + 1)).as("once").isNegative();
        assertThat(print).as("and prints the anchors when asked").isGreaterThan(requested);
        assertThat(returns).as("then returns").isGreaterThan(print).isLessThan(goOffline);
        assertThat(goOffline).as("before the first boot step").isGreaterThan(requested);
    }
}
