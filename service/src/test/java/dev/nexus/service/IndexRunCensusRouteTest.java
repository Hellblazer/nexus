// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.IndexRunCensusReads;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd: {@code GET /v1/catalog/index-run/census?limit=N} on a real PostgreSQL through the real route: the envelope,
 * the bounds, tenant scope, the per-bucket timeout and the six-hour edge. What each bucket computes, row by row against the
 * client's rule, is {@link IndexRunCensusParityTest}'s table; the statements' plans are {@link IndexRunCensusPlanTest}'s.
 * Every exclusion here stands beside a control row that IS counted in the same fixture, so an empty answer cannot come from
 * a fixture that holds nothing.
 */
class IndexRunCensusRouteTest extends IndexRunCensusTestBase {

    @Test
    void theAnswerCarriesTheConstantsItAppliesAndOneEntryPerBucket() throws Exception {
        var body = census("");
        assertThat(body.keySet()).containsExactlyInAnyOrder("limit", "threshold_s", "floor", "stale", "fence", "failed_hidden");
        assertThat(body).containsEntry("limit", 1000).containsEntry("threshold_s", 21_600).containsEntry("floor", "2026-08-07T16:00:35Z");
        assertThat(bucket(body, "stale").keySet()).containsExactlyInAnyOrder("count", "capped", "notes", "examples");
        assertThat(bucket(body, "fence").keySet()).containsExactlyInAnyOrder(
            "checked", "reported_null", "undated_reported_null", "floor_candidates", "anchor", "post_anchor");
        assertThat(bucket(bucket(body, "fence"), "anchor").keySet()).containsExactlyInAnyOrder(
            "kind", "at", "earliest_stamped_at", "proven_at");
        assertThat(bucket(body, "failed_hidden").keySet()).containsExactlyInAnyOrder("failed", "count", "capped", "examples");
        // an empty tenant answers zero everywhere, never a timed_out entry
        assertThat(bucket(body, "stale")).containsEntry("count", 0).containsEntry("capped", false);
        assertThat(bucket(body, "fence")).containsEntry("checked", 0).containsEntry("reported_null", 0);
        assertThat(bucket(body, "failed_hidden")).containsEntry("count", 0);
    }

    @Test
    void aBadLimitIsA400_andOnlyGetIsAllowed() throws Exception {
        for (String bad : List.of("0", "-1", "10001", "abc", "1.5")) {
            assertThat(get(TOKEN, TENANT, "/v1/catalog/index-run/census?limit=" + bad).statusCode()).as("limit=" + bad).isEqualTo(400);
        }
        assertThat(census("?limit=10000")).containsEntry("limit", 10_000);
        assertThat(post(TOKEN, TENANT, "/v1/catalog/index-run/census", "{}").statusCode()).isEqualTo(405);
    }

    // ── stale ───────────────────────────────────────────────────────────────

    private void staleDocs(int n, String prefix) throws Exception {
        for (int i = 1; i <= n; i++) insert(TENANT, Doc.of(prefix + "." + i).state("indexing").started(ago(8 * 3600)));
    }

    @Test
    void staleStopsAtTheLimit_countIsLimitWhenCapped_andCappedSaysSo() throws Exception {
        staleDocs(5, "9.1");
        insert(TENANT, Doc.of("9.2.1").state("indexing").started(ago(60)));   // control: too fresh, never counted
        assertThat(bucket(census("?limit=3"), "stale")).containsEntry("count", 3).containsEntry("capped", true);
        assertThat(bucket(census("?limit=4"), "stale")).containsEntry("count", 4).containsEntry("capped", true);
        // exactly as many as the limit: not capped; more than the limit: not capped
        assertThat(bucket(census("?limit=5"), "stale")).containsEntry("count", 5).containsEntry("capped", false);
        assertThat(bucket(census("?limit=6"), "stale")).containsEntry("count", 5).containsEntry("capped", false);
    }

    @Test
    void staleNamesAtMostTenExamples_inTumblerOrder_andCountsAllOfThem() throws Exception {
        staleDocs(12, "9.3");
        var stale = bucket(census("?limit=100"), "stale");
        assertThat(stale).containsEntry("count", 12);
        var examples = list(stale, "examples");
        assertThat(examples).hasSize(10);
        assertThat(examples).extracting(e -> (String) e.get("tumbler")).isSorted();
        assertThat(examples.get(0).keySet()).containsExactlyInAnyOrder("tumbler", "source_uri", "started_at", "age_s", "note");
        assertThat(((Number) examples.get(0).get("age_s")).longValue()).isBetween(8L * 3600, 8L * 3600 + 300);
    }

    @Test
    void anotherTenantsStaleFencesAreInvisible_andSoAreOurs() throws Exception {
        insert(TENANT, Doc.of("9.4.1").state("indexing").started(ago(8 * 3600)));
        insert(TENANT_B, Doc.of("9.4.1").state("indexing").started(ago(8 * 3600)));   // the very same tumbler
        insert(TENANT_B, Doc.of("9.4.2").state("indexing").started(ago(8 * 3600)));
        assertThat(bucket(census(""), "stale")).containsEntry("count", 1);
        var resp = get(TOKEN_B, TENANT_B, "/v1/catalog/index-run/census");
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        assertThat(bucket(mapper.readValue(resp.body(), MAP_T), "stale")).containsEntry("count", 2);
    }

    /** The six-hour edge against a clock the test holds: inclusive at exactly six hours, one second short is not stale. */
    @Test
    void theSixHourEdgeIsInclusive() throws Exception {
        Instant now = Instant.parse("2026-11-11T12:00:00Z");
        insert(TENANT, Doc.of("9.5.1").state("indexing").started(at("2026-11-11T06:00:00Z")));   // exactly 6 h
        insert(TENANT, Doc.of("9.5.2").state("indexing").started(at("2026-11-11T06:00:01Z")));   // 5 h 59 m 59 s
        var reads = new IndexRunCensusReads(repo, Clock.fixed(now, ZoneOffset.UTC));
        var stale = reads.read(TENANT, 10).stale().value();
        assertThat(stale.count()).isEqualTo(1);
        assertThat(stale.examples()).extracting(IndexRunCensusReads.StaleDoc::tumbler).containsExactly("9.5.1");
        assertThat(stale.examples().get(0).ageS()).isEqualTo(21_600L);
    }

    // ── fence ───────────────────────────────────────────────────────────────

    @Test
    void postAnchorCountsEveryUnstampedDocument_theListStopsAtTheLimit() throws Exception {
        insert(TENANT, Doc.of("9.6.1").state("complete").indexed(at("2026-08-20T10:00:00Z")).run("rA"));
        insert(TENANT, Doc.of("9.6.2").state("complete").indexed(at("2026-08-20T10:00:05Z")).run("rA"));
        for (int i = 1; i <= 5; i++) insert(TENANT, Doc.of("9.7." + i).indexed(at("2026-09-0" + i + "T00:00:00Z")));
        var post = bucket(bucket(census("?limit=3"), "fence"), "post_anchor");
        assertThat(post).containsEntry("count", 5).containsEntry("capped", true);
        assertThat(list(post, "examples")).extracting(e -> (String) e.get("tumbler")).containsExactly("9.7.1", "9.7.2", "9.7.3");
        assertThat(list(post, "examples").get(0).keySet()).containsExactlyInAnyOrder("tumbler", "source_uri", "file_path", "indexed_at");
        var exact = bucket(bucket(census("?limit=5"), "fence"), "post_anchor");
        assertThat(exact).containsEntry("count", 5).containsEntry("capped", false);
        assertThat(list(exact, "examples")).hasSize(5);
    }

    @Test
    void theFenceReadsOnlyOurTenant() throws Exception {
        insert(TENANT, Doc.of("9.8.1").state("complete").indexed(at("2026-08-20T10:00:00Z")).run("rA"));
        insert(TENANT_B, Doc.of("9.8.1").state("complete").indexed(at("2026-08-01T10:00:00Z")).run("rZ"));
        insert(TENANT_B, Doc.of("9.8.2").state("complete").indexed(at("2026-08-01T10:00:05Z")).run("rZ"));
        var fence = bucket(census(""), "fence");
        assertThat(fence).containsEntry("checked", 1);
        assertThat(bucket(fence, "anchor")).containsEntry("kind", "earliest").containsEntry("at", "2026-08-20T10:00:00Z");
    }

    // ── failed_hidden ───────────────────────────────────────────────────────

    private void hiddenFailed(String tumbler, String hash) throws Exception {
        insert(TENANT, Doc.of(tumbler).state("failed").collection(C1).chunks(2).meta("{\"content_hash\":\"" + hash + "\"}"));
        chunk(TENANT, C1, "{\"content_hash\":\"" + hash + "\"}");
        chunk(TENANT, C1, "{\"content_hash\":\"" + hash + "\"}");
    }

    @Test
    void failedStopsAtTheLimit_andThenTheHiddenCountIsAtLeast() throws Exception {
        collection(TENANT, C1);
        for (int i = 1; i <= 4; i++) hiddenFailed("9.9." + i, "hh" + i);
        var capped = bucket(census("?limit=2"), "failed_hidden");
        assertThat(bucket(capped, "failed")).containsEntry("count", 2).containsEntry("capped", true);
        assertThat(capped).containsEntry("capped", true).containsEntry("count", 2);
        var exact = bucket(census("?limit=4"), "failed_hidden");
        assertThat(bucket(exact, "failed")).containsEntry("count", 4).containsEntry("capped", false);
        assertThat(exact).containsEntry("capped", false).containsEntry("count", 4);
        assertThat(list(exact, "examples")).extracting(e -> (String) e.get("tumbler")).containsExactly("9.9.1", "9.9.2", "9.9.3", "9.9.4");
        assertThat(list(exact, "examples").get(0)).containsEntry("chunk_count", 2).containsEntry("chunks_stored", 2);
    }

    @Test
    void failedChunksOfAnotherTenantsCollectionAreNotCountedAsOurs() throws Exception {
        collection(TENANT, C1);
        collection(TENANT_B, C1);
        insert(TENANT, Doc.of("9.10.1").state("failed").collection(C1).chunks(2).meta("{\"content_hash\":\"hx\"}"));
        chunk(TENANT_B, C1, "{\"content_hash\":\"hx\"}");      // the same collection name and hash, in another tenant
        assertThat(bucket(census(""), "failed_hidden")).containsEntry("count", 0);
        chunk(TENANT, C1, "{\"content_hash\":\"hx\"}");        // control: ours
        assertThat(bucket(census(""), "failed_hidden")).containsEntry("count", 1);
    }

    // ── the bound ───────────────────────────────────────────────────────────

    /**
     * One bucket outlasting its bound is {@code timed_out}; the others still answer. Deterministic: another session holds
     * ACCESS EXCLUSIVE on the manifest table, which only the failed_hidden statement reads, so that statement waits on the
     * lock until its statement_timeout fires and the stale and fence buckets (which never touch it) answer in full.
     */
    @Test
    void aBucketThatOutlastsItsBoundIsTimedOut_andTheOthersStillAnswer() throws Exception {
        insert(TENANT, Doc.of("9.11.1").state("indexing").started(ago(8 * 3600)));
        insert(TENANT, Doc.of("9.11.2").state("failed").collection(C1).chunks(1));
        System.setProperty(CatalogRepository.FILLABLE_COUNT_TIMEOUT_PROPERTY, "300");
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            PgContainerHelper.lockTable(holder, CATALOG_DOCUMENT_CHUNKS, PgContainerHelper.LockMode.ACCESS_EXCLUSIVE);
            long t0 = System.nanoTime();
            var body = census("");
            long ms = (System.nanoTime() - t0) / 1_000_000;
            assertThat(bucket(body, "failed_hidden")).isEqualTo(Map.of("timed_out", true));
            assertThat(bucket(body, "stale")).containsEntry("count", 1);
            assertThat(bucket(body, "fence")).containsEntry("checked", 2);
            assertThat(ms).as("answered at the bound, not after the lock was released").isLessThan(5_000);
            holder.rollback();
        } finally {
            System.clearProperty(CatalogRepository.FILLABLE_COUNT_TIMEOUT_PROPERTY);
        }
        assertThat(bucket(census(""), "failed_hidden")).containsKey("count");
    }

    /** All three buckets blocked: all three are timed_out in a 200, never a 503 (a 503 is for a read the route cannot start). */
    @Test
    void everyBucketTimedOutIsStillA200WithThreeTimedOutEntries() throws Exception {
        insert(TENANT, Doc.of("9.12.1").state("indexing").started(ago(8 * 3600)));
        System.setProperty(CatalogRepository.FILLABLE_COUNT_TIMEOUT_PROPERTY, "300");
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            PgContainerHelper.lockTable(holder, CATALOG_DOCUMENTS, PgContainerHelper.LockMode.ACCESS_EXCLUSIVE);
            var body = census("");
            for (String b : List.of("stale", "fence", "failed_hidden")) assertThat(bucket(body, b)).as(b).isEqualTo(Map.of("timed_out", true));
            holder.rollback();
        } finally {
            System.clearProperty(CatalogRepository.FILLABLE_COUNT_TIMEOUT_PROPERTY);
        }
    }

    // ── read-only ───────────────────────────────────────────────────────────

    @Test
    void theRouteWritesNothing() throws Exception {
        collection(TENANT, C1);
        insert(TENANT, Doc.of("9.13.1").state("indexing").started(ago(8 * 3600)));
        hiddenFailed("9.13.2", "hw");
        insert(TENANT, Doc.of("9.13.3").indexed(at("2026-09-01T00:00:00Z")));
        String before = snapshot();
        census("");
        census("?limit=1");
        assertThat(snapshot()).isEqualTo(before);
    }

    private String snapshot() throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).select(CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.INDEX_STATE,
                    CATALOG_DOCUMENTS.INDEX_RUN_ID, CATALOG_DOCUMENTS.INDEX_STARTED_AT, CATALOG_DOCUMENTS.INDEXED_AT,
                    CATALOG_DOCUMENTS.CHUNK_COUNT, CATALOG_DOCUMENTS.DELETED_AT)
                .from(CATALOG_DOCUMENTS).orderBy(CATALOG_DOCUMENTS.TUMBLER).fetch().formatCSV()
                + DSL.using(su, SQLDialect.POSTGRES).selectCount().from(CATALOG_DOCUMENT_CHUNKS).fetchOne(0, Integer.class);
        }
    }
}
