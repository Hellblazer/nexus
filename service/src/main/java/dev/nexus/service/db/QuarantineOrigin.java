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
 * move and the client's move both write), or carries no tag at all and sits in a sibling {@code X} could have
 * been moved into: {@code quarantine-X} (the reaper's name) or the name derived from {@code X}'s catalog ROW
 * ({@code quarantine-<content_type>__<owner_id>__<embedding_model>__<model_version>}, the client's name). A row
 * tagged for another origin is never {@code X}'s, whichever sibling it sits in, so two origins that share a
 * sibling each take only their own rows. The set is the one {@link PgVectorRepository#resolveQuarantineSiblings}
 * and the restore function already use, plus the untagged case they treat as "any origin that asks".
 *
 * <p>Statements are typed jOOQ, no SQL template ({@code RawSqlGateTest}). Nothing here writes a manifest table,
 * so none of it needs {@code DeadlockRetry} of its own; the two callers that matter are already inside
 * {@code manifestWriteTxn}.
 */
public final class QuarantineOrigin {

    /** The name prefix of a quarantine collection; the one predicate the engine's guards share. */
    public static final String PREFIX = "quarantine-";

    /** Origin collection deleted: its rows were taken out of one sibling. */
    public static final String OP_DELETE_ORIGIN = "collection_delete_quarantine";
    /** A quarantine collection itself was deleted. */
    public static final String OP_DELETE_QUARANTINE = "quarantine_collection_delete";
    /** Origin collection renamed: its rows in one sibling were retagged. */
    public static final String OP_RETAG = "quarantine_retag";

    /** The actor of an engine-side audit row, as the SQL-side producers write it. */
    private static final String ACTOR = "engine";

    private static final Field<String> ORIGIN_TAG = DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, "origin_collection");

    private QuarantineOrigin() {
    }

    /** True for a {@code quarantine-*} collection name. */
    public static boolean isQuarantineName(String name) {
        return name != null && name.startsWith(PREFIX);
    }

    /**
     * Refuses {@code name} when it is a quarantine collection, naming the sanctioned verbs. Used where an engine
     * path would move or delete a quarantine collection's chunks with no audit row (store-delete, rename, rehome).
     *
     * @param verb what the caller asked for, as the message names it ({@code store-delete}, {@code rename}, ...)
     * @throws IllegalArgumentException when {@code name} is a quarantine collection (a 400 on every route)
     */
    public static void requireNotQuarantine(String verb, String name) {
        if (isQuarantineName(name)) {
            throw new IllegalArgumentException(
                verb + " is refused on the quarantine collection " + name + ": it would move or delete quarantined "
                + "chunks outside the audited paths. Return chunks to their origin with `nx t3 quarantine restore`, "
                + "let `nx t3 gc` expire them (client expiry), or delete the whole quarantine collection with "
                + "collection delete, which writes a gc_audit row.");
        }
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
     * the same relation, read from the registry. An origin with no catalog row (the production dead-origin
     * shape) has only the reaper's name.
     */
    private static Condition rowsOf(String tenant, String origin) {
        var sib = CATALOG_COLLECTIONS.as("quarantine_sibling");
        var org = CATALOG_COLLECTIONS.as("quarantine_origin");
        Condition inQuarantine = CHUNKS.COLLECTION.in(
            DSL.select(CATALOG_COLLECTIONS.NAME).from(CATALOG_COLLECTIONS)
               .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)
                   .and(CATALOG_COLLECTIONS.LIFECYCLE_STATE.eq("quarantine"))));
        Condition inRowDerivedSibling = CHUNKS.COLLECTION.in(
            DSL.select(sib.NAME).from(sib).join(org).on(org.TENANT_ID.eq(sib.TENANT_ID))
               .where(sib.TENANT_ID.eq(tenant)
                   .and(org.NAME.eq(origin))
                   .and(sib.LIFECYCLE_STATE.eq("quarantine"))
                   .and(sib.OWNER_ID.eq(org.OWNER_ID))
                   .and(sib.EMBEDDING_MODEL.eq(org.EMBEDDING_MODEL))
                   .and(sib.MODEL_VERSION.eq(org.MODEL_VERSION))
                   .and(sib.CONTENT_TYPE.eq(org.CONTENT_TYPE)
                       .or(sib.CONTENT_TYPE.eq(DSL.val(PREFIX).concat(org.CONTENT_TYPE))))));
        Condition untaggedHere = ORIGIN_TAG.isNull()
            .and(CHUNKS.COLLECTION.eq(PREFIX + origin).or(inRowDerivedSibling));
        return CHUNKS.TENANT_ID.eq(tenant).and(inQuarantine).and(ORIGIN_TAG.eq(origin).or(untaggedHere));
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
        Condition rows = rowsOf(tenant, origin);
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
        Condition rows = rowsOf(tenant, from);
        JSONB incoming = JSONB.jsonb(CatalogRepository.MAPPER.createObjectNode().put("origin_collection", to).toString());
        int total = 0;
        List<String> siblings = siblingsHolding(ctx, rows);
        for (String sibling : siblings) {
            // Shallow merge, so every other key the move wrote (quarantined_at, quarantined_by, ...) survives.
            // Does not touch last_written_at: a retag is maintenance, never a client re-write.
            List<String> hex = sorted(ctx.update(CHUNKS)
                .set(CHUNKS.METADATA, PgVectorRepository.mergeMetadata(CHUNKS.METADATA, DSL.val(incoming), null))
                .where(rows.and(CHUNKS.COLLECTION.eq(sibling)))
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
