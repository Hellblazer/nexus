// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.TreeSet;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Two source-scanning censuses over the Java test tree (nexus-dscbd, follow-up to nexus-7xfh7). No database.
 *
 * <p><b>Role census.</b> On the per-fork shared cluster a role outlives the class that made it, and the
 * role-creating helpers are {@code IF NOT EXISTS}: a second class that creates the same fixed name silently keeps
 * the first creator's password and attributes. So every role a test source creates must be one of: used by exactly
 * one test class; used by several, each of which declares it exclusive in {@code startEmptyDatabase(...)} (absent at
 * acquire, dropped at stop); or a member of a named family allowlisted below with its reason. The creating calls are
 * {@code bootstrapNonSuperuserOwner}, {@code bootstrapAdminRole}, {@code bootstrapServiceRole} (also a test-local
 * wrapper of the same name) and the bare-role helpers {@code createLoginRole}, {@code createNologinRole},
 * {@code createNologinRoleWithPassword}, {@code createSuperuserLoginRole}. The role argument is resolved through
 * string literals, {@code String} constants and locals of the same file, and {@code Class.CONSTANT} of another test
 * class, joined by {@code +}; a part that cannot be resolved (a method parameter, say) becomes {@code *}. A role that
 * resolves to nothing but {@code *} fails: the census cannot attribute it. Calls spanning lines are parsed, and a
 * call named only in a comment, a string or a method reference is not counted (a method reference to a creating
 * helper fails, since it hides the role).
 *
 * <p>The migration fixes some names ({@code nexus_diag}): a creator of one of {@link #RESERVED} must declare it
 * exclusive or take a dedicated container ({@code startDedicated} / {@code newContainer}), even when it is the only
 * class that uses the name, because the template migration may already hold it.
 *
 * <p>Bare {@code CREATE ROLE} statements: the raw-SQL gates keep SQL out of Java tests, so a role is created only
 * through the helpers above. A string literal that holds such a statement is reported unless its file is in
 * {@link #BARE_CREATE_ROLE_FILES}.
 *
 * <p>What it does NOT check: exclusivity is read per class, not per test method (a class that declares the role in
 * one method and creates it in another passes); an exclusive declaration is matched by name, so a role declared
 * exclusive but never dropped by a path outside {@code stop()} is not found; and a family's passing proves only
 * that every user derives the same password from the role name.
 *
 * <p><b>Roster ratchet.</b> The callers of {@code PgContainerHelper.startDedicated} (both overloads, direct calls and
 * method references) must equal the roster in that method's javadoc plus {@link #INFRA_CALLERS}, in both directions,
 * so the cut in container boots (141 to 14 in nexus-7xfh7) cannot regress silently. A static import of an entry point
 * fails, as it hides the call.
 *
 * <p>Each check is falsified on string fixtures further down: a planted second class on a non-exclusive role, a
 * new {@code startDedicated} caller, a roster name with no caller.
 */
class TestRoleUniquenessScanTest {

    private static final Path TEST_ROOT = Path.of("src", "test", "java");
    private static final Path HELPER = TEST_ROOT.resolve(Path.of("dev", "nexus", "service", "PgContainerHelper.java"));

    /** The plumbing that defines the entry points and role helpers, and this file (its fixtures name them). */
    private static final Set<String> NOT_SCANNED = Set.of(
        "PgContainerHelper.java", "SharedCluster.java", "SharedDatabaseHandle.java", "TestRoleUniquenessScanTest.java");

    /** Names the template migration may already hold: a creator declares them exclusive or takes its own container. */
    private static final Set<String> RESERVED = Set.of("nexus_diag");

    /** A family of roles built from a per-test label. Key is the resolved pattern, {@code *} standing for the label;
     *  {@code labelCall} names the method whose string-literal arguments are the labels. */
    record Family(Set<String> classes, String labelCall, String reason) {}

    /**
     * {@code nexus_admin_<label>}: the three Vectors rigs name the migrating role after the test's label. Labels
     * repeat across VectorsUnifyChunksIntegrationTest and VectorsUnifyCentroidsIntegrationTest ("fresh", "straddle",
     * "degenerate", "collision", "checkenforce", "hnswfull", "rollback"), and across tests of one class. It passes
     * without per-label uniqueness because the role is created by the one helper ({@code bootstrapNonSuperuserOwner},
     * so identical attributes) with the password {@code <role>_pass}, derived from the role name in every user (the
     * scan verifies that formula), so the first creator's password IS every later class's password and the
     * {@code IF NOT EXISTS} keep-first is a no-op. The scan also fails if any other class resolves to a name this
     * family covers.
     */
    private static final Map<String, Family> FAMILIES = Map.of(
        "nexus_admin_*", new Family(
            Set.of("VectorsUnifyChunksIntegrationTest", "VectorsUnifyCentroidsIntegrationTest",
                "VectorsRepointFunctionsIntegrationTest"),
            "newRig",
            "same helper, password derived from the role name in every user, so a shared name is the same role"));

    /** Files that hold a bare create-role statement in a string literal, each with why. */
    private static final Map<String, String> BARE_CREATE_ROLE_FILES = Map.of(
        "RawSqlGateTest", "a changelog fixture the gate parses; never sent to a database",
        "DdlLogParserTest", "a server-log line the parser under test reads; never sent to a database");

    /** Callers of startDedicated that the javadoc roster does not list: tests of the container factory and its log. */
    private static final Map<String, String> INFRA_CALLERS = Map.of(
        "PgServerLogTest", "tests the server-log reader against containers whose log it controls",
        "DdlCountSwitchTest", "tests the DDL counter's switch on a dedicated container");

    /** Floors, from the live count of 2026-10-21 (530 classes, 263 call sites, 245 roles, 10 startDedicated callers, 8 roster names) minus a small margin; the scan must not shrink silently. */
    private static final int MIN_FILES = 500;
    private static final int MIN_CALL_SITES = 250;
    private static final int MIN_ROLES = 235;
    private static final int MIN_START_DEDICATED_CALLERS = 9;
    private static final int MIN_JAVADOC_ROSTER = 7;

    // ── lexing ────────────────────────────────────────────────────────────────────────────────────────────────

    /** Group 1: a string, text block or char literal; group 2: a comment. Possessive unrolled loops, so the matcher
     *  recurses once per escape, not per character. */
    private static final Pattern LEX = Pattern.compile(
        "(\"\"\"[^\"\\\\]*+(?:(?:\\\\.|\"(?!\"\"))[^\"\\\\]*+)*+\"\"\"|\"[^\"\\\\\\n]*+(?:\\\\.[^\"\\\\\\n]*+)*+\""
            + "|'(?:\\\\.|[^'\\\\])')|(//[^\\n]*|/\\*.*?\\*/)",
        Pattern.DOTALL);

    /** The source with comments and, unless {@code keepLiterals}, literal contents turned to spaces. Same length
     *  as the input, so an offset found in one form addresses the other. */
    static String mask(String src, boolean keepLiterals) {
        StringBuilder out = new StringBuilder(src.length());
        Matcher m = LEX.matcher(src);
        int last = 0;
        while (m.find()) {
            out.append(src, last, m.start());
            String piece = m.group();
            if (m.group(2) != null || !keepLiterals) {
                for (int i = 0; i < piece.length(); i++) {
                    out.append(piece.charAt(i) == '\n' ? '\n' : ' ');
                }
            } else {
                out.append(piece);
            }
            last = m.end();
        }
        return out.append(src, last, src.length()).toString();
    }

    /** Index after the literal that starts at {@code i}. */
    private static int skipLiteral(String s, int i) {
        char q = s.charAt(i);
        if (q == '"' && s.startsWith("\"\"\"", i)) {
            int j = i + 3;
            while (j < s.length()) {
                if (s.charAt(j) == '\\') {
                    j += 2;
                } else if (s.startsWith("\"\"\"", j)) {
                    return j + 3;
                } else {
                    j++;
                }
            }
            return s.length();
        }
        int j = i + 1;
        while (j < s.length()) {
            char c = s.charAt(j);
            if (c == '\\') {
                j += 2;
            } else if (c == q) {
                return j + 1;
            } else {
                j++;
            }
        }
        return s.length();
    }

    private static boolean isOpen(char c) {
        return c == '(' || c == '[' || c == '{';
    }

    private static boolean isClose(char c) {
        return c == ')' || c == ']' || c == '}';
    }

    /** Pieces of {@code s} between top-level occurrences of {@code sep}, outside literals and brackets. */
    static List<String> splitTop(String s, char sep) {
        List<String> parts = new ArrayList<>();
        int depth = 0;
        int start = 0;
        int i = 0;
        while (i < s.length()) {
            char c = s.charAt(i);
            if (c == '"' || c == '\'') {
                i = skipLiteral(s, i);
                continue;
            }
            if (isOpen(c)) {
                depth++;
            } else if (isClose(c)) {
                depth--;
            } else if (c == sep && depth == 0) {
                parts.add(s.substring(start, i).strip());
                start = i + 1;
            }
            i++;
        }
        parts.add(s.substring(start).strip());
        return parts;
    }

    /** The top-level arguments of the call whose opening parenthesis is at {@code open}; empty for {@code ()}. */
    static List<String> argsAt(String lit, int open) {
        List<String> args = new ArrayList<>();
        int depth = 0;
        int start = open + 1;
        int i = open + 1;
        while (i < lit.length()) {
            char c = lit.charAt(i);
            if (c == '"' || c == '\'') {
                i = skipLiteral(lit, i);
                continue;
            }
            if (isOpen(c)) {
                depth++;
            } else if (isClose(c)) {
                if (depth == 0) {
                    String last = lit.substring(start, i).strip();
                    if (!last.isEmpty() || !args.isEmpty()) {
                        args.add(last);
                    }
                    return args;
                }
                depth--;
            } else if (c == ',' && depth == 0) {
                args.add(lit.substring(start, i).strip());
                start = i + 1;
            }
            i++;
        }
        throw new IllegalStateException("unbalanced call at offset " + open);
    }

    /** The text from {@code from} to the next top-level semicolon. */
    private static String untilSemicolon(String lit, int from) {
        int depth = 0;
        int i = from;
        while (i < lit.length()) {
            char c = lit.charAt(i);
            if (c == '"' || c == '\'') {
                i = skipLiteral(lit, i);
                continue;
            }
            if (isOpen(c)) {
                depth++;
            } else if (isClose(c)) {
                depth--;
            } else if (c == ';' && depth <= 0) {
                break;
            }
            i++;
        }
        return lit.substring(from, Math.min(i, lit.length())).strip();
    }

    // ── one source file ────────────────────────────────────────────────────────────────────────────────────────

    /** A test source: {@code lit} has comments gone and literals kept; {@code code} has both gone. */
    static final class Src {
        final String name;
        final String raw;
        final String lit;
        final String code;
        private Map<String, List<String>> inits;

        Src(String name, String raw) {
            this.name = name;
            this.raw = raw;
            this.lit = mask(raw, true);
            this.code = mask(raw, false);
        }

        private static final Pattern STRING_DECL = Pattern.compile("\\bString\\s+(\\w+)\\s*=");

        /** Initializer expressions of every {@code String name = ...;} in the file, by name. */
        Map<String, List<String>> inits() {
            if (inits == null) {
                inits = new TreeMap<>();
                Matcher m = STRING_DECL.matcher(code);
                while (m.find()) {
                    inits.computeIfAbsent(m.group(1), k -> new ArrayList<>()).add(untilSemicolon(lit, m.end()));
                }
            }
            return inits;
        }
    }

    private static final Pattern IDENT = Pattern.compile("[A-Za-z_]\\w*");
    private static final Pattern QUALIFIED = Pattern.compile("([A-Za-z_]\\w*)\\s*\\.\\s*([A-Za-z_]\\w*)");
    private static final Pattern PLAIN_LITERAL = Pattern.compile("\"([^\"\\\\]*)\"");

    /** The values {@code expr} can take; {@code *} stands for a part the scan cannot resolve. */
    static Set<String> eval(String expr, Src s, Map<String, Src> all, int depth) {
        if (depth > 8) {
            return Set.of("*");
        }
        Set<String> acc = Set.of("");
        for (String term : splitTop(expr, '+')) {
            Set<String> opts = term(term, s, all, depth);
            Set<String> next = new TreeSet<>();
            for (String a : acc) {
                for (String o : opts) {
                    next.add((a + o).replaceAll("\\*+", "*"));
                }
            }
            acc = next;
        }
        return acc;
    }

    private static Set<String> term(String t, Src s, Map<String, Src> all, int depth) {
        t = t.strip();
        if (t.length() > 1 && t.startsWith("(") && t.endsWith(")") && splitTop(t.substring(1, t.length() - 1), '+').size() >= 1
            && splitTop(t, '+').size() == 1) {
            return eval(t.substring(1, t.length() - 1), s, all, depth + 1);
        }
        Matcher lit = PLAIN_LITERAL.matcher(t);
        if (lit.matches()) {
            return Set.of(lit.group(1));
        }
        if (IDENT.matcher(t).matches()) {
            return fromInits(s.inits().get(t), s, all, depth);
        }
        Matcher q = QUALIFIED.matcher(t);
        if (q.matches()) {
            Src other = all.get(q.group(1));
            return other == null ? Set.of("*") : fromInits(other.inits().get(q.group(2)), other, all, depth);
        }
        return Set.of("*");
    }

    private static Set<String> fromInits(List<String> exprs, Src owner, Map<String, Src> all, int depth) {
        if (exprs == null) {
            return Set.of("*");
        }
        Set<String> out = new TreeSet<>();
        for (String e : exprs) {
            out.addAll(eval(e, owner, all, depth + 1));
        }
        return out;
    }

    // ── the role census ────────────────────────────────────────────────────────────────────────────────────────

    private static final Pattern ROLE_CALL = Pattern.compile(
        "\\b(bootstrapNonSuperuserOwner|bootstrapAdminRole|bootstrapServiceRole|createLoginRole"
            + "|createNologinRoleWithPassword|createNologinRole|createSuperuserLoginRole)\\s*\\(");
    private static final Pattern ROLE_METHOD_REF = Pattern.compile(
        "::\\s*(bootstrapNonSuperuserOwner|bootstrapAdminRole|bootstrapServiceRole|createLoginRole"
            + "|createNologinRoleWithPassword|createNologinRole|createSuperuserLoginRole)\\b");
    private static final Pattern START_EMPTY = Pattern.compile("\\bstartEmptyDatabase\\s*\\(");
    private static final Pattern DEDICATED = Pattern.compile(
        "\\bPgContainerHelper\\s*(?:\\.|::)\\s*(?:startDedicated|newContainer)\\b");
    /** A parameter declaration such as {@code String role}: the call-shaped text is a method declaration. */
    private static final Pattern DECLARATION =
        Pattern.compile("(?:final\\s+)?[A-Za-z_][\\w.<>?\\[\\]]*\\s+([A-Za-z_]\\w*)");
    private static final Pattern BARE_CREATE_ROLE = Pattern.compile("(?i)create\\s+" + "role\\b");

    /** One role-creating call: the class, the helper, the role values it can take and the password values. */
    record Use(String clazz, String helper, Set<String> roles, Set<String> passwords) {}

    record Census(List<Use> uses, Map<String, Set<String>> exclusive, Set<String> dedicated, List<String> problems) {}

    static Census collect(Map<String, Src> all, Set<String> scanned) {
        List<Use> uses = new ArrayList<>();
        Map<String, Set<String>> exclusive = new TreeMap<>();
        Set<String> dedicated = new TreeSet<>();
        List<String> problems = new ArrayList<>();
        for (String name : new TreeSet<>(scanned)) {
            Src s = all.get(name);
            Matcher mr = ROLE_METHOD_REF.matcher(s.code);
            while (mr.find()) {
                problems.add(name + ": a method reference to " + mr.group(1) + " hides the role; call it directly");
            }
            Matcher m = ROLE_CALL.matcher(s.code);
            while (m.find()) {
                String helper = m.group(1);
                List<String> args = argsAt(s.lit, m.end() - 1);
                if (args.size() < 2) {
                    problems.add(name + ": " + helper + " call with fewer than two arguments, cannot read the role");
                    continue;
                }
                if (DECLARATION.matcher(args.get(1)).matches()) {
                    continue; // the declaration of a test-local wrapper such as Hygiene001's bootstrapAdminRole
                }
                Set<String> roles = new TreeSet<>(eval(args.get(1), s, all, 0));
                Set<String> passwords = args.size() > 2 && !helper.equals("createNologinRole")
                    ? eval(args.get(2), s, all, 0) : Set.of();
                if (roles.contains("*") && roles.size() == 1 && IDENT.matcher(args.get(1)).matches()) {
                    roles = new TreeSet<>(throughEnclosingMethod(s, all, m.start(), args.get(1), problems, helper));
                }
                if (!roles.isEmpty()) {
                    uses.add(new Use(name, helper, roles, passwords));
                }
            }
            Matcher se = START_EMPTY.matcher(s.code);
            while (se.find()) {
                for (String arg : argsAt(s.lit, se.end() - 1)) {
                    for (String role : eval(arg, s, all, 0)) {
                        if (!role.contains("*")) {
                            exclusive.computeIfAbsent(name, k -> new TreeSet<>()).add(role);
                        }
                    }
                }
            }
            if (DEDICATED.matcher(s.code).find()) {
                dedicated.add(name);
            }
        }
        return new Census(uses, exclusive, dedicated, problems);
    }

    private static final Pattern METHOD_DECL = Pattern.compile(
        "\\b([A-Za-z_]\\w*)\\s*\\(([^()]*)\\)\\s*(?:throws\\s+[\\w.,\\s]+?)?\\s*\\{");
    private static final Set<String> NOT_A_METHOD = Set.of("if", "for", "while", "switch", "catch", "synchronized", "try");

    /**
     * A role argument that is a parameter of the method the call sits in (a test-local {@code makeRole(String role)}):
     * the role values are whatever that method's callers in the same file pass. A wrapper named like a creating
     * helper is skipped, its callers being counted at their own call. Returns no roles, and records a problem, when
     * the parameter cannot be traced.
     */
    private static Set<String> throughEnclosingMethod(Src s, Map<String, Src> all, int callAt, String ident,
                                                      List<String> problems, String helper) {
        Matcher d = METHOD_DECL.matcher(s.code);
        String method = null;
        String params = null;
        int declAt = -1;
        while (d.find() && d.start() < callAt) {
            if (!NOT_A_METHOD.contains(d.group(1))) {
                method = d.group(1);
                params = d.group(2);
                declAt = d.start();
            }
        }
        if (method == null) {
            problems.add(s.name + ": " + helper + " role argument '" + ident + "' cannot be resolved");
            return Set.of();
        }
        List<String> ps = splitTop(params, ',');
        int index = -1;
        for (int i = 0; i < ps.size(); i++) {
            Matcher pm = DECLARATION.matcher(ps.get(i));
            if (pm.matches() && pm.group(1).equals(ident)) {
                index = i;
            }
        }
        if (index < 0) {
            problems.add(s.name + ": " + helper + " role argument '" + ident + "' cannot be resolved");
            return Set.of();
        }
        if (ROLE_CALL.matcher(method + "(").find()) {
            return Set.of(); // a wrapper named like a creating helper: counted where it is called
        }
        Set<String> roles = new TreeSet<>();
        Matcher c = Pattern.compile("\\b" + Pattern.quote(method) + "\\s*\\(").matcher(s.code);
        while (c.find()) {
            if (c.start() == declAt) {
                continue;
            }
            List<String> args = argsAt(s.lit, c.end() - 1);
            if (args.size() > index) {
                roles.addAll(eval(args.get(index), s, all, 0));
            }
        }
        if (roles.isEmpty() || roles.equals(Set.of("*"))) {
            problems.add(s.name + ": " + helper + " role argument '" + ident + "' is a parameter of " + method
                + "(...) whose callers do not pass a resolvable role");
            return Set.of();
        }
        return roles;
    }

    /** Problems in the role census of {@code scanned} (plus the literals check over every scanned file). */
    static List<String> roleProblems(Map<String, Src> all, Set<String> scanned, Map<String, Family> families,
                                     Set<String> reserved, Map<String, String> bareAllowed,
                                     Map<String, Set<String>> rolesOut, int[] callSitesOut) {
        Census c = collect(all, scanned);
        List<String> problems = new ArrayList<>(c.problems());
        Map<String, Set<String>> exactRoles = new TreeMap<>();
        Map<String, List<Use>> familyUses = new TreeMap<>();
        for (Use u : c.uses()) {
            for (String role : u.roles()) {
                if (role.contains("*")) {
                    familyUses.computeIfAbsent(role, k -> new ArrayList<>()).add(u);
                } else {
                    exactRoles.computeIfAbsent(role, k -> new TreeSet<>()).add(u.clazz());
                }
            }
        }
        Map<String, Set<String>> familyNames = new TreeMap<>();
        for (var f : families.entrySet()) {
            for (String cls : f.getValue().classes()) {
                Src fs = all.get(cls);
                if (fs == null || f.getValue().labelCall() == null) {
                    continue;
                }
                Matcher lc = Pattern.compile("\\b" + Pattern.quote(f.getValue().labelCall()) + "\\s*\\(").matcher(fs.code);
                while (lc.find()) {
                    for (String arg : argsAt(fs.lit, lc.end() - 1)) {
                        Matcher lit = PLAIN_LITERAL.matcher(arg);
                        if (lit.matches()) {
                            familyNames.computeIfAbsent(f.getKey(), k -> new TreeSet<>())
                                .add(f.getKey().replace("*", lit.group(1)));
                        }
                    }
                }
            }
        }
        // A class that declares a role exclusive creates it, if not through a helper this scan reads (a changelog
        // parameter, say: Taxonomy011ForeignOwnedDiagViewTest), so a declaration counts as a use.
        c.exclusive().forEach((cls, declared) -> declared.forEach(
            role -> exactRoles.computeIfAbsent(role, k -> new TreeSet<>()).add(cls)));
        for (var e : exactRoles.entrySet()) {
            String role = e.getKey();
            Set<String> users = e.getValue();
            if (reserved.contains(role)) {
                for (String cls : users) {
                    if (!c.exclusive().getOrDefault(cls, Set.of()).contains(role) && !c.dedicated().contains(cls)) {
                        problems.add(cls + ": creates " + role + ", a name the migration fixes, but neither declares it "
                            + "exclusive in startEmptyDatabase(...) nor takes a dedicated container");
                    }
                }
            }
            if (users.size() > 1) {
                List<String> undeclared = new ArrayList<>();
                for (String cls : users) {
                    if (!c.exclusive().getOrDefault(cls, Set.of()).contains(role)) {
                        undeclared.add(cls);
                    }
                }
                if (!undeclared.isEmpty()) {
                    problems.add("role " + role + " is created by " + users + " and " + undeclared + " do not declare it "
                        + "exclusive: on the shared cluster the first creator's password and attributes win. Give each "
                        + "class its own name, or declare it in startEmptyDatabase(...) in every one");
                }
            }
            for (var f : families.entrySet()) {
                if (familyNames.getOrDefault(f.getKey(), Set.of()).contains(role)) {
                    for (String cls : users) {
                        if (!f.getValue().classes().contains(cls)) {
                            problems.add(cls + ": creates " + role + ", a name family " + f.getKey()
                                + " builds, which only " + new TreeSet<>(f.getValue().classes()) + " may use");
                        }
                    }
                }
            }
        }
        for (var e : familyUses.entrySet()) {
            Family f = families.get(e.getKey());
            if (f == null) {
                problems.add("role pattern " + e.getKey() + " (a part is built at run time) used by "
                    + e.getValue().stream().map(Use::clazz).collect(java.util.stream.Collectors.toCollection(TreeSet::new))
                    + " is not an allowlisted family; make the name a literal or add the family with a reason");
                continue;
            }
            Set<String> users = new TreeSet<>();
            for (Use u : e.getValue()) {
                users.add(u.clazz());
                if (!u.passwords().equals(Set.of(e.getKey() + "_pass"))) {
                    problems.add(u.clazz() + ": family " + e.getKey() + " needs the password <role>_pass, got "
                        + u.passwords());
                }
            }
            if (!users.equals(f.classes())) {
                problems.add("family " + e.getKey() + " is used by " + users + " but allowlisted for " + new TreeSet<>(f.classes()));
            }
        }
        for (String key : families.keySet()) {
            if (!familyUses.containsKey(key)) {
                problems.add("family " + key + " is allowlisted but no scanned class builds a role of that shape; remove it");
            }
        }
        for (String name : new TreeSet<>(scanned)) {
            Matcher lm = LEX.matcher(all.get(name).raw);
            while (lm.find()) {
                if (lm.group(1) != null && BARE_CREATE_ROLE.matcher(lm.group(1)).find() && !bareAllowed.containsKey(name)) {
                    problems.add(name + ": a string literal holds a bare create-role statement, a role this census cannot "
                        + "attribute; use PgContainerHelper.createLoginRole / createNologinRole");
                    break;
                }
            }
        }
        if (rolesOut != null) {
            exactRoles.forEach((r, cl) -> rolesOut.put(r, cl));
            familyUses.forEach((r, us) -> rolesOut.put(r, us.stream().map(Use::clazz).collect(
                java.util.stream.Collectors.toCollection(TreeSet::new))));
        }
        if (callSitesOut != null) {
            callSitesOut[0] = c.uses().size();
        }
        return problems;
    }

    // ── the roster ratchet ─────────────────────────────────────────────────────────────────────────────────────

    private static final Pattern START_DEDICATED_CALL = Pattern.compile(
        "\\bPgContainerHelper\\s*(?:\\.\\s*startDedicated\\s*\\(|::\\s*startDedicated\\b)");
    private static final Pattern STATIC_IMPORT_DEDICATED = Pattern.compile(
        "import\\s+static\\s+[\\w.]*PgContainerHelper\\s*\\.\\s*(?:startDedicated|\\*)");
    private static final Pattern CODE_NAME = Pattern.compile("\\{@code\\s+([A-Z]\\w*)\\}");

    /** Simple names of the classes that call startDedicated, plus problems for a static import that hides one. */
    static Set<String> startDedicatedCallers(Map<String, Src> all, Set<String> scanned, List<String> problems) {
        Set<String> callers = new TreeSet<>();
        for (String name : scanned) {
            Src s = all.get(name);
            if (STATIC_IMPORT_DEDICATED.matcher(s.code).find()) {
                problems.add(name + ": a static import of PgContainerHelper hides startDedicated from this scan; qualify it");
            }
            if (START_DEDICATED_CALL.matcher(s.code).find()) {
                callers.add(name);
            }
        }
        return callers;
    }

    /** Class names the startDedicated javadoc lists in its roster, newContainer entries (another method) left out. */
    static Set<String> javadocRoster(String helperSource) {
        int at = helperSource.indexOf("public static PostgreSQLContainer<?> startDedicated() {");
        if (at < 0) {
            throw new IllegalStateException("PgContainerHelper.startDedicated() not found");
        }
        String doc = helperSource.substring(helperSource.lastIndexOf("/**", at), at);
        Set<String> names = new TreeSet<>();
        String[] items = doc.split("<li>");
        for (int i = 1; i < items.length; i++) {
            String item = items[i];
            int end = item.indexOf("</li>");
            if (end >= 0) {
                item = item.substring(0, end);
            }
            if (item.contains("newContainer")) {
                continue;
            }
            Matcher m = CODE_NAME.matcher(item);
            while (m.find()) {
                if (m.group(1).endsWith("Test")) {
                    names.add(m.group(1));
                }
            }
        }
        return names;
    }

    static List<String> rosterDiff(Set<String> callers, Set<String> roster, Map<String, String> infra) {
        Set<String> expected = new TreeSet<>(roster);
        expected.addAll(infra.keySet());
        List<String> problems = new ArrayList<>();
        for (String c : callers) {
            if (!expected.contains(c)) {
                problems.add(c + ": calls startDedicated but is not in its javadoc roster. If it needs cluster state, "
                    + "a sized container or its own server log, add it there with the reason; otherwise take "
                    + "startEmptyDatabase or a clone");
            }
        }
        for (String r : expected) {
            if (!callers.contains(r)) {
                problems.add(r + ": the roster names it but it no longer calls startDedicated; remove it from the "
                    + "javadoc (or INFRA_CALLERS)");
            }
        }
        return problems;
    }

    // ── the live scans ─────────────────────────────────────────────────────────────────────────────────────────

    private static Map<String, Src> readTree() throws IOException {
        Map<String, Src> all = new TreeMap<>();
        List<Path> files;
        try (Stream<Path> s = Files.walk(TEST_ROOT)) {
            files = s.filter(p -> p.toString().endsWith(".java")).sorted().toList();
        }
        for (Path p : files) {
            String n = p.getFileName().toString();
            String simple = n.substring(0, n.length() - ".java".length());
            all.put(simple, new Src(simple, Files.readString(p)));
        }
        return all;
    }

    private static Set<String> scannedNames(Map<String, Src> all) {
        Set<String> scanned = new TreeSet<>(all.keySet());
        NOT_SCANNED.forEach(f -> scanned.remove(f.substring(0, f.length() - ".java".length())));
        return scanned;
    }

    @Test
    void every_test_role_is_unique_to_its_class_or_declared_exclusive() throws IOException {
        Map<String, Src> all = readTree();
        assertThat(all.size()).as("the scan reads the whole test tree").isGreaterThan(MIN_FILES);
        Set<String> scanned = scannedNames(all);
        Map<String, Set<String>> roles = new TreeMap<>();
        int[] callSites = new int[1];
        List<String> problems = roleProblems(all, scanned, FAMILIES, RESERVED, BARE_CREATE_ROLE_FILES, roles, callSites);

        long shared = roles.values().stream().filter(c -> c.size() > 1).count();
        System.out.println("TestRoleUniquenessScan: examined " + roles.size() + " roles (" + shared + " used by more than one "
            + "class) from " + callSites[0] + " role-creating call sites in " + scanned.size() + " scanned classes");
        roles.forEach((r, cl) -> System.out.println("  role " + r + " <- " + cl));

        assertThat(callSites[0]).as("role-creating call sites examined").isGreaterThanOrEqualTo(MIN_CALL_SITES);
        assertThat(roles.size()).as("distinct roles examined").isGreaterThanOrEqualTo(MIN_ROLES);
        assertThat(roles.keySet()).as("non-vacuity: the known shared cases are examined")
            .contains("nexus_admin_diagview_replay", "nexus_diag", "nexus_admin_*");
        assertThat(roles.get("nexus_admin_diagview_replay"))
            .containsExactlyInAnyOrder("GrantsNexusDiagViewAccessIntegrationTest", "Taxonomy011ForeignOwnedDiagViewTest");
        assertThat(problems).as("test roles against the shared cluster").isEmpty();
    }

    @Test
    void startDedicated_callers_equal_the_javadoc_roster() throws IOException {
        Map<String, Src> all = readTree();
        Set<String> scanned = scannedNames(all);
        List<String> problems = new ArrayList<>();
        Set<String> callers = startDedicatedCallers(all, scanned, problems);
        Set<String> roster = javadocRoster(Files.readString(HELPER));

        System.out.println("TestRoleUniquenessScan: startDedicated callers (" + callers.size() + "): " + callers);
        System.out.println("TestRoleUniquenessScan: javadoc roster (" + roster.size() + "): " + roster
            + " + infra " + new TreeSet<>(INFRA_CALLERS.keySet()));

        assertThat(roster.size()).as("names read from the javadoc roster").isGreaterThanOrEqualTo(MIN_JAVADOC_ROSTER);
        assertThat(callers.size()).as("startDedicated callers found").isGreaterThanOrEqualTo(MIN_START_DEDICATED_CALLERS);
        problems.addAll(rosterDiff(callers, roster, INFRA_CALLERS));
        assertThat(problems).as("startDedicated callers against the roster").isEmpty();
    }

    // ── the checks, falsified on string fixtures ───────────────────────────────────────────────────────────────

    private static Map<String, Src> fixture(String... nameAndSource) {
        Map<String, Src> all = new TreeMap<>();
        for (int i = 0; i < nameAndSource.length; i += 2) {
            all.put(nameAndSource[i], new Src(nameAndSource[i], nameAndSource[i + 1]));
        }
        return all;
    }

    private static List<String> roles(Map<String, Src> all) {
        return roleProblems(all, new TreeSet<>(all.keySet()), Map.of(), RESERVED, Map.of(), null, null);
    }

    private static String serviceClass(String role) {
        return "class X { void f() { PgContainerHelper.bootstrapServiceRole(su, \"" + role + "\", \"pw\"); } }";
    }

    @Test
    void a_second_class_on_a_role_that_is_not_declared_exclusive_fails() {
        assertThat(roles(fixture("A", serviceClass("svc_x"), "B", serviceClass("svc_x"))))
            .singleElement().asString().contains("svc_x").contains("[A, B]").contains("do not declare it exclusive");
        assertThat(roles(fixture("A", serviceClass("svc_x"), "B", serviceClass("svc_y")))).isEmpty();
    }

    @Test
    void a_role_declared_exclusive_by_every_user_passes_and_by_only_one_fails() {
        String a = "class A { void f() { var pg = PgContainerHelper.startEmptyDatabase(ROLE);"
            + " PgContainerHelper.bootstrapNonSuperuserOwner(su, ROLE, \"pw\"); } static final String ROLE = \"r_x\"; }";
        String b = a.replace("class A", "class B");
        String bUndeclared = "class B { void f() { PgContainerHelper.startEmptyDatabase();"
            + " PgContainerHelper.bootstrapNonSuperuserOwner(su, \"r_x\", \"pw\"); } }";
        assertThat(roles(fixture("A", a, "B", b))).isEmpty();
        assertThat(roles(fixture("A", a, "B", bUndeclared))).singleElement().asString().contains("[B] do not declare");
    }

    @Test
    void constants_locals_concatenation_and_multiline_calls_resolve() {
        String a = """
            class A {
                static final String PREFIX = "svc_";
                static final String ROLE = PREFIX + "multi";
                void f() {
                    final String local = ROLE + "_x";
                    PgContainerHelper.bootstrapServiceRole(
                        su,
                        local,
                        "pw");
                }
            }
            """;
        String b = """
            class B { void f() { PgContainerHelper.createNologinRole(su, A.ROLE + "_x"); } }
            """;
        assertThat(roles(fixture("A", a, "B", b))).singleElement().asString().contains("svc_multi_x").contains("[A, B]");
    }

    @Test
    void a_role_named_only_in_a_comment_a_string_or_a_text_block_is_not_counted() {
        String a = """
            class A {
                // PgContainerHelper.bootstrapServiceRole(su, "svc_x", "pw");
                /* PgContainerHelper.createNologinRole(su, "svc_x"); */
                String s = "PgContainerHelper.createNologinRole(su, \\"svc_x\\")";
                String t = \"\"\"
                    PgContainerHelper.createNologinRole(su, "svc_x")
                    \"\"\";
            }
            """;
        assertThat(roles(fixture("A", a, "B", serviceClass("svc_x")))).isEmpty();
    }

    @Test
    void an_unresolvable_role_and_a_method_reference_fail_but_a_wrapper_forward_does_not() {
        String unresolved = "class A { void f(String who) { PgContainerHelper.createLoginRole(su, who, \"pw\", true); } }";
        assertThat(roles(fixture("A", unresolved))).singleElement().asString().contains("'who'").contains("callers do not pass");
        String ref = "class A { Object f = (CheckedRole) PgContainerHelper::createNologinRole; }";
        assertThat(roles(fixture("A", ref))).singleElement().asString().contains("method reference");
        String wrapper = """
            class W {
                static void bootstrapAdminRole(PostgreSQLContainer<?> pg, String role, String pass) {
                    PgContainerHelper.bootstrapNonSuperuserOwner(su, role, pass);
                }
            }
            """;
        String caller = "class C { void f() { W.bootstrapAdminRole(pg, \"r_c\", \"pw\"); } }";
        assertThat(roles(fixture("W", wrapper, "C", caller))).isEmpty();
    }

    @Test
    void a_role_passed_through_a_local_helper_method_is_traced_to_its_callers() {
        String a = """
            class A {
                void t() throws Exception { makeRole("r_one"); makeRole("r_two"); }
                private void makeRole(String role) throws Exception {
                    try (var c = pg.createConnection("")) {
                        PgContainerHelper.createNologinRole(c, role);
                    }
                }
            }
            """;
        String b = "class B { void f() { PgContainerHelper.createNologinRole(su, \"r_one\"); } }";
        assertThat(roles(fixture("A", a, "B", b))).singleElement().asString().contains("r_one").contains("[A, B]");
        assertThat(roles(fixture("A", a))).isEmpty();
        String uncalled = "class A { private void makeRole(String role) { PgContainerHelper.createNologinRole(c, role); } }";
        assertThat(roles(fixture("A", uncalled))).singleElement().asString().contains("whose callers do not pass");
    }

    @Test
    void a_creator_of_a_role_the_migration_fixes_must_declare_it_exclusive_or_take_its_own_container() {
        String bare = "class A { void f() { PgContainerHelper.startEmptyDatabase();"
            + " PgContainerHelper.createLoginRole(su, \"nexus_diag\", \"pw\", true); } }";
        String declared = "class A { void f() { PgContainerHelper.startEmptyDatabase(\"nexus_diag\");"
            + " PgContainerHelper.createLoginRole(su, \"nexus_diag\", \"pw\", true); } }";
        String dedicated = "class A { void f() { PgContainerHelper.startDedicated();"
            + " PgContainerHelper.createLoginRole(su, \"nexus_diag\", \"pw\", true); } }";
        assertThat(roles(fixture("A", bare))).singleElement().asString().contains("nexus_diag").contains("exclusive");
        assertThat(roles(fixture("A", declared))).isEmpty();
        assertThat(roles(fixture("A", dedicated))).isEmpty();
    }

    @Test
    void a_run_time_built_role_needs_an_allowlisted_family_with_the_derived_password() {
        String rig = """
            class %s {
                static void newRig(String label) {
                    String role = "nexus_admin_" + label;
                    String pass = %s;
                    PgContainerHelper.bootstrapNonSuperuserOwner(su, role, pass);
                }
            }
            """;
        Map<String, Src> ok = fixture("A", rig.formatted("A", "role + \"_pass\""), "B", rig.formatted("B", "role + \"_pass\""));
        Map<String, Family> fam = Map.of("nexus_admin_*", new Family(Set.of("A", "B"), "newRig", "r"));
        Set<String> both = new TreeSet<>(ok.keySet());
        assertThat(roleProblems(ok, both, fam, Set.of(), Map.of(), null, null)).isEmpty();
        assertThat(roleProblems(ok, both, Map.of(), Set.of(), Map.of(), null, null))
            .singleElement().asString().contains("not an allowlisted family");
        Map<String, Src> wrongPass = fixture("A", rig.formatted("A", "role + \"_pass\""), "B", rig.formatted("B", "\"fixed\""));
        assertThat(roleProblems(wrongPass, both, fam, Set.of(), Map.of(), null, null))
            .singleElement().asString().contains("B: family nexus_admin_* needs the password");
        assertThat(roleProblems(ok, Set.of("A"), fam, Set.of(), Map.of(), null, null))
            .singleElement().asString().contains("is used by [A] but allowlisted for [A, B]");
        assertThat(roleProblems(ok, both, Map.of("other_*", new Family(Set.of("A"), "newRig", "r"), "nexus_admin_*", fam.get("nexus_admin_*")),
            Set.of(), Map.of(), null, null)).singleElement().asString().contains("other_* is allowlisted but no scanned class");
    }

    @Test
    void a_literal_inside_a_family_from_a_class_outside_it_fails() {
        String rig = "class A { static void newRig(String label) { String role = \"nexus_admin_\" + label;"
            + " PgContainerHelper.bootstrapNonSuperuserOwner(su, role, role + \"_pass\"); }"
            + " void t() { newRig(\"fresh\"); } }";
        Map<String, Src> all = fixture("A", rig, "Z", serviceClass("nexus_admin_fresh"));
        Map<String, Family> fam = Map.of("nexus_admin_*", new Family(Set.of("A"), "newRig", "r"));
        assertThat(roleProblems(all, new TreeSet<>(all.keySet()), fam, Set.of(), Map.of(), null, null))
            .singleElement().asString().contains("Z: creates nexus_admin_fresh, a name family nexus_admin_* builds");
    }

    @Test
    void a_bare_create_role_literal_fails_unless_its_file_is_allowlisted() {
        String bare = "class A { String s = \"cre\" + \"x\"; String t = \"" + "CREATE" + " ROLE r_x\"; }";
        Map<String, Src> all = fixture("A", bare);
        assertThat(roleProblems(all, Set.of("A"), Map.of(), Set.of(), Map.of(), null, null))
            .singleElement().asString().contains("bare create-role statement");
        assertThat(roleProblems(all, Set.of("A"), Map.of(), Set.of(), Map.of("A", "why"), null, null)).isEmpty();
    }

    @Test
    void a_huge_literal_lexes_without_overflowing_the_stack() {
        String big = "x".repeat(200_000);
        String src = "class X { String a = \"" + big + "\"; String b = \"\"\"\n" + big + "\"\"\"; }";
        assertThat(mask(src, false)).hasSize(src.length());
        assertThat(mask(src, true)).isEqualTo(src);
    }

    @Test
    void the_roster_ratchet_fails_on_a_new_caller_and_on_a_roster_name_with_no_caller() {
        String caller = "class %s { void f() { PgContainerHelper.startDedicated(); PgContainerHelper.startDedicated(\"3g\", 1L); } }";
        String viaRef = "class R { Object f = withCount(PgContainerHelper::startDedicated); }";
        String prose = "class P { /** PgContainerHelper.startDedicated() */ String s = \"PgContainerHelper.startDedicated()\"; }";
        Map<String, Src> all = fixture("A", caller.formatted("A"), "N", caller.formatted("N"), "R", viaRef, "P", prose);
        List<String> found = new ArrayList<>();
        Set<String> callers = startDedicatedCallers(all, new TreeSet<>(all.keySet()), found);
        assertThat(callers).as("direct calls and method references count, comments and strings do not")
            .containsExactly("A", "N", "R");
        assertThat(found).isEmpty();

        assertThat(rosterDiff(callers, Set.of("A", "N", "R"), Map.of())).isEmpty();
        assertThat(rosterDiff(callers, Set.of("A", "R"), Map.of()))
            .singleElement().asString().startsWith("N: calls startDedicated but is not in its javadoc roster");
        assertThat(rosterDiff(callers, Set.of("A", "N", "R", "Gone"), Map.of()))
            .singleElement().asString().startsWith("Gone: the roster names it but it no longer calls startDedicated");
        assertThat(rosterDiff(callers, Set.of("A", "N"), Map.of("R", "infra"))).isEmpty();

        List<String> imports = new ArrayList<>();
        Map<String, Src> hidden = fixture("H", "import static dev.nexus.service.PgContainerHelper.startDedicated;\nclass H {}");
        startDedicatedCallers(hidden, Set.of("H"), imports);
        assertThat(imports).singleElement().asString().contains("static import");
    }

    @Test
    void the_javadoc_roster_is_read_per_item_and_newContainer_items_are_left_out() {
        String helper = """
            /**
             * Boots a container.
             * <ul>
             *   <li>{@code AlphaTest}, {@code BetaIntegrationTest} -- a reason mentioning {@code ALTER SYSTEM}.</li>
             *   <li>{@code GammaTest} (a different image) builds its own through {@link #newContainer}.</li>
             * </ul>
             * Also {@code DeltaTest} outside any item.
             */
            @SuppressWarnings("resource")
            public static PostgreSQLContainer<?> startDedicated() {
            """;
        assertThat(javadocRoster(helper)).containsExactly("AlphaTest", "BetaIntegrationTest");
    }
}
