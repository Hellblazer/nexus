// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import org.junit.jupiter.api.Test;

import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-5clu8: the X-Nexus-Skipped-Collections header carries labels a raw header value cannot (a space, non-ASCII
 * letters, a comma). Each name is form-encoded and the names joined with a comma; a legacy name encodes to itself.
 * The client half is tests/db/test_skipped_collections_header.py, which decodes this test's exact values.
 */
class SkippedCollectionsHeaderTest {

    private static List<String> decode(String header) {
        List<String> out = new ArrayList<>();
        for (String part : header.split(",")) {
            out.add(URLDecoder.decode(part, StandardCharsets.UTF_8));
        }
        return out;
    }

    @Test
    void aLegacyNameEncodesToItselfSoAnOlderClientReadsItUnchanged() {
        List<String> legacy = List.of("docs__1-1__voyage-context-3__v1", "knowledge__notes", "code__a.b-c_d__v2");
        assertThat(VectorHandler.encodeSkippedCollections(legacy)).isEqualTo(String.join(",", legacy));
    }

    @Test
    void aLabelHoldingASpaceNonAsciiAPercentAColonOrACommaRoundTrips() {
        List<String> labels = List.of("notes 50%: café", "日本語のノート", "a,b", "plus+sign", "x‍y");
        String header = VectorHandler.encodeSkippedCollections(labels);
        assertThat(header).isEqualTo("notes+50%25%3A+caf%C3%A9,%E6%97%A5%E6%9C%AC%E8%AA%9E%E3%81%AE%E3%83%8E%E3%83%BC%E3%83%88,"
            + "a%2Cb,plus%2Bsign,x%E2%80%8Dy");
        assertThat(header.chars().allMatch(c -> c >= 0x21 && c <= 0x7e)).as("visible ASCII only").isTrue();
        assertThat(decode(header)).isEqualTo(labels);
    }
}
