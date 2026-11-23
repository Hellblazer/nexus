// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.datamove.DataMove;

/**
 * RDR-229 P3.3c (nexus-p2m1x.21; linda's ruling and Sam's, T2 {@code nexus/sam-ruling-validate-bounds-lock-wait-only-2026-11-23}): the
 * per-leaf route of the composite foreign key {@code chunks_collection_id_fk} from {@code nexus.chunks} to the registry, as SQL text that
 * the chunks swap's {@code on_complete} writes into the registry. {@link Rdr229SwapMove} calls it; nothing here runs SQL.
 *
 * <p><b>Why.</b> PostgreSQL 17 takes no {@code NOT VALID} foreign key on a partitioned table, so the composite added on the parent was
 * validated in the statement that added it: SHARE ROW EXCLUSIVE on chunks, every leaf and the registry for a scan whose cost grows with
 * the rows, inside the swap's 1400 ms bound. Now the parent ADD is a catalog-only step, because every leaf already carries a validated foreign
 * key that PostgreSQL adopts (measured on PG 17.11, T3 {@code pg17-partitioned-fk-adoption-measured-2026-11-23}: 2 ms, no scan, on a table
 * with a 2M-row leaf).
 *
 * <p><b>The three steps, three kinds of move.</b>
 * <ol>
 *   <li>The chunks swap's {@code on_complete} (tight bound, catalog only): for every leaf of chunks that has no validated matching key, add
 *       {@code <leaf>_cid_fk} {@code NOT VALID} (SHARE ROW EXCLUSIVE on the leaf and the registry for an instant) and register one
 *       <i>leaf validation move</i>; then register the <i>adoption move</i> {@link #ADOPT_LABEL}.</li>
 *   <li>A leaf validation move ({@link #LEAF_LABEL_PREFIX}{@code <leaf>}): its {@code on_complete} validates the leaf's key, SHARE UPDATE
 *       EXCLUSIVE on the leaf and ROW SHARE on the registry, so no writer waits on it. Each is its own completion transaction, so a restart
 *       loses at most the leaf in flight. It runs with no statement bound ({@link DataMove#NO_STATEMENT_BOUND_MS}) and a lock wait only.</li>
 *   <li>The adoption move ({@link #ADOPT_LABEL}, tight bound 1400 ms): its {@code on_complete} first checks, in the same transaction, that
 *       EVERY leaf has a validated key that matches the composite exactly ({@link #hasValidatedMatch}). Only then does it add the parent
 *       constraint, which PostgreSQL adopts leaf by leaf with no scan. If a leaf lacks one (a leaf created after the swap, or a key dropped), the
 *       parent is NOT added (PostgreSQL would silently build a second, scanned key on that leaf): the step adds the leaf's key NOT VALID,
 *       registers or re-opens the leaf's validation move, and re-opens itself, so the move stays open and retries; when everything it
 *       needed is already queued it raises 55P03 (transient: blocked with a backoff), as the swap's child-index wait does.</li>
 * </ol>
 *
 * <p><b>One definition.</b> {@link #FK_DEFINITION} is the one text of the key, used for the leaves and for the parent, so they cannot drift:
 * PostgreSQL adopts a leaf key only when columns, their order, the referenced key, the delete and update actions, the match type and the
 * deferrability all agree (measured: a mismatched or NOT VALID leaf key is not an error, the parent ADD builds a new one and scans).
 *
 * <p><b>The moves' own text.</b> A leaf validation move and the adoption move are registered with the registry's own stamp move text
 * ({@code rdr229-stamp-catalog_collections}, vectors-070-1): a walk of the small registry table and a verification that returns nothing once
 * it has run, so a re-check in the completion transaction costs a few milliseconds and their runs do not walk chunks. They are registered
 * with {@code registered_at = clock_timestamp()} in the order leaves first, adoption last, so the worker (oldest first) validates the leaves
 * before the adoption move looks.
 */
public final class Rdr229ChunksFk {

    private Rdr229ChunksFk() { /* static */ }

    /** The adoption move: adds the composite on the parent once every leaf has its validated key. */
    public static final String ADOPT_LABEL = "rdr229-fkadopt-chunks";

    /** A leaf validation move is this prefix and the leaf's table name; a label holds no {@code /} (the registry's CHECK). */
    public static final String LEAF_LABEL_PREFIX = "rdr229-fkvalidate-chunks-leaf-";

    /** A leaf's key is its table name and this suffix (the id unique index of the leaf is named with {@code _cid_uq}, the same length). */
    public static final String LEAF_CONSTRAINT_SUFFIX = "_cid_fk";

    /** The composite on the id from nexus.chunks to the registry: the parent's constraint name. */
    public static final String PARENT_CONSTRAINT = Rdr229SwapMove.COMPOSITE_CONSTRAINT;

    /** The referencing columns, in order; the leaf keys and the parent key all use this one list. */
    static final String FK_COLUMNS = "tenant_id, collection_id, embedding_model";

    /**
     * The key itself, the one text of every key this class adds, on the parent and on every leaf. ON DELETE RESTRICT, ON UPDATE NO ACTION,
     * MATCH SIMPLE and NOT DEFERRABLE are what the RDR-225 composite is; the actions are spelled so the leaf and the parent cannot differ.
     */
    public static final String FK_DEFINITION = "FOREIGN KEY (" + FK_COLUMNS + ") REFERENCES nexus.catalog_collections (" + FK_COLUMNS
        + ") ON DELETE RESTRICT";

    /** The registry's stamp move text (vectors-070-1), word for word, as dollar-quoted text: the template and the verification. */
    private static final String REGISTRY_TEMPLATE = "UPDATE <driving_table> SET superseded_by_id = COALESCE((SELECT s.collection_id FROM "
        + "nexus.catalog_collections s WHERE s.tenant_id = t.tenant_id AND s.name = t.superseded_by), t.collection_id) WHERE "
        + "t.superseded_by <> '' AND t.superseded_by_id IS NULL AND <chunking_clause>";
    private static final String REGISTRY_VERIFICATION = "SELECT 1 FROM nexus.catalog_collections t WHERE t.superseded_by <> '' AND "
        + "t.superseded_by_id IS NULL AND (t.tenant_id = $1 OR $1 IS NULL)";

    /**
     * SQL: true when leaf {@code rel} (a {@code regclass} expression) carries a validated foreign key to the registry that PostgreSQL
     * adopts: same columns in the same order, same referenced columns, ON DELETE RESTRICT (r), ON UPDATE NO ACTION (a), MATCH SIMPLE (s), not
     * deferrable. The column numbers are read from each table, so a leaf with a dropped column is compared by name.
     */
    public static String hasValidatedMatch(String rel) {
        return matching(rel, true);
    }

    /** As {@link #hasValidatedMatch}, whether or not the key is validated: a leaf with one has nothing to add (only to validate). */
    public static String hasMatch(String rel) {
        return matching(rel, false);
    }

    private static String matching(String rel, boolean validated) {
        return "EXISTS (SELECT 1 FROM pg_catalog.pg_constraint k"
            + " WHERE k.conrelid = " + rel + " AND k.contype = 'f'" + (validated ? " AND k.convalidated" : "")
            + " AND k.confrelid = 'nexus.catalog_collections'::pg_catalog.regclass"
            + " AND k.conkey = " + attnums(rel, "tenant_id", "collection_id", "embedding_model")
            + " AND k.confkey = " + attnums("'nexus.catalog_collections'::pg_catalog.regclass", "tenant_id", "collection_id", "embedding_model")
            + " AND k.confupdtype = 'a' AND k.confdeltype = 'r' AND k.confmatchtype = 's' AND NOT k.condeferrable AND NOT k.condeferred"
            // not attached to another key already, no SET NULL / SET DEFAULT column list (PG15+), no PERIOD (PG18+: the column does not
            // exist before it, so it is read through to_jsonb)
            + " AND k.conparentid = 0 AND k.confdelsetcols IS NULL"
            + " AND NOT COALESCE((pg_catalog.to_jsonb(k) ->> 'conperiod')::pg_catalog.bool, false))";
    }

    /** SQL: the attnums of {@code columns} of relation {@code rel}, in that order, as an int2[]. */
    private static String attnums(String rel, String... columns) {
        StringBuilder sb = new StringBuilder("ARRAY[");
        for (int i = 0; i < columns.length; i++) {
            sb.append(i == 0 ? "" : ", ").append("(SELECT a.attnum FROM pg_catalog.pg_attribute a WHERE a.attrelid = ").append(rel)
                .append(" AND a.attname = '").append(columns[i]).append("' AND NOT a.attisdropped)");
        }
        return sb.append("]::pg_catalog.int2[]").toString();
    }

    /** PL/pgSQL declarations the {@link #reconcileLeaves} block needs. */
    static final String RECONCILE_DECLARE = """
            l             record;
            n             bigint;
            pending       boolean := false;
            paused_names  text;
            is_failed     boolean;
            is_paused     boolean;
            is_superseded boolean;
        """;

    /**
     * PL/pgSQL (no DECLARE, no BEGIN): for every leaf of nexus.chunks without a validated matching key, add the key NOT VALID if the leaf has
     * none, and register its validation move or re-open a finished one. Sets {@code pending} when any leaf needed it. Idempotent; the leaf's
     * catalog is the only input. With {@code strict} (the adoption move) a leaf whose validation move is failed or superseded raises
     * SQLSTATE 55000 naming it (the adoption cannot go on until an operator re-arms it, and it must not wait on it for ever), and a paused one
     * is collected in {@code paused_names} for the caller to raise 55P03 with.
     */
    static String reconcileLeaves(boolean strict) {
        String labelExpr = "'" + LEAF_LABEL_PREFIX + "' || l.leaf";
        String check = strict ? """
                    SELECT m.failed_at IS NOT NULL, m.paused_at IS NOT NULL,
                           EXISTS (SELECT 1 FROM nexus.data_moves s WHERE s.supersedes = m.label)
                      INTO is_failed, is_paused, is_superseded
                      FROM nexus.data_moves m WHERE m.label = %1$s;
                    IF is_failed OR is_superseded THEN
                        RAISE EXCEPTION USING MESSAGE = 'rdr229 adoption of %2$s is stuck: the validation move ' || %1$s || ' is '
                                || CASE WHEN is_failed THEN 'failed' ELSE 'superseded' END || '; re-arm it to go on',
                            ERRCODE = 'object_not_in_prerequisite_state';
                    END IF;
                    IF is_paused THEN
                        paused_names := COALESCE(paused_names || ', ', '') || l.leaf;
                    END IF;
            """.formatted(labelExpr, PARENT_CONSTRAINT) : "";
        return """
                FOR l IN SELECT pt.relid::pg_catalog.regclass AS rel, c.relname::pg_catalog.text AS leaf, n.nspname::pg_catalog.text AS ns
                           FROM pg_catalog.pg_partition_tree('nexus.chunks'::pg_catalog.regclass) pt
                           JOIN pg_catalog.pg_class c ON c.oid = pt.relid
                           JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                          WHERE pt.isleaf AND NOT %1$s
                          ORDER BY c.relname
                LOOP
                    pending := true;
                    IF NOT %2$s THEN
                        EXECUTE pg_catalog.format('ALTER TABLE %%I.%%I ADD CONSTRAINT %%I %3$s NOT VALID', l.ns, l.leaf, l.leaf || '%4$s');
                    END IF;
                    INSERT INTO nexus.data_moves (label, template, driving_schema, driving_table, chunk_size, sleep_ms,
                                                  statement_timeout_ms, verification, on_complete, changeset_id, registered_at)
                    SELECT %5$s,
                           $ltpl$%6$s$ltpl$,
                           'nexus', 'catalog_collections', 1000, 0, %7$d,
                           $lver$%8$s$lver$,
                           pg_catalog.format($lv$%9$s $lv$, l.ns, l.leaf),
                           (SELECT s.changeset_id FROM nexus.data_moves s WHERE s.label = '%10$s'),
                           pg_catalog.clock_timestamp()
                     WHERE NOT EXISTS (SELECT 1 FROM nexus.data_moves m WHERE m.label = %5$s);
                    UPDATE nexus.data_moves SET completed_at = NULL WHERE label = %5$s AND completed_at IS NOT NULL;
            %11$s    END LOOP;
            """.formatted(hasValidatedMatch("pt.relid"), hasMatch("l.rel"), FK_DEFINITION, LEAF_CONSTRAINT_SUFFIX, labelExpr,
                REGISTRY_TEMPLATE, DataMove.NO_STATEMENT_BOUND_MS, REGISTRY_VERIFICATION, leafOnComplete(),
                LeafIndexPass.swapLabel("chunks"), check);
    }

    /**
     * The on_complete of a leaf validation move, as a {@code format} template with {@code %1$L} the leaf's schema and {@code %2$L} its name as
     * literals. It validates each key of the leaf to the registry that is not validated yet, whatever its name. A leaf that is gone raises
     * 42P01 naming it: a move that finds nothing to validate because the leaf is not there has not validated anything, so it fails loud rather
     * than completing. The text has no percent sign of its own (the swap formats it once more).
     */
    public static String leafOnComplete() {
        return "DO $v$ DECLARE c record; t pg_catalog.text; BEGIN t := pg_catalog.quote_ident(%1$L) || '.' || pg_catalog.quote_ident(%2$L); "
            + "IF pg_catalog.to_regclass(t) IS NULL THEN RAISE EXCEPTION USING MESSAGE = 'rdr229 leaf validation: ' || t || ' does not exist', "
            + "ERRCODE = 'undefined_table'; END IF; "
            + "FOR c IN SELECT k.conname FROM pg_catalog.pg_constraint k WHERE k.conrelid = pg_catalog.to_regclass(t) AND k.contype = 'f' "
            + "AND NOT k.convalidated AND k.confrelid = 'nexus.catalog_collections'::pg_catalog.regclass LOOP "
            + "EXECUTE 'ALTER TABLE ' || t || ' VALIDATE CONSTRAINT ' || pg_catalog.quote_ident(c.conname); END LOOP; END $v$";
    }

    /** PL/pgSQL for the swap's own on_complete: the leaf keys and moves, then the adoption move (registered once, after the leaves). */
    public static String swapSteps() {
        return reconcileLeaves(false) + registerAdoptMove();
    }

    private static String registerAdoptMove() {
        return """
                INSERT INTO nexus.data_moves (label, template, driving_schema, driving_table, chunk_size, sleep_ms,
                                              statement_timeout_ms, verification, on_complete, changeset_id, registered_at)
                SELECT '%1$s',
                       $atpl$%2$s$atpl$,
                       'nexus', 'catalog_collections', 1000, 0, %3$d,
                       $aver$%4$s$aver$,
                       $aoc$%5$s$aoc$,
                       (SELECT s.changeset_id FROM nexus.data_moves s WHERE s.label = '%6$s'),
                       pg_catalog.clock_timestamp()
                 WHERE NOT EXISTS (SELECT 1 FROM nexus.data_moves m WHERE m.label = '%1$s');
            """.formatted(ADOPT_LABEL, REGISTRY_TEMPLATE, Rdr229SwapMove.STATEMENT_TIMEOUT_MS, REGISTRY_VERIFICATION, adoptOnComplete(),
                LeafIndexPass.swapLabel("chunks"));
    }

    /**
     * The adoption move's on_complete (one DO block, no EXCEPTION clause). It takes SHARE ROW EXCLUSIVE on chunks (every partition) first, so
     * no leaf is created between its check and the key it adds (a leaf is created with ACCESS EXCLUSIVE on its model partition, which this
     * lock conflicts with); the lock wait is the move's 700 ms. Done when the composite exists. Otherwise: reconcile the leaves; if any
     * has no validated matching key the move re-opens itself (completed_at back to NULL, and registered_at now, so it sorts after the leaf
     * moves just queued) and returns, which the finisher reads as "not done", not as a completion; a leaf move that is failed or superseded
     * raises 55000 naming it; one that is paused raises 55P03 naming the leaf. With every leaf validated it adds the parent constraint, which
     * PostgreSQL adopts leaf by leaf with no scan.
     */
    public static String adoptOnComplete() {
        return "DO $adopt$\nDECLARE\n" + RECONCILE_DECLARE + "BEGIN\n"
            + """
                IF EXISTS (SELECT 1 FROM pg_catalog.pg_constraint
                            WHERE conrelid = 'nexus.chunks'::pg_catalog.regclass AND conname = '%1$s') THEN
                    RETURN;
                END IF;
                LOCK TABLE nexus.chunks IN SHARE ROW EXCLUSIVE MODE;
            """.formatted(PARENT_CONSTRAINT)
            + reconcileLeaves(true)
            + """
                IF paused_names IS NOT NULL THEN
                    RAISE EXCEPTION USING MESSAGE = 'rdr229 adoption of %2$s is waiting: the validation move of leaf ' || paused_names || ' is paused',
                        ERRCODE = 'lock_not_available';
                END IF;
                IF pending THEN
                    UPDATE nexus.data_moves SET completed_at = NULL, registered_at = pg_catalog.clock_timestamp() WHERE label = '%1$s';
                    RETURN;
                END IF;
                ALTER TABLE nexus.chunks ADD CONSTRAINT %2$s %3$s;
            """.formatted(ADOPT_LABEL, PARENT_CONSTRAINT, FK_DEFINITION)
            + "END\n$adopt$";
    }
}
