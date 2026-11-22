// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import dev.nexus.service.P31CheckReplay;
import dev.nexus.service.PgCatalogProbes;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.VectorSeeds;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Table;
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
 *   <li>The orphan guard (a scan for a name with no registry row, so the VALIDATE waits instead of failing) may be absent only from a
 *       table whose validated foreign key to the registry forbids an orphan (nexus.chunks: chunks_collection_fk), because the guard can
 *       never find one there and only doubles the scan of the largest table. The set of such tables is read from pg_constraint, not
 *       written down: round 1 dropped the guard for nexus.taxonomy_centroids on the premise that it has the same foreign key, and it has
 *       none (review N1, nexus-p2m1x.17), so an orphan centroid made the move FAIL instead of leaving its CHECK NOT VALID.</li>
 * </ul>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class StampMoveOnCompleteGuardIntegrationTest {

    private static final Table<?> CENTROIDS = DSL.table(DSL.name("nexus", "taxonomy_centroids"));
    private static final Field<String> TENANT_F = DSL.field(DSL.name("tenant_id"), String.class);
    private static final Field<String> CENTROID_NAME = DSL.field(DSL.name("collection"), String.class);
    private static final Field<Long> CENTROID_ID = DSL.field(DSL.name("collection_id"), Long.class);
    private static final Field<Long> TOPIC_ID = DSL.field(DSL.name("topic_id"), Long.class);
    private static final Field<String> LABEL_F = DSL.field(DSL.name("label"), String.class);
    private static final Field<String> MODEL_F = DSL.field(DSL.name("embedding_model"), String.class);
    private static final String ORPHAN_NAME = "p31-no-such-collection";

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
        MoveSql.runOnComplete(su, body);   // the worker's own call
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

    /**
     * The tables with a VALIDATED foreign key to the registry that is not a partition's inherited copy, read from pg_constraint: the tables
     * on which an orphan cannot exist. (A name-id table that is absent here has no such key, whatever a comment says.)
     */
    private Set<String> tablesWithRegistryForeignKey() {
        Table<?> con = DSL.table(DSL.name("pg_catalog", "pg_constraint")).as("c");
        Table<?> from = DSL.table(DSL.name("pg_catalog", "pg_class")).as("rel");
        Table<?> to = DSL.table(DSL.name("pg_catalog", "pg_class")).as("ref");
        Field<String> relname = DSL.field(DSL.name("rel", "relname"), String.class);
        var out = new java.util.TreeSet<String>();
        out.addAll(ctx.selectDistinct(relname).from(con)
            .join(from).on(DSL.field(DSL.name("rel", "oid")).eq(DSL.field(DSL.name("c", "conrelid"))))
            .join(to).on(DSL.field(DSL.name("ref", "oid")).eq(DSL.field(DSL.name("c", "confrelid"))))
            .where(DSL.field(DSL.name("c", "contype")).cast(String.class).eq("f"))
            .and(DSL.field(DSL.name("ref", "relname"), String.class).eq("catalog_collections"))
            .and(DSL.field(DSL.name("c", "conparentid"), Long.class).eq(0L))
            .and(DSL.field(DSL.name("c", "convalidated"), Boolean.class))
            .fetch(relname));
        return out;
    }

    private boolean scansForOrphans(String table) {
        String body = onComplete.get(StampMoveSpecs.LABEL_PREFIX + table);
        return body.contains("FROM nexus." + table + " t WHERE") && body.contains("nexus.catalog_collections cc");
    }

    @Test
    void theOrphanGuardIsAbsentOnlyWhereTheRegistryForeignKeyForbidsAnOrphan_andPresentOnEveryOtherNameIdTable() {
        var withFk = tablesWithRegistryForeignKey();
        assertThat(withFk).as("non-vacuity: pg_constraint names chunks, whose FK is the premise of the one absent guard").contains("chunks");
        assertThat(withFk).as("the premise that failed in review: taxonomy_centroids has no FK to the registry").doesNotContain("taxonomy_centroids");
        int guarded = 0;
        int unguarded = 0;
        for (var check : StampMoveSpecs.checks()) {
            if (!check.kind().equals("NAME_ID") || check.table().equals("aspect_extraction_queue")) {
                continue;
            }
            if (scansForOrphans(check.table())) {
                guarded++;
            } else {
                unguarded++;
                assertThat(withFk).as("%s has no orphan guard in its on_complete, so the database must forbid an orphan there (pg_constraint)", check.table())
                    .contains(check.table());
            }
        }
        assertThat(unguarded).as("non-vacuity: at least one table (chunks) is relieved of the scan").isPositive();
        assertThat(guarded).as("non-vacuity: the guard is on the other tables").isGreaterThanOrEqualTo(10);
    }

    /**
     * Review N1: a centroid whose collection has no registry row (name set, id NULL) is possible, because taxonomy_centroids has no FK to the
     * registry. The stamp's on_complete must then leave the CHECKs NOT VALID and complete, as for every other table without the key; a bare
     * VALIDATE raised 23514 and failed the move for good.
     */
    @Test
    void anOrphanCentroidLetsTheCentroidStampComplete_itsChecksStayNotValid() throws Exception {
        String tenant = "p31-guard-orphan-centroid";
        try {
            StampMoveSeeds.seed(ctx, "taxonomy_centroids", tenant, new StampMoveSeeds.Plan(0, true, false, false));   // one whole (stamped) centroid
            P31CheckReplay.lift(ctx, changesetFor("taxonomy_centroids"));
            try {
                ctx.insertInto(CENTROIDS, TENANT_F, CENTROID_NAME, CENTROID_ID, TOPIC_ID, LABEL_F, MODEL_F, VectorSeeds.centroidEmbedding(768))
                    .select(ctx.select(TENANT_F, DSL.inline(ORPHAN_NAME), DSL.inline((Long) null), TOPIC_ID, DSL.inline("orphan"), MODEL_F,
                            VectorSeeds.centroidEmbedding(768))
                        .from(CENTROIDS).where(TENANT_F.eq(tenant)))
                    .execute();
            } finally {
                P31CheckReplay.restore(ctx, changesetFor("taxonomy_centroids"));
            }
            var constraints = StampMoveSpecs.constraintsByTable().get("taxonomy_centroids");
            for (String constraint : constraints) {
                assertThat(PgCatalogProbes.constraintValidated(ctx, constraint)).as("non-vacuity: %s is NOT VALID before", constraint).isFalse();
            }
            assertThat(ctx.fetchCount(CENTROIDS, TENANT_F.eq(tenant), CENTROID_ID.isNull(), CENTROID_NAME.ne("")))
                .as("non-vacuity: the orphan row is there (name set, id NULL), and the registry has no row for it").isEqualTo(1);
            assertThat(ctx.fetchCount(DSL.table(DSL.name("nexus", "catalog_collections")), TENANT_F.eq(tenant), DSL.field(DSL.name("name"), String.class).eq(ORPHAN_NAME))).isZero();
            assertThatThrownBy(() -> PgContainerHelper.validateConstraint(su, CENTROIDS, constraints.get(0)))
                .as("non-vacuity: a bare VALIDATE refuses with the orphan there (what the dropped guard let the move run into)")
                .hasMessageContaining("violated by some row");

            complete(onComplete.get(StampMoveSpecs.LABEL_PREFIX + "taxonomy_centroids"));   // must not raise

            for (String constraint : constraints) {
                assertThat(PgCatalogProbes.constraintValidated(ctx, constraint)).as("%s: the VALIDATE waits while an orphan is there", constraint).isFalse();
            }
        } finally {
            StampMoveSeeds.clean(ctx, tenant);
            P31CheckReplay.lift(ctx, changesetFor("taxonomy_centroids"));
            P31CheckReplay.restore(ctx, changesetFor("taxonomy_centroids"));
        }
    }

    @Test
    void theTablesWithoutTheOrphanGuardStillValidateBothOfTheirChecks() throws Exception {
        // a table relieved of the scan (chunks) validates the name-id CHECK and the NOT NULL one without it
        var unguarded = StampMoveSpecs.checks().stream().filter(c -> c.kind().equals("NAME_ID") && !c.table().equals("aspect_extraction_queue"))
            .map(StampMoveSpecs.Check::table).distinct().filter(t -> !scansForOrphans(t)).toList();
        assertThat(unguarded).as("non-vacuity: some table is without the scan").isNotEmpty();
        for (String table : unguarded) {
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
