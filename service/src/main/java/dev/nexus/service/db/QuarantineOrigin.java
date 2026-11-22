// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;

import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.impl.DSL;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

/**
 * The quarantine rows of one ORIGIN collection, found, deleted and retagged inside the caller's transaction
 * (nexus-wbfpw.68 producer half, nexus-wbfpw.71; Sam, 2026-10-03, option (b)).
 *
 * <p>RDR-192 Day 2 says every move and expiry of a quarantined chunk is audited. Collection delete and rename
 * were the paths that broke it: delete removed the origin's rows by {@code collection = name} and left its
 * {@code quarantine-} sibling behind, rename left the sibling's rows tagged for a name that no longer exists.
 * Both produce rows that nothing expires. Here the delete takes them with the origin and the rename retags them,
 * each with one {@code gc_audit} row per sibling, written in the same transaction as the change it documents.
 *
 * <p><strong>Which rows are an origin's.</strong> A quarantine row belongs to {@code X} when it sits in a
 * registered quarantine collection and either carries {@code metadata.origin_collection = X} (what the engine's
 * move and the client's move both write), or carries no tag at all and can be attributed to {@code X} alone:
 * it sits in {@code quarantine-X} (the reaper's name, which embeds the origin exactly, so this arm is
 * unconditional), or it sits in the sibling derived from {@code X}'s catalog ROW
 * ({@code quarantine-<content_type>__<owner_id>__<embedding_model>__<model_version>}, the client's name) AND
 * {@code X} is the only registered live collection ({@code superseded_by = ''}, not a quarantine collection)
 * with those row attributes. When another live collection shares them (a slug twin and a conformant twin, say)
 * an untagged row in the shared sibling could be either's, so neither claims it: it stays where it is instead
 * of being deleted with, or retagged for, the wrong origin. A rename tombstone does not count as a twin (it is
 * retired), and a rename leaves its own target out of the count, because the target is registered with
 * {@code X}'s attributes by the time the retag runs. A row tagged for another origin is never {@code X}'s,
 * whichever sibling it sits in, so two origins that share a sibling each take only their own tagged rows. The
 * set is the one {@link PgVectorRepository#resolveQuarantineSiblings} and the restore function already use,
 * plus the untagged case they treat as "any origin that asks".
 *
 * <p>Statements are typed jOOQ, no SQL template ({@code RawSqlGateTest}). Nothing here writes a manifest table,
 * so none of it needs {@code DeadlockRetry} of its own; the two callers that matter are already inside
 * {@code manifestWriteTxn}.
 */
public final class QuarantineOrigin {

    /**
     * The name prefix a quarantine collection is LABELLED with (the reaper's {@code quarantine-<origin>}, the
     * client's row-derived sibling). Since RDR-229 P2.5 it builds and finds names; whether a collection IS a
     * quarantine collection is its row's {@code lifecycle_state} ({@link #isQuarantine}).
     */
    public static final String PREFIX = "quarantine-";

    /**
     * The metadata key beside {@code origin_collection} that holds the origin's registry {@code collection_id}
     * (RDR-229 P3.1). A JSONB key, not a column; the move writes the name tag, and a retag writes both.
     */
    public static final String ORIGIN_ID_KEY = "origin_collection_id";

    /** Origin collection deleted: its rows were taken out of one sibling. */
    public static final String OP_DELETE_ORIGIN = "collection_delete_quarantine";
    /** A quarantine collection itself was deleted. */
    public static final String OP_DELETE_QUARANTINE = "quarantine_collection_delete";
    /** Origin collection renamed: its rows in one sibling were retagged. */
    public static final String OP_RETAG = "quarantine_retag";

    /** The actor of an engine-side audit row, as the SQL-side producers write it. */
    private static final String ACTOR = "engine";

    private static final JSONB EMPTY_OBJECT = JSONB.jsonb("{}");

    private static final Field<String> ORIGIN_TAG = DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, "origin_collection");

    private QuarantineOrigin() {
    }

    /**
     * True for a {@code quarantine-*} collection NAME. Private since RDR-229 P2.5: it is the rule for a name that
     * has no catalog row (a rename or rehome TARGET, where no row exists yet, and a name nothing registered), and
     * nothing outside this class asks a bare name any more. A collection that has a row is judged by the row.
     */
    private static boolean isQuarantineName(String name) {
        return name != null && name.startsWith(PREFIX);
    }

    /**
     * Whether a collection is a quarantine collection, given the {@code lifecycle_state} of its catalog row
     * ({@link CollectionRegistry#lifecycleState}) or empty when it has none (RDR-229 P2.5). A row decides: a
     * {@code quarantine-} name whose row is {@code live} is not one, and a name with no prefix whose row says
     * {@code quarantine} is. With no row the name is judged by its prefix, which is what every guard did before
     * the row was consulted, so a name nothing registered keeps its old outcome.
     */
    public static boolean isQuarantine(Optional<String> lifecycleState, String name) {
        return lifecycleState.map("quarantine"::equals).orElseGet(() -> isQuarantineName(name));
    }

    /** {@link #isQuarantine(Optional, String)} with the row read on the caller's own transaction. */
    public static boolean isQuarantine(DSLContext ctx, String tenant, String name) {
        return isQuarantine(CollectionRegistry.lifecycleState(ctx, tenant, name), name);
    }

    /** {@link #isQuarantine(Optional, String)} with the row read through the registry cache or a short read. */
    public static boolean isQuarantine(TenantScope scope, String tenant, String name) {
        return isQuarantine(CollectionRegistry.lifecycleState(scope, tenant, name), name);
    }

    /**
     * Refuses a collection that is a quarantine collection, naming the sanctioned verbs (RDR-229 P2.5; store-delete,
     * and BOTH ends of rename and rehome). The catalog row decides ({@link #isQuarantine}): the registry cache when
     * the row is cached, else one single-column read in a short transaction. A name with no row, such as a rename or
     * rehome target not yet registered, is judged by its prefix, as it always was. (The allow-list CHECK, a name
     * rule, is a separate guard and is not touched.)
     *
     * @param verb what the caller asked for, as the message names it ({@code store-delete}, {@code rename}, ...)
     * @throws IllegalArgumentException when {@code name} is a quarantine collection (a 400 on every route)
     */
    public static void requireNotQuarantine(TenantScope scope, String tenant, String verb, String name) {
        if (isQuarantine(scope, tenant, name)) {
            throw refused(verb, name);
        }
    }

    /**
     * The manifest-less census and reapable listing's refusal (RDR-192 MVV (a)): a quarantine collection is out of
     * both by construction. {@code lifecycleState} is the collection's row state, or empty for a name with no row
     * (RDR-229 P2.5; such a name is judged by its prefix, as before).
     */
    public static void requireOutOfCensus(String collection, Optional<String> lifecycleState) {
        if (isQuarantine(lifecycleState, collection)) {
            throw new IllegalArgumentException(
                "collection " + collection + " is a quarantine collection; quarantine rows "
                + "are out of the manifest-less census and the reapable listing by construction (RDR-192 MVV (a))");
        }
    }

    /**
     * The sibling a mover route names ({@code POST /v1/vectors/gc/quarantine-orphans}): the SQL mover registers whatever
     * it is given as a {@code quarantine} collection, so a name without the prefix would mint a quarantine row
     * with a plain name. Refused, a registration-side name guard like the client route's (RDR-229 P2.5).
     */
    public static void requireMoverSibling(String quarantineCollection) {
        if (!isQuarantineName(quarantineCollection)) {
            throw new IllegalArgumentException("quarantine_collection " + quarantineCollection + " must start with the "
                + "prefix quarantine- : the move registers it as a quarantine collection, and only a quarantine- name "
                + "may be one");
        }
    }

    private static IllegalArgumentException refused(String verb, String name) {
        return new IllegalArgumentException(
            verb + " is refused on the quarantine collection " + name + ": it would move or delete quarantined "
            + "chunks outside the audited paths. Return chunks to their origin with `nx t3 quarantine restore`, "
            + "let `nx t3 gc` expire them (client expiry), or delete the whole quarantine collection with "
            + "collection delete, which writes a gc_audit row.");
    }

    /**
     * The rows of {@code origin} in every registered quarantine collection of {@code tenant}.
     *
     * <p>The "untagged" arm: the reaper's own sibling is found by NAME ({@code quarantine-} + the origin's
     * name, which embeds the origin exactly). The client's sibling is found by the origin's catalog ROW, not by
     * a name this class would have to build: a registered quarantine collection whose own registered
     * {@code owner_id}, {@code embedding_model} and {@code model_version} equal the origin row's, and whose
     * {@code content_type} is the origin's or {@code quarantine-} + the origin's (the two shapes the engine's
     * registration has written: the older functions split the sibling name, the later ones copy the origin's
     * row). The engine never parses or renders collection names ({@code CollectionParseGateTest}), and this is
     * the same relation, read from the registry. That arm applies only when the origin is the sole live
     * collection with those row attributes (see the class doc). An origin with no catalog row (the production
     * dead-origin shape) has only the reaper's name.
     *
     * @param exceptName a live collection to leave out of the sole-claimant count (a rename's target, which
     *                   shares the origin's attributes once its row is written), or {@code null}
     */
    private static Condition rowsOf(String tenant, String origin, String exceptName) {
        var sib = CATALOG_COLLECTIONS.as("quarantine_sibling");
        var org = CATALOG_COLLECTIONS.as("quarantine_origin");
        var twin = CATALOG_COLLECTIONS.as("quarantine_twin");
        Condition inQuarantine = CHUNKS.COLLECTION.in(
            DSL.select(CATALOG_COLLECTIONS.NAME).from(CATALOG_COLLECTIONS)
               .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)
                   .and(CATALOG_COLLECTIONS.LIFECYCLE_STATE.eq("quarantine"))));
        Condition anotherLiveTwin = twin.TENANT_ID.eq(tenant)
            .and(twin.NAME.ne(origin))
            .and(exceptName == null ? DSL.noCondition() : twin.NAME.ne(exceptName))
            .and(twin.LIFECYCLE_STATE.ne("quarantine"))
            .and(twin.SUPERSEDED_BY.eq(""))
            .and(twin.OWNER_ID.eq(org.OWNER_ID))
            .and(twin.EMBEDDING_MODEL.eq(org.EMBEDDING_MODEL))
            .and(twin.MODEL_VERSION.eq(org.MODEL_VERSION))
            .and(twin.CONTENT_TYPE.eq(org.CONTENT_TYPE));
        Condition inRowDerivedSibling = CHUNKS.COLLECTION.in(
            DSL.select(sib.NAME).from(sib).join(org).on(org.TENANT_ID.eq(sib.TENANT_ID))
               .where(sib.TENANT_ID.eq(tenant)
                   .and(org.NAME.eq(origin))
                   .and(sib.LIFECYCLE_STATE.eq("quarantine"))
                   .and(sib.OWNER_ID.eq(org.OWNER_ID))
                   .and(sib.EMBEDDING_MODEL.eq(org.EMBEDDING_MODEL))
                   .and(sib.MODEL_VERSION.eq(org.MODEL_VERSION))
                   .and(sib.CONTENT_TYPE.eq(org.CONTENT_TYPE)
                       .or(sib.CONTENT_TYPE.eq(DSL.val(PREFIX).concat(org.CONTENT_TYPE))))
                   .and(DSL.notExists(DSL.selectOne().from(twin).where(anotherLiveTwin)))));
        Condition untaggedHere = ORIGIN_TAG.isNull()
            .and(CHUNKS.COLLECTION.eq(PREFIX + origin).or(inRowDerivedSibling));
        return CHUNKS.TENANT_ID.eq(tenant).and(inQuarantine).and(ORIGIN_TAG.eq(origin).or(untaggedHere));
    }

    /**
     * True when {@code name} is a registered, live ({@code superseded_by = ''}) collection of {@code tenant}.
     * A live origin keeps its quarantine rows: it is still the place a restore returns them to.
     */
    static boolean isLiveRegistered(DSLContext ctx, String tenant, String name) {
        return ctx.fetchExists(ctx.selectOne().from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)
                .and(CATALOG_COLLECTIONS.NAME.eq(name))
                .and(CATALOG_COLLECTIONS.SUPERSEDED_BY.eq(""))));
    }

    /**
     * True when any registered quarantine collection of {@code tenant} still holds a row that is {@code origin}'s
     * by the rule in the class doc. The ghost sweep's hold: an origin whose chunks all sit in quarantine has no
     * content of its own, but deleting its registry row would leave those rows with no live origin (restore
     * refuses a non-live origin, the engine's expiry skips it, and {@code nx t3 gc} refuses its name).
     */
    static boolean holdsRowsOf(DSLContext ctx, String tenant, String origin) {
        return holdsRowsOf(ctx, tenant, origin, CollectionRegistry.lifecycleState(ctx, tenant, origin));
    }

    /** {@link #holdsRowsOf(DSLContext, String, String)} for a caller that already holds the origin's row state. */
    static boolean holdsRowsOf(DSLContext ctx, String tenant, String origin, Optional<String> lifecycleState) {
        return !isQuarantine(lifecycleState, origin)
            && ctx.fetchExists(ctx.selectOne().from(CHUNKS).where(rowsOf(tenant, origin, null)));
    }

    /**
     * How many quarantine rows are {@code origin}'s: what {@link #deleteRowsOf} would remove. Read-only; the
     * re-embed plan states it before a job deletes its source (RDR-210 Cutover). A quarantine name has none.
     */
    static int countRowsOf(DSLContext ctx, String tenant, String origin) {
        return isQuarantine(ctx, tenant, origin) ? 0 : ctx.fetchCount(CHUNKS, rowsOf(tenant, origin, null));
    }

    /** The quarantine collections that hold at least one row of {@code origin}, by name. */
    private static List<String> siblingsHolding(DSLContext ctx, Condition rows) {
        return ctx.selectDistinct(CHUNKS.COLLECTION).from(CHUNKS).where(rows)
            .orderBy(CHUNKS.COLLECTION).fetch(CHUNKS.COLLECTION);
    }

    private static List<String> sorted(List<String> hex) {
        var out = new ArrayList<>(hex);
        java.util.Collections.sort(out);
        return out;
    }

    /**
     * Deletes the quarantine rows of {@code origin} and writes one {@link #OP_DELETE_ORIGIN} audit row per sibling
     * that lost rows, in the caller's transaction.
     *
     * @return sibling name to rows deleted, for the siblings that lost rows, in name order
     */
    static Map<String, Integer> deleteRowsOf(DSLContext ctx, String tenant, String origin) {
        Condition rows = rowsOf(tenant, origin, null);
        Map<String, Integer> out = new LinkedHashMap<>();
        List<String> siblings = siblingsHolding(ctx, rows);
        for (String sibling : siblings) {
            List<String> hex = sorted(ctx.deleteFrom(CHUNKS).where(rows.and(CHUNKS.COLLECTION.eq(sibling)))
                .returningResult(ChashHex.hex(CHUNKS.CHASH)).fetch(ChashHex.hex(CHUNKS.CHASH)));
            if (hex.isEmpty()) {
                continue;
            }
            Map<String, Object> details = new LinkedHashMap<>();
            details.put("origin_collection", origin);
            details.put("count", hex.size());
            CatalogRepository.insertGcAuditRow(ctx, tenant, OP_DELETE_ORIGIN, sibling, ACTOR, false, hex, details);
            out.put(sibling, hex.size());
        }
        return out;
    }

    /**
     * Retags the quarantine rows of {@code from} to {@code to} (an untagged row in one of {@code from}'s siblings
     * is tagged {@code to}) and writes one {@link #OP_RETAG} audit row per sibling that held rows. Sibling names
     * are not renamed: the engine finds siblings by this tag.
     *
     * @return rows retagged
     */
    static int retagRowsOf(DSLContext ctx, String tenant, String from, String to) {
        Condition rows = rowsOf(tenant, from, to);
        // RDR-229 P3.1 stage 2b (nexus-p2m1x.17): the tag names the origin by id as well as by name, so a reader can
        // follow a rename by id. A metadata key, not a column. The id is the registry's now, in this transaction (the
        // rename registers its target before the retag runs); a target with no registry row is tagged by name alone.
        Long toId = ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(to))
            .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
        var tag = CatalogRepository.MAPPER.createObjectNode().put("origin_collection", to);
        if (toId != null) {
            tag.put(ORIGIN_ID_KEY, toId);
        }
        JSONB incoming = JSONB.jsonb(tag.toString());
        int total = 0;
        List<String> siblings = siblingsHolding(ctx, rows);
        for (String sibling : siblings) {
            // Shallow merge, so every other key the move wrote (quarantined_at, quarantined_by, ...) survives.
            // COALESCE: jsonb_concat(NULL, x) is NULL, which would leave a NULL-metadata row untagged while
            // counting and auditing it as retagged. Only rows whose tag actually changes are returned.
            // Does not touch last_written_at: a retag is maintenance, never a client re-write.
            // The row's own id is set too (the sibling's): chunks carries a NOT VALID CHECK evaluated on every
            // updated row, which refuses a quarantine row the stamp has not reached.
            List<String> hex = sorted(ctx.update(CHUNKS)
                .set(CHUNKS.METADATA, PgVectorRepository.mergeMetadata(
                    DSL.coalesce(CHUNKS.METADATA, DSL.val(EMPTY_OBJECT)), DSL.val(incoming), null))
                .set(CHUNKS.COLLECTION_ID, CollectionIds.idOf(tenant, sibling))
                .where(rows.and(CHUNKS.COLLECTION.eq(sibling)).and(ORIGIN_TAG.isDistinctFrom(to)))
                .returningResult(ChashHex.hex(CHUNKS.CHASH)).fetch(ChashHex.hex(CHUNKS.CHASH)));
            if (hex.isEmpty()) {
                continue;
            }
            Map<String, Object> details = new LinkedHashMap<>();
            details.put("from", from);
            details.put("to", to);
            details.put("count", hex.size());
            CatalogRepository.insertGcAuditRow(ctx, tenant, OP_RETAG, sibling, ACTOR, false, hex, details);
            total += hex.size();
        }
        return total;
    }

    /**
     * Deletes every chunk of the quarantine collection {@code name} and writes one {@link #OP_DELETE_QUARANTINE}
     * audit row, in the caller's transaction. The row carries the chashes (truncated at the audit cap, the count
     * exact), so the rows a deliberate delete removed are on record.
     *
     * @return rows deleted
     */
    static int deleteQuarantineCollectionRows(DSLContext ctx, String tenant, String name) {
        List<String> hex = sorted(ctx.deleteFrom(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(name)))
            .returningResult(ChashHex.hex(CHUNKS.CHASH)).fetch(ChashHex.hex(CHUNKS.CHASH)));
        if (!hex.isEmpty()) {
            CatalogRepository.insertGcAuditRow(ctx, tenant, OP_DELETE_QUARANTINE, name, ACTOR, false, hex,
                Map.of("count", hex.size()));
        }
        return hex.size();
    }
}
