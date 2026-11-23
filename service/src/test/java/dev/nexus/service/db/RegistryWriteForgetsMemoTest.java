// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * The memo-invalidation lint (RDR-229 P3.5a, linda 6(d)). {@code WriteRegistryMemo} answers "this transaction read the row FOR KEY
 * SHARE", not "what the row is now", so a statement that UPDATEs or DELETEs a {@code catalog_collections} row must drop it
 * ({@code forget}, {@code forgetWriteMemo}) in the same transaction. Every such statement in {@code service/src/main}, outside
 * {@code CollectionTombstones} (which forgets at each of its own), has a forget within the 12 lines before it. An INSERT needs none: a
 * label the transaction read has a row, so an insert of it conflicts and never lands, and the memo holds no entry for a label with no row.
 */
class RegistryWriteForgetsMemoTest {

    private static final Pattern WRITE = Pattern.compile("\\b(?:update|deleteFrom)\\(CATALOG_COLLECTIONS\\)");
    private static final Pattern FORGET = Pattern.compile("forgetWriteMemo|WriteRegistryMemo\\.forget");

    @Test
    void everyUpdateOrDeleteOfTheRegistryRowDropsTheMemoFirst() throws IOException {
        List<String> missing = new ArrayList<>();
        int examined = 0;
        try (Stream<Path> walk = Files.walk(Path.of("src/main/java"))) {
            for (Path f : walk.filter(p -> p.toString().endsWith(".java")).sorted().toList()) {
                if (f.getFileName().toString().equals("CollectionTombstones.java")) {
                    continue;
                }
                List<String> lines = Files.readAllLines(f);
                for (int i = 0; i < lines.size(); i++) {
                    String line = lines.get(i);
                    if (line.trim().startsWith("//") || line.trim().startsWith("*")) {
                        continue;
                    }
                    Matcher m = WRITE.matcher(line);
                    if (!m.find()) {
                        continue;
                    }
                    examined++;
                    boolean forgot = false;
                    for (int j = Math.max(0, i - 12); j < i; j++) {
                        forgot |= FORGET.matcher(lines.get(j)).find();
                    }
                    if (!forgot) {
                        missing.add(f.getFileName() + ":" + (i + 1));
                    }
                }
            }
        }
        System.err.printf("registry_write_forgets_memo examined=%d missing=%s%n", examined, missing);
        assertThat(examined).as("non-vacuity: the registry-row writes found outside CollectionTombstones").isGreaterThanOrEqualTo(5);
        assertThat(missing).as("an UPDATE or DELETE of a catalog_collections row with no memo forget before it").isEmpty();
    }
}
