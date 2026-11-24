// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.datamove.CompletionCheck;
import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5b (nexus-p2m1x.24): brings a migrated test database to the state the stamp moves leave, so the form A changesets of
 * {@code vectors-078} (each CONTINUE-gated on the completion check's rule for a stamp label, which a fresh walk never meets) apply.
 *
 * <p>The state is the completion check's own ({@link CompletionCheck}): every row the stamp moves reach carries its collection's id
 * ({@link #stampEveryRow}), every stamp move has {@code completed_at} ({@link #setEveryStampCompletedAt}), and every CHECK of
 * {@code db/rdr229/p31-checks.tsv} is validated ({@link #validateEveryCheck}), which is what a move's {@code on_complete} does when no
 * orphan row is left. Setting {@code completed_at} alone is not that state, and the gate must not open on it.
 *
 * <p>The switch. {@code -Dnexus.test.formA.applied=true} makes {@link PgContainerHelper#applyProductSchema} (and the classes that
 * migrate in stages, through {@link #applyFormAIfRequested}) finish with {@link #completeAndApplyFormA}, so a test class written for the
 * fresh walk runs again against the form A bodies. Off by default: the default run is the fresh walk, where the fifteen are skipped.
 */
public final class Rdr229CompleteStamps {

    public static final String SWITCH = "nexus.test.formA.applied";
    private static final String MASTER_CHANGELOG = "db/changelog/db.changelog-master.xml";
    private static final String CHECKS_LIST = "db/rdr229/p31-checks.tsv";
    private static final String CONTINUE_LIST = "db/rdr229/p35b-continue-changesets.txt";
    private static final String STAMP_PREFIX = "rdr229-stamp-";
    private static final Table<?> MOVES = DSL.table(DSL.name("nexus", "data_moves"));
    private static final Field<String> MOVE_LABEL = DSL.field(DSL.name("label"), String.class);
    private static final Field<OffsetDateTime> MOVE_COMPLETED = DSL.field(DSL.name("completed_at"), OffsetDateTime.class);
    private static final Table<?> JOURNAL = DSL.table(DSL.name("public", "databasechangelog"));

    private Rdr229CompleteStamps() { /* static */ }

    /** Whether the run asked for the form A bodies ({@value #SWITCH}). */
    public static boolean formAApplied() {
        return Boolean.getBoolean(SWITCH);
    }

    /**
     * When the switch is on: {@link #completeAndApplyFormA}. Call it after the migration a staged test runs, on a superuser connection
     * to the migrated database.
     */
    public static void applyFormAIfRequested(Connection su) throws Exception {
        if (formAApplied()) {
            completeAndApplyFormA(su);
        }
    }

    /**
     * {@link #complete} the stamps, then run the master changelog again on {@code su}: the fifteen CONTINUE changesets now pass their gate
     * and apply. Asserts that they are all recorded, so a variant that applied nothing fails and does not pass vacuously.
     */
    public static void completeAndApplyFormA(Connection su) throws Exception {
        su.setAutoCommit(true);
        DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
        complete(su, ctx);
        Database db = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(su));
        // not closed: Liquibase.close() would close su, the caller's connection (PgContainerHelper.applyProductSchema does the same)
        new Liquibase(MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), db).update(new Contexts(), new LabelExpression());
        su.setAutoCommit(true);
        List<String> ids = continueList();
        Field<String> id = DSL.field(DSL.name("id"), String.class);
        assertThat(ctx.select(id).from(JOURNAL).where(id.in(ids)).fetch(id))
            .as("non-vacuity: every form A changeset of vectors-078 and vectors-084 is recorded, so its body is the one in the database")
            .containsExactlyInAnyOrderElementsOf(ids);
    }

    /**
     * Applies exactly the function definitions of vectors-084's changesets (the by-id overloads of
     * {@code nexus.taxonomy_ann_query_<dim>}), without the CONTINUE gate and without completing any stamp, for a test that latches
     * gate 3 by hand on a fresh walk and so reaches {@code TaxonomyCentroidRepository.annQuery}, which calls the by-id overload with no
     * fallback. Each changeset's own forward SQL is replayed from the changelog on the classpath
     * ({@link PgContainerHelper#replayChangeset}), never copied here; its preconditions (the CONTINUE gate) are not evaluated. Idempotent:
     * the definitions are {@code CREATE OR REPLACE}, so it is safe on a database where {@link #completeAndApplyFormA} already applied
     * them. Records nothing in {@code databasechangelog}.
     *
     * @return the changeset ids replayed (the three vectors-084 ids of the shipped list), so a list that yields none cannot pass vacuously
     */
    public static List<String> applyAnnQueryById(Connection su) throws Exception {
        List<String> ids = continueList().stream().filter(id -> id.startsWith("vectors-084-")).toList();
        assertThat(ids).as("non-vacuity: the shipped list names vectors-084's three changesets").hasSize(3);
        su.setAutoCommit(true);
        for (String id : ids) {
            PgContainerHelper.replayChangeset(su, P35bContinueChangesets.file(id), id);
        }
        return ids;
    }

    /** Stamp every row, record every move complete, validate every CHECK; asserts the completion check agrees. */
    public static void complete(Connection su, DSLContext ctx) throws Exception {
        stampEveryRow(ctx);
        setEveryStampCompletedAt(ctx);
        validateEveryCheck(su, ctx);
        assertThat(stampLabelStates(ctx).stream().filter(s -> !s.satisfied()).map(s -> s.label() + ": " + s.why()).toList())
            .as("the completion check counts every stamp label complete: every row stamped, every CHECK validated")
            .isEmpty();
    }

    /**
     * Gives every row of every table of the list the id of the registry row of its tenant and name, where it has none and the name is
     * set: the stamp moves' UPDATE, typed. A row whose name has no registry row stays as it is (an orphan).
     */
    public static void stampEveryRow(DSLContext ctx) throws IOException {
        for (String[] r : checkRows()) {
            if (!r[4].equals("NAME_ID") || !PgCatalogProbes.tableExists(ctx, "nexus", r[0])) { // aspect_extraction_queue is absent on most installs
                continue;
            }
            Table<?> t = DSL.table(DSL.name("nexus", r[0])).as("t");
            Field<String> tenant = DSL.field(DSL.name("t", "tenant_id"), String.class);
            Field<String> name = DSL.field(DSL.name("t", r[1]), String.class);
            Field<Long> id = DSL.field(DSL.name("t", r[2]), Long.class);
            var cc = CATALOG_COLLECTIONS.as("cc");
            var registryId = DSL.select(cc.COLLECTION_ID).from(cc).where(cc.TENANT_ID.eq(tenant)).and(cc.NAME.eq(name));
            ctx.update(t).set(id, registryId.asField())
                .where(name.isNotNull()).and(name.ne("")).and(id.isNull()).andExists(registryId)
                .execute();
        }
    }

    /** {@code completed_at} on every stamp move that has none, as the worker records a finished move; the count set. */
    public static int setEveryStampCompletedAt(DSLContext ctx) {
        return ctx.update(MOVES).set(MOVE_COMPLETED, OffsetDateTime.now())
            .where(MOVE_LABEL.like(STAMP_PREFIX + "%")).and(MOVE_COMPLETED.isNull()).execute();
    }

    /**
     * {@code ALTER TABLE .. VALIDATE CONSTRAINT} on every listed CHECK still NOT VALID, as {@code on_complete} does, except the named
     * ones: an orphan row makes the VALIDATE fail, so {@code on_complete} leaves that CHECK NOT VALID and the caller names it.
     */
    public static void validateEveryCheck(Connection su, DSLContext ctx, String... except) throws Exception {
        for (CompletionCheck.CheckState c : CompletionCheck.evaluate(ctx).checks()) {
            if (c.present() && !c.validated() && !Arrays.asList(except).contains(c.constraint())) {
                PgContainerHelper.validateConstraintStandalone(su, DSL.table(DSL.name("nexus", c.table())), c.constraint());
            }
        }
    }

    /** The completion check's verdict on each stamp label. */
    public static List<CompletionCheck.LabelState> stampLabelStates(DSLContext ctx) {
        return CompletionCheck.evaluate(ctx).labels().stream().filter(l -> l.label().startsWith(STAMP_PREFIX)).toList();
    }

    /** Stamp moves with no {@code completed_at}. */
    public static int incompleteStampMoves(DSLContext ctx) {
        return ctx.fetchCount(MOVES, MOVE_LABEL.like(STAMP_PREFIX + "%").and(MOVE_COMPLETED.isNull()));
    }

    /** Every row of the list: table, name column, id column, constraint, kind. */
    public static List<String[]> checkRows() throws IOException {
        List<String[]> out = new ArrayList<>();
        try (InputStream in = Rdr229CompleteStamps.class.getClassLoader().getResourceAsStream(CHECKS_LIST)) {
            assertThat(in).as("%s is on the classpath", CHECKS_LIST).isNotNull();
            BufferedReader reader = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8));
            for (String line = reader.readLine(); line != null; line = reader.readLine()) {
                if (line.isBlank() || line.startsWith("#") || line.startsWith("table\t")) {
                    continue;
                }
                out.add(line.split("\t", -1));
            }
        }
        return out;
    }

    /** The fifteen changeset ids of vectors-078 (twelve) and vectors-084 (three), the list P3.7 reads. */
    public static List<String> continueList() throws IOException {
        List<String> out = new ArrayList<>();
        try (InputStream in = Rdr229CompleteStamps.class.getClassLoader().getResourceAsStream(CONTINUE_LIST)) {
            assertThat(in).as("%s is on the classpath", CONTINUE_LIST).isNotNull();
            BufferedReader reader = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8));
            for (String line = reader.readLine(); line != null; line = reader.readLine()) {
                if (!line.isBlank() && !line.startsWith("#")) {
                    out.add(line.strip());
                }
            }
        }
        assertThat(out).as("non-vacuity: the list names the vectors-078 and vectors-084 changesets").hasSize(15);
        return out;
    }
}
