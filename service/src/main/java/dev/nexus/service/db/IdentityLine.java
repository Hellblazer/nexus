// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.Condition;

import java.util.LinkedHashSet;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static dev.nexus.service.jooq.nexus.Tables.MEMORY;
import static org.jooq.impl.DSL.not;

/**
 * The {@code identity: <id>} line of a board entry (RDR-253): the line that makes a T2 entry in project
 * {@code threads} a fenced one. One definition, as a Java pattern for the fence that reads an entry it is about
 * to change and as a SQL condition for the sweeps that must leave such entries alone, so the two cannot drift:
 * a line is {@code identity:} at the start of a line, optional blanks, an {@code i<digits>} id, optional trailing
 * blanks, the end of the line.
 */
public final class IdentityLine {

    /** The project whose entries are fenced when they carry the line. */
    public static final String PROJECT = "threads";

    private static final Pattern LINE =
        Pattern.compile("(?:^|\\n)identity:[ \\t]*(i[0-9]+)[ \\t\\r]*(?=\\n|$)");
    /** The same line as a PostgreSQL regular expression, for {@code content ~ ...}. */
    private static final String SQL_LINE = "(^|\n)identity:[ \t]*i[0-9]+[ \t\r]*(\n|$)";

    private IdentityLine() {
    }

    /** The distinct identity ids named by the {@code identity:} lines of {@code content}, in order; empty for none. */
    public static Set<String> ids(String content) {
        Set<String> out = new LinkedHashSet<>();
        if (content != null) {
            Matcher m = LINE.matcher(content);
            while (m.find()) {
                out.add(m.group(1));
            }
        }
        return out;
    }

    /** True for a row of the threads project that carries an identity line (a fenced board entry). */
    public static Condition fenced() {
        return MEMORY.PROJECT.eq(PROJECT).and(MEMORY.CONTENT.likeRegex(SQL_LINE));
    }

    /** The negation of {@link #fenced()}: what {@code expire} and {@code reap} may touch. */
    public static Condition notFenced() {
        return not(fenced());
    }
}
