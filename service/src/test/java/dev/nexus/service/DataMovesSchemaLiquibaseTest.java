// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.jooq.nexus.tables.records.DataMovesRecord;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.jooq.DSLContext;
import org.jooq.InsertSetMoreStep;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.DriverManager;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.function.Consumer;
import java.util.stream.Collectors;

import static dev.nexus.service.jooq.nexus.Tables.DATA_MOVES;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-246 Step 4 (bead nexus-f3yqd.2.12): the shape of {@code nexus.data_moves}
 * ({@code datamoves-001-registry.xml}) on real Postgres: exact columns and defaults, every CHECK refusing a
 * bad row and accepting its edge, the supersedes unique and foreign key, the partial index, no RLS, and no
 * nexus_svc privilege. Same layout as {@link ReembedJobsSchemaLiquibaseTest}.
 *
 * <p>The structure and CHECK tests read the shared, already-migrated cluster. The grant test builds its own
 * database with a non-superuser owner and runs the changelog twice, because the runAlways
 * {@code grants-nexus-svc-1} loop re-grants DML on every owned table at each boot: a REVOKE in the changeset
 * alone would hold only until the next one. That test pins the loop's exclusion. It does NOT exercise the
 * changeset's REVOKE: on a fresh walk the default ACL ({@code ALTER DEFAULT PRIVILEGES ... TO nexus_svc}) is
 * installed by {@code grants-nexus-svc-1}, which runs after {@code data_moves} is created, so there is
 * nothing for the REVOKE to remove. {@link GrantsNexusSvcUpgradeIntegrationTest} covers it: there the default ACL
 * already exists when the table is created.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class DataMovesSchemaLiquibaseTest {

    /** column -> information_schema udt_name + is_nullable. */
    private static final Map<String, String> COLUMNS = new TreeMap<>(Map.ofEntries(
        Map.entry("label", "text NO"),
        Map.entry("template", "text NO"),
        Map.entry("driving_schema", "text NO"),
        Map.entry("driving_table", "text NO"),
        Map.entry("chunk_size", "int4 NO"),
        Map.entry("sleep_ms", "int4 NO"),
        Map.entry("statement_timeout_ms", "int4 NO"),
        Map.entry("verification", "text NO"),
        Map.entry("on_complete", "text YES"),
        Map.entry("changeset_id", "text NO"),
        Map.entry("supersedes", "text YES"),
        Map.entry("registered_at", "timestamptz NO"),
        Map.entry("failed_at", "timestamptz YES"),
        Map.entry("failure", "text YES"),
        Map.entry("completed_at", "timestamptz YES"),
        Map.entry("transient_retries", "int4 NO"),
        Map.entry("last_transient_at", "timestamptz YES"),
        Map.entry("last_transient", "text YES"),
        Map.entry("paused_at", "timestamptz YES"),
        Map.entry("paused_by", "text YES"),
        Map.entry("verify_statement_timeout_ms", "int4 NO")));

    private static final List<String> CONSTRAINTS = List.of(
        "data_moves_pk", "data_moves_label_check", "data_moves_label_nonempty_check", "data_moves_chunk_size_check", "data_moves_sleep_ms_check",
        "data_moves_statement_timeout_ms_check", "data_moves_supersedes_unique", "data_moves_supersedes_fk",
        "data_moves_supersedes_self_check", "data_moves_terminal_exclusive_check", "data_moves_failure_pair_check",
        "data_moves_transient_retries_check", "data_moves_transient_retries_stamp_check", "data_moves_transient_pair_check", "data_moves_paused_by_check",
        "data_moves_verify_statement_timeout_ms_check");

    /** Column values only: the registry stores SQL as text and these tests never run it. */
    private static final String TEMPLATE_TEXT = "the template text of a move";
    private static final String VERIFICATION_TEXT = "the verification query of a move";

    private static final List<String> DML = List.of("SELECT", "INSERT", "UPDATE", "DELETE");

    PostgreSQLContainer<?> pg;

    @BeforeAll
    void startAll() {
        pg = PgContainerHelper.start();
    }

    @AfterAll
    void stopAll() {
        if (pg != null) pg.stop();
    }

    // ── columns, defaults, constraints, the partial index ────────────────────

    @Test
    void dataMoves_hasExactColumnsDefaultsConstraintsAndOpenIndex() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);

            assertThat(columns(ctx)).as("nexus.data_moves exact column set and types").isEqualTo(COLUMNS);
            assertThat(PgCatalogProbes.columnInfo(ctx, "nexus", "data_moves", "registered_at").columnDefault())
                .as("registered_at defaults to now()").isEqualTo("now()");
            assertThat(PgCatalogProbes.columnInfo(ctx, "nexus", "data_moves", "transient_retries").columnDefault())
                .as("transient_retries defaults to 0").isEqualTo("0");
            assertThat(PgCatalogProbes.columnInfo(ctx, "nexus", "data_moves", "verify_statement_timeout_ms").columnDefault())
                .as("verify_statement_timeout_ms defaults to 60 s (datamoves-002)").isEqualTo("60000");


            Map<String, String> constraints = constraints(ctx);
            assertThat(constraints).containsOnlyKeys(CONSTRAINTS);
            assertThat(constraints.get("data_moves_pk")).isEqualTo("PRIMARY KEY (label)");
            assertThat(constraints.get("data_moves_supersedes_unique")).isEqualTo("UNIQUE (supersedes)");
            assertThat(constraints.get("data_moves_supersedes_fk"))
                .contains("FOREIGN KEY (supersedes)").contains("data_moves(label)");

            String open = PgCatalogProbes.indexDef(ctx, "nexus", "data_moves_open_registered_at");
            assertThat(open).as("the worker's open-move scan").contains("INDEX data_moves_open_registered_at")
                .contains("(registered_at)").contains("completed_at IS NULL").contains("failed_at IS NULL")
                .doesNotContain("UNIQUE").doesNotContain("paused_at");
        }
    }

    @Test
    void aMinimalRow_takesTheDefaults() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            insert(ctx, "defaults-move");
            DataMovesRecord row = ctx.selectFrom(DATA_MOVES).where(DATA_MOVES.LABEL.eq("defaults-move")).fetchOne();
            assertThat(row).isNotNull();
            assertThat(row.getRegisteredAt()).as("registered_at default").isNotNull();
            assertThat(row.getTransientRetries()).as("transient_retries default").isZero();
            assertThat(row.getOnComplete()).isNull();
            assertThat(row.getSupersedes()).isNull();
            assertThat(row.getFailedAt()).isNull();
            assertThat(row.getFailure()).isNull();
            assertThat(row.getCompletedAt()).isNull();
            assertThat(row.getLastTransientAt()).isNull();
            assertThat(row.getLastTransient()).isNull();
            assertThat(row.getPausedAt()).isNull();
            assertThat(row.getPausedBy()).isNull();
        }
    }

    // ── each CHECK refuses a bad row and accepts its edge ────────────────────

    @Test
    void labelCheck_refusesASlash() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThatThrownBy(() -> insert(ctx, "a/b"))
                .as("a '/' would make one move's runs read as another's")
                .hasMessageContaining("data_moves_label_check");
            assertThatThrownBy(() -> insert(ctx, "/lead")).hasMessageContaining("data_moves_label_check");
            assertThatThrownBy(() -> insert(ctx, "trail/")).hasMessageContaining("data_moves_label_check");
            assertThatThrownBy(() -> insert(ctx, ""))
                .as("an empty label names no move").hasMessageContaining("data_moves_label_nonempty_check");
            insert(ctx, "label.with-dots_and:colons");
        }
    }

    @Test
    void chunkSizeCheck_boundsAtBothEdges() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (int bad : new int[] {0, -1, 1_000_001, Integer.MAX_VALUE}) {
                assertThatThrownBy(() -> insert(ctx, "chunk-bad-" + bad, m -> m.set(DATA_MOVES.CHUNK_SIZE, bad)))
                    .as("chunk_size " + bad).hasMessageContaining("data_moves_chunk_size_check");
            }
            insert(ctx, "chunk-low", m -> m.set(DATA_MOVES.CHUNK_SIZE, 1));
            insert(ctx, "chunk-high", m -> m.set(DATA_MOVES.CHUNK_SIZE, 1_000_000));
        }
    }

    @Test
    void sleepMsCheck_boundsAtBothEdges() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (int bad : new int[] {-1, 60_001, Integer.MAX_VALUE}) {
                assertThatThrownBy(() -> insert(ctx, "sleep-bad-" + bad, m -> m.set(DATA_MOVES.SLEEP_MS, bad)))
                    .as("sleep_ms " + bad).hasMessageContaining("data_moves_sleep_ms_check");
            }
            insert(ctx, "sleep-low", m -> m.set(DATA_MOVES.SLEEP_MS, 0));
            insert(ctx, "sleep-high", m -> m.set(DATA_MOVES.SLEEP_MS, 60_000));
        }
    }

    @Test
    void verifyStatementTimeoutCheck_refusesZeroAndNegative_andAMinimalRowTakesSixtySeconds() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (int bad : new int[] {0, -1}) {
                assertThatThrownBy(() -> insert(ctx, "verify-timeout-bad-" + bad,
                        m -> m.set(DATA_MOVES.VERIFY_STATEMENT_TIMEOUT_MS, bad)))
                    .as("verify_statement_timeout_ms " + bad)
                    .hasMessageContaining("data_moves_verify_statement_timeout_ms_check");
            }
            insert(ctx, "verify-timeout-one", m -> m.set(DATA_MOVES.VERIFY_STATEMENT_TIMEOUT_MS, 1));
            insert(ctx, "verify-timeout-default");
            assertThat(ctx.select(DATA_MOVES.VERIFY_STATEMENT_TIMEOUT_MS).from(DATA_MOVES)
                .where(DATA_MOVES.LABEL.eq("verify-timeout-default")).fetchOne(DATA_MOVES.VERIFY_STATEMENT_TIMEOUT_MS))
                .as("a registering INSERT that names no such column").isEqualTo(60_000);
            assertThat(ctx.select(DATA_MOVES.STATEMENT_TIMEOUT_MS).from(DATA_MOVES)
                .where(DATA_MOVES.LABEL.eq("verify-timeout-default")).fetchOne(DATA_MOVES.STATEMENT_TIMEOUT_MS))
                .as("the completion's bound is its own column, unchanged").isEqualTo(30_000);
        }
    }

    @Test
    void statementTimeoutCheck_refusesZeroAndNegative() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (int bad : new int[] {0, -1}) {
                assertThatThrownBy(() -> insert(ctx, "timeout-bad-" + bad,
                        m -> m.set(DATA_MOVES.STATEMENT_TIMEOUT_MS, bad)))
                    .as("statement_timeout_ms " + bad).hasMessageContaining("data_moves_statement_timeout_ms_check");
            }
            insert(ctx, "timeout-one", m -> m.set(DATA_MOVES.STATEMENT_TIMEOUT_MS, 1));
        }
    }

    @Test
    void verificationIsNotNull() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThatThrownBy(() -> insert(ctx, "no-verification", m -> m.set(DATA_MOVES.VERIFICATION, (String) null)))
                .as("a move with no verification query is refused")
                .hasMessageContaining("verification").hasMessageContaining("not-null");
        }
    }

    @Test
    void failedAndCompleted_areNotBothSet_andFailedAtPairsWithFailure() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            OffsetDateTime now = OffsetDateTime.now();
            assertThatThrownBy(() -> insert(ctx, "both-terminal", m -> m
                    .set(DATA_MOVES.FAILED_AT, now).set(DATA_MOVES.FAILURE, "boom").set(DATA_MOVES.COMPLETED_AT, now)))
                .as("failed and completed together").hasMessageContaining("data_moves_terminal_exclusive_check");
            assertThatThrownBy(() -> insert(ctx, "failed-no-text", m -> m.set(DATA_MOVES.FAILED_AT, now)))
                .as("failed_at without failure").hasMessageContaining("data_moves_failure_pair_check");
            assertThatThrownBy(() -> insert(ctx, "text-no-failed", m -> m.set(DATA_MOVES.FAILURE, "boom")))
                .as("failure without failed_at").hasMessageContaining("data_moves_failure_pair_check");

            insert(ctx, "failed-ok", m -> m.set(DATA_MOVES.FAILED_AT, now).set(DATA_MOVES.FAILURE, "boom"));
            insert(ctx, "completed-ok", m -> m.set(DATA_MOVES.COMPLETED_AT, now));
        }
    }

    @Test
    void supersedes_refusesSelf_unknownLabel_andASecondReplacement() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            insert(ctx, "sup-base");
            assertThatThrownBy(() -> insert(ctx, "sup-self", m -> m.set(DATA_MOVES.SUPERSEDES, "sup-self")))
                .as("a move cannot supersede itself").hasMessageContaining("data_moves_supersedes_self_check");
            assertThatThrownBy(() -> insert(ctx, "sup-orphan", m -> m.set(DATA_MOVES.SUPERSEDES, "no-such-move")))
                .as("supersedes names a registered move").hasMessageContaining("data_moves_supersedes_fk");

            insert(ctx, "sup-next", m -> m.set(DATA_MOVES.SUPERSEDES, "sup-base"));
            assertThatThrownBy(() -> insert(ctx, "sup-rival", m -> m.set(DATA_MOVES.SUPERSEDES, "sup-base")))
                .as("one move is replaced at most once").hasMessageContaining("data_moves_supersedes_unique");
            // NULLs never collide: any number of moves supersede nothing (sup-base and sup-next already do).
            insert(ctx, "sup-none-a");
            insert(ctx, "sup-none-b");
            // a chain is fine: the replacement can itself be replaced
            insert(ctx, "sup-third", m -> m.set(DATA_MOVES.SUPERSEDES, "sup-next"));
            assertThatThrownBy(() -> insert(ctx, "sup-base", m -> { }))
                .as("label is the primary key").hasMessageContaining("data_moves_pk");
        }
    }

    @Test
    void transientRetriesCheck_refusesNegative() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            OffsetDateTime now = OffsetDateTime.now();
            // stamped, so the stamp check passes and only the sign is wrong
            assertThatThrownBy(() -> insert(ctx, "retries-neg", m -> m.set(DATA_MOVES.TRANSIENT_RETRIES, -1)
                    .set(DATA_MOVES.LAST_TRANSIENT_AT, now).set(DATA_MOVES.LAST_TRANSIENT, "no free slot")))
                .hasMessageContaining("data_moves_transient_retries_check");
            insert(ctx, "retries-zero", m -> m.set(DATA_MOVES.TRANSIENT_RETRIES, 0));
            insert(ctx, "retries-five", m -> m.set(DATA_MOVES.TRANSIENT_RETRIES, 5)
                .set(DATA_MOVES.LAST_TRANSIENT_AT, now).set(DATA_MOVES.LAST_TRANSIENT, "no free slot"));
        }
    }

    @Test
    void transientRetries_zeroMeansNoTimestamp_positiveMeansOne() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            OffsetDateTime now = OffsetDateTime.now();
            assertThatThrownBy(() -> insert(ctx, "stamp-zero-with-time", m -> m
                    .set(DATA_MOVES.LAST_TRANSIENT_AT, now).set(DATA_MOVES.LAST_TRANSIENT, "no free slot")))
                .as("zero retries with a timestamp").hasMessageContaining("data_moves_transient_retries_stamp_check");
            assertThatThrownBy(() -> insert(ctx, "stamp-one-no-time", m -> m.set(DATA_MOVES.TRANSIENT_RETRIES, 1)))
                .as("a positive count with no timestamp").hasMessageContaining("data_moves_transient_retries_stamp_check");
            insert(ctx, "stamp-zero-none");
            insert(ctx, "stamp-two-both", m -> m.set(DATA_MOVES.TRANSIENT_RETRIES, 2)
                .set(DATA_MOVES.LAST_TRANSIENT_AT, now).set(DATA_MOVES.LAST_TRANSIENT, "no free slot"));

            // the counter and the stamp move together in one UPDATE, and reset together
            assertThat(ctx.update(DATA_MOVES).set(DATA_MOVES.TRANSIENT_RETRIES, 3)
                .set(DATA_MOVES.LAST_TRANSIENT_AT, now).set(DATA_MOVES.LAST_TRANSIENT, "again")
                .where(DATA_MOVES.LABEL.eq("stamp-two-both")).execute()).isEqualTo(1);
            assertThat(ctx.update(DATA_MOVES).set(DATA_MOVES.TRANSIENT_RETRIES, 0)
                .setNull(DATA_MOVES.LAST_TRANSIENT_AT).setNull(DATA_MOVES.LAST_TRANSIENT)
                .where(DATA_MOVES.LABEL.eq("stamp-two-both")).execute()).isEqualTo(1);
        }
    }

    @Test
    void lastTransientAt_pairsWithLastTransient() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            OffsetDateTime now = OffsetDateTime.now();
            // retries 1 keeps the stamp check satisfied for the timestamp-only row, so the pair check is what refuses
            assertThatThrownBy(() -> insert(ctx, "transient-at-only", m -> m
                    .set(DATA_MOVES.TRANSIENT_RETRIES, 1).set(DATA_MOVES.LAST_TRANSIENT_AT, now)))
                .hasMessageContaining("data_moves_transient_pair_check");
            assertThatThrownBy(() -> insert(ctx, "transient-text-only",
                    m -> m.set(DATA_MOVES.LAST_TRANSIENT, "no free slot")))
                .hasMessageContaining("data_moves_transient_pair_check");
            insert(ctx, "transient-both", m -> m.set(DATA_MOVES.TRANSIENT_RETRIES, 1)
                .set(DATA_MOVES.LAST_TRANSIENT_AT, now).set(DATA_MOVES.LAST_TRANSIENT, "no free slot"));
        }
    }

    @Test
    void pausedBy_needsPausedAt() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            OffsetDateTime now = OffsetDateTime.now();
            assertThatThrownBy(() -> insert(ctx, "paused-by-only", m -> m.set(DATA_MOVES.PAUSED_BY, "sam")))
                .hasMessageContaining("data_moves_paused_by_check");
            insert(ctx, "paused-at-only", m -> m.set(DATA_MOVES.PAUSED_AT, now));
            insert(ctx, "paused-both", m -> m.set(DATA_MOVES.PAUSED_AT, now).set(DATA_MOVES.PAUSED_BY, "sam"));

            // an operator's pause is a plain UPDATE of an existing row, and so is lifting it
            insert(ctx, "paused-later");
            assertThat(ctx.update(DATA_MOVES).set(DATA_MOVES.PAUSED_AT, now).set(DATA_MOVES.PAUSED_BY, "sam")
                .where(DATA_MOVES.LABEL.eq("paused-later")).execute()).isEqualTo(1);
            assertThatThrownBy(() -> ctx.update(DATA_MOVES).setNull(DATA_MOVES.PAUSED_AT)
                    .where(DATA_MOVES.LABEL.eq("paused-later")).execute())
                .as("lifting the pause clears paused_by too").hasMessageContaining("data_moves_paused_by_check");
            assertThat(ctx.update(DATA_MOVES).setNull(DATA_MOVES.PAUSED_AT).setNull(DATA_MOVES.PAUSED_BY)
                .where(DATA_MOVES.LABEL.eq("paused-later")).execute()).isEqualTo(1);
        }
    }

    // ── access: admin only, no RLS ───────────────────────────────────────────

    @Test
    void dataMoves_hasNoRls_andNexusSvcHoldsNoPrivilegeOnTheSharedCluster() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgCatalogProbes.RowSecurity rls = PgCatalogProbes.rowSecurity(ctx, "nexus", "data_moves");
            assertThat(rls).as("nexus.data_moves must exist in pg_class").isNotNull();
            assertThat(rls.enabled()).as("global table: no RLS ENABLE").isFalse();
            assertThat(rls.forced()).as("global table: no RLS FORCE").isFalse();
            assertThat(PgCatalogProbes.policies(ctx, "nexus", "data_moves")).as("no policy").isEmpty();
            assertThat(PgCatalogProbes.columnExists(ctx, "nexus", "data_moves", "tenant_id"))
                .as("no tenant column").isFalse();
            for (String priv : DML) {
                assertThat(hasTablePriv(ctx, "nexus_svc", "nexus.data_moves", priv))
                    .as("nexus_svc holds no " + priv + " on nexus.data_moves").isFalse();
            }
            assertThat(hasTablePriv(ctx, "nexus_svc", "nexus.chunks", "INSERT"))
                .as("non-vacuity: the grants loop still grants nexus_svc a sibling table").isTrue();
        }
    }

    /**
     * The production shape: a non-superuser owner runs the full changelog, then runs it again. The runAlways
     * {@code grants-nexus-svc-1} loop grants DML on every table the owner owns at each boot, so its exclusion is
     * what keeps {@code nexus_svc} out on the second boot (and the first, where the loop already ran after the
     * table was created). The changeset's own REVOKE is not tested here; see the class comment.
     */
    @Test
    void nexusSvcStaysWithoutPrivilege_afterTheChangelogRunsAgainAsTheOwner() throws Exception {
        String admin = "nexus_admin_datamoves";
        String pass = "nexus_admin_datamoves_pw";
        try (var empty = PgContainerHelper.startEmptyDatabase();
             Connection su = empty.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.bootstrapNonSuperuserOwner(su, admin, pass);
            liquibaseUpdate(empty.getJdbcUrl(), admin, pass);
            DSLContext sctx = DSL.using(su, SQLDialect.POSTGRES);

            assertThat(PgCatalogProbes.relationOwner(sctx, "nexus", "data_moves"))
                .as("the migrating role owns the registry").isEqualTo(admin);
            for (String priv : DML) {
                assertThat(hasTablePriv(sctx, "nexus_svc", "nexus.data_moves", priv))
                    .as("first boot: nexus_svc " + priv).isFalse();
            }

            // Between the boots, take a sibling grant away: only the grants loop running again can give it back,
            // so seeing it return proves the loop ran while data_moves stayed excluded.
            sctx.revoke(DSL.privilege("insert")).on(DSL.table(DSL.name("nexus", "reembed_jobs")))
                .from(DSL.user(DSL.name("nexus_svc"))).execute();
            assertThat(hasTablePriv(sctx, "nexus_svc", "nexus.reembed_jobs", "INSERT"))
                .as("the sibling grant is gone before the second boot").isFalse();

            liquibaseUpdate(empty.getJdbcUrl(), admin, pass);
            assertThat(hasTablePriv(sctx, "nexus_svc", "nexus.reembed_jobs", "INSERT"))
                .as("non-vacuity: the second boot's grants loop ran and restored the sibling grant").isTrue();
            for (String priv : DML) {
                assertThat(hasTablePriv(sctx, "nexus_svc", "nexus.data_moves", priv))
                    .as("second boot (runAlways grants loop): nexus_svc " + priv).isFalse();
            }

            try (Connection as = DriverManager.getConnection(empty.getJdbcUrl(), admin, pass)) {
                DSLContext actx = DSL.using(as, SQLDialect.POSTGRES);
                insert(actx, "admin-move");
                assertThat(actx.fetchCount(DATA_MOVES, DATA_MOVES.LABEL.eq("admin-move")))
                    .as("nexus_admin reads and writes the registry (the walk itself registered the first real move)").isEqualTo(1);
            }
        }
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private static void liquibaseUpdate(String url, String user, String pass) throws Exception {
        try (Connection conn = DriverManager.getConnection(url, user, pass)) {
            new Liquibase("db/changelog/db.changelog-master.xml", new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(conn)))
                .update(new Contexts());
        }
    }

    private static boolean hasTablePriv(DSLContext ctx, String role, String relation, String priv) {
        var has = DSL.function("has_table_privilege", SQLDataType.BOOLEAN,
            DSL.val(role), DSL.val(relation), DSL.val(priv));
        return Boolean.TRUE.equals(ctx.select(has).fetchOne(has));
    }

    private static Map<String, String> columns(DSLContext ctx) {
        Map<String, String> actual = new TreeMap<>();
        for (String col : PgCatalogProbes.columnNames(ctx, "nexus", "data_moves")) {
            PgCatalogProbes.ColumnInfo info = PgCatalogProbes.columnInfo(ctx, "nexus", "data_moves", col);
            actual.put(col, info.udtName() + " " + info.isNullable());
        }
        return actual;
    }

    private static Map<String, String> constraints(DSLContext ctx) {
        return PgCatalogProbes.constraintDefinitionsIn(ctx, List.of("nexus")).stream()
            .filter(c -> c.table().equals("data_moves"))
            .collect(Collectors.toMap(
                PgCatalogProbes.ConstraintDefinition::conname,
                PgCatalogProbes.ConstraintDefinition::definition));
    }

    private static int insert(DSLContext ctx, String label) {
        return insert(ctx, label, m -> { });
    }

    /** A valid row, then {@code more} overrides or adds columns. */
    private static int insert(DSLContext ctx, String label, Consumer<InsertSetMoreStep<DataMovesRecord>> more) {
        var step = ctx.insertInto(DATA_MOVES)
            .set(DATA_MOVES.LABEL, label)
            .set(DATA_MOVES.TEMPLATE, TEMPLATE_TEXT)
            .set(DATA_MOVES.DRIVING_SCHEMA, "nexus")
            .set(DATA_MOVES.DRIVING_TABLE, "t")
            .set(DATA_MOVES.CHUNK_SIZE, 1000)
            .set(DATA_MOVES.SLEEP_MS, 50)
            .set(DATA_MOVES.STATEMENT_TIMEOUT_MS, 30000)
            .set(DATA_MOVES.VERIFICATION, VERIFICATION_TEXT)
            .set(DATA_MOVES.CHANGESET_ID, "test-1");
        more.accept(step);
        return step.execute();
    }
}
