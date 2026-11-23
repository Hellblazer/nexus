// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.db.JsonbSupport.jsonbRequired;
import static dev.nexus.service.db.TaxonomyBatchRows.*;
import static dev.nexus.service.jooq.nexus.Tables.*;
import static org.jooq.impl.DSL.*;

/**
 * Two statics of the taxonomy fidelity import, moved out of {@link TaxonomyRepository} unchanged so that class stays under its
 * file-size ceiling (RDR-229 P3.5c assembly, nexus-p2m1x.25): the topics id sequence advance and the topic-links batch.
 */
final class TaxonomyImportSupport {

    private TaxonomyImportSupport() { /* static */ }

    /** Bind-parameter bound of one multi-row import statement (PG's Int16 bind-count limit, with room). */
    static final int MAX_BATCH_PARAMS = 30_000;

    /**
     * g37fr FINDING 2 (RDR-155 P4b, engine v0.1.53): fidelity import
     * preserves source ids verbatim, which leaves the topics BIGSERIAL
     * sequence BEHIND the imported ids — the next live serial INSERT
     * (persist_rebuild) then collides 409 on a shared store. Standard PG
     * import discipline: advance the sequence past the imported id inside
     * the import transaction. The GREATEST against the sequence's own
     * last_value means setval never moves the sequence backward. Requires
     * UPDATE on the sequence (granted to nexus_svc by taxonomy-005).
     *
     * NOT atomic against a concurrent nextval() on the same sequence: the
     * last_value read and the setval are two steps, so a live serial
     * INSERT racing this import could claim an id above the value setval
     * then writes. Accepted: fidelity import is a migration/seeding path,
     * never concurrent with live topic writes for the same tenant; if an
     * import-during-serving path ever appears, revisit with a lock on the
     * sequence or an advisory lock keyed on the table.
     */
    // nexus-zrcj7 (Sam's no-SQL-strings-in-Java directive): retired from a raw
    // ctx.execute("SELECT setval(pg_get_serial_sequence(...), GREATEST(...))", ...)
    // template onto typed jOOQ DSL -- setval/pg_get_serial_sequence are ordinary
    // PostgreSQL functions (DSL.function(name, type, boundArgs...), the same idiom
    // this class already uses at set_config/pg_advisory_xact_lock/hashtext above);
    // GREATEST is jOOQ core's own DSL.greatest(...); the scalar subquery
    // "(SELECT last_value FROM nexus.topics_id_seq)" is jOOQ's type-safe
    // field(Select) scalar-subquery wrapping (manual: "SQL building > Column
    // expressions > Scalar subqueries") over DSL.table(DSL.name(...)) + a
    // DSL.name(...)-addressed column -- the same safe quoted-identifier idiom
    // other engine code uses for a schema-qualified
    // relation this class has no generated Table for (a bare PostgreSQL sequence
    // read as a one-row relation, which jOOQ codegen does not model as a Table).
    static void advanceTopicsIdSequence(DSLContext ctx, long maxImportedId) {
        Field<Long> lastValue = field(name("last_value"), Long.class);
        Field<Long> lastValueSubquery =
            field(select(lastValue).from(table(name("nexus", "topics_id_seq"))));
        ctx.select(function("setval", Long.class,
                function("pg_get_serial_sequence", String.class, val("nexus.topics"), val("id")),
                greatest(lastValueSubquery, val(maxImportedId))))
           .fetch();
    }

    static int importLinksBatch(DSLContext ctx, String tenant, List<Map<String, Object>> rows) {
        // Conflict key: (tenant_id, from_topic_id, to_topic_id). Sort by
        // (from_topic_id, to_topic_id) so concurrent batches lock TOPIC_LINKS rows in
        // one global order — deadlock avoidance, nexus-ps9wb.
        var unique = new LinkedHashMap<String, Map<String, Object>>(rows.size());
        for (var r : rows) unique.put(reqL(r, "from_topic_id") + "::" + reqL(r, "to_topic_id"), r);
        List<Map<String, Object>> deduped = new ArrayList<>(unique.values());
        deduped.sort(Comparator
                .comparing((Map<String, Object> r) -> reqL(r, "from_topic_id"))
                .thenComparing(r -> reqL(r, "to_topic_id")));

        final int cols = 5;
        final int chunkSize = Math.max(1, MAX_BATCH_PARAMS / cols);
        for (int start = 0; start < deduped.size(); start += chunkSize) {
            List<Map<String, Object>> batch = deduped.subList(start, Math.min(start + chunkSize, deduped.size()));
            var insert = ctx.insertInto(TOPIC_LINKS,
                    TOPIC_LINKS.TENANT_ID, TOPIC_LINKS.FROM_TOPIC_ID,
                    TOPIC_LINKS.TO_TOPIC_ID, TOPIC_LINKS.LINK_COUNT, TOPIC_LINKS.LINK_TYPES);
            for (var r : batch) {
                // nexus-cefa1.6: see upsertTopicLink's identical jsonbRequired comment.
                insert = insert.values(tenant, reqL(r, "from_topic_id"), reqL(r, "to_topic_id"),
                        optI(r, "link_count", 0), jsonbRequired(optS(r, "link_types"), "link_types"));
            }
            insert.onConflict(TOPIC_LINKS.TENANT_ID, TOPIC_LINKS.FROM_TOPIC_ID, TOPIC_LINKS.TO_TOPIC_ID)
                  .doUpdate()
                  .set(TOPIC_LINKS.LINK_COUNT,
                       field("GREATEST(nexus.topic_links.link_count, EXCLUDED.link_count)", Integer.class))
                  .set(TOPIC_LINKS.LINK_TYPES, field("EXCLUDED.link_types", JSONB.class))
                  .execute();
        }
        return rows.size();
    }
}
