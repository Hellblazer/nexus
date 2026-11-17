// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

/**
 * nexus-f3yqd.2.19 (RDR-246): the rows of {@code data-move-verification-parity.tsv}, read by the two tests that must give one
 * verdict per row: the registration gate's rule ({@code DataMoveVerificationParityRulesTest}) and the data-move worker's
 * ({@code MoveSqlParityIntegrationTest}).
 *
 * <p>File format: one row per line, {@code verdict TAB name TAB text}; a blank line or one starting with {@code #} is skipped, and the one {@code @vtab TAB text} line defines {@code {VTAB}}.
 * {@code verdict} is a {@link Verdict}, written {@code accept}, {@code refuse} or {@code refuse-loud:<SQLSTATE>}. In {@code text}, {@code \\} is a backslash, {@code \n} a newline,
 * {@code \r} a carriage return, {@code \t} a tab, and {@code {VTAB}} expands to the file's {@code @vtab} line, a derived table every row can select from, so that an
 * {@code accept} row is real SQL the worker's test runs on a server. A row's text is the value the registry column stores (what the
 * gate reads after the registering INSERT's own literal is decoded), which is what the worker is given.
 */
public final class VerificationParityFixture {

    private VerificationParityFixture() { /* static */ }

    public static final String RESOURCE = "/data-move-verification-parity.tsv";

    /**
     * The verdict a row asks of both rules. {@code ACCEPT}: the gate lets the text register and the worker runs it.
     * {@code REFUSE}: the gate refuses it and the worker refuses it itself ({@code MoveSql.Refused}). {@code REFUSE_LOUD}: the gate
     * refuses it and the worker does not run it either, but not by its own rule: the driver's count passes or the text is not SQL,
     * and the server rejects the statement while it is being described (a SQLException). A row moves from REFUSE_LOUD to REFUSE
     * when the worker gets a rule of its own for it.
     */
    public enum Verdict { ACCEPT, REFUSE, REFUSE_LOUD }

    /** One row: {@code verdict} is what both rules must give. */
    public record Row(int line, Verdict verdict, String sqlState, String name, String text) {
        public boolean accept() {
            return verdict == Verdict.ACCEPT;
        }

        @Override
        public String toString() {
            return "line " + line + " " + name;
        }
    }

    /** The rows in the file today. A shorter file is a deletion, and the tests refuse it; a row added raises this. */
    public static final int MIN_ROWS = 64;

    /** The lexer-edge rows: each pins a place where the gate's lexer, the driver's and PostgreSQL's differ, and none may be dropped. */
    public static final List<String> REQUIRED_ROWS = List.of(
        "edge-nested-comment-opener-overlap",
        "edge-comment-opener-overlap-plain",
        "edge-dollar-directly-after-a-dollar",
        "edge-adjacent-dollar-quotes",
        "edge-e-string-at-the-start",
        "edge-comment-opener-overlap-hides-a-placeholder",
        "edge-non-ascii-dollar-quote-tag",
        "edge-line-comment-ended-by-a-carriage-return",
        "edge-currency-sign-before-a-dollar-tag",
        "edge-e-string-after-a-paren",
        "edge-non-ascii-tag-directly-before-a-placeholder",
        "edge-non-ascii-tag-multiplication-sign",
        "edge-supplementary-plane-dollar-tag",
        "edge-placeholder-followed-by-a-non-ascii-letter",
        "edge-placeholder-after-a-non-ascii-letter",
        "edge-non-ascii-text-away-from-any-dollar",
        "edge-e-string-after-a-closing-quote",
        "edge-e-string-after-a-dollar",
        "edge-e-string-after-a-backslash",
        "edge-e-string-after-a-non-ascii-letter",
        "zz-e-string-doubled-quote-then-escaped-quote");

    public static List<Row> rows() {
        String file;
        try (InputStream in = VerificationParityFixture.class.getResourceAsStream(RESOURCE)) {
            if (in == null) {
                throw new IllegalStateException("no test resource " + RESOURCE);
            }
            file = new String(in.readAllBytes(), StandardCharsets.UTF_8);
        } catch (IOException e) {
            throw new IllegalStateException(e);
        }
        List<Row> rows = new ArrayList<>();
        String[] lines = file.split("\n", -1);
        String vtab = null;
        for (String line : lines) {
            if (line.startsWith("@vtab\t")) {
                vtab = line.substring("@vtab\t".length());
            }
        }
        if (vtab == null) {
            throw new IllegalStateException(RESOURCE + " has no @vtab line");
        }
        for (int i = 0; i < lines.length; i++) {
            String line = lines[i];
            if (line.isBlank() || line.startsWith("#") || line.startsWith("@vtab\t")) {
                continue;
            }
            String[] cells = line.split("\t", 3);
            if (cells.length != 3) {
                throw new IllegalStateException(RESOURCE + " line " + (i + 1) + ": want verdict TAB name TAB text");
            }
            String[] verdictAndState = cells[0].split(":", 2);
            Verdict verdict = switch (verdictAndState[0]) {
                case "accept" -> Verdict.ACCEPT;
                case "refuse" -> Verdict.REFUSE;
                case "refuse-loud" -> Verdict.REFUSE_LOUD;
                default -> throw new IllegalStateException(RESOURCE + " line " + (i + 1) + ": verdict is accept, refuse or refuse-loud:<SQLSTATE>, not " + cells[0]);
            };
            String sqlState = verdictAndState.length == 2 ? verdictAndState[1] : null;
            if ((verdict == Verdict.REFUSE_LOUD) != (sqlState != null) || sqlState != null && !sqlState.matches("[0-9A-Z]{5}")) {
                throw new IllegalStateException(RESOURCE + " line " + (i + 1) + ": refuse-loud, and only refuse-loud, names its SQLSTATE: " + cells[0]);
            }
            rows.add(new Row(i + 1, verdict, sqlState, cells[1], decode(cells[2]).replace("{VTAB}", vtab)));
        }
        if (rows.size() < MIN_ROWS) {
            throw new IllegalStateException(RESOURCE + " has " + rows.size() + " rows, fewer than the " + MIN_ROWS + " it holds today");
        }
        List<String> missing = new ArrayList<>(REQUIRED_ROWS);
        rows.forEach(r -> missing.remove(r.name()));
        if (!missing.isEmpty()) {
            throw new IllegalStateException(RESOURCE + " lost required rows " + missing);
        }
        return rows;
    }

    private static String decode(String cell) {
        StringBuilder out = new StringBuilder();
        for (int i = 0; i < cell.length(); i++) {
            char c = cell.charAt(i);
            if (c != '\\') {
                out.append(c);
                continue;
            }
            if (++i >= cell.length()) {
                throw new IllegalStateException("a trailing backslash in " + cell);
            }
            switch (cell.charAt(i)) {
                case '\\' -> out.append('\\');
                case 'n' -> out.append('\n');
                case 't' -> out.append('\t');
                case 'r' -> out.append('\r');
                default -> throw new IllegalStateException("unknown escape \\" + cell.charAt(i) + " in " + cell);
            }
        }
        return out.toString();
    }
}
