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
     * Generated record count under dev.nexus.service.jooq.nexus.tables.records.*
     *
     * <p>50 -&gt; 51 (bead nexus-melvx, RDR-178 Gap 5): added {@code MigrationJobsRecord}
     * for the new {@code nexus.migration_jobs} table (async ingest-cloud job tracking).
     */
    private static final int EXPECTED_RECORD_TYPES = 51;

    @Test
    void enumeratesEveryGeneratedRecordTypeViaTheSchemaModel() {
        List<Class<?>> records = JooqRecordReflectionFeature.recordTypes();

        assertEquals(
                EXPECTED_RECORD_TYPES,
                records.size(),
                "Feature must enumerate every jOOQ generated record type via the schema "
                        + "model; a different count means the schema grew/shrank (update this "
                        + "guard deliberately) or the discovery path went vacuous");

        assertTrue(
                records.stream()
                        .allMatch(c -> c.getName()
                                .startsWith("dev.nexus.service.jooq.nexus.tables.records.")),
                "every enumerated type must be a generated record class");
    }
}
