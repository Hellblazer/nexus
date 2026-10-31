// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.TreeSet;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-236 Phase 0 (nexus-qyx35): pins {@link ChunkMetadataKeys} to the key table it is generated from,
 * {@code src/nexus/tables/chunk-metadata-keys.toml}. The test reads the TOML itself with a line parser
 * (every row's {@code match} and {@code emit} are single-line inline tables by the file's own rule),
 * so a hand edit to the generated file, or a table edit without regeneration, fails here and not in
 * review. It runs from the repo root's {@code service/} module directory, as the other repo-file pins do.
 *
 * <p>It also holds the one engine consumer that exists today, {@link ManifestLabelStage#ANSWERED_KEYS},
 * to the table: every key the label stage answers must be a DOCUMENT or POSITION key (this is the check
 * that would have caught {@code bib_openalex_id} and {@code bib_doi} missing from the Python allowlist).
 */
class ChunkMetadataKeysTest {

    private static final Path TABLE =
        Path.of("..", "src", "nexus", "tables", "chunk-metadata-keys.toml").normalize();

    private static final Pattern STRING = Pattern.compile("\"([^\"]*)\"");
    private static final Pattern MATCH_KEY = Pattern.compile("^match\\s*=\\s*\\{\\s*key\\s*=\\s*(.+?)\\s*\\}\\s*$");
    private static final Pattern EMIT_FIELD = Pattern.compile("\\b(class|kind)\\s*=\\s*\"([^\"]*)\"");

    private record Parsed(Map<String, Set<String>> metadataKeysByClass, int rows) {}

    private static List<String> strings(String fragment) {
        List<String> out = new ArrayList<>();
        Matcher m = STRING.matcher(fragment);
        while (m.find()) {
            out.add(m.group(1));
        }
        return out;
    }

    private static Parsed parse() throws IOException {
        assertThat(TABLE).as("the key table must exist at %s", TABLE).exists();
        Map<String, Set<String>> byClass = new TreeMap<>();
        List<String> keys = null;
        int rows = 0;
        for (String raw : Files.readAllLines(TABLE)) {
            String line = raw.strip();
            if (line.startsWith("#")) {
                continue;
            }
            if (line.equals("[[row]]")) {
                keys = null;
                rows++;
            } else {
                Matcher mk = MATCH_KEY.matcher(line);
                if (mk.matches()) {
                    keys = strings(mk.group(1));
                } else if (line.startsWith("emit")) {
                    assertThat(keys).as("an emit line must follow its match line: %s", line).isNotNull();
                    String cls = null;
                    String kind = null;
                    Matcher e = EMIT_FIELD.matcher(line);
                    while (e.find()) {
                        if (e.group(1).equals("class")) {
                            cls = e.group(2);
                        } else {
                            kind = e.group(2);
                        }
                    }
                    assertThat(cls).as("emit class in: %s", line).isNotNull();
                    if ("metadata-key".equals(kind)) {
                        byClass.computeIfAbsent(cls, c -> new TreeSet<>()).addAll(keys);
                    }
                }
            }
        }
        return new Parsed(byClass, rows);
    }

    @Test
    void theParserExaminedTheWholeTable() throws IOException {
        Parsed p = parse();
        // Non-vacuity: a parser that read nothing would pass every equality below with empty sets.
        assertThat(p.rows()).as("table rows examined").isGreaterThanOrEqualTo(30);
        int keys = p.metadataKeysByClass().values().stream().mapToInt(Set::size).sum();
        assertThat(keys).as("metadata keys examined").isGreaterThanOrEqualTo(45);
    }

    @Test
    void generatedSetsEqualTheTable() throws IOException {
        Parsed p = parse();
        Map<String, Set<String>> generated = Map.of(
            "CHUNK", ChunkMetadataKeys.CHUNK,
            "DOCUMENT", ChunkMetadataKeys.DOCUMENT,
            "POSITION", ChunkMetadataKeys.POSITION,
            "LEGACY", ChunkMetadataKeys.LEGACY);
        for (Map.Entry<String, Set<String>> e : generated.entrySet()) {
            assertThat(e.getValue())
                .as("%s keys: ChunkMetadataKeys.java must equal the %s rows of chunk-metadata-keys.toml; "
                    + "regenerate with scripts/gen_chunk_metadata_keys_java.py --write", e.getKey(), e.getKey())
                .isEqualTo(p.metadataKeysByClass().getOrDefault(e.getKey(), Set.of()));
        }
        // ROW is the one class the generator leaves out on purpose: the reaper's quarantine keys are
        // engine-owned and EngineOwnedPredicateIntegrationTest forbids a main source naming them.
        Set<String> emittedOrRow = new HashSet<>(generated.keySet());
        emittedOrRow.add("ROW");
        assertThat(p.metadataKeysByClass().keySet())
            .as("a class in the table the generator does not emit")
            .isSubsetOf(emittedOrRow);
    }

    @Test
    void theClassesAreDisjoint() {
        Set<String> seen = new HashSet<>();
        for (Set<String> keys : List.of(ChunkMetadataKeys.CHUNK, ChunkMetadataKeys.DOCUMENT,
                ChunkMetadataKeys.POSITION, ChunkMetadataKeys.LEGACY)) {
            for (String k : keys) {
                assertThat(seen.add(k)).as("key %s is in two classes", k).isTrue();
            }
        }
    }

    @Test
    void everyKeyTheLabelStageAnswersIsADocumentOrPositionKey() {
        Set<String> homed = new HashSet<>(ChunkMetadataKeys.DOCUMENT);
        homed.addAll(ChunkMetadataKeys.POSITION);
        assertThat(ManifestLabelStage.ANSWERED_KEYS)
            .as("ManifestLabelStage answers a key the table does not class as DOCUMENT or POSITION")
            .isSubsetOf(homed);
        assertThat(ManifestLabelStage.LABEL_KEYS).isSubsetOf(ChunkMetadataKeys.POSITION);
    }
}
