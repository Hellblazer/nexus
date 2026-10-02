// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-223 Phase 3 Step 2 (nexus-z0o2p.24): the ownership guard is opt-in at the repository (a null
 * guard is the unguarded write the contract tests and fixtures rely on), so a handler that forgets
 * to pass one would fail OPEN. This scan closes that for the four guarded methods named in
 * {@code GUARDED_METHODS}, by a text match over the main sources: every call of one of them
 * outside the repository itself must be in {@code VectorHandler} and must build its guard with
 * {@code ownershipGuard(}. It is a name match, not a proof about routes: a new repository method
 * that writes chunks, a direct SQL insert, or a changeset that inserts chunks is invisible to it
 * (the SQL chunk inserters are listed, with reasons, in the allowlist that {@code OwnershipGuard}'s
 * Javadoc names).
 *
 * <p>Method references are caught too: a {@code repo::upsertChunks} cannot carry a guard, so any
 * {@code ::<guarded method>} outside the repository fails, in any file. A call through reflection
 * or a lambda that names the method as a string is not matched.
 *
 * <p>A new caller in another class fails here, with its file named, until it either routes through
 * {@code VectorHandler} or is added to {@link #ALLOWED_CALLERS} with a reason it cannot write an
 * ownerless chunk.
 */
final class OwnershipGuardCoverageScan {

    private static final List<String> GUARDED_METHODS = List.of(
        "upsertChunksWithTokens(", "upsertChunksWithVectors(", "putWithTokens(", "upsertChunks(");

    /** File name to the reason it may call a guarded method without a guard. Empty on purpose. */
    private static final java.util.Map<String, String> ALLOWED_CALLERS = java.util.Map.of();

    private OwnershipGuardCoverageScan() {
    }

    static void assertEveryGuardedRepositoryCallPassesAGuard() throws IOException {
        Path root = Path.of("src", "main", "java", "dev", "nexus", "service");
        List<String> problems = new ArrayList<>();
        int handlerCalls = 0;
        try (Stream<Path> files = Files.walk(root)) {
            for (Path file : (Iterable<Path>) files.filter(f -> f.toString().endsWith(".java"))::iterator) {
                String name = file.getFileName().toString();
                if (name.equals("PgVectorRepository.java")) {
                    continue;
                }
                String src = Files.readString(file);
                for (String method : GUARDED_METHODS) {
                    String reference = "::" + method.substring(0, method.length() - 1);
                    int ref = 0;
                    while ((ref = src.indexOf(reference, ref)) >= 0) {
                        int after = ref + reference.length();
                        // "::upsertChunks" must not match the prefix of "::upsertChunksWithTokens"
                        if (after >= src.length() || !Character.isJavaIdentifierPart(src.charAt(after))) {
                            problems.add(name + ": method reference " + reference + " cannot carry an ownershipGuard");
                        }
                        ref = after;
                    }
                    String needle = "." + method;
                    int from = 0;
                    while ((from = src.indexOf(needle, from)) >= 0) {
                        int end = src.indexOf(';', from);
                        String statement = src.substring(from, end < 0 ? src.length() : end);
                        if (name.equals("VectorHandler.java")) {
                            handlerCalls++;
                            if (!statement.contains("ownershipGuard(")) {
                                problems.add(name + ": " + method + " called without ownershipGuard(): " + statement.replaceAll("\\s+", " "));
                            }
                        } else if (!ALLOWED_CALLERS.containsKey(name)) {
                            problems.add(name + " calls " + method + " and is neither VectorHandler nor allowed");
                        }
                        from += needle.length();
                    }
                }
            }
        }
        assertThat(problems).as("guard coverage").isEmpty();
        assertThat(handlerCalls).as("non-vacuity: VectorHandler's three guarded call sites were found").isGreaterThanOrEqualTo(3);
    }
}
