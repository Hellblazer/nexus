// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

/**
 * What may reach the {@code ownerless_chunk_write_*} log line from a tenant or a client (nexus-z0o2p.36
 * round 2, code review M3): each forgery below is a value that, left alone, would end the record, add a
 * field or close a delimiter. Each test names the one rule that stops it.
 */
class OwnerlessLogTextTest {

    @ParameterizedTest
    @ValueSource(strings = {"\u001b", "\u0000", "\r", "\n", " ", " ", "\u0085", "‮", "​",
        "\u007f", "\t", " "})
    void plain_turnsEveryControlSeparatorAndFormatCharacterIntoASpace(String bad) {
        String out = OwnerlessLogText.plain("a" + bad + "b", 100);
        assertThat(out).as("U+%04X", (int) bad.charAt(0)).isEqualTo("a b");
    }

    @Test
    void plain_collapsesRunsTrimsAndNeverReturnsNull() {
        assertThat(OwnerlessLogText.plain("    a \r\n\u001b b \u0000", 100)).isEqualTo("a b");
        assertThat(OwnerlessLogText.plain(null, 100)).isEmpty();
        assertThat(OwnerlessLogText.plain("  ", 100)).isEmpty();
    }

    @Test
    void plain_cutsToTheBoundInCodePoints_neverInsideASurrogatePair() {
        String smiley = new String(Character.toChars(0x1F600));
        assertThat(OwnerlessLogText.plain("abc" + smiley + smiley, 4)).isEqualTo("abc" + smiley);
        assertThat(OwnerlessLogText.plain("x".repeat(500), 120)).hasSize(120);
    }

    @Test
    void plain_replacesAnUnpairedSurrogate() {
        assertThat(OwnerlessLogText.plain("a\ud800b", 100)).isEqualTo("a b");
    }

    @Test
    void quoted_cannotCloseItsQuoteOrEscapeTheClosingOne() {
        assertThat(OwnerlessLogText.quoted("1.0\" tenant=evil x=\"", 100)).isEqualTo("1.0' tenant=evil x='");
        assertThat(OwnerlessLogText.quoted("end\\", 100)).as("a trailing backslash must not escape the quote")
            .isEqualTo("end/");
    }

    @Test
    void quoted_blankAndControlOnlyAreAbsent() {
        assertThat(OwnerlessLogText.quoted(null, 100)).isEqualTo("absent");
        assertThat(OwnerlessLogText.quoted("   ", 100)).isEqualTo("absent");
        assertThat(OwnerlessLogText.quoted(" \u001b", 100)).isEqualTo("absent");
    }

    @Test
    void metaValue_cannotEndItsPairStartOneOrCloseTheBracket() {
        assertThat(OwnerlessLogText.metaValue("x;source_agent=evil;", 100))
            .as("';' ends a pair and '=' starts a value").isEqualTo("x,source_agent:evil,");
        assertThat(OwnerlessLogText.metaValue("x] y [z", 100)).isEqualTo("x) y (z");
        assertThat(OwnerlessLogText.metaValue("a\"b\\c", 100)).isEqualTo("a'b/c");
        assertThat(OwnerlessLogText.metaValue("t u\u001bv", 100)).isEqualTo("t u v");
    }

    @Test
    void metaValue_redactsTheUserinfoAndTheQueryOfAUrl() {
        assertThat(OwnerlessLogText.metaValue("https://alice:s3cret@example.org/a/b?token=abc#frag", 200))
            .isEqualTo("https://REDACTED@example.org/a/b?REDACTED")
            .doesNotContain("alice").doesNotContain("s3cret").doesNotContain("token");
        assertThat(OwnerlessLogText.metaValue("https://example.org/a/b", 200))
            .as("a clean URL is left alone").isEqualTo("https://example.org/a/b");
        assertThat(OwnerlessLogText.metaValue("What now? A title #2", 200))
            .as("a title that merely contains ? or # is not a URL").isEqualTo("What now? A title #2");
        assertThat(OwnerlessLogText.metaValue("/home/u/notes?.md", 200))
            .as("a path is not a URL").isEqualTo("/home/u/notes?.md");
    }
}
