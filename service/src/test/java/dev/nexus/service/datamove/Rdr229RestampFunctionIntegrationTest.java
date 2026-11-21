// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.P31CheckReplay;
import dev.nexus.service.PgCatalogProbes;
import dev.nexus.service.PgContainerHelper;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.IOException;
import java.nio.file.Files;
import java.sql.Connection;
import java.time.OffsetDateTime;

/**
 * RDR-229 P3.1 (nexus-p2m1x.17), fix round A, critique S1 / code review F4: the way back from an orphan. A stamp move that
 * found an orphan row (a name with no registry row in its tenant) COMPLETES and leaves its table's CHECK NOT VALID; it does not
 * run again, so registering the missing collection did not stamp the row and the VALIDATE kept raising 23514 (the doctor's old
 * advice, "register that collection", was not true). The operator step is {@code SELECT nexus.rdr229_restamp('<table>')}, run as the
 * migrating role: it registers the table's stamp move again, as a new row (a label is a primary key) that supersedes the
 * completed one and copies its template, verification, on_complete and throttle, but only when the move is complete, a CHECK
 * of the table is NOT VALID and no orphan row remains. It answers with what it did or why it refused, and raises nothing.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229RestampFunctionIntegrationTest {

    private static final Table<?> MOVES = DSL.table(DSL.name("nexus", "data_moves"));
    private static final Table<?> DOCS = DSL.table(DSL.name("nexus", "catalog_documents"));
    private static final Table<?> REMAP = DSL.table(DSL.name("nexus", "chash_remap"));
    private static final Table<?> REGISTRY = DSL.table(DSL.name("nexus", "catalog_collections"));
    private static final String TENANT = "p31-restamp";
    private static final String LIVE = "code__p31restamp__bge-base-en-v15-768__v1";
    private static final String GHOST = "ghost-collection-p31-restamp";
    private static final String BASE = "rdr229-stamp-catalog_documents";
    private static final String DOCS_CHECK = "catalog_documents_physical_collection_id_check";

    private PostgreSQLContainer<?> pg;
    private Connection su;
    private DSLContext ctx;

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        su = pg.createConnection("");
        su.setAutoCommit(true);
        ctx = DSL.using(su, SQLDialect.POSTGRES);
    }

    @AfterAll
    void stop() throws Exception {
        if (su != null) su.close();
        if (pg != null) pg.stop();
    }

    @BeforeEach
    void clean() {
        ctx.deleteFrom(MOVES).execute();
        ctx.deleteFrom(DOCS).where(text("tenant_id").eq(TENANT)).execute();
        ctx.deleteFrom(REMAP).where(text("tenant_id").eq(TENANT)).execute();
        ctx.deleteFrom(REGISTRY).where(text("tenant_id").eq(TENANT)).execute();
        PgContainerHelper.insertCollection(ctx, TENANT, LIVE);
        // every case starts from the expand's state: the CHECKs present and NOT VALID over no row
        P31CheckReplay.lift(ctx, P31CheckReplay.CATALOG_DOCUMENTS);
        P31CheckReplay.restore(ctx, P31CheckReplay.CATALOG_DOCUMENTS);
        P31CheckReplay.lift(ctx, "vectors-068-14");
        P31CheckReplay.restore(ctx, "vectors-068-14");
    }

    private static Field<String> text(String name) {
        return DSL.field(DSL.name(name), String.class);
    }

    private static Field<OffsetDateTime> stamp(String name) {
        return DSL.field(DSL.name(name), SQLDataType.TIMESTAMPWITHTIMEZONE);
    }

    /** A registered stamp move over {@code table}, with distinctive text so a copy is recognisable. */
    private void register(String label, String table, boolean complete, String supersedes) {
        ctx.insertInto(MOVES)
            .set(text("label"), label).set(text("template"), "T-" + table).set(text("driving_schema"), "nexus")
            .set(text("driving_table"), table)
            .set(DSL.field(DSL.name("chunk_size"), Integer.class), 7)
            .set(DSL.field(DSL.name("sleep_ms"), Integer.class), 11)
            .set(DSL.field(DSL.name("statement_timeout_ms"), Integer.class), 12_345)
            .set(text("verification"), "V-" + table).set(text("on_complete"), "C-" + table).set(text("changeset_id"), "cs-" + table)
            .set(text("supersedes"), supersedes)
            .set(stamp("completed_at"), complete ? OffsetDateTime.now() : null).execute();
    }

    private String restamp(String table) {
        return ctx.select(DSL.function(DSL.name("nexus", "rdr229_restamp"), SQLDataType.VARCHAR, DSL.val(table))).fetchOne(0, String.class);
    }

    private void orphanDoc() {
        P31CheckReplay.lift(ctx, P31CheckReplay.CATALOG_DOCUMENTS);
        try {
            ctx.insertInto(DOCS).set(text("tenant_id"), TENANT).set(text("tumbler"), "1.9.1").set(text("title"), "t")
                .set(text("physical_collection"), GHOST).execute();
        } finally {
            P31CheckReplay.restore(ctx, P31CheckReplay.CATALOG_DOCUMENTS);
        }
    }

    private Record move(String label) {
        return ctx.selectFrom(MOVES).where(text("label").eq(label)).fetchOne();
    }

    @Test
    void itRefusesWhatIsNotAStampMove_inWords_andRaisesNothing() {
        assertThat(restamp("not_a_table")).startsWith("not registered:").contains("not a registered stamp move");
        assertThat(ctx.fetchCount(MOVES)).as("nothing was registered").isZero();
    }

    @Test
    void itRefusesWhileTheMoveHasNotCompleted() {
        register(BASE, "catalog_documents", false, null);
        assertThat(restamp("catalog_documents")).startsWith("not registered:").contains(BASE).contains("has not completed");
        assertThat(ctx.fetchCount(MOVES)).isEqualTo(1);
    }

    @Test
    void itRefusesWhenEveryCheckIsValidated_thereIsNothingToFinish() throws Exception {
        register(BASE, "catalog_documents", true, null);
        PgContainerHelper.validateConstraint(su, DOCS, DOCS_CHECK);
        assertThat(PgCatalogProbes.constraintValidated(ctx, DOCS_CHECK)).as("non-vacuity").isTrue();
        assertThat(restamp("catalog_documents")).startsWith("not registered:").contains("nothing to finish");
        assertThat(ctx.fetchCount(MOVES)).isEqualTo(1);
    }

    @Test
    void itRefusesWhileAnOrphanRowRemains_andNamesTheColumn_neverTheNameOrTheTenant() {
        register(BASE, "catalog_documents", true, null);
        orphanDoc();
        String answer = restamp("catalog_documents");
        assertThat(answer).startsWith("not registered:").contains("orphan").contains("physical_collection");
        assertThat(answer).doesNotContain(GHOST).doesNotContain(TENANT);
        assertThat(ctx.fetchCount(MOVES)).isEqualTo(1);
    }

    @Test
    void anOrphanOnTheSecondNameColumnOfATwoNameTableRefuses_too() {
        register("rdr229-stamp-chash_remap", "chash_remap", true, null);
        P31CheckReplay.lift(ctx, "vectors-068-14");
        try {
            ctx.insertInto(REMAP).set(text("tenant_id"), TENANT).set(text("source_collection"), LIVE).set(text("old_id"), "o")
                .set(DSL.field(DSL.name("new_chash"), byte[].class), new byte[32]).set(text("target_collection"), GHOST)
                .set(stamp("created_at"), OffsetDateTime.now()).set(text("provenance"), "test").execute();
        } finally {
            P31CheckReplay.restore(ctx, "vectors-068-14");
        }
        assertThat(restamp("chash_remap")).startsWith("not registered:").contains("target_collection");
    }

    @Test
    void onceNoOrphanRemains_itRegistersTheMoveAgain_asANewRowThatSupersedesTheCompletedOne_andChainsOnRerun() {
        register(BASE, "catalog_documents", true, null);
        orphanDoc();
        assertThat(restamp("catalog_documents")).startsWith("not registered:");
        // the operator registers the missing collection (the row is then resolvable) ...
        PgContainerHelper.insertCollection(ctx, TENANT, GHOST);

        assertThat(restamp("catalog_documents")).isEqualTo("registered: " + BASE + "-rerun-1");

        Record again = move(BASE + "-rerun-1");
        assertThat(again).as("a new row").isNotNull();
        Record base = move(BASE);
        assertThat(again.get(text("supersedes"))).as("it supersedes the move it re-runs").isEqualTo(BASE);
        for (String column : new String[] {"template", "driving_schema", "driving_table", "verification", "on_complete", "changeset_id"}) {
            assertThat(again.get(text(column))).as("copied: %s", column).isEqualTo(base.get(text(column)));
        }
        assertThat(again.get(DSL.field(DSL.name("chunk_size"), Integer.class))).isEqualTo(7);
        assertThat(again.get(DSL.field(DSL.name("sleep_ms"), Integer.class))).isEqualTo(11);
        assertThat(again.get(DSL.field(DSL.name("statement_timeout_ms"), Integer.class))).isEqualTo(12_345);
        assertThat(again.get(stamp("completed_at"))).as("open: the worker runs it").isNull();
        assertThat(again.get(stamp("failed_at"))).isNull();

        // a second call while the first re-run is open registers nothing
        assertThat(restamp("catalog_documents")).startsWith("not registered:").contains(BASE + "-rerun-1").contains("has not completed");
        assertThat(ctx.fetchCount(MOVES)).isEqualTo(2);

        // it completes with the CHECK still NOT VALID (say a new orphan arrived and left again): the next re-run chains on it
        ctx.update(MOVES).set(stamp("completed_at"), OffsetDateTime.now()).where(text("label").eq(BASE + "-rerun-1")).execute();
        assertThat(restamp("catalog_documents")).isEqualTo("registered: " + BASE + "-rerun-2");
        assertThat(move(BASE + "-rerun-2").get(text("supersedes"))).isEqualTo(BASE + "-rerun-1");
    }

    private static final String PROBE_MS = "nexus.rdr229_restamp_probe_ms";
    private static final String PROBE_BLOCKS = "nexus.rdr229_restamp_probe_blocks";

    /** A session GUC, as {@code SET} would; the function reads it with current_setting. */
    private void guc(String name, String value) {
        ctx.select(DSL.function("set_config", SQLDataType.VARCHAR, DSL.val(name), DSL.val(value), DSL.val(false))).fetch();
    }

    /**
     * Review N3. The orphan probe reads a table's rows with no index, so it carries its own time bound. (A SET statement_timeout INSIDE the
     * function would not bound it: PostgreSQL arms that timer when the top-level statement starts, and a plpgsql function is one statement.
     * So the probe reads the table in block ranges and stops between ranges once nexus.rdr229_restamp_probe_ms has passed.) On timeout the
     * operator gets text, nothing is registered, and the same call can be run again.
     */
    @Test
    void theOrphanProbeStopsAtItsBound_saysSo_andRegistersNothing() {
        register(BASE, "catalog_documents", true, null);
        guc(PROBE_MS, "0");
        try {
            String answer = restamp("catalog_documents");
            assertThat(answer).startsWith("not registered:").contains("did not finish").contains("nothing was registered").contains(PROBE_MS);
        } finally {
            guc(PROBE_MS, "60000");
        }
        assertThat(ctx.fetchCount(MOVES)).as("nothing was registered").isEqualTo(1);
        assertThat(restamp("catalog_documents")).as("the same call, within its default bound, registers").isEqualTo("registered: " + BASE + "-rerun-1");
    }

    /** The block-range reading finds an orphan in a later range, not only in the first: one block a range, rows over several blocks, the orphan last. */
    @Test
    void theOrphanProbeReadsEveryBlockRange_soAnOrphanInALaterBlockIsFound() {
        register(BASE, "catalog_documents", true, null);
        P31CheckReplay.lift(ctx, P31CheckReplay.CATALOG_DOCUMENTS);
        try {
            var insert = ctx.insertInto(DOCS, text("tenant_id"), text("tumbler"), text("title"), text("physical_collection"));
            var rows = insert.values(TENANT, "2.0", "x".repeat(200), LIVE);
            for (int n = 1; n < 400; n++) {
                rows = rows.values(TENANT, "2." + n, "x".repeat(200), LIVE);
            }
            rows.execute();
        } finally {
            P31CheckReplay.restore(ctx, P31CheckReplay.CATALOG_DOCUMENTS);
        }
        orphanDoc();   // written after the 400: a later block
        var ctid = DSL.field(DSL.name("t", "ctid")).cast(String.class);
        long lastBlock = ctx.select(ctid).from(DOCS.as("t")).fetch(ctid).stream()
            .mapToLong(c -> Long.parseLong(c.substring(1, c.indexOf(',')))).max().orElseThrow();
        assertThat(lastBlock).as("non-vacuity: the rows span several blocks, so one block a range is several ranges").isGreaterThan(3);
        guc(PROBE_BLOCKS, "1");
        try {
            assertThat(restamp("catalog_documents")).startsWith("not registered:").contains("orphan").contains("physical_collection");
        } finally {
            guc(PROBE_BLOCKS, "8192");
        }
        ctx.deleteFrom(DOCS).where(text("tenant_id").eq(TENANT)).execute();
    }

    /** Review N3: the function is the migrating role's alone. REVOKE ... FROM PUBLIC is written into its ACL and the service role cannot call it. */
    @Test
    void executeIsRevokedFromPublic_andTheServiceRoleCannotCallIt() throws Exception {
        var proc = DSL.table(DSL.name("pg_catalog", "pg_proc")).as("p");
        var acl = ctx.select(DSL.field(DSL.name("p", "proacl")).cast(String.class)).from(proc)
            .where(DSL.field(DSL.name("p", "proname"), String.class).eq("rdr229_restamp")).fetchOne(0, String.class);
        assertThat(acl).as("non-vacuity: the REVOKE wrote an ACL (a function with the default ACL has none)").isNotNull();
        assertThat(acl).as("no EXECUTE for PUBLIC (an aclitem with an empty grantee reads =X/owner)").doesNotStartWith("{=").doesNotContain(",=");
        assertThat(ctx.select(DSL.function("has_function_privilege", SQLDataType.BOOLEAN, DSL.val(PgContainerHelper.SVC_USERNAME),
                DSL.val("nexus.rdr229_restamp(text)"), DSL.val("EXECUTE"))).fetchOne(0, Boolean.class))
            .as("nexus_svc holds no EXECUTE").isFalse();
        try (Connection svc = java.sql.DriverManager.getConnection(pg.getJdbcUrl(), PgContainerHelper.SVC_USERNAME, PgContainerHelper.SVC_PASSWORD)) {
            var asSvc = DSL.using(svc, SQLDialect.POSTGRES);
            assertThat(org.assertj.core.api.Assertions.catchThrowable(() -> asSvc.select(DSL.function(DSL.name("nexus", "rdr229_restamp"),
                    SQLDataType.VARCHAR, DSL.val("catalog_documents"))).fetchOne(0, String.class)))
                .as("a call as nexus_svc is refused by the function's ACL").hasMessageContaining("permission denied for function rdr229_restamp");
        }
    }

    /** The function carries its own copy of the list (the name and id columns and the constraints per table): the TSV is the one list, this pins them equal. */
    @Test
    void theFunctionsListIsTheSameAsP31ChecksTsv() throws IOException {
        String xml = Files.readString(java.nio.file.Path.of("src", "main", "resources", "db", "changelog", "vectors-071-collection-id-restamp.xml"));
        int examined = 0;
        for (var c : StampMoveSpecs.checks()) {
            if (!c.validated()) {
                continue;   // a HISTORY column: no CHECK, no stamp
            }
            String tuple = "('" + c.table() + "', '" + c.nameColumn() + "', '" + c.idColumn() + "', '" + c.constraint() + "', '" + c.kind() + "')";
            assertThat(xml).as("rdr229_restamp lists %s", tuple).contains(tuple);
            examined++;
        }
        assertThat(examined).as("non-vacuity: every CHECK of the list").isEqualTo(20);
    }
}
