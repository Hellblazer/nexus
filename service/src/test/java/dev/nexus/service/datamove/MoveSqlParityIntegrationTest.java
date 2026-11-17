// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.VerificationParityFixture;
import dev.nexus.service.VerificationParityFixture.Row;
import dev.nexus.service.VerificationParityFixture.Verdict;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.EnumMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

/**
 * nexus-f3yqd.2.19 (RDR-246): the data-move worker's verdict on a verification text, row by row of
 * {@code data-move-verification-parity.tsv}, the file the registration gate's rule is held to in
 * {@code dev.nexus.service.db.DataMoveVerificationParityRulesTest}. What {@link MoveSql#countRows} does with a text is one of
 * three outcomes: it runs ({@code accept}), it throws {@link MoveSql.Refused} (the text fails {@link MoveSql#refusal}, or the
 * driver's parameter count, the one the server describes, which needs a real connection, is not the number of {@code $1}:
 * {@code refuse}), or the server rejects it while it is being described and a {@link SQLException} comes out
 * ({@code refuse-loud}). Each row names the one outcome it expects, so an accept row the server rejects (a typo in the file)
 * fails here, and so does a loud row that gains a worker rule of its own, until the row says {@code refuse}.
 */
class MoveSqlParityIntegrationTest extends DataMoveIntegrationBase {

    private enum Outcome { RAN, REFUSED, SERVER_REJECTED }

    @Test
    void theWorkerGivesEveryRowItsVerdict() throws Exception {
        List<Row> rows = VerificationParityFixture.rows();
        assertThat(rows).as("rows read (a file that read as empty would pass vacuously)").hasSizeGreaterThanOrEqualTo(40);

        List<String> wrong = new ArrayList<>();
        Map<Verdict, Integer> examined = new EnumMap<>(Verdict.class);
        try (var conn = CallConnection.open(adminConnection, WORKER_NAME + "-parity")) {
            for (Row row : rows) {
                Outcome outcome;
                String detail;
                try {
                    detail = "ran, " + MoveSql.countRows(conn.connection(), row.text(), "acme", 10) + " rows";
                    outcome = Outcome.RAN;
                } catch (MoveSql.Refused e) {
                    detail = "refused: " + e.getMessage();
                    outcome = Outcome.REFUSED;
                } catch (SQLException e) {
                    detail = "the server rejected it: sqlstate=" + e.getSQLState() + " " + e.getMessage();
                    outcome = Outcome.SERVER_REJECTED;
                }
                Outcome want = switch (row.verdict()) {
                    case ACCEPT -> Outcome.RAN;
                    case REFUSE -> Outcome.REFUSED;
                    case REFUSE_LOUD -> Outcome.SERVER_REJECTED;
                };
                if (outcome == want) {
                    examined.merge(row.verdict(), 1, Integer::sum);
                } else {
                    wrong.add(row + ": want " + want + ", the worker " + detail
                        + "  text=[" + row.text().replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t") + "]");
                }
            }
        }
        assertThat(wrong).as("rows where the worker's outcome is not the file's verdict").isEmpty();
        for (Verdict v : Verdict.values()) {
            assertThat(examined.getOrDefault(v, 0)).as("%s rows examined", v)
                .isEqualTo((int) rows.stream().filter(r -> r.verdict() == v).count()).isPositive();
        }
    }
}
