// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.tuples;

import dev.nexus.service.db.SchemaViolationException;
import dev.nexus.service.db.TooLargeException;
import dev.nexus.service.db.TupleLimits;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * nexus-mdbds.2.9 (RDR-239 S2): the shape, nonce and size checks {@code out} runs, held in
 * one stateless validator so the boot check can run the very same checks. One case per
 * refusal, each asserting the message names the failing field, plus one accepted post per
 * shipped template. Needs no database: the templates come from the bundled resources.
 */
class TupleShapeValidatorTest {

    private static final long SWEEP_INTERVAL_SECONDS = 3600L;
    private static TemplateRegistry registry;

    @BeforeAll
    static void loadShippedTemplates() {
        registry = TemplateRegistry.loadAtBoot(null, null, SWEEP_INTERVAL_SECONDS);
    }

    private static TemplateSchema template(String name) {
        TemplateSchema t = registry.byName(name);
        assertNotNull(t, "shipped template " + name + " must be present");
        return t;
    }

    private static String bytes(int n) {
        return "x".repeat(n);
    }

    // ── one accepted post per shipped template ───────────────────────────

    private static Map<String, Runnable> acceptedPosts() {
        Map<String, Runnable> m = new LinkedHashMap<>();
        m.put("aspects", () -> TupleShapeValidator.validateOut(template("aspects"),
                Map.of("queue", "aspects"), Map.of(), "{}", "n1", false));
        m.put("board/<topic>", () -> TupleShapeValidator.validateOut(template("board/<topic>"),
                Map.of("topic", "t"), Map.of("from", "a"), "hello", "n1", false));
        m.put("board/ci/<topic>", () -> TupleShapeValidator.validateOut(template("board/ci/<topic>"),
                Map.of("topic", "t"), Map.of("from", "a", "kind", "k"), "hello", "n1", false));
        m.put("directory/<name>", () -> TupleShapeValidator.validateOut(template("directory/<name>"),
                Map.of("name", "n"), Map.of("session_id", "s"), null, "n1", false));
        m.put("lock/<resource>", () -> TupleShapeValidator.validateOut(template("lock/<resource>"),
                Map.of("resource", "r"), Map.of("from", "a"), null, null, false));
        m.put("mailbox/<address>", () -> TupleShapeValidator.validateOut(template("mailbox/<address>"),
                Map.of("to", "x"), Map.of("from", "a", "address_kind", "agent"), "hi", "n1", false));
        m.put("queue/<name>", () -> TupleShapeValidator.validateOut(template("queue/<name>"),
                Map.of("queue", "q"), Map.of("from", "a"), "job", "n1", false));
        return m;
    }

    @Test
    void everyShippedTemplateAcceptsAWellFormedPost() {
        Map<String, Runnable> posts = acceptedPosts();
        Set<String> shipped = new HashSet<>();
        registry.templates().forEach(t -> shipped.add(t.name()));
        assertEquals(shipped, posts.keySet(),
                "a shipped template with no accepted-post case would pass this test vacuously");
        for (var e : posts.entrySet()) {
            assertDoesNotThrow(() -> e.getValue().run(), "accepted post for " + e.getKey());
        }
    }

    // ── shape refusals ───────────────────────────────────────────────────

    @Test
    void missingKeyIsRefusedNamingTheKey() {
        var ex = assertThrows(SchemaViolationException.class, () ->
                TupleShapeValidator.validateOut(template("queue/<name>"), Map.of(),
                        Map.of("from", "a"), null, "n", false));
        assertEquals("queue", ex.field());
        assertTrue(ex.getMessage().contains("'queue'"), ex.getMessage());
    }

    @Test
    void blankKeyIsRefusedNamingTheKey() {
        var ex = assertThrows(SchemaViolationException.class, () ->
                TupleShapeValidator.validateOut(template("queue/<name>"), Map.of("queue", "  "),
                        Map.of("from", "a"), null, "n", false));
        assertEquals("queue", ex.field());
    }

    @Test
    void keyOutsideItsEnumIsRefusedNamingTheKey() {
        var ex = assertThrows(SchemaViolationException.class, () ->
                TupleShapeValidator.validateOut(template("aspects"), Map.of("queue", "bogus"),
                        Map.of(), null, "n", false));
        assertEquals("queue", ex.field());
        assertTrue(ex.getMessage().contains("not in [aspects]"), ex.getMessage());
    }

    @Test
    void undeclaredKeyIsRefusedNamingTheKey() {
        var ex = assertThrows(SchemaViolationException.class, () ->
                TupleShapeValidator.validateOut(template("queue/<name>"),
                        Map.of("queue", "q", "extra", "v"), Map.of("from", "a"), null, "n", false));
        assertEquals("extra", ex.field());
        assertTrue(ex.getMessage().contains("not a declared key"), ex.getMessage());
    }

    @Test
    void nonceSuppliedAsAKeyIsRefusedWithTheNonceHint() {
        var ex = assertThrows(SchemaViolationException.class, () ->
                TupleShapeValidator.validateOut(template("queue/<name>"),
                        Map.of("queue", "q", "nonce", "v"), Map.of("from", "a"), null, "n", false));
        assertEquals("nonce", ex.field());
        assertTrue(ex.getMessage().contains("not a key: the nonce is its own field"), ex.getMessage());
    }

    @Test
    void undeclaredDimensionIsRefusedNamingTheDimension() {
        var ex = assertThrows(SchemaViolationException.class, () ->
                TupleShapeValidator.validateOut(template("queue/<name>"), Map.of("queue", "q"),
                        Map.of("from", "a", "surprise", "v"), null, "n", false));
        assertEquals("surprise", ex.field());
        assertTrue(ex.getMessage().contains("not a declared dimension"), ex.getMessage());
    }

    @Test
    void nonceSuppliedAsADimensionIsRefusedWithTheNonceHint() {
        var ex = assertThrows(SchemaViolationException.class, () ->
                TupleShapeValidator.validateOut(template("queue/<name>"), Map.of("queue", "q"),
                        Map.of("from", "a", "nonce", "v"), null, "n", false));
        assertEquals("nonce", ex.field());
        assertTrue(ex.getMessage().contains("not a dimension: the nonce is its own field"), ex.getMessage());
    }

    @Test
    void missingRequiredDimensionIsRefusedNamingTheDimension() {
        var ex = assertThrows(SchemaViolationException.class, () ->
                TupleShapeValidator.validateOut(template("queue/<name>"), Map.of("queue", "q"),
                        Map.of(), null, "n", false));
        assertEquals("from", ex.field());
        assertTrue(ex.getMessage().contains("required dimension missing or blank"), ex.getMessage());
    }

    @Test
    void dimensionOutsideItsEnumIsRefusedNamingTheDimension() {
        var ex = assertThrows(SchemaViolationException.class, () ->
                TupleShapeValidator.validateOut(template("mailbox/<address>"), Map.of("to", "x"),
                        Map.of("from", "a", "address_kind", "bogus"), null, "n", false));
        assertEquals("address_kind", ex.field());
        assertTrue(ex.getMessage().contains("not in [agent, session]"), ex.getMessage());
    }

    // ── nonce ────────────────────────────────────────────────────────────

    @Test
    void blankNonceUnderKeysPlusNonceIsRefusedNamingTheNonce() {
        for (String blank : new String[] {null, "", "   "}) {
            var ex = assertThrows(SchemaViolationException.class, () ->
                    TupleShapeValidator.validateOut(template("queue/<name>"), Map.of("queue", "q"),
                            Map.of("from", "a"), null, blank, false));
            assertEquals("nonce", ex.field());
            assertTrue(ex.getMessage().contains("required for id_from=keys+nonce"), ex.getMessage());
        }
    }

    @Test
    void deferredNonceSkipsTheNonceChecks() {
        // ackWithReply: the reply's nonce does not exist yet, so neither the size nor the
        // presence check applies.
        assertDoesNotThrow(() -> TupleShapeValidator.validateOut(template("queue/<name>"),
                Map.of("queue", "q"), Map.of("from", "a"), null, null, true));
        assertDoesNotThrow(() -> TupleShapeValidator.validateOut(template("queue/<name>"),
                Map.of("queue", "q"), Map.of("from", "a"), null,
                bytes(TupleLimits.MAX_NONCE_BYTES + 1), true));
    }

    @Test
    void nonceUnderIdFromKeysIsNotChecked() {
        // Today's behaviour, kept by this refactor: a template with id_from=keys ignores a
        // supplied nonce rather than refusing it.
        assertDoesNotThrow(() -> TupleShapeValidator.checkNonce(template("lock/<resource>"), "supplied"));
        assertDoesNotThrow(() -> TupleShapeValidator.checkNonce(template("lock/<resource>"), null));
    }

    // ── size refusals ────────────────────────────────────────────────────

    @Test
    void bodyOverTheTemplateCeilingIsRefusedNamingTheBody() {
        var ex = assertThrows(TooLargeException.class, () ->
                TupleShapeValidator.validateOut(template("board/<topic>"), Map.of("topic", "t"),
                        Map.of("from", "a"), bytes(1025), "n", false));
        assertEquals("body", ex.field());
        assertEquals(1024L, ex.limitBytes());
        assertTrue(ex.getMessage().contains("'body'"), ex.getMessage());
    }

    @Test
    void bodyOverTheGlobalCapIsRefusedWhenTheTemplateDeclaresNone() {
        var ex = assertThrows(TooLargeException.class, () ->
                TupleShapeValidator.validateOut(template("queue/<name>"), Map.of("queue", "q"),
                        Map.of("from", "a"), bytes(TupleLimits.MAX_BODY_BYTES + 1), "n", false));
        assertEquals("body", ex.field());
        assertEquals((long) TupleLimits.MAX_BODY_BYTES, ex.limitBytes());
    }

    @Test
    void anyBodyOnAZeroCeilingTemplateIsRefused() {
        var ex = assertThrows(TooLargeException.class, () ->
                TupleShapeValidator.validateOut(template("directory/<name>"), Map.of("name", "n"),
                        Map.of("session_id", "s"), "x", "n", false));
        assertEquals("body", ex.field());
        assertEquals(0L, ex.limitBytes());
    }

    @Test
    void keyValueOverTheFieldLimitIsRefusedNamingTheKeyField() {
        var ex = assertThrows(TooLargeException.class, () ->
                TupleShapeValidator.validateOut(template("queue/<name>"),
                        Map.of("queue", bytes(TupleLimits.MAX_FIELD_VALUE_BYTES + 1)),
                        Map.of("from", "a"), null, "n", false));
        assertEquals("keys.queue", ex.field());
        assertTrue(ex.getMessage().contains("'keys.queue'"), ex.getMessage());
    }

    @Test
    void dimensionValueOverTheFieldLimitIsRefusedNamingTheDimField() {
        var ex = assertThrows(TooLargeException.class, () ->
                TupleShapeValidator.validateOut(template("queue/<name>"), Map.of("queue", "q"),
                        Map.of("from", bytes(TupleLimits.MAX_FIELD_VALUE_BYTES + 1)), null, "n", false));
        assertEquals("dims.from", ex.field());
    }

    @Test
    void nonceOverTheLimitIsRefusedNamingTheNonce() {
        var ex = assertThrows(TooLargeException.class, () ->
                TupleShapeValidator.validateOut(template("queue/<name>"), Map.of("queue", "q"),
                        Map.of("from", "a"), null, bytes(TupleLimits.MAX_NONCE_BYTES + 1), false));
        assertEquals("nonce", ex.field());
    }

    @Test
    void subspaceOverTheLimitIsRefusedNamingTheSubspace() {
        var ex = assertThrows(TooLargeException.class, () ->
                TupleShapeValidator.checkSubspaceSize(bytes(TupleLimits.MAX_SUBSPACE_BYTES + 1)));
        assertEquals("subspace", ex.field());
        assertDoesNotThrow(() -> TupleShapeValidator.checkSubspaceSize(bytes(TupleLimits.MAX_SUBSPACE_BYTES)));
        assertDoesNotThrow(() -> TupleShapeValidator.checkSubspaceSize(null));
    }

    @Test
    void checkFieldSizeNeverEchoesTheValueAndAcceptsNull() {
        var ex = assertThrows(TooLargeException.class, () ->
                TupleShapeValidator.checkFieldSize("claimant", "secret-value-" + bytes(20), 5));
        assertEquals("claimant", ex.field());
        assertTrue(!ex.getMessage().contains("secret-value"), ex.getMessage());
        assertDoesNotThrow(() -> TupleShapeValidator.checkFieldSize("claimant", null, 0));
    }

    // ── ordering (pinned by review history) ──────────────────────────────

    @Test
    void sizeRefusalOutranksShapeRefusal() {
        // nexus-r7xao: an oversized value for a key outside its enum must raise TooLarge, never
        // the value-echoing "not in [...]" SchemaViolation.
        assertThrows(TooLargeException.class, () ->
                TupleShapeValidator.validateOut(template("aspects"),
                        Map.of("queue", bytes(TupleLimits.MAX_FIELD_VALUE_BYTES + 1)),
                        Map.of(), null, "n", false));
    }

    @Test
    void shapeRefusalOutranksNonceRefusal() {
        // nexus-h61dl.3: shape first, then nonce.
        var ex = assertThrows(SchemaViolationException.class, () ->
                TupleShapeValidator.validateOut(template("queue/<name>"), Map.of(),
                        Map.of("from", "a"), null, null, false));
        assertEquals("queue", ex.field());
    }

    @Test
    void nullKeysAndDimsAreTreatedAsEmpty() {
        var ex = assertThrows(SchemaViolationException.class, () ->
                TupleShapeValidator.validateOut(template("queue/<name>"), null, null, null, "n", false));
        assertEquals("queue", ex.field());
    }
}
