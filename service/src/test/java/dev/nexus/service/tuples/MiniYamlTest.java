// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.tuples;

import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** nexus-em75s.3 — the RDR-205 template YAML subset parser. */
class MiniYamlTest {

    @Test
    void parsesScalarsAndNestedMapping() {
        String yaml = """
                name: ledger/<session_id>
                id_from: keys
                take:
                  enabled: false
                retention_seconds: 7776000
                """;
        Map<String, Object> doc = MiniYaml.parse("t.yaml", yaml);
        assertEquals("ledger/<session_id>", doc.get("name"));
        assertEquals("keys", doc.get("id_from"));
        assertEquals(7776000L, doc.get("retention_seconds"));
        @SuppressWarnings("unchecked")
        Map<String, Object> take = (Map<String, Object>) doc.get("take");
        assertEquals(Boolean.FALSE, take.get("enabled"));
    }

    @Test
    void parsesBlockSequenceOfScalars() {
        String yaml = """
                keys:
                  - agent_id
                  - kind
                """;
        Map<String, Object> doc = MiniYaml.parse("t.yaml", yaml);
        assertEquals(List.of("agent_id", "kind"), doc.get("keys"));
    }

    @Test
    void parsesFlowSequence() {
        Map<String, Object> doc = MiniYaml.parse("t.yaml", "values: [agent, instance]\n");
        assertEquals(List.of("agent", "instance"), doc.get("values"));
    }

    @Test
    void parsesEmptyFlowSequence() {
        Map<String, Object> doc = MiniYaml.parse("t.yaml", "id_dims: []\n");
        assertEquals(List.of(), doc.get("id_dims"));
    }

    @Test
    void skipsBlankLinesAndFullLineComments() {
        String yaml = """
                # a comment

                name: mailbox/<address>

                # another comment
                id_from: keys+nonce
                """;
        Map<String, Object> doc = MiniYaml.parse("t.yaml", yaml);
        assertEquals("mailbox/<address>", doc.get("name"));
        assertEquals("keys+nonce", doc.get("id_from"));
    }

    @Test
    void nestedDimensionsMapping() {
        String yaml = """
                dimensions:
                  from:
                    type: string
                    required: true
                  kind:
                    type: string
                """;
        Map<String, Object> doc = MiniYaml.parse("t.yaml", yaml);
        @SuppressWarnings("unchecked")
        Map<String, Object> dims = (Map<String, Object>) doc.get("dimensions");
        @SuppressWarnings("unchecked")
        Map<String, Object> from = (Map<String, Object>) dims.get("from");
        assertEquals("string", from.get("type"));
        assertEquals(Boolean.TRUE, from.get("required"));
    }

    @Test
    void tabsAreRejected() {
        var ex = assertThrows(TemplateRegistryException.class,
                () -> MiniYaml.parse("t.yaml", "name:\tledger\n"));
        assertTrue(ex.getMessage().contains("t.yaml"));
        assertTrue(ex.getMessage().contains("tab"));
    }

    @Test
    void inconsistentIndentationIsRejected() {
        String yaml = """
                take:
                  enabled: false
                   max_attempts: 3
                """;
        var ex = assertThrows(TemplateRegistryException.class, () -> MiniYaml.parse("t.yaml", yaml));
        assertTrue(ex.getMessage().contains("t.yaml"));
    }

    @Test
    void emptySequenceItemIsRejected() {
        String yaml = """
                keys:
                  -
                """;
        assertThrows(TemplateRegistryException.class, () -> MiniYaml.parse("t.yaml", yaml));
    }
}
