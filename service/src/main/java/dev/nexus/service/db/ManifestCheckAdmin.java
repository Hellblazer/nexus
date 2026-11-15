// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;

import org.jooq.DSLContext;
import org.jooq.impl.DSL;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.ArrayList;
import java.util.List;
import java.util.Objects;

/**
 * The census, the add-NOT-VALID and the VALIDATE of one manifest CHECK of RDR-236 (nexus-6pfgb.42), chosen by a
 * {@link ManifestCheck}: {@code CHUNK_INDEX} ({@code chunk_index = position}) or {@code LABELS} ({@code labels_written
 * OR the three section labels are NULL}) on {@code nexus.catalog_document_chunks}. They run on the engine's admin
 * connection ({@link AdminDdlSession}), as the catalog TTL CHECK ({@link CatalogTtlCheckAdmin}) does, and for the same
 * reason are never a Liquibase changeset: a boot-time VALIDATE crash-loops an install whose rows differ from the
 * measured one, and no release rewrites user data at engine boot. Nothing in the engine calls them: the routes
 * ({@code ManifestCheckHandler}) are reached by an operator's request. Neither step changes a row.
 *
 * <p><b>Writer audit (nexus-6pfgb.42, 2026-11-14), CHUNK_INDEX.</b> {@code chunk_index} is NOT NULL since hygiene-001,
 * and a BEFORE INSERT trigger ({@code trg_catalog_document_chunks_chunk_index_default}) fills it from {@code position}
 * when a writer omits it. No writer in this repository sets the two apart, so the census is expected to read zero; the
 * engine stores a caller's {@code chunk_index} as sent, so the CHECK is what stops a later writer.
 *
 * <p><b>Writer audit, LABELS.</b> Every writer of {@code section_title}, {@code section_type}, {@code page_number}
 * and {@code labels_written} on the table, and whether it can write a label while {@code labels_written} is false:
 * <ul>
 *   <li>{@code CatalogRepository.insertManifestChunkRows}, the one manifest INSERT (single-homed), for
 *       writeManifest, writeManifestMany, appendManifest(Many), importChunk(s) and the re-embed move: the three labels
 *       and {@code labels_written} are columns of one row, and {@code labelsWritten(row)} is TRUE whenever the request
 *       row carries any label key. All three ON CONFLICT arms (PLAIN, UPSERT_APPEND, UPSERT_IMPORT) set the labels and
 *       the marker from the same {@code excluded} row. It stores a request that says {@code labels_written: false}
 *       beside a non-NULL label as sent (an explicit boolean wins): that is the one way a live writer can break the
 *       CHECK, and it takes a contradictory request. No client in this repository sends one: the Python wire
 *       ({@code manifest_row_wire}, {@code _manifest_chunk_rows}, {@code note_manifest_rows}) sends the stored pair back
 *       (NULL labels and false, or labels and true) or sends the label keys with no explicit false. After the CHECK a
 *       caller that did would get SQLSTATE 23514 on that row, which is the CHECK's purpose.</li>
 *   <li>{@code ManifestFill} (the label rung): sets the three labels and {@code labels_written = true} in one UPDATE;
 *       its span rung touches neither. Cannot.</li>
 *   <li>The quarantine-restore reattach (plpgsql, vectors-025/030/053) and the manifest backfill name neither the
 *       labels nor the marker: a new row takes NULL labels and false. Cannot.</li>
 *   <li>Tenant import ({@code TenantImportLoad}, one INSERT ... SELECT of the export file's columns): carries the pair
 *       the source row held, so it can write a violating row only from a source row that already was one (which the
 *       census of the source engine counts). Pass-through.</li>
 *   <li>{@code ManifestReplace.update} (nexus-z0o2p.29, the manifest replace of {@code writeManifestRows}): for a
 *       position the document holds and the new set lists with other values, one UPDATE sets every payload column,
 *       {@code chash} and the three labels and {@code labels_written} among them, from the incoming row. The pair
 *       comes from the same {@code labelsWritten(row)} as the insert's, so it has the first item's one exposure: a
 *       request that says {@code labels_written: false} beside a label, stored as sent, 23514 after the CHECK.
 *       The delete-and-insert fallback is the first item.</li>
 *   <li>The collection rename ({@code ChashRepository.renameCollection}): UPDATEs {@code collection} and
 *       {@code embedding_model} only. A re-home that moves documents goes through the first item's INSERT or the
 *       replace above. Cannot.</li>
 *   <li>Triggers: the stamp triggers are statement-level and write {@code chunk_orphaned_at}; the chunk_index default
 *       trigger touches {@code chunk_index} only. Changesets: catalog-046 adds the columns (NULL, false); no changeset
 *       sets a label or the marker. Cannot.</li>
 * </ul>
 * The census is expected to read zero. A dry run of the key sweep already counts the same rows per tenant as
 * {@code manifest_unwritten_label_rows}, informationally.
 *
 * <p><b>The predicate</b> is {@link ManifestCheck#holds()}, one object: the CHECK is its rendering, the census counts
 * its negation.
 *
 * <p><b>Guards.</b> As the TTL step: the migration walk's lock is read once before the first statement,
 * {@code lock_timeout} is 5 s, and one run holds {@link ManifestCheck#lockKey()} at a time (distinct for each check and
 * from the chunk step's and the TTL step's keys, so they can run side by side).
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
 * after the ADD and reports {@code violating_after_add}. No in-repository writer breaks either predicate, so the window
 * is theory.
 */
public class ManifestCheckAdmin {

    private static final Logger log = LoggerFactory.getLogger(ManifestCheckAdmin.class);

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

    /** The constraint's state, as {@code pg_constraint} holds it. */
    public record Action(String action, AdminDdlSession.State after) {}

    /** One tenant's standing in the table-wide census. */
    public record TenantCensus(String tenant, long rows, long violating) {}

    private final AdminConnection admin;
    private final ManifestCheck check;
    private final AdminDdlSession.Settings settings;

    public ManifestCheckAdmin(AdminConnection admin, ManifestCheck check) {
        this.admin = Objects.requireNonNull(admin, "admin");
        this.check = Objects.requireNonNull(check, "check");
        this.settings = new AdminDdlSession.Settings(check.applicationName(), check.lockKey(), check.route(),
            CATALOG_DOCUMENT_CHUNKS.getSchema().getName(), CATALOG_DOCUMENT_CHUNKS.getName(), "census");
    }

    /** The check this admin runs. */
    public ManifestCheck check() {
        return check;
    }

    // ---- public --------------------------------------------------------------------------------------------------

    /** The constraint's state, read on the admin connection (absent: {@code present} false). */
    public AdminDdlSession.State state() {
        try (AdminDdlSession s = AdminDdlSession.open(admin, settings, false)) {
            return s.states(List.of(check.constraint())).get(0);
        }
    }

    /**
     * The table-wide census: every tenant's manifest rows and the rows the CHECK would refuse, read in one pass on the
     * admin session. Refuses ({@link AdminDdlSession.Refused} 503, {@code admin_cannot_see_all_tenants}) unless the
     * session reads every row ({@link AdminDdlSession#readEveryTenant}, which says what that costs). Tenants with no
     * rows do not appear.
     */
    public List<TenantCensus> census() {
        try (AdminDdlSession s = AdminDdlSession.open(admin, settings, false)) {
            s.setSession("statement_timeout", CENSUS_STATEMENT_TIMEOUT);
            return s.readEveryTenant(OWNER_POINT_AT, OWNER_CENSUS_STATEMENT_TIMEOUT,
                this::censusOn);
        }
    }

    private List<TenantCensus> censusOn(DSLContext ctx) {
        var rows = ctx.select(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, DSL.count(), DSL.count().filterWhere(check.violation()))
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
     * (SQLSTATE 23514) until it is fixed. The caller ({@code ManifestCheckHandler}) refuses the add while the
     * census reads a violator anywhere.
     */
    public Action add() {
        try (AdminDdlSession s = AdminDdlSession.open(admin, settings, true)) {
            s.guard(ADD_STATEMENT_TIMEOUT);
            AdminDdlSession.State before = s.states(List.of(check.constraint())).get(0);
            if (before.present()) {
                return new Action("present", before);
            }
            s.ddl(ManifestCheck.addStatement(), List.of(CATALOG_DOCUMENT_CHUNKS.getSchema().getName(),
                CATALOG_DOCUMENT_CHUNKS.getName(), check.constraint(), check.checkExpression()));
            AdminDdlSession.State after = s.states(List.of(check.constraint())).get(0);
            log.info("event={}_added validated={}", check.event(), after.validated());
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
        try (AdminDdlSession s = AdminDdlSession.open(admin, settings, true)) {
            s.guard(VALIDATE_STATEMENT_TIMEOUT);
            AdminDdlSession.State before = s.states(List.of(check.constraint())).get(0);
            if (!before.present()) {
                return new Action("absent", before);
            }
            if (Boolean.TRUE.equals(before.validated())) {
                return new Action("already_valid", before);
            }
            s.ddl("ALTER TABLE %I.%I VALIDATE CONSTRAINT %I", List.of(CATALOG_DOCUMENT_CHUNKS.getSchema().getName(),
                CATALOG_DOCUMENT_CHUNKS.getName(), check.constraint()));
            AdminDdlSession.State after = s.states(List.of(check.constraint())).get(0);
            log.info("event={}_validated validated={}", check.event(), after.validated());
            return new Action("validated", after);
        }
    }
}
