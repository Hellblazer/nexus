// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.VerificationParityFixture;
import dev.nexus.service.VerificationParityFixture.Row;
import java.util.List;
import org.junit.jupiter.api.Test;

/**
 * nexus-f3yqd.2.19 (RDR-246): the registration gate's verdict on a verification text, row by row of
 * {@code data-move-verification-parity.tsv}. The data-move worker's side of the same file is
 * {@code dev.nexus.service.datamove.MoveSqlParityIntegrationTest}; a row's verdict is one and the same on both, so a change to
 * either rule that the other does not follow fails here or there.
 */
class DataMoveVerificationParityRulesTest {

    @Test
    void theGateGivesEveryRowItsVerdict() {
        List<Row> rows = VerificationParityFixture.rows();
        assertThat(rows).as("rows read (a file that read as empty would pass vacuously)").hasSizeGreaterThanOrEqualTo(40);
        assertThat(rows).as("the file holds both verdicts").anyMatch(Row::accept).anyMatch(r -> !r.accept());
        assertThat(rows).extracting(Row::name).as("names are unique").doesNotHaveDuplicates();

        int accepted = 0;
        int refused = 0;
        for (Row row : rows) {
            boolean refuses = DataMoveChangesetRules.refusesVerification(row.text());
            assertThat(refuses).as("%s: the gate %s %s", row, refuses ? "refuses" : "accepts", show(row.text()))
                .isEqualTo(!row.accept());
            if (refuses) refused++; else accepted++;
        }
        assertThat(accepted).as("accept rows examined").isEqualTo((int) rows.stream().filter(Row::accept).count());
        assertThat(refused).as("refuse rows examined").isEqualTo((int) rows.stream().filter(r -> !r.accept()).count());
    }

    private static String show(String text) {
        return "[" + text.replace("\n", "\\n").replace("\t", "\\t") + "]";
    }
}
