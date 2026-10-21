// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.http.IdempotencyKeyGuard;
import org.junit.jupiter.api.Test;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayDeque;
import java.util.Deque;
import java.util.LinkedHashSet;
import java.util.Set;
import java.util.regex.Pattern;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-222 Phase 1 (bead nexus-yye8l.5), the reverse direction of the keyed-route census. {@code KeyedSweepRouteTest}
 * proves every route in {@link IdempotencyKeyGuard#KEYED_ROUTES} reaches a keyed handler; this proves every route
 * whose handler asks for a key ({@code keyedRequest(ex)}) is in that set. A handler that judges keys behind a route
 * the guard does not list would still be refused by the guard with {@code idempotency_key_unsupported}, so its keyed
 * form would be unreachable and nothing would say so.
 *
 * <p>Static: it reads the handler sources, finds EVERY call site of {@code keyedRequest} (an assignment, an
 * {@code if} condition, an argument), attributes each to its enclosing method by brace matching over the source with
 * comments and string literals blanked, and maps that method to its {@code /v1/vectors} operation through
 * {@code VectorHandler}'s dispatch {@code case}. A call it cannot attribute to a dispatched method (inside a nested
 * type, or in a helper no {@code case} reaches) fails the test rather than being guessed at.
 */
class KeyedRoutesCensusTest {

    private static final Path HTTP_DIR = Path.of("src/main/java/dev/nexus/service/http");
    /** A call of {@code keyedRequest}, not its declaration ({@code KeyedRequest keyedRequest(...)}). */
    private static final Pattern CALL = Pattern.compile("(?<![\\w])(?<!KeyedRequest )keyedRequest\\s*\\(");
    private static final String VECTORS_PREFIX = "/v1/vectors";

    @Test
    void everyRouteWhoseHandlerCallsKeyedRequestIsInKeyedRoutes() throws Exception {
        Path vectorHandler = HTTP_DIR.resolve("VectorHandler.java");
        assertThat(vectorHandler).as("the census runs from the service module directory").exists();

        // No other handler may call keyedRequest: this census only knows how to map VectorHandler's dispatch.
        try (Stream<Path> files = Files.list(HTTP_DIR)) {
            for (Path p : (Iterable<Path>) files.filter(f -> f.toString().endsWith(".java"))::iterator) {
                if (p.getFileName().toString().equals("VectorHandler.java")) continue;
                assertThat(CALL.matcher(blank(Files.readString(p))).find())
                    .as("%s calls keyedRequest: teach this census to map its routes", p.getFileName()).isFalse();
            }
        }

        Set<String> routes = routesCallingKeyedRequest(Files.readString(vectorHandler));
        assertThat(routes).as("non-vacuity: the two sweep handlers call keyedRequest").hasSizeGreaterThanOrEqualTo(2);

        assertThat(IdempotencyKeyGuard.KEYED_ROUTES)
            .as("every route whose handler calls keyedRequest (%s) is listed in KEYED_ROUTES", routes)
            .containsAll(routes);
        assertThat(routes).as("and the two sets agree")
            .containsExactlyInAnyOrderElementsOf(IdempotencyKeyGuard.KEYED_ROUTES);
    }

    @Test
    void theCensusSeesACallInAnIfConditionOnARouteMissingFromKeyedRoutes() {
        String src = """
            final class H {
                public void handle(HttpExchange ex) {
                    switch (op) {
                        case "/gc/quarantine-orphans" -> handleQ(ex, m);
                        case "/gc/brand-new" -> handleNew(ex, m);
                    }
                }
                private void handleQ(HttpExchange ex, String m) {
                    KeyedRequest keyed = keyedRequest(ex);
                }
                private void handleNew(HttpExchange ex, String m) {
                    String s = "}{ keyedRequest(not a call)";   // keyedRequest( in a comment and a string
                    if (keyedRequest(ex) != null) { run(); }
                }
                private KeyedRequest keyedRequest(HttpExchange ex) { return null; }
            }
            """;

        Set<String> routes = routesCallingKeyedRequest(src);

        assertThat(routes).containsExactlyInAnyOrder("/v1/vectors/gc/quarantine-orphans", "/v1/vectors/gc/brand-new");
        assertThat(IdempotencyKeyGuard.KEYED_ROUTES).as("the new route is not listed, so the real census would fail")
            .doesNotContain("/v1/vectors/gc/brand-new");
    }

    @Test
    void aCallItCannotAttributeToADispatchedMethodFailsLoudly() {
        String helper = """
            final class H {
                public void handle(HttpExchange ex) {
                    switch (op) { case "/gc/a" -> handleA(ex, m); }
                }
                private void handleA(HttpExchange ex, String m) { }
                private void sneaky(HttpExchange ex) { var k = keyedRequest(ex); }
            }
            """;
        assertThatThrownBy(() -> routesCallingKeyedRequest(helper))
            .hasMessageContaining("sneaky").hasMessageContaining("dispatched");

        String nested = """
            final class H {
                public void handle(HttpExchange ex) {
                    switch (op) { case "/gc/a" -> inner(ex, m); }
                }
                static final class Inner { void inner(HttpExchange ex) { keyedRequest(ex); } }
            }
            """;
        assertThatThrownBy(() -> routesCallingKeyedRequest(nested)).hasMessageContaining("nested");
    }

    /** The {@code /v1/vectors} routes of every method that calls {@code keyedRequest}; throws on one it cannot place. */
    static Set<String> routesCallingKeyedRequest(String source) {
        String src = blank(source);
        Set<String> routes = new LinkedHashSet<>();
        var call = CALL.matcher(src);
        while (call.find()) {
            String method = enclosingMethod(src, call.start());
            var dispatch = Pattern.compile("case\\s+\"(/[^\"]+)\"\\s*->\\s*" + Pattern.quote(method) + "\\s*\\(")
                .matcher(source);
            if (!dispatch.find()) {
                throw new AssertionError("keyedRequest is called from " + method + ", which no case label of the "
                    + "dispatch reaches: teach this census to map it (a helper is not a dispatched handler)");
            }
            routes.add(VECTORS_PREFIX + dispatch.group(1));
        }
        return routes;
    }

    private static final Pattern TYPE_KEYWORD = Pattern.compile("\\b(class|record|interface|enum)\\b");
    private static final Pattern TOP_CLASS = Pattern.compile("\\bclass\\s+(VectorHandler|H)\\b");
    private static final Pattern ANNOTATION = Pattern.compile("@\\w+(\\s*\\([^)]*\\))?");

    /** The name of the top-level-class method whose body contains {@code offset}. */
    private static String enclosingMethod(String src, int offset) {
        Deque<Integer> open = new ArrayDeque<>();
        for (int i = 0; i < offset; i++) {
            char c = src.charAt(i);
            if (c == '{') open.push(i);
            else if (c == '}' && !open.isEmpty()) open.pop();
        }
        // Open braces, innermost first; the last is the top-level class body and the one before it the member
        // (method, nested type, initialiser) that holds the call, whatever blocks lie inside it.
        Integer[] braces = open.toArray(new Integer[0]);
        if (braces.length < 2) {
            throw new AssertionError("a keyedRequest call at offset " + offset + " is not inside a method body");
        }
        int memberBrace = braces[braces.length - 2];
        int classBrace = braces[braces.length - 1];
        String header = ANNOTATION.matcher(src.substring(lastBoundary(src, memberBrace), memberBrace)).replaceAll(" ");
        if (TYPE_KEYWORD.matcher(header).find()) {
            throw new AssertionError("a keyedRequest call sits inside a nested type (" + header.strip() + "): "
                + "teach this census to map it");
        }
        String declaring = src.substring(lastBoundary(src, classBrace), classBrace);
        if (!TOP_CLASS.matcher(declaring).find()) {
            throw new AssertionError("unexpected top-level declaration (" + declaring.strip() + ")");
        }
        var name = Pattern.compile("(\\w+)\\s*\\(").matcher(header);
        if (!name.find()) {
            throw new AssertionError("cannot read a method name from the header: " + header.strip());
        }
        return name.group(1);
    }

    /** Index just after the previous {@code ;}, {@code {} or {@code }} before {@code at}: where a header starts. */
    private static int lastBoundary(String src, int at) {
        for (int i = at - 1; i >= 0; i--) {
            char c = src.charAt(i);
            if (c == ';' || c == '{' || c == '}') return i + 1;
        }
        return 0;
    }

    /** {@code src} with comments and string/char literal contents replaced by spaces, offsets preserved. */
    static String blank(String src) {
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
                if (i < n) { out[i++] = ' '; }
                if (i < n) { out[i++] = ' '; }
            } else if (c == '"' && i + 2 < n && out[i + 1] == '"' && out[i + 2] == '"') {
                i += 3;
                while (i + 2 < n && !(out[i] == '"' && out[i + 1] == '"' && out[i + 2] == '"')) {
                    if (out[i] != '\n') out[i] = ' ';
                    i++;
                }
                i += 3;
            } else if (c == '"' || c == '\'') {
                char q = c;
                i++;
                while (i < n && out[i] != q) {
                    if (out[i] == '\\' && i + 1 < n) { out[i++] = ' '; }
                    if (out[i] != '\n') out[i] = ' ';
                    i++;
                }
                i++;
            } else {
                i++;
            }
        }
        return new String(out);
    }
}
