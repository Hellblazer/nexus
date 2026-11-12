// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record2;
import org.jooq.Record4;
import org.jooq.Record6;
import org.jooq.Select;
import org.jooq.impl.DSL;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;
import java.util.function.BiFunction;
import java.util.function.LongSupplier;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;

/**
 * nexus-f3yqd: the bounded reads behind {@code GET /v1/catalog/index-run/census?limit=N}, the engine form of the two
 * {@code nx doctor} rows that read the index-run fence of every document: the stale index-run fences row
 * ({@code nexus.health._check_stale_indexing_runs}) and the failed-runs-with-hidden-chunks row
 * ({@code nexus.health._check_failed_runs_hidden_chunks}). Both read the whole catalog through the list route (22 s on a
 * laptop tenant); here each is one bounded, tenant-scoped, read-only answer, and a bucket that cannot be read in time says
 * so ({@code timed_out}) while the others still answer.
 *
 * <p>Every bucket reads the tenant's LIVE documents ({@code deleted_at IS NULL}), the population the client's walk sees.
 *
 * <h2>stale</h2> The documents stranded in {@code index_state = 'indexing'} beyond {@link #STALE_THRESHOLD_S} (the client's
 * {@code _STALE_INDEXING_THRESHOLD_HOURS}, 6 h, inclusive): {@code index_started_at <= now - 6 h}. A document with no
 * {@code index_started_at} is not counted, as the client skips it. Read from {@code idx_catalog_documents_index_state}
 * (catalog-020-2, a partial index on {@code (tenant_id, index_state) WHERE index_state <> 'complete'}); the statement stops
 * at {@code limit + 1} rows, so {@code count} is {@code limit} and {@code capped} true when more exist (which rows a capped
 * answer holds is the planner's). {@code notes} counts the stale documents of content type {@code knowledge} (the client
 * counts the distinct names of them, a name being {@code source_uri} else the tumbler; a live document's name is unique,
 * so the two agree).
 *
 * <h2>fence</h2> What the client's NULL-fence branch computes from the whole walk, from the whole tenant in SQL (no index
 * covers a NULL or a complete state, so this is the one bucket that reads every live row of the tenant, in the database,
 * bounded by the statement timeout; {@code limit} bounds only the documents listed): {@code checked} (documents carrying a
 * state), {@code reported_null} (carrying none), {@code undated_reported_null} (no {@code indexed_at}), {@code
 * floor_candidates} (no state and {@code indexed_at} after {@link #FENCE_FLOOR}, the release of full producer coverage),
 * and the fence baseline the client derives: the earliest {@code indexed_at} of a COMPLETE document ({@code earliest}) and,
 * preferred, the earliest run among COMPLETE documents that share one {@code index_run_id} with at least one other
 * ({@code proven}: only the batched begin can produce it). {@code post_anchor} is the unstamped documents indexed after both
 * the floor and the baseline, with the exact {@code count}, listed with the fields the client classifies by (tumbler, source
 * URI, file path, {@code indexed_at}). Where the client bounds its own ledger (5,000 run ids, 1,000 candidates) this is exact.
 *
 * <h2>failed_hidden</h2> {@code failed} counts the failed documents ({@code index_state = 'failed'}, from the same partial
 * index, bounded by {@code limit}); {@code count} is how many of them
 * {@code nexus.catalog.manifest_heal.heal_manifest_gaps(dry_run=True)} would rebuild, that is, those that have NO manifest
 * row, record a content hash ({@code metadata.content_hash}, else {@code index_content_hash}) and a physical collection that
 * is registered, and whose collection holds {@code n >= 1} chunk rows carrying that hash in their metadata (live or not:
 * the heal reads with {@code include_non_live}), with {@code chunk_count == 0} or {@code n <= chunk_count}. A failed
 * document that keeps manifest rows (an old, readable manifest) is not counted, and neither is one the heal leaves for a
 * re-index (more stored rows than its {@code chunk_count}). When {@code failed} is capped {@code count} is "at least": the
 * documents past the bound were not examined, so {@code capped} with a {@code count} of 0 is NOT a clean answer, it is "not
 * fully checked" (the same holds of {@code stale}: {@code capped} makes {@code count} {@code limit}, "at least"). A caller
 * that needs the whole set asks for {@code limit=10000}.
 *
 * <h2>Which rows a capped answer holds</h2> The stale and failed statements stop at {@code limit + 1} rows in the
 * planner's order, so a capped answer is a SAMPLE, not a stable prefix. An {@code ORDER BY tumbler} would make it stable
 * but cannot use the partial index's order ({@code (tenant_id, index_state)} holds equal keys in physical order), so it
 * reads every non-complete row of the state and sorts; the plan test pins that cost, and the planner's order is kept.
 * An uncapped answer is exact and is listed in tumbler order. A {@code content_hash} that is not a string is compared as its JSON text.
 */
public final class IndexRunCensusReads {

    private static final Logger log = LoggerFactory.getLogger(IndexRunCensusReads.class);

    /** The most documents one bucket lists or counts; the route answers 400 above it. Same bound as the section-label count. */
    public static final int MAX_LIMIT = CatalogRepository.FILLABLE_COUNT_MAX_LIMIT;

    /** The limit a call without one uses. */
    public static final int DEFAULT_LIMIT = 1_000;

    /** The most example documents the stale and failed_hidden buckets name (the client names ten). */
    public static final int MAX_EXAMPLES = 10;

    /** How long an {@code indexing} fence may stand before it is stale: the client's {@code _STALE_INDEXING_THRESHOLD_HOURS} (6.0). */
    public static final long STALE_THRESHOLD_S = Duration.ofHours(6).toSeconds();

    /**
     * v7.3.0 tag time: the first public release in which every producer stamps the fence; the client's
     * {@code _PRODUCER_FENCE_RELEASE_DT}. No document indexed before it is an unstamped-producer candidate.
     */
    public static final Instant FENCE_FLOOR = Instant.parse("2026-08-07T16:00:35Z");

    /**
     * The call's overall time budget in milliseconds, shared by the three buckets: each runs under the smaller of its own
     * bound ({@link CatalogRepository#FILLABLE_COUNT_TIMEOUT_MS}, 10 s) and what is left of this, under the public edge's
     * roughly 30 s. WORST CASE: the call answers within this budget plus the small cost of opening the transactions (the
     * three bounds alone would sum to 30 s); a bucket the budget never reached is {@code timed_out}. A system property
     * replaces it (the test hook), read on every call.
     */
    public static final long CALL_BUDGET_MS = 25_000;
    public static final String CALL_BUDGET_PROPERTY = "nexus.catalog.index_run_census_budget_ms";

    /** The chunk-row hashes looked up per statement. */
    private static final int HASH_BATCH = 500;

    // ── answers ──────────────────────────────────────────────────────────────

    /** A bucket's answer, or none when it could not be read in time. */
    public record Outcome<T>(T value, boolean timedOut) {
        static <T> Outcome<T> of(T value) {
            return new Outcome<>(value, false);
        }

        static <T> Outcome<T> none() {
            return new Outcome<>(null, true);
        }
    }

    public record StaleDoc(String tumbler, String sourceUri, String startedAt, long ageS, boolean note) {}

    public record Stale(int count, boolean capped, int notes, List<StaleDoc> examples) {}

    public record Unstamped(String tumbler, String sourceUri, String filePath, String indexedAt) {}

    /** {@code kind} is {@code proven}, {@code earliest} or null (no baseline); {@code at} is the baseline used. */
    public record Anchor(String kind, String at, String earliestStampedAt, String provenAt) {}

    public record PostAnchor(int count, boolean capped, List<Unstamped> examples) {}

    public record Fence(int checked, int reportedNull, int undatedReportedNull, int floorCandidates, Anchor anchor,
                        PostAnchor postAnchor) {}

    public record HiddenDoc(String tumbler, String sourceUri, int chunkCount, int chunksStored) {}

    public record FailedHidden(int failed, boolean failedCapped, int count, boolean capped, List<HiddenDoc> examples) {}

    public record Census(int limit, Outcome<Stale> stale, Outcome<Fence> fence, Outcome<FailedHidden> failedHidden) {}

    private final CatalogRepository repo;
    private final Clock clock;
    private final LongSupplier nanos;

    public IndexRunCensusReads(CatalogRepository repo) {
        this(repo, Clock.systemUTC());
    }

    /** The clock decides what "six hours ago" means; a test fixes it. */
    public IndexRunCensusReads(CatalogRepository repo, Clock clock) {
        this(repo, clock, System::nanoTime);
    }

    /** {@code nanos} is the monotonic source every time bound reads; a test replaces it to spend a budget deterministically. */
    public IndexRunCensusReads(CatalogRepository repo, Clock clock, LongSupplier nanos) {
        this.repo = repo;
        this.clock = clock;
        this.nanos = nanos;
    }

    // ── statements (public so a plan test can explain the exact statement the route runs) ──

    /** The tenant's live documents stranded in {@code indexing} since {@code cutoff} or earlier, {@code fetchLimit} at most. */
    public static Select<Record4<String, String, String, OffsetDateTime>> staleQuery(
            DSLContext ctx, String tenant, OffsetDateTime cutoff, int fetchLimit) {
        var d = CATALOG_DOCUMENTS;
        return ctx.select(d.TUMBLER, d.SOURCE_URI, d.CONTENT_TYPE, d.INDEX_STARTED_AT)
            .from(d)
            .where(d.TENANT_ID.eq(tenant)
                .and(d.DELETED_AT.isNull())
                // = 'indexing' implies the partial index's predicate; the explicit conjunct makes the index eligible
                // without relying on the planner proving it.
                .and(d.INDEX_STATE.eq("indexing"))
                .and(d.INDEX_STATE.ne("complete"))
                .and(d.INDEX_STARTED_AT.isNotNull())
                .and(d.INDEX_STARTED_AT.le(cutoff)))
            .limit(fetchLimit);
    }

    /** The tenant's live failed documents, {@code fetchLimit} at most: tumbler, source URI, chunk_count, collection, hash, has a manifest. */
    public static Select<Record6<String, String, Integer, String, String, Boolean>> failedQuery(
            DSLContext ctx, String tenant, int fetchLimit) {
        var d = CATALOG_DOCUMENTS;
        var m = CATALOG_DOCUMENT_CHUNKS;
        // the client's _content_hash: meta.content_hash, else the hash the fence recorded
        Field<String> contentHash = DSL.coalesce(
            DSL.nullif(DSL.jsonbGetAttributeAsText(d.METADATA, "content_hash"), ""),
            DSL.nullif(d.INDEX_CONTENT_HASH, ""),
            DSL.inline(""));
        Field<Boolean> hasManifest = DSL.field(DSL.exists(
            ctx.selectOne().from(m).where(m.TENANT_ID.eq(d.TENANT_ID).and(m.DOC_ID.eq(d.TUMBLER)))));
        return ctx.select(d.TUMBLER, d.SOURCE_URI, d.CHUNK_COUNT, d.PHYSICAL_COLLECTION, contentHash, hasManifest)
            .from(d)
            .where(d.TENANT_ID.eq(tenant)
                .and(d.DELETED_AT.isNull())
                .and(d.INDEX_STATE.eq("failed"))
                .and(d.INDEX_STATE.ne("complete")))
            .limit(fetchLimit);
    }

    // ── the call ─────────────────────────────────────────────────────────────

    /**
     * @param limit 1 to {@link #MAX_LIMIT}
     * @throws IllegalArgumentException when {@code limit} is outside that range
     */
    public Census read(String tenant, int limit) {
        if (limit < 1 || limit > MAX_LIMIT) {
            throw new IllegalArgumentException("limit must be 1.." + MAX_LIMIT);
        }
        Instant now = clock.instant();
        // ONE deadline for the call: each bucket runs under min(its own bound, what is left of the call budget), and a
        // bucket that starts with none left is reported timed_out without a statement.
        long callDeadline = nanos.getAsLong() + TimeUnit.MILLISECONDS.toNanos(Long.getLong(CALL_BUDGET_PROPERTY, CALL_BUDGET_MS));
        return new Census(limit,
            bucket("stale", tenant, callDeadline, (ctx, budget) -> stale(ctx, budget, tenant, limit, now)),
            bucket("fence", tenant, callDeadline, (ctx, budget) -> fence(ctx, budget, tenant, limit)),
            bucket("failed_hidden", tenant, callDeadline, (ctx, budget) -> failedHidden(ctx, budget, tenant, limit)));
    }

    private static int boundMs() {
        return Integer.getInteger(CatalogRepository.FILLABLE_COUNT_TIMEOUT_PROPERTY, CatalogRepository.FILLABLE_COUNT_TIMEOUT_MS);
    }

    /**
     * The time one bucket may spend: until {@code min(start + its bound, the call's deadline)}. Every statement of the
     * bucket sets its {@code statement_timeout} to what is left, and a bucket with nothing left throws the cancelled error
     * instead of issuing a statement (a {@code statement_timeout} of 0 means NO limit in PostgreSQL, so a spent budget must
     * never be armed).
     */
    public static final class Budget {
        private final long deadline;
        private final LongSupplier nanos;

        public Budget(long startNanos, long callDeadlineNanos, int boundMs, LongSupplier nanos) {
            this.deadline = Math.min(startNanos + TimeUnit.MILLISECONDS.toNanos(boundMs), callDeadlineNanos);
            this.nanos = nanos;
        }

        /** Milliseconds left, at least 1; throws the cancelled error (SQLSTATE 57014) when none is. */
        public long leftMs() {
            long leftMs = TimeUnit.NANOSECONDS.toMillis(deadline - nanos.getAsLong());
            if (leftMs <= 0) {
                throw new org.jooq.exception.DataAccessException("census bucket exceeded its time bound",
                    new java.sql.SQLException("time bound", CatalogRepository.QUERY_CANCELED));
            }
            return leftMs;
        }

        /** Sets the statement timeout to the time left (one read of the clock). */
        void arm(DSLContext ctx) {
            PgSession.setLocal(ctx, "statement_timeout", Long.toString(leftMs()));
        }
    }

    private static boolean cancelled(Throwable t) {
        int depth = 0;
        for (Throwable c = t; c != null && depth < 32; c = c.getCause(), depth++) {
            if (c instanceof java.sql.SQLException sql && CatalogRepository.QUERY_CANCELED.equals(sql.getSQLState())) return true;
        }
        return false;
    }

    /**
     * One bucket in its own transaction under its own bound and the call's deadline: a cancelled statement, at any point of
     * the bucket, is a {@code timed_out} entry (nothing of the bucket's partial work is kept), never the whole call.
     */
    private <T> Outcome<T> bucket(String name, String tenant, long callDeadline, BiFunction<DSLContext, Budget, T> work) {
        long start = nanos.getAsLong();
        if (callDeadline - start <= 0) {
            log.warn("event=catalog_index_run_census_bucket_unread tenant={} bucket={}", tenant, name);
            return Outcome.none();
        }
        Budget budget = new Budget(start, callDeadline, boundMs(), nanos);
        try {
            return Outcome.of(repo.tenantScope().withTenant(tenant, ctx -> work.apply(ctx, budget)));
        } catch (RuntimeException e) {
            if (!cancelled(e)) throw e;
            log.warn("event=catalog_index_run_census_bucket_timeout tenant={} bucket={}", tenant, name);
            return Outcome.none();
        }
    }

    private static String iso(OffsetDateTime ts) {
        return ts == null ? null : DateTimeFormatter.ISO_INSTANT.format(ts.toInstant());
    }

    // ── stale ────────────────────────────────────────────────────────────────

    private Stale stale(DSLContext ctx, Budget budget, String tenant, int limit, Instant now) {
        budget.arm(ctx);
        OffsetDateTime cutoff = OffsetDateTime.ofInstant(now.minusSeconds(STALE_THRESHOLD_S), ZoneOffset.UTC);
        var rows = new ArrayList<>(staleQuery(ctx, tenant, cutoff, limit + 1).fetch());
        boolean capped = rows.size() > limit;
        if (capped) rows = new ArrayList<>(rows.subList(0, limit));
        rows.sort(Comparator.comparing(r -> r.value1()));
        int notes = 0;
        List<StaleDoc> examples = new ArrayList<>();
        for (var r : rows) {
            boolean note = "knowledge".equals(r.value3());
            if (note) notes++;
            if (examples.size() < MAX_EXAMPLES) {
                long ageS = Duration.between(r.value4().toInstant(), now).toSeconds();
                examples.add(new StaleDoc(r.value1(), r.value2(), iso(r.value4()), ageS, note));
            }
        }
        return new Stale(rows.size(), capped, notes, List.copyOf(examples));
    }

    // ── fence ────────────────────────────────────────────────────────────────

    private Fence fence(DSLContext ctx, Budget budget, String tenant, int limit) {
        var d = CATALOG_DOCUMENTS;
        OffsetDateTime floor = OffsetDateTime.ofInstant(FENCE_FLOOR, ZoneOffset.UTC);
        Condition live = d.TENANT_ID.eq(tenant).and(d.DELETED_AT.isNull());
        Condition noState = d.INDEX_STATE.isNull();

        budget.arm(ctx);
        var agg = ctx.select(
                DSL.count().filterWhere(d.INDEX_STATE.isNotNull()),
                DSL.count().filterWhere(noState),
                DSL.count().filterWhere(noState.and(d.INDEXED_AT.isNull())),
                DSL.count().filterWhere(noState.and(d.INDEXED_AT.gt(floor))),
                DSL.min(d.INDEXED_AT).filterWhere(d.INDEX_STATE.eq("complete")))
            .from(d).where(live).fetchOne();
        OffsetDateTime earliest = agg.value5();

        // The earliest run, among COMPLETE documents with a usable indexed_at, that stamped two or more of them.
        budget.arm(ctx);
        var runs = ctx.select(d.INDEX_RUN_ID, DSL.min(d.INDEXED_AT).as("first"), DSL.count().as("c"))
            .from(d)
            .where(live.and(d.INDEX_STATE.eq("complete")).and(d.INDEXED_AT.isNotNull()).and(d.INDEX_RUN_ID.ne("")))
            .groupBy(d.INDEX_RUN_ID)
            .having(DSL.count().ge(2))
            .asTable("runs");
        OffsetDateTime proven = ctx.select(DSL.min(runs.field("first", OffsetDateTime.class))).from(runs)
            .fetchOne(0, OffsetDateTime.class);

        OffsetDateTime baseline = proven != null ? proven : earliest;
        Anchor anchor = new Anchor(proven != null ? "proven" : earliest != null ? "earliest" : null,
            iso(baseline), iso(earliest), iso(proven));

        PostAnchor post = new PostAnchor(0, false, List.of());
        if (baseline != null) {
            budget.arm(ctx);
            var rows = ctx.select(d.TUMBLER, d.SOURCE_URI, d.FILE_PATH, d.INDEXED_AT, DSL.count().over())
                .from(d)
                .where(live.and(noState).and(d.INDEXED_AT.gt(floor)).and(d.INDEXED_AT.gt(baseline)))
                .orderBy(d.TUMBLER)
                .limit(limit + 1)
                .fetch();
            int count = rows.isEmpty() ? 0 : rows.get(0).value5();
            List<Unstamped> listed = new ArrayList<>();
            for (int i = 0; i < Math.min(rows.size(), limit); i++) {
                var r = rows.get(i);
                listed.add(new Unstamped(r.value1(), r.value2(), r.value3(), iso(r.value4())));
            }
            post = new PostAnchor(count, count > limit, List.copyOf(listed));
        }
        return new Fence(agg.value1(), agg.value2(), agg.value3(), agg.value4(), anchor, post);
    }

    // ── failed_hidden ────────────────────────────────────────────────────────

    private FailedHidden failedHidden(DSLContext ctx, Budget budget, String tenant, int limit) {
        budget.arm(ctx);
        var rows = new ArrayList<>(failedQuery(ctx, tenant, limit + 1).fetch());
        boolean capped = rows.size() > limit;
        if (capped) rows = new ArrayList<>(rows.subList(0, limit));
        int failed = rows.size();

        // The documents the heal would try to rebuild: no manifest row, and a hash and a collection to look the chunks up by.
        Map<String, List<Candidate>> byCollection = new LinkedHashMap<>();
        for (var r : rows) {
            boolean hasManifest = Boolean.TRUE.equals(r.value6());
            String collection = r.value4() == null ? "" : r.value4();
            String hash = r.value5() == null ? "" : r.value5();
            if (hasManifest || collection.isEmpty() || hash.isEmpty()) continue;
            byCollection.computeIfAbsent(collection, k -> new ArrayList<>())
                .add(new Candidate(r.value1(), r.value2(), r.value3() == null ? 0 : r.value3(), hash));
        }
        if (byCollection.isEmpty()) return new FailedHidden(failed, capped, 0, capped, List.of());

        budget.arm(ctx);
        Map<String, String> models = new HashMap<>();
        for (var c : ctx.select(CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.EMBEDDING_MODEL).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.NAME.in(byCollection.keySet()))).fetch()) {
            models.put(c.value1(), c.value2());
        }

        List<HiddenDoc> hidden = new ArrayList<>();
        for (var entry : byCollection.entrySet()) {
            String model = models.get(entry.getKey());
            if (model == null) continue;   // not registered: the heal finds no collection and leaves the document alone
            Map<String, Integer> stored = storedByHash(ctx, budget, tenant, entry.getKey(), model,
                entry.getValue().stream().map(Candidate::hash).distinct().toList());
            for (Candidate c : entry.getValue()) {
                int n = stored.getOrDefault(c.hash(), 0);
                // heal_manifest_gaps: no stored row means nothing to rebuild from; more stored rows than chunk_count leaves
                // the document for a re-index instead of writing; the rest are rebuilt (counted in a dry run too).
                if (n >= 1 && (c.chunkCount() == 0 || n <= c.chunkCount())) {
                    hidden.add(new HiddenDoc(c.tumbler(), c.sourceUri(), c.chunkCount(), n));
                }
            }
        }
        hidden.sort(Comparator.comparing(HiddenDoc::tumbler));
        List<HiddenDoc> examples = hidden.size() <= MAX_EXAMPLES ? List.copyOf(hidden) : List.copyOf(hidden.subList(0, MAX_EXAMPLES));
        return new FailedHidden(failed, capped, hidden.size(), capped, examples);
    }

    private record Candidate(String tumbler, String sourceUri, int chunkCount, String hash) {}

    /**
     * The statement that counts the chunk rows of {@code collection} per recorded {@code content_hash}, for the given
     * hashes. The collection and its model are constants, so the planner prunes {@code nexus.chunks} to that collection's
     * leaf at plan time (IndexRunCensusPlanTest pins one leaf in the plan); a row is counted live or not (the heal's
     * read asks for non-live rows too). {@code content_hash} is a legacy metadata key with NO index: the statement reads
     * every row of the leaf once, the same work the client's {@code $in} read did server-side (measured in IndexRunCensusPlanTest), and it is the only statement of the census that scales with a collection's chunks.
     * A derived table, so the grouped expression is one column and not two bind-parameter spellings of it. Public so a plan
     * test can explain the exact statement the route runs.
     */
    public static Select<Record2<String, Integer>> storedByHashQuery(DSLContext ctx, String tenant, String collection,
                                                                    String model, List<String> hashes) {
        Field<String> hash = DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, "content_hash");
        var rows = ctx.select(hash.as("h")).from(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(collection))
                .and(CHUNKS.EMBEDDING_MODEL.eq(model)).and(hash.in(hashes)))
            .asTable("rows");
        Field<String> h = rows.field("h", String.class);
        return ctx.select(h, DSL.count()).from(rows).groupBy(h);
    }

    private static Map<String, Integer> storedByHash(DSLContext ctx, Budget budget, String tenant, String collection,
                                                      String model, List<String> hashes) {
        Map<String, Integer> out = new HashMap<>();
        for (int i = 0; i < hashes.size(); i += HASH_BATCH) {
            budget.arm(ctx);
            var batch = hashes.subList(i, Math.min(i + HASH_BATCH, hashes.size()));
            for (var r : storedByHashQuery(ctx, tenant, collection, model, batch).fetch()) {
                out.merge(r.value1(), r.value2(), Integer::sum);
            }
        }
        return out;
    }
}
