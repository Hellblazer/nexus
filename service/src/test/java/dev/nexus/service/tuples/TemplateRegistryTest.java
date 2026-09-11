// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.tuples;

import dev.nexus.service.NexusService;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * nexus-em75s.3 — the RDR-205 tuple template registry: boot loader, boot-time
 * validator (including the claim-log-TTL boot check), and {@code registry()}.
 *
 * <p>The sweep interval used throughout is {@link NexusService#SWEEP_INTERVAL_HOURS}
 * (widened to public for exactly this test/loader, rather than mirrored — see the
 * javadoc on that field), converted to seconds, matching how {@code loadAtBoot}
 * will actually be called in production.
 */
class TemplateRegistryTest {

    private static final long SWEEP_INTERVAL_SECONDS = NexusService.SWEEP_INTERVAL_HOURS * 3600L;

    // ── real v1 resource templates ──────────────────────────────────────

    @Test
    void bothV1ResourceTemplatesLoadAtBoot() {
        TemplateRegistry registry = TemplateRegistry.loadAtBoot(null, null, SWEEP_INTERVAL_SECONDS);
        assertEquals(List.of(TemplateRegistry.SOURCE_RESOURCES), registry.sources());
        List<String> names = registry.templates().stream().map(TemplateSchema::name).toList();
        assertEquals(List.of("ledger/<session_id>", "mailbox/<address>"), names);
    }

    @Test
    void resourceTemplatesMatchTheirRdrSpecifiedShape() {
        TemplateRegistry registry = TemplateRegistry.loadAtBoot(null, null, SWEEP_INTERVAL_SECONDS);
        TemplateSchema ledger = registry.templates().stream()
                .filter(t -> t.name().equals("ledger/<session_id>")).findFirst().orElseThrow();
        assertEquals(Set.of("agent_id", "kind"), Set.copyOf(ledger.keys()));
        assertEquals(List.of("start", "report"), ledger.keyValues().get("kind"));
        assertFalse(ledger.keyValues().containsKey("agent_id"));
        assertFalse(ledger.take().enabled());
        assertEquals(7_776_000L, ledger.retentionSeconds());
        assertEquals(TemplateSchema.IdFrom.KEYS, ledger.idFrom());

        TemplateSchema mailbox = registry.templates().stream()
                .filter(t -> t.name().equals("mailbox/<address>")).findFirst().orElseThrow();
        assertEquals(Set.of("to"), Set.copyOf(mailbox.keys()));
        assertTrue(mailbox.keyValues().isEmpty());
        assertTrue(mailbox.take().enabled());
        assertEquals(3L, mailbox.take().maxAttempts());
        assertEquals(900L, mailbox.take().maxLeaseSeconds());
        assertEquals(604_800L, mailbox.retentionSeconds());
        assertEquals(TemplateSchema.IdFrom.KEYS_NONCE, mailbox.idFrom());
        assertEquals(List.of("from"), mailbox.idDims());
        assertTrue(mailbox.dimensions().get("from").required());
        assertEquals(List.of("agent", "instance"), mailbox.dimensions().get("address_kind").values());
    }

    // ── registry() digest ───────────────────────────────────────────────

    @Test
    void registryDigestIsStableAcrossTwoLoads() {
        String d1 = TemplateRegistry.loadAtBoot(null, null, SWEEP_INTERVAL_SECONDS).digest();
        String d2 = TemplateRegistry.loadAtBoot(null, null, SWEEP_INTERVAL_SECONDS).digest();
        assertEquals(d1, d2);
        assertNotNull(d1);
    }

    @Test
    void registryDigestChangesWhenATemplateChanges() {
        String base = minimalTemplateYaml("ledger/<session_id>", 7_776_000L);
        String changed = minimalTemplateYaml("ledger/<session_id>", 7_000_000L);

        TemplateRegistry r1 = TemplateRegistry.load(
                List.of(new TemplateRegistry.SourceGroup("test",
                        List.of(new TemplateRegistry.TemplateSource("a.yaml", base)))),
                DAYS(180), SWEEP_INTERVAL_SECONDS);
        TemplateRegistry r2 = TemplateRegistry.load(
                List.of(new TemplateRegistry.SourceGroup("test",
                        List.of(new TemplateRegistry.TemplateSource("a.yaml", changed)))),
                DAYS(180), SWEEP_INTERVAL_SECONDS);

        assertNotEquals(r1.digest(), r2.digest());
    }

    @Test
    void registryDigestChangesWhenAKeyGainsAPinnedValueSet() {
        String unconstrained = minimalTemplateYaml("ledger/<session_id>", 100L);
        String constrained = """
                name: ledger/<session_id>
                keys:
                  agent_id:
                    values: [start, report]
                id_from: keys
                take:
                  enabled: false
                retention_seconds: 100
                """;

        TemplateRegistry r1 = TemplateRegistry.load(
                List.of(new TemplateRegistry.SourceGroup("test",
                        List.of(new TemplateRegistry.TemplateSource("a.yaml", unconstrained)))),
                DAYS(180), SWEEP_INTERVAL_SECONDS);
        TemplateRegistry r2 = TemplateRegistry.load(
                List.of(new TemplateRegistry.SourceGroup("test",
                        List.of(new TemplateRegistry.TemplateSource("a.yaml", constrained)))),
                DAYS(180), SWEEP_INTERVAL_SECONDS);

        assertNotEquals(r1.digest(), r2.digest());
    }

    @Test
    void registrySnapshotCarriesDigestSourcesAndTemplates() {
        TemplateRegistry registry = TemplateRegistry.loadAtBoot(null, null, SWEEP_INTERVAL_SECONDS);
        TemplateRegistry.Snapshot snap = registry.registry();
        assertEquals(registry.digest(), snap.digest());
        assertEquals(registry.sources(), snap.sources());
        assertEquals(registry.templates(), snap.templates());
    }

    // ── duplicate name ──────────────────────────────────────────────────

    @Test
    void duplicateTemplateNameFailsLoad() {
        String yaml = minimalTemplateYaml("ledger/<session_id>", 100L);
        var groups = List.of(new TemplateRegistry.SourceGroup("test", List.of(
                new TemplateRegistry.TemplateSource("first.yaml", yaml),
                new TemplateRegistry.TemplateSource("second.yaml", yaml))));
        var ex = assertThrows(TemplateRegistryException.class,
                () -> TemplateRegistry.load(groups, DAYS(180), SWEEP_INTERVAL_SECONDS));
        assertTrue(ex.getMessage().contains("second.yaml"), ex.getMessage());
        assertTrue(ex.getMessage().contains("duplicate"), ex.getMessage());
        assertTrue(ex.getMessage().contains("ledger/<session_id>"), ex.getMessage());
    }

    // ── empty parameter segment (load rule, verbatim from May) ─────────

    @Test
    void emptyParameterSegmentFailsLoad() {
        String yaml = minimalTemplateYaml("ledger/<>", 100L);
        var groups = List.of(new TemplateRegistry.SourceGroup("test",
                List.of(new TemplateRegistry.TemplateSource("bad.yaml", yaml))));
        var ex = assertThrows(TemplateRegistryException.class,
                () -> TemplateRegistry.load(groups, DAYS(180), SWEEP_INTERVAL_SECONDS));
        assertTrue(ex.getMessage().contains("bad.yaml"), ex.getMessage());
        assertTrue(ex.getMessage().contains("name"), ex.getMessage());
    }

    // ── a breaching template file names the file and field ─────────────

    @Test
    void breachingTemplateFileNamesFileAndField() {
        String yaml = """
                name: ledger/<session_id>
                keys:
                  - agent_id
                id_from: keys
                take:
                  enabled: false
                """; // retention_seconds missing
        var groups = List.of(new TemplateRegistry.SourceGroup("test",
                List.of(new TemplateRegistry.TemplateSource("broken.yaml", yaml))));
        var ex = assertThrows(TemplateRegistryException.class,
                () -> TemplateRegistry.load(groups, DAYS(180), SWEEP_INTERVAL_SECONDS));
        assertTrue(ex.getMessage().contains("broken.yaml"), ex.getMessage());
        assertTrue(ex.getMessage().contains("retention_seconds"), ex.getMessage());
    }

    // ── boot-time claim-log-TTL check ───────────────────────────────────

    @Test
    void bootCheckPassesWhenLogTtlStrictlyExceedsRetentionPlusOneSweepInterval() {
        long retentionSeconds = 100L;
        long logTtlSeconds = retentionSeconds + SWEEP_INTERVAL_SECONDS + 1; // strictly greater
        String yaml = minimalTemplateYaml("ledger/<session_id>", retentionSeconds);
        var groups = List.of(new TemplateRegistry.SourceGroup("test",
                List.of(new TemplateRegistry.TemplateSource("t.yaml", yaml))));
        TemplateRegistry registry = TemplateRegistry.load(groups, logTtlSeconds, SWEEP_INTERVAL_SECONDS);
        assertEquals(1, registry.templates().size());
    }

    @Test
    void bootCheckRefusesOnEquality() {
        long retentionSeconds = 100L;
        long logTtlSeconds = retentionSeconds + SWEEP_INTERVAL_SECONDS; // equality — must refuse
        String yaml = minimalTemplateYaml("ledger/<session_id>", retentionSeconds);
        var groups = List.of(new TemplateRegistry.SourceGroup("test",
                List.of(new TemplateRegistry.TemplateSource("t.yaml", yaml))));
        var ex = assertThrows(TemplateRegistryException.class,
                () -> TemplateRegistry.load(groups, logTtlSeconds, SWEEP_INTERVAL_SECONDS));
        assertTrue(ex.getMessage().contains("ledger/<session_id>"), ex.getMessage());
        assertTrue(ex.getMessage().contains("refuses to boot"), ex.getMessage());
    }

    @Test
    void bootCheckRefusesWhenLogTtlIsShorter() {
        long retentionSeconds = 100L;
        long logTtlSeconds = retentionSeconds; // far short
        String yaml = minimalTemplateYaml("ledger/<session_id>", retentionSeconds);
        var groups = List.of(new TemplateRegistry.SourceGroup("test",
                List.of(new TemplateRegistry.TemplateSource("t.yaml", yaml))));
        assertThrows(TemplateRegistryException.class,
                () -> TemplateRegistry.load(groups, logTtlSeconds, SWEEP_INTERVAL_SECONDS));
    }

    @Test
    void defaultClaimLogTtlPassesBootCheckForBothV1Templates() {
        // Exercised implicitly by bothV1ResourceTemplatesLoadAtBoot (default TTL, no
        // NX_TUPLE_CLAIM_LOG_TTL_DAYS override), stated explicitly here for the record.
        TemplateRegistry registry = TemplateRegistry.loadAtBoot(null, null, SWEEP_INTERVAL_SECONDS);
        assertEquals(2, registry.templates().size());
    }

    @Test
    void claimLogTtlDaysEnvNonIntegerIsRejected() {
        assertThrows(TemplateRegistryException.class,
                () -> TemplateRegistry.loadAtBoot(null, "not-a-number", SWEEP_INTERVAL_SECONDS));
    }

    @Test
    void claimLogTtlDaysEnvNonPositiveIsRejected() {
        assertThrows(TemplateRegistryException.class,
                () -> TemplateRegistry.loadAtBoot(null, "0", SWEEP_INTERVAL_SECONDS));
    }

    // ── literal-before-template resolution (May load rule) ─────────────

    @Test
    void literalNameIsLookedUpBeforeTemplates() {
        String literal = minimalTemplateYaml("ledger/fixed", 100L);
        String templated = minimalTemplateYaml("ledger/<session_id>", 100L);
        var groups = List.of(new TemplateRegistry.SourceGroup("test", List.of(
                new TemplateRegistry.TemplateSource("literal.yaml", literal),
                new TemplateRegistry.TemplateSource("templated.yaml", templated))));
        TemplateRegistry registry = TemplateRegistry.load(groups, DAYS(180), SWEEP_INTERVAL_SECONDS);

        TemplateSchema resolved = registry.resolve("ledger/fixed");
        assertNotNull(resolved);
        assertEquals("ledger/fixed", resolved.name());
        assertTrue(resolved.isLiteral());

        TemplateSchema resolvedOther = registry.resolve("ledger/some-session-id");
        assertNotNull(resolvedOther);
        assertEquals("ledger/<session_id>", resolvedOther.name());

        assertNull(registry.resolve("mailbox/x"));
    }

    // ── NX_TUPLE_TEMPLATE_DIR second source ─────────────────────────────

    @Test
    void templateDirAddsASecondSourceListedByRegistry(@TempDir Path dir) throws IOException {
        Path extra = dir.resolve("extra.yaml");
        Files.writeString(extra, minimalTemplateYaml("test/<id>", 100L), StandardCharsets.UTF_8);

        TemplateRegistry registry = TemplateRegistry.loadAtBoot(dir.toString(), null, SWEEP_INTERVAL_SECONDS);

        assertEquals(2, registry.sources().size());
        assertEquals(TemplateRegistry.SOURCE_RESOURCES, registry.sources().get(0));
        assertTrue(registry.sources().get(1).startsWith("directory:"), registry.sources().get(1));
        assertEquals(3, registry.templates().size());
        assertTrue(registry.templates().stream().anyMatch(t -> t.name().equals("test/<id>")));
    }

    @Test
    void unsetTemplateDirReportsResourcesSourceOnly() {
        TemplateRegistry registry = TemplateRegistry.loadAtBoot("", null, SWEEP_INTERVAL_SECONDS);
        assertEquals(List.of(TemplateRegistry.SOURCE_RESOURCES), registry.sources());
    }

    @Test
    void templateDirPointingAtANonDirectoryIsABreach() throws IOException {
        Path notADir = Files.createTempFile("nexus-em75s3-", ".txt");
        try {
            assertThrows(TemplateRegistryException.class,
                    () -> TemplateRegistry.loadAtBoot(notADir.toString(), null, SWEEP_INTERVAL_SECONDS));
        } finally {
            Files.deleteIfExists(notADir);
        }
    }

    // ── helpers ──────────────────────────────────────────────────────────

    private static long DAYS(long days) {
        return days * 86_400L;
    }

    private static String minimalTemplateYaml(String name, long retentionSeconds) {
        return "name: " + name + "\n"
                + "keys:\n"
                + "  - agent_id\n"
                + "id_from: keys\n"
                + "take:\n"
                + "  enabled: false\n"
                + "retention_seconds: " + retentionSeconds + "\n";
    }
}
