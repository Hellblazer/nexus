// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The findings query and the Finding record are taken from
//   /Users/hal.hildebrand/git/liquibase_validation/src/main/java/io/github/eyupmiduck/changelogvalidator/PlpgsqlCheck.java
// which is licensed under the MIT License:
//
//   MIT License
//
//   Copyright (c) 2026 eyupmiduck
//
//   Permission is hereby granted, free of charge, to any person obtaining a copy
//   of this software and associated documentation files (the "Software"), to deal
//   in the Software without restriction, including without limitation the rights
//   to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
//   copies of the Software, and to permit persons to whom the Software is
//   furnished to do so, subject to the following conditions:
//
//   The above copyright notice and this permission notice shall be included in all
//   copies or substantial portions of the Software.
//
//   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
//   IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
//   FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
//   AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
//   LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
//   OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
//   SOFTWARE.
//
// Changes here: the YAML allow-list loader and the allow-list matching are not copied; the query text lives in
// the classpath resource plpgsql-check-findings.sql instead of a Java text block; the query also passes each
// trigger's REFERENCING transition-table names (oldtable and newtable) to the analyser and sets fatal_errors to
// false so the analyser keeps going after its first error.
package dev.nexus.service;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.sql.Array;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.Collection;
import java.util.List;

/**
 * Runs the {@code plpgsql_check} static analyser over the PL/pgSQL routines in a set of schemas. The database
 * must have the {@code plpgsql_check} extension installed. Findings come from
 * {@code plpgsql_check_function_tb(..., all_warnings => true)}. A trigger function is analysed once per relation
 * it is attached to (the analyser needs the trigger relation to resolve NEW and OLD); an unattached trigger
 * function is skipped, since it cannot be analysed without one.
 */
final class PlpgsqlCheck {

    private PlpgsqlCheck() {}

    /** One warning or error reported by {@code plpgsql_check_function_tb}. */
    record Finding(String schema, String function, int line, String level, String statement, String message) {
        String describe() {
            return schema + "." + function + ":" + line + ": " + level + ": " + message;
        }
    }

    /** Every finding for the functions and procedures in {@code schemas}, all warning categories on. */
    static List<Finding> findFindings(Connection connection, Collection<String> schemas) throws SQLException, IOException {
        String query;
        try (InputStream in = PlpgsqlCheck.class.getResourceAsStream("/plpgsql-check-findings.sql")) {
            query = new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
        List<Finding> findings = new ArrayList<>();
        Array schemaArray = connection.createArrayOf("text", schemas.toArray(String[]::new));
        try (PreparedStatement statement = connection.prepareStatement(query)) {
            statement.setArray(1, schemaArray);
            try (ResultSet rs = statement.executeQuery()) {
                while (rs.next()) {
                    findings.add(new Finding(rs.getString(1), rs.getString(2), rs.getInt(3), rs.getString(4),
                        rs.getString(5), rs.getString(6)));
                }
            }
        } finally {
            schemaArray.free();
        }
        return findings;
    }
}
