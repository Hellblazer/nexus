// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;

import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-37n79: {@code nexus-service --trust-anchors} prints what the default TrustManager accepts,
 * in the protocol {@code service/src/build/java/NativeTrustStore.java anchors} parses. The native
 * build runs it against the built executable; this pins the mode's selection and output on the JVM.
 */
class TrustAnchorsTest {

    @Test
    void onlyTheFlagAloneSelectsTheMode() {
        assertThat(TrustAnchors.requested(new String[]{"--trust-anchors"})).isTrue();
        assertThat(TrustAnchors.requested(new String[]{})).isFalse();
        assertThat(TrustAnchors.requested(new String[]{"--trust-anchors", "x"})).isFalse();
        assertThat(TrustAnchors.requested(new String[]{"--trust"})).isFalse();
    }

    @Test
    void printsTheCountThenOneSubjectLinePerAcceptedIssuer() throws Exception {
        var bytes = new ByteArrayOutputStream();
        TrustAnchors.print(new PrintStream(bytes, true, StandardCharsets.UTF_8));
        List<String> lines = bytes.toString(StandardCharsets.UTF_8).lines().toList();

        int accepted = TrustAnchors.accepted().size();
        assertThat(accepted).as("the JVM's default trust store is not empty").isPositive();
        assertThat(lines.get(0)).isEqualTo("trust_anchors count=" + accepted);
        assertThat(lines.subList(1, lines.size())).hasSize(accepted)
            .allMatch(l -> l.startsWith("trust_anchor ") && l.length() > "trust_anchor ".length());
    }
}
