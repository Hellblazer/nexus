// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;

import org.jooq.Condition;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.List;
import java.util.Objects;

/**
 * The catalog TTL CHECK of RDR-236 (nexus-6pfgb.44): {@code nexus.catalog_documents} may not hold an entry whose
 * {@code metadata} carries a note TTL and no {@code put_at} stamp. Its census, its add-NOT-VALID and its VALIDATE, run
 * on the engine's admin connection ({@link AdminDdlSession}). Never a Liquibase changeset: a boot-time VALIDATE
 * crash-loops an install whose rows differ from the measured one, and a NOT VALID CHECK is evaluated on every row that
 * is UPDATEd, so it goes in only when an operator sends it, after the Phase 4 Step 2c paired cut and after the
 * backfill has stamped the entries the census counts. This is its own step, not the chunk-table routes of
 * nexus-6pfgb.43 (those carry {@code nexus.chunks} constraints only). Nothing in the engine
 * calls it: the routes ({@code CatalogTtlCheckHandler}) are reached by an operator's request.
 *
 * <p><b>The predicate</b> ({@link #ttlRequiresPutAt}) is one {@link Condition}, built once. The CHECK is its
 * rendering ({@link #checkExpression}); the census counts the rows for which it is FALSE ({@link #violation}). They
 * cannot disagree, because there is one object.
 *
 * <p><b>What counts as a TTL.</b> Exactly what {@code nexus.catalog.note_ttl.note_ttl_days} reads as more than zero,
 * so the CHECK says "every entry the expiry clock would run on has a clock start of its own":
 * <ul>
 *   <li>a JSON number from 1 up to (not including) 1000000000: Python truncates toward zero, so 0 and 0.5 are
 *       permanent, and a TTL past {@code timedelta.max.days} (999999999) is permanent too, as is anything larger,
 *       1e400 included;</li>
 *   <li><b>a string of decimal digits counts</b>, with an optional leading {@code +}, leading zeros and surrounding
 *       whitespace, when its value is in the same range: {@code "7"} is a TTL because {@code int("7")} is 7 and the
 *       reader expires on it. The string forms Python's {@code int()} also takes but this does not (digits with
 *       underscores, non-ASCII digits) are not counted; such an entry falls back to {@code indexed_at} in
 *       {@code clock_start}, which expires it late and never early. No writer produces a string TTL (the put path
 *       writes integers), so the regular expression is a guard against hand edits and imports, not a live path;</li>
 *   <li>a boolean, null, object, array, a non-digit string ("abc") and an absent key are not a TTL; the cast below is
 *       never applied to them.</li>
 * </ul>
 * {@code put_at} is present when it is a non-empty JSON string: {@code ""} means absent ({@code _NOTE_META_UNSET}
 * writes {@code put_at: ""} with {@code ttl_days: 0} on a retraction, and that pair passes), and a number, boolean,
 * object or null is not a stamp. A string that is not an ISO instant passes: SQL cannot mirror {@code parse_iso}.
 * A NULL {@code metadata}, or one that is not an object, passes.
 *
 * <p><b>Cast safety.</b> Postgres does not guarantee that the operands of AND are evaluated in the written order,
 * so no guard is an AND: every cast sits inside a CASE branch chosen by {@code jsonb_typeof}, and the string branch
 * uses no cast at all (a regular expression bounds the digits, so the longest string cannot overflow a numeric). A
 * jsonb number is already a numeric, so the one cast is total. The predicate is never NULL: {@code put_at} is
 * coalesced, because a NULL from a missing key would make the CHECK pass.
 *
 * <p><b>Guards.</b> The same as the chunk constraints: the migration walk's lock is read once before the first
 * statement, {@code lock_timeout} is 5 s, and one run holds {@link #ADVISORY_LOCK_KEY} at a time.
 *
 * <p><b>All tenants.</b> {@code nexus.catalog_documents} is FORCE row-level-security with one policy keyed to the
 * tenant stamp, and no policy that lets an owner read every row. {@link #census} therefore first requires a session
 * that is not row filtered (superuser or BYPASSRLS) and refuses with {@code admin_cannot_see_all_tenants} otherwise,
 * so an unrestricted-looking zero is never an undercount; the handler adds the scoped-count comparison the chunk
 * routes use. VALIDATE itself is not row filtered (it reads the table as the engine's constraint check), so even a
 * census that missed a tenant could not validate a table with a violator: it would fail with {@code check_violation}
 * and leave the constraint NOT VALID.
 */
public class CatalogTtlCheckAdmin {

    private static final Logger log = LoggerFactory.getLogger(CatalogTtlCheckAdmin.class);

    /** The session advisory lock a run holds: the bytes of {@code "catttlck"} as a bigint. */
    public static final long ADVISORY_LOCK_KEY = 0x636174746c636b6bL;

    public static final String CONSTRAINT = "catalog_documents_ttl_put_at_check";

    private static final String ADD_STATEMENT_TIMEOUT = "60s";
    private static final String VALIDATE_STATEMENT_TIMEOUT = "30min";
    private static final String CENSUS_STATEMENT_TIMEOUT = "10min";

    private static final AdminDdlSession.Settings SETTINGS = new AdminDdlSession.Settings(
        "nexus-catalog-ttl-check", ADVISORY_LOCK_KEY, "catalog-ttl-check", CATALOG_DOCUMENTS.getSchema().getName(),
        CATALOG_DOCUMENTS.getName(), "census");

    /** The largest TTL, in days, that the reader runs a clock on: {@code timedelta.max.days} is 999999999. */
    private static final BigDecimal TTL_CEILING = BigDecimal.valueOf(1_000_000_000L);
    /** A decimal string whose value is 1 up to 999999999: what {@code int()} reads as a TTL in range. */
    private static final String TTL_DIGITS = "^\\s*\\+?0*[1-9][0-9]{0,8}\\s*$";

    /** The constraint's state, as {@code pg_constraint} holds it. */
    public record Action(String action, AdminDdlSession.State after) {}

    /** One tenant's standing in the table-wide census. */
    public record TenantCensus(String tenant, long rows, long ttlRows, long violating, long violatingTombstoned) {}

    private final AdminConnection admin;

    public CatalogTtlCheckAdmin(AdminConnection admin) {
        this.admin = Objects.requireNonNull(admin, "admin");
    }

    // ---- the predicate -------------------------------------------------------------------------------------------

    private static final Field<JSONB> METADATA = DSL.field(DSL.name("metadata"), SQLDataType.JSONB);

    private static Field<String> typeOf(String key) {
        return DSL.function("jsonb_typeof", SQLDataType.VARCHAR, DSL.jsonbGetAttribute(METADATA, key));
    }

    /**
     * TRUE exactly when the entry carries a TTL in the sense of the class comment, never NULL. The one place the
     * TTL forms are written: each cast or pattern sits in the CASE branch its {@code jsonb_typeof} selects.
     */
    private static Field<Boolean> carriesTtlField() {
        Field<String> ttlText = DSL.jsonbGetAttributeAsText(METADATA, "ttl_days");
        Field<BigDecimal> ttlNumber = DSL.cast(DSL.jsonbGetAttribute(METADATA, "ttl_days"), SQLDataType.NUMERIC);
        Condition numberIsTtl = ttlNumber.ge(BigDecimal.ONE).and(ttlNumber.lt(TTL_CEILING));
        Condition stringIsTtl = ttlText.likeRegex(TTL_DIGITS);
        return DSL.case_()
            .when(typeOf("ttl_days").eq("number"), DSL.field(numberIsTtl))
            .when(typeOf("ttl_days").eq("string"), DSL.field(stringIsTtl))
            .else_(DSL.inline(false));
    }

    /**
     * The CHECK: TRUE or FALSE for every row, never NULL. FALSE exactly when the entry carries a TTL and no
     * non-empty string {@code put_at}.
     */
    static Condition ttlRequiresPutAt() {
        // put_at is coalesced: a missing key is a NULL type, and NULL AND x would let the row through the CHECK.
        Condition putAtIsStamp = DSL.coalesce(typeOf("put_at"), DSL.inline("")).eq("string")
            .and(DSL.coalesce(DSL.jsonbGetAttributeAsText(METADATA, "put_at"), DSL.inline("")).ne(""));
        return DSL.condition(DSL.case_().when(DSL.condition(carriesTtlField()), DSL.field(putAtIsStamp))
            .else_(DSL.inline(true)));
    }

    /** The rows the CHECK refuses: the predicate is FALSE (it is never NULL, so this is its plain negation). */
    static Condition violation() {
        return DSL.condition(DSL.field(ttlRequiresPutAt()).isFalse());
    }

    /** The entries that carry a TTL, with or without a stamp: the predicate's own notion of "has a TTL". */
    static Condition carriesTtl() {
        return DSL.condition(carriesTtlField());
    }

    /** The CHECK expression as the server is sent it: the predicate, inlined. */
    static String checkExpression() {
        return DSL.using(SQLDialect.POSTGRES).renderInlined(ttlRequiresPutAt());
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
     * The table-wide census: every tenant's rows, the entries that carry a TTL, and the entries the CHECK would
     * refuse, read in one pass on the admin session. Refuses ({@link AdminDdlSession.Refused} 503,
     * {@code admin_cannot_see_all_tenants}) unless the session is not row filtered. Tenants with no rows do not
     * appear.
     */
    public List<TenantCensus> census() {
        try (AdminDdlSession s = AdminDdlSession.open(admin, SETTINGS, false)) {
            s.requireUnrestrictedView("a superuser or BYPASSRLS role (catalog_documents has no policy that lets an "
                + "owner read every tenant's rows)");
            s.setSession("statement_timeout", CENSUS_STATEMENT_TIMEOUT);
            var rows = s.ctx().select(CATALOG_DOCUMENTS.TENANT_ID, DSL.count(),
                    DSL.count().filterWhere(carriesTtl()),
                    DSL.count().filterWhere(violation()),
                    DSL.count().filterWhere(violation().and(CATALOG_DOCUMENTS.DELETED_AT.isNotNull())))
                .from(CATALOG_DOCUMENTS)
                .groupBy(CATALOG_DOCUMENTS.TENANT_ID)
                .orderBy(CATALOG_DOCUMENTS.TENANT_ID)
                .fetch();
            List<TenantCensus> out = new ArrayList<>();
            for (var r : rows) {
                out.add(new TenantCensus(r.get(0, String.class), r.get(1, Long.class), r.get(2, Long.class),
                    r.get(3, Long.class), r.get(4, Long.class)));
            }
            return out;
        }
    }

    /**
     * Add the CHECK NOT VALID. A constraint that exists is left alone ({@code present}). No row is changed, but a NOT
     * VALID CHECK is evaluated against every new row version, an UPDATE that touches no metadata included: an entry
     * that already breaks it could no longer be UPDATEd or tombstoned (SQLSTATE 23514) until it is fixed. The caller
     * ({@code CatalogTtlCheckHandler}) refuses the add while the census reads a violator anywhere.
     */
    public Action add() {
        try (AdminDdlSession s = AdminDdlSession.open(admin, SETTINGS, true)) {
            s.guard(ADD_STATEMENT_TIMEOUT);
            AdminDdlSession.State before = s.states(List.of(CONSTRAINT)).get(0);
            if (before.present()) {
                return new Action("present", before);
            }
            s.ddl(addStatement(), List.of(CATALOG_DOCUMENTS.getSchema().getName(), CATALOG_DOCUMENTS.getName(),
                CONSTRAINT, checkExpression()));
            AdminDdlSession.State after = s.states(List.of(CONSTRAINT)).get(0);
            log.info("event=catalog_ttl_check_added validated={}", after.validated());
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
            s.ddl("ALTER TABLE %I.%I VALIDATE CONSTRAINT %I", List.of(CATALOG_DOCUMENTS.getSchema().getName(),
                CATALOG_DOCUMENTS.getName(), CONSTRAINT));
            AdminDdlSession.State after = s.states(List.of(CONSTRAINT)).get(0);
            log.info("event=catalog_ttl_check_validated validated={}", after.validated());
            return new Action("validated", after);
        }
    }
}
