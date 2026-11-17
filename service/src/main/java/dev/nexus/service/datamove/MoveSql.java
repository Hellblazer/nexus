// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.sql.Types;
import java.util.regex.Pattern;

/**
 * The one place a move's registry text reaches the server as SQL (RDR-246, The registry): the verification query and
 * {@code on_complete}. Both are values of {@code nexus.data_moves} written by the changeset that registered the move; they
 * are not assembled here, and nothing is concatenated into them. The statement gate sanctions exactly the two calls below.
 *
 * <p><b>The bind parameter.</b> The registry's verification is written with one bind parameter, {@code $1}, the tenant:
 * {@code tenant_id = $1 OR $1 IS NULL}. The JDBC driver binds {@code ?}, and a literal {@code $1} would reach the server
 * with no parameter supplied, so {@link #bindForm} renders each {@code $1} as {@code ?} (a {@code ?} the text already has,
 * a jsonb operator say, is doubled first so the driver reads it as a literal) and {@link #countRows} binds the one tenant
 * to every occurrence. A {@code $1} inside a string literal is rewritten too; a verification must not carry one.
 */
final class MoveSql {

    private MoveSql() { /* static */ }

    private static final Pattern BIND_PARAMETER = Pattern.compile("\\$1(?![0-9A-Za-z_$])");

    /**
     * The verification text as the driver binds it: a literal {@code ?} doubled, every {@code $1} a {@code CAST(? AS text)}.
     * The cast types the parameter wherever it stands ({@code $1 IS NULL} alone leaves it untyped), and a tenant id is text.
     */
    static String bindForm(String verification) {
        return BIND_PARAMETER.matcher(verification.replace("?", "??")).replaceAll("CAST(? AS text)");
    }

    /** How many {@code $1} the verification text carries. */
    static int placeholders(String verification) {
        return (int) BIND_PARAMETER.matcher(verification).results().count();
    }

    /**
     * Runs the verification query and counts the rows it returns, reading at most {@code cap + 1} of them (a count above
     * {@code cap} is reported as "more than cap", so a table of millions of old-shape rows is not read to the end).
     *
     * @param tenant the tenant bound to every {@code $1}; null for the table-wide run
     * @throws SQLException what the query raised
     */
    static long countRows(Connection connection, String verification, String tenant, int cap) throws SQLException {
        String sql = bindForm(verification);
        int binds = placeholders(verification);
        try (PreparedStatement ps = connection.prepareStatement(sql)) {
            for (int i = 1; i <= binds; i++) {
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
        String sql = onComplete;
        try (Statement st = connection.createStatement()) {
            st.execute(sql);
        }
    }
}
