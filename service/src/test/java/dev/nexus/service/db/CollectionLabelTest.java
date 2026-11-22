// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.junit.jupiter.params.provider.MethodSource;
import org.junit.jupiter.params.provider.Arguments;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.io.IOException;
import java.io.InputStream;
import java.io.UncheckedIOException;
import java.util.ArrayList;
import java.util.List;

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

    /**
     * The case table the client's tests/test_rdr229_label_rule.py reads too (nexus-mrmqw): {@code rules} is the clause
     * order both halves check in, and each case names the first clause its label breaks ({@code null}: legal). The
     * engine's verdict is {@link CollectionLabel#shapeBreach}, then the minted form ({@code not_minted}), which is
     * {@link CollectionLabel#validateUserLabel}'s order.
     */
    private static final JsonNode CASES = readCases();

    private static JsonNode readCases() {
        try (InputStream in = CollectionLabelTest.class.getResourceAsStream("/label-rule-cases.json")) {
            if (in == null) {
                throw new IllegalStateException("resource label-rule-cases.json is missing");
            }
            return new ObjectMapper().readTree(in);
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
    }

    private static String labelOf(JsonNode c) {
        if (c.has("label")) {
            return c.get("label").asText();
        }
        StringBuilder sb = new StringBuilder();
        for (JsonNode part : c.get("parts")) {
            sb.append(repeat(part.get(0).asText(), part.get(1).asInt()));
        }
        return sb.toString();
    }

    static Stream<Arguments> cases() {
        List<Arguments> out = new ArrayList<>();
        for (JsonNode c : CASES.get("cases")) {
            out.add(Arguments.of(labelOf(c), c.get("rule").isNull() ? null : c.get("rule").asText()));
        }
        return out.stream();
    }

    private static String engineRule(String label) {
        return CollectionLabel.shapeBreach(label).map(CollectionLabel.Breach::rule)
            .orElse(CollectionLabel.isMintedForm(label) ? "not_minted" : null);
    }

    @Test
    void theCaseTableIsNotEmptyAndCoversEveryRule() {
        assertThat(CASES.get("cases").size()).isGreaterThanOrEqualTo(60);
        java.util.Set<String> named = new java.util.HashSet<>();
        CASES.get("cases").forEach(c -> named.add(c.get("rule").isNull() ? "legal" : c.get("rule").asText()));
        java.util.Set<String> expected = new java.util.HashSet<>(CollectionLabel.shapeRules());
        expected.add("not_minted");
        expected.add("legal");
        assertThat(named).isEqualTo(expected);
    }

    /** nexus-pqirj: the engine checks the clauses in the table's order, as the client's LABEL_RULES does. */
    @Test
    void theEngineChecksTheClausesInTheTablesOrder() {
        List<String> order = new ArrayList<>(CollectionLabel.shapeRules());
        order.add("not_minted");
        List<String> table = new ArrayList<>();
        CASES.get("rules").forEach(r -> table.add(r.asText()));
        assertThat(order).isEqualTo(table);
    }

    @ParameterizedTest
    @MethodSource("cases")
    void eachCaseBreaksTheRuleTheTableNames(String label, String rule) {
        assertThat(engineRule(label)).isEqualTo(rule);
        if (rule == null) {
            CollectionLabel.validateUserLabel(label);
        } else {
            assertThatThrownBy(() -> CollectionLabel.validateUserLabel(label)).isInstanceOf(IllegalArgumentException.class);
        }
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
     * The '/' clause is Sam's ruling (T2 nexus/sam-ruling-rdr229-label-slash-2026-11-21): NoteUri.collectionOf ends a
     * collection at the first '/', and file paths are built from a label. A later ruling flips
     * CollectionLabel.REFUSE_SLASH (and the client's LABEL_REFUSES_SLASH) and changes this test and "a/b" and "/" in
     * label-rule-cases.json.
     */
    @Test
    void aLabelHoldingASlashIsRefused() {
        assertThat(CollectionLabel.REFUSE_SLASH).isTrue();
        assertThatThrownBy(() -> CollectionLabel.validateUserLabel("a/b")).hasMessageContaining("/");
    }

    /**
     * The ',' and edge-whitespace clause is Sam's ruling on ask rdr229-label-comma-space (2026-11-21): MCP search and
     * plan confinement read a corpus list with split(",") then strip(). Whitespace is the Zs category, Python's
     * str.strip() set once controls are refused, so the engine and the client refuse the same labels.
     */
    @Test
    void aLabelHoldingACommaOrEdgeWhitespaceIsRefused() {
        assertThat(CollectionLabel.REFUSE_COMMA_AND_EDGE_SPACE).isTrue();
        for (String label : java.util.List.of("a,b", ",")) {
            assertThatThrownBy(() -> CollectionLabel.validateUserLabel(label)).hasMessageContaining("','");
            assertThat(CollectionLabel.shapeBreach(label)).map(CollectionLabel.Breach::rule).contains("no_comma");
        }
        for (String label : java.util.List.of(" notes", "notes ", " ", "\u00a0notes", "notes\u3000", "\u2003")) {
            assertThatThrownBy(() -> CollectionLabel.validateUserLabel(label)).hasMessageContaining("whitespace");
            assertThat(CollectionLabel.shapeBreach(label)).map(CollectionLabel.Breach::rule).contains("no_edge_whitespace");
        }
        for (String label : java.util.List.of("notes 50%: café", "a b", "a\u00a0b", "notes\u200d")) {
            CollectionLabel.validateUserLabel(label);
        }
    }

    @Test
    void theMintersOwnLabelIsStillAMintedLabelAndTheUserRuleDoesNotTouchIt() {
        // P3.8's minter never calls validateUserLabel: it renders through mintedLabel, which stays a minted form.
        String minted = CollectionLabel.mintedLabel(7);
        assertThat(CollectionLabel.isMintedForm(minted)).isTrue();
        assertThatThrownBy(() -> CollectionLabel.validateUserLabel(minted)).isInstanceOf(IllegalArgumentException.class);
    }

    // ── the two-stage use: what must stand before a read, and the shape without the minted form ──

    @Test
    void requireStorableRefusesOnlyWhatTheDatabaseCannotStore() {
        CollectionLabel.requireStorable(null);
        CollectionLabel.requireStorable("a/b");
        CollectionLabel.requireStorable(repeat("é", 200));
        CollectionLabel.requireStorable("a\u0001b");
        CollectionLabel.requireStorable("c1");
        assertThatThrownBy(() -> CollectionLabel.requireStorable("a\u0000b")).isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("control");
        assertThatThrownBy(() -> CollectionLabel.requireStorable("a\ud800b")).isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void validateShapeRefusesTheShapeAndLetsTheMintedFormThrough() {
        CollectionLabel.validateShape("c42");
        CollectionLabel.validateShape("My Notes");
        assertThatThrownBy(() -> CollectionLabel.validateShape("a/b")).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> CollectionLabel.validateShape(repeat("é", 128))).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> CollectionLabel.validateShape("")).isInstanceOf(IllegalArgumentException.class);
    }
}
