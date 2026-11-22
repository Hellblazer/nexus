// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import org.jooq.DSLContext;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;

import java.util.List;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27): the name-key drop move, the RDR-246 data move that drops the registry name foreign keys that
 * refuse a one-row rename (RDR item 6, Switch). PostgreSQL's default {@code ON UPDATE NO ACTION} refuses the UPDATE of
 * {@code catalog_collections.name} while a row of a referencing table names the old label (T2 {@code 229-research-58}), so the
 * seven keys listed in {@link #NAME_KEYS} go before the one-row rename runs, and the queue's key too where that table still exists
 * (P4.1's primary key swap cannot drop {@code (tenant_id, name)} while a key references it, T2 {@code 229-research-61}).
 *
 * <p><b>The definition is Java, not a changeset.</b> The move is registered at run time, once gate 3 holds, by an engine pass that
 * calls {@link #registerIfAbsent}: never in the migrate phase and never by the read-only {@link CompletionCheck} (Sam's rule, no
 * boot-time data migrations; linda's ruling 2026-11-22 on nexus-p2m1x.27, option (a), runtime form). That pass is stage 2 of P3.7;
 * this class is the one operation it calls. Nothing here runs on its own.
 *
 * <p>Shape: the driving table is the small {@code nexus.catalog_collections}, so the move is short (T2 {@code 229-research-63});
 * the template is a no-op re-stamp of the rows whose id is NULL (the registry's id is an identity column, so none is: the walk has
 * nothing to do), the verification returns those same rows (zero on every install), and {@code on_complete} drops the keys. Because
 * the {@code on_complete} names {@code nexus.chunks} and {@code topic_assignments}, {@link ChunkFamily} classes the move as
 * chunk-family, so {@link VerifyingFinisher} ends the per-collection index builders before it runs, as RDR-246 asks
 * ("Finishing a move"). The throttle is P3.2's (T2 {@code nexus_rdr/229-p32-fork-results-2026-11-20}, Sam's ask C ruling
 * 2026-11-20): {@code chunk_size} 10000, {@code sleep_ms} 8000, {@code statement_timeout_ms} 1400 (so a lock_timeout of 700 ms, under
 * deadlock_timeout of 1 s), measured at 21 ms holding ACCESS EXCLUSIVE on the registry, the chunks tree and six tables. Raise rule: if
 * the move times out twice (57014 or 55P03), raise it alone to 4000 and record why.
 */
public final class NameKeyDropMove {

    /** The move's label. {@link CompletionCheck} reads its {@code completed_at}; a label with no row is incomplete. */
    public static final String LABEL = "rdr229-name-key-drop";

    /**
     * {@code changeset_id} of the row. The move has no changeset (it is registered by an engine pass), so the column, NOT NULL,
     * names the registrant.
     */
    public static final String CHANGESET_ID = "engine-pass:rdr229-name-key-drop";

    /** One registry name foreign key: the table it is on and its constraint name. */
    public record NameKey(String table, String constraint) { }

    /** The seven keys to {@code (tenant_id, name)} that refuse a one-row rename (RDR item 6, Switch), in a fixed order. */
    public static final List<NameKey> NAME_KEYS = List.of(
        new NameKey("chunks", "chunks_collection_fk"),
        new NameKey("canary_query_ring", "canary_query_ring_collection_fk"),
        new NameKey("document_aspects", "document_aspects_collection_fk"),
        new NameKey("topics", "topics_collection_fk"),
        new NameKey("taxonomy_meta", "taxonomy_meta_collection_fk"),
        new NameKey("document_highlights", "document_highlights_collection_fk"),
        new NameKey("topic_assignments", "topic_assignments_collection_fk"));

    /** The queue's key: ON UPDATE CASCADE refuses nothing, but P4.1's swap cannot drop the name key under it. Guarded: the table is absent on most installs. */
    public static final NameKey QUEUE_KEY = new NameKey("aspect_extraction_queue", "aspect_extraction_queue_collection_fk");

    static final String DRIVING_SCHEMA = "nexus";
    static final String DRIVING_TABLE = "catalog_collections";
    static final int CHUNK_SIZE = 10_000;
    static final int SLEEP_MS = 8_000;
    static final int STATEMENT_TIMEOUT_MS = 1_400;

    /** The no-op re-stamp: the registry's id rewritten to itself where it is NULL. {@code <driving_table>} expands to the table with alias {@code t}. */
    static final String TEMPLATE = "UPDATE <driving_table> SET collection_id = t.collection_id "
        + "WHERE t.collection_id IS NULL AND <chunking_clause>";

    /** Registry rows with a NULL id; the one bind is the tenant, NULL for the table-wide run. */
    static final String VERIFICATION = "SELECT 1 FROM nexus.catalog_collections t WHERE t.collection_id IS NULL "
        + "AND (t.tenant_id = $1 OR $1 IS NULL)";

    private NameKeyDropMove() { /* static */ }

    /** The {@code on_complete}: one DO block, every drop {@code IF EXISTS}, the queue's under {@code to_regclass(...) IS NOT NULL} (the guard fk-002:217-223 uses). */
    static String onComplete() {
        StringBuilder b = new StringBuilder("DO $v$ BEGIN ");
        for (NameKey k : NAME_KEYS) {
            b.append("ALTER TABLE IF EXISTS nexus.").append(k.table()).append(" DROP CONSTRAINT IF EXISTS ").append(k.constraint())
                .append("; ");
        }
        b.append("IF to_regclass('nexus.").append(QUEUE_KEY.table()).append("') IS NOT NULL THEN ALTER TABLE nexus.")
            .append(QUEUE_KEY.table()).append(" DROP CONSTRAINT IF EXISTS ").append(QUEUE_KEY.constraint())
            .append("; END IF; END $v$");
        return b.toString();
    }

    /**
     * Register the move if {@code snapshot} says gate 3 holds and no row of {@link #LABEL} exists: ONE INSERT into
     * {@code nexus.data_moves}, {@code ON CONFLICT (label) DO NOTHING}, so a second call, a second engine and a reboot add nothing. The
     * caller owns the connection: {@code admin} is a context on the admin connection (the migrating role, the only role that writes the
     * registry). It runs one statement and keeps no state.
     *
     * <p>The caller is the engine pass (stage 2) and, until then, the tests' harness. The pass also reports in {@code boot_work} and
     * yields to the migration lock and the walk's termination list; none of that is this method's.
     *
     * @param snapshot the completion check's latest result; null (not read yet) or gate 3 not holding registers nothing
     * @return true when this call inserted the row
     */
    public static boolean registerIfAbsent(CompletionCheck.Result snapshot, DSLContext admin) {
        if (snapshot == null || !snapshot.gate3()) {
            return false;
        }
        var moves = DSL.table(DSL.name("nexus", "data_moves"));
        return admin.insertInto(moves,
                DSL.field(DSL.name("label"), String.class), DSL.field(DSL.name("template"), String.class),
                DSL.field(DSL.name("driving_schema"), String.class), DSL.field(DSL.name("driving_table"), String.class),
                DSL.field(DSL.name("chunk_size"), SQLDataType.INTEGER), DSL.field(DSL.name("sleep_ms"), SQLDataType.INTEGER),
                DSL.field(DSL.name("statement_timeout_ms"), SQLDataType.INTEGER),
                DSL.field(DSL.name("verification"), String.class), DSL.field(DSL.name("on_complete"), String.class),
                DSL.field(DSL.name("changeset_id"), String.class))
            .values(LABEL, TEMPLATE, DRIVING_SCHEMA, DRIVING_TABLE, CHUNK_SIZE, SLEEP_MS, STATEMENT_TIMEOUT_MS, VERIFICATION,
                onComplete(), CHANGESET_ID)
            .onConflict(DSL.field(DSL.name("label"), String.class)).doNothing()
            .execute() == 1;
    }
}
