// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.jooq.exception.DataAccessException;
import org.junit.jupiter.api.Test;

import java.sql.SQLException;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-4lnn1.1.10: a canary failure in the cloud logged jOOQ's message, which opens with the whole SQL text, so the
 * 200-character cap ended inside the select list and the cause and SQLState were never logged.
 */
class PciQueryRingSanitizeTest {

    private static final String SELECT = "select 'plain_search_1024'.'id', 'plain_search_1024'.'content', "
        + "'plain_search_1024'.'collection', 'plain_search_1024'.'distance', 'plain_search_1024'.'metadata', "
        + "'plain_search_1024'.'retention' from nexus.plain_search_1024(cast(? as nexus.vector), ?, ?, ?, ?, ?, ?, ?)";

    @Test
    void aJooqFailureLogsTheSqlStateAndTheCauseAndNotTheSql() {
        SQLException cause = new SQLException("ERROR: canceling statement due to statement timeout", "57014");
        DataAccessException e = new DataAccessException("SQL [" + SELECT + "]; " + cause.getMessage(), cause);

        String s = PciQueryRing.sanitize(e);

        assertThat(s).startsWith("DataAccessException: ")
            .contains("sqlstate=57014")
            .contains("canceling statement due to statement timeout")
            .doesNotContain("'plain_search_1024'.'id'");
    }

    @Test
    void theCauseIsFoundDeeperInTheChain() {
        SQLException cause = new SQLException("ERROR: could not open relation", "XX000");
        RuntimeException e = new RuntimeException("wrapper", new DataAccessException("SQL [" + SELECT + "]; x", cause));

        assertThat(PciQueryRing.sanitize(e)).contains("sqlstate=XX000").contains("could not open relation");
    }

    @Test
    void aVectorQuotedInTheCauseIsMasked() {
        SQLException cause = new SQLException("ERROR: invalid input [0.0123, -0.4567, 0.891011, 0.121314]", "22P02");

        String s = PciQueryRing.sanitize(new DataAccessException("SQL [" + SELECT + "]; boom", cause));

        assertThat(s).contains("sqlstate=22P02").contains("[vector]").doesNotContain("0.0123");
    }

    @Test
    void aJooqMessageWithNoSqlExceptionDropsTheSqlText() {
        String s = PciQueryRing.sanitize(new DataAccessException("SQL [" + SELECT + "]; Connection reset"));

        assertThat(s).isEqualTo("DataAccessException: Connection reset");
    }

    @Test
    void anOrdinaryExceptionIsBoundedAsBefore() {
        String s = PciQueryRing.sanitize(new IllegalStateException("x".repeat(500)));

        assertThat(s).isEqualTo("IllegalStateException: " + "x".repeat(200));
    }
}
