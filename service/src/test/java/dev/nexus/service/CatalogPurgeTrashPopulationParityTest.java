// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantScope;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-ff85q — {@code purge-trash}'s dry-run population and its execute population
 * MUST be the same set under identical state.
 *
 * <p>PRODUCTION FAILURE THIS PINS (2026-08-03, engine v0.1.63): a first real
 * {@code nx catalog purge-trash --no-dry-run --confirm} reported
 * {@code documents_purged=2} against a dry-run that had reported
 * {@code documents_purged=63}. 61 age-eligible tombstones survived, and the partial
 * execution was reported as a bare success.
 *
 * <p>ROOT CAUSE the tests below reproduce: {@link CatalogRepository#purgeTrash} built
 * the SQL function's {@code older_than interval} argument via
 * {@code YearToSecond.valueOf(Duration.ofDays(n))}. jOOQ NORMALISES a {@link
 * java.time.Duration} into its year/month/day fields using FIXED 30-day months and
 * 365.25-day years, so 30 days becomes the interval {@code '+0-1 +0'} — literally
 * "1 month" — and 365 days becomes {@code '+1-0 +5'} ("1 year 5 days"). PostgreSQL then
 * evaluates {@code NOW() - interval '1 mon'} with CALENDAR arithmetic, landing on a
 * threshold that is NOT n days back. The dry-run preview
 * ({@code CatalogRepository#agedTombstoneCount}) meanwhile computed its own threshold in
 * Java as {@code now().minusDays(n)} — exact days. Two different cut points over one
 * population: every tombstone in the gap is counted by the preview and skipped by the
 * DELETE, silently.
 *
 * <p>DETERMINISM (why 365 and not 30): the skew for a 30-day threshold is the difference
 * between "30 days" and "one calendar month", which is ZERO whenever the preceding month
 * happens to have 30 days (Apr/Jun/Sep/Nov) — a test using 30 would pass a third of the
 * year regardless of the bug. jOOQ's year is 365.25 days, so a 365-day threshold ALWAYS
 * normalises to {@code 1 year + 5 days} and PostgreSQL ALWAYS resolves that to 370 or 371
 * real days, never 365. The skew is therefore non-zero in every calendar month, and this
 * test is deterministic year-round. Same reason the fixture's offsets are expressed as
 * exact {@code interval 'N days'} backdates rather than wall-clock waits.
 *
 * <p><strong>SCOPE — THIS SUITE PINS THE UNDER-DELETION DIRECTION ONLY. GREEN HERE IS NOT
 * "BOTH DIRECTIONS PINNED."</strong> The determinism choice above has a structural
 * consequence: a 365-day threshold makes the buggy calendar interval strictly LARGER than
 * the exact-day request (370-371 &gt; 365), so this fixture can only ever exercise the
 * conservative skew — the execute purging FEWER documents than the preview promised, which
 * is the production-observed nexus-ff85q signature. The opposite direction is real and
 * worse: a 30-day threshold evaluated in a SHORT month normalises to FEWER real days
 * ({@code NOW() - interval '1 mon'} from March is 28 days back), so the execute would purge
 * tombstones NEWER than the dry-run ever showed the operator — silent data loss rather than
 * silent incompleteness. Nothing here or in {@code CatalogPurgeTrashTest} /
 * {@code CatalogHandlerPurgeTrashTest} covers it, and no FIXED day-count can: which
 * direction the skew takes depends on the real calendar months "now" spans, so forcing it
 * deterministically requires controlling the DB clock, which none of these suites do.
 *
 * <p>The FIX is symmetric by construction and closes both directions at once — {@code
 * CatalogRepository#olderThanInterval} puts the entire magnitude in the interval's DAY
 * field, leaving nothing for jOOQ to normalise into months or years and therefore no
 * calendar arithmetic to shrink OR grow the threshold. What is missing is REGRESSION
 * COVERAGE, not correctness: a future change that reintroduces calendar-interval math
 * through some other path, and happens to shrink rather than grow at a 365-day threshold,
 * would pass this suite. The clock-injected test that would close that gap is tracked on
 * nexus-nhfqa.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogPurgeTrashPopulationParityTest {

    private static final String SVC_ROLE = "svc_purge_parity";
    private static final String SVC_PASS = "svc_purge_parity_pass";

    private static final String TENANT     = "purge-parity";
    private static final String COLLECTION = "knowledge__purge-parity__minilm-l6-v2-384__v1";

    /** Threshold used throughout: see the class javadoc's DETERMINISM note. */
    private static final int OLDER_THAN_DAYS = 365;

    /**
     * Tombstone ages seeded, in days. All are &gt;= 365 so ALL of them are age-eligible
     * under the verb's documented contract ("tombstoned at least 365 days ago").
     * 366/368/370 sit inside the buggy interval's 370-371-day gap — they are exactly the
     * production population that the preview counted and the DELETE skipped. 400/500 clear
     * every interpretation and are the analogue of production's "2 that did get purged".
     */
    private static final int[] TOMB_AGES_DAYS = {366, 367, 368, 369, 400, 500};

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    CatalogRepository catalogRepo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            // purge_trash(interval) EXECUTE is not part of bootstrapServiceRole's
            // fixed grant set (nexus-cbo4a batch 1a) -- kept as an explicit grant.
            su.createStatement().execute(
                "GRANT EXECUTE ON FUNCTION nexus.purge_trash(interval) TO " + SVC_ROLE);
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        catalogRepo = new CatalogRepository(new TenantScope(svcDs));
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    /**
     * Rebuild the fixture from scratch before each test so the destructive test does not
     * make the non-destructive one order-dependent (the sibling {@code
     * CatalogPurgeTrashTest} is deliberately ordered + shared-fixture; this suite is not).
     */
    @BeforeEach
    void seed() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DELETE FROM nexus.catalog_documents WHERE tenant_id = '" + TENANT + "'");
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('" + TENANT + "', '"
                + COLLECTION + "') ON CONFLICT DO NOTHING");

            // A live document — must never be touched by any of this.
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, physical_collection) "
                + "VALUES ('" + TENANT + "', 'parity-live', 'Live', '" + COLLECTION + "')");

            // A tombstone NEWER than the threshold — must never be purged.
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, physical_collection, deleted_at) "
                + "VALUES ('" + TENANT + "', 'parity-tomb-fresh', 'Fresh', '" + COLLECTION + "', "
                + "NOW() - interval '10 days')");

            // The age-eligible tombstones. No manifest rows and no chunks_<dim> rows for any
            // of them: this is the DOMINANT production shape (yesterday's vanished/orphaned
            // tombstones own zero chunks) and it also proves the doc purge is not gated on
            // owning stranded chunks.
            for (int ageDays : TOMB_AGES_DAYS) {
                su.createStatement().execute(
                    "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, physical_collection, deleted_at) "
                    + "VALUES ('" + TENANT + "', 'parity-tomb-" + ageDays + "', 'Tomb " + ageDays + "', '"
                    + COLLECTION + "', NOW() - interval '" + ageDays + " days')");
            }
        }
    }

    private long liveTombstoneRowsRemaining() throws Exception {
        try (Connection su = pg.createConnection("")) {
            var rs = su.createStatement().executeQuery(
                "SELECT count(*) FROM nexus.catalog_documents WHERE tenant_id = '" + TENANT
                + "' AND deleted_at IS NOT NULL AND deleted_at <= NOW() - interval '"
                + OLDER_THAN_DAYS + " days'");
            rs.next();
            return rs.getLong(1);
        }
    }

    /**
     * THE BEAD'S INVARIANT: the population the dry-run reports and the population the
     * execute purges are the same set. Fails pre-fix with preview=6, purged=2 — the exact
     * production signature at a different scale.
     */
    @Test
    void executePurgesExactlyTheDryRunsReportedPopulation() throws Exception {
        Map<String, Object> preview = catalogRepo.purgeTrashPreview(TENANT, OLDER_THAN_DAYS);
        long previewed = ((Number) preview.get("documents_purged")).longValue();
        assertThat(previewed)
            .as("dry-run must count every tombstone older than %d days", OLDER_THAN_DAYS)
            .isEqualTo(TOMB_AGES_DAYS.length);

        Map<String, Object> executed = catalogRepo.purgeTrash(TENANT, OLDER_THAN_DAYS);
        long purged = ((Number) executed.get("documents_purged")).longValue();

        assertThat(purged)
            .as("execute must purge EXACTLY the dry-run's reported population — a purge that "
                + "takes a subset and returns success is the nexus-ff85q silent-partial")
            .isEqualTo(previewed);

        assertThat(liveTombstoneRowsRemaining())
            .as("no age-eligible tombstone may survive a completed purge")
            .isZero();
    }

    /**
     * The threshold the execute path actually applies must be the one the caller asked
     * for. Pinned independently of the parity test so a future change that makes both
     * sides consistently WRONG (e.g. by teaching the preview jOOQ's 30-day months) still
     * fails here.
     */
    @Test
    void executeAppliesTheRequestedDayThreshold_notACalendarMonthApproximation() throws Exception {
        catalogRepo.purgeTrash(TENANT, OLDER_THAN_DAYS);

        try (Connection su = pg.createConnection("")) {
            var rs = su.createStatement().executeQuery(
                "SELECT tumbler FROM nexus.catalog_documents WHERE tenant_id = '" + TENANT
                + "' ORDER BY tumbler");
            var survivors = new java.util.ArrayList<String>();
            while (rs.next()) survivors.add(rs.getString(1));

            assertThat(survivors)
                .as("exactly the live doc and the 10-day-old tombstone survive a 365-day purge; "
                    + "every 366+-day tombstone is gone")
                .containsExactlyInAnyOrderElementsOf(List.of("parity-live", "parity-tomb-fresh"));
        }
    }

    /**
     * Even with the threshold fixed, a purge that removes fewer rows than it found
     * eligible must SAY SO rather than returning a bare success. Pins the reporting half
     * of the bead: the response carries the eligible count alongside the purged count, so
     * a caller (and {@code nx catalog purge-trash}) can detect partiality without a
     * follow-up dry-run.
     */
    @Test
    void executeReportsTheEligiblePopulationAlongsideThePurgedCount() {
        Map<String, Object> executed = catalogRepo.purgeTrash(TENANT, OLDER_THAN_DAYS);

        assertThat(executed)
            .as("execute response must expose the eligible population it measured")
            .containsKey("documents_eligible");
        assertThat(((Number) executed.get("documents_eligible")).longValue())
            .isEqualTo(TOMB_AGES_DAYS.length);
        assertThat(((Number) executed.get("documents_purged")).longValue())
            .isEqualTo(((Number) executed.get("documents_eligible")).longValue());
    }
}
