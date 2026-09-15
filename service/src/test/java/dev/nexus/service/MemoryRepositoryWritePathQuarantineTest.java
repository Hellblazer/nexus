package dev.nexus.service;

import dev.nexus.service.db.MemoryRepository;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.nexus.tables.records.MemoryRecord;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-207 bead nexus-l3yuc.4: what each {@link MemoryRepository} write path does to a
 * quarantined row, by shape (RDR-207 § Technical Design, assumption A5).
 *
 * <ul>
 *   <li><b>Conflict branch</b> ({@code upsert}, {@code importRow}, {@code importBatch}):
 *       a write naming an existing title keeps the SAME id, takes the written content
 *       and the write's own TTL, and clears both stamps.</li>
 *   <li><b>Similarity scan</b> ({@code putOrMerge}): a quarantined row is never a merge
 *       target (covered by name in {@code MemoryRepositoryQuarantineReadPathTest}).</li>
 *   <li><b>Explicit ids</b> ({@code mergeMemories}): REFUSES on a quarantined kept id or
 *       delete id, writing nothing. This is also the test that catches bead .3 putting
 *       the read predicate on mergeMemories: with it, the quarantined delete id would
 *       be skipped silently and the merge would succeed.</li>
 * </ul>
 * {@code delete} and {@code deleteById} stay unconditional on purpose (the explicit
 * manual override) and are not exercised here.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class MemoryRepositoryWritePathQuarantineTest {

    private static final String SVC_ROLE = "svc_writepath_test";
    private static final String SVC_PASS = "svc_writepath_test_pass";

    PostgreSQLContainer<?> pg;
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
        repo = new MemoryRepository(new TenantScope(svcDs));
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private static String tenant() { return "tenant-w-" + System.nanoTime(); }
    private static String project() { return "proj-" + System.nanoTime(); }
    private static final OffsetDateTime OLD = OffsetDateTime.now(ZoneOffset.UTC).minusDays(30);

    /** A quarantined AND marked row: imported past its TTL, expired, summarized. */
    private long quarantinedMarked(String t, String p, String title) {
        long id = repo.importRow(t, p, title, "original " + title, "t", null, null, 1, OLD, 0, null);
        assertThat(repo.expire(t).quarantinedIds()).containsExactly(id);
        repo.insertSummary(t, p, "summary of " + title, List.of(id), "m", null);
        MemoryRecord q = repo.listQuarantined(t, p).get(0);
        assertThat(q.getQuarantinedAt()).isNotNull();
        assertThat(q.getRolledUpAt()).isNotNull();
        assertThat(repo.findById(t, id)).as("hidden before the write").isEmpty();
        return id;
    }

    private static void assertRestoredByWrite(MemoryRecord r, long id, String content, Integer ttl) {
        assertThat(r.getId()).as("same id through the title key").isEqualTo(id);
        assertThat(r.getContent()).isEqualTo(content);
        assertThat(r.getTtlDays()).as("the TTL the write gave it").isEqualTo(ttl);
        assertThat(r.getQuarantinedAt()).isNull();
        assertThat(r.getRolledUpAt()).isNull();
    }

    // ── Conflict branch ───────────────────────────────────────────────────────

    @Test
    void upsert_onQuarantinedMarkedTitle_readableAgainWithPutsTtl_sameId() {
        String t = tenant(); String p = project();
        long id = quarantinedMarked(t, p, "keep");

        long written = repo.upsert(t, p, "keep", "new content", "t", null, null, 45);

        assertThat(written).isEqualTo(id);
        MemoryRecord r = repo.findById(t, id).orElseThrow();
        assertRestoredByWrite(r, id, "new content", 45);
        assertThat(repo.listQuarantined(t, p)).isEmpty();
    }

    @Test
    void importRow_onQuarantinedMarkedTitle_readableAgainWithFidelityFields_sameId() {
        String t = tenant(); String p = project();
        long id = quarantinedMarked(t, p, "keep");
        OffsetDateTime ts = OffsetDateTime.now(ZoneOffset.UTC).minusDays(2).withNano(0);
        OffsetDateTime la = OffsetDateTime.now(ZoneOffset.UTC).minusDays(1).withNano(0);

        long written = repo.importRow(t, p, "keep", "imported content", "x,y", "sess", "agent",
            90, ts, 7, la);

        assertThat(written).isEqualTo(id);
        MemoryRecord r = repo.findById(t, id).orElseThrow();
        assertRestoredByWrite(r, id, "imported content", 90);
        assertThat(r.getTimestamp().toInstant()).isEqualTo(ts.toInstant());
        // findById tracks access: +1 on the imported 7, last_accessed moves to now.
        assertThat(r.getAccessCount()).isEqualTo(8);
        assertThat(r.getTags()).isEqualTo("x,y");
        assertThat(r.getSession()).isEqualTo("sess");
    }

    @Test
    void importBatch_onQuarantinedMarkedTitle_readableAgain_sameId() {
        String t = tenant(); String p = project();
        long id = quarantinedMarked(t, p, "keep");
        OffsetDateTime ts = OffsetDateTime.now(ZoneOffset.UTC).minusDays(2).withNano(0);

        int n = repo.importBatch(t, List.of(
            new MemoryRepository.ImportRow(p, "keep", "batched content", "b", null, null, 60, ts, 0, null),
            new MemoryRepository.ImportRow(p, "other", "another row", "b", null, null, 60, ts, 0, null)));

        assertThat(n).isEqualTo(2);
        MemoryRecord r = repo.findById(t, id).orElseThrow();
        assertRestoredByWrite(r, id, "batched content", 60);
        assertThat(repo.listQuarantined(t, p)).isEmpty();
        assertThat(repo.findByProject(t, p)).hasSize(2);
    }

    // ── Explicit ids ──────────────────────────────────────────────────────────

    @Test
    void mergeMemories_quarantinedDeleteId_refusedNothingWritten() {
        String t = tenant(); String p = project();
        long hidden = quarantinedMarked(t, p, "hidden");
        long keep = repo.upsert(t, p, "keep", "keep content", "t", null, null, 30);

        assertThatThrownBy(() -> repo.mergeMemories(t, keep, List.of(hidden), "merged"))
            .isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("quarantined");

        assertThat(repo.findById(t, keep).orElseThrow().getContent())
            .as("nothing written to the kept row").isEqualTo("keep content");
        assertThat(repo.listQuarantined(t, p)).extracting(MemoryRecord::getId)
            .as("the quarantined row was not deleted").containsExactly(hidden);
    }

    @Test
    void mergeMemories_quarantinedKeepId_refusedNothingWritten() {
        String t = tenant(); String p = project();
        long hidden = quarantinedMarked(t, p, "hidden");
        long victim = repo.upsert(t, p, "victim", "victim content", "t", null, null, 30);

        assertThatThrownBy(() -> repo.mergeMemories(t, hidden, List.of(victim), "merged"))
            .isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("quarantined");

        assertThat(repo.findById(t, victim)).as("the delete id survives").isPresent();
        MemoryRecord q = repo.listQuarantined(t, p).get(0);
        assertThat(q.getContent()).as("the quarantined row's content is untouched")
            .isEqualTo("original hidden");
    }

    @Test
    void mergeMemories_liveIds_stillMerges() {
        String t = tenant(); String p = project();
        long keep = repo.upsert(t, p, "keep", "a", "t", null, null, 30);
        long drop = repo.upsert(t, p, "drop", "b", "t", null, null, 30);

        repo.mergeMemories(t, keep, List.of(drop), "a+b");

        assertThat(repo.findById(t, keep).orElseThrow().getContent()).isEqualTo("a+b");
        assertThat(repo.findById(t, drop)).isEmpty();
        assertThat(repo.listQuarantined(t, p)).as("deleted, not quarantined").isEmpty();
    }
}
