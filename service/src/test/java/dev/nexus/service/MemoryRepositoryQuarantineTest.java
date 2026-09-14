package dev.nexus.service;

import dev.nexus.service.db.MemoryRepository;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.nexus.tables.records.MemoryRecord;
import dev.nexus.service.jooq.nexus.tables.records.MemorySummariesRecord;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Optional;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-207 bead nexus-l3yuc.2: the quarantine boundary in {@link MemoryRepository}.
 *
 * <p>Expiry quarantines instead of deleting; reap deletes only rows that are both
 * quarantined and marked rolled-up; restore clears both stamps and makes the row
 * permanent; insertSummary marks its sources in the same transaction as the summary
 * row and refuses on any unknown source id. Scenarios follow RDR-207 § Test Plan.
 *
 * <p>Same fixture as {@link MemoryRepositoryTest}: service role (NOSUPERUSER
 * NOBYPASSRLS) over {@link TenantScope}, so every call runs under RLS. expire and
 * reap are tenant-wide, so each test that asserts exact contents uses its own
 * tenant string.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class MemoryRepositoryQuarantineTest {

    private static final String SVC_ROLE = "svc_quarantine_test";
    private static final String SVC_PASS = "svc_quarantine_test_pass";

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    MemoryRepository repo;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        repo = new MemoryRepository(tenantScope);
    }

    @AfterAll
    void stopAll() throws Exception {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private static String tenant() { return "tenant-q-" + System.nanoTime(); }
    private static String project() { return "proj-" + System.nanoTime(); }

    /** A row past its TTL: ttl 1 day, created three days ago, never accessed. */
    private long expiredRow(String tenant, String project, String title) {
        return repo.importRow(tenant, project, title, "content of " + title, "t", null, null,
            1, OffsetDateTime.now(ZoneOffset.UTC).minusDays(3), 0, null);
    }

    /** A live row with a generous TTL. */
    private long liveRow(String tenant, String project, String title) {
        return repo.upsert(tenant, project, title, "content of " + title, "t", null, null, 30);
    }

    private MemoryRecord quarantined(String tenant, String project, long id) {
        return repo.listQuarantined(tenant, project).stream()
            .filter(r -> r.getId() == id).findFirst()
            .orElseThrow(() -> new AssertionError("row " + id + " not in the quarantined list"));
    }

    // ── expire ────────────────────────────────────────────────────────────────

    @Test
    void expire_quarantinesInsteadOfDeleting_deletedIdsEmpty() {
        String t = tenant(); String p = project();
        long stale = expiredRow(t, p, "stale");
        long live = liveRow(t, p, "live");

        MemoryRepository.ExpireResult r = repo.expire(t);

        assertThat(r.deletedIds()).as("deleted_ids is always empty from this engine on").isEmpty();
        assertThat(r.quarantinedIds()).as("exactly the stale row is quarantined").containsExactly(stale);

        MemoryRecord q = quarantined(t, p, stale);
        assertThat(q.getQuarantinedAt()).as("the row still exists, stamped").isNotNull();
        assertThat(q.getRolledUpAt()).as("expire never marks").isNull();
        assertThat(repo.listQuarantined(t, p)).extracting(MemoryRecord::getId)
            .as("the live row is not quarantined").containsExactly(stale);
        assertThat(repo.findById(t, live)).isPresent();
    }

    @Test
    void expire_secondSweep_returnsNothingNew() {
        String t = tenant(); String p = project();
        long stale = expiredRow(t, p, "stale");

        assertThat(repo.expire(t).quarantinedIds()).containsExactly(stale);
        OffsetDateTime firstStamp = quarantined(t, p, stale).getQuarantinedAt();

        MemoryRepository.ExpireResult again = repo.expire(t);
        assertThat(again.quarantinedIds())
            .as("quarantined_at IS NULL restricts the candidates: a second sweep re-stamps nothing")
            .isEmpty();
        assertThat(quarantined(t, p, stale).getQuarantinedAt())
            .as("the original stamp is untouched").isEqualTo(firstStamp);
    }

    // ── reap ──────────────────────────────────────────────────────────────────

    @Test
    void reap_unmarkedOnly_deletesNothing() {
        String t = tenant(); String p = project();
        long stale = expiredRow(t, p, "stale");
        repo.expire(t);

        assertThat(repo.reap(t)).as("no mark, no deletion").isEmpty();
        assertThat(repo.listQuarantined(t, p)).extracting(MemoryRecord::getId).containsExactly(stale);
    }

    @Test
    void reap_deletesExactlyTheMarkedQuarantinedRow() {
        String t = tenant(); String p = project();
        long marked = expiredRow(t, p, "marked");
        long unmarked = expiredRow(t, p, "unmarked");
        long liveMarked = liveRow(t, p, "live-marked");
        repo.expire(t);
        repo.insertSummary(t, p, "summary covering marked and a live row",
            List.of(marked, liveMarked), "test-model", "l3yuc");

        List<Long> reaped = repo.reap(t);

        assertThat(reaped).as("only the row with BOTH stamps is deleted").containsExactly(marked);
        assertThat(repo.listQuarantined(t, p)).extracting(MemoryRecord::getId)
            .as("the unmarked quarantined row survives").containsExactly(unmarked);
        assertThat(repo.findById(t, liveMarked))
            .as("a marked but live row is never reaped").isPresent();
    }

    // ── insertSummary ─────────────────────────────────────────────────────────

    @Test
    void insertSummary_marksEverySource_inOneTransaction() {
        String t = tenant(); String p = project();
        long a = expiredRow(t, p, "a");
        long b = expiredRow(t, p, "b");
        repo.expire(t);

        long summaryId = repo.insertSummary(t, p, "a and b, summarized", List.of(a, b), "test-model", "l3yuc");

        assertThat(summaryId).isPositive();
        List<MemorySummariesRecord> summaries = repo.listSummaries(t, p);
        assertThat(summaries).hasSize(1);
        assertThat(summaries.get(0).getSourceIds()).containsExactlyInAnyOrder(a, b);
        assertThat(summaries.get(0).getModel()).isEqualTo("test-model");
        assertThat(summaries.get(0).getProducedBy()).isEqualTo("l3yuc");
        assertThat(quarantined(t, p, a).getRolledUpAt()).isNotNull();
        assertThat(quarantined(t, p, b).getRolledUpAt()).isNotNull();
    }

    @Test
    void insertSummary_unknownSourceId_refusedNothingWritten() {
        String t = tenant(); String p = project();
        long known = expiredRow(t, p, "known");
        repo.expire(t);

        assertThatThrownBy(() -> repo.insertSummary(t, p, "summary", List.of(known, 987654321L),
                "test-model", "l3yuc"))
            .isInstanceOf(MemoryRepository.UnknownSourceIdException.class)
            .hasMessageContaining("1 of 2");

        assertThat(repo.listSummaries(t, p)).as("no summary row").isEmpty();
        assertThat(quarantined(t, p, known).getRolledUpAt()).as("no mark on the known id").isNull();
    }

    @Test
    void insertSummary_sourceFromAnotherProject_isUnknown() {
        String t = tenant(); String p = project(); String other = project();
        long elsewhere = liveRow(t, other, "elsewhere");

        assertThatThrownBy(() -> repo.insertSummary(t, p, "summary", List.of(elsewhere), "m", null))
            .as("a source id must be a row of THAT project")
            .isInstanceOf(MemoryRepository.UnknownSourceIdException.class);
        assertThat(repo.listSummaries(t, null)).isEmpty();
    }

    @Test
    void insertSummary_emptySourcesOrBlankContent_isBadRequest() {
        String t = tenant(); String p = project();
        assertThatThrownBy(() -> repo.insertSummary(t, p, "summary", List.of(), "m", null))
            .isInstanceOf(IllegalArgumentException.class)
            .isNotInstanceOf(IllegalStateException.class);
        assertThatThrownBy(() -> repo.insertSummary(t, p, "   ", List.of(1L), "m", null))
            .isInstanceOf(IllegalArgumentException.class)
            .isNotInstanceOf(IllegalStateException.class);
    }

    @Test
    void insertSummary_coveringAgain_movesTheMark() throws Exception {
        String t = tenant(); String p = project();
        long a = expiredRow(t, p, "a");
        repo.expire(t);
        repo.insertSummary(t, p, "first", List.of(a), "m", null);
        OffsetDateTime first = quarantined(t, p, a).getRolledUpAt();
        Thread.sleep(5);
        repo.insertSummary(t, p, "second", List.of(a), "m", null);

        assertThat(quarantined(t, p, a).getRolledUpAt()).isAfter(first);
        assertThat(repo.listSummaries(t, p)).hasSize(2);
    }

    // ── restore ───────────────────────────────────────────────────────────────

    @Test
    void restore_clearsBothStampsAndTtl_rowReadableAgain() {
        String t = tenant(); String p = project();
        long a = expiredRow(t, p, "a");
        repo.expire(t);
        repo.insertSummary(t, p, "summary", List.of(a), "m", null);
        assertThat(repo.findById(t, a)).as("hidden while quarantined").isEmpty();

        assertThat(repo.restore(t, a)).isTrue();

        Optional<MemoryRecord> back = repo.findById(t, a);
        assertThat(back).as("readable again").isPresent();
        assertThat(back.get().getQuarantinedAt()).isNull();
        assertThat(back.get().getRolledUpAt()).isNull();
        assertThat(back.get().getTtlDays()).as("restored rows are permanent").isNull();
        assertThat(repo.listQuarantined(t, p)).isEmpty();
    }

    @Test
    void restore_liveOrUnknownId_returnsFalse() {
        String t = tenant(); String p = project();
        long live = liveRow(t, p, "live");
        assertThat(repo.restore(t, live)).as("a live row is not a restore target").isFalse();
        assertThat(repo.restore(t, 987654321L)).isFalse();
        assertThat(repo.findById(t, live).get().getTtlDays()).as("untouched").isEqualTo(30);
    }

    // ── The full cycle ────────────────────────────────────────────────────────

    @Test
    void fullCycle_quarantineMarkRestorePutExpire_reapDeletesNothing() {
        String t = tenant(); String p = project();
        long a = expiredRow(t, p, "cycle");

        assertThat(repo.expire(t).quarantinedIds()).containsExactly(a);
        repo.insertSummary(t, p, "summary", List.of(a), "m", null);
        assertThat(repo.restore(t, a)).isTrue();

        // A put gives the row a new TTL (importRow is the put that can also backdate
        // it past that TTL; same title, so the conflict branch keeps the id).
        long again = repo.importRow(t, p, "cycle", "rewritten", "t", null, null,
            1, OffsetDateTime.now(ZoneOffset.UTC).minusDays(3), 0, null);
        assertThat(again).as("same id through the title key").isEqualTo(a);

        assertThat(repo.expire(t).quarantinedIds()).as("re-quarantined").containsExactly(a);
        assertThat(repo.reap(t)).as("unmarked after restore: reap deletes nothing").isEmpty();
        MemoryRecord q = quarantined(t, p, a);
        assertThat(q.getQuarantinedAt()).isNotNull();
        assertThat(q.getRolledUpAt()).as("the earlier mark did not survive restore").isNull();
    }

    // ── Shape and tenant isolation ────────────────────────────────────────────

    @Test
    void listQuarantined_carriesTheFullRecordShape_andProjectIsOptional() {
        String t = tenant(); String p1 = project(); String p2 = project();
        long a = expiredRow(t, p1, "a");
        long b = expiredRow(t, p2, "b");
        repo.expire(t);

        MemoryRecord q = quarantined(t, p1, a);
        assertThat(q.getTitle()).isEqualTo("a");
        assertThat(q.getContent()).as("Phase 3's rollup reads content from here").isEqualTo("content of a");
        assertThat(q.getTimestamp()).isNotNull();
        assertThat(q.getTtlDays()).isEqualTo(1);
        assertThat(q.getTags()).isEqualTo("t");

        assertThat(repo.listQuarantined(t, null)).extracting(MemoryRecord::getId)
            .as("project absent means the whole tenant").containsExactlyInAnyOrder(a, b);
        assertThat(repo.listQuarantined(t, "")).extracting(MemoryRecord::getId)
            .containsExactlyInAnyOrder(a, b);
        assertThat(repo.listQuarantined(t, p2)).extracting(MemoryRecord::getId).containsExactly(b);
    }

    @Test
    void tenantIsolation_quarantinedRowsAndSummariesInvisibleToOtherTenant() {
        String ta = tenant(); String tb = tenant(); String p = project();
        long a = expiredRow(ta, p, "a");
        repo.expire(ta);
        repo.insertSummary(ta, p, "summary", List.of(a), "m", null);

        assertThat(repo.listQuarantined(tb, p)).isEmpty();
        assertThat(repo.listQuarantined(tb, null)).isEmpty();
        assertThat(repo.listSummaries(tb, p)).isEmpty();
        assertThat(repo.listSummaries(tb, null)).isEmpty();
        assertThat(repo.expire(tb).quarantinedIds()).as("expire is tenant-scoped").isEmpty();
        assertThat(repo.reap(tb)).as("reap is tenant-scoped").isEmpty();
        assertThat(repo.restore(tb, a)).as("restore is tenant-scoped").isFalse();
        assertThat(repo.listQuarantined(ta, p)).extracting(MemoryRecord::getId)
            .as("tenant A still sees its row (positive control)").containsExactly(a);
    }
}
