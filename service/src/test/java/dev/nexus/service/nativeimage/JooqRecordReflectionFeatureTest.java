package dev.nexus.service.nativeimage;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.List;
import org.junit.jupiter.api.Test;

/**
 * Structural guard for {@link JooqRecordReflectionFeature}.
 *
 * <p>This is a GUARD, not proof of the fix. The bug it defends against —
 * jOOQ {@code INSERT … RETURNING} hitting {@code MissingReflectionRegistrationError}
 * on an unregistered generated record constructor — manifests ONLY in the GraalVM
 * native image; on the JVM reflection always succeeds, so no JVM test can reproduce
 * it or prove the native fix. The real gate is the {@code -Pnative} build plus the
 * {@code tests/e2e/migration-rehearsal/run.sh --fullstack} run reaching
 * {@code document_aspects > 0} (nexus-i9o37.3).
 *
 * <p>What this test DOES catch cheaply: a Feature that silently enumerates nothing
 * (vacuous registration) or schema-model drift where {@code Nexus.NEXUS.getTables()}
 * stops returning some generated tables. It exercises the Feature's OWN discovery
 * path ({@link JooqRecordReflectionFeature#recordTypes()}) rather than a parallel
 * re-derivation, so passing means the exact set the Feature registers is the full set.
 */
class JooqRecordReflectionFeatureTest {

    /**
     * Generated record count across BOTH generated schemas: dev.nexus.service.jooq.nexus
     * (nexus schema, T2 stores) and dev.nexus.service.jooq.t1 (t1 schema, T1 scratch).
     *
     * <p>50 -&gt; 51 (bead nexus-melvx, RDR-178 Gap 5): added {@code MigrationJobsRecord}
     * for the new {@code nexus.migration_jobs} table (async ingest-cloud job tracking).
     *
     * <p>51 -&gt; 52 (bead nexus-opr9m): the Feature only ever enumerated
     * {@code Nexus.NEXUS.getTables()} — the {@code t1} schema (a SEPARATE generated
     * schema, nexus-gmiaf.13) was never registered, so {@code ScratchRecord}'s
     * constructor was unreachable via reflection in the native image. Every
     * {@code selectFrom(SCRATCH)} read (get/search/list) hit
     * {@code MissingReflectionRegistrationError} -&gt; HTTP 500 in the deployed cloud
     * native-image binary; writes (plain {@code INSERT...execute()}, no
     * record-materialization) were unaffected, matching the observed 100%
     * get/search failure vs 0% put failure.
     */
    // 52 -> 53: RDR-182 nexus-ng2sy added nexus.claude_assisted_remediation_consents
    // (the service-mode consent-audit table), so jOOQ codegen emits one more record
    // type. The feature enumerates via the schema model, so the new record is already
    // registered for native-image reflection; this guard is the deliberate count bump.
    private static final int EXPECTED_RECORD_TYPES = 53;

    @Test
    void enumeratesEveryGeneratedRecordTypeViaTheSchemaModel() {
        List<Class<?>> records = JooqRecordReflectionFeature.recordTypes();

        assertEquals(
                EXPECTED_RECORD_TYPES,
                records.size(),
                "Feature must enumerate every jOOQ generated record type via the schema "
                        + "model (both the nexus and t1 schemas); a different count means a "
                        + "schema grew/shrank (update this guard deliberately) or the discovery "
                        + "path went vacuous");

        assertTrue(
                records.stream()
                        .allMatch(c -> c.getName()
                                .startsWith("dev.nexus.service.jooq.nexus.tables.records.")
                                || c.getName()
                                        .startsWith("dev.nexus.service.jooq.t1.tables.records.")),
                "every enumerated type must be a generated record class from the nexus or t1 schema");

        assertTrue(
                records.stream()
                        .anyMatch(c -> c.getName().equals(
                                "dev.nexus.service.jooq.t1.tables.records.ScratchRecord")),
                "the t1 schema's ScratchRecord must be registered — this is the nexus-opr9m regression guard");
    }
}
