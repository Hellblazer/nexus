// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.vectors.ChunkMetadataKeys;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.Record2;
import org.jooq.Select;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeSet;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;

/**
 * nexus-f3yqd: the bounded read behind {@code GET /v1/catalog/stub-metadata?limit=N}, the engine form of the
 * {@code nx doctor} "PDF chunk metadata" row ({@code nexus.health._pdf_stubs_in_collection}). Per knowledge and docs
 * collection of the tenant it counts the placeholder chunks of PDF documents, up to {@code limit}, and names a few of the
 * documents that own them.
 *
 * <p><b>The rule is the client's.</b> A chunk is a stub when ALL of these hold; the Python function each leg mirrors is
 * named, and {@code service/src/test/resources/pdf-stub-metadata-cases.json} holds both halves to one table:
 * <ul>
 *   <li>the chunk ROW's metadata carries any key of {@code ChunkMetadataKeys.DOCUMENT}, {@code POSITION} or
 *       {@code LEGACY} ({@code non_chunk_row_keys()}). Key presence, so a key holding JSON null counts, and {@code title}
 *       is itself a DOCUMENT key, so a row whose only key is an empty title is a stub;</li>
 *   <li>the row holds no truthy {@code title} ({@code not meta.get("title")}): Python falsiness, so absent, null,
 *       {@code false}, {@code 0}, {@code ""}, {@code []} and {@code {}} are all "no title" and a blank string is a title
 *       ({@link #pythonFalsy});</li>
 *   <li>the OWNING document is live, sits in the collection ({@code physical_collection}), is a PDF by
 *       {@code nexus.catalog_source_format(content_type, file_path)} (the function the client's
 *       {@code catalog_source_format} mirrors: extension first, then the catalog type) and its catalog metadata records
 *       no truthy {@code extraction_method} ({@code not (d.meta or {}).get("extraction_method")}). The chunk row's own
 *       method is never read (it is the last writer's);</li>
 *   <li>the chunk is a row of that document's manifest at that collection, and the chunk row exists at the
 *       collection's embedding model (a manifest chunk with no T3 row is skipped).</li>
 * </ul>
 * A chunk the manifest lists at two positions of one document counts once (the client keeps one chash per document);
 * a chunk two documents hold counts under each. {@code content_type} of the collection REGISTRY decides which
 * collections are examined ({@code knowledge} and {@code docs}); the client lists T3 and filters on the name prefix, and the
 * engine does not parse names (CollectionParseGateTest). The two agree for a prefixed name because the client registers a
 * collection with the first segment of its name as the content type ({@code corpus.collection_registration_kwargs}; pinned
 * by {@code tests/catalog/test_pdf_stub_metadata_cases.py}). They differ only in the engine's favour: a bare name with no
 * {@code __} registers as {@code knowledge} though the client's prefix scan skips it, so the engine can examine a
 * collection the client does not, never the reverse.
 *
 * <p><b>Naming.</b> An example is the owning document's {@code source_uri}, else its {@code file_path}, else
 * {@code "content_hash " + first 12 characters of the stub chunk's content_hash} (or {@code "?"}), as the client names
 * it. The client's last form reads {@code str()} of the value, so a non-string {@code content_hash} on a document with
 * neither URI nor path can be named differently here; the count is unaffected.
 *
 * <p><b>Bounds, one statement per collection.</b> A first statement lists the tenant's examined collections (the registry,
 * tiny). Then each collection is read by a statement of its own, in its own transaction, whose inner select stops at
 * {@code limit + 1} stub rows: a collection with a backlog costs {@code limit + 1} chunk reads and only an all-clear
 * collection reads every candidate chunk (the converged case, which cannot stop early). Every statement runs in a
 * {@code tenantScope.withTenant} transaction (FORCE row-level security scopes each table; the tenant is also an explicit
 * equality) under a {@code statement_timeout}. The bound of a collection is the smaller of {@link #collectionBoundMs} (the
 * {@link CatalogRepository#FILLABLE_COUNT_TIMEOUT_MS} of the fillable counts, 10 s, with its test hook) and what is left of
 * the call's overall budget ({@link #BUDGET_MS}, 25 s, under the public edge's roughly 30 s, with its own test hook), so one
 * slow collection cannot blank the others and the whole call cannot outlast the edge. A collection whose statement is
 * cancelled (SQLSTATE {@link CatalogRepository#QUERY_CANCELED}), or that the exhausted budget never reached, is reported
 * {@code timed_out} with NO count: it is "cannot say", never zero, and the other collections answer. A failure of the
 * collection list itself is the only whole-call failure (the route answers 503). The chunk read names the collection's
 * embedding model as a constant, so the planner prunes {@code nexus.chunks} to that model's partitions at plan time (the
 * model is the registry's, as the client's by-collection read uses it; the registry's composite foreign key gives a
 * collection exactly one model, so no chunk of the collection sits in another). Which {@code limit + 1} rows a capped
 * collection returns is the planner's; its count is {@code limit}, {@code capped} is true, and its examples are drawn from
 * those rows.
 */
public final class PdfStubMetadataReads {

    /** The most stub chunks one collection reports; the route answers 400 above it. Same bound as the section-label count. */
    public static final int MAX_LIMIT = CatalogRepository.FILLABLE_COUNT_MAX_LIMIT;

    /** The limit a call without one uses. */
    public static final int DEFAULT_LIMIT = 1_000;

    /** The most example sources one collection names. */
    public static final int MAX_EXAMPLES = 10;

    /** The registry content types whose collections are examined (the client scans the knowledge and docs prefixes). */
    static final List<String> SCANNED_CONTENT_TYPES = List.of("knowledge", "docs");

    /** The call's overall time budget in milliseconds; a system property replaces it (the test hook), read on every call. */
    public static final long BUDGET_MS = 25_000;
    public static final String BUDGET_PROPERTY = "nexus.catalog.stub_metadata_budget_ms";

    /**
     * One collection's answer: {@code stubChunks} is at most {@code limit}; {@code capped} means MORE than {@code limit}
     * exist, so the count is "at least" that; {@code examples} are at most {@link #MAX_EXAMPLES} owning-document names.
     * {@code timedOut} means the collection could not be read in time: it carries no count ({@code stubChunks} 0 and
     * {@code capped} false are meaningless then) and no example.
     */
    public record CollectionStubs(String collection, int stubChunks, boolean capped, List<String> examples,
                                  boolean timedOut) {
        static CollectionStubs timedOut(String collection) {
            return new CollectionStubs(collection, 0, false, List.of(), true);
        }
    }

    /** An examined collection and the embedding model the registry gives it. */
    record CollectionRef(String name, String embeddingModel) {}

    /** The answer: every examined collection, in name order, stub-free ones included (they were examined). */
    public record Page(int limit, List<CollectionStubs> collections) {}

    /** The JSON values Python reads as false: what {@code not value} is true for besides an absent key. */
    private static final List<JSONB> PYTHON_FALSY = List.of(
        JSONB.valueOf("null"), JSONB.valueOf("false"), JSONB.valueOf("0"), JSONB.valueOf("\"\""),
        JSONB.valueOf("[]"), JSONB.valueOf("{}"));

    /** The keys a reader must not take from a shared chunk row: DOCUMENT, POSITION and LEGACY, sorted. */
    private static final List<String> NON_CHUNK_ROW_KEYS = List.copyOf(new TreeSet<>(java.util.stream.Stream.of(
        ChunkMetadataKeys.DOCUMENT, ChunkMetadataKeys.POSITION, ChunkMetadataKeys.LEGACY)
        .flatMap(java.util.Set::stream).toList()));

    private final CatalogRepository repo;

    public PdfStubMetadataReads(CatalogRepository repo) {
        this.repo = repo;
    }

    /**
     * {@code not value} in Python, over a jsonb value read with {@code ->}: SQL NULL (key absent) or JSON null, false, a
     * zero number, an empty string, array or object. jsonb equality compares numbers by value, so {@code 0.0} and
     * {@code -0} match {@code 0}.
     */
    static Condition pythonFalsy(Field<JSONB> value) {
        return value.isNull().or(value.in(PYTHON_FALSY));
    }

    /** The chunk row carries at least one key of DOCUMENT, POSITION or LEGACY (key presence, whatever the value). */
    static Condition carriesNonChunkRowKey(Field<JSONB> metadata) {
        Condition any = DSL.falseCondition();
        for (String key : NON_CHUNK_ROW_KEYS) {
            // "metadata -> key IS NOT NULL" is key presence: a key holding JSON null reads as the jsonb value null, not SQL
            // NULL (the idiom of ManifestFillablePredicates.hasLabelKey; the ? operator does not survive inlined rendering).
            any = any.or(DSL.jsonbGetAttribute(metadata, key).isNotNull());
        }
        return any;
    }

    /** The tenant's examined collections: knowledge and docs by registry content type, in name order. Public for the plan and scope tests. */
    public static Select<Record2<String, String>> collectionsQuery(DSLContext ctx, String tenant) {
        var cols = CATALOG_COLLECTIONS;
        return ctx.select(cols.NAME, cols.EMBEDDING_MODEL).from(cols)
            .where(cols.TENANT_ID.eq(tenant).and(cols.CONTENT_TYPE.in(SCANNED_CONTENT_TYPES)))
            .orderBy(cols.NAME);
    }

    /**
     * The statement of ONE collection: one row per stub source, {@code cnt} the stub chunks under that source in the first
     * {@code fetchLimit} stub rows (none when the collection has no stub). The caller passes {@code limit + 1}.
     * {@code model} is the collection's registry model, a constant so the chunk partitions prune at plan time. Public so a
     * plan-shape test can explain the exact statement the route runs.
     */
    public static Select<Record2<String, Integer>> query(DSLContext ctx, String tenant, String collection, String model,
                                                         int fetchLimit) {
        var d = CATALOG_DOCUMENTS.as("d");
        var m = CATALOG_DOCUMENT_CHUNKS.as("m");
        var earlier = CATALOG_DOCUMENT_CHUNKS.as("earlier");
        var ch = CHUNKS.as("ch");

        Field<String> contentHash12 = DSL.nullif(DSL.left(DSL.jsonbGetAttributeAsText(ch.METADATA, "content_hash"), 12), "");
        Field<String> source = DSL.coalesce(DSL.nullif(d.SOURCE_URI, ""), DSL.nullif(d.FILE_PATH, ""),
            DSL.concat(DSL.inline("content_hash "), DSL.coalesce(contentHash12, DSL.inline("?"))));
        Field<String> format = DSL.function(DSL.name("nexus", "catalog_source_format"), SQLDataType.VARCHAR,
            d.CONTENT_TYPE, d.FILE_PATH);

        Table<?> stubs = ctx.select(source.as("src"))
            .from(d)
            .join(m).on(m.TENANT_ID.eq(d.TENANT_ID).and(m.DOC_ID.eq(d.TUMBLER)))
            .join(ch).on(ch.TENANT_ID.eq(m.TENANT_ID).and(ch.COLLECTION.eq(m.COLLECTION)).and(ch.CHASH.eq(m.CHASH)))
            .where(d.TENANT_ID.eq(tenant)
                .and(d.PHYSICAL_COLLECTION.eq(collection))
                .and(d.DELETED_AT.isNull())
                .and(format.eq("pdf"))
                .and(pythonFalsy(DSL.jsonbGetAttribute(d.METADATA, "extraction_method")))
                .and(m.COLLECTION.eq(collection))
                .and(ch.TENANT_ID.eq(tenant))
                .and(ch.EMBEDDING_MODEL.eq(model))
                .and(carriesNonChunkRowKey(ch.METADATA))
                .and(pythonFalsy(DSL.jsonbGetAttribute(ch.METADATA, "title")))
                // one chash per document: only the first position of a chunk a manifest lists twice
                .and(DSL.notExists(ctx.selectOne().from(earlier)
                    .where(earlier.TENANT_ID.eq(m.TENANT_ID).and(earlier.DOC_ID.eq(m.DOC_ID))
                        .and(earlier.COLLECTION.eq(m.COLLECTION)).and(earlier.CHASH.eq(m.CHASH))
                        .and(earlier.POSITION.lt(m.POSITION)))))
            )
            .limit(fetchLimit)
            .asTable("s");
        Field<String> src = stubs.field("src", String.class);
        return ctx.select(src, DSL.count()).from(stubs).groupBy(src);
    }

    /** The statement bound of one collection read, in milliseconds: the fillable counts' bound unless its test hook replaces it. */
    static int collectionBoundMs() {
        return Integer.getInteger(CatalogRepository.FILLABLE_COUNT_TIMEOUT_PROPERTY, CatalogRepository.FILLABLE_COUNT_TIMEOUT_MS);
    }

    /** True when {@code t} or a cause (depth-bounded) is a SQLException cancelled by its statement_timeout. */
    private static boolean cancelled(Throwable t) {
        int depth = 0;
        for (Throwable c = t; c != null && depth < 32; c = c.getCause(), depth++) {
            if (c instanceof java.sql.SQLException sql && CatalogRepository.QUERY_CANCELED.equals(sql.getSQLState())) return true;
        }
        return false;
    }

    /**
     * @param limit 1 to {@link #MAX_LIMIT}
     * @throws IllegalArgumentException when {@code limit} is outside that range
     * @throws RuntimeException when the collection LIST cannot be read (a statement timeout there has SQLSTATE
     *                          {@link CatalogRepository#QUERY_CANCELED} in its cause chain); a collection's own timeout is
     *                          an entry, not an exception
     */
    public Page read(String tenant, int limit) {
        if (limit < 1 || limit > MAX_LIMIT) {
            throw new IllegalArgumentException("limit must be 1.." + MAX_LIMIT);
        }
        long deadline = System.nanoTime() + java.util.concurrent.TimeUnit.MILLISECONDS.toNanos(
            Long.getLong(BUDGET_PROPERTY, BUDGET_MS));
        List<CollectionRef> refs = repo.tenantScope().withTenant(tenant, ctx -> {
            PgSession.setLocal(ctx, "statement_timeout", Integer.toString(collectionBoundMs()));
            List<CollectionRef> out = new ArrayList<>();
            for (var r : collectionsQuery(ctx, tenant).fetch()) out.add(new CollectionRef(r.value1(), r.value2()));
            return out;
        });
        List<CollectionStubs> out = new ArrayList<>(refs.size());
        for (CollectionRef ref : refs) {
            long leftMs = java.util.concurrent.TimeUnit.NANOSECONDS.toMillis(deadline - System.nanoTime());
            if (leftMs <= 0) {
                out.add(CollectionStubs.timedOut(ref.name()));
                continue;
            }
            int boundMs = (int) Math.min(collectionBoundMs(), leftMs);
            try {
                out.add(readOne(tenant, ref, limit, boundMs));
            } catch (RuntimeException e) {
                if (!cancelled(e)) throw e;
                out.add(CollectionStubs.timedOut(ref.name()));
            }
        }
        return new Page(limit, List.copyOf(out));
    }

    private CollectionStubs readOne(String tenant, CollectionRef ref, int limit, int boundMs) {
        return repo.tenantScope().withTenant(tenant, ctx -> {
            PgSession.setLocal(ctx, "statement_timeout", Integer.toString(boundMs));
            Map<String, Integer> sources = new LinkedHashMap<>();
            for (var r : query(ctx, tenant, ref.name(), ref.embeddingModel(), limit + 1).fetch()) {
                sources.merge(r.value1(), r.value2(), Integer::sum);
            }
            int total = sources.values().stream().mapToInt(Integer::intValue).sum();
            List<String> examples = sources.entrySet().stream()
                .sorted(Map.Entry.<String, Integer>comparingByValue(Comparator.reverseOrder())
                    .thenComparing(Map.Entry.comparingByKey()))
                .limit(MAX_EXAMPLES).map(Map.Entry::getKey).toList();
            return new CollectionStubs(ref.name(), Math.min(total, limit), total > limit, examples, false);
        });
    }
}
