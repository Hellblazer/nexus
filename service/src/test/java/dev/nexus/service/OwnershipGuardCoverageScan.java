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
 * to pass one, or a new route that writes chunks, would fail OPEN. This scan closes that by
 * reading the main sources: every call of a guarded repository method outside the repository itself
 * must be in {@code VectorHandler} and must build its guard with {@code ownershipGuard(}.
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

    /**
     * The one repository method that writes an ownerless chunk by design (reference-only rows, for
     * fixtures). Its route is retired (410), it takes no guard, and it is not in
     * {@link #GUARDED_METHODS}, so nothing else would notice a main caller. Decision (nexus-z0o2p.36):
     * the method stays, because ~17 test fixtures across four test classes write reference-only rows
     * through it; the pin below keeps every main source other than the repository from naming it.
     */
    private static final String REFERENCE_ONLY_METHOD = "upsertReferenceOnlyChunk";

    private OwnershipGuardCoverageScan() {
    }

    /**
     * No main-source file outside {@code PgVectorRepository} names the reference-only writer, as a
     * call, a method reference, or in any other position. Scans {@code src/main} only, so test
     * callers stay free. A comment that names it fails too, deliberately: the pin is on the name.
     */
    static void assertNoMainSourceOutsideTheRepositoryNamesTheReferenceOnlyWriter() throws IOException {
        Path root = Path.of("src", "main");
        List<String> problems = new ArrayList<>();
        boolean repositoryDefinesIt = false;
        int scanned = 0;
        try (Stream<Path> files = Files.walk(root)) {
            for (Path file : (Iterable<Path>) files.filter(f -> f.toString().endsWith(".java"))::iterator) {
                scanned++;
                String src = Files.readString(file);
                if (file.getFileName().toString().equals("PgVectorRepository.java")) {
                    repositoryDefinesIt = src.contains("public void " + REFERENCE_ONLY_METHOD + "(");
                } else if (src.contains(REFERENCE_ONLY_METHOD)) {
                    problems.add(file.getFileName() + " names " + REFERENCE_ONLY_METHOD
                        + ", which writes an ownerless chunk and takes no ownership guard");
                }
            }
        }
        assertThat(scanned).as("non-vacuity: the main sources were walked").isGreaterThan(50);
        assertThat(repositoryDefinesIt)
            .as("non-vacuity: PgVectorRepository still defines %s (else this pin has nothing to pin)",
                REFERENCE_ONLY_METHOD)
            .isTrue();
        assertThat(problems).as("reference-only writer callers in main").isEmpty();
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
