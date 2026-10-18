// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.http.IdempotencyKeyGuard;
import org.junit.jupiter.api.Test;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashSet;
import java.util.Set;
import java.util.regex.Pattern;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-222 Phase 1 (bead nexus-yye8l.5), the reverse direction of the keyed-route census. {@code KeyedSweepRouteTest}
 * proves every route in {@link IdempotencyKeyGuard#KEYED_ROUTES} reaches a keyed handler; this proves every route
 * whose handler asks for a key ({@code keyedRequest(ex)}) is in that set. A handler that judges keys behind a route
 * the guard does not list would still be refused by the guard with {@code idempotency_key_unsupported}, so its keyed
 * form would be unreachable and nothing would say so.
 *
 * <p>Static: it reads the handler sources, finds each method that calls {@code keyedRequest}, maps the method to its
 * {@code /v1/vectors} operation through {@code VectorHandler}'s dispatch {@code case}, and compares.
 */
class KeyedRoutesCensusTest {

    private static final Path HTTP_DIR = Path.of("src/main/java/dev/nexus/service/http");
    private static final Pattern CALL = Pattern.compile("=\\s*keyedRequest\\(");
    private static final Pattern METHOD_DECL = Pattern.compile("(?m)^\\s+(?:private |public |static )*void (handle\\w+)\\(");
    private static final String VECTORS_PREFIX = "/v1/vectors";

    @Test
    void everyRouteWhoseHandlerCallsKeyedRequestIsInKeyedRoutes() throws Exception {
        Path vectorHandler = HTTP_DIR.resolve("VectorHandler.java");
        assertThat(vectorHandler).as("the census runs from the service module directory").exists();
        String src = Files.readString(vectorHandler);

        // No other handler may call keyedRequest: this census only knows how to map VectorHandler's dispatch.
        try (Stream<Path> files = Files.list(HTTP_DIR)) {
            for (Path p : (Iterable<Path>) files.filter(f -> f.toString().endsWith(".java"))::iterator) {
                if (p.getFileName().toString().equals("VectorHandler.java")) continue;
                assertThat(CALL.matcher(Files.readString(p)).find())
                    .as("%s calls keyedRequest: teach this census to map its routes", p.getFileName()).isFalse();
            }
        }

        Set<String> methods = new LinkedHashSet<>();
        var call = CALL.matcher(src);
        while (call.find()) {
            String head = src.substring(0, call.start());
            var decl = METHOD_DECL.matcher(head);
            String last = null;
            while (decl.find()) last = decl.group(1);
            assertThat(last).as("a handler method encloses the keyedRequest call at offset %d", call.start())
                .isNotNull();
            methods.add(last);
        }
        assertThat(methods).as("non-vacuity: the two sweep handlers call keyedRequest").hasSizeGreaterThanOrEqualTo(2);

        Set<String> routes = new LinkedHashSet<>();
        for (String method : methods) {
            var dispatch = Pattern.compile("case \"(/[^\"]+)\"\\s*->\\s*" + method + "\\(").matcher(src);
            assertThat(dispatch.find()).as("%s is dispatched from a case label in VectorHandler.handle", method).isTrue();
            routes.add(VECTORS_PREFIX + dispatch.group(1));
        }

        assertThat(IdempotencyKeyGuard.KEYED_ROUTES)
            .as("every route whose handler calls keyedRequest (%s) is listed in KEYED_ROUTES", routes)
            .containsAll(routes);
        assertThat(routes).as("and the two sets agree").containsExactlyInAnyOrderElementsOf(IdempotencyKeyGuard.KEYED_ROUTES);
    }
}
