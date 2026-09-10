// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.tuples;

import org.junit.jupiter.api.Test;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * nexus-em75s.3 — field-level validation of the RDR-205 document shape.
 * Uses hand-built {@code Map} documents so these tests are independent of
 * {@link MiniYaml}'s own syntax (that parser is covered separately by
 * {@link MiniYamlTest}).
 */
class TemplateSchemaParserTest {

    /** A minimal valid mailbox-shaped document, mutated per test. */
    private static Map<String, Object> validDoc() {
        Map<String, Object> doc = new LinkedHashMap<>();
        doc.put("name", "mailbox/<address>");
        doc.put("keys", List.of("to"));
        Map<String, Object> from = new LinkedHashMap<>();
        from.put("type", "string");
        from.put("required", true);
        Map<String, Object> dims = new LinkedHashMap<>();
        dims.put("from", from);
        doc.put("dimensions", dims);
        doc.put("id_from", "keys+nonce");
        doc.put("id_dims", List.of("from"));
        Map<String, Object> take = new LinkedHashMap<>();
        take.put("enabled", true);
        take.put("max_attempts", 3L);
        take.put("max_lease_seconds", 900L);
        doc.put("take", take);
        doc.put("retention_seconds", 604800L);
        return doc;
    }

    @Test
    void parsesAValidDocument() {
        TemplateSchema t = TemplateSchemaParser.parse("mailbox.yaml", validDoc());
        assertEquals("mailbox/<address>", t.name());
        assertEquals(List.of("mailbox", "<address>"), t.nameSegments());
        assertEquals(List.of("to"), t.keys());
        assertEquals(TemplateSchema.IdFrom.KEYS_NONCE, t.idFrom());
        assertEquals(List.of("from"), t.idDims());
        assertTrue(t.take().enabled());
        assertEquals(604800L, t.retentionSeconds());
        assertFalse(t.isLiteral());
    }

    @Test
    void literalNameHasNoParamSegment() {
        Map<String, Object> doc = validDoc();
        doc.put("name", "ledger/fixed");
        doc.put("id_dims", List.of());
        TemplateSchema t = TemplateSchemaParser.parse("t.yaml", doc);
        assertTrue(t.isLiteral());
    }

    @Test
    void missingRetentionSecondsNamesFileAndField() {
        Map<String, Object> doc = validDoc();
        doc.remove("retention_seconds");
        var ex = assertThrows(TemplateRegistryException.class,
                () -> TemplateSchemaParser.parse("broken.yaml", doc));
        assertTrue(ex.getMessage().contains("broken.yaml"), ex.getMessage());
        assertTrue(ex.getMessage().contains("retention_seconds"), ex.getMessage());
    }

    @Test
    void zeroRetentionSecondsIsABreach() {
        Map<String, Object> doc = validDoc();
        doc.put("retention_seconds", 0L);
        var ex = assertThrows(TemplateRegistryException.class,
                () -> TemplateSchemaParser.parse("broken.yaml", doc));
        assertTrue(ex.getMessage().contains("retention_seconds"), ex.getMessage());
    }

    @Test
    void unknownTopLevelFieldIsABreach() {
        Map<String, Object> doc = validDoc();
        doc.put("tier", "hot");
        var ex = assertThrows(TemplateRegistryException.class,
                () -> TemplateSchemaParser.parse("broken.yaml", doc));
        assertTrue(ex.getMessage().contains("broken.yaml"), ex.getMessage());
        assertTrue(ex.getMessage().contains("tier"), ex.getMessage());
        assertTrue(ex.getMessage().contains("unknown field"), ex.getMessage());
    }

    @Test
    void idDimsReferencingUndefinedDimensionIsABreach() {
        Map<String, Object> doc = validDoc();
        doc.put("id_dims", List.of("nonexistent"));
        var ex = assertThrows(TemplateRegistryException.class,
                () -> TemplateSchemaParser.parse("broken.yaml", doc));
        assertTrue(ex.getMessage().contains("id_dims"), ex.getMessage());
        assertTrue(ex.getMessage().contains("nonexistent"), ex.getMessage());
    }

    @Test
    void idDimsReferencingNonRequiredDimensionIsABreach() {
        Map<String, Object> doc = validDoc();
        @SuppressWarnings("unchecked")
        Map<String, Object> dims = (Map<String, Object>) doc.get("dimensions");
        @SuppressWarnings("unchecked")
        Map<String, Object> from = (Map<String, Object>) dims.get("from");
        from.put("required", false);
        var ex = assertThrows(TemplateRegistryException.class,
                () -> TemplateSchemaParser.parse("broken.yaml", doc));
        assertTrue(ex.getMessage().contains("id_dims"), ex.getMessage());
        assertTrue(ex.getMessage().contains("required"), ex.getMessage());
    }

    @Test
    void emptyParameterSegmentInNameIsABreach() {
        Map<String, Object> doc = validDoc();
        doc.put("name", "ledger/<>");
        var ex = assertThrows(TemplateRegistryException.class,
                () -> TemplateSchemaParser.parse("broken.yaml", doc));
        assertTrue(ex.getMessage().contains("name"), ex.getMessage());
    }

    @Test
    void emptyKeysListIsABreach() {
        Map<String, Object> doc = validDoc();
        doc.put("keys", List.of());
        var ex = assertThrows(TemplateRegistryException.class,
                () -> TemplateSchemaParser.parse("broken.yaml", doc));
        assertTrue(ex.getMessage().contains("keys"), ex.getMessage());
    }

    @Test
    void invalidIdFromIsABreach() {
        Map<String, Object> doc = validDoc();
        doc.put("id_from", "bogus");
        var ex = assertThrows(TemplateRegistryException.class,
                () -> TemplateSchemaParser.parse("broken.yaml", doc));
        assertTrue(ex.getMessage().contains("id_from"), ex.getMessage());
    }

    @Test
    void missingTakeEnabledIsABreach() {
        Map<String, Object> doc = validDoc();
        Map<String, Object> take = new LinkedHashMap<>();
        doc.put("take", take);
        var ex = assertThrows(TemplateRegistryException.class,
                () -> TemplateSchemaParser.parse("broken.yaml", doc));
        assertTrue(ex.getMessage().contains("take.enabled"), ex.getMessage());
    }
}
