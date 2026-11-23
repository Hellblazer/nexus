// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.TreeSet;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25): the three {@link QuarantineOrigin} overloads that delegate with {@code Gate3.never()} stay only
 * for callers in another group's files and for tests (none left: the assembly threaded the gate through ReembedSourceReader and ChashRepository).
 * A production caller that is not on that list fails here, so a name-path read cannot be added unseen. The list can only shrink:
 * an entry whose call is gone fails too, so it is removed when the caller takes the gate.
 */
class Rdr229P35cGateOverloadLintTest {

    /** The delegating forms: method name and the argument count that selects the name-path overload. */
    private static final Pattern CALL = Pattern.compile("QuarantineOrigin\\.(countRowsOf|isLiveRegistered|retagRowsOf)\\(([^;]*?)\\)\\s*[;)\\s,?:&|]");

    /** Empty since the assembly threaded the gate through ReembedSourceReader and ChashRepository: no production caller uses a gateless form. */
    private static final Set<String> KNOWN = Set.of();

    private static int arguments(String list) {
        int depth = 0, count = 1;
        for (char c : list.toCharArray()) {
            if (c == '(') depth++;
            else if (c == ')') depth--;
            else if (c == ',' && depth == 0) count++;
        }
        return count;
    }

    private static int gateless(String method) {
        return method.equals("retagRowsOf") ? 4 : 3;
    }

    @Test
    void onlyTheKnownProductionCallersUseTheNeverDelegatingOverloads() throws IOException {
        Path root = Path.of("src", "main", "java", "dev", "nexus", "service");
        Set<String> found = new TreeSet<>();
        int scanned = 0;
        try (Stream<Path> files = Files.walk(root)) {
            for (Path p : files.filter(f -> f.toString().endsWith(".java")).toList()) {
                scanned++;
                String text = Files.readString(p);
                Matcher m = CALL.matcher(text);
                while (m.find()) {
                    if (arguments(m.group(2)) == gateless(m.group(1))) {
                        found.add(root.relativize(p).toString().replace('\\', '/') + " " + m.group(1));
                    }
                }
            }
        }
        assertThat(scanned).as("non-vacuity: the walk saw the main tree").isGreaterThan(100);
        assertThat(found).as("production callers of a Gate3.never() delegating overload (thread the gate, or list the file with a reason)")
            .isEqualTo(new TreeSet<>(KNOWN));
    }

    @Test
    void theLintSeesAGatelessCallAndIgnoresAGatedOne() {
        List<String> seen = new ArrayList<>();
        for (String source : List.of("QuarantineOrigin.countRowsOf(ctx, t, o);", "QuarantineOrigin.countRowsOf(ctx, t, o, gate3);")) {
            Matcher m = CALL.matcher(source);
            if (m.find() && arguments(m.group(2)) == gateless(m.group(1))) seen.add(source);
        }
        assertThat(seen).containsExactly("QuarantineOrigin.countRowsOf(ctx, t, o);");
    }

    // ---- every method or constructor in main whose body hands Gate3.never() to the overload it delegates to (linda 2026-11-23) ----------

    /** A statement {@code return f(..., Gate3.never());} or {@code this(..., Gate3.never(), ...);}: the overload that delegates. */
    private static final Pattern DELEGATE = Pattern.compile(
        "(?:return\\s+(?:[\\w.]+\\.)?(\\w+)|\\b(this))\\s*\\(([^;{}]*?\\bGate3\\.never\\(\\)[^;{}]*?)\\)\\s*;", Pattern.DOTALL);

    private static List<String> splitArgs(String list) {
        List<String> out = new ArrayList<>();
        int depth = 0;
        StringBuilder cur = new StringBuilder();
        for (char c : list.toCharArray()) {
            if (c == '(' || c == '[' || c == '<') depth++;
            else if (c == ')' || c == ']' || c == '>') depth--;
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

    /** A call's argument text starting after its opening parenthesis at {@code from}; null when unbalanced. */
    private static String argText(String text, int from) {
        int depth = 1;
        for (int j = from; j < text.length(); j++) {
            char c = text.charAt(j);
            if (c == '(') depth++;
            else if (c == ')' && --depth == 0) return text.substring(from, j);
        }
        return null;
    }

    /** A declaration's parameter list reads {@code Type name, Type name}: every parameter is two tokens and none holds a parenthesis. */
    private static boolean looksLikeParameters(List<String> args) {
        return !args.isEmpty() && args.stream().allMatch(a -> a.matches("(?:final\\s+)?[\\w.<>\\[\\]?, ]+\\s+\\w+"));
    }

    /**
     * The delegating overloads of production code, and the calls of them in production code that name no gate. A call counts when its argument
     * count equals the overload's (the overload's own call minus the Gate3.never() argument), it is in the class itself or qualified by it, and
     * no argument mentions a gate (a call that passes {@code gate3} reaches the gated form). Production code reaches a name-path read by a
     * one-argument shortfall like this one only by mistake (resolveNeedEmbedIdx called the four-argument chunkScope, so after the latch and a
     * one-row rename every flush re-embedded).
     */
    @Test
    void noProductionCallReachesAnOverloadThatDelegatesWithGate3Never() throws IOException {
        Path root = Path.of("src", "main", "java", "dev", "nexus", "service");
        java.util.Map<Path, String> sources = new java.util.LinkedHashMap<>();
        try (Stream<Path> files = Files.walk(root)) {
            for (Path p : files.filter(f -> f.toString().endsWith(".java")).toList()) sources.put(p, Files.readString(p));
        }
        record Overload(String owner, String name, int arity, Path file) { }
        List<Overload> overloads = new ArrayList<>();
        for (var e : sources.entrySet()) {
            String owner = e.getKey().getFileName().toString().replace(".java", "");
            Matcher m = DELEGATE.matcher(e.getValue());
            while (m.find()) {
                List<String> args = splitArgs(m.group(3));
                if (args.stream().noneMatch(a -> a.endsWith("Gate3.never()"))) continue;
                overloads.add(new Overload(owner, m.group(2) != null ? "<init>" : m.group(1), args.size() - 1, e.getKey()));
            }
        }
        assertThat(overloads.size()).as("non-vacuity: the scan finds the delegating overloads of main").isGreaterThan(30);
        Set<String> offenders = new TreeSet<>();
        for (Overload o : overloads) {
            for (var e : sources.entrySet()) {
                String text = e.getValue();
                boolean sameFile = e.getKey().equals(o.file());
                Pattern call = o.name().equals("<init>")
                    ? Pattern.compile("new\\s+(?:[\\w.]*\\.)?" + o.owner() + "\\s*\\(")
                    : Pattern.compile("(?<![\\w])(?:(?:[\\w.]*\\.)?" + o.owner() + "\\.)?" + o.name() + "\\s*\\(");
                Matcher m = call.matcher(text);
                while (m.find()) {
                    boolean qualified = m.group().contains(o.owner() + ".");
                    if (!o.name().equals("<init>") && !sameFile && !qualified) continue;
                    int lineStart = text.lastIndexOf('\n', m.start()) + 1;
                    String before = text.substring(lineStart, m.start());
                    if (before.strip().startsWith("*") || before.strip().startsWith("//")) continue;
                    String argsText = argText(text, m.end());
                    if (argsText == null) continue;
                    List<String> args = splitArgs(argsText);
                    if (args.size() != o.arity() || looksLikeParameters(args)) continue;   // another overload, or the declaration itself
                    if (args.stream().anyMatch(a -> a.toLowerCase().contains("gate") || a.endsWith("Gate3.never()"))) continue;
                    int line = (int) text.substring(0, m.start()).chars().filter(c -> c == '\n').count() + 1;
                    offenders.add(e.getKey().getFileName() + ":" + line + " calls " + o.owner() + "." + o.name() + "/" + o.arity());
                }
            }
        }
        assertThat(offenders).as("production calls of an overload that delegates with Gate3.never(): pass the gate the caller holds").isEmpty();
    }
}
