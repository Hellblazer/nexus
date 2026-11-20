// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.stream.Stream;

/**
 * RDR-229 P2.5 review M5: every main-code statement that WRITES {@code catalog_collections.lifecycle_state} is
 * listed here, with its eviction check, and a new one fails the build until it is listed.
 *
 * <p>Why: {@link CollectionRegistry#lifecycleState} answers "is it a quarantine collection" from a process-local
 * cache. That answer cannot go stale only because {@code 'quarantine'} is written at INSERT and never flipped to
 * or from afterwards. A writer that changes {@code lifecycle_state} in place would make the cache wrong unless it
 * evicts the row ({@link CollectionRegistry#evict}). Whoever adds a writer must add it to {@link #WRITERS} and
 * say how it keeps the cache right.
 *
 * <p>What is scanned: every {@code insertInto(CATALOG_COLLECTIONS ...)} and {@code update(CATALOG_COLLECTIONS ...)}
 * statement in {@code service/src/main/java} (comment-blanked, one statement to its closing semicolon) whose text
 * names {@code LIFECYCLE_STATE}: its column list, or its {@code SET}/{@code DO UPDATE} list. A SQL function that
 * writes the column is in a changelog, not here, and writes {@code 'quarantine'} at INSERT.
 */
class CollectionLifecycleWritersPinTest {

    private static final String CATALOG_REPOSITORY = "dev/nexus/service/db/CatalogRepository.java";

    /** Each writer, in source order, with how its effect on the registry cache is safe. */
    private static final Map<String, List<String>> WRITERS = Map.of(CATALOG_REPOSITORY, List.of(
        "insert  upsertCollection: a new row, state from the request (live by default); the DO UPDATE arm leaves "
            + "lifecycle_state out, so an existing row's state never changes. No eviction needed.",
        "insert  renameCollectionTxn target upsert: copies the SOURCE row's state onto the new name and sets it in "
            + "its DO UPDATE arm; renameCollection evicts oldName and newName after commit, and the state is not "
            + "flipped to or from quarantine (rename refuses a quarantine source and target).",
        "update  sweepGhostsAndMarkDormant, live to dormant: live and dormant only, never quarantine, so an "
            + "is-quarantine answer is unchanged; the sweep evicts the rows it deletes.",
        "update  sweepGhostsAndMarkDormant, dormant to live: same.",
        "insert  importCollectionsBatch: ON CONFLICT DO NOTHING, fresh rows only (an import never overwrites).",
        "insert  doImportCollection: the single-row form of the same."));

    @Test
    void everyWriterOfLifecycleStateIsListedWithItsEvictionCheck() throws IOException {
        Path root = Path.of("src", "main", "java");
        Map<String, Integer> actual = new TreeMap<>();
        int filesScanned = 0;
        try (Stream<Path> files = Files.walk(root)) {
            for (Path p : files.filter(f -> f.toString().endsWith(".java")).toList()) {
                filesScanned++;
                int n = writerStatements(Files.readString(p));
                if (n > 0) actual.put(root.relativize(p).toString().replace('\\', '/'), n);
            }
        }
        assertThat(filesScanned).as("non-vacuity: the walk saw the main tree").isGreaterThan(100);
        Map<String, Integer> declared = new TreeMap<>();
        WRITERS.forEach((file, list) -> declared.put(file, list.size()));

        assertThat(actual)
            .as("a writer of catalog_collections.lifecycle_state was added or removed: list it in WRITERS with how it "
                + "keeps CollectionRegistry's cached lifecycle_state right (evict it, or show it cannot touch "
                + "'quarantine'); the cache's \"cannot go stale\" argument depends on this list")
            .isEqualTo(declared);
    }

    /** The statements in {@code source} that insert into or update catalog_collections and name LIFECYCLE_STATE. */
    static int writerStatements(String source) {
        String blanked = RawSqlGateTest.blankComments(source);
        java.util.regex.Matcher m = java.util.regex.Pattern
            .compile("(insertInto|update)\\(\\s*(dev\\.nexus\\.service\\.jooq\\.nexus\\.Tables\\.)?CATALOG_COLLECTIONS\\b")
            .matcher(blanked);
        int count = 0;
        while (m.find()) {
            int i = m.start();
            int depth = 0;
            while (i < blanked.length()) {
                char c = blanked.charAt(i);
                if (c == '(' || c == '[' || c == '{') depth++;
                else if (c == ')' || c == ']' || c == '}') depth--;
                else if (c == ';' && depth <= 0) break;
                i++;
            }
            if (blanked.substring(m.start(), i).contains("LIFECYCLE_STATE")) count++;
        }
        return count;
    }

    @Test
    void theScannerSeesAWriterAndIgnoresAReader() {
        String writer = "ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.LIFECYCLE_STATE, \"dormant\").where(x).execute();";
        String insert = "ctx.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.LIFECYCLE_STATE)"
            + ".values(a, b).onConflict(c).doNothing().execute();";
        String reader = "ctx.select(CATALOG_COLLECTIONS.LIFECYCLE_STATE).from(CATALOG_COLLECTIONS).fetch();";
        String commented = "// ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.LIFECYCLE_STATE, x).execute();";
        String otherColumn = "ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.DISPLAY_NAME, x).execute();";

        assertThat(writerStatements(writer)).isEqualTo(1);
        assertThat(writerStatements(insert)).isEqualTo(1);
        assertThat(writerStatements(reader)).isZero();
        assertThat(writerStatements(commented)).isZero();
        assertThat(writerStatements(otherColumn)).isZero();
    }
}
