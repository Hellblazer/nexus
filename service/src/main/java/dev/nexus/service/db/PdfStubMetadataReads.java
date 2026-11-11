// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.vectors.ChunkMetadataKeys;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.Record3;
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
 * collections are examined ({@code knowledge} and {@code docs}); the client lists T3 and filters on the name prefix,
 * and the engine does not parse names (CollectionParseGateTest), so a collection whose registry type disagrees with its
 * name prefix is the one place the two can differ.
 *
 * <p><b>Naming.</b> An example is the owning document's {@code source_uri}, else its {@code file_path}, else
 * {@code "content_hash " + first 12 characters of the stub chunk's content_hash} (or {@code "?"}), as the client names
 * it. The client's last form reads {@code str()} of the value, so a non-string {@code content_hash} on a document with
 * neither URI nor path can be named differently here; the count is unaffected.
 *
 * <p><b>Bounds.</b> ONE statement for every collection: a {@code LEFT JOIN LATERAL} per collection whose inner select
 * stops at {@code limit + 1} stub rows, so a collection with a backlog costs {@code limit + 1} chunk reads and only an
 * all-clear collection reads every candidate chunk (the converged case, which cannot stop early). It runs in a
 * {@code tenantScope.withTenant} transaction (FORCE row-level security scopes every table; the tenant is also an
 * explicit equality everywhere) under the {@code statement_timeout} of {@link CatalogRepository#manifestSectionLabelFillable}
 * and its test hook; a timeout is SQLSTATE {@link CatalogRepository#QUERY_CANCELED}, the transaction ends, and no part of the
 * answer leaves this method. Which {@code limit + 1} rows a capped collection returns is the planner's; its count is
 * {@code limit} and {@code capped} is true, and its examples are drawn from those rows.
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

    /**
     * One collection's answer: {@code stubChunks} is at most {@code limit}; {@code capped} means more than {@code limit}
     * exist, so the count is a lower bound; {@code examples} are at most {@link #MAX_EXAMPLES} owning-document names.
     */
    public record CollectionStubs(String collection, int stubChunks, boolean capped, List<String> examples) {}

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

    /**
     * The statement: one row per (examined collection, stub source), {@code cnt} the stub chunks under that source in the
     * collection's first {@code fetchLimit} stub rows; a collection with no stub is one row with a NULL source and count.
     * The caller passes {@code limit + 1}. Public so a plan-shape test can explain the exact statement the route runs.
     */
    public static Select<Record3<String, String, Integer>> query(DSLContext ctx, String tenant, int fetchLimit) {
        var cols = CATALOG_COLLECTIONS.as("cols");
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
            .join(ch).on(ch.TENANT_ID.eq(m.TENANT_ID).and(ch.COLLECTION.eq(m.COLLECTION)).and(ch.CHASH.eq(m.CHASH))
                .and(ch.EMBEDDING_MODEL.eq(cols.EMBEDDING_MODEL)))
            .where(d.TENANT_ID.eq(tenant)
                .and(d.PHYSICAL_COLLECTION.eq(cols.NAME))
                .and(d.DELETED_AT.isNull())
                .and(format.eq("pdf"))
                .and(pythonFalsy(DSL.jsonbGetAttribute(d.METADATA, "extraction_method")))
                .and(m.COLLECTION.eq(cols.NAME))
                .and(ch.TENANT_ID.eq(tenant))
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
        var perCollection = DSL.lateral(ctx.select(src.as("src"), DSL.count().as("cnt")).from(stubs).groupBy(src)).as("g");

        return ctx.select(cols.NAME, perCollection.field("src", String.class), perCollection.field("cnt", Integer.class))
            .from(cols)
            .leftJoin(perCollection).on(DSL.trueCondition())
            .where(cols.TENANT_ID.eq(tenant).and(cols.CONTENT_TYPE.in(SCANNED_CONTENT_TYPES)))
            .orderBy(cols.NAME);
    }

    /**
     * @param limit 1 to {@link #MAX_LIMIT}
     * @throws IllegalArgumentException when {@code limit} is outside that range
     */
    public Page read(String tenant, int limit) {
        if (limit < 1 || limit > MAX_LIMIT) {
            throw new IllegalArgumentException("limit must be 1.." + MAX_LIMIT);
        }
        return repo.tenantScope().withTenant(tenant, ctx -> {
            PgSession.setLocal(ctx, "statement_timeout", Integer.toString(Integer.getInteger(
                CatalogRepository.FILLABLE_COUNT_TIMEOUT_PROPERTY, CatalogRepository.FILLABLE_COUNT_TIMEOUT_MS)));
            var rows = query(ctx, tenant, limit + 1).fetch();
            Map<String, Map<String, Integer>> bySource = new LinkedHashMap<>();
            for (var r : rows) {
                Map<String, Integer> sources = bySource.computeIfAbsent(r.value1(), k -> new LinkedHashMap<>());
                if (r.value2() != null) {
                    sources.merge(r.value2(), r.value3(), Integer::sum);
                }
            }
            List<CollectionStubs> out = new ArrayList<>(bySource.size());
            bySource.forEach((collection, sources) -> {
                int total = sources.values().stream().mapToInt(Integer::intValue).sum();
                List<String> examples = sources.entrySet().stream()
                    .sorted(Map.Entry.<String, Integer>comparingByValue(Comparator.reverseOrder())
                        .thenComparing(Map.Entry.comparingByKey()))
                    .limit(MAX_EXAMPLES).map(Map.Entry::getKey).toList();
                out.add(new CollectionStubs(collection, Math.min(total, limit), total > limit, examples));
            });
            return new Page(limit, List.copyOf(out));
        });
    }
}
