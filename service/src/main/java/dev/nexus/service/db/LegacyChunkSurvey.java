// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;

import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;

import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * nexus-f3yqd: what is wrong with a chunk whose chash is not 32 bytes, and whose it is. One classification, shared by the VALIDATE
 * job's refusals ({@link ChashConstraintValidator}) and the doctor's opt-in detail ({@code GET
 * /v1/schema/chash-conformance?detail=legacy_chunks}), so the two never name a chunk differently. Nothing here changes a row.
 *
 * <p><b>These rows are not expected.</b> The engine has no repair for them: boot migrations hygiene-001-5 (chunks) and
 * hygiene-001-8 (orphan manifest rows) delete every non-32-byte row, hygiene-001 first shipped in engine 0.1.91, and the NOT
 * VALID width CHECK refuses a new one. A VALIDATE refusal therefore means the CHECK was bypassed, or dropped and re-added after
 * rows were written. These reports say which rows, what they are and whose, so an operator can decide; they do not fix them.
 *
 * <p><b>Reasons</b>, by the first that holds: {@link #UNEXPECTED_WIDTH} (not 16 bytes: the legacy width is 16), {@link
 * #NO_TEXT} (no {@code chunk_text}: a reference-only chunk), {@link #LEGACY_DIGEST_KEY} (the 16 bytes are the first 16 of {@code
 * sha256(chunk_text as UTF-8)}, the RDR-108 derivation: a pre-RDR-180 key of a chunk whose text is present), else {@link
 * #TEXT_MISMATCH} (16 bytes that are not that text's digest prefix). A manifest row with no chunk row is {@link
 * #MANIFEST_WITHOUT_CHUNK}: FK-guarded, since the manifest-to-chunk foreign key is validated, so it appears only where that key
 * was dropped too.
 *
 * <p><b>Owners</b> ({@link #owners}) are the documents that hold the chunk, found the two ways the engine knows an owner: the
 * manifest rows that name the chunk ({@code via: "manifest"}), and a note, a document with no source file in the chunk's
 * collection whose {@code metadata.doc_id} stamp is the chunk's hex chash ({@code via: "note"}; the reaper's census and the
 * sweep's notes guard match a manifest-less note the same way). At most five are listed, with {@code capped} when more hold it.
 * No owner is {@code owners: []}: an answer the session could read. It needs a tenant-scoped session (row-level security hides
 * another tenant's documents), so a caller that has none, or whose lookup ran out of time, says {@code owners: null}, never an
 * empty list it did not look up.
 */
public final class LegacyChunkSurvey {

    public static final String LEGACY_DIGEST_KEY = "legacy_digest_key";
    public static final String TEXT_MISMATCH = "text_mismatch";
    public static final String NO_TEXT = "no_text";
    public static final String UNEXPECTED_WIDTH = "unexpected_width";
    public static final String MANIFEST_WITHOUT_CHUNK = "manifest_without_chunk";

    private static final int LEGACY_BYTES = 16;
    private static final int CHASH_BYTES = 32;
    private static final int DEFAULT_OWNER_CAP = 5;
    private static final String QUERY_CANCELED = "57014";
    private static final String LOCK_NOT_AVAILABLE = "55P03";

    private LegacyChunkSurvey() { }

    /** One holder of a chunk: {@code via} is {@code manifest} or {@code note}. */
    public record Owner(String docId, String title, String source, String via) {}

    /** The owners of one chunk, at most {@code cap} of them; {@code capped} says more hold it; {@code owners} null: not looked up. */
    public record Owners(List<Owner> owners, boolean capped) {
        public static Owners unknown() {
            return new Owners(null, false);
        }
    }

    /** One chunk that is not 32 bytes: its tenant, collection, model, chash (hex), reason and owners. */
    public record Bad(String tenant, String collection, String model, String chashHex, String reason, Owners owners) {}

    /** Counts per reason among a table's bad rows, the first {@code cap} of them in key order; {@code capped} only when more exist. */
    public record Counts(Map<String, Long> byReason, boolean capped) {}

    /** One document that holds a bad manifest row. */
    public record DocRef(String tenant, String docId, String title) {}

    /** Up to a cap of the documents holding bad manifest rows of one reason, in id order; {@code capped} only when more exist. */
    public record DocList(List<DocRef> docs, boolean capped) {}

    /**
     * Sets the bounds of the next statement of a request that has a deadline, and says whether any time is left. A caller asks it
     * before EACH statement, so a lookup of two statements cannot overshoot the deadline by the second.
     */
    @FunctionalInterface
    public interface Gate {
        boolean next(DSLContext ctx);
    }

    /** A gate for one request: each statement bounded by the lesser of {@code timeoutMs} and what is left of {@code deadlineNanos}. */
    public static Gate gate(long deadlineNanos, int timeoutMs) {
        return ctx -> {
            long leftMs = (deadlineNanos - System.nanoTime()) / 1_000_000L;
            if (leftMs <= 0) return false;
            PgSession.setStatementAndLockBounds(ctx, (int) Math.min(timeoutMs, leftMs), 2_000);
            return true;
        };
    }

    // ── the classification ──────────────────────────────────────────────────

    public static Field<Integer> width(Field<byte[]> chash) {
        return DSL.function("octet_length", SQLDataType.INTEGER, chash);
    }

    /** True when the old 16 bytes are the first 16 of sha256(chunk_text as UTF-8). */
    public static Condition digestMatches(Field<byte[]> chash, Field<String> text) {
        Field<byte[]> digest = DSL.function("sha256", SQLDataType.BLOB,
            DSL.function("convert_to", SQLDataType.BLOB, text, DSL.inline("UTF8")));
        Field<byte[]> firstHalf = DSL.function("substring", SQLDataType.BLOB, digest, DSL.inline(1), DSL.val(LEGACY_BYTES));
        return firstHalf.eq(chash);
    }

    /** The reason of a row whose chash is not 32 bytes; the order is the one the class comment gives. */
    public static Field<String> reasonOf(Field<byte[]> chash, Field<String> text) {
        return DSL.when(width(chash).ne(LEGACY_BYTES), DSL.val(UNEXPECTED_WIDTH))
            .when(text.isNull(), DSL.val(NO_TEXT))
            .when(digestMatches(chash, text), DSL.val(LEGACY_DIGEST_KEY))
            .otherwise(DSL.val(TEXT_MISMATCH));
    }

    /** {@code cap + 1} without wrapping at the top of the int range. */
    private static int plusOne(int cap) {
        return (int) Math.min(Integer.MAX_VALUE, cap + 1L);
    }

    private static Field<byte[]> chashOf() {
        return DSL.field(DSL.name("chash"), SQLDataType.VARBINARY);
    }

    private static Field<String> textOf() {
        return DSL.field(DSL.name("chunk_text"), SQLDataType.CLOB);
    }

    /** Counts of the first {@code cap} of {@code reasons} (read with one row more than the cap): capped only when that row is there. */
    private static Counts tally(List<String> reasons, int cap) {
        boolean capped = reasons.size() > cap;
        Map<String, Long> by = new java.util.TreeMap<>();
        for (String r : capped ? reasons.subList(0, cap) : reasons) by.merge(r, 1L, Long::sum);
        return new Counts(new LinkedHashMap<>(by), capped);
    }

    // ── a chunks relation: counts per reason, and the chunks themselves ───

    /**
     * Counts per reason among the first {@code cap} chunks of {@code rel} (a chunks table or leaf) that satisfy {@code scope}
     * and whose chash is not 32 bytes, in key order. One more row than the cap is read, so {@code capped} is exact. The caller
     * sets the statement timeout.
     */
    public static Counts counts(DSLContext ctx, Table<?> rel, Condition scope, int cap) {
        Field<byte[]> chash = chashOf();
        Field<String> reason = reasonOf(chash, textOf());
        Field<String> tenantKey = DSL.field(DSL.name("tenant_id"), SQLDataType.CLOB);
        Field<String> collectionKey = DSL.field(DSL.name("collection"), SQLDataType.CLOB);
        Field<String> modelKey = DSL.field(DSL.name("embedding_model"), SQLDataType.CLOB);
        // the first cap bad rows in key order: the same sample every call, never whichever rows the scan met first
        return tally(ctx.select(reason).from(rel).where(scope.and(width(chash).ne(CHASH_BYTES)))
            .orderBy(tenantKey, collectionKey, chash, modelKey).limit(plusOne(cap)).fetch(reason), cap);
    }

    /**
     * Up to {@code cap + 1} chunks of {@code rel} that satisfy {@code scope} and whose chash is not 32 bytes, in key order, without
     * owners (the caller resolves those through a tenant-scoped session). Every bad chunk is listed, whatever its reason, so a
     * list longer than {@code cap} is exactly a capped one.
     */
    public static List<Bad> chunks(DSLContext ctx, Table<?> rel, Condition scope, int cap) {
        Field<byte[]> chash = chashOf();
        Field<String> reason = reasonOf(chash, textOf());
        Field<String> tenant = DSL.field(DSL.name("tenant_id"), SQLDataType.CLOB);
        Field<String> collection = DSL.field(DSL.name("collection"), SQLDataType.CLOB);
        Field<String> model = DSL.field(DSL.name("embedding_model"), SQLDataType.CLOB);
        var rows = ctx.select(tenant, collection, model, chash, reason.as("reason")).from(rel)
            .where(scope.and(width(chash).ne(CHASH_BYTES)))
            .orderBy(tenant, collection, chash, model).limit(plusOne(cap));
        List<Bad> out = new ArrayList<>();
        for (Record r : rows.fetch()) {
            out.add(new Bad(r.get(tenant), r.get(collection), r.get(model), HexFormat.of().formatHex(r.get(chash)),
                r.get("reason", String.class), Owners.unknown()));
        }
        return out;
    }

    // ── a manifest: counts per reason, by the chunk each bad row names ──────

    private static Field<String> manifestReason() {
        var c = dev.nexus.service.jooq.nexus.Tables.CHUNKS;
        return DSL.when(c.CHASH.isNull(), DSL.val(MANIFEST_WITHOUT_CHUNK)).otherwise(reasonOf(c.CHASH, c.CHUNK_TEXT));
    }

    /**
     * Counts per reason among the first {@code cap} non-32-byte manifest rows of {@code tenant} (in document and position order):
     * each row by what its chunk is ({@link #MANIFEST_WITHOUT_CHUNK} when there is no chunk row). One more row than the cap is
     * read, so {@code capped} is exact. Through a tenant-scoped session.
     */
    public static Counts manifestCounts(DSLContext ctx, String tenant, int cap) {
        var m = CATALOG_DOCUMENT_CHUNKS;
        var c = dev.nexus.service.jooq.nexus.Tables.CHUNKS;
        Field<String> reason = manifestReason();
        return tally(ctx.select(reason).from(m).leftJoin(c)
            .on(c.TENANT_ID.eq(m.TENANT_ID)).and(c.COLLECTION.eq(m.COLLECTION)).and(c.EMBEDDING_MODEL.eq(m.EMBEDDING_MODEL)).and(c.CHASH.eq(m.CHASH))
            .where(m.TENANT_ID.eq(tenant)).and(width(m.CHASH).ne(CHASH_BYTES)).orderBy(m.DOC_ID, m.POSITION).limit(plusOne(cap)).fetch(reason), cap);
    }

    /**
     * The documents that hold the non-32-byte manifest rows of {@code tenant}, per reason: at most {@code cap} of each reason's
     * documents in id order (one more is read, so {@code capped} is exact), each with its title (null when the document row is
     * gone). Through a tenant-scoped session.
     */
    public static Map<String, DocList> manifestDocs(DSLContext ctx, String tenant, int cap) {
        var m = CATALOG_DOCUMENT_CHUNKS;
        var c = dev.nexus.service.jooq.nexus.Tables.CHUNKS;
        var d = CATALOG_DOCUMENTS;
        var q = ctx.selectDistinct(manifestReason().as("reason"), m.DOC_ID.as("doc_id"), d.TITLE.as("title")).from(m).leftJoin(c)
            .on(c.TENANT_ID.eq(m.TENANT_ID)).and(c.COLLECTION.eq(m.COLLECTION)).and(c.EMBEDDING_MODEL.eq(m.EMBEDDING_MODEL)).and(c.CHASH.eq(m.CHASH))
            .leftJoin(d).on(d.TENANT_ID.eq(m.TENANT_ID)).and(d.TUMBLER.eq(m.DOC_ID))
            .where(m.TENANT_ID.eq(tenant)).and(width(m.CHASH).ne(CHASH_BYTES)).asTable("q");
        Field<String> qReason = q.field("reason", String.class);
        Field<String> qDoc = q.field("doc_id", String.class);
        Field<String> qTitle = q.field("title", String.class);
        var numbered = ctx.select(qReason, qDoc, qTitle, DSL.rowNumber().over(DSL.partitionBy(qReason).orderBy(qDoc)).as("rn")).from(q).asTable("w");
        Field<Integer> rn = numbered.field("rn", Integer.class);
        Map<String, List<DocRef>> by = new java.util.TreeMap<>();
        for (Record r : ctx.select(numbered.field("reason", String.class), numbered.field("doc_id", String.class), numbered.field("title", String.class))
                .from(numbered).where(rn.le(plusOne(cap))).orderBy(numbered.field("reason", String.class), numbered.field("doc_id", String.class)).fetch()) {
            by.computeIfAbsent(r.get(0, String.class), k -> new ArrayList<>()).add(new DocRef(tenant, r.get(1, String.class), r.get(2, String.class)));
        }
        Map<String, DocList> out = new LinkedHashMap<>();
        by.forEach((k, v) -> out.put(k, v.size() > cap ? new DocList(List.copyOf(v.subList(0, cap)), true) : new DocList(List.copyOf(v), false)));
        return out;
    }

    // ── the doctor's detail ─────────────────────────────────────────────────

    /**
     * The doctor's view of one tenant. {@code partial} is true when the request deadline passed, or a statement was cut by it or
     * by a lock it could not get, before every stage ran: a stage that did not finish is NULL (unknown, never an empty answer),
     * and owners not looked up are null.
     */
    public record TenantDetail(Counts chunkCounts, Counts manifestCounts, Map<String, DocList> manifestDocs, List<Bad> chunks,
                               boolean chunksCapped, boolean partial) {}

    /** The state of one request's stages: whether any was skipped or cut. */
    private static final class Budget {
        final Gate gate;
        boolean partial;

        Budget(long deadlineNanos, int timeoutMs) {
            this.gate = gate(deadlineNanos, timeoutMs);
        }
    }

    /**
     * One stage in a transaction of its own (a statement cut by its timeout aborts only that transaction, so the stages that
     * finished keep their answer). A stage that has no time left, or is cut by it, yields {@code fallback} and marks the request
     * partial; any other failure propagates.
     */
    private static <T> T stage(TenantScope scope, String tenant, Budget b, T fallback, java.util.function.Function<DSLContext, T> body) {
        try {
            return scope.withTenant(tenant, ctx -> {
                if (!b.gate.next(ctx)) {
                    b.partial = true;
                    return fallback;
                }
                return body.apply(ctx);
            });
        } catch (RuntimeException e) {
            // a statement cut by its timeout, or a lock it could not get within its bound, is the request running out of time
            if (!hasSqlState(e, QUERY_CANCELED) && !hasSqlState(e, LOCK_NOT_AVAILABLE)) throw e;
            b.partial = true;
            return fallback;
        }
    }

    private static boolean hasSqlState(Throwable t, String state) {
        int depth = 0;
        for (Throwable c = t; c != null && depth < 32; c = c.getCause(), depth++) {
            if (c instanceof java.sql.SQLException sql && state.equals(sql.getSQLState())) return true;
        }
        return false;
    }

    /**
     * One tenant's account, through its own scoped sessions: counts per reason among its first {@code countCap} non-32-byte
     * chunks and manifest rows, the documents that hold the bad manifest rows (up to {@code listCap} per reason), and up to
     * {@code listCap} of the chunks with their owners. Scans the tenant's own chunk partition for the legacy-width rows (no
     * index serves that), so it is for a caller that asked. EVERY statement, owners' two included, is bounded by {@code timeoutMs}
     * AND by what is left of {@code deadlineMs} for the whole call; past it, or cut by it, the rest is skipped and the result says
     * {@code partial}. A failure other than a statement timeout propagates.
     */
    public static TenantDetail forTenant(TenantScope scope, String tenant, int countCap, int listCap, int timeoutMs, long deadlineMs) {
        Budget b = new Budget(System.nanoTime() + deadlineMs * 1_000_000L, timeoutMs);
        Condition mine = DSL.field(DSL.name("tenant_id"), SQLDataType.CLOB).eq(tenant);
        Table<?> rel = dev.nexus.service.jooq.nexus.Tables.CHUNKS;
        Counts chunkCounts = stage(scope, tenant, b, null, ctx -> counts(ctx, rel, mine, countCap));
        Counts manifestCounts = stage(scope, tenant, b, null, ctx -> manifestCounts(ctx, tenant, countCap));
        Map<String, DocList> docs = stage(scope, tenant, b, null, ctx -> manifestDocs(ctx, tenant, listCap));
        List<Bad> listed = stage(scope, tenant, b, null, ctx -> chunks(ctx, rel, mine, listCap));
        if (listed == null) return new TenantDetail(chunkCounts, manifestCounts, docs, null, false, true);
        boolean capped = listed.size() > listCap;
        if (capped) listed = listed.subList(0, listCap);
        List<Bad> withOwners = new ArrayList<>(listed.size());
        for (Bad bad : listed) {
            Owners o = stage(scope, tenant, b, Owners.unknown(),
                ctx -> owners(ctx, tenant, bad.collection(), bad.model(), HexFormat.of().parseHex(bad.chashHex()), DEFAULT_OWNER_CAP, b.gate));
            if (o.owners() == null) b.partial = true;   // never an empty list that was not looked up
            withOwners.add(new Bad(bad.tenant(), bad.collection(), bad.model(), bad.chashHex(), bad.reason(), o));
        }
        return new TenantDetail(chunkCounts, manifestCounts, docs, List.copyOf(withOwners), capped, b.partial);
    }

    // ── owners ──────────────────────────────────────────────────────────────

    public static Owners owners(DSLContext ctx, String tenant, String collection, String model, byte[] chash) {
        return owners(ctx, tenant, collection, model, chash, DEFAULT_OWNER_CAP, c -> true);
    }

    /**
     * The documents that hold {@code (collection, model, chash)} in {@code tenant}: those whose manifest names it, then the notes
     * stamped with its hex. Through a tenant-scoped session; at most {@code cap}, with {@code capped} when more exist. {@code gate}
     * is asked before each of the (up to) two statements; a refusal makes the whole answer {@link Owners#unknown()}.
     */
    public static Owners owners(DSLContext ctx, String tenant, String collection, String model, byte[] chash, int cap, Gate gate) {
        var m = CATALOG_DOCUMENT_CHUNKS;
        var d = CATALOG_DOCUMENTS;
        List<Owner> out = new ArrayList<>();
        if (!gate.next(ctx)) return Owners.unknown();
        for (Record r : ctx.selectDistinct(d.TUMBLER, d.TITLE, d.FILE_PATH, d.SOURCE_URI).from(m).join(d)
                .on(d.TENANT_ID.eq(m.TENANT_ID)).and(d.TUMBLER.eq(m.DOC_ID))
                .where(m.TENANT_ID.eq(tenant)).and(m.COLLECTION.eq(collection)).and(m.EMBEDDING_MODEL.eq(model)).and(m.CHASH.eq(chash))
                .orderBy(d.TUMBLER).limit(plusOne(cap)).fetch()) {
            out.add(new Owner(r.get(d.TUMBLER), r.get(d.TITLE), source(r.get(d.SOURCE_URI), r.get(d.FILE_PATH)), "manifest"));
        }
        if (out.size() <= cap) {
            if (!gate.next(ctx)) return Owners.unknown();
            Field<String> stamp = DSL.jsonbGetAttributeAsText(d.METADATA, "doc_id");
            String hex = HexFormat.of().formatHex(chash);
            for (Record r : ctx.select(d.TUMBLER, d.TITLE, d.FILE_PATH, d.SOURCE_URI).from(d)
                    .where(d.TENANT_ID.eq(tenant)).and(d.PHYSICAL_COLLECTION.eq(collection)).and(d.DELETED_AT.isNull())
                    .and(d.FILE_PATH.eq("")).and(stamp.eq(hex)).orderBy(d.TUMBLER).limit(plusOne(cap)).fetch()) {
                if (out.stream().noneMatch(o -> o.docId().equals(r.get(d.TUMBLER)))) {
                    out.add(new Owner(r.get(d.TUMBLER), r.get(d.TITLE), source(r.get(d.SOURCE_URI), r.get(d.FILE_PATH)), "note"));
                }
            }
        }
        boolean capped = out.size() > cap;
        return new Owners(List.copyOf(capped ? out.subList(0, cap) : out), capped);
    }

    private static String source(String sourceUri, String filePath) {
        if (sourceUri != null && !sourceUri.isBlank()) return sourceUri;
        return filePath == null ? "" : filePath;
    }

    /** The JSON shape of an owner list, for the two reports that carry it. */
    public static List<Map<String, Object>> ownersJson(Owners o) {
        if (o == null || o.owners() == null) return null;
        List<Map<String, Object>> out = new ArrayList<>();
        for (Owner w : o.owners()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("doc_id", w.docId());
            m.put("title", w.title());
            m.put("source", w.source());
            m.put("via", w.via());
            out.add(m);
        }
        return out;
    }

    /** The JSON shape of the documents holding bad manifest rows, per reason. */
    public static Map<String, Object> docsJson(Map<String, DocList> docs) {
        Map<String, Object> out = new LinkedHashMap<>();
        for (var e : docs.entrySet()) {
            List<Map<String, Object>> list = new ArrayList<>();
            for (DocRef r : e.getValue().docs()) {
                Map<String, Object> m = new LinkedHashMap<>();
                m.put("tenant", r.tenant());
                m.put("doc_id", r.docId());
                m.put("title", r.title());
                list.add(m);
            }
            Map<String, Object> entry = new LinkedHashMap<>();
            entry.put("docs", list);
            entry.put("capped", e.getValue().capped());
            out.put(e.getKey(), entry);
        }
        return out;
    }
}
