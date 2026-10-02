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
     * The reference-only writer is GONE from main (nexus-z0o2p.36): {@code upsertReferenceOnlyChunk},
     * its SQL builder and its write gate were deleted, because a reference-only row is a chunk with
     * no manifest row, the ownerless write this phase refuses, and no production path wrote one.
     * Tests build such rows with {@code PgContainerHelper#insertReferenceOnlyChunk}. These names must
     * stay absent from every main file, java or resource, so a writer cannot come back under its old
     * name, nor a stale comment suggest one exists.
     */
    private static final List<String> REMOVED_REFERENCE_ONLY_NAMES = List.of(
        "upsertReferenceOnlyChunk", "referenceOnlyInsertQuery", "REFERENCE_ONLY_WRITES_ENABLED");

    /**
     * The only {@code .retention()} column accessor main code may use is the content upsert's
     * {@code .set(ch.retention(), "full")}: the promotion of a row to full content. A write that binds
     * any other value, or a new use of the accessor, is a new retention writer and fails here until it
     * is reviewed (it would be a reference-only writer in everything but name).
     */
    private static final java.util.regex.Pattern RETENTION_ACCESSOR =
        java.util.regex.Pattern.compile("\\.retention\\(\\)(?!\\s*,\\s*\"full\"\\s*\\))");

    private OwnershipGuardCoverageScan() {
    }

    /**
     * No main file names a removed reference-only writer symbol, and no main Java binds a retention
     * other than {@code "full"}. Scans {@code src/main} (java and resources); test callers stay free.
     * A comment that names a removed symbol fails too, deliberately: the pin is on the name.
     */
    static void assertTheReferenceOnlyWriterIsAbsentFromMain() throws IOException {
        Path root = Path.of("src", "main");
        List<String> problems = problemsOfTheReferenceOnlyPin(root);
        assertThat(problems).as("reference-only writer in main").isEmpty();
    }

    /** The scan itself, over a root, so a test can run it against a planted tree. */
    static List<String> problemsOfTheReferenceOnlyPin(Path root) throws IOException {
        List<String> problems = new ArrayList<>();
        int scanned = 0;
        boolean sawTheRepository = false;
        try (Stream<Path> files = Files.walk(root)) {
            for (Path file : (Iterable<Path>) files.filter(Files::isRegularFile)::iterator) {
                scanned++;
                String name = file.getFileName().toString();
                String src = Files.readString(file);
                if (name.equals("PgVectorRepository.java") && src.contains("upsertChunksInternal")) {
                    sawTheRepository = true;
                }
                for (String removed : REMOVED_REFERENCE_ONLY_NAMES) {
                    if (src.contains(removed)) {
                        problems.add(file + " names " + removed + ", which was removed: a reference-only "
                            + "row is an ownerless chunk and the engine has no writer for one");
                    }
                }
                if (name.endsWith(".java") && RETENTION_ACCESSOR.matcher(src).find()) {
                    problems.add(file + " uses .retention() for something other than .set(.., \"full\"): "
                        + "a new retention writer needs review");
                }
            }
        }
        assertThat(scanned).as("non-vacuity: files were walked under %s", root).isGreaterThan(0);
        if (root.endsWith("main")) {
            assertThat(scanned).as("non-vacuity: the main sources were walked").isGreaterThan(50);
            assertThat(sawTheRepository)
                .as("non-vacuity: PgVectorRepository was read (else the retention rule saw nothing)")
                .isTrue();
        }
        return problems;
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
