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

import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;

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

    /**
     * The {@code schema.function} names {@link #findFindings} examines: the same selection (schema, kind,
     * language; a trigger function only when attached to a relation), typed jOOQ over the catalogs, with no
     * analyser call. A routine the analyser finds nothing in is absent from the findings, so a "no finding for f"
     * test must also assert f is in this set, or a rename or a gap in the scan passes it. Keep the selection in
     * step with plpgsql-check-findings.sql.
     */
    static java.util.Set<String> scannedRoutines(Connection connection, Collection<String> schemas) {
        var p = DSL.table(DSL.name("pg_catalog", "pg_proc")).as("p");
        var n = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
        var l = DSL.table(DSL.name("pg_catalog", "pg_language")).as("l");
        var t = DSL.table(DSL.name("pg_catalog", "pg_type")).as("t");
        var tg = DSL.table(DSL.name("pg_catalog", "pg_trigger")).as("tg");
        Field<String> nspname = DSL.field(DSL.name("n", "nspname"), String.class);
        Field<String> proname = DSL.field(DSL.name("p", "proname"), String.class);
        Field<Long> prorettype = DSL.field(DSL.name("p", "prorettype"), Long.class);
        Field<Long> procOid = DSL.field(DSL.name("p", "oid"), Long.class);
        var triggerType = DSL.select(DSL.field(DSL.name("t", "oid"), Long.class)).from(t)
            .where(DSL.field(DSL.name("t", "typname"), String.class).eq("trigger")
                .and(DSL.field(DSL.name("t", "typnamespace"), Long.class).eq(
                    DSL.select(DSL.field(DSL.name("pn", "oid"), Long.class))
                        .from(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("pn"))
                        .where(DSL.field(DSL.name("pn", "nspname"), String.class).eq("pg_catalog")))));
        var attached = DSL.selectOne().from(tg)
            .where(DSL.field(DSL.name("tg", "tgfoid"), Long.class).eq(procOid));
        java.util.Set<String> out = new java.util.TreeSet<>();
        for (var r : DSL.using(connection, SQLDialect.POSTGRES)
                .selectDistinct(nspname, proname).from(p)
                .join(n).on(DSL.field(DSL.name("n", "oid"), Long.class).eq(DSL.field(DSL.name("p", "pronamespace"), Long.class)))
                .join(l).on(DSL.field(DSL.name("l", "oid"), Long.class).eq(DSL.field(DSL.name("p", "prolang"), Long.class)))
                .where(nspname.in(schemas)
                    .and(DSL.field(DSL.name("p", "prokind"), String.class).in("f", "p"))
                    .and(DSL.field(DSL.name("l", "lanname"), String.class).eq("plpgsql"))
                    .and(prorettype.ne(DSL.field(triggerType)).or(DSL.exists(attached))))
                .fetch()) {
            out.add(r.get(nspname) + "." + r.get(proname));
        }
        return out;
    }
}
