// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * RDR-229 P3.5c (folio's reviews of 025821a9eb and 6e7d2379de): finds, in a set of production sources, every call that reaches a name-path
 * overload, that is a method or constructor whose body hands {@code Gate3.never()} to the overload it delegates to. Text-based, over comment-free
 * source in the repository's formatting (members at four spaces, nested-class members at eight):
 * <ul>
 *   <li>a <b>delegating overload</b> is a member whose body holds a call to its own name (or {@code this(}/{@code super(}) with
 *       {@code Gate3.never()} as an argument, written in any form (a {@code return}, an assignment, a statement); its arity is its declared
 *       parameter count;</li>
 *   <li>a <b>call</b> of one is {@code name(} in its own file, {@code Owner.name(}, {@code v.name(} where {@code v} is declared or assigned as
 *       an {@code Owner} in that file, {@code new Owner(}, or an unqualified {@code name(} in a file that statically imports it; whose argument
 *       count equals the overload's arity; that is not a declaration; and none of whose arguments is a {@code Gate3} (an identifier the file
 *       declares with that type, a {@code Gate3.never()} or a lambda that is one) -- a variable merely named "gate" is not a Gate3;</li>
 *   <li>a call made inside another delegating overload is a <b>chain</b> and does not count by itself: it counts only through a call of that
 *       overload from outside, which is flagged on its own;</li>
 *   <li>any other use of {@code Gate3.never()} in these sources (a field, a lambda, an argument of a call that is not a delegation) is an
 *       offender unless its file is in {@code allowedFiles}.</li>
 * </ul>
 */
final class GateNeverLint {

    private GateNeverLint() {
    }

    /** What a scan saw, for the test's non-vacuity. */
    record Result(Set<String> offenders, int overloads, int neverUses) { }

    private record Member(String file, String owner, String name, int arity, int start, int end, boolean ctor, boolean gateParam) { }

    /** The text with comments blanked (same length, so offsets and line numbers hold). */
    static String stripComments(String s) {
        StringBuilder out = new StringBuilder(s.length());
        int i = 0, n = s.length();
        while (i < n) {
            char c = s.charAt(i);
            if (c == '"' || c == '\'') {
                char q = c;
                out.append(c);
                i++;
                while (i < n && s.charAt(i) != q) {
                    if (s.charAt(i) == '\\' && i + 1 < n) {
                        out.append(s.charAt(i++));
                    }
                    out.append(s.charAt(i++));
                }
                if (i < n) out.append(s.charAt(i++));
            } else if (c == '/' && i + 1 < n && s.charAt(i + 1) == '/') {
                while (i < n && s.charAt(i) != '\n') {
                    out.append(' ');
                    i++;
                }
            } else if (c == '/' && i + 1 < n && s.charAt(i + 1) == '*') {
                out.append("  ");
                i += 2;
                while (i < n && !(s.charAt(i) == '*' && i + 1 < n && s.charAt(i + 1) == '/')) {
                    out.append(s.charAt(i) == '\n' ? '\n' : ' ');
                    i++;
                }
                if (i < n) {
                    out.append("  ");
                    i += 2;
                }
            } else {
                out.append(c);
                i++;
            }
        }
        return out.toString();
    }

    static List<String> splitArgs(String list) {
        List<String> out = new ArrayList<>();
        int depth = 0;
        StringBuilder cur = new StringBuilder();
        for (char c : list.toCharArray()) {
            if (c == '(' || c == '[' || c == '{') depth++;
            else if (c == ')' || c == ']' || c == '}') depth--;
            if (c == ',' && depth == 0) {
                out.add(cur.toString().trim());
                cur.setLength(0);
            } else {
                cur.append(c);
            }
        }
        if (!cur.toString().isBlank()) out.add(cur.toString().trim());
        return out;
    }

    /** The index of the parenthesis that closes the one opened just before {@code from}; -1 when unbalanced. */
    private static int closeOf(String text, int from) {
        int depth = 1;
        for (int j = from; j < text.length(); j++) {
            char c = text.charAt(j);
            if (c == '(') depth++;
            else if (c == ')' && --depth == 0) return j;
        }
        return -1;
    }

    private static int closeBrace(String text, int open) {
        int depth = 0;
        for (int j = open; j < text.length(); j++) {
            char c = text.charAt(j);
            if (c == '{') depth++;
            else if (c == '}' && --depth == 0) return j;
        }
        return -1;
    }

    private static final Set<String> NOT_MEMBERS = Set.of("if", "for", "while", "switch", "return", "catch", "synchronized", "try", "else",
        "new", "this", "super", "throw", "assert", "do");

    private static final Pattern MEMBER = Pattern.compile(
        "(?m)^( {4}| {8})(?:(?:public|private|protected|static|final|synchronized|abstract|default)\\s+)*(?:<[^>\\n]*>\\s+)?"
            + "(?:[\\w.]+(?:<[^\\n(]*?>)?(?:\\[\\])*\\s+)?(\\w+)\\s*\\(");

    private static List<Member> members(String file, String owner, String text) {
        List<Member> out = new ArrayList<>();
        Matcher m = MEMBER.matcher(text);
        while (m.find()) {
            String name = m.group(2);
            if (NOT_MEMBERS.contains(name)) continue;
            int open = m.end() - 1;
            int close = closeOf(text, open + 1);
            if (close < 0) continue;
            int k = close + 1;
            while (k < text.length() && Character.isWhitespace(text.charAt(k))) k++;
            if (text.startsWith("throws", k)) {
                while (k < text.length() && text.charAt(k) != '{' && text.charAt(k) != ';') k++;
            }
            if (k >= text.length() || text.charAt(k) != '{') continue;   // an abstract member or a call at member indent
            int end = closeBrace(text, k);
            if (end < 0) continue;
            int arity = splitArgs(text.substring(open + 1, close)).size();
            out.add(new Member(file, owner, name, arity, m.start(), end, name.equals(owner), text.substring(open + 1, close).contains("Gate3")));
        }
        return out;
    }

    private static Member enclosing(List<Member> members, int offset) {
        Member best = null;
        for (Member mem : members) {
            if (offset > mem.start() && offset < mem.end() && (best == null || mem.start() > best.start())) best = mem;
        }
        return best;
    }

    private static int lineOf(String text, int offset) {
        int line = 1;
        for (int i = 0; i < offset && i < text.length(); i++) if (text.charAt(i) == '\n') line++;
        return line;
    }

    private static final Set<String> gateMethods = new java.util.HashSet<>();

    private static final Pattern NEVER = Pattern.compile("\\bGate3\\s*\\.\\s*never\\s*\\(\\s*\\)");

    /**
     * @param sources       file name (a path ending in {@code Owner.java}) to source text
     * @param allowedFiles  files that may use {@code Gate3.never()} outside a delegation (Gate3.java itself, and any boot path named)
     */
    static Result scan(Map<String, String> sources, Set<String> allowedFiles) {
        Map<String, String> text = new LinkedHashMap<>();
        Map<String, List<Member>> membersOf = new LinkedHashMap<>();
        for (var e : sources.entrySet()) {
            String file = e.getKey();
            String owner = file.substring(file.lastIndexOf('/') + 1).replace(".java", "");
            String t = stripComments(e.getValue());
            text.put(file, t);
            membersOf.put(file, members(file, owner, t));
        }
        Set<String> offenders = new TreeSet<>();
        // the methods that return a Gate3 (repo.gate3()): an argument that is a call of one is a gate
        gateMethods.clear();
        for (String t : text.values()) {
            Matcher gm = Pattern.compile("\\bGate3\\s+(\\w+)\\s*\\(").matcher(t);
            while (gm.find()) gateMethods.add(gm.group(1));
        }
        // 1. the delegating overloads, and the uses of Gate3.never() that are not one
        List<Member> overloads = new ArrayList<>();
        int neverUses = 0;
        for (var e : text.entrySet()) {
            String file = e.getKey();
            String t = e.getValue();
            Matcher nm = NEVER.matcher(t);
            while (nm.find()) {
                neverUses++;
                if (allowedFiles.contains(file.substring(file.lastIndexOf('/') + 1))) continue;
                Member enc = enclosing(membersOf.get(file), nm.start());
                boolean delegation = false;
                if (enc != null) {
                    // the innermost call that holds this Gate3.never() as an argument
                    int depth = 0, p = nm.start() - 1;
                    int callOpen = -1;
                    for (; p > enc.start(); p--) {
                        char c = t.charAt(p);
                        if (c == ')') depth++;
                        else if (c == '(') {
                            if (depth == 0) {
                                callOpen = p;
                                break;
                            }
                            depth--;
                        }
                    }
                    if (callOpen > 0) {
                        int q = callOpen - 1;
                        while (q >= 0 && Character.isWhitespace(t.charAt(q))) q--;
                        int endName = q + 1;
                        while (q >= 0 && (Character.isJavaIdentifierPart(t.charAt(q)))) q--;
                        String callee = t.substring(q + 1, endName);
                        if (callee.equals(enc.name()) || callee.equals("this") || callee.equals("super")) {
                            delegation = true;
                            if (overloads.stream().noneMatch(o -> o.file().equals(file) && o.start() == enc.start())) overloads.add(enc);
                        }
                    }
                }
                if (!delegation) {
                    offenders.add(file + ":" + lineOf(t, nm.start()) + " uses Gate3.never() outside a delegation to an overload");
                }
            }
        }
        // a member of the same name (or a constructor of the same class) whose body calls a gateless overload of its own family with no gate is a
        // gateless overload too: the chain of shorter forms (a six-argument form calling the seven-argument one that delegates with never())
        boolean grew = true;
        while (grew) {
            grew = false;
            for (var e : text.entrySet()) {
                String file = e.getKey();
                String t = e.getValue();
                for (Member mem : membersOf.get(file)) {
                    if (overloads.stream().anyMatch(x -> x.file().equals(file) && x.start() == mem.start())) continue;
                    String body = t.substring(mem.start(), mem.end());
                    for (Member o : new ArrayList<>(overloads)) {
                        // the same family (name), or a member that has a gated sibling of its own name and so is a form of its family
                        boolean gatedSibling = membersOf.get(file).stream().anyMatch(x -> x != mem && x.name().equals(mem.name()) && x.gateParam());
                        if (!o.file().equals(file) || !(o.name().equals(mem.name()) || gatedSibling) || (o.name().equals(mem.name()) && o.arity() == mem.arity())) continue;
                        Matcher cm = Pattern.compile((o.ctor() ? "(?:\\bthis|\\bnew\\s+" + o.owner() + ")" : "(?<![\\w.])" + Pattern.quote(o.name())) + "\\s*\\(").matcher(body);
                        while (cm.find()) {
                            int close = closeOf(body, cm.end());
                            if (close < 0) continue;
                            List<String> args = splitArgs(body.substring(cm.end(), close));
                            if (args.size() == o.arity() && args.stream().noneMatch(a -> isGate(a, t))) {
                                overloads.add(mem);
                                grew = true;
                                break;
                            }
                        }
                        if (grew) break;
                    }
                    if (grew) break;
                }
                if (grew) break;
            }
        }
        // 2. the calls of the overloads
        for (Member o : overloads) {
            for (var e : text.entrySet()) {
                String file = e.getKey();
                String t = e.getValue();
                boolean sameFile = file.equals(o.file());
                Set<String> receivers = receiversOf(t, o.owner());
                boolean staticImport = Pattern.compile("import\\s+static\\s+[\\w.]*\\b" + o.owner() + "\\.(?:" + Pattern.quote(o.name()) + "|\\*)\\s*;").matcher(t).find();
                Pattern call;
                if (o.ctor()) {
                    call = Pattern.compile("(?<![\\w.])(?:new\\s+(?:[\\w.]*\\.)?" + o.owner() + (sameFile ? "|this|super" : "") + ")\\s*\\(");
                } else {
                    call = Pattern.compile("(?<![\\w.])(?:(?:[\\w.]*\\.)?" + o.owner() + "\\.|(?:" + String.join("|", receivers.isEmpty() ? List.of("\\b\\B") : receivers)
                        + ")\\.)?" + Pattern.quote(o.name()) + "\\s*\\(");
                }
                Matcher m = call.matcher(t);
                while (m.find()) {
                    String head = m.group();
                    boolean qualified = head.contains(".");
                    if (!o.ctor() && !qualified && !sameFile && !staticImport) continue;
                    if (!o.ctor() && qualified && !head.contains(o.owner() + ".") && receivers.stream().noneMatch(r -> head.startsWith(r + "."))) continue;
                    int close = closeOf(t, m.end());
                    if (close < 0) continue;
                    List<String> args = splitArgs(t.substring(m.end(), close));
                    int k = close + 1;
                    while (k < t.length() && Character.isWhitespace(t.charAt(k))) k++;
                    if (k < t.length() && (t.charAt(k) == '{' || t.startsWith("throws", k))) continue;   // a declaration, not a call
                    if (args.size() != o.arity()) continue;
                    if (args.stream().anyMatch(a -> isGate(a, t))) continue;
                    Member enc = enclosing(membersOf.get(file), m.start());
                    final Member encF = enc;
                    if (head.startsWith("this") || head.startsWith("super")) {
                        // only a constructor of the owner itself calls its constructors through this( or super(
                        if (enc == null || !enc.ctor() || !enc.owner().equals(o.owner())) continue;
                    }
                    if (enc != null && overloads.stream().anyMatch(x -> x.file().equals(file) && x.start() == encF.start())) continue;   // a chain
                    if (enc != null && enc.start() == o.start() && file.equals(o.file()) && !o.ctor()) continue;   // the overload's own recursion
                    offenders.add(file + ":" + lineOf(t, m.start()) + " calls " + o.owner() + "." + o.name() + "/" + o.arity() + " without a gate");
                }
            }
        }
        return new Result(offenders, overloads.size(), neverUses);
    }

    /** The names a file declares or assigns as an {@code owner}: {@code Owner v}, {@code Owner<..> v}, {@code var v = new Owner(}. */
    private static Set<String> receiversOf(String t, String owner) {
        Set<String> out = new LinkedHashSet<>();
        Matcher a = Pattern.compile("\\b" + owner + "(?:<[^>;()]*>)?\\s+(\\w+)\\s*[=;,)]").matcher(t);
        while (a.find()) out.add(a.group(1));
        Matcher b = Pattern.compile("\\b(?:var|final\\s+var)\\s+(\\w+)\\s*=\\s*new\\s+" + owner + "\\b").matcher(t);
        while (b.find()) out.add(b.group(1));
        Matcher c = Pattern.compile("\\b" + owner + "(?:<[^>;()]*>)?\\s+(\\w+)\\s*\\)").matcher(t);
        while (c.find()) out.add(c.group(1));
        return out;
    }

    /** An argument is a Gate3 when it is {@code Gate3.never()}, a lambda, or an identifier the file declares with the type {@code Gate3}. */
    private static boolean isGate(String arg, String fileText) {
        String a = arg.strip();
        if (NEVER.matcher(a).find() || a.matches("\\(\\s*\\)\\s*->.*")) return true;
        String id = a.replaceFirst("^this\\.", "");
        Matcher call = Pattern.compile("(?:[\\w.]+(?:\\(\\))?\\.)?(\\w+)\\(\\)").matcher(id);
        if (call.matches() && gateMethods.contains(call.group(1))) return true;
        if (!id.matches("[A-Za-z_]\\w*")) return false;
        return Pattern.compile("\\b(?:dev\\.nexus\\.service\\.datamove\\.)?Gate3\\s+" + id + "\\b").matcher(fileText).find();
    }
}
