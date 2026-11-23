// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.sql.Types;
import java.util.Optional;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * The one place a move's registry text reaches the server as SQL (RDR-246, The registry): the verification query and
 * {@code on_complete}. Both are values of {@code nexus.data_moves} written by the changeset that registered the move; they
 * are not assembled here, and nothing is concatenated into them. The statement gate sanctions exactly the two calls below
 * (Sam, 2026-11-17).
 *
 * <p><b>The bind parameter.</b> The registry's verification is written with one bind parameter, {@code $1}, the tenant:
 * {@code tenant_id = $1 OR $1 IS NULL}. The JDBC driver binds {@code ?}, and a literal {@code $1} would reach the server
 * with no parameter supplied, so {@link #bindForm} renders each standalone {@code $1} as a typed {@code CAST(? AS text)}
 * and {@link #countRows} binds the one tenant to every occurrence. The text is not parsed for quotes, so the rule is a
 * refusal and never a repair ({@link #refusal}): a verification with any {@code ?}, or with a {@code $} followed by a digit
 * that is not a standalone {@code $1} (not part of a longer name, not {@code $2}), is not run. What the regex cannot see, a
 * {@code $1} inside a string literal or a comment, the driver does: {@link #countRows} compares the statement's own parameter
 * count with the number of {@code $1} and refuses a mismatch.
 *
 * <p><b>The query returns one row per old-shape row.</b> {@link #countRows} counts the rows it returns, up to a cap, so a
 * registrant writes {@code SELECT 1 FROM ... WHERE <old shape>}, never a wide select.
 */
final class MoveSql {

    private MoveSql() { /* static */ }

    /**
     * A verification text the worker will not run, and why. Unchecked: the finisher reports it as a {@code VERIFICATION_REFUSED}
     * failure, a bind-count mismatch found while preparing included.
     */
    static final class Refused extends RuntimeException {
        private static final long serialVersionUID = 1L;

        Refused(String message) {
            super(message);
        }
    }

    /**
     * A {@code $1} standing alone: no word character, {@code $} or non-ASCII character before or after it. The boundary of the registration gate
     * ({@code DataMoveChangesetRules.STANDALONE_FIRST_PLACEHOLDER}), so the worker and the gate agree.
     */
    private static final Pattern STANDALONE_FIRST_PLACEHOLDER = Pattern.compile("(?<![\\w$\\P{ASCII}])\\$1(?![\\w$\\P{ASCII}])");

    /**
     * A {@code $} directly next to a character outside ASCII. PostgreSQL reads every such character as part of an identifier, the
     * driver only the ones Java calls identifier parts, so they disagree on where a dollar-quote tag ends ({@code $×$ $1 $×$}),
     * and the {@code $1} rewrite below would cut a tag in two. The gate's {@code DataMoveChangesetRules.NON_ASCII_DOLLAR}.
     */
    private static final Pattern NON_ASCII_DOLLAR = Pattern.compile("\\P{ASCII}\\$|\\$\\P{ASCII}");

    /** A block-comment opener whose star is also the closer's: the one place the driver's lexer and PostgreSQL's read a comment differently. */
    private static final String OPENER_OVERLAP = "/*/";

    private static final Pattern DOLLAR_DIGIT = Pattern.compile("\\$\\d");

    /**
     * Why the verification text is not run, or empty when it may be: it is blank, it contains {@code /*}{@code /}, or it has a {@code $} next to a non-ASCII
     * character (the registration gate refuses all three too, nexus-f3yqd.2.19), it contains a {@code ?} (the driver would read it as
     * a parameter, and cannot be told otherwise inside a literal), or a {@code $} followed by a digit is left once the
     * standalone {@code $1}s are removed ({@code $2}, {@code $10}, {@code my$1}, {@code $1x}, {@code $$1 x$$}).
     */
    static Optional<String> refusal(String verification) {
        if (verification.isBlank()) {
            return Optional.of("the verification text is empty. It is a SELECT of the rows still in the old shape; the driver "
                + "cannot run an empty statement");
        }
        if (verification.contains(OPENER_OVERLAP)) {
            return Optional.of("the verification text contains '/*/'. The driver ends a block comment at that '/', PostgreSQL does "
                + "not (it nests), so the two disagree about which text is a comment and the bind parameters cannot be counted. "
                + "Write the comment with a space after '/*'");
        }
        if (verification.indexOf('?') >= 0) {
            return Optional.of("the verification text contains a '?'. The driver reads a '?' as a bind parameter, so the text "
                + "must not carry one (write the tenant as $1 and spell a jsonb or pattern '?' another way)");
        }
        if (NON_ASCII_DOLLAR.matcher(verification).find()) {
            return Optional.of("the verification text has a '$' directly next to a character outside ASCII. PostgreSQL and the "
                + "driver disagree on where a dollar-quote tag or a parameter ends there, so the bind parameters cannot be counted. "
                + "Keep the characters beside a '$' ASCII, or move the non-ASCII text into a string literal away from any '$'");
        }
        Matcher left = DOLLAR_DIGIT.matcher(STANDALONE_FIRST_PLACEHOLDER.matcher(verification).replaceAll(""));
        if (left.find()) {
            return Optional.of("the verification text contains '" + left.group() + "' that is not a standalone $1. The only "
                + "parameter is $1 (the tenant); $2, $10, a $1 inside a longer name and a dollar-quote tag that starts with "
                + "a digit are refused");
        }
        return Optional.empty();
    }

    /** The verification text as the driver binds it: every standalone {@code $1} a {@code CAST(? AS text)}; {@link #refusal} came first. */
    static String bindForm(String verification) {
        return STANDALONE_FIRST_PLACEHOLDER.matcher(verification).replaceAll("CAST(? AS text)");
    }

    /** How many standalone {@code $1} the verification text carries. */
    static int placeholders(String verification) {
        return (int) STANDALONE_FIRST_PLACEHOLDER.matcher(verification).results().count();
    }

    /**
     * Runs the verification query and counts the rows it returns, reading at most {@code cap + 1} of them (a count above
     * {@code cap} is reported as "more than cap", so a table of millions of old-shape rows is not read to the end). Each
     * row is read in full, so the query selects {@code 1}.
     *
     * @param tenant the tenant bound to every {@code $1}; null for the table-wide run
     * @throws SQLException what the query raised
     * @throws Refused      the text fails {@link #refusal}, or the driver sees a different number of parameters than the
     *                      text has {@code $1} (one inside a literal or a comment)
     */
    static long countRows(Connection connection, String verification, String tenant, int cap) throws SQLException {
        Optional<String> refused = refusal(verification);
        if (refused.isPresent()) {
            throw new Refused(refused.get());
        }
        String sql = bindForm(verification);
        int expected = placeholders(verification);
        try (PreparedStatement ps = connection.prepareStatement(sql)) {
            int actual = ps.getParameterMetaData().getParameterCount();
            if (actual != expected) {
                throw new Refused("the verification text has " + expected + " $1 but the driver sees " + actual
                    + " bind parameters: a $1 inside a string literal, a comment or a quoted name is not a parameter");
            }
            for (int i = 1; i <= actual; i++) {
                if (tenant == null) {
                    ps.setNull(i, Types.VARCHAR);
                } else {
                    ps.setString(i, tenant);
                }
            }
            ps.setMaxRows(cap + 1);
            long rows = 0;
            try (ResultSet rs = ps.executeQuery()) {
                while (rs.next()) {
                    rows++;
                }
            }
            return rows;
        }
    }

    /** Runs {@code on_complete} on {@code connection}, in whatever transaction the caller has open. */
    static void runOnComplete(Connection connection, String onComplete) throws SQLException {
        runOnComplete(connection, onComplete, st -> { });
    }

    /** As above; {@code inFlight} gets the statement before it runs, so another thread can cancel it (the engine is stopping). */
    static void runOnComplete(Connection connection, String onComplete, java.util.function.Consumer<Statement> inFlight) throws SQLException {
        String sql = onComplete;
        try (Statement st = connection.createStatement()) {
            inFlight.accept(st);
            st.execute(sql);
        }
    }
}
