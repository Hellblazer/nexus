// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.List;
import java.util.regex.Pattern;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-236 Phase 4 Step 2a (nexus-6pfgb.66), review I3: the route census of the chunk-metadata allow-list. The check
 * is called by hand from each client route that writes chunk metadata, so a new route that writes chunk metadata
 * without calling it is invisible to Step 2b's window and unenforced in Step 2c. This reads every engine source for a
 * call of the repository and combined-write entry points that write {@code chunks.metadata} on a client's behalf
 * ({@code upsertChunks*}, {@code updateMetadata*}, {@code appendCombined}, {@code appendManyCombined}, {@code
 * writeManyCombined}) and fails when the method that holds the call has no {@code observeChunkKeys} call.
 *
 * <p>A call from anywhere but a handler fails the same way: an engine-internal writer that calls one of these is
 * either seen by the check or named in the seams of {@code ChunkKeyAllowlistCheck}'s javadoc, and this census makes
 * that a decision, not an accident. A method-local scan, not a flow analysis: it proves the call is in the method,
 * not that it runs on every path; the log tests run each route.
 */
class ChunkKeyRouteCensusTest {

    private static final Path MAIN = Path.of("src/main/java");
    private static final Pattern WRITE_CALL = Pattern.compile(
        "\\.\\s*(upsertChunks\\w*|updateMetadata\\w*|appendCombined|appendManyCombined|writeManyCombined)\\s*\\(");
    private static final Pattern OBSERVE = Pattern.compile("\\bobserveChunkKeys\\s*\\(");
    private static final Pattern ANNOTATION = Pattern.compile("@\\w+(\\s*\\([^)]*\\))?");
    private static final Pattern TYPE_KEYWORD = Pattern.compile("\\b(class|record|interface|enum)\\b");

    /** A write call and the member that holds it. */
    record Site(String call, String member, boolean observed) { }

    @Test
    void everyMethodThatWritesChunkMetadataForAClientCallsTheAllowlistCheck() throws Exception {
        assertThat(MAIN).as("the census runs from the service module directory").exists();
        List<String> unobserved = new ArrayList<>();
        int sites = 0;
        List<String> members = new ArrayList<>();
        try (Stream<Path> files = Files.walk(MAIN)) {
            for (Path p : (Iterable<Path>) files.filter(f -> f.toString().endsWith(".java"))::iterator) {
                for (Site site : sites(Files.readString(p))) {
                    sites++;
                    members.add(p.getFileName() + "#" + site.member());
                    if (!site.observed()) {
                        unobserved.add(p.getFileName() + "#" + site.member() + " calls " + site.call());
                    }
                }
            }
        }
        assertThat(sites).as("non-vacuity: the two handlers call the write entry points").isGreaterThanOrEqualTo(6);
        assertThat(members.stream().distinct().count()).as("non-vacuity: five client routes, %s", members)
            .isGreaterThanOrEqualTo(5);
        assertThat(unobserved).as("a method that writes chunk metadata without calling observeChunkKeys; call the "
            + "check before the write, or name the writer among the seams in ChunkKeyAllowlistCheck's javadoc "
            + "and teach this census about it").isEmpty();
    }

    @Test
    void theCensusFlagsAWriteCallWithoutTheCheck_andPassesOneWithIt() {
        String src = """
            final class H {
                void good(Ex ex) {
                    observeChunkKeys(ex, t, c, "good", metas, 1);
                    repo.upsertChunksWithTokens(t, c, ids);
                }
                void bad(Ex ex) {
                    String s = "observeChunkKeys( in a string";   // a string and a comment do not count
                    // observeChunkKeys(ex)
                    var r = repo.updateMetadataWithMissing(t, c, ids);
                }
                void viaLambda(Ex ex) {
                    run(() -> { combinedWriteService.appendManyCombined(a, b); });
                }
            }
            """;
        List<Site> found = sites(src);
        assertThat(found).extracting(Site::member, Site::observed).containsExactly(
            org.assertj.core.groups.Tuple.tuple("good", true),
            org.assertj.core.groups.Tuple.tuple("bad", false),
            org.assertj.core.groups.Tuple.tuple("viaLambda", false));
    }

    /** Every write call in {@code source} with the member (method) holding it and whether that member observes. */
    static List<Site> sites(String source) {
        String src = KeyedRoutesCensusTest.blank(source);
        List<Site> out = new ArrayList<>();
        var call = WRITE_CALL.matcher(src);
        while (call.find()) {
            int[] member = enclosingMember(src, call.start());
            String body = src.substring(member[0], member[1]);
            String header = ANNOTATION.matcher(src.substring(lastBoundary(src, member[0]), member[0])).replaceAll(" ")
                .strip();
            var name = Pattern.compile("(\\w+)\\s*\\(").matcher(header);
            String memberName = name.find() ? name.group(1) : header;
            out.add(new Site(call.group(1), memberName, OBSERVE.matcher(body).find()));
        }
        return out;
    }

    /** The [open, close) offsets of the member block (the child of the top-level class body) holding {@code offset}. */
    private static int[] enclosingMember(String src, int offset) {
        Deque<Integer> open = new ArrayDeque<>();
        for (int i = 0; i < offset; i++) {
            char c = src.charAt(i);
            if (c == '{') open.push(i);
            else if (c == '}' && !open.isEmpty()) open.pop();
        }
        Integer[] braces = open.toArray(new Integer[0]);   // innermost first
        if (braces.length < 2) {
            throw new AssertionError("a chunk-metadata write call at offset " + offset + " is outside a method body");
        }
        int memberBrace = braces[braces.length - 2];
        String header = src.substring(lastBoundary(src, memberBrace), memberBrace);
        if (TYPE_KEYWORD.matcher(header).find()) {
            throw new AssertionError("a chunk-metadata write call sits inside a nested type (" + header.strip()
                + "): teach this census to read it");
        }
        int depth = 0;
        for (int i = memberBrace; i < src.length(); i++) {
            char c = src.charAt(i);
            if (c == '{') depth++;
            else if (c == '}' && --depth == 0) return new int[] {memberBrace, i + 1};
        }
        throw new AssertionError("unbalanced braces after offset " + memberBrace);
    }

    private static int lastBoundary(String src, int at) {
        for (int i = at - 1; i >= 0; i--) {
            char c = src.charAt(i);
            if (c == ';' || c == '{' || c == '}') return i + 1;
        }
        return 0;
    }
}
