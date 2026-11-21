// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.P31CheckReplay;
import dev.nexus.service.PgCatalogProbes;
import dev.nexus.service.PgContainerHelper;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;

/**
 * RDR-229 P3.1 (nexus-p2m1x.17), fix round A, critique S2 / code review F3 and the critique's observation on the orphan guard:
 * the shape of the stamp moves' {@code on_complete}, read from the rows the walk registered.
 *
 * <ul>
 *   <li>Every on_complete VALIDATEs a CHECK only if the constraint exists. After the R5 down-step the CHECKs are gone while
 *       DATABASECHANGELOG still records vectors-068 as run, so a redeploy does not re-add them; a plain VALIDATE then raised
 *       42704, which the worker classifies as permanent, and gate 1 never held. A missing CHECK is the completion check's
 *       {@code checks_missing}, a better failure than a failed move.</li>
 *   <li>The orphan guard (a scan for a name with no registry row, so the VALIDATE waits instead of failing) is absent from the
 *       moves of nexus.chunks and nexus.taxonomy_centroids: their composite foreign key to the registry forbids an orphan, so the
 *       guard can never find one and only doubles the scan of the two largest tables. Every other table keeps it.</li>
 * </ul>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class StampMoveOnCompleteGuardIntegrationTest {

    /** The tables whose composite FK to the registry makes an orphan impossible. */
    private static final Set<String> FK_TABLES = Set.of("chunks", "taxonomy_centroids");

    private PostgreSQLContainer<?> pg;
    private Connection su;
    private DSLContext ctx;
    /** label to on_complete, as the walk registered them. */
    private final Map<String, String> onComplete = new LinkedHashMap<>();

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        su = pg.createConnection("");
        su.setAutoCommit(true);
        ctx = DSL.using(su, SQLDialect.POSTGRES);
        var label = DSL.field(DSL.name("label"), String.class);
        var sql = DSL.field(DSL.name("on_complete"), String.class);
        for (var r : ctx.select(label, sql).from(DSL.table(DSL.name("nexus", "data_moves")))
                .where(label.like(StampMoveSpecs.LABEL_PREFIX + "%")).orderBy(label).fetch()) {
            onComplete.put(r.value1(), r.value2());
        }
    }

    @AfterAll
    void stop() throws Exception {
        if (su != null) su.close();
        if (pg != null) pg.stop();
    }

    /** Runs a move's on_complete as the worker does, on the migrating connection. */
    private void complete(String body) throws Exception {
        try (var statement = su.createStatement()) {
            statement.execute(body);
        }
    }

    private void dropCheck(String table, String constraint) {
        ctx.alterTable(DSL.table(DSL.name("nexus", table))).dropConstraintIfExists(DSL.name(constraint)).execute();
    }

    @Test
    void everyStampMoveCompletesWhenItsChecksAreGone_afterTheDownStepAndARedeploy() throws Exception {
        int examined = 0;
        for (var spec : StampMoveSpecs.SPECS) {
            String label = spec.label();
            assertThat(onComplete).as("the walk registers %s", label).containsKey(label);
            var constraints = StampMoveSpecs.constraintsByTable().get(spec.table());
            assertThat(constraints).as("%s names its table's CHECKs", label).isNotEmpty();
            for (String constraint : constraints) {
                dropCheck(spec.table(), constraint);
                assertThat(PgCatalogProbes.constraintCount(ctx, constraint)).as("non-vacuity: %s is gone", constraint).isZero();
            }
            complete(onComplete.get(label));   // before the fix: 42704, undefined_object, permanent
            for (String constraint : constraints) {
                assertThat(PgCatalogProbes.constraintCount(ctx, constraint)).as("%s: on_complete re-adds nothing", label).isZero();
            }
            examined++;
        }
        assertThat(examined).as("non-vacuity: every table's stamp move, the queue's aside (absent on this install)").isEqualTo(15);
    }

    @Test
    void theOrphanGuardIsAbsentForTheFkGuardedTablesAndPresentForEveryOtherNameIdTable() {
        int guarded = 0;
        for (var check : StampMoveSpecs.checks()) {
            if (!check.kind().equals("NAME_ID") || check.table().equals("aspect_extraction_queue")) {
                continue;
            }
            String body = onComplete.get(StampMoveSpecs.LABEL_PREFIX + check.table());
            boolean scansForOrphans = body.contains("FROM nexus." + check.table() + " t WHERE")
                && body.contains("nexus.catalog_collections cc");
            if (FK_TABLES.contains(check.table())) {
                assertThat(scansForOrphans).as("%s: an orphan is impossible (composite FK), so no scan", check.table()).isFalse();
            } else {
                assertThat(scansForOrphans).as("%s keeps its orphan guard", check.table()).isTrue();
                guarded++;
            }
        }
        assertThat(guarded).as("non-vacuity: the guard is on the other tables").isGreaterThanOrEqualTo(10);
    }

    @Test
    void theFkGuardedTablesStillValidateBothOfTheirChecks() throws Exception {
        // chunks and taxonomy_centroids validate the name-id CHECK and the NOT NULL one without the scan
        for (String table : FK_TABLES) {
            P31CheckReplay.lift(ctx, changesetFor(table));      // gone, or NOT VALID from an earlier case: start from none
            P31CheckReplay.restore(ctx, changesetFor(table));   // and add them back NOT VALID, as the expand leaves them
            var constraints = StampMoveSpecs.constraintsByTable().get(table);
            assertThat(constraints).hasSize(2);
            for (String constraint : constraints) {
                assertThat(PgCatalogProbes.constraintValidated(ctx, constraint)).as("non-vacuity: %s is NOT VALID before", constraint).isFalse();
            }
            complete(onComplete.get(StampMoveSpecs.LABEL_PREFIX + table));
            for (String constraint : constraints) {
                assertThat(PgCatalogProbes.constraintValidated(ctx, constraint)).as("%s validated by on_complete", constraint).isTrue();
            }
        }
    }

    /** The vectors-068 changeset that adds the table's CHECKs, from the changeset ids in the expand file (chunks 068-3, taxonomy_centroids 068-4). */
    private static String changesetFor(String table) {
        return switch (table) {
            case "chunks" -> "vectors-068-3";
            case "taxonomy_centroids" -> "vectors-068-4";
            default -> throw new IllegalArgumentException(table);
        };
    }
}
