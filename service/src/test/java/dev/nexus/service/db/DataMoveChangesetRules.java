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

    /** A changeset read from a changelog file: its executable statements, comment and CONTINUE-precondition checks. */
    record Changeset(String file, String id, List<Stmt> statements, String comment, List<Stmt> continueChecks, int opaqueChildren) {
        String key() {
            return file + "::" + id;
        }
    }

    record Finding(String kind, String detail) {
        @Override
        public String toString() {
            return kind + " (" + detail + ")";
        }
    }

    /** What the rules say of one changeset: its findings before any exemption, and whether a valid exemption line lifts them. */
    record Entry(Changeset changeset, List<Finding> findings, boolean exempt) {
        Set<String> kinds() {
            Set<String> kinds = new LinkedHashSet<>();
            for (Finding f : findings) kinds.add(f.kind());
            return kinds;
        }
    }

    // ---- reading -----------------------------------------------------------------------------------------------------

    /** Text of a changeset's child elements that the rules do not read ({@code sqlFile}, a custom change and the like). */
    private static final Set<String> READ_OR_NONEXECUTABLE =
        Set.of("sql", "comment", "rollback", "preConditions", "validCheckSum");

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
                int opaque = 0;
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
                        default -> {
                            if (!READ_OR_NONEXECUTABLE.contains(name)) opaque++;
                        }
                    }
                }
                out.add(new Changeset(file.getFileName().toString(), cs.getAttribute("id"), statements, comment.toString(), checks, opaque));
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
                masked.append(body, i, end + 1);
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
        return s.toLowerCase().replace("\"", "").replaceAll("\\s+", " ").replaceAll(" ?\\. ?", ".").trim();
    }

    // ---- statement shapes --------------------------------------------------------------------------------------------

    private static final String IDENT = "[a-z_][\\w$]*(?:\\.[a-z_][\\w$]*)*";

    private static final Pattern ALTER_TABLE = Pattern.compile("^alter table (?:if exists )?(?:only )?(" + IDENT + ")\\*? (.*)$");
    private static final Pattern INSERT_INTO = Pattern.compile("^insert into (" + IDENT + ")\\b");
    private static final Pattern DML_WORD = Pattern.compile("\\b(?:update|delete from|insert into|merge into)\\b");
    private static final Pattern SELECT_WORD = Pattern.compile("\\bselect\\b");
    private static final Pattern SELECT_INTO = Pattern.compile("^select\\b.*\\binto\\b");
    private static final Pattern ALTER_TYPE = Pattern.compile("^alter (?:column )?" + IDENT + " (?:set data )?type\\b");
    private static final Pattern GENERATED = Pattern.compile("\\bgenerated always as \\(");
    private static final Pattern CTAS_HEAD = Pattern.compile(
        "^create (?:(?:global |local )?(?:temp |temporary )|unlogged )?table (?:if not exists )?" + IDENT + " ?");
    private static final Pattern CTAS_AS = Pattern.compile(
        "^(?:(?:using \\w+|with \\([^)]*\\)|tablespace \\w+|on commit (?:drop|delete rows|preserve rows)) )*as\\b");
    private static final Pattern ADD_CONSTRAINT = Pattern.compile(
        "^add (?:constraint\\b|(?:primary key|unique|foreign key|check|exclude)\\b)");
    private static final Pattern NOT_VALID = Pattern.compile("\\bnot valid\\b");
    private static final Pattern VALIDATE = Pattern.compile("^validate constraint\\b");
    private static final Pattern SET_NOT_NULL = Pattern.compile("^alter (?:column )?" + IDENT + " set not null\\b");
    private static final Pattern DROP_COLUMN = Pattern.compile("^drop column\\b");
    private static final Pattern RENAME_TO = Pattern.compile("^rename to (" + IDENT + ")");
    private static final Pattern RENAME_OTHER = Pattern.compile("^rename (?:column|constraint)\\b");
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

    /** A move a changeset registers: the label and the table its dml_utils runs are cut over. */
    record Registration(String label, String drivingTable) {}

    /** The registrations in a registering INSERT, or null when the statement is not one the gate can read. */
    private static List<Registration> parseRegistering(Stmt stmt) {
        Matcher head = Pattern.compile("^insert into (?:nexus\\.)?data_moves ?\\(([^)]*)\\) ?values ?(.*)$").matcher(stmt.text());
        if (!head.matches()) return null;
        List<String> columns = new ArrayList<>();
        for (String c : head.group(1).split(",")) columns.add(c.trim());
        int label = columns.indexOf("label");
        int table = columns.indexOf("driving_table");
        if (label < 0 || table < 0) return null;
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
            if (l == null || t == null || l.isEmpty() || t.isEmpty()) return null;
            out.add(new Registration(l, t.toLowerCase().trim()));
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

    /** Runs the rules over the changesets in changelog order; a registering INSERT makes its table a driven table for what follows it. */
    static List<Entry> evaluate(List<Changeset> inOrder) {
        Map<String, Set<String>> labelsByTable = new LinkedHashMap<>();
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
                    for (Registration r : regs) labelsByTable.computeIfAbsent(r.drivingTable(), k -> new LinkedHashSet<>()).add(r.label());
                }
            }
            for (Stmt s : cs.statements()) {
                row1(s, findings);
                alterTable(s, cs, labelsByTable, findings);
                dropTable(s, cs, labelsByTable, findings);
            }
            boolean exempt = exemption(cs, labelsByTable, findings);
            out.add(new Entry(cs, findings, exempt));
        }
        return out;
    }

    private static boolean isDataMovesInsert(Stmt s) {
        Matcher m = INSERT_INTO.matcher(s.text());
        return m.find() && isDataMoves(m.group(1));
    }

    /** Rule 1: row DML. */
    private static void row1(Stmt s, List<Finding> findings) {
        String t = s.text();
        String shown = t.length() > 90 ? t.substring(0, 90) + "..." : t;
        if (t.startsWith("update ")) findings.add(new Finding("r1.update", shown));
        else if (t.startsWith("delete from ")) findings.add(new Finding("r1.delete", shown));
        else if (t.startsWith("merge into ")) findings.add(new Finding("r1.merge", shown));
        else if (t.startsWith("with ") && DML_WORD.matcher(t).find()) findings.add(new Finding("r1.with-dml", shown));
        else if (t.startsWith("insert into ")) {
            // a registering INSERT lists VALUES; its SQL is text in a literal (masked), so only a SELECT in it is DML
            if (SELECT_WORD.matcher(t).find()) findings.add(new Finding("r1.insert-select", shown));
        } else if (SELECT_INTO.matcher(t).find()) findings.add(new Finding("r1.select-into", shown));
        else if (isCreateTableAs(t)) findings.add(new Finding("r1.ctas", shown));
        Matcher at = ALTER_TABLE.matcher(t);
        if (at.matches()) {
            for (String action : splitTop(at.group(2))) {
                if (ALTER_TYPE.matcher(action).find()) findings.add(new Finding("r1.alter-type", shown));
                if (GENERATED.matcher(action).find() && action.startsWith("add ")) findings.add(new Finding("r1.generated", shown));
            }
        }
    }

    /** Rules 2 and 3 on an ALTER TABLE. */
    private static void alterTable(Stmt s, Changeset cs, Map<String, Set<String>> driven, List<Finding> findings) {
        Matcher at = ALTER_TABLE.matcher(s.text());
        if (!at.matches()) return;
        String table = bare(at.group(1));
        for (String action : splitTop(at.group(2))) {
            if (growsWithUserData(table)) {
                if (ADD_CONSTRAINT.matcher(action).find() && !NOT_VALID.matcher(action).find()) {
                    findings.add(new Finding("r2.add-constraint", table + ": " + abbreviate(action)));
                }
                if (VALIDATE.matcher(action).find()) findings.add(new Finding("r2.validate", table + ": " + abbreviate(action)));
                if (SET_NOT_NULL.matcher(action).find()) findings.add(new Finding("r2.set-not-null", table + ": " + abbreviate(action)));
            }
            Set<String> touched = new LinkedHashSet<>();
            Matcher rename = RENAME_TO.matcher(action);
            if (DROP_COLUMN.matcher(action).find() || RENAME_OTHER.matcher(action).find()) touched.add(table);
            else if (rename.find()) {
                touched.add(table);
                touched.add(bare(rename.group(1)));
            }
            for (String t : touched) waitsForItsMove(t, cs, driven, abbreviate(s.text()), findings);
        }
    }

    private static void dropTable(Stmt s, Changeset cs, Map<String, Set<String>> driven, List<Finding> findings) {
        Matcher m = DROP_TABLE.matcher(s.text());
        if (!m.matches()) return;
        for (String name : m.group(1).split(",")) {
            String t = bare(name.trim());
            if (!t.isEmpty()) waitsForItsMove(t, cs, driven, abbreviate(s.text()), findings);
        }
    }

    /** Rule 3: a destructive statement on a driven table needs a CONTINUE precondition on that move's completed_at. */
    private static void waitsForItsMove(String table, Changeset cs, Map<String, Set<String>> driven, String shown, List<Finding> findings) {
        Set<String> labels = driven.get(table);
        if (labels == null) return;
        for (Stmt check : cs.continueChecks()) {
            if (!check.text().contains("data_moves") || !check.text().contains("completed_at")) continue;
            for (String label : labels) {
                if (check.literals().contains(label)) return;
            }
        }
        findings.add(new Finding("r3.destructive-without-precondition", table + " is driven by move " + labels
            + "; no onFail=CONTINUE precondition reads nexus.data_moves.completed_at for one of them: " + shown));
    }

    /** The exemption line, if the changeset has one: lifts every rule when it names a small table the changeset touches. */
    private static boolean exemption(Changeset cs, Map<String, Set<String>> driven, List<Finding> findings) {
        Matcher m = EXEMPTION.matcher(cs.comment());
        if (!m.find()) return false;
        String table = bare(m.group(1)).toLowerCase();
        if (growsWithUserData(table) || driven.containsKey(table)) {
            findings.add(new Finding("exemption", table + " grows with user data or is driven by a registered move, so it cannot be exempted"));
            return false;
        }
        Pattern named = Pattern.compile("\\b" + Pattern.quote(table) + "\\b");
        for (Stmt s : cs.statements()) {
            if (named.matcher(s.text()).find()) return true;
        }
        findings.add(new Finding("exemption", "the changeset's statements never name " + table));
        return false;
    }

    private static String abbreviate(String s) {
        return s.length() > 110 ? s.substring(0, 110) + "..." : s;
    }

    // ---- the frozen list ---------------------------------------------------------------------------------------------

    /** The frozen list: entries as {@code file::id}, and the include (file) after which no entry may sit. */
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
     * What is wrong with a frozen list against the evaluated changelog: an entry that names no changeset, one that sits after
     * the freeze point in changelog order (the list never grows to admit a new changeset), and one whose changeset has no
     * finding left (stale: the list holds exactly the offenders).
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
            if (point >= 0 && includeOrder.indexOf(e.changeset().file()) > point) {
                problems.add("frozen entry sits after the freeze point " + frozen.freezePoint() + " (the list never admits a new changeset): " + key);
            }
            if (e.findings().isEmpty()) problems.add("stale frozen entry (no rule finds it any more, remove it): " + key);
        }
        return problems;
    }

    /** Changesets with a finding that neither the frozen list nor a valid exemption line covers. */
    static List<Entry> offenders(List<Entry> entries, Frozen frozen) {
        List<Entry> out = new ArrayList<>();
        for (Entry e : entries) {
            if (e.findings().isEmpty() || frozen.keys().contains(e.changeset().key()) || e.exempt()) continue;
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
