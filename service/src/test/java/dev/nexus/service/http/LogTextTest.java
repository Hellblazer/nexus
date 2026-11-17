// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

/** nexus-dngtz.1.8: {@link LogText#escapeControl} keeps client text on one log line. */
class LogTextTest {

    @Test
    void aNewlineBecomesBackslashX0a() {
        assertThat(LogText.escapeControl("a\nb")).isEqualTo("a\\x0ab");
    }

    @Test
    void aCarriageReturnAndTabAndNulAreEscaped() {
        assertThat(LogText.escapeControl("\r")).isEqualTo("\\x0d");
        assertThat(LogText.escapeControl("\t")).isEqualTo("\\x09");
        assertThat(LogText.escapeControl("\u0000")).isEqualTo("\\x00");
        assertThat(LogText.escapeControl("\u001f")).isEqualTo("\\x1f");
    }

    @Test
    void aSingleBackslashBecomesTwo() {
        assertThat(LogText.escapeControl("\\")).isEqualTo("\\\\");
    }

    @Test
    void aTypedBackslashXTextCannotImpersonateAnEscape() {
        // The two characters backslash and 'n' are text, not a newline, and the result differs
        // from the escape of a real newline: the escape is injective.
        String typed = LogText.escapeControl("a\\x0ab");
        String real = LogText.escapeControl("a\nb");
        assertThat(typed).isEqualTo("a\\\\x0ab").isNotEqualTo(real);
    }

    @Test
    void deleteAndTheNextLineCharacterBecomeTwoDigitHex() {
        assertThat(LogText.escapeControl("\u007f")).isEqualTo("\\x7f");
        assertThat(LogText.escapeControl("\u0085")).isEqualTo("\\x85");
    }

    @Test
    void theLineAndParagraphSeparatorsBecomeFourDigitUnicodeEscapes() {
        assertThat(LogText.escapeControl("\u2028")).isEqualTo("\\u2028");
        assertThat(LogText.escapeControl("\u2029")).isEqualTo("\\u2029");
    }

    @Test
    void plainAsciiAndOrdinaryUnicodeAreUnchanged() {
        String plain = "/v1/catalog/doc?x=1 ~ {}[]";
        assertThat(LogText.escapeControl(plain)).isSameAs(plain);
        assertThat(LogText.escapeControl("café 中文")).isEqualTo("café 中文");
        assertThat(LogText.escapeControl("")).isEmpty();
    }

    @Test
    void nullBecomesADash() {
        assertThat(LogText.escapeControl(null)).isEqualTo("-");
    }

    @Test
    void everyControlCharacterIsEscapedToItsOwnHexForm() {
        // Enumerated by hand, not by the production predicate: C0 (0x00-0x1F), DEL and C1 (0x7F-0x9F),
        // and the two Unicode line separators.
        java.util.List<Integer> chars = new java.util.ArrayList<>();
        for (int c = 0x00; c <= 0x1F; c++) chars.add(c);
        for (int c = 0x7F; c <= 0x9F; c++) chars.add(c);
        chars.add(0x2028);
        chars.add(0x2029);
        assertThat(chars).as("non-vacuity: 32 + 33 + 2 characters").hasSize(67);
        for (int c : chars) {
            String expected = c < 0x100 ? String.format("\\x%02x", c) : String.format("\\u%04x", c);
            assertThat(LogText.escapeControl("a" + (char) c + "b"))
                .as("char 0x%04x", c).isEqualTo("a" + expected + "b");
        }
    }

    @Test
    void theC1ControlU009bIsEscaped() {
        assertThat(LogText.escapeControl("\u009b")).isEqualTo("\\x9b");
    }

    @Test
    void charactersJustOutsideTheControlRangesPassThrough() {
        // 0x20 (space), 0x7E (~), 0xA0 (no-break space), 0xA1, 0x2027 and 0x202A are not control chars.
        String ok = " ~\u00a0\u00a1\u2027\u202a";
        assertThat(LogText.escapeControl(ok)).isSameAs(ok);
    }

    // nexus-dngtz.1.11: the MDC value sits inside the bracketed prefix, so it also escapes the delimiters.

    @Test
    void mdcValueEscapesSpaceAndBracketsAndEquals() {
        assertThat(LogText.mdcValue("a b")).isEqualTo("a\\x20b");
        assertThat(LogText.mdcValue("a]b")).isEqualTo("a\\x5db");
        assertThat(LogText.mdcValue("a[b")).isEqualTo("a\\x5bb");
        assertThat(LogText.mdcValue("a=b")).isEqualTo("a\\x3db");
    }

    @Test
    void mdcValueEscapesNewlineAndDoublesBackslash() {
        assertThat(LogText.mdcValue("a\nb")).isEqualTo("a\\x0ab");
        assertThat(LogText.mdcValue("a\\x20b")).isEqualTo("a\\\\x20b");
    }

    @Test
    void mdcValueOfAForgedFieldCarriesNoDelimiter() {
        String out = LogText.mdcValue("x\n] - event=forged a=b");
        assertThat(out).isEqualTo("x\\x0a\\x5d\\x20-\\x20event\\x3dforged\\x20a\\x3db");
        assertThat(out).doesNotContain("\n", " ", "]", "[", "=");
    }

    @Test
    void mdcValueOfNullIsADash() {
        assertThat(LogText.mdcValue(null)).isEqualTo("-");
    }

    @Test
    void aPlainSessionIdIsUnchangedAndTheSameInstance() {
        String id = "sess-1.2_ab:CD/ef";
        assertThat(LogText.mdcValue(id)).isSameAs(id);
    }
}
