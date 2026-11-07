// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.Table;

import java.lang.reflect.Modifier;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Static census of the engine routes that return chunk text (nexus-rfvyy.7.4, RDR-243 Test Plan "Route parity").
 *
 * <p>A {@code pg_proc} check alone misses every read that is not a SQL function, and get-by-id is a jOOQ query
 * (RDR-243 Key Discoveries). So the census starts from the ROUTE TABLE, the {@code case "/x" -> handler(...)} lines
 * of {@code VectorHandler}'s dispatch, follows each handler to the {@code PgVectorRepository} methods it can reach,
 * and calls a route a chunk-text route when a reachable method reads chunk text:
 * <ul>
 *   <li>it selects the {@code chunk_text} column ({@code chunkText()} or {@code CHUNK_TEXT}), or reads a whole row of
 *       a table that has the column ({@code selectFrom(T)}, {@code select().from(T)}, {@code T.asterisk()}), or</li>
 *   <li>it calls a generated jOOQ table-function whose {@code pg_proc} result row carries chunk text. The caller
 *       passes that set, derived from {@code pg_proc} by result shape, never typed: a function added in a later
 *       changeset joins it without this class changing.</li>
 * </ul>
 * Everything is attributed by brace matching over the source with comments and string literals blanked; a text read
 * it cannot attribute to a method (inside a nested type, or a field initialiser) fails the census rather than being
 * guessed at, and so does a route case whose handler is not a method of the handler class.
 *
 * <p>Two things read the literals the call graph blanks (nexus-rfvyy.7.17). A raw-SQL read, {@code chunk_text} or a
 * call of a derived text function written inside a string literal or text block, is found on each member's
 * comment-free text; a SQL constant in a field initialiser has no method to attribute to and fails the census. And
 * "conforms" is a USE check, not a mention check, at both ends and per call: a text-reading repository method must
 * put {@code excludeSensitive} inside the arguments of the call that gates the read ({@link #flagGatesTheRead}), and
 * every repository call a handler makes that can reach a text read must carry the flag the handler parsed in its own
 * arguments. The route recipes of {@code VectorSensitivityExcludeIntegrationTest} stay the backstop for what text
 * matching cannot see.
 *
 * <p>Call graph. In the handler, an unqualified {@code name(} reaches a handler method and {@code repo.name(} a
 * repository method; inside the repository an unqualified {@code name(} (or {@code ::name}) reaches a repository
 * method. A receiver other than {@code repo} is not followed: {@code body.get(...)} must not read as {@code
 * repo.get(...)}. Overloads collapse to one name, which can only add reachable methods, so the census errs toward
 * calling a route a text route.
 */
final class ChunkTextRouteCensus {

    private ChunkTextRouteCensus() { }

    /**
     * A method (or nested type, or a field with an initialiser) of a top-level class: its name, its header plus body
     * with comments AND string literals blanked ({@code text}, the form every call-graph match runs on), and the same
     * span with only the comments blanked ({@code literalText}, the form the raw-SQL detector runs on).
     */
    record Member(String name, String text, boolean nestedType, String literalText) { }

    /**
     * What the census found for one route. {@code handlerCallsWithoutFlag} names each repository call in a reached
     * handler method that can reach a text read and does not carry the parsed flag in its own arguments.
     */
    record Finding(String route, String handler, Set<String> sinkMethods, boolean handlerParsesSensitivity,
                   Set<String> sinkMethodsWithoutFlag, Set<String> exemptSinksReached,
                   Set<String> handlerCallsWithoutFlag) {
        boolean readsChunkText() { return !sinkMethods.isEmpty(); }
        boolean conforms() { return !readsChunkText() || (handlerParsesSensitivity && sinkMethodsWithoutFlag.isEmpty()); }
    }

    private static final Pattern CASE = Pattern.compile("case\\s+\"(/[^\"]*)\"\\s*->\\s*(\\w+)\\s*\\(");
    private static final Pattern ANNOTATION_START = Pattern.compile("@\\s*\\w+(?:\\s*\\.\\s*\\w+)*");
    private static final Pattern TYPE_KEYWORD = Pattern.compile("\\b(class|record|interface|enum)\\b");
    private static final Pattern TOP_CLASS = Pattern.compile("\\b(class|record|interface|enum)\\s+(\\w+)");
    private static final Pattern CHUNK_TEXT_READ = Pattern.compile("\\bchunkText\\s*\\(\\s*\\)|\\bCHUNK_TEXT\\b|\\btextPrefix\\s*\\(\\s*\\)");
    /** A raw-SQL read: the column named in a string literal or text block ({@code chunk_text_hash} is another column). */
    private static final Pattern RAW_CHUNK_TEXT = Pattern.compile("(?i)\\bchunk_text\\b");
    private static final Pattern CONSTANT_CALL = Pattern.compile("\\b([A-Z][A-Z0-9_]+)\\s*\\.\\s*call\\s*\\(");
    private static final Pattern METHOD_REF = Pattern.compile("\\bthis\\s*::\\s*(\\w+)");
    private static final Pattern REPO_CALL = Pattern.compile("\\brepo\\s*\\.\\s*(\\w+)\\s*\\(");
    /** The flag a text-reading repository method must carry, as parameter or as {@code wherePlan.excludeSensitive()}. */
    static final String FLAG = "excludeSensitive";

    /**
     * A whole-row read of a table: the jOOQ forms that select every column without naming {@code chunk_text}
     * (nexus-rfvyy.7.17 r1 F4). The captured group is the table constant.
     */
    /** An optional {@code Tables.} qualifier before a table constant. */
    private static final String TABLES_PREFIX = "(?:Tables\\s*\\.\\s*)?";

    private static final List<Pattern> WHOLE_ROW_READS = List.of(
        Pattern.compile("\\bselectFrom\\s*\\(\\s*" + TABLES_PREFIX + "([A-Z][A-Z0-9_]*)(?![\\w(])"),
        Pattern.compile("\\.\\s*select\\s*\\(\\s*\\)\\s*\\.\\s*from\\s*\\(\\s*" + TABLES_PREFIX + "([A-Z][A-Z0-9_]*)(?![\\w(])"),
        Pattern.compile("(?<![\\w.])" + TABLES_PREFIX + "([A-Z][A-Z0-9_]*)\\s*\\.\\s*(?:as\\s*\\([^)]*\\)\\s*\\.\\s*)?(?:asterisk|fields)\\s*\\(\\s*\\)"),
        Pattern.compile("\\.\\s*fetch(?:Into)?\\s*\\(\\s*" + TABLES_PREFIX + "([A-Z][A-Z0-9_]*)\\s*[,)]"));

    /**
     * The same whole-row forms over a per-dimension chunk handle ({@code ch.table()}, nexus-rfvyy.7.17 r2): the
     * receiver counts when the member declares it a {@code ChunkTable} or takes it from {@code DimTables.CHUNKS},
     * or the handle is written inline ({@code DimTables.CHUNKS.get(dim).table()}). Every chunk table has the column.
     */
    private static final String HANDLE = "(\\w+|DimTables\\s*\\.\\s*CHUNKS\\s*\\.\\s*get\\s*\\([^)]*\\))\\s*\\.\\s*table\\s*\\(\\s*\\)";
    private static final List<Pattern> WHOLE_ROW_HANDLE_READS = List.of(
        Pattern.compile("\\bselectFrom\\s*\\(\\s*" + HANDLE),
        Pattern.compile("\\.\\s*select\\s*\\(\\s*\\)\\s*\\.\\s*from\\s*\\(\\s*" + HANDLE),
        Pattern.compile(HANDLE + "\\s*\\.\\s*(?:as\\s*\\([^)]*\\)\\s*\\.\\s*)?(?:asterisk|fields)\\s*\\(\\s*\\)"),
        Pattern.compile("\\.\\s*fetch(?:Into)?\\s*\\(\\s*" + HANDLE + "\\s*[,)]"));

    /** A raw-SQL whole-row read: {@code select *} or {@code select x.*} from a relation; group 1 is its name. */
    private static final Pattern RAW_SELECT_STAR = Pattern.compile(
        "(?i)\\bselect\\s+(?:distinct\\s+)?(?:\\w+\\s*\\.\\s*)?\\*\\s+from\\s+(?:\\w+\\s*\\.\\s*)?\"?(\\w+)");

    /**
     * The generated jOOQ table constants ({@code Tables.CHUNKS}, {@code Tables.PDF_CHUNKS}, ...) that have a
     * {@code chunk_text} column, read from the generated classes by reflection, never typed.
     */
    static final Set<String> TEXT_TABLES = tablesWithChunkText();

    static Set<String> tablesWithChunkText() {
        Set<String> out = new TreeSet<>();
        try {
            for (var f : dev.nexus.service.jooq.nexus.Tables.class.getFields()) {
                if (!Modifier.isStatic(f.getModifiers()) || !Table.class.isAssignableFrom(f.getType())) continue;
                Table<?> t = (Table<?>) f.get(null);
                if (t.field("chunk_text") != null) out.add(f.getName());
            }
        } catch (IllegalAccessException e) {
            throw new IllegalStateException(e);
        }
        if (!out.contains("CHUNKS")) {
            throw new AssertionError("the generated Tables class lists no table with a chunk_text column: the "
                + "whole-row-read detection would be vacuous");
        }
        return out;
    }

    // ── the census ────────────────────────────────────────────────────────────

    /**
     * @param handlerSource  raw source of the handler class (the dispatch is read from it)
     * @param repoSource     raw source of the repository class
     * @param textFunctions  lower-case names of the generated table-functions whose result carries chunk text
     * @param exemptSinks    repository methods that touch the chunk_text column without returning it (a write that
     *                       sets the column, a read that only compares); they are reported per route as {@link
     *                       Finding#exemptSinksReached} and neither make a route a chunk-text route nor need the flag
     */
    static List<Finding> census(String handlerSource, String repoSource, Set<String> textFunctions,
                                Set<String> exemptSinks) {
        List<Member> handler = membersOf(handlerSource);
        List<Member> repo = membersOf(repoSource);
        Map<String, List<Member>> handlerByName = byName(handler);
        Map<String, List<Member>> repoByName = byName(repo);

        Set<String> sinks = allSinks(repoSource, textFunctions);

        List<Finding> out = new ArrayList<>();
        for (var e : dispatch(handlerSource).entrySet()) {
            String route = e.getKey();
            String entry = e.getValue();
            if (!handlerByName.containsKey(entry)) {
                throw new AssertionError("route " + route + " dispatches to " + entry
                    + ", which is not a method of the handler class: teach this census to follow it");
            }
            Set<String> handlerReach = reach(Set.of(entry), handlerByName, CALL_HANDLER);
            Set<String> repoEntry = new LinkedHashSet<>();
            for (String h : handlerReach) {
                for (Member m : handlerByName.get(h)) {
                    Matcher rc = REPO_CALL.matcher(m.text());
                    while (rc.find()) repoEntry.add(rc.group(1));
                }
            }
            Set<String> repoReach = reach(repoEntry, repoByName, CALL_REPO);
            Set<String> reached = new TreeSet<>();
            Set<String> exemptReached = new TreeSet<>();
            for (String r : repoReach) {
                if (!sinks.contains(r)) continue;
                if (exemptSinks.contains(r)) exemptReached.add(r); else reached.add(r);
            }
            // PER CALL (nexus-rfvyy.7.17 r1 S1): every repository call in a reached handler method that can reach a
            // text read must carry the flag the method parsed in its own argument list, and at least one such call
            // must exist. A method that parses the flag and passes it to one call while another call reads text
            // without it fails on the second.
            Set<String> uncarried = new TreeSet<>();
            int sinkCalls = 0;
            for (String h : handlerReach) {
                if (h.equals("optExcludeSensitive")) continue;
                for (Member m : handlerByName.get(h)) {
                    Set<String> parsed = variablesAssignedFrom(m.text(), "optExcludeSensitive");
                    Matcher rc = REPO_CALL.matcher(m.text());
                    while (rc.find()) {
                        String callee = rc.group(1);
                        boolean toText = false;
                        for (String r : reach(Set.of(callee), repoByName, CALL_REPO)) {
                            if (sinks.contains(r) && !exemptSinks.contains(r)) toText = true;
                        }
                        if (!toText) continue;
                        sinkCalls++;
                        if (!carriesParsedFlag(balanced(m.text(), rc.end() - 1), parsed, PARSE_CALL)) {
                            uncarried.add(h + " -> repo." + callee + "(...)");
                        }
                    }
                }
            }
            boolean parses = sinkCalls > 0 && uncarried.isEmpty();
            Set<String> unflagged = new TreeSet<>();
            for (String s : reached) {
                boolean flagged = false;
                for (Member m : repoByName.get(s)) {
                    if (readsChunkText(m, textFunctions) && flagGatesTheRead(m.text(), textFunctions, sinks, s)) {
                        flagged = true;
                    }
                }
                if (!flagged) unflagged.add(s);
            }
            out.add(new Finding(route, entry, reached, parses, unflagged, exemptReached, uncarried));
        }
        return out;
    }

    /** Every repository method that reads chunk text; a read inside a nested type fails the census. */
    static Set<String> allSinks(String repoSource, Set<String> textFunctions) {
        Set<String> sinks = new TreeSet<>();
        for (Member m : membersOf(repoSource)) {
            if (readsChunkText(m, textFunctions)) {
                if (m.name().equals(FIELD) || m.name().equals("<init>")) {
                    throw new AssertionError("the repository reads chunk text in a field initialiser or an initialiser"
                        + " block (" + m.name() + "): teach this census to attribute it");
                }
                if (m.nestedType()) {
                    throw new AssertionError("the repository reads chunk text inside " + m.name()
                        + ": teach this census to attribute a nested type");
                }
                sinks.add(m.name());
            }
        }
        return sinks;
    }

    private static final Pattern CALL_HANDLER = Pattern.compile("(?<![\\w.])(\\w+)\\s*\\(|(?<!\\w)this\\s*\\.\\s*(\\w+)\\s*\\(");
    private static final Pattern CALL_REPO = CALL_HANDLER;

    private static Set<String> reach(Set<String> start, Map<String, List<Member>> methods, Pattern call) {
        Set<String> seen = new LinkedHashSet<>();
        Deque<String> todo = new ArrayDeque<>();
        for (String s : start) if (methods.containsKey(s) && seen.add(s)) todo.add(s);
        while (!todo.isEmpty()) {
            String cur = todo.poll();
            for (Member m : methods.get(cur)) {
                Matcher c = call.matcher(m.text());
                while (c.find()) {
                    String n = c.group(1) != null ? c.group(1) : c.group(2);
                    if (methods.containsKey(n) && seen.add(n)) todo.add(n);
                }
                Matcher r = METHOD_REF.matcher(m.text());
                while (r.find()) {
                    if (methods.containsKey(r.group(1)) && seen.add(r.group(1))) todo.add(r.group(1));
                }
            }
        }
        return seen;
    }

    /** The lower-case relation names of {@link #TEXT_TABLES} ({@code chunks}, {@code pdf_chunks}). */
    static final Set<String> TEXT_RELATIONS = textRelations();

    private static Set<String> textRelations() {
        Set<String> out = new TreeSet<>();
        try {
            for (String name : TEXT_TABLES) {
                Table<?> t = (Table<?>) dev.nexus.service.jooq.nexus.Tables.class.getField(name).get(null);
                out.add(t.getName().toLowerCase(java.util.Locale.ROOT));
            }
        } catch (ReflectiveOperationException e) {
            throw new IllegalStateException(e);
        }
        return out;
    }

    /** A relation with the column: a {@link #TEXT_RELATIONS} name, or one of its per-dimension or leaf children. */
    static boolean isTextRelation(String relation) {
        String r = relation.toLowerCase(java.util.Locale.ROOT);
        for (String base : TEXT_RELATIONS) if (r.equals(base) || r.startsWith(base + "_")) return true;
        return false;
    }

    private static boolean isChunkHandle(String receiver, String memberText) {
        if (receiver.startsWith("DimTables")) return true;
        String q = Pattern.quote(receiver);
        return Pattern.compile("\\bChunkTable\\s+" + q + "\\b").matcher(memberText).find()
            || Pattern.compile("\\b" + q + "\\s*=\\s*DimTables\\s*\\.\\s*CHUNKS\\b").matcher(memberText).find();
    }

    static boolean readsChunkText(Member m, Set<String> textFunctions) {
        if (CHUNK_TEXT_READ.matcher(m.text()).find()) return true;
        Matcher c = CONSTANT_CALL.matcher(m.text());
        while (c.find()) {
            if (textFunctions.contains(c.group(1).toLowerCase(java.util.Locale.ROOT))) return true;
        }
        // a whole-row read of a table that has the column, without naming it (selectFrom(PDF_CHUNKS))
        for (Pattern whole : WHOLE_ROW_READS) {
            Matcher w = whole.matcher(m.text());
            while (w.find()) if (TEXT_TABLES.contains(w.group(1))) return true;
        }
        for (Pattern whole : WHOLE_ROW_HANDLE_READS) {
            Matcher w = whole.matcher(m.text());
            while (w.find()) if (isChunkHandle(w.group(1), m.text())) return true;
        }
        // raw SQL: the column, or a call of one of the text functions, written inside a string literal
        if (RAW_CHUNK_TEXT.matcher(m.literalText()).find()) return true;
        Matcher star = RAW_SELECT_STAR.matcher(m.literalText());
        while (star.find()) if (isTextRelation(star.group(1))) return true;
        for (String fn : textFunctions) {
            if (Pattern.compile("(?i)\\b" + Pattern.quote(fn) + "\\s*\\(").matcher(m.literalText()).find()) return true;
        }
        return false;
    }

    // ── the flag at the repository end ────────────────────────────────────────

    /** The sensitivity-condition builders a jOOQ read ANDs into its WHERE when the flag is set. */
    static final Set<String> GATE_CONDITIONS = Set.of("notSensitiveCondition", "servableUnderExclude");
    /** Methods that look the flag up after a read and drop the flagged rows. */
    static final Set<String> POST_LOOKUP_GATES = Set.of("attachSensitive", "enrichSearchRows");

    private static final Pattern FLAG_DECL = Pattern.compile("\\b(?:boolean|Boolean)\\s+" + FLAG + "\\b");
    private static final Pattern FLAG_USE = Pattern.compile("\\b" + FLAG + "\\b");
    private static final Pattern FLAG_TERNARY =
        Pattern.compile("\\b" + FLAG + "\\b(?:\\s*\\(\\s*\\))?\\s*\\?\\s*(?:\\w+\\s*\\.\\s*)*(\\w+)\\s*\\(");
    private static final Pattern CONSTANT_CALL_OPEN = Pattern.compile("\\b([A-Z][A-Z0-9_]+)\\s*\\.\\s*call\\s*\\(");
    private static final Pattern NAME_CALL_OPEN = Pattern.compile("(?<![\\w])(\\w+)\\s*\\(");

    /**
     * True when the member USES the flag as a gate (nexus-rfvyy.7.17 r1 S2), not merely mentions it. A use counts
     * when the flag expression sits (a) inside the argument list of a call of a text function
     * ({@code PLAIN_SEARCH_384.call(..., flag)}), (b) inside the argument list of a call to another sink method or to
     * a post-lookup drop ({@link #POST_LOOKUP_GATES}), (c) inside the arguments of a condition builder
     * ({@link #GATE_CONDITIONS}), or (d) as the condition of a ternary that picks such a builder
     * ({@code flag ? notSensitiveCondition(c) : trueCondition()}). The parameter declaration, a log line and a
     * comparison do not count. {@code self} is excluded from the sink methods so a method cannot pass its own flag
     * to itself.
     */
    static boolean flagGatesTheRead(String blankedMemberText, Set<String> textFunctions, Set<String> sinkMethods,
                                    String self) {
        String text = blankDeclarations(blankedMemberText);
        List<int[]> uses = new ArrayList<>();
        Matcher u = FLAG_USE.matcher(text);
        while (u.find()) uses.add(new int[] {u.start(), u.end()});
        if (uses.isEmpty()) return false;
        Matcher t = FLAG_TERNARY.matcher(text);
        while (t.find()) if (GATE_CONDITIONS.contains(t.group(1))) return true;
        Matcher c = CONSTANT_CALL_OPEN.matcher(text);
        while (c.find()) {
            if (textFunctions.contains(c.group(1).toLowerCase(java.util.Locale.ROOT))
                    && anyInside(uses, c.end() - 1, text)) {
                return true;
            }
        }
        Matcher n = NAME_CALL_OPEN.matcher(text);
        while (n.find()) {
            String name = n.group(1);
            boolean gate = (sinkMethods.contains(name) && !name.equals(self))
                || POST_LOOKUP_GATES.contains(name) || GATE_CONDITIONS.contains(name);
            if (gate && anyInside(uses, n.end() - 1, text)) return true;
        }
        return false;
    }

    private static String blankDeclarations(String text) {
        Matcher d = FLAG_DECL.matcher(text);
        StringBuilder sb = new StringBuilder(text);
        while (d.find()) for (int i = d.start(); i < d.end(); i++) sb.setCharAt(i, ' ');
        return sb.toString();
    }

    /** True when a flag occurrence lies inside the parentheses opened at {@code open}. */
    private static boolean anyInside(List<int[]> uses, int open, String text) {
        int close = closeOf(text, open);
        for (int[] use : uses) if (use[0] > open && use[1] <= close) return true;
        return false;
    }

    // ── the flag at the handler end ───────────────────────────────────────────

    private static final Pattern PARSE_CALL = Pattern.compile("\\boptExcludeSensitive\\s*\\(");

    /** The variables a member assigns from a call of {@code parseCallee} (a method name, qualified or not). */
    static Set<String> variablesAssignedFrom(String blankedMemberText, String parseCallee) {
        Set<String> vars = new LinkedHashSet<>();
        Matcher a = Pattern.compile("(\\w+)\\s*=\\s*(?:\\w+\\s*\\.\\s*)*" + Pattern.quote(parseCallee) + "\\s*\\(")
            .matcher(blankedMemberText);
        while (a.find()) vars.add(a.group(1));
        return vars;
    }

    /** True when one call's argument list holds the parse itself or a variable the member assigned from it. */
    static boolean carriesParsedFlag(String args, Set<String> parsedVars, Pattern inlineParse) {
        if (inlineParse.matcher(args).find()) return true;
        for (String v : parsedVars) {
            if (Pattern.compile("\\b" + Pattern.quote(v) + "\\b").matcher(args).find()) return true;
        }
        return false;
    }

    /**
     * For a handler outside the vector census (the catalog by-address handlers): true when the member contains at
     * least one {@code repo.<repoMethod>(...)} call and EVERY such call carries, in its own argument list, a variable
     * the member assigned from {@code parseCallee(...)} or the parse itself.
     */
    static boolean everyRepoCallCarriesTheParsedFlag(String blankedMemberText, String repoMethod, String parseCallee) {
        Set<String> vars = variablesAssignedFrom(blankedMemberText, parseCallee);
        Pattern inline = Pattern.compile("\\b" + Pattern.quote(parseCallee) + "\\s*\\(");
        Matcher r = Pattern.compile("\\brepo\\s*\\.\\s*" + Pattern.quote(repoMethod) + "\\s*\\(").matcher(blankedMemberText);
        int calls = 0;
        while (r.find()) {
            calls++;
            if (!carriesParsedFlag(balanced(blankedMemberText, r.end() - 1), vars, inline)) return false;
        }
        return calls > 0;
    }

    /** The text between the parenthesis at {@code open} and its match (the rest of the text if unbalanced). */
    private static String balanced(String s, int open) {
        return s.substring(open + 1, closeOf(s, open));
    }

    /** The index of the parenthesis matching the one at {@code open} (the end of the text if unbalanced). */
    private static int closeOf(String s, int open) {
        int depth = 0;
        for (int i = open; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '(') depth++;
            else if (c == ')' && --depth == 0) return i;
        }
        return s.length();
    }

    private static Map<String, List<Member>> byName(List<Member> members) {
        Map<String, List<Member>> out = new LinkedHashMap<>();
        for (Member m : members) {
            if (!m.nestedType()) out.computeIfAbsent(m.name(), k -> new ArrayList<>()).add(m);
        }
        return out;
    }

    /** {@code route path -> handler method} from the dispatch {@code case} lines, in source order. */
    static Map<String, String> dispatch(String rawSource) {
        Map<String, String> out = new LinkedHashMap<>();
        Matcher m = CASE.matcher(rawSource);
        while (m.find()) out.put(m.group(1), m.group(2));
        return out;
    }

    // ── members of a top-level class ──────────────────────────────────────────

    /**
     * The members declared directly in the top-level class body of an already-blanked source: methods and
     * constructors by name, nested types by {@code <type Name>}, anything without a parameter list (field
     * initialisers, static blocks) as {@code <init>}. Member text is its header plus its body.
     */
    static List<Member> membersOf(String rawSource) {
        return members(KeyedRoutesCensusTest.blank(rawSource), blankComments(rawSource));
    }

    /** Name of a brace-less field with an initialiser, such as a SQL constant. */
    static final String FIELD = "<field>";

    /**
     * {@code rawSource} with its comments blanked and its string literals and text blocks kept (same length, so an
     * offset in it is an offset in {@code KeyedRoutesCensusTest.blank(rawSource)}). A {@code //} or a quote inside a
     * literal is not mistaken for a comment or a literal start.
     */
    static String blankComments(String src) {
        char[] out = src.toCharArray();
        int n = out.length;
        int i = 0;
        while (i < n) {
            char c = out[i];
            if (c == '/' && i + 1 < n && out[i + 1] == '/') {
                while (i < n && out[i] != '\n') out[i++] = ' ';
            } else if (c == '/' && i + 1 < n && out[i + 1] == '*') {
                while (i < n && !(out[i] == '*' && i + 1 < n && out[i + 1] == '/')) {
                    if (out[i] != '\n') out[i] = ' ';
                    i++;
                }
                if (i < n) out[i++] = ' ';
                if (i < n) out[i++] = ' ';
            } else if (c == '"' && i + 2 < n && out[i + 1] == '"' && out[i + 2] == '"') {
                i += 3;
                while (i + 2 < n && !(out[i] == '"' && out[i + 1] == '"' && out[i + 2] == '"')) i++;
                i += 3;
            } else if (c == '"' || c == '\'') {
                char q = c;
                i++;
                while (i < n && out[i] != q) {
                    if (out[i] == '\\' && i + 1 < n) i++;
                    i++;
                }
                i++;
            } else {
                i++;
            }
        }
        return new String(out);
    }

    static List<Member> members(String blanked) {
        return members(blanked, blanked);
    }

    static List<Member> members(String blanked, String commentFree) {
        int classBrace = -1;
        Matcher top = TOP_CLASS.matcher(blanked);
        while (top.find()) {
            int brace = blanked.indexOf('{', top.end());
            if (brace >= 0) { classBrace = brace; break; }
        }
        if (classBrace < 0) return List.of();   // package-info.java, a module descriptor: no members
        List<Member> out = new ArrayList<>();
        int depth = 0;
        int paren = 0;   // parentheses open at class-body level: an annotation's array braces sit inside them
        int headerStart = classBrace + 1;
        int memberOpen = -1;
        for (int i = classBrace + 1; i < blanked.length(); i++) {
            char c = blanked.charAt(i);
            if (depth == 0 && c == '(') {
                paren++;
            } else if (depth == 0 && c == ')') {
                paren = Math.max(0, paren - 1);
            } else if ((c == '{' || c == '}') && depth == 0 && paren > 0) {
                continue;
            } else if (c == '{') {
                if (depth == 0) memberOpen = i;
                depth++;
            } else if (c == '}') {
                if (depth == 0) break;   // end of the class body
                depth--;
                if (depth == 0) {
                    out.add(member(blanked.substring(headerStart, memberOpen), blanked.substring(memberOpen, i + 1),
                                   commentFree.substring(headerStart, i + 1)));
                    headerStart = i + 1;
                }
            } else if (c == ';' && depth == 0 && paren == 0) {
                String seg = stripAnnotations(blanked.substring(headerStart, i + 1));
                int eq = seg.indexOf('=');
                int par = seg.indexOf('(');
                if (eq >= 0 && (par < 0 || eq < par)) {   // a field with an initialiser, not a call statement
                    out.add(new Member(FIELD, blanked.substring(headerStart, i + 1), false,
                                       commentFree.substring(headerStart, i + 1)));
                }
                headerStart = i + 1;
            }
        }
        return out;
    }

    /**
     * {@code text} with every annotation, its parenthesised arguments included, replaced by spaces of the same length
     * (nexus-rfvyy.7.17 r1 F6): an annotated field initialiser, {@code @SuppressWarnings("x") static final String SQL
     * = ...}, has a parenthesis before its {@code =} that is the annotation's, not a method's.
     */
    static String stripAnnotations(String text) {
        StringBuilder sb = new StringBuilder(text);
        Matcher a = ANNOTATION_START.matcher(text);
        int from = 0;
        while (a.find(from)) {
            int end = a.end();
            int j = end;
            while (j < text.length() && Character.isWhitespace(text.charAt(j))) j++;
            if (j < text.length() && text.charAt(j) == '(') end = closeOf(text, j) + 1;
            for (int i = a.start(); i < Math.min(end, sb.length()); i++) sb.setCharAt(i, ' ');
            from = Math.max(end, a.end());
            if (from >= text.length()) break;
        }
        return sb.toString();
    }

    private static Member member(String header, String body, String literalSpan) {
        String h = stripAnnotations(header);
        Matcher t = Pattern.compile("\\b(class|record|interface|enum)\\s+(\\w+)").matcher(h);
        if (t.find() && !h.contains("=")) {
            return new Member("<type " + t.group(2) + ">", header + body, true, literalSpan);
        }
        Matcher n = Pattern.compile("(\\w+)\\s*\\(").matcher(h);
        String name = (n.find() && !h.contains("=")) ? n.group(1) : "<init>";
        return new Member(name, header + body, false, literalSpan);
    }

    /**
     * Every member of a source that reads chunk text, as {@code Class#member}: used by the sink census over files
     * the route census does not follow. A read inside a nested type is reported against the type.
     */
    static Set<String> textReaders(String fileName, String rawSource, Set<String> textFunctions) {
        Set<String> out = new TreeSet<>();
        for (Member m : membersOf(rawSource)) {
            if (readsChunkText(m, textFunctions)) out.add(fileName + "#" + m.name());
        }
        return out;
    }

    /** The blanked text (header and body) of every member of a source named {@code name}. */
    static List<String> memberTexts(String rawSource, String name) {
        List<String> out = new ArrayList<>();
        for (Member m : membersOf(rawSource)) if (m.name().equals(name)) out.add(m.text());
        return out;
    }

    static boolean hasTypeKeyword(String s) { return TYPE_KEYWORD.matcher(s).find(); }
}
