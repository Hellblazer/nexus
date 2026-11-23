// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23), linda's ship-blocker ruling of 2026-11-23: the process-wide registry, which every static
 * method serves, holds a {@code Gate3} that throws if it is consulted. A static path that reached {@code readerKey} would key on
 * the name for ever after the latch, so the static methods stay label to row lookups (correct in both postures) and any path
 * that needs the name-or-id choice is moved onto an injected instance. Three checks: the sentinel throws, each static method
 * runs without consulting it, and no main source outside the registry calls {@code readerKey} (the count a reviewer reads).
 */
class CollectionRegistryStaticPathTest {

    private static final String TENANT = "cr-static-path-tenant";
    private static final CollectionRow ROW = new CollectionRow("code", "1.1", "voyage-code-3", 1024, "live", "", 41L, "code__1-1__voyage-code-3__v1");

    @Test
    void theProcessWideRegistryThrowsIfItsGateIsConsulted() {
        assertThatThrownBy(() -> CollectionRegistry.shared().readerKey(ROW))
            .isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("holds no Gate3");
    }

    @Test
    void everyStaticMethodRunsWithoutConsultingTheGate() {
        // markKnown, cached, isKnown, evict and evictTenant are the static surface that needs no database; require, lookup and
        // requireWritable fill through the same instance methods (resolveLabel), which never read the gate either.
        CollectionRegistry.markKnown(TENANT, ROW.label(), ROW);
        assertThat(CollectionRegistry.isKnown(TENANT, ROW.label())).isTrue();
        assertThat(CollectionRegistry.cached(TENANT, ROW.label())).contains(ROW);
        assertThat(CollectionRegistry.shared().cachedById(TENANT, 41L)).contains(ROW);
        CollectionRegistry.evict(TENANT, ROW.label());
        assertThat(CollectionRegistry.isKnown(TENANT, ROW.label())).isFalse();
        assertThat(CollectionRegistry.shared().cachedById(TENANT, 41L)).isEmpty();
        CollectionRegistry.markKnown(TENANT, ROW.label(), ROW);
        assertThat(CollectionRegistry.shared().evictTenant(TENANT)).isEqualTo(1);
        assertThat(CollectionRegistry.isKnown(TENANT, ROW.label())).isFalse();
        CollectionRegistry.evictAll();
    }

    /**
     * The call sites of {@code readerKey} in {@code src/main}: none outside the registry that defines it. A reader that keys on
     * the name or the id (P3.5c) takes an injected registry, and this count rises with it; it is read in review, never raised
     * to silence this test.
     */
    @Test
    void noMainSourceOutsideTheRegistryCallsReaderKey() throws IOException {
        List<String> calls = new ArrayList<>();
        try (Stream<Path> walk = Files.walk(Path.of("src", "main", "java"))) {
            for (Path p : (Iterable<Path>) walk.filter(f -> f.toString().endsWith(".java"))::iterator) {
                if (p.getFileName().toString().equals("CollectionRegistry.java")) {
                    continue;
                }
                List<String> lines = Files.readAllLines(p);
                for (int i = 0; i < lines.size(); i++) {
                    String code = lines.get(i).strip();
                    if (code.contains("readerKey(") && !code.startsWith("*") && !code.startsWith("//")) {
                        calls.add(p + ":" + (i + 1));
                    }
                }
            }
        }
        assertThat(calls).as("readerKey call sites in src/main outside CollectionRegistry (reviewers count these)").isEmpty();
    }
}
