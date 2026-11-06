// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import dev.nexus.service.vectors.PgVectorRepository.QuarantineRestoreOutcome;
import dev.nexus.service.vectors.PgVectorRepository.QuarantineRestoreOutcome.Row;
import dev.nexus.service.vectors.PgVectorRepository.QuarantineRestoreOutcome.Source;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-k9kr9: the keyed quarantine-restore call takes at most {@link VectorHandler#MAX_KEYED_QUARANTINE_RESTORE_CHASHES}
 * chashes so its full 200 body fits the ledger's 64 KB stored cap ({@link KeyedSweep#STORED_RESPONSE_CAP_BYTES}),
 * measured the way the ledger measures it ({@link KeyedSweep#pgJsonbBytes}, Postgres's jsonb text form). This test
 * RECOMPUTES the number from the real body builder with every field at its longest, so the constant is a measurement
 * the build can check, not a guess: it fails when a field is added to the row, when the cap moves, or when K is not
 * the largest count that fits.
 */
class KeyedRestoreBodySizeTest {

    private static final int CAP = KeyedSweep.STORED_RESPONSE_CAP_BYTES;
    private static final int ENVELOPE_SIBLINGS = 16;
    private static final int NAME_CHARS = 128;

    /** Every field at the longest value it can take in a keyed body (a keyed call is never a dry run). */
    private static Row worstRow(int n) {
        String chash = String.format("%064x", n);
        return new Row(chash, "dim_conflict", Boolean.FALSE, "2026-11-05T12:34:56.123456789Z", "no_live_owner", false,
            "9".repeat(64), "t".repeat(200), Integer.MAX_VALUE, "c".repeat(200), "owner_link_dropped",
            Integer.MAX_VALUE, Integer.MAX_VALUE);
    }

    /**
     * A worst-case outcome: {@code rows} worst rows, a long origin, {@code siblingCount} long sibling names, one audit
     * id per sibling (capped at 16: a call writes one audit row per sibling that moved something, and the K chashes
     * of a call rarely reach that many), the audit source object, a cursor.
     */
    private static QuarantineRestoreOutcome outcome(int rows, int siblingCount) {
        List<Row> list = new ArrayList<>();
        for (int i = 0; i < rows; i++) list.add(worstRow(i));
        List<String> siblings = new ArrayList<>();
        List<Long> auditIds = new ArrayList<>();
        for (int i = 0; i < siblingCount; i++) {
            siblings.add(String.format("quarantine-%03d-", i).concat("s".repeat(NAME_CHARS - 15)));
            if (i < ENVELOPE_SIBLINGS) auditIds.add(Long.MAX_VALUE - i);
        }
        return new QuarantineRestoreOutcome(list, auditIds.isEmpty() ? null : auditIds.get(0), false,
            new Source(Long.MAX_VALUE, "gc_quarantine_orphans_bounded", Integer.MAX_VALUE, Integer.MAX_VALUE,
                Integer.MAX_VALUE, Integer.MAX_VALUE),
            "f".repeat(64), auditIds, siblings);
    }

    private static Map<String, Object> body(int rows) {
        return body(rows, ENVELOPE_SIBLINGS);
    }

    private static Map<String, Object> body(int rows, int siblingCount) {
        return VectorHandler.quarantineRestoreBody("o".repeat(NAME_CHARS), false, true, outcome(rows, siblingCount), true);
    }

    @Test
    void kIsTheLargestCountWhoseWorstCaseBodyFitsTheStoredCap() {
        int envelope = KeyedSweep.pgJsonbBytes(body(0));
        int worstRow = KeyedSweep.pgJsonbBytes(body(2)) - KeyedSweep.pgJsonbBytes(body(1));   // incl. the ", " between rows

        assertThat(envelope).as("the worst-case envelope fits the reserve").isLessThanOrEqualTo(
            VectorHandler.KEYED_RESTORE_ENVELOPE_RESERVE_BYTES);
        assertThat(worstRow).as("the named worst row is the measured one").isEqualTo(
            VectorHandler.KEYED_RESTORE_WORST_ROW_BYTES);
        int k = VectorHandler.MAX_KEYED_QUARANTINE_RESTORE_CHASHES;
        assertThat(k).as("K is floor((cap - envelope reserve) / worst row)")
            .isEqualTo((CAP - VectorHandler.KEYED_RESTORE_ENVELOPE_RESERVE_BYTES) / worstRow);
        assertThat(KeyedSweep.pgJsonbBytes(body(k))).as("K worst-case rows plus the worst envelope fit")
            .isLessThanOrEqualTo(CAP);
        assertThat(body(k).get("quarantine_collections")).as("the envelope assumption: %d siblings of %d characters",
            ENVELOPE_SIBLINGS, NAME_CHARS).asList().hasSize(ENVELOPE_SIBLINGS);
    }

    /**
     * The sibling count and the name length are ASSUMPTIONS of the measurement above, not engine bounds: a sibling is
     * any registered quarantine collection holding a chunk tagged with the origin, and nothing limits how many an
     * origin has. So the engine cannot assert them; the keyed path enforces the cap on the real body instead
     * ({@link VectorHandler#keyedQuarantineRestoreBody}). This pins that enforcement at its edge: beside K worst rows,
     * the largest sibling count that fits is returned whole, one more is refused with the measured size, and the
     * refusal exists because the ledger's own cut would have rewritten a body that has no sample.
     */
    @Test
    void aKeyedBodyOverTheCapIsRefusedByTheBackstop_notCutByTheLedger() throws Exception {
        int k = VectorHandler.MAX_KEYED_QUARANTINE_RESTORE_CHASHES;
        int fits = ENVELOPE_SIBLINGS;
        while (KeyedSweep.pgJsonbBytes(body(k, fits + 1)) <= CAP) fits++;
        assertThat(fits).as("the reserve holds at least the assumed %d siblings beside K worst rows", ENVELOPE_SIBLINGS)
            .isGreaterThanOrEqualTo(ENVELOPE_SIBLINGS);
        final int edge = fits;
        String origin = "o".repeat(NAME_CHARS);

        var atEdge = VectorHandler.keyedQuarantineRestoreBody(origin, true, outcome(k, edge));
        assertThat(KeyedSweep.pgJsonbBytes(atEdge)).isLessThanOrEqualTo(CAP);
        assertThat(atEdge).as("returned whole, rows included").containsKey("rows");

        var over = outcome(k, edge + 1);
        assertThatThrownBy(() -> VectorHandler.keyedQuarantineRestoreBody(origin, true, over))
            .isInstanceOfSatisfying(VectorHandler.KeyedRestoreBodyOverCapException.class, e -> {
                assertThat(e.capBytes()).isEqualTo(CAP);
                assertThat(e.bodyBytes()).isGreaterThan(CAP).isEqualTo(KeyedSweep.pgJsonbBytes(body(k, edge + 1)));
                assertThat(e.getMessage()).contains(VectorHandler.KEYED_RESTORE_BODY_OVER_CAP)
                    .contains(String.valueOf(e.bodyBytes())).contains(String.valueOf(CAP));
            });

        // What the backstop prevents: the ledger's cut adds a sample to a body that has none, and stores it over the cap.
        String live = VectorHandler.MAPPER.writeValueAsString(body(k, edge + 1));
        String stored = new KeyedSweep(null).storedResponse(body(k, edge + 1), live);
        assertThat(stored).as("the ledger's cut would have rewritten the body").contains("sample_truncated");
        assertThat(KeyedSweep.pgJsonbBytes(VectorHandler.MAPPER.readValue(stored, Map.class)))
            .as("and still stored it over the cap").isGreaterThan(CAP);
    }

    @Test
    void theKeyedBodyIsTheUnkeyedBodyExceptForTheClippedTitles() {
        var out = new QuarantineRestoreOutcome(List.of(worstRow(1)), 7L, false, null, null, List.of(7L),
            List.of("quarantine-x"));
        Map<String, Object> keyed = VectorHandler.quarantineRestoreBody("x", false, true, out, true);
        Map<String, Object> unkeyed = VectorHandler.quarantineRestoreBody("x", false, true, out, false);

        @SuppressWarnings("unchecked")
        var kRow = ((List<Map<String, Object>>) keyed.get("rows")).get(0);
        @SuppressWarnings("unchecked")
        var uRow = ((List<Map<String, Object>>) unkeyed.get("rows")).get(0);
        assertThat(uRow.get("owner_title")).as("the unkeyed body is not clipped").isEqualTo("t".repeat(200));
        assertThat(uRow.get("chunk_title")).isEqualTo("c".repeat(200));
        assertThat(kRow.get("owner_title")).isEqualTo("t".repeat(VectorHandler.KEYED_RESTORE_TITLE_MAX_JSON_BYTES));
        assertThat(kRow.get("chunk_title")).isEqualTo("c".repeat(VectorHandler.KEYED_RESTORE_TITLE_MAX_JSON_BYTES));
        var kCopy = new java.util.LinkedHashMap<>(kRow);
        var uCopy = new java.util.LinkedHashMap<>(uRow);
        for (String f : List.of("owner_title", "chunk_title")) { kCopy.remove(f); uCopy.remove(f); }
        assertThat(kCopy).as("every other field is identical").isEqualTo(uCopy);
        assertThat(keyed.keySet()).as("same keys, same order").containsExactlyElementsOf(unkeyed.keySet());
    }

    @Test
    void clipJsonBytesCutsAtTheEscapedByteCountAndNeverSplitsACharacter() {
        assertThat(VectorHandler.clipJsonBytes(null, 10)).isNull();
        assertThat(VectorHandler.clipJsonBytes("short", 10)).isEqualTo("short");
        assertThat(VectorHandler.clipJsonBytes("abcdef", 4)).isEqualTo("abcd");
        // A quote escapes to 2 bytes, a control character to 6.
        assertThat(VectorHandler.clipJsonBytes("\"\"\"", 5)).isEqualTo("\"\"");
        assertThat(VectorHandler.clipJsonBytes("\u0001\u0001", 11)).isEqualTo("\u0001");
        // Multi-byte: e-acute is 2 bytes, a CJK character 3, an emoji (a surrogate pair) 4.
        assertThat(VectorHandler.clipJsonBytes("ééé", 5)).isEqualTo("éé");
        assertThat(VectorHandler.clipJsonBytes("中中", 5)).isEqualTo("中");
        String emoji = new String(Character.toChars(0x1F600));
        assertThat(VectorHandler.clipJsonBytes(emoji + emoji, 7)).as("a pair is kept whole or dropped whole")
            .isEqualTo(emoji);
        // Whatever the content, the clipped text renders within the bound as Postgres renders it (quotes excluded).
        for (String s : List.of("x".repeat(500), "\u0001".repeat(500), "\"".repeat(500), "中".repeat(500),
                                emoji.repeat(300))) {
            String clipped = VectorHandler.clipJsonBytes(s, VectorHandler.KEYED_RESTORE_TITLE_MAX_JSON_BYTES);
            assertThat(KeyedSweep.pgJsonbBytes(clipped) - 2).isLessThanOrEqualTo(
                VectorHandler.KEYED_RESTORE_TITLE_MAX_JSON_BYTES);
        }
    }
}
