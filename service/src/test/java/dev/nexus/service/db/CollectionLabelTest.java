// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.junit.jupiter.params.provider.MethodSource;

import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 P3.6 and P3.8 (nexus-p2m1x.26, nexus-p2m1x.28): the minted label form, {@code c} followed by
 * decimal digits, lives in one place, so the label rule that reserves it and the minter that renders it
 * cannot drift apart.
 */
class CollectionLabelTest {

    @ParameterizedTest
    @ValueSource(strings = {"c1", "c0", "c42", "c007", "c1234567890123456789"})
    void theMintedFormIsCFollowedByAsciiDigitsWithLeadingZerosReserved(String label) {
        assertThat(CollectionLabel.isMintedForm(label)).isTrue();
    }

    @ParameterizedTest
    @ValueSource(strings = {"", "c", "C1", "cc1", "c1a", "c-1", "c 1", " c1", "c1 ", "c1\n", "c١",
        "c１", "docs__1-1__voyage-context-3__v1", "knowledge", "1c"})
    void anythingElseIsNotTheMintedForm(String label) {
        assertThat(CollectionLabel.isMintedForm(label)).isFalse();
    }

    @Test
    void aNullLabelIsNotTheMintedForm() {
        assertThat(CollectionLabel.isMintedForm(null)).isFalse();
    }

    @Test
    void mintedLabelRendersCAndTheIdWithNoPadding() {
        assertThat(CollectionLabel.mintedLabel(1)).isEqualTo("c1");
        assertThat(CollectionLabel.mintedLabel(7)).isEqualTo("c7");
        assertThat(CollectionLabel.mintedLabel(Long.MAX_VALUE)).isEqualTo("c" + Long.MAX_VALUE);
    }

    @ParameterizedTest
    @ValueSource(longs = {0, -1, Long.MIN_VALUE})
    void mintedLabelRefusesAnIdThatIsNotPositive(long id) {
        assertThatThrownBy(() -> CollectionLabel.mintedLabel(id)).isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void everyRenderedLabelIsOfTheMintedForm() {
        long[] ids = {1, 2, 9, 10, 99, 100, 12345, 1L << 31, 1L << 40, Long.MAX_VALUE - 1, Long.MAX_VALUE};
        for (long id : ids) {
            assertThat(CollectionLabel.isMintedForm(CollectionLabel.mintedLabel(id))).as("id %d", id).isTrue();
        }
        java.util.Random rnd = new java.util.Random(229L);
        for (int i = 0; i < 1000; i++) {
            long id = 1 + (rnd.nextLong() & Long.MAX_VALUE) % (Long.MAX_VALUE - 1);
            assertThat(CollectionLabel.isMintedForm(CollectionLabel.mintedLabel(id))).as("id %d", id).isTrue();
        }
    }

    // ── RDR-229 P3.6: validateUserLabel, the label rule's engine half (twin of corpus.validate_collection_name) ──

    private static String repeat(String s, int n) {
        return s.repeat(n);
    }

    static Stream<String> legalLabels() {
        return Stream.of(
            repeat("a", 100), "My Notes", "notes 50%: café", "日本語のノート", "ab", "a", "-leading-hyphen", "trailing_",
            "a.b", "code__myrepo", "docs__1-1__voyage-context-3__v1", "c", "C1", "c1a", "cc1", "c-1", "ac1",
            "c\u0661",                      // c + ARABIC-INDIC DIGIT ONE: not an ASCII digit
            repeat("é", 127),                // 254 bytes
            repeat("é", 127) + "a",          // exactly 255 bytes
            repeat("a", 255),
            "x\u200dy",                     // ZERO WIDTH JOINER (Cf) is allowed
            "a b", " leading space",
            "a\ud83d\ude00b");             // a supplementary character, a valid surrogate pair
    }

    static Stream<String> refusedLabels() {
        return Stream.of(
            "", "a\u0000b", "a\nb", "a\tb", "a\u001fb", "a\u007fb", "a\u0085b", "a\u2028b", "a\u2029b",
            "a\ud800b",                     // a lone high surrogate: not valid UTF-8
            "a\udc00b",                     // a lone low surrogate
            repeat("a", 256), repeat("é", 128), repeat("é", 127) + "ab",
            "c1", "c0", "c007", "c1234567890123456789",
            "a/b", "/");
    }

    @ParameterizedTest
    @MethodSource("legalLabels")
    void aLabelTheRuleAllowsIsAccepted(String label) {
        CollectionLabel.validateUserLabel(label);
    }

    @ParameterizedTest
    @MethodSource("refusedLabels")
    void aLabelTheRuleRefusesRaisesIllegalArgument(String label) {
        assertThatThrownBy(() -> CollectionLabel.validateUserLabel(label)).isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void aNullLabelIsRefused() {
        assertThatThrownBy(() -> CollectionLabel.validateUserLabel(null)).isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void theRefusalsNameTheirReason() {
        assertThatThrownBy(() -> CollectionLabel.validateUserLabel("c42")).hasMessageContaining("minted");
        assertThatThrownBy(() -> CollectionLabel.validateUserLabel(repeat("é", 128))).hasMessageContaining("256 bytes");
        assertThatThrownBy(() -> CollectionLabel.validateUserLabel("a\u0001b")).hasMessageContaining("control");
    }

    /**
     * The '/' clause is PENDING Sam's ruling (board ask rdr229-label-slash): NoteUri.collectionOf ends a collection
     * at the first '/'. If he allows '/', flip CollectionLabel.REFUSE_SLASH (and the client's LABEL_REFUSES_SLASH),
     * and change this one test and "a/b" and "/" in refusedLabels.
     */
    @Test
    void aSlashIsRefusedUntilSamRulesOnIt() {
        assertThat(CollectionLabel.REFUSE_SLASH).isTrue();
        assertThatThrownBy(() -> CollectionLabel.validateUserLabel("a/b")).hasMessageContaining("/");
    }

    @Test
    void theMintersOwnLabelIsStillAMintedLabelAndTheUserRuleDoesNotTouchIt() {
        // P3.8's minter never calls validateUserLabel: it renders through mintedLabel, which stays a minted form.
        String minted = CollectionLabel.mintedLabel(7);
        assertThat(CollectionLabel.isMintedForm(minted)).isTrue();
        assertThatThrownBy(() -> CollectionLabel.validateUserLabel(minted)).isInstanceOf(IllegalArgumentException.class);
    }
}
