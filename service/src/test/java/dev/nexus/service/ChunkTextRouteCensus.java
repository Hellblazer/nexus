// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

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
 *   <li>it selects the {@code chunk_text} column ({@code chunkText()} or {@code CHUNK_TEXT}), or</li>
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
 * "conforms" is a USE check, not a mention check: a text-reading repository method must read {@code excludeSensitive}
 * beyond declaring it, and the handler's parsed flag must sit inside a repository call's arguments. The route recipes
 * of {@code VectorSensitivityExcludeIntegrationTest} stay the backstop for what text matching cannot see.
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

    /** What the census found for one route. */
    record Finding(String route, String handler, Set<String> sinkMethods, boolean handlerParsesSensitivity,
                   Set<String> sinkMethodsWithoutFlag, Set<String> exemptSinksReached) {
        boolean readsChunkText() { return !sinkMethods.isEmpty(); }
        boolean conforms() { return !readsChunkText() || (handlerParsesSensitivity && sinkMethodsWithoutFlag.isEmpty()); }
    }

    private static final Pattern CASE = Pattern.compile("case\\s+\"(/[^\"]*)\"\\s*->\\s*(\\w+)\\s*\\(");
    private static final Pattern ANNOTATION = Pattern.compile("@\\w+(\\s*\\([^)]*\\))?");
    private static final Pattern TYPE_KEYWORD = Pattern.compile("\\b(class|record|interface|enum)\\b");
    private static final Pattern TOP_CLASS = Pattern.compile("\\b(class|record|interface|enum)\\s+(\\w+)");
    private static final Pattern CHUNK_TEXT_READ = Pattern.compile("\\bchunkText\\s*\\(\\s*\\)|\\bCHUNK_TEXT\\b|\\btextPrefix\\s*\\(\\s*\\)");
    /** A raw-SQL read: the column named in a string literal or text block ({@code chunk_text_hash} is another column). */
    private static final Pattern RAW_CHUNK_TEXT = Pattern.compile("(?i)\\bchunk_text\\b");
    private static final Pattern CONSTANT_CALL = Pattern.compile("\\b([A-Z][A-Z0-9_]+)\\s*\\.\\s*call\\s*\\(");
    private static final Pattern CALL = Pattern.compile("(?<![\\w.])(\\w+)\\s*\\(");
    private static final Pattern METHOD_REF = Pattern.compile("\\bthis\\s*::\\s*(\\w+)");
    private static final Pattern REPO_CALL = Pattern.compile("\\brepo\\s*\\.\\s*(\\w+)\\s*\\(");
    /** The flag a text-reading repository method must carry, as parameter or as {@code wherePlan.excludeSensitive()}. */
    static final String FLAG = "excludeSensitive";

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
            boolean parses = false;
            for (String h : handlerReach) {
                for (Member m : handlerByName.get(h)) {
                    if (!h.equals("optExcludeSensitive")
                            && parsedFlagReachesTheRepository(m.text(), handlerByName.keySet())) {
                        parses = true;
                    }
                }
            }
            Set<String> unflagged = new TreeSet<>();
            for (String s : reached) {
                boolean flagged = false;
                for (Member m : repoByName.get(s)) {
                    if (readsChunkText(m, textFunctions) && usesFlag(m.text())) flagged = true;
                }
                if (!flagged) unflagged.add(s);
            }
            out.add(new Finding(route, entry, reached, parses, unflagged, exemptReached));
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

    static boolean readsChunkText(Member m, Set<String> textFunctions) {
        if (CHUNK_TEXT_READ.matcher(m.text()).find()) return true;
        Matcher c = CONSTANT_CALL.matcher(m.text());
        while (c.find()) {
            if (textFunctions.contains(c.group(1).toLowerCase(java.util.Locale.ROOT))) return true;
        }
        // raw SQL: the column, or a call of one of the text functions, written inside a string literal
        if (RAW_CHUNK_TEXT.matcher(m.literalText()).find()) return true;
        for (String fn : textFunctions) {
            if (Pattern.compile("(?i)\\b" + Pattern.quote(fn) + "\\s*\\(").matcher(m.literalText()).find()) return true;
        }
        return false;
    }

    /**
     * True when the text of a member USES the flag, not merely mentions it: the parameter declaration is taken out
     * first, so a method that declares {@code boolean excludeSensitive} and never reads it does not pass, while one
     * that reads {@code wherePlan.excludeSensitive()} (no parameter) does.
     */
    static boolean usesFlag(String blankedMemberText) {
        String noDecl = Pattern.compile("\\b(?:boolean|Boolean)\\s+" + FLAG + "\\b")
            .matcher(blankedMemberText).replaceAll(" ");
        return Pattern.compile("\\b" + FLAG + "\\b").matcher(noDecl).find();
    }

    private static final Pattern PARSE_CALL = Pattern.compile("\\boptExcludeSensitive\\s*\\(");
    private static final Pattern PARSE_ASSIGN = Pattern.compile("(\\w+)\\s*=\\s*optExcludeSensitive\\s*\\(");
    private static final Pattern REPO_CALL_OPEN = Pattern.compile("\\brepo\\s*\\.\\s*\\w+\\s*\\(");
    private static final Pattern OWN_CALL_OPEN = Pattern.compile("(?<![\\w.])(\\w+)\\s*\\(");

    /**
     * True when the member parses the flag with {@code optExcludeSensitive(...)} and what it parsed reaches a
     * repository call: the variable (or the inline parse) is inside the argument list of a {@code repo.x(...)} call,
     * or of a call to another handler method that carries it on. A parse whose result is dropped does not count.
     */
    static boolean parsedFlagReachesTheRepository(String blankedMemberText, Set<String> handlerMethods) {
        if (!PARSE_CALL.matcher(blankedMemberText).find()) return false;
        Set<String> vars = new LinkedHashSet<>();
        Matcher a = PARSE_ASSIGN.matcher(blankedMemberText);
        while (a.find()) vars.add(a.group(1));
        List<String> argLists = new ArrayList<>();
        Matcher r = REPO_CALL_OPEN.matcher(blankedMemberText);
        while (r.find()) argLists.add(balanced(blankedMemberText, r.end() - 1));
        Matcher o = OWN_CALL_OPEN.matcher(blankedMemberText);
        while (o.find()) {
            if (handlerMethods.contains(o.group(1)) && !o.group(1).equals("optExcludeSensitive")) {
                argLists.add(balanced(blankedMemberText, o.end() - 1));
            }
        }
        for (String args : argLists) {
            if (PARSE_CALL.matcher(args).find()) return true;
            for (String v : vars) {
                if (Pattern.compile("\\b" + Pattern.quote(v) + "\\b").matcher(args).find()) return true;
            }
        }
        return false;
    }

    /** The text between the parenthesis at {@code open} and its match (the rest of the text if unbalanced). */
    private static String balanced(String s, int open) {
        int depth = 0;
        for (int i = open; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '(') depth++;
            else if (c == ')' && --depth == 0) return s.substring(open + 1, i);
        }
        return s.substring(open + 1);
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
        int headerStart = classBrace + 1;
        int memberOpen = -1;
        for (int i = classBrace + 1; i < blanked.length(); i++) {
            char c = blanked.charAt(i);
            if (c == '{') {
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
            } else if (c == ';' && depth == 0) {
                String seg = blanked.substring(headerStart, i + 1);
                int eq = seg.indexOf('=');
                int paren = seg.indexOf('(');
                if (eq >= 0 && (paren < 0 || eq < paren)) {   // a field with an initialiser, not a call statement
                    out.add(new Member(FIELD, seg, false, commentFree.substring(headerStart, i + 1)));
                }
                headerStart = i + 1;
            }
        }
        return out;
    }

    private static Member member(String header, String body, String literalSpan) {
        String h = ANNOTATION.matcher(header).replaceAll(" ");
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
