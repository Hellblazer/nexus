// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.w3c.dom.Element;
import org.w3c.dom.Node;
import org.w3c.dom.NodeList;
import org.xml.sax.InputSource;

import javax.xml.XMLConstants;
import javax.xml.parsers.DocumentBuilderFactory;
import java.io.IOException;
import java.io.StringReader;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * The rules of {@link DataMoveChangesetGateTest} (RDR-246, Technical Design, "The check that stops a new boot move"), kept
 * apart from the test so the test can drive them over fixture changelogs as well as the shipped ones. The rules read
 * changelog TEXT: each changeset's {@code <sql>} bodies are masked (comments, string literals and dollar-quoted bodies
 * become placeholders), split at semicolons, lowercased and matched by statement shape. A rule is therefore blind to what
 * the mask hides (a function body, a DO block, the SQL held in a string) and to anything outside {@code <sql>}
 * ({@code sqlFile}, a custom change). The test class states those limits next to the RDR's own.
 *
 * <p>Every finding has a kind, named {@code r1.*} (row DML), {@code r2.*} (boot scans on a table that grows),
 * {@code r3.*} (a destructive statement on a registered move's table) or {@code exemption}.
 */
final class DataMoveChangesetRules {

    private DataMoveChangesetRules() {}

    // ---- the tables rule 2 names -------------------------------------------------------------------------------------

    /** Tables that grow with user data besides {@code nexus.chunks} and its per-model tables ({@code chunks_*}). */
    static final Set<String> GROWING_TABLES =
        Set.of("catalog_document_chunks", "catalog_documents", "catalog_links", "topic_assignments");

    /** True for {@code nexus.chunks}, a per-model chunk table or leaf ({@code chunks_*}), or a table in {@link #GROWING_TABLES}. */
    static boolean growsWithUserData(String bareTable) {
        return bareTable.equals("chunks") || bareTable.startsWith("chunks_") || GROWING_TABLES.contains(bareTable);
    }

    // ---- model -------------------------------------------------------------------------------------------------------

    /** One statement: lowercased text with literals replaced by {@code '#n#'}, and the literals of its whole body. */
    record Stmt(String text, List<String> literals) {}

    /** A child of a changeset the rules cannot read: a native Liquibase change type, with its {@code tableName} when it has one. */
    record Opaque(String element, String table) {}

    /**
     * A changeset read from a changelog file: its executable statements, comment and CONTINUE-precondition checks, the native
     * change elements it holds ({@link Opaque}) and how many {@code sqlFile} children it has (counted, never read).
     */
    record Changeset(String file, String id, String author, List<Stmt> statements, String comment, List<Stmt> continueChecks,
                     List<Opaque> opaque, int sqlFiles) {
        /** The frozen list's key: the file, the changeset id and its author (Liquibase's own identity of a changeset). */
        String key() {
            return file + "::" + id + "::" + author;
        }
    }

    /**
     * One finding. {@code table} is the bare table it concerns (null when the statement has no single target) and {@code text}
     * the statement it came from, which the exemption line uses to decide whether it may lift the finding.
     */
    record Finding(String kind, String detail, String table, String text) {
        Finding(String kind, String detail) {
            this(kind, detail, null, "");
        }

        @Override
        public String toString() {
            return kind + " (" + detail + ")";
        }
    }

    /**
     * What the rules say of one changeset: its findings, whether it carries a valid exemption line, and the findings that line
     * does not lift (the ones on any table but the exempted one).
     */
    record Entry(Changeset changeset, List<Finding> findings, boolean exempt, List<Finding> unlifted) {
        Set<String> kinds() {
            Set<String> kinds = new LinkedHashSet<>();
            for (Finding f : findings) kinds.add(f.kind());
            return kinds;
        }

        /** The kinds of the findings no exemption lifts: what makes the changeset an offender. */
        Set<String> unliftedKinds() {
            Set<String> kinds = new LinkedHashSet<>();
            for (Finding f : unlifted) kinds.add(f.kind());
            return kinds;
        }
    }

    // ---- reading -----------------------------------------------------------------------------------------------------

    /**
     * Children that are read ({@code sql}, {@code comment}, {@code preConditions}) or cannot write or scan a row of any table; any
     * other is {@link Opaque}. The inert ones: {@code rollback} (never runs at boot), {@code validCheckSum} (a checksum
     * declaration), {@code empty} (no change), {@code output} (prints a message), {@code tagDatabase} (writes one
     * DATABASECHANGELOG tag row, Liquibase's own bookkeeping) and {@code stop} (halts the update with a message, so it runs no
     * data statement). Each is a no-op for the question the gate asks, whether an upgrade boot rewrites a table that grows.
     */
    private static final Set<String> READ_OR_NONEXECUTABLE =
        Set.of("sql", "comment", "rollback", "preConditions", "validCheckSum", "empty", "output", "tagDatabase", "stop");

    static List<Changeset> read(List<Path> filesInOrder) throws Exception {
        List<Changeset> out = new ArrayList<>();
        DocumentBuilderFactory dbf = DocumentBuilderFactory.newInstance();
        dbf.setNamespaceAware(true);
        dbf.setFeature(XMLConstants.FEATURE_SECURE_PROCESSING, true);
        dbf.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true);
        for (Path file : filesInOrder) {
            Element root = dbf.newDocumentBuilder()
                .parse(new InputSource(new StringReader(Files.readString(file)))).getDocumentElement();
            for (Element cs : children(root, "changeSet")) {
                List<Stmt> statements = new ArrayList<>();
                StringBuilder comment = new StringBuilder();
                List<Stmt> checks = new ArrayList<>();
                List<Opaque> opaque = new ArrayList<>();
                int sqlFiles = 0;
                for (Element child : children(cs, null)) {
                    String name = localName(child);
                    switch (name) {
                        case "sql" -> statements.addAll(statements(child.getTextContent()));
                        case "comment" -> comment.append(child.getTextContent()).append('\n');
                        case "preConditions" -> {
                            if ("CONTINUE".equalsIgnoreCase(child.getAttribute("onFail").trim())) {
                                for (Element check : descendants(child, "sqlCheck")) {
                                    checks.addAll(statements(check.getTextContent()));
                                }
                            }
                        }
                        case "sqlFile" -> sqlFiles++;
                        default -> {
                            if (!READ_OR_NONEXECUTABLE.contains(name)) {
                                String table = child.getAttribute("tableName").trim();
                                opaque.add(new Opaque(name, table.isEmpty() ? null : bare(table.toLowerCase())));
                            }
                        }
                    }
                }
                out.add(new Changeset(file.getFileName().toString(), cs.getAttribute("id"), cs.getAttribute("author"), statements,
                    comment.toString(), checks, opaque, sqlFiles));
            }
        }
        return out;
    }

    private static String localName(Element e) {
        return e.getLocalName() != null ? e.getLocalName() : e.getTagName();
    }

    private static List<Element> children(Element parent, String name) {
        List<Element> out = new ArrayList<>();
        for (Node n = parent.getFirstChild(); n != null; n = n.getNextSibling()) {
            if (n instanceof Element e && (name == null || name.equals(localName(e)))) out.add(e);
        }
        return out;
    }

    private static List<Element> descendants(Element parent, String name) {
        List<Element> out = new ArrayList<>();
        NodeList all = parent.getElementsByTagNameNS("*", name);
        for (int i = 0; i < all.getLength(); i++) out.add((Element) all.item(i));
        return out;
    }

    // ---- masking and statement splitting -----------------------------------------------------------------------------

    private static final Pattern DOLLAR_TAG = Pattern.compile("\\$(?:[A-Za-z_]\\w*)?\\$");

    private static boolean identChar(char c) {
        return Character.isLetterOrDigit(c) || c == '_';
    }

    /**
     * Splits a SQL body into statements, each lowercased, whitespace-collapsed, double quotes dropped, with comments removed
     * and every string literal, escape string and dollar-quoted body replaced by a {@code '#n#'} placeholder (the literal's
     * text is kept in {@link Stmt#literals()}). The semicolons inside a mask are therefore never statement ends.
     */
    static List<Stmt> statements(String body) {
        return statements(body, false);
    }

    /** As {@link #statements(String)}; with {@code maskQuotedNames} a double-quoted identifier is masked too (a {@code ""} escape ends one span and starts the next, so both are hidden). */
    static List<Stmt> statements(String body, boolean maskQuotedNames) {
        StringBuilder masked = new StringBuilder();
        List<String> literals = new ArrayList<>();
        int n = body.length();
        int i = 0;
        while (i < n) {
            char c = body.charAt(i);
            if (body.startsWith("--", i)) {
                int end = body.indexOf('\n', i);
                i = end < 0 ? n : end;
                masked.append(' ');
            } else if (body.startsWith("/*", i)) {
                int depth = 1;
                i += 2;
                while (i < n && depth > 0) {
                    if (body.startsWith("/*", i)) { depth++; i += 2; }
                    else if (body.startsWith("*/", i)) { depth--; i += 2; }
                    else i++;
                }
                masked.append(' ');
            } else if (c == '\'') {
                boolean escapes = i > 0 && (body.charAt(i - 1) == 'e' || body.charAt(i - 1) == 'E')
                    && (i < 2 || !identChar(body.charAt(i - 2)));
                StringBuilder lit = new StringBuilder();
                int j = i + 1;
                while (j < n) {
                    char d = body.charAt(j);
                    if (escapes && d == '\\' && j + 1 < n) { lit.append(body, j, j + 2); j += 2; continue; }
                    if (d == '\'') {
                        if (j + 1 < n && body.charAt(j + 1) == '\'') { lit.append('\''); j += 2; continue; }
                        break;
                    }
                    lit.append(d);
                    j++;
                }
                literals.add(lit.toString());
                masked.append("'#").append(literals.size() - 1).append("#'");
                i = j + 1;
            } else if (c == '"') {
                int end = body.indexOf('"', i + 1);
                if (end < 0) end = n - 1;
                if (maskQuotedNames) masked.append("\"q\"");
                else masked.append(body, i, end + 1);
                i = end + 1;
            } else if (c == '$' && (i == 0 || !identChar(body.charAt(i - 1)))) {
                Matcher m = DOLLAR_TAG.matcher(body).region(i, n);
                if (m.lookingAt()) {
                    String tag = m.group();
                    int end = body.indexOf(tag, i + tag.length());
                    if (end < 0) end = n - tag.length();
                    literals.add(body.substring(i + tag.length(), end));
                    masked.append("'#").append(literals.size() - 1).append("#'");
                    i = end + tag.length();
                } else {
                    masked.append(c);
                    i++;
                }
            } else {
                masked.append(c);
                i++;
            }
        }
        List<Stmt> out = new ArrayList<>();
        for (String raw : masked.toString().split(";")) {
            String text = normalise(raw);
            if (!text.isEmpty()) out.add(new Stmt(text, literals));
        }
        return out;
    }

    static String normalise(String s) {
        return s.toLowerCase().replace("\"", " ").replaceAll("\\s+", " ").replaceAll(" ?\\. ?", ".").trim();
    }

    // ---- statement shapes --------------------------------------------------------------------------------------------

    private static final String IDENT = "[a-z_][\\w$]*(?:\\.[a-z_][\\w$]*)*";

    private static final Pattern ALTER_TABLE = Pattern.compile("^alter table (?:if exists )?(?:only )?(" + IDENT + ")(?: ?\\*)? (.*)$");
    private static final Pattern INSERT_INTO = Pattern.compile("^insert into (" + IDENT + ")\\b");
    private static final Pattern DML_WORD = Pattern.compile("\\b(?:update|delete from|insert into|merge into)\\b");
    private static final Pattern SELECT_WORD = Pattern.compile("\\bselect\\b");
    private static final Pattern SELECT_INTO = Pattern.compile("^select\\b.*\\binto\\b");
    private static final Pattern ALTER_TYPE = Pattern.compile("^alter (?:column )?" + IDENT + " (?:set data )?type\\b");
    private static final Pattern GENERATED = Pattern.compile("\\bgenerated always as ?\\(");
    private static final Pattern CTAS_HEAD = Pattern.compile(
        "^create (?:(?:global |local )?(?:temp |temporary )|unlogged )?table (?:if not exists )?" + IDENT + " ?");
    private static final Pattern CTAS_AS = Pattern.compile(
        "^(?:(?:using \\w+|with \\([^)]*\\)|tablespace \\w+|on commit (?:drop|delete rows|preserve rows)) )*as\\b");
    private static final Pattern ADD_CONSTRAINT = Pattern.compile(
        "^add (?:constraint\\b|(?:primary key|unique|foreign key|check|exclude)\\b)");
    private static final Pattern NOT_VALID = Pattern.compile("\\bnot valid\\b");
    private static final Pattern VALIDATE = Pattern.compile("^validate constraint\\b");
    private static final Pattern SET_NOT_NULL = Pattern.compile("^alter (?:column )?" + IDENT + " set not null\\b");
    /** DROP [COLUMN] [IF EXISTS] x: any drop action that is not DROP CONSTRAINT (DROP DEFAULT and DROP NOT NULL follow ALTER COLUMN). */
    private static final Pattern DROP_COLUMN = Pattern.compile("^drop (?!constraint\\b)");
    private static final Pattern RENAME_TO = Pattern.compile("^rename to (" + IDENT + ")");
    /** RENAME [COLUMN] x TO y; RENAME CONSTRAINT and RENAME TO are handled apart. */
    private static final Pattern RENAME_COLUMN = Pattern.compile("^rename (?!constraint\\b|to\\b)");
    private static final Pattern UPDATE_TARGET = Pattern.compile("^update (?:only )?(" + IDENT + ")");
    private static final Pattern DELETE_TARGET = Pattern.compile("^delete from (?:only )?(" + IDENT + ")");
    private static final Pattern MERGE_TARGET = Pattern.compile("^merge into (?:only )?(" + IDENT + ")");
    private static final Pattern GROWING_WORD = Pattern.compile(
        "\\bchunks\\b|\\bchunks_\\w+|\\b(?:" + String.join("|", GROWING_TABLES) + ")\\b");
    private static final Pattern DROP_TABLE = Pattern.compile("^drop table (?:if exists )?(.*?)(?: cascade| restrict)?$");

    static String bare(String qualified) {
        return qualified.substring(qualified.lastIndexOf('.') + 1);
    }

    private static boolean isDataMoves(String table) {
        return bare(table).equals("data_moves") && (table.equals("data_moves") || table.equals("nexus.data_moves"));
    }

    /** Splits at commas outside parentheses (the actions of one ALTER TABLE, the values of one tuple). */
    static List<String> splitTop(String s) {
        List<String> out = new ArrayList<>();
        int depth = 0;
        StringBuilder cur = new StringBuilder();
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '(') depth++;
            else if (c == ')') depth--;
            if (c == ',' && depth == 0) {
                out.add(cur.toString().trim());
                cur.setLength(0);
            } else {
                cur.append(c);
            }
        }
        out.add(cur.toString().trim());
        return out;
    }

    /** True when the statement is CREATE TABLE ... AS: after the name, an optional balanced column list, then AS. */
    private static boolean isCreateTableAs(String s) {
        Matcher head = CTAS_HEAD.matcher(s);
        if (!head.find()) return false;
        String rest = s.substring(head.end());
        if (rest.startsWith("(")) {
            int depth = 0;
            int i = 0;
            for (; i < rest.length(); i++) {
                if (rest.charAt(i) == '(') depth++;
                else if (rest.charAt(i) == ')' && --depth == 0) break;
            }
            if (i >= rest.length()) return false;
            rest = rest.substring(i + 1).trim();
        }
        return CTAS_AS.matcher(rest).find();
    }

    // ---- registering INSERT ------------------------------------------------------------------------------------------

    /** A move a changeset registers: its label, the table its dml_utils runs are cut over, and the label it supersedes (or null). */
    record Registration(String label, String drivingTable, String supersedes, String verification) {}

    /** The registrations in a registering INSERT, or null when the statement is not one the gate can read. */
    private static List<Registration> parseRegistering(Stmt stmt) {
        Matcher head = Pattern.compile("^insert into (?:nexus\\.)?data_moves ?\\(([^)]*)\\) ?values ?(.*)$").matcher(stmt.text());
        if (!head.matches()) return null;
        List<String> columns = new ArrayList<>();
        for (String c : head.group(1).split(",")) columns.add(c.trim());
        int label = columns.indexOf("label");
        int table = columns.indexOf("driving_table");
        int supersedes = columns.indexOf("supersedes");
        int verification = columns.indexOf("verification");
        if (label < 0 || table < 0 || verification < 0) return null;
        List<Registration> out = new ArrayList<>();
        String rest = head.group(2).trim();
        while (rest.startsWith("(")) {
            int depth = 0;
            int i = 0;
            for (; i < rest.length(); i++) {
                if (rest.charAt(i) == '(') depth++;
                else if (rest.charAt(i) == ')' && --depth == 0) break;
            }
            if (i >= rest.length()) return null;
            List<String> values = splitTop(rest.substring(1, i));
            if (values.size() != columns.size()) return null;
            String l = literal(values.get(label), stmt.literals());
            String t = literal(values.get(table), stmt.literals());
            String v = literal(values.get(verification), stmt.literals());
            if (l == null || t == null || v == null || l.isEmpty() || t.isEmpty()) return null;
            // NULL, or anything that is not a literal label, names no superseded move
            String sup = supersedes < 0 ? null : literal(values.get(supersedes), stmt.literals());
            out.add(new Registration(l, t.toLowerCase().trim(), sup, v));
            rest = rest.substring(i + 1).trim();
            if (rest.startsWith(",")) rest = rest.substring(1).trim();
            else break;
        }
        return out.isEmpty() ? null : out;
    }

    private static String literal(String value, List<String> literals) {
        Matcher m = Pattern.compile("^'#(\\d+)#'$").matcher(value.trim());
        return m.matches() ? literals.get(Integer.parseInt(m.group(1))) : null;
    }

    // ---- the rules ---------------------------------------------------------------------------------------------------

    private static final Pattern EXEMPTION = Pattern.compile("(?m)^[ \\t]*data-move-exempt:[ \\t]*([A-Za-z_][\\w.]*)[ \\t]*:[ \\t]*(\\S.*?)[ \\t]*$");

    /**
     * The moves registered so far: the labels that count as "a move that drives" each table, and the table of each label. A move
     * that supersedes another also counts for the table of the move it replaces, so a step waiting on the old label or on the
     * new one is waiting on a move that drives that table (RDR-246, Finishing a move).
     */
    private static final class Moves {
        final Map<String, Set<String>> labelsByTable = new LinkedHashMap<>();
        final Map<String, String> tableOfLabel = new LinkedHashMap<>();
        final Map<String, String> supersededBy = new LinkedHashMap<>();

        /** Registers a move; its label counts for its own table and, down the chain of moves it supersedes, for each of theirs. */
        void register(Registration r) {
            tableOfLabel.put(r.label(), r.drivingTable());
            if (r.supersedes() != null) supersededBy.put(r.label(), r.supersedes());
            labelsByTable.computeIfAbsent(r.drivingTable(), k -> new LinkedHashSet<>()).add(r.label());
            Set<String> seen = new LinkedHashSet<>();
            String replaced = r.supersedes();
            while (replaced != null && seen.add(replaced)) {
                String table = tableOfLabel.get(replaced);
                if (table != null) labelsByTable.computeIfAbsent(table, k -> new LinkedHashSet<>()).add(r.label());
                replaced = supersededBy.get(replaced);
            }
        }

        boolean drives(String table) {
            return labelsByTable.containsKey(table);
        }
    }

    /** Runs the rules over the changesets in changelog order; a registering INSERT makes its table a driven table for what follows it. */
    static List<Entry> evaluate(List<Changeset> inOrder) {
        Moves moves = new Moves();
        List<Entry> out = new ArrayList<>();
        for (Changeset cs : inOrder) {
            List<Finding> findings = new ArrayList<>();
            // registrations first: the registering changeset's own destructive statements are held to rule 3 too
            for (Stmt s : cs.statements()) {
                if (!s.text().startsWith("insert into ") || !isDataMovesInsert(s)) continue;
                List<Registration> regs = parseRegistering(s);
                if (regs == null) {
                    findings.add(new Finding("r1.registering-unreadable",
                        "an INSERT into nexus.data_moves the gate cannot read (it names its columns, lists VALUES literals for label and driving_table)"));
                } else {
                    for (Registration r : regs) {
                        moves.register(r);
                        if (r.verification().isBlank()) {
                            findings.add(new Finding("r1.verification-empty", "the verification of move " + r.label()
                                + " is empty; it is a SELECT of the rows still in the old shape, and the worker refuses an empty one"));
                        } else if (badPlaceholders(r.verification())) {
                            findings.add(new Finding("r1.verification-placeholder", "the verification of move " + r.label()
                                + " has a '?' or a $-digit placeholder the worker refuses (a permanent failure). Write jsonb_exists, "
                                + "jsonb_exists_any or jsonb_exists_all for the jsonb ?, ?| and ?& operators, and chr(63) or E'\\x3f' for a ? in a "
                                + "pattern. The tenant is the one standalone $1, never in a comment, string literal, dollar quote or quoted name"));
                        }
                    }
                }
            }
            for (Stmt s : cs.statements()) {
                row1(s, findings);
                alterTable(s, cs, moves, findings);
                dropTable(s, cs, moves, findings);
            }
            for (Opaque o : cs.opaque()) {
                findings.add(new Finding("opaque.native-change", "<" + o.element() + (o.table() == null ? "" : " tableName=" + o.table())
                    + "> is a Liquibase change type the gate cannot read; write the change as <sql>", o.table(), ""));
            }
            Set<String> exemptTables = exemptions(cs, moves, findings);
            List<Finding> unlifted = new ArrayList<>();
            for (Finding f : findings) {
                boolean lifted = false;
                for (String table : exemptTables) lifted |= lifts(table, f, moves);
                if (!lifted) unlifted.add(f);
            }
            out.add(new Entry(cs, findings, !exemptTables.isEmpty(), unlifted));
        }
        return out;
    }

    /**
     * A standalone {@code $1}: no word character and no {@code $} on either side. This is the boundary of the data-move worker's
     * check of the verification query (MoveSql, bead nexus-f3yqd.2.14), which binds the tenant as the one parameter {@code $1}. The gate checks this boundary and the
     * raw-versus-masked count of {@link #badPlaceholders}; it does not parse the verification as SQL and does not check anything else
     * the worker checks. Exact regex: {@code (?<![\w$])\$1(?![\w$])}.
     */
    static final Pattern STANDALONE_FIRST_PLACEHOLDER = Pattern.compile("(?<![\\w$])\\$1(?![\\w$])");

    /**
     * True when a verification query has a {@code ?}, a {@code $}-digit form that is not a standalone {@code $1} ({@code $2},
     * {@code $10}, {@code my$1}, {@code $1x}, {@code $$1}), or a standalone {@code $1} that sits in a comment, a string literal or a
     * dollar-quoted body or a double-quoted name.
     *
     * <p>A {@code ?} is found only as the character. An E-string escape such as {@code E'\x3f'} is deliberately not decoded: the
     * worker sees no {@code ?} in that text either, so it is the way to put a question mark in a pattern ({@code chr(63)} is the
     * other).
     *
     * <p>The last case is why this scans two ways. The worker (MoveSql, nexus-f3yqd.2.14) rewrites every {@code $1} to
     * {@code CAST(? AS text)} by TEXT, comments and quotes included, while pgjdbc skips comments, literals and dollar quotes when it
     * counts the placeholders of the statement. The two counts then disagree, {@code setString} goes out of range, and the move fails
     * permanently after it has registered. So the first checks read the raw text of the column's literal (nothing masked), and the
     * count of standalone {@code $1} in the raw text must equal the count after {@link #statements} masks comments, literals and
     * dollar quotes; a {@code $1} the mask hides makes them differ.
     */
    static boolean badPlaceholders(String verification) {
        if (verification.indexOf('?') >= 0) return true;
        if (Pattern.compile("\\$\\d").matcher(STANDALONE_FIRST_PLACEHOLDER.matcher(verification).replaceAll(" ")).find()) return true;
        long raw = STANDALONE_FIRST_PLACEHOLDER.matcher(verification).results().count();
        long visible = 0;
        for (Stmt st : statements(verification, true)) visible += STANDALONE_FIRST_PLACEHOLDER.matcher(st.text()).results().count();
        return raw != visible;
    }

    private static boolean isDataMovesInsert(Stmt s) {
        Matcher m = INSERT_INTO.matcher(s.text());
        return m.find() && isDataMoves(m.group(1));
    }

    private static String target(Pattern p, String t) {
        Matcher m = p.matcher(t);
        return m.find() ? bare(m.group(1)) : null;
    }

    private static Finding at(String kind, String shown, String table, String text) {
        return new Finding(kind, shown, table, text);
    }

    /** Rule 1: row DML. */
    private static void row1(Stmt s, List<Finding> findings) {
        String t = s.text();
        String shown = t.length() > 90 ? t.substring(0, 90) + "..." : t;
        if (t.startsWith("update ")) findings.add(at("r1.update", shown, target(UPDATE_TARGET, t), t));
        else if (t.startsWith("delete from ")) findings.add(at("r1.delete", shown, target(DELETE_TARGET, t), t));
        else if (t.startsWith("merge into ")) findings.add(at("r1.merge", shown, target(MERGE_TARGET, t), t));
        else if (t.startsWith("with ") && DML_WORD.matcher(t).find()) findings.add(at("r1.with-dml", shown, null, t));
        else if (t.startsWith("insert into ")) {
            // a registering INSERT lists VALUES; its SQL is text in a literal (masked), so only a SELECT in it is DML
            if (SELECT_WORD.matcher(t).find()) findings.add(at("r1.insert-select", shown, target(INSERT_INTO, t), t));
        } else if (SELECT_INTO.matcher(t).find()) findings.add(at("r1.select-into", shown, null, t));
        else if (isCreateTableAs(t)) findings.add(at("r1.ctas", shown, null, t));
        Matcher alter = ALTER_TABLE.matcher(t);
        if (alter.matches()) {
            String table = bare(alter.group(1));
            for (String action : splitTop(alter.group(2))) {
                if (ALTER_TYPE.matcher(action).find()) findings.add(at("r1.alter-type", shown, table, t));
                if (GENERATED.matcher(action).find() && action.startsWith("add ")) findings.add(at("r1.generated", shown, table, t));
            }
        }
    }

    /** Rules 2 and 3 on an ALTER TABLE. */
    private static void alterTable(Stmt s, Changeset cs, Moves moves, List<Finding> findings) {
        Matcher alter = ALTER_TABLE.matcher(s.text());
        if (!alter.matches()) return;
        String table = bare(alter.group(1));
        for (String action : splitTop(alter.group(2))) {
            if (growsWithUserData(table)) {
                if (ADD_CONSTRAINT.matcher(action).find() && !NOT_VALID.matcher(action).find()) {
                    findings.add(at("r2.add-constraint", table + ": " + abbreviate(action), table, s.text()));
                }
                if (VALIDATE.matcher(action).find()) findings.add(at("r2.validate", table + ": " + abbreviate(action), table, s.text()));
                if (SET_NOT_NULL.matcher(action).find()) findings.add(at("r2.set-not-null", table + ": " + abbreviate(action), table, s.text()));
            }
            Set<String> touched = new LinkedHashSet<>();
            Matcher rename = RENAME_TO.matcher(action);
            if (DROP_COLUMN.matcher(action).find() || RENAME_COLUMN.matcher(action).find()) touched.add(table);
            else if (rename.find()) {
                touched.add(table);
                touched.add(bare(rename.group(1)));
            }
            for (String t : touched) waitsForItsMove(t, cs, moves, abbreviate(s.text()), s.text(), findings);
        }
    }

    private static void dropTable(Stmt s, Changeset cs, Moves moves, List<Finding> findings) {
        Matcher m = DROP_TABLE.matcher(s.text());
        if (!m.matches()) return;
        for (String name : m.group(1).split(",")) {
            String t = bare(name.trim());
            if (!t.isEmpty()) waitsForItsMove(t, cs, moves, abbreviate(s.text()), s.text(), findings);
        }
    }

    /** Rule 3: a destructive statement on a driven table needs a CONTINUE precondition on that move's completed_at. */
    private static void waitsForItsMove(String table, Changeset cs, Moves moves, String shown, String text, List<Finding> findings) {
        Set<String> labels = moves.labelsByTable.get(table);
        if (labels == null) return;
        for (Stmt check : cs.continueChecks()) {
            if (!check.text().contains("data_moves") || !check.text().contains("completed_at")) continue;
            for (String label : labels) {
                if (check.literals().contains(label)) return;
            }
        }
        findings.add(at("r3.destructive-without-precondition", table + " is driven by move " + labels
            + "; no onFail=CONTINUE precondition reads nexus.data_moves.completed_at for one of them: " + shown, table, text));
    }

    /**
     * The tables of the changeset's valid exemption lines. Every {@code data-move-exempt:} line is read, so a changeset that
     * touches two small tables can name both; each line lifts only findings on its own table (see {@link #lifts}). An invalid line
     * adds an {@code exemption} finding and lifts nothing. A valid line names a small table the changeset names.
     */
    private static Set<String> exemptions(Changeset cs, Moves moves, List<Finding> findings) {
        Set<String> valid = new LinkedHashSet<>();
        Matcher m = EXEMPTION.matcher(cs.comment());
        while (m.find()) {
            String table = bare(m.group(1)).toLowerCase();
            if (growsWithUserData(table) || moves.drives(table)) {
                findings.add(new Finding("exemption", table + " grows with user data or is driven by a registered move, so it cannot be exempted"));
                continue;
            }
            if (namedByTheChangeset(cs, table)) valid.add(table);
            else findings.add(new Finding("exemption", "the changeset's statements never name " + table));
        }
        return valid;
    }

    private static boolean namedByTheChangeset(Changeset cs, String table) {
        Pattern named = namesTable(table);
        for (Stmt s : cs.statements()) {
            if (named.matcher(s.text()).find()) return true;
        }
        for (Opaque o : cs.opaque()) {
            if (table.equals(o.table())) return true;
        }
        return false;
    }

    private static Pattern namesTable(String table) {
        return Pattern.compile("\\b" + Pattern.quote(table) + "\\b");
    }

    /**
     * Whether the exemption for {@code table} lifts a finding: the finding is on that table (its target; with no single target,
     * the statement names that table), and the statement names no table that grows or that a registered move drives, anywhere in it
     * (a subquery, a FROM). So {@code UPDATE plans ...; UPDATE chunks ...} stays red on the second, and so does
     * {@code UPDATE chunks ... (SELECT ... FROM plans)}.
     */
    private static boolean lifts(String table, Finding f, Moves moves) {
        if (GROWING_WORD.matcher(f.text()).find()) return false;
        for (String driven : moves.labelsByTable.keySet()) {
            if (namesTable(driven).matcher(f.text()).find()) return false;
        }
        if (f.table() != null) return f.table().equals(table);
        return !f.text().isEmpty() && namesTable(table).matcher(f.text()).find();
    }

    private static String abbreviate(String s) {
        return s.length() > 110 ? s.substring(0, 110) + "..." : s;
    }

    // ---- the frozen list ---------------------------------------------------------------------------------------------

    /** The frozen list: entries as {@code file::id::author}, and the include (file) from which on no entry may sit. */
    record Frozen(Set<String> keys, String freezePoint) {
        static Frozen parse(List<String> lines) {
            Set<String> keys = new LinkedHashSet<>();
            String point = null;
            Pattern freeze = Pattern.compile("^#\\s*freeze-point:\\s*(\\S+)\\s*$");
            for (String line : lines) {
                Matcher m = freeze.matcher(line);
                if (m.matches()) point = m.group(1);
                else if (!line.isBlank() && !line.startsWith("#")) keys.add(line.trim());
            }
            return new Frozen(keys, point);
        }
    }

    /**
     * What is wrong with a frozen list against the evaluated changelog: an entry that names no changeset, one in the freeze-point
     * include or after it in changelog order (so a changeset in a later or the closing include cannot be added), and one whose
     * changeset has no finding left (stale: the list holds exactly the offenders). It does NOT catch a new changeset added to an
     * include that sits before the freeze point; that is review's.
     */
    static List<String> frozenProblems(Frozen frozen, List<String> includeOrder, List<Entry> entries) {
        List<String> problems = new ArrayList<>();
        int point = includeOrder.indexOf(frozen.freezePoint());
        if (point < 0) problems.add("the freeze point " + frozen.freezePoint() + " is not an include of the changelog");
        Map<String, Entry> byKey = new LinkedHashMap<>();
        for (Entry e : entries) byKey.put(e.changeset().key(), e);
        for (String key : frozen.keys()) {
            Entry e = byKey.get(key);
            if (e == null) {
                problems.add("frozen entry names no changeset: " + key);
                continue;
            }
            if (point >= 0 && includeOrder.indexOf(e.changeset().file()) >= point) {
                problems.add("frozen entry sits in or after the freeze-point include " + frozen.freezePoint() + " (a changeset there cannot be frozen): " + key);
            }
            if (e.findings().isEmpty()) problems.add("stale frozen entry (no rule finds it any more, remove it): " + key);
        }
        return problems;
    }

    /** Changesets with a finding that neither the frozen list nor a valid exemption line (for that finding's table) covers. */
    static List<Entry> offenders(List<Entry> entries, Frozen frozen) {
        List<Entry> out = new ArrayList<>();
        for (Entry e : entries) {
            if (e.unlifted().isEmpty() || frozen.keys().contains(e.changeset().key())) continue;
            out.add(e);
        }
        return out;
    }

    /** Reads the includes of a master changelog, in order, without the comments (the text "include" appears in prose there). */
    static List<String> includes(Path master) throws IOException {
        String text = Files.readString(master).replaceAll("(?s)<!--.*?-->", "");
        Matcher m = Pattern.compile("<include\\s+file=\"[^\"]*?([^/\"]+)\"").matcher(text);
        List<String> out = new ArrayList<>();
        while (m.find()) out.add(m.group(1));
        return out;
    }
}
