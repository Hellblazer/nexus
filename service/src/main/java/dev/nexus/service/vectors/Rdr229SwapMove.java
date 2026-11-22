// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

/**
 * RDR-229 P3.3c (nexus-p2m1x.21): the text and tuning of the two swap moves, {@code rdr229-swap-chunks} and
 * {@code rdr229-swap-taxonomy_centroids}. {@link LeafIndexPass} (P3.3b) owns the identity of the row (label, driving table,
 * {@code changeset_id}, {@code supersedes}) and registers it once every leaf of the table has a valid id unique key; this class
 * owns everything else in it ({@link Spec}).
 *
 * <p><b>Template.</b> A re-stamp of the rows whose {@code collection_id} is still NULL: the stamp move's own text (vectors-070),
 * which matches no row once the stamp has run. A template with no DML is refused by dml_utils, so the swap's walk is this no-op.
 * Throttle: P3.2's proposed values for both swaps, chunk_size 10000 and sleep_ms 8000 (T2
 * {@code nexus_rdr/229-p32-fork-results-2026-11-20 (2/2)}, section 5; ask C).
 *
 * <p><b>Bounds.</b> {@link #STATEMENT_TIMEOUT_MS} 1400 ms for the completion (the framework derives {@code lock_timeout} as
 * min(5 s, statement/2), 700 ms, so there is no lock constant here); {@link #VERIFY_STATEMENT_TIMEOUT_MS} 60 s for the finisher's
 * first, READ ONLY verification pass, which is the warm-up (Sam, T2 {@code nexus/sam-ruling-split-move-bounds-2026-11-21}).
 *
 * <p><b>Verification</b> (runs READ ONLY, ACCESS SHARE only, once table-wide and once per tenant, and again warm inside the
 * completion transaction under the 1400 ms budget): it returns a row for each leaf of the table without a valid unique index on the
 * id key, and no row otherwise. Everything else in it is a plain scan that returns nothing, written as
 * {@code WHERE (SELECT count(..) ..) < 0} so the planner has to run it; there is no {@code pg_prewarm}. For the chunks swap that is
 * (1) the anti-join of {@code nexus.chunks} to {@code nexus.catalog_collections} on the composite's three columns, which is the
 * query the composite's validation runs (so whichever plan the planner picks, it reads what {@code on_complete} will read: the leaf
 * heaps or the id-key indexes, and the registry), and (2) a count over a column no index holds, which forces a read of every heap
 * page. Both arms run in the table-wide run only ({@code $1 IS NULL}): they are about the table, not a tenant, so a tenant's run
 * would repeat the whole scan. They do not report an orphan row: the table-wide run reads the registry as the migrating role, whose
 * owner-read policy (vectors-065) is not present on an install where the serving role migrates, and there a missing registry would
 * read as every chunk orphaned and fail the move for good; the composite's own validation reports a real orphan.
 *
 * <p><b>on_complete</b>, one transaction, no EXCEPTION clause (a 55P03 lock timeout propagates, and the finisher records it as a
 * transient blocked state and retries, RDR-246 fail-fast): (a) refuse when the table has no valid unique key on the id columns
 * (SQLSTATE 55000, permanent); (b) chunks only, add the three child id keys beside their name keys, {@code NOT VALID},
 * {@code ON UPDATE CASCADE}, each with its name key's ON DELETE action (the manifest's is DEFERRABLE INITIALLY DEFERRED, see the comment in {@code chunksOnComplete}); (c) chunks only, register the three
 * validation moves, after their constraints exist in this transaction; (d) chunks only, add the RDR-225 composite on the id, which
 * validates directly because PostgreSQL takes no NOT VALID on a partitioned referencing table. It drops nothing: the name primary
 * keys, the three name child keys and {@code chunks_collection_fk} stay (P3.7 and P4.1). Every step is guarded by what the catalog
 * holds, so a second pass adds and registers nothing.
 *
 * <p><b>Names.</b> A child id key is its name key's name with {@code _id} before the {@code _fk} suffix ({@code fk_catalog_chunks_chunk_id},
 * {@code topic_assignments_chunk_id_fk}, {@code chunk_orphaned_at_chunk_id_fk}); the composite is {@code chunks_collection_id_fk}, as
 * P3.3a names {@code <table>_collection_id_fk}. A validation move is {@code rdr229-fkvalidate-<table>-chunk-fk}: P3.3a's
 * {@code rdr229-fkvalidate-<table>} is taken on {@code topic_assignments} by the twin to the registry, and the other label needs
 * the suffix to stay distinct (the completion check's read set lists labels by name, so these three join it when P3.11 or P3.4
 * reads gate 2). A validation move carries its parent's {@code changeset_id} (S-3 point 3), read from the swap's row.
 */
public final class Rdr229SwapMove {

    /** The completion's statement bound; the framework derives lock_timeout 700 ms from it. */
    public static final int STATEMENT_TIMEOUT_MS = 1_400;

    /** The first, READ ONLY verification pass's bound; set explicitly, not left to the column's default. */
    public static final int VERIFY_STATEMENT_TIMEOUT_MS = 60_000;

    /** P3.2, ask C: rows per chunk and the pause after each, for both swaps and the three validations. */
    static final int CHUNK_SIZE = 10_000;
    static final int SLEEP_MS = 8_000;

    /** The validation moves: SUE only, so the bound is the generous one (P3.2: longest statement 0.8 s). */
    private static final int VALIDATION_STATEMENT_TIMEOUT_MS = 60_000;

    /** The label prefix and suffix of a child's validation move: {@code rdr229-fkvalidate-<table>-chunk-fk}. */
    static final String VALIDATION_LABEL_PREFIX = "rdr229-fkvalidate-";
    static final String VALIDATION_LABEL_SUFFIX = "-chunk-fk";

    /** The composite on the id from nexus.chunks to the registry. */
    static final String COMPOSITE_CONSTRAINT = "chunks_collection_id_fk";

    /**
     * What a swap row carries beyond its identity.
     *
     * @param template                   the re-stamp of rows still NULL; holds {@code <driving_table>} and {@code <chunking_clause>} once each
     * @param verification               the READ ONLY verification, with the one bind parameter {@code $1} (the tenant, NULL table-wide)
     * @param onComplete                 the completion transaction's SQL
     */
    public record Spec(String template, int chunkSize, int sleepMs, int statementTimeoutMs, int verifyStatementTimeoutMs,
                       String verification, String onComplete) { }

    private Rdr229SwapMove() { /* static */ }

    /** The swap row's tuning and text for {@code table}: {@code chunks} or {@code taxonomy_centroids}. */
    public static Spec spec(String table) {
        return switch (table) {
            case "chunks" -> new Spec(restamp("collection", "collection_id"), CHUNK_SIZE, SLEEP_MS, STATEMENT_TIMEOUT_MS,
                VERIFY_STATEMENT_TIMEOUT_MS, chunksVerification(), chunksOnComplete());
            case "taxonomy_centroids" -> new Spec(restamp("collection", "collection_id"), CHUNK_SIZE, SLEEP_MS,
                STATEMENT_TIMEOUT_MS, VERIFY_STATEMENT_TIMEOUT_MS, leafKeyVerification("taxonomy_centroids", "topic_id"),
                centroidsOnComplete());
            default -> throw new IllegalArgumentException("no swap move for table " + table
                + ": the swaps are nexus.chunks and nexus.taxonomy_centroids");
        };
    }

    /** The label of the validation move of {@code childTable}'s id key into chunks. */
    public static String validationLabel(String childTable) {
        return VALIDATION_LABEL_PREFIX + childTable + VALIDATION_LABEL_SUFFIX;
    }

    // ── template ───────────────────────────────────────────────────────────────────────────────────────────

    /** The stamp move's template (vectors-070): a re-stamp of the rows whose id is still NULL, over a table whose name is {@code nameColumn}. */
    private static String restamp(String nameColumn, String idColumn) {
        return "UPDATE <driving_table> SET " + idColumn + " = COALESCE(t." + idColumn
            + ", (SELECT cc.collection_id FROM nexus.catalog_collections cc WHERE cc.tenant_id = t.tenant_id AND cc.name = t."
            + nameColumn + ")) WHERE NULLIF(t." + nameColumn + ", '') IS NOT NULL AND t." + idColumn
            + " IS NULL AND EXISTS (SELECT 1 FROM nexus.catalog_collections cc WHERE cc.tenant_id = t.tenant_id AND cc.name = t."
            + nameColumn + ") AND <chunking_clause>";
    }

    /** The stamp move's verification (vectors-070): the rows still NULL whose name the registry carries. */
    private static String restampVerification(String table, String nameColumn, String idColumn) {
        return "SELECT 1 FROM nexus." + table + " t WHERE NULLIF(t." + nameColumn + ", '') IS NOT NULL AND t." + idColumn
            + " IS NULL AND EXISTS (SELECT 1 FROM nexus.catalog_collections cc WHERE cc.tenant_id = t.tenant_id AND cc.name = t."
            + nameColumn + ") AND (t.tenant_id = $1 OR $1 IS NULL)";
    }

    // ── verification ───────────────────────────────────────────────────────────────────────────────────────

    /**
     * One row per leaf of {@code table}'s tree with no valid unique index on exactly the id key's columns (the key's own columns,
     * sorted, so the column order of the index does not matter). Catalog reads only, and only in the table-wide run: a leaf's index is
     * the table's, not a tenant's.
     */
    private static String leafKeyVerification(String table, String keyColumn) {
        return """
            SELECT 1
              FROM pg_catalog.pg_partition_tree('nexus.%s'::pg_catalog.regclass) pt
             WHERE $1 IS NULL
               AND pt.isleaf
               AND NOT EXISTS (SELECT 1
                                 FROM pg_catalog.pg_index i
                                WHERE i.indrelid = pt.relid
                                  AND i.indisunique
                                  AND i.indisvalid
                                  AND i.indpred IS NULL
                                  AND i.indnatts = 4
                                  AND (SELECT pg_catalog.array_agg(a.attname::pg_catalog.text ORDER BY a.attname)
                                         FROM pg_catalog.pg_attribute a
                                        WHERE a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey))
                                      = ARRAY[%s])"""
            .formatted(table, sortedKey(keyColumn));
    }

    /** The id key's four columns, sorted, as an ARRAY element list: what {@code array_agg(attname ORDER BY attname)} yields for the key. */
    private static String sortedKey(String keyColumn) {
        return java.util.stream.Stream.of("tenant_id", "collection_id", keyColumn, "embedding_model").sorted()
            .map(c -> "'" + c + "'").collect(java.util.stream.Collectors.joining(", "));
    }

    private static String chunksVerification() {
        return leafKeyVerification("chunks", "chash") + """

            UNION ALL
            SELECT 1
             WHERE $1 IS NULL
               AND (SELECT pg_catalog.count(*)
                      FROM nexus.chunks c
                      LEFT JOIN nexus.catalog_collections cc
                        ON cc.tenant_id = c.tenant_id AND cc.collection_id = c.collection_id AND cc.embedding_model = c.embedding_model
                     WHERE cc.tenant_id IS NULL) < 0
            UNION ALL
            SELECT 1
             WHERE $1 IS NULL
               AND (SELECT pg_catalog.count(c.chunk_text) FROM nexus.chunks c) < 0""";
    }

    // ── on_complete ────────────────────────────────────────────────────────────────────────────────────────

    /** Step (a): raises when {@code table} has no valid unique key on the id columns. Inside a DO block; no EXCEPTION clause anywhere. */
    private static String requireIdKey(String table, String keyColumn) {
        return """
                IF NOT EXISTS (SELECT 1
                                 FROM pg_catalog.pg_constraint k
                                 JOIN pg_catalog.pg_index i ON i.indexrelid = k.conindid
                                WHERE k.conrelid = 'nexus.%1$s'::pg_catalog.regclass
                                  AND k.contype = 'u'
                                  AND i.indisvalid
                                  AND (SELECT pg_catalog.array_agg(a.attname::pg_catalog.text ORDER BY a.attname)
                                         FROM pg_catalog.pg_attribute a
                                        WHERE a.attrelid = k.conrelid AND a.attnum = ANY (k.conkey))
                                      = ARRAY[%3$s]) THEN
                    RAISE EXCEPTION 'rdr229 swap of nexus.%1$s refused: it has no valid unique key on (tenant_id, collection_id, %2$s, embedding_model)'
                        USING ERRCODE = 'object_not_in_prerequisite_state';
                END IF;
            """.formatted(table, keyColumn, sortedKey(keyColumn));
    }

    private static String centroidsOnComplete() {
        return "DO $swap$\nBEGIN\n" + requireIdKey("taxonomy_centroids", "topic_id") + "END\n$swap$";
    }

    /** One child's id key into chunks, added NOT VALID if absent. */
    private static String childKey(String table, String constraint, String idColumn, String onDelete, String deferrable) {
        return """
                IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint
                                WHERE conrelid = 'nexus.%1$s'::pg_catalog.regclass AND conname = '%2$s') THEN
                    ALTER TABLE nexus.%1$s ADD CONSTRAINT %2$s
                        FOREIGN KEY (tenant_id, %3$s, %4$s, embedding_model)
                        REFERENCES nexus.chunks (tenant_id, collection_id, chash, embedding_model)
                        ON UPDATE CASCADE%5$s%6$s NOT VALID;
                END IF;
            """.formatted(table, constraint, idColumn, table.equals("topic_assignments") ? "doc_id" : "chash",
                onDelete.isEmpty() ? "" : " ON DELETE " + onDelete, deferrable.isEmpty() ? "" : " " + deferrable);
    }

    /**
     * One validation move's registration: the child's stamp text word for word (a re-stamp of rows still NULL and the verification of
     * them), and an on_complete that validates the id key only if the catalog has it. The INSERT is guarded by the label, so a second
     * pass adds none; its {@code changeset_id} is the swap row's (a move registered by another move's on_complete takes the id of the
     * row whose text holds its INSERT).
     */
    private static String validationMove(String table, String nameColumn, String idColumn, String constraint) {
        String label = validationLabel(table);
        String validate = "DO $val$ BEGIN IF EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = 'nexus." + table
            + "'::pg_catalog.regclass AND conname = '" + constraint + "') THEN ALTER TABLE nexus." + table
            + " VALIDATE CONSTRAINT " + constraint + "; END IF; END $val$";
        return "            INSERT INTO nexus.data_moves (label, template, driving_schema, driving_table, chunk_size, sleep_ms,\n"
            + "                                          statement_timeout_ms, verification, on_complete, changeset_id)\n"
            + "            SELECT '" + label + "',\n"
            + "                   $tpl$" + restamp(nameColumn, idColumn) + "$tpl$,\n"
            + "                   'nexus', '" + table + "', " + CHUNK_SIZE + ", " + SLEEP_MS + ", " + VALIDATION_STATEMENT_TIMEOUT_MS + ",\n"
            + "                   $ver$" + restampVerification(table, nameColumn, idColumn) + "$ver$,\n"
            + "                   $oc$" + validate + "$oc$,\n"
            + "                   (SELECT s.changeset_id FROM nexus.data_moves s WHERE s.label = '" + LeafIndexPass.swapLabel("chunks") + "')\n"
            + "             WHERE NOT EXISTS (SELECT 1 FROM nexus.data_moves m WHERE m.label = '" + label + "');\n";
    }

    private static String chunksOnComplete() {
        return "DO $swap$\nBEGIN\n"
            + requireIdKey("chunks", "chash")
            // the name keys' own ON DELETE actions (vectors-030:1017-1050): the manifest's is NO ACTION, the other two CASCADE.
            // The manifest's id key is DEFERRABLE INITIALLY DEFERRED, NOT its name key's INITIALLY IMMEDIATE (linda, 2026-11-22):
            // purge_trash and its kin defer only fk_catalog_chunks_chunk by name (the live definition of purge_trash,
            // vectors-030:3115, and CatalogRepository#deferManifestChunkFk), and delete chunks while manifest rows still exist, so
            // an immediate id key would raise 23503 there until P3.5b; changing those sites needs changesets against the DDL cap.
            // PostgreSQL never defers a referential action, so ON UPDATE CASCADE still fires at the statement and only the
            // orphan check moves to the commit. The name key stays IMMEDIATE until P4.1; nexus-p2m1x.35 decides this key's final mode.
            + childKey("catalog_document_chunks", "fk_catalog_chunks_chunk_id", "collection_id", "", "DEFERRABLE INITIALLY DEFERRED")
            + childKey("topic_assignments", "topic_assignments_chunk_id_fk", "source_collection_id", "CASCADE", "")
            + childKey("chunk_orphaned_at", "chunk_orphaned_at_chunk_id_fk", "collection_id", "CASCADE", "")
            // the validation moves, registered after the constraints they validate exist in this transaction
            + validationMove("catalog_document_chunks", "collection", "collection_id", "fk_catalog_chunks_chunk_id")
            + validationMove("topic_assignments", "source_collection", "source_collection_id", "topic_assignments_chunk_id_fk")
            + validationMove("chunk_orphaned_at", "collection", "collection_id", "chunk_orphaned_at_chunk_id_fk")
            // the RDR-225 composite on the id: added without NOT VALID, so it validates here (the referencing table is partitioned)
            + """
                IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint
                                WHERE conrelid = 'nexus.chunks'::pg_catalog.regclass AND conname = '%s') THEN
                    ALTER TABLE nexus.chunks ADD CONSTRAINT %s
                        FOREIGN KEY (tenant_id, collection_id, embedding_model)
                        REFERENCES nexus.catalog_collections (tenant_id, collection_id, embedding_model)
                        ON DELETE RESTRICT;
                END IF;
            """.formatted(COMPOSITE_CONSTRAINT, COMPOSITE_CONSTRAINT)
            + "END\n$swap$";
    }
}
