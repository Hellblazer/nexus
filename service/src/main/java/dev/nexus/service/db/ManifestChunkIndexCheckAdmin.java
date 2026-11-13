// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;

import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.ArrayList;
import java.util.List;
import java.util.Objects;

/**
 * The manifest chunk-index CHECK of RDR-236 (nexus-6pfgb.42): {@code nexus.catalog_document_chunks} may not hold a row
 * whose {@code chunk_index} differs from its {@code position}. Its census, its add-NOT-VALID and its VALIDATE, run on
 * the engine's admin connection ({@link AdminDdlSession}), as the catalog TTL CHECK ({@link CatalogTtlCheckAdmin})
 * does, and for the same reason it is never a Liquibase changeset: a boot-time VALIDATE crash-loops an install whose
 * rows differ from the measured one, and no release rewrites user data at engine boot. Nothing in the engine calls it:
 * the routes ({@code ManifestChunkIndexCheckHandler}) are reached by an operator's request.
 *
 * <p><b>Why the equality holds.</b> {@code chunk_index} is NOT NULL since hygiene-001, and a BEFORE INSERT trigger
 * ({@code trg_catalog_document_chunks_chunk_index_default}) fills it from {@code position} when a writer omits it. No
 * writer in this repository sets the two apart (audit on nexus-6pfgb.42), so the census is expected to read zero; the
 * engine stores a caller's {@code chunk_index} as sent, so the CHECK is what stops a later writer. It changes neither column.
 *
 * <p><b>The predicate</b> ({@link #chunkIndexEqualsPosition}) is one {@link Condition}, built once. The CHECK is its
 * rendering ({@link #checkExpression}); the census counts the rows for which it is FALSE ({@link #violation}). One
 * object, so they cannot disagree. Both columns are NOT NULL, so the predicate is never NULL (a NULL would pass the
 * CHECK).
 *
 * <p><b>Guards.</b> As the TTL step: the migration walk's lock is read once before the first statement,
 * {@code lock_timeout} is 5 s, and one run holds {@link #ADVISORY_LOCK_KEY} at a time (distinct from the chunk step's
 * and the TTL step's keys, so the three can run side by side).
 *
 * <p><b>All tenants.</b> {@code nexus.catalog_document_chunks} is FORCE row-level-security with one policy keyed to
 * the tenant stamp and no policy that lets an owner read every row. The census therefore reads through
 * {@link AdminDdlSession#readEveryTenant}: a superuser or BYPASSRLS session reads as it is; the table's owner (the
 * production {@code nexus_admin}) reads with FORCE lifted for one transaction that is rolled back, under an ACCESS
 * EXCLUSIVE lock on the table for the scan, bounded at {@link #OWNER_CENSUS_STATEMENT_TIMEOUT}; any other role is
 * refused {@code admin_cannot_see_all_tenants}. The handler adds the scoped-count comparison.
 *
 * <p><b>What each step needs.</b> The census: the all-tenant view. ADD NOT VALID: ownership of the table, and no scan.
 * VALIDATE: ownership and no row visibility (the constraint check is not row filtered).
 *
 * <p><b>The census-to-ADD window.</b> The routes count, then add, with no lock between. A NOT VALID CHECK is evaluated
 * on every new row version, so a row that breaks it could no longer be updated until fixed; the add route counts again
 * after the ADD and reports {@code violating_after_add}. No in-repository writer sets the columns apart, so the window is theory.
 */
public class ManifestChunkIndexCheckAdmin {

    private static final Logger log = LoggerFactory.getLogger(ManifestChunkIndexCheckAdmin.class);

    /** The session advisory lock a run holds: the bytes of {@code "mfxidxck"} as a bigint. */
    public static final long ADVISORY_LOCK_KEY = 0x6d6678696478636bL;

    public static final String CONSTRAINT = "catalog_document_chunks_chunk_index_position_check";

    private static final String ADD_STATEMENT_TIMEOUT = "60s";
    private static final String VALIDATE_STATEMENT_TIMEOUT = "30min";
    private static final String CENSUS_STATEMENT_TIMEOUT = "10min";
    /**
     * The census as the table's owner (FORCE lifted) holds ACCESS EXCLUSIVE on the table for the scan: every manifest
     * read and write waits behind it. A minute is the bound; past it the census stops and changes nothing.
     */
    static final String OWNER_CENSUS_STATEMENT_TIMEOUT = "60s";
    private static final String OWNER_POINT_AT = "the role that owns nexus.catalog_document_chunks (the schema owner, "
        + "nexus_admin), or a superuser or BYPASSRLS role";

    private static final AdminDdlSession.Settings SETTINGS = new AdminDdlSession.Settings(
        "nexus-manifest-chunk-index-check", ADVISORY_LOCK_KEY, "manifest-chunk-index-check",
        CATALOG_DOCUMENT_CHUNKS.getSchema().getName(), CATALOG_DOCUMENT_CHUNKS.getName(), "census");

    /** The constraint's state, as {@code pg_constraint} holds it. */
    public record Action(String action, AdminDdlSession.State after) {}

    /** One tenant's standing in the table-wide census. */
    public record TenantCensus(String tenant, long rows, long violating) {}

    private final AdminConnection admin;

    public ManifestChunkIndexCheckAdmin(AdminConnection admin) {
        this.admin = Objects.requireNonNull(admin, "admin");
    }

    // ---- the predicate -------------------------------------------------------------------------------------------

    private static final Field<Integer> CHUNK_INDEX = DSL.field(DSL.name("chunk_index"), SQLDataType.INTEGER);
    private static final Field<Integer> POSITION = DSL.field(DSL.name("position"), SQLDataType.INTEGER);

    /** The CHECK: {@code chunk_index = position}. Unqualified names, so the stored definition reads as a column check. */
    static Condition chunkIndexEqualsPosition() {
        return CHUNK_INDEX.eq(POSITION);
    }

    /** The rows the CHECK refuses: the predicate is FALSE (both columns are NOT NULL, so this is its plain negation). */
    static Condition violation() {
        return CHUNK_INDEX.ne(POSITION);
    }

    /** The CHECK expression as the server is sent it: the predicate, inlined. */
    static String checkExpression() {
        return DSL.using(SQLDialect.POSTGRES).renderInlined(chunkIndexEqualsPosition());
    }

    /** The {@code format()} template of the ADD; the expression is its one {@code %s}, never concatenated. */
    static String addStatement() {
        return "ALTER TABLE %I.%I ADD CONSTRAINT %I CHECK (%s) NOT VALID";
    }

    // ---- public --------------------------------------------------------------------------------------------------

    /** The constraint's state, read on the admin connection (absent: {@code present} false). */
    public AdminDdlSession.State state() {
        try (AdminDdlSession s = AdminDdlSession.open(admin, SETTINGS, false)) {
            return s.states(List.of(CONSTRAINT)).get(0);
        }
    }

    /**
     * The table-wide census: every tenant's manifest rows and the rows the CHECK would refuse, read in one pass on the
     * admin session. Refuses ({@link AdminDdlSession.Refused} 503, {@code admin_cannot_see_all_tenants}) unless the
     * session reads every row ({@link AdminDdlSession#readEveryTenant}, which says what that costs). Tenants with no
     * rows do not appear.
     */
    public List<TenantCensus> census() {
        try (AdminDdlSession s = AdminDdlSession.open(admin, SETTINGS, false)) {
            s.setSession("statement_timeout", CENSUS_STATEMENT_TIMEOUT);
            return s.readEveryTenant(OWNER_POINT_AT, OWNER_CENSUS_STATEMENT_TIMEOUT,
                ManifestChunkIndexCheckAdmin::censusOn);
        }
    }

    private static List<TenantCensus> censusOn(DSLContext ctx) {
        var rows = ctx.select(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, DSL.count(), DSL.count().filterWhere(violation()))
            .from(CATALOG_DOCUMENT_CHUNKS)
            .groupBy(CATALOG_DOCUMENT_CHUNKS.TENANT_ID)
            .orderBy(CATALOG_DOCUMENT_CHUNKS.TENANT_ID)
            .fetch();
        List<TenantCensus> out = new ArrayList<>();
        for (var r : rows) {
            out.add(new TenantCensus(r.get(0, String.class), r.get(1, Long.class), r.get(2, Long.class)));
        }
        return out;
    }

    /**
     * Add the CHECK NOT VALID. A constraint that exists is left alone ({@code present}). No row is changed, but a NOT
     * VALID CHECK is evaluated against every new row version: a row that already breaks it could no longer be UPDATEd
     * (SQLSTATE 23514) until it is fixed. The caller ({@code ManifestChunkIndexCheckHandler}) refuses the add while the
     * census reads a violator anywhere.
     */
    public Action add() {
        try (AdminDdlSession s = AdminDdlSession.open(admin, SETTINGS, true)) {
            s.guard(ADD_STATEMENT_TIMEOUT);
            AdminDdlSession.State before = s.states(List.of(CONSTRAINT)).get(0);
            if (before.present()) {
                return new Action("present", before);
            }
            s.ddl(addStatement(), List.of(CATALOG_DOCUMENT_CHUNKS.getSchema().getName(),
                CATALOG_DOCUMENT_CHUNKS.getName(), CONSTRAINT, checkExpression()));
            AdminDdlSession.State after = s.states(List.of(CONSTRAINT)).get(0);
            log.info("event=manifest_chunk_index_check_added validated={}", after.validated());
            return new Action("added", after);
        }
    }

    /**
     * VALIDATE the CHECK if it exists and is not yet valid. {@code convalidated} is read first, so a rerun does
     * nothing and takes no scan; an absent constraint is reported, not created. DATA EFFECT: none; it reads the table
     * under SHARE UPDATE EXCLUSIVE, which lets writers through, and fails ({@code check_violation}, 409) leaving the
     * constraint NOT VALID if a row breaks it.
     */
    public Action validate() {
        try (AdminDdlSession s = AdminDdlSession.open(admin, SETTINGS, true)) {
            s.guard(VALIDATE_STATEMENT_TIMEOUT);
            AdminDdlSession.State before = s.states(List.of(CONSTRAINT)).get(0);
            if (!before.present()) {
                return new Action("absent", before);
            }
            if (Boolean.TRUE.equals(before.validated())) {
                return new Action("already_valid", before);
            }
            s.ddl("ALTER TABLE %I.%I VALIDATE CONSTRAINT %I", List.of(CATALOG_DOCUMENT_CHUNKS.getSchema().getName(),
                CATALOG_DOCUMENT_CHUNKS.getName(), CONSTRAINT));
            AdminDdlSession.State after = s.states(List.of(CONSTRAINT)).get(0);
            log.info("event=manifest_chunk_index_check_validated validated={}", after.validated());
            return new Action("validated", after);
        }
    }
}
