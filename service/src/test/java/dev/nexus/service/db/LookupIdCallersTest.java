// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.TreeSet;
import java.util.regex.Pattern;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23, review follow-up): {@code CollectionRegistry#lookupId} answers from the cache and takes no lock, so it is
 * a read for the route boundary and never a writer's registry check. {@code resolveId} and {@code cachedById} are package-private; this pins
 * the one caller of {@code lookupId} outside the {@code db} package: the route boundary's filter. A new caller outside it fails here and has to be
 * argued (a write must read its id {@code FOR KEY SHARE}, {@code CollectionRegistry#requireIdForWrite}).
 */
class LookupIdCallersTest {

    private static final Pattern CALL = Pattern.compile("\\.\\s*lookupId\\s*\\(");

    @Test
    void theRouteBoundaryIsTheOnlyCallerOfLookupIdOutsideTheRegistryPackage() throws IOException {
        TreeSet<String> callers = new TreeSet<>();
        int scanned = 0;
        try (Stream<Path> walk = Files.walk(Path.of("src/main/java"))) {
            for (Path f : walk.filter(p -> p.toString().endsWith(".java")).toList()) {
                scanned++;
                String src = Files.readString(f).replaceAll("(?s)/\\*.*?\\*/", "").replaceAll("//[^\\n]*", "");
                if (CALL.matcher(src).find() && !f.getParent().endsWith(Path.of("dev/nexus/service/db"))) {
                    callers.add(f.getFileName().toString());
                }
            }
        }
        assertThat(scanned).as("non-vacuity: the main sources scanned").isGreaterThan(100);
        assertThat(callers).containsExactly("CollectionSelectorFilter.java");
    }
}
