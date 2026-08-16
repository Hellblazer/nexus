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
    // 53 -> 54: nexus-24p05 added nexus.retention_markers (verify-fill watermark
    // rollback detector)
    // (the service-mode consent-audit table), so jOOQ codegen emits one more record
    // type. The feature enumerates via the schema model, so the new record is already
    // registered for native-image reflection; this guard is the deliberate count bump.
    // 54 -> 56: RDR-186 nexus-146xx.3/.10 added nexus.chash_remap (wire re-id map,
    // raw-fact substrate) and nexus.ladder_completions (upgrade-ladder bookkeeping).
    // 56 -> 57: nexus-146xx.5 added nexus.remap_membership(text,text) — jOOQ
    // generates a record type for the table-valued function's RETURNS TABLE shape.
    // 57 -> 60: nexus-146xx.16 added the engine-hosted streaming-PDF buffer
    // (nexus.pdf_pipeline, nexus.pdf_pages, nexus.pdf_chunks).
    // 60 -> 61: RDR-180 (nexus-jxizy.2) added nexus.chash_alias (the legacy-id
    // resolution map for the bytea-chash rekey).
    // 61 -> 60: RDR-187 (nexus-piwya.9) DROPPED nexus.chash_index — the router
    // remnant of the split-store architecture; its generated record left with
    // the table (codegen derives from the changelog-booted schema).
    // 60 -> 61: nexus-jqvzk added nexus.gc_audit (the engine-side audit record
    // for destructive T3 operations, catalog-018-gc-audit.xml). The Feature
    // enumerates via the schema model, so GcAuditRecord is registered for
    // native-image reflection by construction — this is the deliberate bump.
    // It matters: recordGcAudit uses INSERT ... RETURNING, which is exactly the
    // shape that hit MissingReflectionRegistrationError in the native image.
    // 61 -> 63: nexus-5xn3k.1 added nexus.manifest_verify(text) and
    // nexus.manifest_verify_all() (catalog-020-index-run-fence.xml) — both
    // RETURNS TABLE, and jOOQ generates a Record type per table-returning
    // function. The fence COLUMNS on catalog_documents change no record
    // count; the two function-return records are the whole delta. This is
    // the deliberate bump the assertion message demands.
    // 63 -> 64: RDR-180 (nexus-du2dw) added nexus.chash_conformance_report(int)
    // (rdr180-021-chash-conformance-report.xml) — RETURNS TABLE, so jOOQ
    // generates one more Record type (ChashConformanceReportRecord). The
    // Feature enumerates via the schema model, so it is already registered
    // for native-image reflection by construction — this is the deliberate
    // bump the assertion message demands.
    // 64 -> 67: nexus-lns3o (engine half, taxonomy-006-assign-from-chashes.xml)
    // added nexus.assign_from_chashes_384/768/1024(text, text[], boolean) —
    // three per-dim RETURNS TABLE functions (server-side compute-and-persist
    // taxonomy assignment from just-upserted chashes), one generated Record
    // type each. This is the deliberate bump the assertion message demands.
    // 67 -> 69: RDR-191 Phase 1 (catalog-023-quarantine-functions.xml) added
    // nexus.gc_quarantine_orphans(...) and nexus.gc_expire_quarantine(...) —
    // both RETURNS TABLE, one generated Record type each
    // (GcQuarantineOrphansRecord, GcExpireQuarantineRecord). The third new
    // function, nexus.gc_restore_rereferenced(...), RETURNS bigint (a
    // scalar routine, not a table function) and generates NO Record type —
    // it is the reason this bump is +2, not +3. This is the deliberate
    // bump the assertion message demands.
    // 69 -> 65: RDR-191 repoint batch (nexus-o8dil.18/.48, the
    // vectors-004-unify-chunks.xml + taxonomy-007-unify-centroids.xml
    // changeset pair) collapsed SIX per-dim tables — chunks_384/768/1024
    // and taxonomy_centroids_384/768/1024 — into TWO unified tables,
    // nexus.chunks and nexus.taxonomy_centroids. jOOQ generates one Record
    // type per table, so this is -6 record types for the retired shards
    // +2 for their unified replacements = a net -4. This is the
    // deliberate bump (downward, for once) the assertion message demands.
    // 65 -> 63: RDR-191 Phase 6 (nexus-o8dil.33, catalog-030-retire-manifest-
    // verify.xml) DROPPED nexus.manifest_orphans(int) and
    // nexus.manifest_verify_all() — both RETURNS TABLE, one generated Record
    // type each (-2). nexus.manifest_backfill(), also dropped by the same
    // changeset, RETURNS bigint (a scalar routine, no Record type, same
    // shape as gc_restore_rereferenced above) — no further delta.
    // nexus.manifest_verify(text) is DELIBERATELY KEPT (completeIndexRun
    // depends on it), so its Record type is unaffected.
    // 63 -> 62: nexus-lgdel.l1 (legacy-001-drop-chash-alias.xml) DROPPED
    // nexus.chash_alias — a plain table, one generated Record type (-1).
    // nexus.chash_old_bytes(text), also dropped by the same changelog,
    // RETURNS bytea (a scalar routine, no Record type, same shape as
    // gc_restore_rereferenced above) — no further delta. The layered
    // CREATE OR REPLACE on nexus.remap_membership(text,text) changes its
    // body only, not its RETURNS TABLE signature — its existing Record
    // type is unaffected.
    private static final int EXPECTED_RECORD_TYPES = 62;

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
