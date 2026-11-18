// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.MemoryRepository;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.http.MemoryHandler;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.math.BigInteger;
import java.net.http.HttpClient;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.MEMORY;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-209 Phase 0b, beads nexus-1gdv0.1.1 and nexus-1gdv0.1.2: {@code POST /v1/memory/search} takes a page,
 * stamps only the rows it returns, and reports the total even for an empty page.
 *
 * <p>Repository tests call {@link MemoryRepository#searchPage} directly; wire tests drive the real HTTP
 * service. Both run on one real Postgres with row-level security, so another tenant's rows are neither
 * stamped nor counted.
 *
 * <p>Seeded rows share one content string, so they share one {@code ts_rank} and come back in id order (the
 * deterministic tiebreak). That makes "the first five" a well-defined set the counter checks can name.
 *
 * <p>Concurrency: the total and the page come from ONE SQL statement (see {@code MemorySearchPage}), and one
 * statement runs on one snapshot even at READ COMMITTED, so a concurrent insert, delete or quarantine cannot
 * make them disagree. No test here races a writer against the read (that would need a hook between two
 * statements, and there is only one). What IS pinned is the statement count: {@code MemorySearchPageStatementsTest}
 * records the SQL a page runs and fails if the total moves to a second statement. Order, the quarantine
 * predicate on the total, and the guard's placement past the end are pinned here.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class MemorySearchPageTest {

    private static final String TOKEN = "memory-search-page-token-a1";
    private static final String OTHER_TOKEN = "memory-search-page-token-b2";
    private static final String SVC_ROLE = "svc_search_page_test";
    private static final String SVC_PASS = "svc_search_page_test_pass";
    private static final String TENANT = TenantConstants.DEFAULT_TENANT;
    private static final String OTHER_TENANT = "search-page-other-tenant";

    private static final int N = 30;

    PostgreSQLContainer<?> pg;
    NexusService service;
    HttpClient http;
    com.zaxxer.hikari.HikariDataSource svcDs;
    TenantScope scope;
    MemoryRepository repo;
    final ObjectMapper mapper = new ObjectMapper();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), TOKEN, TENANT, "bound");
            PgContainerHelper.seedServiceToken(
                DSL.using(su, SQLDialect.POSTGRES), OTHER_TOKEN, OTHER_TENANT, "bound-other");
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        scope = new TenantScope(svcDs);
        repo = new MemoryRepository(scope);
        service = new NexusService(0, TOKEN, svcDs);
        service.start();
        http = TestHttp.client();
    }

    @AfterAll
    void stopAll() {
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── fixtures ──────────────────────────────────────────────────────────────

    /** Seeds {@code n} rows with identical content; returns their ids ascending (the page order). */
    private List<Long> seed(String tenant, String project, String term, int n) {
        List<Long> ids = new ArrayList<>();
        for (int i = 0; i < n; i++) {
            ids.add(repo.upsert(tenant, project, String.format("row-%02d", i),
                term + " shared body", "t", null, null, 30));
        }
        return ids.stream().sorted().toList();
    }

    /** Reads the stored counter without stamping it ({@code findById} itself stamps). */
    private int accessCount(String tenant, long id) {
        return scope.withTenant(tenant, ctx ->
            ctx.select(MEMORY.ACCESS_COUNT).from(MEMORY).where(MEMORY.ID.eq(id)).fetchOne(MEMORY.ACCESS_COUNT));
    }

    private List<Long> withCount(String tenant, List<Long> ids, int count) {
        return ids.stream().filter(id -> accessCount(tenant, id) == count).toList();
    }

    private static String unique(String stem) {
        return stem + System.nanoTime();
    }

    // ── repository: the page ──────────────────────────────────────────────────

    @Test
    void page_stampsOnlyItsRows_andReportsTheTotal() {
        String proj = unique("sp-proj-");
        List<Long> ids = seed(TENANT, proj, "zebrapage", N);

        var first = repo.searchPage(TENANT, "zebrapage", proj, true, 5, 0);
        assertThat(first.rows()).extracting(r -> r.getId()).containsExactlyElementsOf(ids.subList(0, 5));
        assertThat(first.matchingTotal()).isEqualTo(N);
        assertThat(first.rows()).allSatisfy(r -> assertThat(r.getAccessCount()).isEqualTo(1));
        assertThat(withCount(TENANT, ids, 1))
            .as("exactly the five returned rows were stamped").containsExactlyElementsOf(ids.subList(0, 5));
        assertThat(withCount(TENANT, ids, 0))
            .as("the 25 matches outside the page keep access_count 0").hasSize(N - 5);

        var second = repo.searchPage(TENANT, "zebrapage", proj, true, 5, 5);
        assertThat(second.rows()).extracting(r -> r.getId()).containsExactlyElementsOf(ids.subList(5, 10));
        assertThat(second.matchingTotal()).isEqualTo(N);
        assertThat(withCount(TENANT, ids, 1)).containsExactlyElementsOf(ids.subList(0, 10));
        assertThat(withCount(TENANT, ids, 0)).hasSize(N - 10);
    }

    @Test
    void page_withAccessSilent_stampsNothing() {
        String proj = unique("sp-silent-");
        List<Long> ids = seed(TENANT, proj, "quokkasilent", N);

        var page = repo.searchPage(TENANT, "quokkasilent", proj, false, 5, 0);

        assertThat(page.rows()).hasSize(5);
        assertThat(page.matchingTotal()).isEqualTo(N);
        assertThat(withCount(TENANT, ids, 0)).hasSize(N);
    }

    @Test
    void noLimit_search_stillReturnsEveryMatch_andStampsEveryMatch() {
        // Back-compat pin: the unpaged repository path an old client reaches is untouched.
        String proj = unique("sp-nolimit-");
        List<Long> ids = seed(TENANT, proj, "wombatunpaged", N);

        var rows = repo.search(TENANT, "wombatunpaged", proj, true);

        assertThat(rows).extracting(r -> r.getId()).containsExactlyElementsOf(ids);
        assertThat(withCount(TENANT, ids, 1)).hasSize(N);
    }

    @Test
    void pagePastTheEnd_reportsTheTotal_stampsNothing_andIsNotDegenerate() {
        String proj = unique("sp-past-");
        List<Long> ids = seed(TENANT, proj, "lemurpast", N);

        var page = repo.searchPage(TENANT, "lemurpast", proj, true, 5, 100);

        assertThat(page.rows()).isEmpty();
        assertThat(page.matchingTotal())
            .as("an empty page past the end has no row to carry the total, so the statement must").isEqualTo(N);
        assertThat(withCount(TENANT, ids, 0)).hasSize(N);
    }

    @Test
    void degenerateGuard_firesOnlyWhenTheTotalIsZero() {
        String proj = unique("sp-guard-");
        seed(TENANT, proj, "ocelotguard", 3);

        assertThatThrownBy(() -> repo.searchPage(TENANT, "and", proj, true, 5, 0))
            .as("a stopword-only query that matches nothing still raises")
            .isInstanceOf(MemoryRepository.DegenerateQueryException.class);
        assertThatThrownBy(() -> repo.searchPage(TENANT, "and", proj, true, 5, 100))
            .isInstanceOf(MemoryRepository.DegenerateQueryException.class);

        var none = repo.searchPage(TENANT, "nosuchwordanywhere", proj, true, 5, 0);
        assertThat(none.rows()).isEmpty();
        assertThat(none.matchingTotal()).as("a real query that matches nothing is a quiet zero").isZero();

        assertThat(repo.searchPage(TENANT, "ocelotguard", proj, true, 5, 100).matchingTotal())
            .as("past the end of a real match set does not trip the guard").isEqualTo(3);
    }

    @Test
    void pagesOfSeven_coverEveryMatchOnce_eachReportingTheTotal() {
        String proj = unique("sp-cover-");
        List<Long> ids = seed(TENANT, proj, "tapircover", N);

        List<Long> seen = new ArrayList<>();
        for (int off = 0; off < N + 7; off += 7) {
            var page = repo.searchPage(TENANT, "tapircover", proj, false, 7, off);
            assertThat(page.matchingTotal()).isEqualTo(N);
            page.rows().forEach(r -> seen.add(r.getId()));
        }
        assertThat(seen).as("same order as the unpaged search, no gaps, no repeats")
            .containsExactlyElementsOf(ids);
    }

    @Test
    void project_isOptional_andFiltersWhenGiven() {
        String term = unique("gnuproj");
        String p1 = unique("sp-pa-");
        String p2 = unique("sp-pb-");
        seed(TENANT, p1, term, 4);
        seed(TENANT, p2, term, 3);

        assertThat(repo.searchPage(TENANT, term, p1, false, 2, 0).matchingTotal()).isEqualTo(4);
        assertThat(repo.searchPage(TENANT, term, null, false, 2, 0).matchingTotal()).isEqualTo(7);
        assertThat(repo.searchPage(TENANT, term, "  ", false, 2, 0).matchingTotal()).isEqualTo(7);
    }

    @Test
    void anotherTenantsRows_areNeitherStampedNorCounted() {
        String proj = unique("sp-tenant-");
        List<Long> mine = seed(TENANT, proj, "okapitenant", 3);
        List<Long> theirs = seed(OTHER_TENANT, proj, "okapitenant", 4);

        var page = repo.searchPage(TENANT, "okapitenant", proj, true, 10, 0);

        assertThat(page.matchingTotal()).isEqualTo(3);
        assertThat(page.rows()).extracting(r -> r.getId()).containsExactlyElementsOf(mine);
        assertThat(withCount(OTHER_TENANT, theirs, 0)).hasSize(4);
    }

    @Test
    void badBounds_areRefused() {
        assertThatThrownBy(() -> repo.searchPage(TENANT, "x", null, true, 0, 0))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> repo.searchPage(TENANT, "x", null, true, 5, -1))
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void quarantinedMatch_isNeitherReturnedNorCounted() {
        String tenant = "search-page-quarantine-tenant";
        String proj = unique("sp-quar-");
        List<Long> live = seed(tenant, proj, "pangolinquar", 4);
        // ttl 1 and created 30 days ago: expire() quarantines it (the path MemoryRepositoryQuarantineReadPathTest uses).
        long cold = repo.importRow(tenant, proj, "cold-row", "pangolinquar shared body", "t", null, null,
            1, OffsetDateTime.now(ZoneOffset.UTC).minusDays(30), 0, null);

        var before = repo.searchPage(tenant, "pangolinquar", proj, false, 100, 0);
        assertThat(before.matchingTotal()).as("non-vacuity: the cold row matches while live").isEqualTo(5);
        assertThat(before.rows()).extracting(r -> r.getId()).contains(cold);

        assertThat(repo.expire(tenant).quarantinedIds()).containsExactly(cold);

        var after = repo.searchPage(tenant, "pangolinquar", proj, false, 100, 0);
        assertThat(after.matchingTotal()).as("a quarantined match is not counted").isEqualTo(4);
        assertThat(after.rows()).extracting(r -> r.getId()).containsExactlyElementsOf(live)
            .doesNotContain(cold);
        assertThat(repo.searchPage(tenant, "pangolinquar", proj, false, 2, 100).matchingTotal())
            .as("nor counted on an empty page past the end").isEqualTo(4);
    }

    @Test
    void pages_followRankDescendingThenId_theSameOrderAsTheUnpagedSearch() {
        String proj = unique("sp-rank-");
        String term = "rankedterm";
        // Weakest first, so id order is not rank order: a title hit outranks a content hit, and more
        // occurrences outrank fewer.
        repo.upsert(TENANT, proj, "plain one", term, "t", null, null, 30);
        repo.upsert(TENANT, proj, term + " alpha", "filler", "t", null, null, 30);
        repo.upsert(TENANT, proj, "plain two", term + " " + term + " " + term + " " + term, "t", null, null, 30);
        repo.upsert(TENANT, proj, term + " beta", term + " " + term, "t", null, null, 30);
        repo.upsert(TENANT, proj, "plain three", term, "t", null, null, 30);   // ties with "plain one": id breaks it

        List<Long> unpaged = repo.search(TENANT, term, proj, false).stream().map(r -> r.getId()).toList();
        assertThat(unpaged).hasSize(5);
        assertThat(unpaged).as("non-vacuity: rank, not id, decides the order here")
            .isNotEqualTo(unpaged.stream().sorted().toList());

        List<Long> onePerPage = new ArrayList<>();
        for (int off = 0; off < 7; off++) {
            var page = repo.searchPage(TENANT, term, proj, false, 1, off);
            assertThat(page.matchingTotal()).isEqualTo(5);
            page.rows().forEach(r -> onePerPage.add(r.getId()));
        }
        assertThat(onePerPage).as("limit 1 at every offset walks the unpaged order").isEqualTo(unpaged);

        assertThat(repo.searchPage(TENANT, term, proj, false, 100, 0).rows())
            .as("one page of everything is in the same order too")
            .extracting(r -> r.getId()).isEqualTo(unpaged);
    }

    @Test
    void guard_isKeyedToTheTotal_notToAnEmptyPage_whenAStopwordQueryMatchesATitle() {
        // "and" is an english stopword, so the guard's english leg is empty and it WOULD raise on any empty
        // result; but the 'simple' title leg finds this row (MemoryRepositoryTest
        // search_stopwordQuery_stillFindsRealTitleHit_doesNotRaise). Paged past the end the page is empty
        // yet the total is 1, so a guard keyed to "no rows on the page" raises here and a guard keyed to the
        // total does not.
        String proj = unique("sp-stopgate-");
        long id = repo.upsert(TENANT, proj, "and-design.md", "body text with no shared words", "t", null, null, 30);

        var past = repo.searchPage(TENANT, "and", proj, true, 5, 100);

        assertThat(past.rows()).isEmpty();
        assertThat(past.matchingTotal()).isEqualTo(1);
        assertThat(accessCount(TENANT, id)).as("an empty page stamps nothing").isZero();
        assertThat(repo.searchPage(TENANT, "and", proj, false, 5, 0).rows())
            .extracting(r -> r.getId()).containsExactly(id);
    }

    // ── wire ──────────────────────────────────────────────────────────────────

    private HttpResponse<String> post(String tenant, String body) throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/memory/search")
            .header("Authorization", "Bearer " + (OTHER_TENANT.equals(tenant) ? OTHER_TOKEN : TOKEN))
            .header("X-Nexus-Tenant", tenant)
            .header("Content-Type", "application/json")
            .POST(java.net.http.HttpRequest.BodyPublishers.ofString(body))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private static String q(String query, String project, String extra) {
        return "{\"query\":\"" + query + "\",\"project\":\"" + project + "\"" + extra + "}";
    }

    private List<Long> idsOf(JsonNode rows) {
        List<Long> out = new ArrayList<>();
        rows.forEach(n -> out.add(n.get("id").asLong()));
        return out;
    }

    @Test
    void wire_limit_answersRowsAndTotal_andStampsOnlyThePage() throws Exception {
        String proj = unique("sp-wire-");
        List<Long> ids = seed(TENANT, proj, "bonobowire", N);

        var r1 = post(TENANT, q("bonobowire", proj, ",\"limit\":5"));
        assertThat(r1.statusCode()).isEqualTo(200);
        JsonNode body = mapper.readTree(r1.body());
        assertThat(body.isObject()).isTrue();
        assertThat(body.get("matching_total").asInt()).isEqualTo(N);
        assertThat(idsOf(body.get("rows"))).containsExactlyElementsOf(ids.subList(0, 5));
        assertThat(withCount(TENANT, ids, 1)).containsExactlyElementsOf(ids.subList(0, 5));
        assertThat(withCount(TENANT, ids, 0)).hasSize(N - 5);

        var r2 = post(TENANT, q("bonobowire", proj, ",\"limit\":5,\"offset\":5"));
        assertThat(idsOf(mapper.readTree(r2.body()).get("rows"))).containsExactlyElementsOf(ids.subList(5, 10));
        assertThat(withCount(TENANT, ids, 1)).containsExactlyElementsOf(ids.subList(0, 10));
        assertThat(withCount(TENANT, ids, 0)).hasSize(N - 10);
    }

    @Test
    void wire_limitWithAccessSilent_stampsNothing() throws Exception {
        String proj = unique("sp-wsilent-");
        List<Long> ids = seed(TENANT, proj, "dingosilent", N);

        var r = post(TENANT, q("dingosilent", proj, ",\"limit\":5,\"access\":\"silent\""));

        assertThat(r.statusCode()).isEqualTo(200);
        assertThat(mapper.readTree(r.body()).get("rows")).hasSize(5);
        assertThat(withCount(TENANT, ids, 0)).hasSize(N);
    }

    @Test
    void wire_noLimit_isTheBareList_andStampsEveryMatch() throws Exception {
        String proj = unique("sp-wbare-");
        List<Long> ids = seed(TENANT, proj, "yakbare", N);

        var r = post(TENANT, q("yakbare", proj, ""));

        assertThat(r.statusCode()).isEqualTo(200);
        JsonNode body = mapper.readTree(r.body());
        assertThat(body.isArray()).as("an old client with no limit keeps the bare list").isTrue();
        assertThat(idsOf(body)).containsExactlyElementsOf(ids);
        assertThat(withCount(TENANT, ids, 1)).hasSize(N);
    }

    @Test
    void wire_offsetWithoutLimit_isIgnored() throws Exception {
        String proj = unique("sp-woff-");
        List<Long> ids = seed(TENANT, proj, "gnuoffset", 6);

        for (String extra : new String[] {",\"offset\":3", ",\"offset\":-3"}) {
            var r = post(TENANT, q("gnuoffset", proj, extra));
            assertThat(r.statusCode()).as(extra).isEqualTo(200);
            assertThat(idsOf(mapper.readTree(r.body()))).as(extra).containsExactlyElementsOf(ids);
        }
    }

    @Test
    void wire_offsetPastTheEnd_isAnEmptyPageWithTheTotal_notA400() throws Exception {
        String proj = unique("sp-wpast-");
        List<Long> ids = seed(TENANT, proj, "kudupast", N);

        var r = post(TENANT, q("kudupast", proj, ",\"limit\":5,\"offset\":1000"));

        assertThat(r.statusCode()).isEqualTo(200);
        JsonNode body = mapper.readTree(r.body());
        assertThat(body.get("rows")).isEmpty();
        assertThat(body.get("matching_total").asInt()).isEqualTo(N);
        assertThat(withCount(TENANT, ids, 0)).hasSize(N);
    }

    @Test
    void wire_degenerateQueryWithLimit_still400sWithItsCode_andARealMissIsAQuietZero() throws Exception {
        String proj = unique("sp-wguard-");
        seed(TENANT, proj, "elandguard", 2);

        var bad = post(TENANT, q("and", proj, ",\"limit\":5"));
        assertThat(bad.statusCode()).isEqualTo(400);
        assertThat(mapper.readTree(bad.body()).get("code").asText()).isEqualTo("no_searchable_terms");

        var miss = post(TENANT, q("nosuchwordanywhere", proj, ",\"limit\":5"));
        assertThat(miss.statusCode()).isEqualTo(200);
        JsonNode body = mapper.readTree(miss.body());
        assertThat(body.get("rows")).isEmpty();
        assertThat(body.get("matching_total").asInt()).isZero();
    }

    @Test
    void wire_badLimitOrOffset_is400WithoutTheDegenerateCode() throws Exception {
        String proj = unique("sp-wbad-");
        seed(TENANT, proj, "gaurbad", 2);

        for (String extra : new String[] {
                ",\"limit\":0", ",\"limit\":-1", ",\"limit\":\"5\"", ",\"limit\":2.5", ",\"limit\":true",
                ",\"limit\":5,\"offset\":-1", ",\"limit\":5,\"offset\":\"x\"", ",\"limit\":5,\"offset\":1.5"}) {
            var r = post(TENANT, q("gaurbad", proj, extra));
            assertThat(r.statusCode()).as(extra).isEqualTo(400);
            assertThat(mapper.readTree(r.body()).has("code")).as(extra).isFalse();
        }
    }

    @Test
    void wire_limitPastTheCeiling_isClamped_neverWrappedToAnotherNumber() throws Exception {
        // The wire does not echo the effective limit, so the clamp is observed where an unclamped (int) cast
        // would change the answer: 3_000_000_000 wraps negative (a 400) and 2^32 + 1 wraps to 1 (one row).
        String proj = unique("sp-wceil-");
        seed(TENANT, proj, "oryxceiling", 3);

        for (String limit : new String[] {"50000", "3000000000", "4294967297", "99999999999999999999999"}) {
            var r = post(TENANT, q("oryxceiling", proj, ",\"limit\":" + limit));
            assertThat(r.statusCode()).as(limit).isEqualTo(200);
            assertThat(mapper.readTree(r.body()).get("rows")).as(limit).hasSize(3);
        }
    }

    @Test
    void limitIsClampedToTheListCeiling_andOffsetIsBounded() {
        assertThat(MemoryHandler.parseBodyLimit(null)).isNull();
        assertThat(MemoryHandler.parseBodyLimit(5)).isEqualTo(5);
        assertThat(MemoryHandler.parseBodyLimit(MemoryHandler.LIST_LIMIT_CEILING + 1))
            .isEqualTo(MemoryHandler.LIST_LIMIT_CEILING);
        assertThat(MemoryHandler.parseBodyLimit(50_000L)).isEqualTo(MemoryHandler.LIST_LIMIT_CEILING);
        assertThat(MemoryHandler.parseBodyLimit(new BigInteger("99999999999999999999999")))
            .as("a number past a long clamps instead of wrapping").isEqualTo(MemoryHandler.LIST_LIMIT_CEILING);
        assertThatThrownBy(() -> MemoryHandler.parseBodyLimit(0)).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> MemoryHandler.parseBodyLimit(new BigInteger("-99999999999999999999")))
            .isInstanceOf(IllegalArgumentException.class);

        assertThat(MemoryHandler.parseBodyOffset(null)).isZero();
        assertThat(MemoryHandler.parseBodyOffset(7)).isEqualTo(7);
        assertThat(MemoryHandler.parseBodyOffset(new BigInteger("99999999999999999999999")))
            .isEqualTo(Integer.MAX_VALUE);
        assertThatThrownBy(() -> MemoryHandler.parseBodyOffset(-1)).isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void wire_anotherTenantsRows_areNeitherStampedNorCounted() throws Exception {
        String proj = unique("sp-wtenant-");
        List<Long> mine = seed(TENANT, proj, "impalawire", 3);
        List<Long> theirs = seed(OTHER_TENANT, proj, "impalawire", 4);

        var r = post(TENANT, q("impalawire", proj, ",\"limit\":2"));

        JsonNode body = mapper.readTree(r.body());
        assertThat(body.get("matching_total").asInt()).isEqualTo(3);
        assertThat(idsOf(body.get("rows"))).containsExactlyElementsOf(mine.subList(0, 2));
        assertThat(withCount(OTHER_TENANT, theirs, 0)).hasSize(4);
    }
}
