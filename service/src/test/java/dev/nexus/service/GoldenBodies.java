// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.databind.node.ObjectNode;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd: golden RESPONSE BODIES of the census routes, committed under {@code service/src/test/resources}, so a
 * change to a body's wire shape fails a test until the golden is updated on purpose. One file holds one entry per fixture
 * scenario (keys sorted, an {@code _readme} first saying what reads the file); a test compares the live route body to its
 * entry and NEVER writes by default.
 *
 * <p>REGENERATE (writes the entries the run produces, drops entries of scenarios the table no longer holds), then review the
 * diff of the golden like any other change:
 * {@code scripts/mvnw-leased.sh -B -q test -Dnexus.golden.write=true -Dtest=IndexRunCensusParityTest,NextSeqDriftParityTest}
 */
final class GoldenBodies {

    /** The system property that switches a run from comparing to writing. */
    static final String WRITE_PROPERTY = "nexus.golden.write";

    private static final ObjectMapper MAPPER = new ObjectMapper()
        .enable(SerializationFeature.ORDER_MAP_ENTRIES_BY_KEYS).enable(SerializationFeature.INDENT_OUTPUT);

    private final Path file;
    private final String readme;
    private final String section;

    /** @param section the top-level object of the file that holds one entry per scenario or case ({@code scenarios}, {@code cases}) */
    GoldenBodies(String resourceName, String section, String readme) {
        this.file = Path.of("src", "test", "resources", resourceName);
        this.section = section;
        this.readme = readme;
    }

    static boolean writing() {
        return Boolean.getBoolean(WRITE_PROPERTY);
    }

    private static JsonNode canonical(Object body) throws IOException {
        return MAPPER.readTree(MAPPER.writeValueAsString(body));
    }

    private ObjectNode load() throws IOException {
        if (!Files.exists(file)) return MAPPER.createObjectNode();
        return (ObjectNode) MAPPER.readTree(Files.readString(file));
    }

    private void store(ObjectNode root) throws IOException {
        root.put("_readme", readme);
        Files.writeString(file, MAPPER.writeValueAsString(MAPPER.treeToValue(root, java.util.Map.class)) + "\n");
    }

    /** Compares {@code body} to the committed entry {@code key}; in write mode stores it instead. */
    synchronized void check(String key, Object body) throws IOException {
        JsonNode live = canonical(body);
        ObjectNode root = load();
        if (writing()) {
            ((ObjectNode) root.with(section)).set(key, live);
            store(root);
            return;
        }
        JsonNode golden = root.path(section).path(key);
        assertThat(golden.isMissingNode()).as("%s has no entry '%s'; run with -D%s=true to write it", file, key, WRITE_PROPERTY).isFalse();
        assertThat(MAPPER.writeValueAsString(live)).as("the route body of '%s' differs from %s (a wire-shape change; regenerate with -D%s=true "
            + "only if it is deliberate)", key, file, WRITE_PROPERTY).isEqualTo(MAPPER.writeValueAsString(golden));
    }

    /** The golden holds exactly {@code keys}: compare mode fails on a missing or an extra entry; write mode drops the extras. */
    synchronized void checkKeys(Set<String> keys) throws IOException {
        ObjectNode root = load();
        var present = new ArrayList<String>();
        root.path(section).fieldNames().forEachRemaining(present::add);
        if (writing()) {
            ObjectNode entries = root.with(section);
            for (String extra : present) if (!keys.contains(extra)) entries.remove(extra);
            store(root);
            return;
        }
        assertThat(present).as("entries of %s", file).containsExactlyInAnyOrderElementsOf(keys);
        assertThat(root.path("_readme").asText()).as("the readme of %s", file).isEqualTo(readme);
    }
}
