// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.jooq.nexus.tables.records.MemoryRecord;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record;
import org.jooq.Result;
import org.jooq.SelectFieldOrAsterisk;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.MEMORY;
import static org.jooq.impl.DSL.condition;
import static org.jooq.impl.DSL.count;
import static org.jooq.impl.DSL.field;
import static org.jooq.impl.DSL.name;
import static org.jooq.impl.DSL.select;
import static org.jooq.impl.DSL.trueCondition;
import static org.jooq.impl.DSL.val;

/**
 * RDR-209 Phase 0b (nexus-1gdv0.1.1, gate finding S3 as nexus-1gdv0.1.2): one page of
 * {@link MemoryRepository#search}, stamping only the rows it returns, with the total that matched.
 *
 * <p><b>How the total is counted.</b> One SQL statement. A CTE {@code matches} holds every match with its
 * rank; {@code pg} is {@code matches} ordered by rank then id and cut to {@code LIMIT/OFFSET}; the select list
 * starts from a one-row {@code count(*)} over {@code matches} and LEFT JOINs the page, so a page past the end
 * still yields one row carrying the total (a window {@code count(*) over ()} rides on returned rows and would
 * lose it there). The result order is the page order, rank descending then id ascending, the same as the
 * unpaged search.
 *
 * <p><b>What that guarantees.</b> The page and the total come from one statement, and one statement reads one
 * snapshot even at READ COMMITTED (the {@link TenantScope#withTenant} default, where each statement takes its
 * own snapshot), so a concurrent insert, delete, update or quarantine cannot make them disagree. A second
 * {@code count} statement in the same transaction would not give that: at READ COMMITTED it sees a later
 * snapshot. Row-level security scopes both sides, so another tenant's rows are neither returned nor counted.
 * The stamp that follows is a separate statement in the same transaction and touches only the returned ids; a
 * row deleted between the two simply loses its stamp, as it does on the unpaged path.
 *
 * <p>The degenerate-query guard runs only when the total is 0, never for an empty page past the end.
 */
public final class MemorySearchPage {

    /** One page of full-text matches and the number of rows that matched in total. */
    public record Page(List<MemoryRecord> rows, int matchingTotal) {}

    private MemorySearchPage() {}

    /**
     * @param limit  rows in the page, at least 1 (the HTTP layer clamps it to its ceiling)
     * @param offset rows to skip, at least 0
     */
    static Page run(DSLContext ctx, String query, String project, boolean trackAccess, int limit, int offset) {
        if (limit < 1) {
            throw new IllegalArgumentException("limit must be a positive integer, got " + limit);
        }
        if (offset < 0) {
            throw new IllegalArgumentException("offset must be a non-negative integer, got " + offset);
        }
        String tsq = MemoryRepository.ftsQuery(query);
        Condition where = condition("fts_vector @@ " + tsq, val(query)).and(MemoryRepository.NOT_QUARANTINED);
        if (project != null && !project.isBlank()) {
            where = where.and(MEMORY.PROJECT.eq(project));
        }
        Field<Double> rank = field("ts_rank(fts_vector, " + tsq + ")", Double.class, val(query));

        var matches = name("matches").as(select(MEMORY.ID, rank.as("rank")).from(MEMORY).where(where));
        Field<Long> mId = field(name("matches", "id"), Long.class);
        Field<Double> mRank = field(name("matches", "rank"), Double.class);
        var pg = name("pg").as(select(mId.as("id"), mRank.as("rank")).from(matches)
            .orderBy(mRank.desc(), mId.asc()).limit(limit).offset(offset));
        Field<Long> pId = field(name("pg", "id"), Long.class);
        Field<Double> pRank = field(name("pg", "rank"), Double.class);
        var total = select(count().as("n")).from(matches).asTable("t");
        Field<Integer> n = field(name("t", "n"), Integer.class);

        List<SelectFieldOrAsterisk> columns = new ArrayList<>();
        columns.add(n);
        columns.addAll(Arrays.asList(MEMORY.fields()));
        Result<Record> result = ctx.with(matches, pg)
            .select(columns)
            .from(total)
            .leftJoin(pg).on(trueCondition())
            .leftJoin(MEMORY).on(MEMORY.ID.eq(pId))
            .orderBy(pRank.desc().nullsLast(), pId.asc())
            .fetch();

        int matchingTotal = result.get(0).get(n);
        List<MemoryRecord> rows = new ArrayList<>();
        for (Record r : result) {
            if (r.get(MEMORY.ID) != null) {
                rows.add(r.into(MEMORY));
            }
        }
        if (matchingTotal == 0) {
            MemoryRepository.guardAgainstDegenerateQuery(ctx, query);
        }
        if (trackAccess && !rows.isEmpty()) {
            MemoryRepository.batchTrackAccess(ctx, rows.stream().map(MemoryRecord::getId).toList());
            OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
            for (MemoryRecord r : rows) {
                r.setAccessCount(r.getAccessCount() + 1);
                r.setLastAccessed(now);
            }
        }
        return new Page(rows, matchingTotal);
    }
}
