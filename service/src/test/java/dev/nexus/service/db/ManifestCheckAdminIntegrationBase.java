// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.vectors.ChunkConstraintAdmin;
import dev.nexus.service.vectors.PciBuilderSession;
import org.assertj.core.api.ThrowableAssert;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.TestMethodOrder;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.sql.DriverManager;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * nexus-6pfgb.42 on a real PostgreSQL: one manifest CHECK ({@link ManifestCheck}), its census and its add / validate
 * steps ({@link ManifestCheckAdmin}). One database per concrete class, ordered steps: the census is read before the
 * constraint exists; the constraint then goes on NOT VALID, refuses, and is VALIDATEd. A subclass says which check it
 * is and how a clean row, a violating row, a break and a repair look for that check; every step is the base's.
 *
 * <p>Manifest rows are written with no chunk behind them and the foreign keys off for the writing session
 * ({@code session_replication_role = replica}, the {@code VectorSensitivityExcludePlanShapeTest} precedent), so a
 * fixture row needs no collection, chunk or document. That also switches off the BEFORE INSERT trigger that fills
 * {@code chunk_index}, so a fixture names it.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
abstract class ManifestCheckAdminIntegrationBase {

    private static final String MODEL = "minilm-l6-v2-384";
    /** SQLSTATE: check_violation. */
    private static final String CHECK_VIOLATION = "23514";

    private PostgreSQLContainer<?> pg;
    private final AtomicInteger seq = new AtomicInteger();

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
    }

    @AfterAll
    void stop() {
        if (pg != null) pg.stop();
    }

    /** The check under test. */
    abstract ManifestCheck check();

    /** The whole rendered CHECK expression. */
    abstract String expectedExpression();

    /** Words the stored constraint definition must contain. */
    abstract List<String> definitionWords();

    /** The column assignments of a row that satisfies the CHECK, for the manifest row {@code position} (varied by position). */
    abstract Map<Field<?>, Object> cleanColumns(int position);

    /** The column assignments of a row the CHECK refuses, for the manifest row {@code position} (varied by position). */
    abstract Map<Field<?>, Object> violatingColumns(int position);

    /** Makes the row {@code doc} (at position 3, clean) a violator. */
    abstract void breakRow(DSLContext ctx, String doc);

    /** Makes the violator {@code doc} clean again, the fix an operator applies. */
    abstract void repairRow(DSLContext ctx, String doc);

    ManifestCheckAdmin admin() {
        return new ManifestCheckAdmin(new AdminConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword()), check());
    }

    private static byte[] chash(String text) throws Exception {
        return MessageDigest.getInstance("SHA-256").digest(text.getBytes(StandardCharsets.UTF_8));
    }

    /** One clean manifest row of {@code tenant} at {@code position}, written as the superuser with the foreign keys off. */
    private String clean(String tenant, int position) throws Exception {
        return insert(tenant, position, cleanColumns(position));
    }

    /** One violating manifest row; the fixture itself may be refused by a live CHECK ({@link DataAccessException}). */
    private String violator(String tenant, int position) throws Exception {
        return insert(tenant, position, violatingColumns(position));
    }

    String insert(String tenant, int position, Map<Field<?>, Object> columns) throws Exception {
        String doc = "doc-" + seq.incrementAndGet();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // RDR-229 P3.1: a manifest row that names a collection carries its registry id, so the tenant's row of the
            // collection exists before the replica role turns the foreign keys off
            PgContainerHelper.insertCollection(ctx, tenant, "mci-collection");
            ctx.set(DSL.name("session_replication_role"), DSL.inline("replica")).execute();
            var insert = ctx.insertInto(CATALOG_DOCUMENT_CHUNKS)
                .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, tenant)
                .set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, doc)
                .set(CATALOG_DOCUMENT_CHUNKS.POSITION, position)
                .set(CATALOG_DOCUMENT_CHUNKS.CHASH, chash(doc))
                .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, "mci-collection")
                .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID, CollectionIds.idOf(tenant, "mci-collection"))
                .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, MODEL)
                // NOT NULL, and the BEFORE INSERT default trigger is off under replica: a check that is not about
                // chunk_index leaves it equal to position, one that is overrides it below
                .set(CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX, position);
            for (var column : columns.entrySet()) {
                @SuppressWarnings("unchecked")
                Field<Object> f = (Field<Object>) column.getKey();
                insert.set(f, column.getValue());
            }
            insert.execute();
        }
        return doc;
    }

    private void breakRow(String doc) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            breakRow(DSL.using(su, SQLDialect.POSTGRES), doc);
        }
    }

    private void repairRow(String doc) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            repairRow(DSL.using(su, SQLDialect.POSTGRES), doc);
        }
    }

    void deleteAll() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).deleteFrom(CATALOG_DOCUMENT_CHUNKS).execute();
        }
    }

    void dropConstraintIfPresent() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).alterTable(CATALOG_DOCUMENT_CHUNKS)
                .dropConstraintIfExists(DSL.name(check().constraint())).execute();
        }
    }

    static void assertCheckViolation(ThrowableAssert.ThrowingCallable call) {
        assertThatThrownBy(call).isInstanceOf(DataAccessException.class)
            .satisfies(e -> assertThat(((DataAccessException) e).sqlState()).isEqualTo(CHECK_VIOLATION));
    }

    private static void assertRefused(ThrowableAssert.ThrowingCallable call, int status, String reason) {
        assertThatThrownBy(call).isInstanceOf(AdminDdlSession.Refused.class).satisfies(e -> {
            var refused = (AdminDdlSession.Refused) e;
            assertThat(refused.reason()).isEqualTo(reason);
            assertThat(refused.status()).isEqualTo(status);
        });
    }

    // ---- 0. the predicate, before any constraint exists ---------------------------------------------------------

    @Test
    @Order(0)
    void theCheckExpressionIsTheCensusPredicateRenderedOnce() {
        assertThat(check().checkExpression()).as("the whole rendered CHECK").isEqualTo(expectedExpression());
        assertThat(ManifestCheck.addStatement()).as("the expression is the template's one argument")
            .contains("%s").doesNotContain(check().constraint());
    }

    // ---- 1. the census is the CHECK's own predicate ---------------------------------------------------------

    @Test
    @Order(10)
    void theCensusCountsRowsAndViolators_perTenant_andAddsNothing() throws Exception {
        deleteAll();
        String a = "mci-a";
        String b = "mci-b";
        clean(a, 0);
        clean(a, 1);
        violator(a, 2);
        violator(a, 3);
        clean(b, 4);
        clean(b, 0);

        List<ManifestCheckAdmin.TenantCensus> census = admin().census();

        assertThat(census).extracting(ManifestCheckAdmin.TenantCensus::tenant).containsExactly(a, b);
        assertThat(census.get(0).rows()).isEqualTo(4);
        assertThat(census.get(0).violating()).isEqualTo(2);
        assertThat(census.get(1).rows()).isEqualTo(2);
        assertThat(census.get(1).violating()).isZero();
        assertThat(admin().state().present()).as("a census changes nothing").isFalse();
        deleteAll();
    }

    // ---- 2. add NOT VALID, then the live constraint -----------------------------------------------------------

    @Test
    @Order(20)
    void addPutsTheCheckOnNotValid_andARerunIsPresent() throws Exception {
        assertThat(admin().state().present()).isFalse();

        var first = admin().add();
        var second = admin().add();

        assertThat(first.action()).isEqualTo("added");
        assertThat(first.after().present()).isTrue();
        assertThat(first.after().validated()).as("added NOT VALID").isFalse();
        assertThat(first.after().definition()).contains(definitionWords()).endsWith("NOT VALID");
        assertThat(second.action()).isEqualTo("present");
    }

    @Test
    @Order(21)
    void theNotValidCheckRefusesAViolatingInsertAndUpdate_andAcceptsTheEquality() throws Exception {
        deleteAll();

        assertCheckViolation(() -> violator("mci-live", 3));
        String ok = clean("mci-live", 3);
        assertCheckViolation(() -> breakRow(ok));
        repairRow(ok);   // and an update that keeps it clean goes through

        assertThat(admin().census().get(0).rows()).isEqualTo(1);
    }

    // ---- 3. validate ------------------------------------------------------------------------------------------

    @Test
    @Order(30)
    void validateReadsConvalidatedFirst_soARerunDoesNothing() throws Exception {
        var first = admin().validate();
        var second = admin().validate();

        assertThat(first.action()).isEqualTo("validated");
        assertThat(first.after().validated()).isTrue();
        assertThat(second.action()).as("convalidated is true, so no VALIDATE is sent").isEqualTo("already_valid");
        assertThat(admin().add().action()).as("add on a valid constraint leaves it").isEqualTo("present");
    }

    @Test
    @Order(31)
    void afterValidateTheCheckStillRefusesAViolator() throws Exception {
        assertCheckViolation(() -> violator("mci-valid", 1));
        clean("mci-valid", 2);
    }

    // ---- 4. a violator: the freeze, and the refusal of VALIDATE -----------------------------------------------

    @Test
    @Order(40)
    void validateOfAConstraintWithAViolatorFailsCheckViolation_leavingItNotValid_andAViolatorCannotBeUpdated()
            throws Exception {
        deleteAll();
        dropConstraintIfPresent();
        String violator = violator("mci-freeze", 1);   // written before the constraint existed
        assertThat(admin().add().after().validated()).as("add() itself does not look for violators").isFalse();

        // DATA EFFECT of add: the violator can no longer be updated, whatever the update sets
        assertCheckViolation(() -> {
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENT_CHUNKS)
                    .set(CATALOG_DOCUMENT_CHUNKS.LINE_START, 1)
                    .where(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(violator)).execute();
            }
        });
        assertRefused(() -> admin().validate(), 409, "check_violation");
        assertThat(admin().state().validated()).as("it stays NOT VALID").isFalse();

        repairRow(violator);   // fixing the row lets VALIDATE through
        assertThat(admin().validate().action()).isEqualTo("validated");
    }

    @Test
    @Order(41)
    void validateOfAnAbsentConstraintIsReportedNotCreated() throws Exception {
        dropConstraintIfPresent();

        assertThat(admin().validate().action()).isEqualTo("absent");
        assertThat(admin().state().present()).isFalse();
    }

    // ---- 5. the run guards, through the session the constraint steps share --------------------------------------

    /** A second session that holds {@code key} as a session advisory lock until it is closed. */
    private final class Held implements AutoCloseable {
        private final Connection conn;

        Held(long key) throws Exception {
            conn = pg.createConnection("");
            conn.setAutoCommit(true);
            assertThat(DSL.using(conn, SQLDialect.POSTGRES)
                .select(DSL.function("pg_try_advisory_lock", SQLDataType.BOOLEAN, DSL.val(key)))
                .fetchOne(0, Boolean.class)).as("fixture: the lock was free").isTrue();
        }

        @Override
        public void close() throws Exception {
            conn.close();   // the session ends and its advisory locks go with it
        }
    }

    @Test
    @Order(60)
    void aRunHoldingTheRunLockRefusesAddAndValidateWith409_butNotTheCensusOrTheState() throws Exception {
        dropConstraintIfPresent();

        try (Held run = new Held(check().lockKey())) {
            assertRefused(() -> admin().add(), 409, "constraint_run_in_progress");
            assertRefused(() -> admin().validate(), 409, "constraint_run_in_progress");
            assertThat(admin().census()).as("a read takes no run lock").isNotNull();
            assertThat(admin().state().present()).isFalse();
        }

        assertThat(admin().state().present()).as("the refused add added nothing").isFalse();
    }

    @Test
    @Order(61)
    void noOtherStepsRunLockIsThisStepsLock() throws Exception {
        dropConstraintIfPresent();
        assertThat(check().lockKey()).as("distinct from every other advisory key the engine takes")
            .isNotEqualTo(ChunkConstraintAdmin.ADVISORY_LOCK_KEY)
            .isNotEqualTo(CatalogTtlCheckAdmin.ADVISORY_LOCK_KEY)
            .isNotEqualTo(ChashConstraintValidator.ADVISORY_LOCK_KEY)
            .isNotEqualTo(PciBuilderSession.PCI_BUILDER_ADVISORY_LOCK_KEY)
            .isNotEqualTo(SchemaMigrator.MIGRATION_ADVISORY_LOCK_KEY)
            .isNotEqualTo(EngineSingletonLock.LOCK_KEY);
        for (ManifestCheck other : ManifestCheck.values()) {
            if (other != check()) {
                assertThat(check().lockKey()).as("and from the other manifest check's").isNotEqualTo(other.lockKey());
            }
        }

        try (Held chunkRun = new Held(ChunkConstraintAdmin.ADVISORY_LOCK_KEY);
             Held ttlRun = new Held(CatalogTtlCheckAdmin.ADVISORY_LOCK_KEY)) {
            assertThat(admin().add().action()).as("another step's run does not hold this step up").isEqualTo("added");
        } finally {
            dropConstraintIfPresent();
        }
    }

    @Test
    @Order(62)
    void aMigrationWalkRefusesAddAndValidateWith503_butNotTheCensusOrTheState() throws Exception {
        dropConstraintIfPresent();

        try (Held walk = new Held(SchemaMigrator.MIGRATION_ADVISORY_LOCK_KEY)) {
            assertRefused(() -> admin().add(), 503, "migration_in_progress");
            assertRefused(() -> admin().validate(), 503, "migration_in_progress");
            assertThat(admin().census()).as("an unrestricted read takes no lock and needs no guard").isNotNull();
        }

        assertThat(admin().add().action()).as("once the walk is over the same call goes through").isEqualTo("added");
        dropConstraintIfPresent();
    }

    // ---- 6. the table's owner, as the managed service's nexus_admin is it ---------------------------------------

    /**
     * Roles are cluster-wide and the engine tests share one cluster, so each concrete class names its own fixture role
     * and creates it from a literal of its own file, where the role census can read it.
     */
    abstract String ownerRole();

    /** {@code PgContainerHelper.createLoginRole(su, <the literal>, OWNER_PASS, false)}. */
    abstract void createOwnerRole(Connection su) throws Exception;

    static final String OWNER_PASS = "mciowneradminpw";
    private boolean ownerRoleExists;
    private String originalOwner;

    private ManifestCheckAdmin ownerAdmin() {
        return new ManifestCheckAdmin(new AdminConnection(pg.getJdbcUrl(), ownerRole(), OWNER_PASS), check());
    }

    /** The table's owner becomes a NOSUPERUSER NOBYPASSRLS login role, as production's nexus_admin is. */
    private void ownTable() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            if (originalOwner == null) {
                originalOwner = DSL.using(su, SQLDialect.POSTGRES)
                    .select(DSL.field(DSL.name("tableowner"), String.class))
                    .from(DSL.table(DSL.name("pg_catalog", "pg_tables")))
                    .where(DSL.field(DSL.name("schemaname"), String.class).eq("nexus"))
                    .and(DSL.field(DSL.name("tablename"), String.class).eq("catalog_document_chunks"))
                    .fetchOne(0, String.class);
            }
            if (!ownerRoleExists) {
                createOwnerRole(su);
                PgContainerHelper.grantSchemaUsage(su, "nexus", ownerRole());
                PgContainerHelper.grantSchemaCreate(su, "nexus", ownerRole());
                ownerRoleExists = true;
            }
            PgContainerHelper.alterTableOwner(su, CATALOG_DOCUMENT_CHUNKS, ownerRole());
        }
    }

    private void disownTable() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.alterTableOwner(su, CATALOG_DOCUMENT_CHUNKS, originalOwner);
        }
    }

    /** Whether FORCE ROW LEVEL SECURITY is on, read as superuser from pg_class. */
    private boolean forceRowSecurity() throws Exception {
        try (Connection su = pg.createConnection("")) {
            return Boolean.TRUE.equals(DSL.using(su, SQLDialect.POSTGRES)
                .select(DSL.field(DSL.name("c", "relforcerowsecurity"), Boolean.class))
                .from(DSL.table(DSL.name("pg_catalog", "pg_class")).as("c"))
                .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n"))
                .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
                .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
                .and(DSL.field(DSL.name("c", "relname"), String.class).eq("catalog_document_chunks"))
                .fetchOne(0, Boolean.class));
        }
    }

    private int rowsTheOwnerSees() throws Exception {
        try (Connection owner = DriverManager.getConnection(pg.getJdbcUrl(), ownerRole(), OWNER_PASS)) {
            return DSL.using(owner, SQLDialect.POSTGRES).fetchCount(CATALOG_DOCUMENT_CHUNKS);
        }
    }

    @Test
    @Order(70)
    void theOwnerWithoutBypassRlsCountsEveryTenant_withForceLiftedForTheReadAndPutBack() throws Exception {
        deleteAll();
        dropConstraintIfPresent();
        String a = "mci-own-a";
        String b = "mci-own-b";
        clean(a, 0);
        violator(a, 1);
        violator(b, 0);
        clean(b, 1);
        List<ManifestCheckAdmin.TenantCensus> asSuperuser = admin().census();
        ownTable();
        try {
            assertThat(rowsTheOwnerSees()).as("fixture: FORCE hides every row from the table's owner").isZero();

            List<ManifestCheckAdmin.TenantCensus> asOwner = ownerAdmin().census();

            assertThat(asOwner).as("the owner's count is the superuser's, every tenant").isEqualTo(asSuperuser);
            assertThat(asOwner).extracting(ManifestCheckAdmin.TenantCensus::tenant).containsExactly(a, b);
            assertThat(asOwner.stream().mapToLong(ManifestCheckAdmin.TenantCensus::violating).sum()).isEqualTo(2);
            assertThat(forceRowSecurity()).as("FORCE ROW LEVEL SECURITY is back").isTrue();
            assertThat(rowsTheOwnerSees()).as("and the owner is blind again").isZero();
        } finally {
            disownTable();
        }
    }

    @Test
    @Order(71)
    void theOwnerNeedsNoRowVisibilityToAddOrValidate_andValidateFindsAViolatorItCannotRead() throws Exception {
        deleteAll();
        dropConstraintIfPresent();
        String violator = violator("mci-own-v", 1);
        ownTable();
        try {
            ManifestCheckAdmin owner = ownerAdmin();
            assertThat(rowsTheOwnerSees()).as("fixture: the owner reads no row under FORCE").isZero();

            assertThat(owner.add().action()).as("ownership is all ADD needs").isEqualTo("added");
            assertRefused(owner::validate, 409, "check_violation");
            assertThat(owner.state().validated()).as("the failed VALIDATE leaves it NOT VALID").isFalse();
            repairRow(violator);
            assertThat(owner.validate().action()).as("fixed, VALIDATE passes as the same blind owner")
                .isEqualTo("validated");
            assertThat(forceRowSecurity()).isTrue();
        } finally {
            disownTable();
            dropConstraintIfPresent();
        }
    }
}
