// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.tuples;

import org.junit.jupiter.api.Test;

import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
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
        doc.put("name", "session-log/fixed");
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
        doc.put("name", "session-log/<>");
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

    // ── keys mapping form: a key may carry values (nexus-em75s.36) ─────

    @Test
    void keysMappingFormParsesUnconstrainedAndConstrainedKeys() {
        Map<String, Object> doc = validDoc();
        Map<String, Object> keysDoc = new LinkedHashMap<>();
        keysDoc.put("to", null);
        Map<String, Object> kindSpec = new LinkedHashMap<>();
        kindSpec.put("values", List.of("start", "report"));
        keysDoc.put("kind", kindSpec);
        doc.put("keys", keysDoc);

        TemplateSchema t = TemplateSchemaParser.parse("t.yaml", doc);
        assertEquals(List.of("to", "kind"), t.keys());
        assertEquals(Map.of("kind", List.of("start", "report")), t.keyValues());
    }

    @Test
    void keysMappingFormEntryWithUnknownSpecFieldIsABreach() {
        Map<String, Object> doc = validDoc();
        Map<String, Object> keysDoc = new LinkedHashMap<>();
        Map<String, Object> spec = new LinkedHashMap<>();
        spec.put("required", true);
        keysDoc.put("to", spec);
        doc.put("keys", keysDoc);

        var ex = assertThrows(TemplateRegistryException.class,
                () -> TemplateSchemaParser.parse("broken.yaml", doc));
        assertTrue(ex.getMessage().contains("keys.to"), ex.getMessage());
        assertTrue(ex.getMessage().contains("unknown field"), ex.getMessage());
    }

    @Test
    void keysMappingFormEntryWithEmptyValuesListIsABreach() {
        Map<String, Object> doc = validDoc();
        Map<String, Object> keysDoc = new LinkedHashMap<>();
        Map<String, Object> spec = new LinkedHashMap<>();
        spec.put("values", List.of());
        keysDoc.put("to", spec);
        doc.put("keys", keysDoc);

        var ex = assertThrows(TemplateRegistryException.class,
                () -> TemplateSchemaParser.parse("broken.yaml", doc));
        assertTrue(ex.getMessage().contains("keys.to.values"), ex.getMessage());
    }

    @Test
    void keysFlatListFormLeavesKeyValuesEmpty() {
        TemplateSchema t = TemplateSchemaParser.parse("mailbox.yaml", validDoc());
        assertTrue(t.keyValues().isEmpty());
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

    // ── max_body_bytes (bead nexus-r7xao) ────────────────────────────────────

    @Test
    void maxBodyBytesAbsent_leavesFieldNull() {
        TemplateSchema t = TemplateSchemaParser.parse("mailbox.yaml", validDoc());
        assertEquals(null, t.maxBodyBytes());
    }

    @Test
    void maxBodyBytesWithinRange_parsesVerbatim() {
        Map<String, Object> doc = validDoc();
        doc.put("max_body_bytes", 10L);
        TemplateSchema t = TemplateSchemaParser.parse("mailbox.yaml", doc);
        assertEquals(10L, t.maxBodyBytes());
    }

    @Test
    void maxBodyBytesZero_parsesAsZero_notNull() {
        Map<String, Object> doc = validDoc();
        doc.put("max_body_bytes", 0L);
        TemplateSchema t = TemplateSchemaParser.parse("session-log.yaml", doc);
        assertEquals(0L, t.maxBodyBytes());
    }

    @Test
    void maxBodyBytesAtGlobalCap_parsesVerbatim() {
        Map<String, Object> doc = validDoc();
        doc.put("max_body_bytes", (long) dev.nexus.service.db.TupleLimits.MAX_BODY_BYTES);
        TemplateSchema t = TemplateSchemaParser.parse("mailbox.yaml", doc);
        assertEquals((long) dev.nexus.service.db.TupleLimits.MAX_BODY_BYTES, t.maxBodyBytes());
    }

    @Test
    void maxBodyBytesOneOverGlobalCap_isABreach() {
        Map<String, Object> doc = validDoc();
        doc.put("max_body_bytes", (long) dev.nexus.service.db.TupleLimits.MAX_BODY_BYTES + 1);
        var ex = assertThrows(TemplateRegistryException.class,
                () -> TemplateSchemaParser.parse("broken.yaml", doc));
        assertTrue(ex.getMessage().contains("max_body_bytes"), ex.getMessage());
    }

    @Test
    void maxBodyBytesNegative_isABreach() {
        Map<String, Object> doc = validDoc();
        doc.put("max_body_bytes", -1L);
        var ex = assertThrows(TemplateRegistryException.class,
                () -> TemplateSchemaParser.parse("broken.yaml", doc));
        assertTrue(ex.getMessage().contains("max_body_bytes"), ex.getMessage());
    }

    @Test
    void maxBodyBytesWrongType_isABreach() {
        Map<String, Object> doc = validDoc();
        doc.put("max_body_bytes", "not-a-number");
        var ex = assertThrows(TemplateRegistryException.class,
                () -> TemplateSchemaParser.parse("broken.yaml", doc));
        assertTrue(ex.getMessage().contains("max_body_bytes"), ex.getMessage());
    }

    // ── lock (RDR-211 Phase 1 Step 1, bead nexus-rplay.3) ─────────────────────

    @Test
    void lockAbsent_defaultsFalse() {
        TemplateSchema t = TemplateSchemaParser.parse("mailbox.yaml", validDoc());
        assertFalse(t.lock());
    }

    @Test
    void lockTrue_parsesVerbatim() {
        Map<String, Object> doc = validDoc();
        doc.put("lock", Boolean.TRUE);
        TemplateSchema t = TemplateSchemaParser.parse("lock.yaml", doc);
        assertTrue(t.lock());
    }

    @Test
    void lockFalse_parsesVerbatim() {
        Map<String, Object> doc = validDoc();
        doc.put("lock", Boolean.FALSE);
        TemplateSchema t = TemplateSchemaParser.parse("lock.yaml", doc);
        assertFalse(t.lock());
    }

    @Test
    void lockWrongType_isABreach() {
        Map<String, Object> doc = validDoc();
        doc.put("lock", "not-a-boolean");
        var ex = assertThrows(TemplateRegistryException.class,
                () -> TemplateSchemaParser.parse("broken.yaml", doc));
        assertTrue(ex.getMessage().contains("lock"), ex.getMessage());
    }

    // ── max_live_rows (RDR-211 Phase 1 Step 1, bead nexus-rplay.5) ───────────

    @Test
    void maxLiveRowsAbsent_leavesFieldNull() {
        TemplateSchema t = TemplateSchemaParser.parse("mailbox.yaml", validDoc());
        assertEquals(null, t.maxLiveRows());
    }

    @Test
    void maxLiveRowsPositive_parsesVerbatim() {
        Map<String, Object> doc = validDoc();
        doc.put("max_live_rows", 500L);
        TemplateSchema t = TemplateSchemaParser.parse("board.yaml", doc);
        assertEquals(500L, t.maxLiveRows());
    }

    @Test
    void maxLiveRowsZero_isABreach() {
        Map<String, Object> doc = validDoc();
        doc.put("max_live_rows", 0L);
        var ex = assertThrows(TemplateRegistryException.class,
                () -> TemplateSchemaParser.parse("broken.yaml", doc));
        assertTrue(ex.getMessage().contains("max_live_rows"), ex.getMessage());
    }

    @Test
    void maxLiveRowsNegative_isABreach() {
        Map<String, Object> doc = validDoc();
        doc.put("max_live_rows", -1L);
        var ex = assertThrows(TemplateRegistryException.class,
                () -> TemplateSchemaParser.parse("broken.yaml", doc));
        assertTrue(ex.getMessage().contains("max_live_rows"), ex.getMessage());
    }

    @Test
    void maxLiveRowsWrongType_isABreach() {
        Map<String, Object> doc = validDoc();
        doc.put("max_live_rows", "not-a-number");
        var ex = assertThrows(TemplateRegistryException.class,
                () -> TemplateSchemaParser.parse("broken.yaml", doc));
        assertTrue(ex.getMessage().contains("max_live_rows"), ex.getMessage());
    }

    // ── claim_log_ttl_seconds (RDR-211 Phase 1 Step 1, bead nexus-rplay.6) ───
    //
    // Only the syntactic "positive integer" shape lives here. "May only shorten
    // the engine default" and "must exceed this template's own retention_seconds
    // by more than one sweep interval" both need the registry's resolved default,
    // which this parser never sees -- TemplateRegistryTest owns those.

    @Test
    void claimLogTtlSecondsAbsent_leavesFieldNull() {
        TemplateSchema t = TemplateSchemaParser.parse("mailbox.yaml", validDoc());
        assertEquals(null, t.claimLogTtlSeconds());
    }

    @Test
    void claimLogTtlSecondsPositive_parsesVerbatim() {
        Map<String, Object> doc = validDoc();
        doc.put("claim_log_ttl_seconds", 3600L);
        TemplateSchema t = TemplateSchemaParser.parse("queue.yaml", doc);
        assertEquals(3600L, t.claimLogTtlSeconds());
    }

    @Test
    void claimLogTtlSecondsZero_isABreach() {
        Map<String, Object> doc = validDoc();
        doc.put("claim_log_ttl_seconds", 0L);
        var ex = assertThrows(TemplateRegistryException.class,
                () -> TemplateSchemaParser.parse("broken.yaml", doc));
        assertTrue(ex.getMessage().contains("claim_log_ttl_seconds"), ex.getMessage());
    }

    @Test
    void claimLogTtlSecondsNegative_isABreach() {
        Map<String, Object> doc = validDoc();
        doc.put("claim_log_ttl_seconds", -1L);
        var ex = assertThrows(TemplateRegistryException.class,
                () -> TemplateSchemaParser.parse("broken.yaml", doc));
        assertTrue(ex.getMessage().contains("claim_log_ttl_seconds"), ex.getMessage());
    }

    @Test
    void claimLogTtlSecondsWrongType_isABreach() {
        Map<String, Object> doc = validDoc();
        doc.put("claim_log_ttl_seconds", "not-a-number");
        var ex = assertThrows(TemplateRegistryException.class,
                () -> TemplateSchemaParser.parse("broken.yaml", doc));
        assertTrue(ex.getMessage().contains("claim_log_ttl_seconds"), ex.getMessage());
    }

    // ── doc block (bead nexus-mdbds.2.10, RDR-239 Technical Design item 1) ──────────

    /** A doc block that fits {@link #validDoc()}: keys {@code to}, dimension {@code from}. */
    private static Map<String, Object> docBlock() {
        Map<String, Object> d = new LinkedHashMap<>();
        d.put("summary", "Messages addressed to one agent or session.");
        d.put("writers", new ArrayList<>(List.of("Any session, by tuple_out")));
        d.put("readers", new ArrayList<>(List.of("The addressee claims with in")));
        Map<String, Object> fields = new LinkedHashMap<>();
        fields.put("to", "the addressee");
        fields.put("from", "the sender");
        d.put("fields", fields);
        d.put("body", "free text");
        Map<String, Object> ex = new LinkedHashMap<>();
        ex.put("subspace", "mailbox/someone");
        ex.put("nonce", "n1");
        Map<String, Object> keys = new LinkedHashMap<>();
        keys.put("to", "someone");
        ex.put("keys", keys);
        Map<String, Object> dims = new LinkedHashMap<>();
        dims.put("from", "other");
        ex.put("dims", dims);
        ex.put("body", "hello");
        d.put("example", ex);
        d.put("see_also", new ArrayList<>(List.of("RDR-239")));
        return d;
    }

    private static Map<String, Object> docDocument(Map<String, Object> block) {
        Map<String, Object> doc = validDoc();
        doc.put("doc", block);
        return doc;
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> sub(Map<String, Object> m, String key) {
        return (Map<String, Object>) m.get(key);
    }

    private static TemplateRegistryException refused(Map<String, Object> document) {
        return assertThrows(TemplateRegistryException.class,
                () -> TemplateSchemaParser.parse("broken.yaml", document));
    }

    private static void assertNames(TemplateRegistryException ex, String field) {
        assertEquals(field, ex.field(), ex.getMessage());
        assertTrue(ex.getMessage().contains("broken.yaml"), ex.getMessage());
    }

    @Test
    void docWithEveryFieldParses() {
        TemplateSchema t = TemplateSchemaParser.parse("mailbox.yaml", docDocument(docBlock()));
        TemplateDoc d = t.doc();
        assertNotNull(d);
        assertEquals("Messages addressed to one agent or session.", d.summary());
        assertEquals(List.of("Any session, by tuple_out"), d.writers());
        assertEquals(List.of("The addressee claims with in"), d.readers());
        assertEquals("the addressee", d.fields().get("to"));
        assertEquals("free text", d.body());
        assertEquals(List.of("RDR-239"), d.seeAlso());
        TemplateDoc.Example ex = d.example();
        assertEquals("mailbox/someone", ex.subspace());
        assertEquals("n1", ex.nonce());
        assertEquals(Map.of("to", "someone"), ex.keys());
        assertEquals(Map.of("from", "other"), ex.dims());
        assertEquals("hello", ex.body());
    }

    @Test
    void templateWithoutDocLoadsWithNullDoc() {
        assertNull(TemplateSchemaParser.parse("mailbox.yaml", validDoc()).doc());
    }

    @Test
    void unknownDocFieldIsRefusedNamingIt() {
        Map<String, Object> block = docBlock();
        block.put("dimension_values", "x");
        assertNames(refused(docDocument(block)), "doc.dimension_values");
    }

    @Test
    void docWithoutSummaryIsRefused() {
        Map<String, Object> block = docBlock();
        block.remove("summary");
        assertNames(refused(docDocument(block)), "doc.summary");
    }

    @Test
    void docThatIsNotAMappingIsRefused() {
        assertNames(refused(docDocument(null)), "doc");
        Map<String, Object> doc = validDoc();
        doc.put("doc", "just a string");
        assertNames(refused(doc), "doc");
    }

    @Test
    void fieldsNameThatIsNotADeclaredKeyOrDimensionIsRefusedNamingIt() {
        Map<String, Object> block = docBlock();
        sub(block, "fields").put("sender", "typo for from");
        assertNames(refused(docDocument(block)), "doc.fields.sender");
    }

    @Test
    void valueMapWhoseKeysDifferFromTheDeclaredValuesIsRefusedNamingBothSets() {
        Map<String, Object> document = validDoc();
        Map<String, Object> kind = new LinkedHashMap<>();
        kind.put("type", "string");
        kind.put("values", List.of("agent", "session"));
        sub(document, "dimensions").put("address_kind", kind);
        Map<String, Object> block = docBlock();
        Map<String, Object> valueMap = new LinkedHashMap<>();
        valueMap.put("agent", "a named agent");
        valueMap.put("pane", "not a declared value");
        sub(block, "fields").put("address_kind", valueMap);
        document.put("doc", block);
        TemplateRegistryException ex = refused(document);
        assertNames(ex, "doc.fields.address_kind");
        assertTrue(ex.getMessage().contains("[agent, pane]"), ex.getMessage());
        assertTrue(ex.getMessage().contains("[agent, session]"), ex.getMessage());

        // exactly the declared values, in any order: accepted
        Map<String, Object> exact = new LinkedHashMap<>();
        exact.put("session", "a session");
        exact.put("agent", "a named agent");
        sub(block, "fields").put("address_kind", exact);
        assertDoesNotThrow(() -> TemplateSchemaParser.parse("t.yaml", document));
    }

    @Test
    void valueMapOnAPinnedKeyIsHeldToThePinnedValues() {
        Map<String, Object> document = validDoc();
        Map<String, Object> spec = new LinkedHashMap<>();
        spec.put("values", List.of("a", "b"));
        Map<String, Object> keys = new LinkedHashMap<>();
        keys.put("to", spec);
        document.put("keys", keys);
        Map<String, Object> block = docBlock();
        Map<String, Object> valueMap = new LinkedHashMap<>();
        valueMap.put("a", "first");
        sub(block, "fields").put("to", valueMap);
        document.put("doc", block);
        assertNames(refused(document), "doc.fields.to");
    }

    @Test
    void valueMapOnAFieldWithNoDeclaredValuesIsFree() {
        Map<String, Object> block = docBlock();
        Map<String, Object> valueMap = new LinkedHashMap<>();
        valueMap.put("anything", "in use today");
        sub(block, "fields").put("from", valueMap);
        assertDoesNotThrow(() -> TemplateSchemaParser.parse("t.yaml", docDocument(block)));
    }

    @Test
    void emptyWritersParsesAsAnEmptyListAndAbsentWritersAsNull() {
        Map<String, Object> block = docBlock();
        block.put("writers", new ArrayList<>());
        assertEquals(List.of(), TemplateSchemaParser.parse("t.yaml", docDocument(block)).doc().writers());
        block.remove("writers");
        assertNull(TemplateSchemaParser.parse("t.yaml", docDocument(block)).doc().writers());
    }

    @Test
    void docBodyOnATemplateWithMaxBodyBytesZeroIsRefused() {
        Map<String, Object> document = docDocument(docBlock());
        document.put("max_body_bytes", 0L);
        assertNames(refused(document), "doc.body");
        // omitting doc.body there is fine (the example then carries no body either)
        Map<String, Object> block = docBlock();
        block.remove("body");
        sub(block, "example").remove("body");
        Map<String, Object> ok = docDocument(block);
        ok.put("max_body_bytes", 0L);
        assertDoesNotThrow(() -> TemplateSchemaParser.parse("t.yaml", ok));
    }

    @Test
    void exampleWithoutSubspaceIsRefused() {
        Map<String, Object> block = docBlock();
        sub(block, "example").remove("subspace");
        assertNames(refused(docDocument(block)), "doc.example.subspace");
    }

    @Test
    void exampleNonceOnATemplateWhoseIdIsNotKeysPlusNonceIsRefused() {
        Map<String, Object> document = docDocument(docBlock());
        document.put("id_from", "keys");
        document.put("id_dims", List.of());
        assertNames(refused(document), "doc.example.nonce");
        // and without the nonce the same template is fine
        sub(sub(document, "doc"), "example").remove("nonce");
        assertDoesNotThrow(() -> TemplateSchemaParser.parse("t.yaml", document));
    }

    @Test
    void exampleKeysAndDimsMiniYamlReturnsAsLongOrBooleanAreReadAsStrings() {
        Map<String, Object> block = docBlock();
        Map<String, Object> ex = sub(block, "example");
        sub(ex, "keys").put("to", 3889972L);
        sub(ex, "dims").put("from", Boolean.TRUE);
        ex.put("nonce", 42L);
        ex.put("body", 7L);
        TemplateDoc.Example parsed = TemplateSchemaParser.parse("t.yaml", docDocument(block)).doc().example();
        assertEquals("3889972", parsed.keys().get("to"));
        assertEquals("true", parsed.dims().get("from"));
        assertEquals("42", parsed.nonce());
        assertEquals("7", parsed.body());
    }

    // ── L7 (nexus-mdbds.2.14): the field-naming refusals the cases above leave untested ──

    @Test
    void writersAndSeeAlsoThatAreNotListsAreRefusedNamingThem() {
        for (String key : List.of("writers", "readers", "see_also")) {
            Map<String, Object> block = docBlock();
            block.put(key, "a lone string");
            assertNames(refused(docDocument(block)), "doc." + key);
        }
    }

    @Test
    void docFieldsThatIsNotAMappingIsRefusedNamingIt() {
        Map<String, Object> block = docBlock();
        block.put("fields", new ArrayList<>(List.of("to")));
        assertNames(refused(docDocument(block)), "doc.fields");
    }

    @Test
    void anEmptyValueMapIsRefusedNamingItsField() {
        Map<String, Object> block = docBlock();
        sub(block, "fields").put("from", new LinkedHashMap<String, Object>());
        TemplateRegistryException ex = refused(docDocument(block));
        assertNames(ex, "doc.fields.from");
        assertTrue(ex.getMessage().contains("empty"), ex.getMessage());

        Map<String, Object> bodyBlock = docBlock();
        bodyBlock.put("body", new LinkedHashMap<String, Object>());
        assertNames(refused(docDocument(bodyBlock)), "doc.body");
    }

    @Test
    void anUnknownFieldInsideTheExampleIsRefusedNamingItAndTheDocBlock() {
        Map<String, Object> block = docBlock();
        sub(block, "example").put("zzz", "x");
        TemplateRegistryException ex = refused(docDocument(block));
        assertNames(ex, "doc.example.zzz");
        assertTrue(ex.getMessage().contains("RDR-239 doc block"), ex.getMessage());
    }

    @Test
    void anUnknownDocFieldMessageNamesTheDocBlockWhileATopLevelOneKeepsTheV1Shape() {
        Map<String, Object> block = docBlock();
        block.put("dimension_values", "x");
        assertTrue(refused(docDocument(block)).getMessage().contains("RDR-239 doc block"));
        Map<String, Object> top = validDoc();
        top.put("bogus", "x");
        TemplateRegistryException ex = refused(top);
        assertNames(ex, "bogus");
        assertTrue(ex.getMessage().contains("RDR-205 v1 document shape"), ex.getMessage());
    }

    @Test
    void aNonScalarExampleNonceOrBodyIsRefusedNamingIt() {
        Map<String, Object> nonce = docBlock();
        sub(nonce, "example").put("nonce", new ArrayList<>(List.of("n1")));
        assertNames(refused(docDocument(nonce)), "doc.example.nonce");

        Map<String, Object> body = docBlock();
        sub(body, "example").put("body", new LinkedHashMap<String, Object>());
        assertNames(refused(docDocument(body)), "doc.example.body");
    }

    @Test
    void exampleKeysOrDimsThatAreNotAMappingAreRefusedNamingThem() {
        Map<String, Object> keys = docBlock();
        sub(keys, "example").put("keys", "someone");
        assertNames(refused(docDocument(keys)), "doc.example.keys");

        Map<String, Object> dims = docBlock();
        sub(dims, "example").put("dims", new ArrayList<>(List.of("other")));
        assertNames(refused(docDocument(dims)), "doc.example.dims");

        Map<String, Object> nonScalar = docBlock();
        sub(sub(nonScalar, "example"), "keys").put("to", new ArrayList<>(List.of("a")));
        assertNames(refused(docDocument(nonScalar)), "doc.example.keys.to");
    }

    // L3: the parsed block is a snapshot shared across requests; nothing may mutate it

    @Test
    void theParsedDocCannotBeMutated() {
        TemplateDoc d = TemplateSchemaParser.parse("mailbox.yaml", docDocument(docBlock())).doc();
        assertThrows(UnsupportedOperationException.class, () -> d.writers().add("x"));
        assertThrows(UnsupportedOperationException.class, () -> d.readers().add("x"));
        assertThrows(UnsupportedOperationException.class, () -> d.seeAlso().add("x"));
        assertThrows(UnsupportedOperationException.class, () -> d.fields().put("x", "y"));
        assertThrows(UnsupportedOperationException.class, () -> d.example().keys().put("x", "y"));
        assertThrows(UnsupportedOperationException.class, () -> d.example().dims().put("x", "y"));

        Map<String, Object> block = docBlock();
        Map<String, Object> valueMap = new LinkedHashMap<>();
        valueMap.put("anything", "in use today");
        sub(block, "fields").put("from", valueMap);
        block.put("body", new LinkedHashMap<>(Map.of("text", "the message")));
        TemplateDoc withMaps = TemplateSchemaParser.parse("t.yaml", docDocument(block)).doc();
        @SuppressWarnings("unchecked")
        Map<String, String> fieldMap = (Map<String, String>) withMaps.fields().get("from");
        assertThrows(UnsupportedOperationException.class, () -> fieldMap.put("x", "y"));
        @SuppressWarnings("unchecked")
        Map<String, String> bodyMap = (Map<String, String>) withMaps.body();
        assertThrows(UnsupportedOperationException.class, () -> bodyMap.put("x", "y"));
    }

    @Test
    void theDocDoesNotAliasTheParsedInput() {
        List<String> writers = new ArrayList<>(List.of("a"));
        Map<String, Object> fields = new LinkedHashMap<>();
        fields.put("to", "the addressee");
        TemplateDoc d = new TemplateDoc("s", writers, null, fields, null, null, null);
        writers.add("late addition");
        fields.put("late", "addition");
        assertEquals(List.of("a"), d.writers());
        assertEquals(Map.of("to", "the addressee"), d.fields());
    }

    // ── byte caps: 300 per description, 4,096 per block (S4 Q3) ─────────────────────

    private static String bytes(int n) {
        return "x".repeat(n);
    }

    private void assertCap(java.util.function.Consumer<Map<String, Object>> setOver,
                           java.util.function.Consumer<Map<String, Object>> setAt, String field) {
        Map<String, Object> over = docBlock();
        setOver.accept(over);
        TemplateRegistryException ex = refused(docDocument(over));
        assertNames(ex, field);
        assertTrue(ex.getMessage().contains("300"), ex.getMessage());
        Map<String, Object> at = docBlock();
        setAt.accept(at);
        assertDoesNotThrow(() -> TemplateSchemaParser.parse("t.yaml", docDocument(at)), field + " at the cap");
    }

    @Test
    void summaryOverThreeHundredBytesIsRefused() {
        assertCap(b -> b.put("summary", bytes(301)), b -> b.put("summary", bytes(300)), "doc.summary");
    }

    @Test
    void multiByteSummaryIsCountedInBytesNotCharacters() {
        // 151 two-byte characters = 302 bytes, 151 chars
        Map<String, Object> block = docBlock();
        block.put("summary", "\u00e9".repeat(151));
        assertNames(refused(docDocument(block)), "doc.summary");
    }

    @Test
    void writersReadersAndSeeAlsoStringsOverThreeHundredBytesAreRefused() {
        for (String f : List.of("writers", "readers", "see_also")) {
            assertCap(b -> b.put(f, new ArrayList<>(List.of("ok", bytes(301)))),
                    b -> b.put(f, new ArrayList<>(List.of("ok", bytes(300)))), "doc." + f + "[1]");
        }
    }

    @Test
    @SuppressWarnings("unchecked")
    void fieldsDescriptionAndValueMapDescriptionOverThreeHundredBytesAreRefused() {
        assertCap(b -> sub(b, "fields").put("to", bytes(301)), b -> sub(b, "fields").put("to", bytes(300)),
                "doc.fields.to");
        assertCap(b -> {
                    Map<String, Object> vm = new LinkedHashMap<>();
                    vm.put("v", bytes(301));
                    sub(b, "fields").put("to", vm);
                },
                b -> {
                    Map<String, Object> vm = new LinkedHashMap<>();
                    vm.put("v", bytes(300));
                    sub(b, "fields").put("to", vm);
                }, "doc.fields.to.v");
    }

    @Test
    void bodyDescriptionAndBodyMapDescriptionOverThreeHundredBytesAreRefused() {
        assertCap(b -> b.put("body", bytes(301)), b -> b.put("body", bytes(300)), "doc.body");
        assertCap(b -> {
                    Map<String, Object> bm = new LinkedHashMap<>();
                    bm.put("state", bytes(301));
                    b.put("body", bm);
                },
                b -> {
                    Map<String, Object> bm = new LinkedHashMap<>();
                    bm.put("state", bytes(300));
                    b.put("body", bm);
                }, "doc.body.state");
    }

    /** The block's weight as the engine's reader sees it: UTF-8 bytes of every mapping key and
     *  every scalar in the parsed mapping, nested. Written out here, not shared with the engine. */
    private static int weight(Object o) {
        if (o instanceof Map<?, ?> m) {
            int w = 0;
            for (var e : m.entrySet()) {
                w += weight(e.getKey()) + weight(e.getValue());
            }
            return w;
        }
        if (o instanceof List<?> l) {
            int w = 0;
            for (Object x : l) {
                w += weight(x);
            }
            return w;
        }
        return o == null ? 0 : String.valueOf(o).getBytes(StandardCharsets.UTF_8).length;
    }

    @Test
    void wholeBlockOverFourThousandNinetySixBytesIsRefusedAndAtTheCapIsNot() {
        Map<String, Object> at = docBlock();
        at.remove("see_also");
        int remaining = 4096 - weight(at) - "see_also".length();
        List<Object> filler = new ArrayList<>();
        while (remaining > 200) {
            filler.add(bytes(200));
            remaining -= 200;
        }
        filler.add(bytes(remaining));
        at.put("see_also", filler);
        assertEquals(4096, weight(at));
        assertDoesNotThrow(() -> TemplateSchemaParser.parse("t.yaml", docDocument(at)));

        Map<String, Object> over = docBlock();
        over.remove("see_also");
        List<Object> filler2 = new ArrayList<>(filler);
        filler2.set(filler2.size() - 1, bytes(remaining + 1));
        over.put("see_also", filler2);
        assertEquals(4097, weight(over));
        TemplateRegistryException ex = refused(docDocument(over));
        assertNames(ex, "doc");
        assertTrue(ex.getMessage().contains("4096"), ex.getMessage());
    }
}
