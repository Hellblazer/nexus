package dev.nexus.service.db;

import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.changelog.ChangeSet;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.exception.LiquibaseException;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record4;
import org.jooq.Result;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import javax.sql.DataSource;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.ZoneId;
import java.time.ZoneOffset;
import java.util.TimeZone;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Applies the Liquibase master changelog to a target {@link DataSource}.
 *
 * <p>Called from {@code Main.java} after HikariCP pool creation and before
 * {@code NexusService.start()}, so the service never serves requests against
 * an unmigrated database.
 *
 * <p><strong>Idempotency.</strong> Liquibase tracks applied changesets in the
 * {@code DATABASECHANGELOG} table; re-running against an already-migrated
 * database is a verified no-op (zero changesets applied, no DDL issued).
 *
 * <p><strong>Privilege requirement.</strong> The connection borrowed from
 * {@code ds} must have DDL privileges: {@code CREATE SCHEMA}, {@code CREATE
 * TABLE}, {@code ALTER TABLE ... ENABLE ROW LEVEL SECURITY}, and
 * {@code CREATE POLICY}. The {@code nexus_svc} role (NOSUPERUSER NOBYPASSRLS)
 * has only DML rights on the application tables and therefore cannot run
 * migrations. In production the caller must supply a <em>separate</em>
 * migration datasource whose credentials hold schema-owner or superuser
 * rights. {@code Main.java} reads {@code NX_DB_ADMIN_*} variables for this
 * purpose, falling back to the regular {@code NX_DB_*} credentials when they
 * are absent (useful in development / single-role setups where the service
 * role also owns the schema).
 *
 * <p><strong>Phase-5 provisioning note.</strong> When the production
 * deployment uses two roles (schema-owner + service role), the Phase-5 {@code
 * nx} provisioning step must:
 * <ol>
 *   <li>Install extensions as superuser BEFORE the first migration run:
 *       {@code CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT
 *       EXISTS pg_trgm;} Neither is a trusted extension and the schema-owner
 *       role below is NOSUPERUSER, so changeset {@code vectors-001-1} fails
 *       without this DBA pre-step (it becomes an idempotent no-op once the
 *       extensions exist).</li>
 *   <li>RELOCATE both extensions into the {@code nexus} schema, also as
 *       superuser, BEFORE the first migration run carrying nexus-cbo4a batch
 *       9 item 0: {@code ALTER EXTENSION vector SET SCHEMA nexus; ALTER
 *       EXTENSION pg_trgm SET SCHEMA nexus;} — see "Extension relocation"
 *       below for why this is a superuser step rather than a Liquibase
 *       changeset, and for the exact failure this DBA step prevents.
 *       <strong>This step presumes the cluster has already completed a PAST
 *       walk of {@code vectors-001-baseline.xml}</strong> (true of every
 *       real production cluster under this project's single-shared-cluster
 *       deployment model). A genuinely brand-new production cluster's
 *       FIRST-EVER walk must NOT relocate ahead of time — doing so breaks
 *       {@code vectors-001-2/-3/-4}'s own bare {@code vector(N)}/{@code
 *       vector_cosine_ops} references, which still need the extension
 *       resolvable via the default, public-only search_path at that point
 *       in the SAME walk. For that shape, skip this step entirely and
 *       instead install the SECURITY DEFINER function pair (or run
 *       {@code nexus.db.pg_provision}'s bootstrap) before the first walk —
 *       see "Extension relocation" below for the mechanism that then
 *       relocates MID-WALK, at the correct sequencing point.</li>
 *   <li>Create the schema-owner role (e.g. {@code nexus_admin}) with
 *       {@code CREATE ON DATABASE nexus} and ownership of the {@code nexus}
 *       and {@code t1} schemas.</li>
 *   <li>Create {@code nexus_svc} as a NOSUPERUSER NOBYPASSRLS LOGIN role.</li>
 *   <li>Supply {@code NX_DB_ADMIN_*} credentials as the schema-owner role and
 *       {@code NX_DB_*} credentials as {@code nexus_svc}.</li>
 * </ol>
 * The changelogs' post-DDL grant DO-blocks (changeset suffix {@code -5} in
 * each baseline) then grant DML rights to {@code nexus_svc} automatically
 * during the first migration run.
 *
 * <p><strong>Extension relocation (nexus-cbo4a batch 9 item 0, Sam's
 * directive, 2026-09-05; REDESIGNED per T2 nexus/critique-nexus-cbo4a-
 * batch-9-search-path, a ship-blocker fix).</strong> {@code vector} and
 * {@code pg_trgm} are both relocatable extensions and live in the {@code
 * nexus} schema, not {@code public} — every SQL function in the changelog
 * references their types/operators/functions as {@code nexus.*} rather than
 * relying on the calling session's search_path. {@code
 * search-path-001-relocate-vector-extensions.xml} GUARDS that this
 * relocation already happened; it does not perform it unconditionally
 * itself, because the schema-owner role (NOSUPERUSER) can never own or
 * relocate an extension that predates this batch — created directly as the
 * cluster's bootstrap superuser, which {@code REASSIGN OWNED BY}
 * unconditionally refuses to ever hand off. An EARLIER design instead had
 * every extension-creation site transfer OWNERSHIP to the schema-owner role
 * via a throwaway superuser role, then relocate unconditionally from an
 * ordinary Liquibase changeset — this bricked every install that predated
 * the batch, since the ownership-transfer backfill is a documented no-op for
 * an extension the bootstrap superuser already owns, with nothing left able
 * to move it. The current design relocates via the guard changeset's own
 * THREE-TIER body instead, uniformly for local and production installs
 * alike: tier 1 attempts the {@code ALTER EXTENSION} directly (succeeds
 * whenever the connecting/migrating role is or can act as superuser — this
 * Phase-5 DBA pre-step for production is exactly what makes tier 1's
 * precondition already satisfied, so the guard changeset MARK_RANs with no
 * body execution at all); tier 2, on {@code insufficient_privilege} (the
 * NOSUPERUSER {@code nexus_admin} case — every real local install, and any
 * production cluster whose DBA skips the relocate-ahead-of-time step
 * above), calls the narrow SECURITY DEFINER helper function ({@code
 * nexus.ensure_vector_extensions_relocated()}, owned by the superuser) that
 * performs the relocation with the function OWNER's privilege; tier 3 (both
 * absent) {@code RAISE EXCEPTION} naming the exact remedy. {@code
 * nexus.db.pg_provision}'s client-side provisioning, run on every local
 * daemon start (and at the end of a from-scratch provision), NEVER
 * relocates itself — an earlier revision did, gated behind a heuristic
 * (a probe for the per-dim chunk tables vectors-001 created,
 * meant to prove "this cluster's walk has already run past the bare
 * vector(N) references") that turned out to be permanently FALSE on every
 * real cluster: {@code vectors-004-unify-chunks.xml} unconditionally drops
 * all three {@code chunks_<dim>} tables in favour of the unified {@code
 * nexus.chunks}, and every cluster old enough to ever reach this changeset
 * has already run that changeset. That eager path silently downgraded to a
 * no-op on every real install and was deleted outright (T2 nexus/critique-
 * nexus-cbo4a-batch-9-gated SIGNIFICANT 1). It now only ensures the
 * {@code nexus} schema and the
 * SECURITY DEFINER function pair exist, which is exactly what tier 2 needs
 * to succeed for a from-scratch install's first-ever walk — that walk runs
 * {@code vectors-001-2/-3/-4}'s own bare references in the SAME continuous
 * walk as the guard changeset, so relocating ahead of time would break
 * them; the guard's tier 2 instead relocates MID-WALK, well after those
 * changesets already ran. A genuinely brand-new PRODUCTION cluster's
 * first-ever walk has the identical hazard and the identical fix: create
 * the extensions in {@code public} (step 1 above) and install the SECURITY
 * DEFINER function pair — or simply run {@code nexus.db.pg_provision}'s
 * bootstrap against that cluster — before the first walk, rather than
 * relocating ahead of time. See {@code
 * search-path-001-relocate-vector-extensions.xml}'s own header for the full
 * three-tier derivation and {@code
 * relocate_vector_extensions_to_nexus_schema}'s own docstring for the
 * mechanism it installs. conexus's PITR-fork walk rehearsal exercises this
 * changelog directly against a production-shaped cluster (always an
 * EXISTING cluster with a past walk, never a from-scratch one), so a DBA
 * who skips this Phase-5 step there is caught as the named FAIL LOUD case,
 * never a silent no-op — the from-scratch-production shape above is
 * currently unrehearsed by any gate in this repository. Also note: an
 * earlier draft believed
 * {@code pg_trgm} (trusted since PG13) needed no special treatment relative
 * to {@code vector} at all — WRONG, caught live by {@code
 * tests/e2e/local-service-gate.sh}'s first real dev-jar run: PostgreSQL
 * stamps every one of pg_trgm's 31 LANGUAGE-C member functions with
 * bootstrap-superuser ownership regardless of who issues CREATE EXTENSION,
 * trusted or not, so both extensions always relocate together, identically,
 * everywhere this Phase-5 step or {@code nexus.db.pg_provision} runs.
 *
 * <p>RDR-152 bead nexus-net63.
 */
public final class SchemaMigrator {

    private static final Logger log = LoggerFactory.getLogger(SchemaMigrator.class);

    /** Classpath location of the master changelog bundled in the service jar. */
    static final String MASTER_CHANGELOG = "db/changelog/db.changelog-master.xml";

    private SchemaMigrator() { /* static utility */ }

    /**
     * What a walk actually did, in truthfully named counts (nexus-x0s52).
     *
     * <p>The old {@code schema_migration_complete} line logged the PRE-update
     * pending count under the name {@code applied_changesets}. Measured on the
     * v0.1.86 PITR fork walk (2026-08-27): the line said 12 where 1 genuinely
     * new changeset landed and 25 rows were touched (24 {@code runAlways}
     * re-runs) — the logged number corresponded to NONE of the three
     * quantities an operator might mean by "applied". These fields are the
     * real ones:
     *
     * @param pendingAtStart  {@code listUnrunChangeSets()} BEFORE the update.
     *                        NOT "new changesets waiting": Liquibase counts the
     *                        {@code runAlways} re-run plan here too (measured:
     *                        11 on a no-op walk of this changelog), which is
     *                        exactly how the old line came to claim 12 applied
     *                        where 1 landed
     * @param newChangesets   distinct changeset IDENTITIES (id, author,
     *                        filename) with a {@code databasechangelog} row
     *                        stamped {@code EXECUTED} above this walk's
     *                        pre-walk {@code orderexecuted} watermark —
     *                        changesets that genuinely landed for the first
     *                        time ("did my one changeset land" reads THIS
     *                        field)
     * @param reexecutedChangesets distinct changeset IDENTITIES stamped
     *                        {@code RERAN} above the watermark — the
     *                        {@code runAlways} / {@code runOnChange} re-runs
     *                        (a clean walk proves they executed, not that
     *                        their content is right)
     * @param markRanChangesets distinct changeset IDENTITIES stamped
     *                        {@code MARK_RAN} above the watermark (a
     *                        {@code <preConditions onFail="MARK_RAN">} skip)
     *                        — reported on its own rather than folded into
     *                        either count above, since it is neither a
     *                        landing nor a re-run
     *
     * <p><strong>Counted by IDENTITY, not by row (nexus-jl08t).</strong> A
     * raw {@code COUNT(*)} grouped by {@code exectype} over the rows this
     * walk touched counts physical {@code databasechangelog} rows, not
     * changesets — and this project's production database carries DUPLICATE
     * rows for at least one changeset identity (confirmed by conexus's
     * 2026-09-14 read-only query of the live cluster: {@code
     * grants-nexus-diag-1}/{@code -2} each have several extra copies, 13 in
     * total against 12 distinct runAlways identities). Liquibase's own
     * {@code RERAN} path ({@code MarkChangeSetRanGenerator}) issues one
     * {@code UPDATE ... WHERE id=? AND author=? AND filename=?} per
     * changeset — a statement with no row-count limit — so it re-stamps
     * EVERY matching physical row with the same {@code dateexecuted} and
     * {@code orderexecuted} while Liquibase itself believes it processed one
     * changeset. That is exactly the mechanism that inflated conexus's
     * engine-service-v0.1.118 log line to
     * {@code reexecuted_changesets=25} against 12 declared {@code runAlways}
     * changesets: a non-distinct row count scoped to that same walk (whether
     * by the dateexecuted window this replaces, or by Liquibase's own
     * per-walk {@code deployment_id}) ALSO reports 25 (verified in
     * {@code SchemaMigratorIntegrationTest}'s "aged database" test, which
     * seeds the exact production duplicate shape and records both wrong
     * values and the fixed one). Grouping by identity before counting
     * removes the row-count amplification entirely: each of the 12
     * {@code runAlways} identities counts once, however many physical copies
     * carry it. The origin of the duplicate rows themselves cannot be
     * recovered from a walk's own bookkeeping — every {@code RERAN} update
     * overwrites each copy's {@code dateexecuted}/{@code orderexecuted}
     * alike — and de-duplicating or deleting them is a data-hygiene decision
     * for the database's owner, not something a walk performs; see
     * {@code migrate()}'s {@code schema_changelog_duplicate_rows} log line.
     *
     * <p><strong>Why {@code orderexecuted}, not {@code deployment_id}
     * (nexus-jl08t).</strong> Liquibase's own per-walk {@code deployment_id}
     * was tried first and rejected: recovering it after {@code
     * liquibase.update()} returns requires reaching into {@code
     * ChangeLogHistoryServiceFactory}'s internal per-{@code Database} cache,
     * a plain {@code HashMap} keyed on the {@code Database} object — and
     * {@code AbstractJdbcDatabase#hashCode()} delegates to its CURRENT JDBC
     * connection wrapper, which Liquibase's own update pipeline replaces
     * mid-walk. The same {@code Database} reference then hashes differently
     * than it did when the entry was cached, so the lookup lands in the
     * wrong bucket and silently returns a fresh, never-generated service
     * instance — measured directly: {@code getDeploymentId()} read
     * {@code null} on every call despite Liquibase's own log line reporting
     * a real id for that same walk. {@code orderexecuted} carries none of
     * that fragility: it is a plain integer column read by this class's own
     * query, independent of any in-process Liquibase object identity or
     * caching, and every physical copy of a duplicated identity's RERAN
     * update shares one {@code orderexecuted} value (confirmed by
     * conexus-9a's production query), so a watermark comparison alone
     * already scopes correctly to this walk.
     *
     * <p><strong>The exact identity.</strong> In a race-free, single-writer
     * walk, {@code newChangesets + reexecutedChangesets + markRanChangesets
     * == pendingAtStart} EXACTLY: {@code pendingAtStart} is already the full
     * set Liquibase's own planner intends to touch this walk (every
     * genuinely-new changeset plus every {@code runAlways}/
     * {@code runOnChange} rerun plus any precondition-skip), and the three
     * counts above partition that same plan by outcome, each de-duplicated
     * to one entry per identity. {@code migrate()} checks this identity on
     * every walk and logs {@code event=schema_migration_count_anomaly} on
     * any mismatch; a real mismatch now means either a second writer
     * advanced {@code public.databasechangelog}'s {@code orderexecuted}
     * sequence during this walk (a genuine concurrent walker against the
     * same database — the watermark scopes out everything ALREADY there
     * before this walk started, but not a third party racing it), or a
     * changeset failed or was skipped without Liquibase ever stamping a row
     * for it — not the retired dateexecuted-clock-window theory this
     * replaces.
     */
    public record MigrationOutcome(
            int pendingAtStart, long newChangesets, long reexecutedChangesets,
            long markRanChangesets) {}

    /**
     * Distinct-identity counts of this walk's {@code databasechangelog} rows,
     * grouped by {@code exectype}, plus how much row-count duplication the
     * walk found (nexus-jl08t). See {@link MigrationOutcome}'s javadoc for
     * why identity de-duplication is required rather than optional.
     */
    private record WalkChangesetCounts(
            long newChangesets, long reexecutedChangesets, long markRanChangesets,
            long duplicateIdentities, long duplicateExtraRows) {}

    /**
     * Applies all pending Liquibase changesets from the master changelog to the
     * database reachable via {@code ds}.
     *
     * <p>Borrows one connection from the pool, runs the full
     * {@link Liquibase#update(Contexts, LabelExpression)} call, then closes the
     * connection. The HikariCP pool returns it to the pool; subsequent service
     * requests use it normally.
     *
     * @param ds migration-capable datasource (schema-owner or superuser rights)
     * @return the walk's real counts — also logged as
     *         {@code event=schema_migration_complete}
     * @throws MigrationException if Liquibase fails or the connection cannot be
     *                             obtained; caller should treat this as fatal
     */
    public static MigrationOutcome migrate(DataSource ds) {
        log.info("event=schema_migration_start changelog={}", MASTER_CHANGELOG);
        try {
            pinJvmTimeZoneToUtc();
        } catch (TimeZonePinFailedException e) {
            throw new MigrationException("JVM timezone pin failed", e);
        }

        try (Connection conn = ds.getConnection()) {
            // nexus-rph82: Liquibase stamps databasechangelog.dateexecuted with the
            // SERVER's now() rendered in the connection's SESSION zone — and pgjdbc
            // negotiates that zone from the JVM default at CONNECT time, so a pool
            // opened before the pin above still carries the old zone. Pin the
            // session too; the JVM pin covers client-side formatting, this covers
            // the stamp itself, and together they hold for every entry point.
            //
            // nexus-zrcj7 step 4 critic follow-up (T2 [24242]): retired the raw
            // "SET TIME ZONE 'UTC'" statement onto its own Postgres-documented
            // equivalent -- SET TIME ZONE 'UTC' IS set_config('TimeZone', 'UTC',
            // false) (false = session-scoped, matching SET rather than SET LOCAL) --
            // called through DSL.using(conn, SQLDialect.POSTGRES) over the SAME bare
            // bootstrap Connection, same conversion shape as maxOrderExecuted/
            // countThisWalkChangesets below. The EXEMPTION_REGISTRY entry
            // this statement carried ("PostgreSQL session syntax, no jOOQ typed-DSL
            // form for a SET statement at all") is retired with it: set_config(...)
            // is an ordinary PostgreSQL function, not the bare SET statement, so this
            // was never actually a no-typed-form case either.
            try {
                DSL.using(conn, SQLDialect.POSTGRES)
                    .select(DSL.function("set_config", SQLDataType.VARCHAR,
                        DSL.val("TimeZone"), DSL.val("UTC"), DSL.val(false)))
                    .fetchOne();
            } catch (DataAccessException e) {
                throw new SQLException("SET TIME ZONE 'UTC' (via set_config) failed", e);
            }
            preflightChashConstraints(conn);

            Database database = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(conn));

            try (Liquibase liquibase = new Liquibase(
                    MASTER_CHANGELOG,
                    new ClassLoaderResourceAccessor(),
                    database)) {

                // Count pending changesets for the structured log entry.
                int pending = liquibase.listUnrunChangeSets(
                    new Contexts(), new LabelExpression()).size();
                log.info("event=schema_migration_pending changesets={}", pending);

                // nexus-jl08t: identify THIS walk's own rows via a pre-walk
                // orderexecuted WATERMARK, not Liquibase's own deployment_id.
                // deployment_id was tried first and rejected: ChangeLogHistoryServiceFactory
                // caches its per-Database service in a plain HashMap keyed on the Database
                // object, and AbstractJdbcDatabase#hashCode() delegates to its current JDBC
                // connection wrapper -- which Liquibase's own update() pipeline replaces
                // mid-walk, changing `database`'s hashCode after it was cached. A HashMap
                // lookup by the SAME object reference then lands in the wrong bucket and
                // silently returns a FRESH, never-generated service instance (measured:
                // getDeploymentId() reads null every time despite Liquibase's own log
                // line reporting a real id for the same walk). ORDEREXECUTED has none of
                // that fragility -- it is a plain integer column this method reads with
                // its own query, entirely independent of any in-process Liquibase object
                // identity or caching behavior, and every physical row of a duplicate
                // identity's RERAN update shares one orderexecuted value (confirmed by
                // conexus-9a's production query), so a plain watermark comparison already
                // scopes to this walk without needing deployment_id at all.
                long orderExecutedWatermark = maxOrderExecuted(conn);

                liquibase.update(new Contexts(), new LabelExpression());

                WalkChangesetCounts counts = countThisWalkChangesets(conn, orderExecutedWatermark);

                if (counts.duplicateIdentities() > 0) {
                    // nexus-jl08t: confirmed on production 2026-09-14 (conexus-9a,
                    // read-only) -- grants-nexus-diag-1/-2 each carry several extra
                    // databasechangelog rows sharing one identity (13 extra rows
                    // across those 2 identities at the time of that query). Every
                    // copy is re-executed together on every runAlways walk because
                    // Liquibase's RERAN UPDATE has no row-count limit; the counts
                    // above are already de-duplicated by identity, so they report
                    // what Liquibase actually ran, not the physical row count. The
                    // duplicates' origin cannot be recovered from a walk's own
                    // bookkeeping -- each RERAN overwrites every copy's own
                    // dateexecuted/deployment_id alike -- and this walk does not
                    // delete or merge them: that is a data-hygiene decision for the
                    // database's owner, not something a migration performs.
                    log.warn("event=schema_changelog_duplicate_rows identity_count={} "
                            + "extra_rows={} orderexecuted_watermark={}",
                            counts.duplicateIdentities(), counts.duplicateExtraRows(),
                            orderExecutedWatermark);
                }

                long newChangesets = counts.newChangesets();
                long reexecuted = counts.reexecutedChangesets();
                long markRan = counts.markRanChangesets();
                // nexus-jl08t: the three outcome counts partition pending_at_start
                // EXACTLY in a race-free single-writer walk -- see MigrationOutcome's
                // javadoc. A mismatch now means a genuine second writer advanced
                // public.databasechangelog's orderexecuted sequence DURING this
                // walk (a concurrent walker against the same database), or a
                // changeset failed/was skipped without Liquibase ever stamping a
                // row for it -- not the retired dateexecuted-clock-window theory
                // (fixed) nor row-count duplication (already de-duplicated above
                // by identity).
                long accountedFor = newChangesets + reexecuted + markRan;
                if (accountedFor != pending) {
                    log.warn("event=schema_migration_count_anomaly accounted_for={} "
                            + "pending_at_start={} new_changesets={} "
                            + "reexecuted_changesets={} mark_ran_changesets={} — "
                            + "new_changesets + reexecuted_changesets + "
                            + "mark_ran_changesets must equal pending_at_start; this "
                            + "mismatch means a second writer advanced "
                            + "public.databasechangelog's orderexecuted sequence "
                            + "during this walk, or a changeset failed or was "
                            + "skipped without leaving a databasechangelog row",
                            accountedFor, pending, newChangesets, reexecuted, markRan);
                }
                // The old line logged the PRE-update pending count as
                // applied_changesets — a quantity the walk never computed
                // (12x overstatement measured on the v0.1.86 fork walk). The
                // misleading field name is deliberately GONE, not repaired in
                // place: a deploy grep for it should find nothing and force a
                // read of the real fields, never silently match new semantics.
                log.info("event=schema_migration_complete new_changesets={} "
                        + "reexecuted_changesets={} pending_at_start={} "
                        + "mark_ran_changesets={}",
                        newChangesets, reexecuted, pending, markRan);
                return new MigrationOutcome(pending, newChangesets, reexecuted, markRan);
            }

        } catch (SQLException e) {
            throw new MigrationException("Failed to obtain DB connection for migration", e);
        } catch (LiquibaseException e) {
            throw new MigrationException("Liquibase migration failed", e);
        }
    }

    // ── nexus-x0s52 / nexus-jl08t: truthful walk counts ──────────────────────
    // nexus-cbo4a batch 9 item 0 (Sam's directive, 2026-09-05): databasechangelog
    // is explicitly schema-qualified as "public" below (DSL.name("public",
    // "databasechangelog")), matching VersionHandler's own DATABASECHANGELOG
    // constant -- this table is Liquibase's own bookkeeping table, created via a
    // migration connection that carries no search_path override, so it lands in
    // Postgres's own default schema ("$user", public) resolving to public.
    //
    // nexus-zrcj7 step 4 review follow-up (critic, T2 [24235]): the methods below
    // run on the BARE bootstrap Connection Liquibase itself borrows, before this
    // class ever constructs its own long-lived DSLContext. That connection is a
    // plain java.sql.Connection like any other, and jOOQ's DSL.using(Connection,
    // SQLDialect) wraps ANY such connection, so typed DSL applies here exactly as
    // elsewhere: DSL.table(DSL.name("public", "databasechangelog")) /
    // DSL.field(DSL.name(...), Class) for Liquibase's own bookkeeping table
    // (outside jOOQ codegen's modeled schemata, but nameable via the same safe
    // quoted-identifier idiom ChashCensus.java/StagingPromoteOps.java/this bead's
    // own TaxonomyRepository#advanceTopicsIdSequence conversion already use).
    // Throws SQLException (matching migrate()'s own catch(SQLException) at its
    // call site) by catching jOOQ's unchecked DataAccessException and rethrowing
    // checked -- jOOQ itself never throws SQLException directly.

    /** The highest {@code orderexecuted} in {@code databasechangelog} before this
     * walk, or 0 when the table does not exist yet (first boot) or is empty --
     * {@code orderexecuted} starts at 1, so 0 never collides with a real row. */
    private static long maxOrderExecuted(Connection conn) throws SQLException {
        try {
            DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
            String regclass = ctx.select(DSL.function(
                    "to_regclass", SQLDataType.VARCHAR, DSL.val("public.databasechangelog")))
                .fetchOne(0, String.class);
            if (regclass == null) {
                return 0L;
            }
            Field<Integer> orderExecuted = DSL.field(DSL.name("orderexecuted"), Integer.class);
            Integer max = ctx.select(DSL.max(orderExecuted))
                .from(DSL.table(DSL.name("public", "databasechangelog")))
                .fetchOne(DSL.max(orderExecuted));
            return max == null ? 0L : max.longValue();
        } catch (DataAccessException e) {
            throw new SQLException("maxOrderExecuted failed", e);
        }
    }

    /**
     * Counts this walk's {@code databasechangelog} rows by outcome, scoped to
     * rows whose {@code orderexecuted} is strictly above the pre-walk watermark
     * and de-duplicated by changeset IDENTITY (id, author, filename) rather than
     * by physical row (nexus-jl08t). See {@link MigrationOutcome}'s javadoc for
     * why a raw row count still over-counts on this project's production
     * database, which carries duplicate rows for at least one changeset
     * identity — every physical copy of a duplicated identity shares the SAME
     * {@code orderexecuted} once re-executed (confirmed by conexus-9a's
     * production query), so the watermark alone scopes correctly but the
     * IDENTITY de-duplication is still required to count changesets, not rows.
     */
    private static WalkChangesetCounts countThisWalkChangesets(Connection conn, long orderExecutedWatermark)
            throws SQLException {
        try {
            DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
            Field<String> idField = DSL.field(DSL.name("id"), String.class);
            Field<String> authorField = DSL.field(DSL.name("author"), String.class);
            Field<String> filenameField = DSL.field(DSL.name("filename"), String.class);
            Field<String> exectypeField = DSL.field(DSL.name("exectype"), String.class);
            Field<Integer> orderExecutedField = DSL.field(DSL.name("orderexecuted"), Integer.class);

            Result<Record4<String, String, String, String>> rows = ctx
                .select(idField, authorField, filenameField, exectypeField)
                .from(DSL.table(DSL.name("public", "databasechangelog")))
                .where(orderExecutedField.gt((int) orderExecutedWatermark))
                .fetch();

            Map<List<String>, Integer> rowsPerIdentity = new LinkedHashMap<>();
            Map<String, Set<List<String>>> identitiesByExecType = new LinkedHashMap<>();
            for (Record4<String, String, String, String> row : rows) {
                List<String> identity = List.of(row.value1(), row.value2(), row.value3());
                rowsPerIdentity.merge(identity, 1, Integer::sum);
                identitiesByExecType
                    .computeIfAbsent(row.value4(), k -> new HashSet<>())
                    .add(identity);
            }

            long newChangesets = identitiesByExecType
                .getOrDefault(ChangeSet.ExecType.EXECUTED.value, Set.of()).size();
            long reexecuted = identitiesByExecType
                .getOrDefault(ChangeSet.ExecType.RERAN.value, Set.of()).size();
            long markRan = identitiesByExecType
                .getOrDefault(ChangeSet.ExecType.MARK_RAN.value, Set.of()).size();

            long duplicateIdentities = rowsPerIdentity.values().stream()
                .filter(count -> count > 1).count();
            long duplicateExtraRows = rowsPerIdentity.values().stream()
                .filter(count -> count > 1).mapToLong(count -> count - 1).sum();

            return new WalkChangesetCounts(
                newChangesets, reexecuted, markRan, duplicateIdentities, duplicateExtraRows);
        } catch (DataAccessException e) {
            throw new SQLException("countThisWalkChangesets failed", e);
        }
    }

    /**
     * The five {@code length(chash)=32} CHECK constraints (catalog-002-hygiene.xml
     * + catalog-013-1) and their owning table, in {@code nexus} schema.
     *
     * <p>RDR-191 Phase 4 (repoint-batch lane D5, bead nexus-o8dil.41 item 6 /
     * bead nexus-o8dil.43's F14c concern): VERIFIED NO CHANGE NEEDED here despite
     * {@code nexus.chunks_384/768/1024} collapsing into the unified {@code
     * nexus.chunks} table. The three {@code chunks_<dim>_chash_len_check}
     * entries below already reference constraint NAMES dropped by rdr180-2 —
     * true before RDR-191 and unchanged after it, since {@link
     * #preflightChashConstraints} probes by CONSTRAINT NAME
     * ({@code pg_constraint WHERE conname = ?}), not by whether the owning
     * TABLE exists. {@code nexus.chunks} is bytea-chash from creation and has
     * no {@code length(text)=32} concept at all (only the separate octet
     * family applies, added {@code NOT VALID} by {@code
     * vectors-004-unify-chunks.xml} step 4) — so this preflight's per-entry
     * lookup returns "not present" for the three {@code chunks_<dim>} rows
     * exactly as it did pre-repoint, a no-op either way. See that changeset's
     * own "LEN-CHECK FAMILY DISPOSITION" header note for the original
     * verification this comment reaffirms.
     */
    private static final Map<String, String> CHASH_LEN_CONSTRAINTS = new LinkedHashMap<>();
    static {
        CHASH_LEN_CONSTRAINTS.put("chunks_384_chash_len_check", "chunks_384");
        CHASH_LEN_CONSTRAINTS.put("chunks_768_chash_len_check", "chunks_768");
        CHASH_LEN_CONSTRAINTS.put("chunks_1024_chash_len_check", "chunks_1024");
        CHASH_LEN_CONSTRAINTS.put("catalog_document_chunks_chash_len_check", "catalog_document_chunks");
        // chash_index entry KEPT past the RDR-187 DROP — deliberately, and
        // contrary to the .5 pin's first reading: an AGED box upgrading to
        // head still crosses catalog-013 (VALIDATE) with the table present
        // en route to the rdr187-2 drop, and a genuinely-violating row there
        // must fail CLEAN at this preflight (named table/count/runbook), not
        // crash-loop at VALIDATE. The preflight is existence-gated, so on
        // modern boxes (constraint validated) and post-drop boxes (table
        // gone) it is a no-op. Pinned by SchemaMigratorIntegrationTest's
        // presentButViolating scenario.
        CHASH_LEN_CONSTRAINTS.put("chash_index_chash_len_check", "chash_index");
    }

    /**
     * nexus-c4143 (root fix): probe for present-but-VIOLATING chash-length
     * constraints BEFORE invoking Liquibase, and fail clean instead of letting
     * catalog-013-2/-3's bare {@code VALIDATE CONSTRAINT} crash-loop.
     *
     * <p>Tests 5/6/8 in {@code SchemaMigratorIntegrationTest} (ms57z / GH#1390,
     * nexus-4m6i0.1/.13) cover a constraint that is MISSING when the VALIDATE
     * changesets first run — the defensive {@code IF EXISTS} guards tolerate that
     * case. This preflight covers the OPPOSITE condition those guards do not
     * help with: the constraint EXISTS (added {@code NOT VALID}) but at least one
     * row genuinely violates it. A bare {@code VALIDATE CONSTRAINT} on a
     * genuinely-violating row is a hard Postgres ERROR regardless of any
     * {@code IF EXISTS} guard around it — same crash-loop mechanism, narrower
     * trigger condition.
     *
     * <p>Only constraints that EXIST and are NOT YET {@code convalidated} are
     * checked: an already-VALID constraint has already been proven, and a
     * missing one is handled separately (and correctly) by the defensive
     * per-table guards already shipped in catalog-013-3 / fk-002-7..11 /
     * fk-003-7..11. On a fresh, not-yet-migrated database none of these
     * constraints exist yet, so every check is a cheap no-op — this preflight
     * costs nothing on the common (happy) path.
     *
     * <p>Violation counting temporarily disables {@code FORCE ROW LEVEL
     * SECURITY} on the affected table (mirroring catalog-013-1b's own pattern)
     * so the count is TRUE regardless of RLS — closing the EXACT visibility gap
     * that caused the 2026-07-08 v0.1.33 production incident (nexus-1wjmq): the
     * migration role is the table owner but holds no BYPASSRLS, so a plain
     * {@code SELECT}/{@code DELETE}/{@code UPDATE} under FORCE RLS silently sees
     * zero rows while the subsequent {@code VALIDATE} (a physical scan, RLS-exempt
     * for DDL) still finds and crashes on the true violating rows. The toggle
     * happens on the SAME migration connection this method already holds
     * schema-owner rights on ({@code ds} is documented as
     * migration-capable/schema-owner), so no additional privilege is required.
     *
     * @throws MigrationException with the violating table/constraint/count named
     *     directly (so an operator does not need to reproduce the RLS-blind
     *     diagnostic dead-end the 2026-07-08 incident hit), or wrapping a genuine
     *     {@link SQLException} from the preflight query itself
     */
    // SANCTIONED RAW (nexus-mzuj9): two of this method's three query shapes have no
    // jOOQ typed-DSL form at all -- (1) pg_constraint is a Postgres SYSTEM CATALOG;
    // jOOQ codegen (service/pom.xml) only covers the nexus/t1 APPLICATION schemas, not
    // pg_catalog, so there is no generated table/field to select against; (2) ALTER
    // TABLE ... {NO} FORCE ROW LEVEL SECURITY is DDL with no jOOQ DSL equivalent
    // whatsoever (jOOQ does not model RLS toggles). The third shape (a per-table
    // SELECT COUNT(*) WHERE length(chash)!=32) COULD be expressed via the generated
    // table references, but is sanctioned as part of the SAME method rather than
    // split out: it runs bracketed between the FORCE-toggle DDL pair inside one
    // logical unit (mirrors the existing PgVectorRepository.rawVectorFetch /
    // TaxonomyCentroidRepository.annQuery precedent of sanctioning a whole method
    // rather than fragmenting a tightly-coupled raw-SQL sequence).
    private static void preflightChashConstraints(Connection conn) {
        List<String> violations = new ArrayList<>();
        try {
            for (Map.Entry<String, String> entry : CHASH_LEN_CONSTRAINTS.entrySet()) {
                String constraint = entry.getKey();
                String table = entry.getValue();

                boolean existsNotValid;
                try (PreparedStatement ps = conn.prepareStatement(
                        "SELECT NOT convalidated FROM pg_constraint WHERE conname = ?")) {
                    ps.setString(1, constraint);
                    try (ResultSet rs = ps.executeQuery()) {
                        existsNotValid = rs.next() && rs.getBoolean(1);
                    }
                }
                if (!existsNotValid) {
                    continue;
                }

                long violatingCount;
                boolean autoCommit = conn.getAutoCommit();
                conn.setAutoCommit(false);
                try {
                    try (Statement alter = conn.createStatement()) {
                        alter.execute("ALTER TABLE nexus." + table + " NO FORCE ROW LEVEL SECURITY");
                    }
                    try (PreparedStatement ps = conn.prepareStatement(
                            "SELECT COUNT(*) FROM nexus." + table + " WHERE length(chash) != 32");
                         ResultSet rs = ps.executeQuery()) {
                        rs.next();
                        violatingCount = rs.getLong(1);
                    }
                    try (Statement alter = conn.createStatement()) {
                        alter.execute("ALTER TABLE nexus." + table + " FORCE ROW LEVEL SECURITY");
                    }
                    conn.commit();
                } catch (SQLException e) {
                    // Postgres DDL is transactional: an uncommitted NO FORCE rolls back
                    // with everything else, so a mid-block failure leaves FORCE RLS
                    // exactly as it was found -- no separate restore step needed.
                    conn.rollback();
                    throw e;
                } finally {
                    conn.setAutoCommit(autoCommit);
                }

                if (violatingCount > 0) {
                    violations.add(table + " (" + constraint + "): " + violatingCount + " violating row(s)");
                    log.error(
                        "event=chash_preflight_violation table={} constraint={} count={}",
                        table, constraint, violatingCount);
                }
            }
        } catch (SQLException e) {
            throw new MigrationException("chash-length preflight query failed", e);
        }

        if (!violations.isEmpty()) {
            throw new MigrationException(
                "chash-length preflight found present-but-violating constraint(s) — refusing to run "
                + "Liquibase (would crash-loop on VALIDATE CONSTRAINT): " + String.join("; ", violations)
                + ". Remediate the violating rows per "
                + "https://github.com/Hellblazer/nexus/blob/main/docs/migration-runbook.md"
                + "#81-recovering-a-store-that-already-migrated-legacy-ids-nexus-pnwu0 before retrying.",
                null);
        }
    }

    /** The one zone this service's clocks agree on. */
    static final String UTC_ID = "UTC";

    /**
     * Pin the JVM default time zone to UTC (nexus-rph82).
     *
     * <p>Liquibase writes {@code databasechangelog.dateexecuted} (a
     * {@code TIMESTAMP WITHOUT TIME ZONE}) in the JVM's default zone. The
     * managed database runs GMT and every post-deploy audit windows that
     * column against {@code now()}, so a JVM-local write from a box seven
     * hours behind reads as seven hours in the past and the audit reports
     * "nothing was applied" for a walk that applied everything — measured
     * 2026-08-27 on a PITR fork of production (conexus-a4, v0.1.86). The
     * failure points the wrong way (towards a spurious rollback or re-run),
     * which is why it is fixed at the source rather than documented.
     *
     * <p>Lives here, not only in {@code Main}, so every entry point that runs
     * the changelog (the service, the migration rehearsals, the test suite)
     * gets the same clock. Idempotent; logs the transition when it happens.
     * {@code Main} also pins it before any datasource is built, because a
     * pooled connection negotiates its session zone at connect time.
     *
     * <p>{@code schema_migration_complete}'s counts no longer depend on this
     * pin (nexus-jl08t): they are keyed on {@code orderexecuted}, a plain
     * integer sequence, not on a {@code dateexecuted} clock window, so this
     * class's own reporting is immune to zone skew regardless of this pin's
     * state. The pin still matters for every EXTERNAL reader of
     * {@code dateexecuted} — conexus's own post-deploy audits window that
     * column against {@code now()} exactly as described above, and see the
     * same zone skew this fixes if it is ever reverted.
     */
    public static void pinJvmTimeZoneToUtc() {
        TimeZone before = TimeZone.getDefault();
        if (!UTC_ID.equals(before.getID())) {
            TimeZone.setDefault(TimeZone.getTimeZone(UTC_ID));
            System.setProperty("user.timezone", UTC_ID);
            log.info("event=schema_migration_jvm_timezone_pinned from={} to={}", before.getID(), UTC_ID);
        }
        assertJvmTimeZoneIsUtc();
    }

    /**
     * Boot-time verification that the pin above actually took (nexus-9gaj7).
     *
     * <p>{@code pinJvmTimeZoneToUtc()} unconditionally calls
     * {@link TimeZone#setDefault(TimeZone)}, but that call is a plain static
     * field write with no return signal — a platform that ignores it (a
     * {@code SecurityManager} rejecting the mutation, a native-image
     * runtime-init ordering surprise, or a later, un-reviewed
     * {@code TimeZone.setDefault} call racing this one on a JVM that does
     * spawn a second thread before {@code main()} finishes) would otherwise
     * fail SILENTLY: every caller downstream keeps assuming UTC (Liquibase's
     * {@code dateexecuted} stamp, {@link CatalogRepository#tsOrNull}, the
     * {@code SET TIME ZONE 'UTC'} session pin below) while the JVM's actual
     * clock reads local time. That is exactly
     * the nexus-rph82 failure shape one layer up: wrong-direction silence
     * that surfaces as "nothing was applied" hours after the fact, not as a
     * boot failure at the one moment it is cheap to diagnose.
     *
     * <p>Compares zone RULES rather than the zone ID string: {@code "UTC"},
     * {@code "Etc/UTC"}, {@code "GMT"}, and {@code "Z"} are all zero-offset,
     * no-DST zones that satisfy the actual requirement (every instant reads
     * the same wall-clock value system-wide) even though their IDs differ —
     * an ID-string compare would false-positive-fail a platform that
     * legitimately resolves the pin to one of those aliases.
     *
     * <p>Package-private for direct unit testing (SchemaMigratorTimeZoneAssertTest),
     * matching the {@link CatalogRepository#tsOrNull}-style test-seam
     * convention already established in this package.
     */
    static void assertJvmTimeZoneIsUtc() {
        ZoneId zone = ZoneId.systemDefault();
        if (!zone.getRules().equals(ZoneOffset.UTC.getRules())) {
            log.error("event=jvm_timezone_pin_failed observed_zone={} remedy=\"pass "
                    + "-Duser.timezone=UTC on the JVM/native-image launch command line "
                    + "and check for a later TimeZone.setDefault(...) call overriding "
                    + "the pin\"", zone.getId());
            throw new TimeZonePinFailedException(
                "JVM default zone is " + zone.getId() + " after pinJvmTimeZoneToUtc(); "
                + "expected UTC (or a zero-offset, no-DST alias). Pass "
                + "-Duser.timezone=UTC on the launch command line.");
        }
    }

    /**
     * Unchecked exception thrown when {@link #assertJvmTimeZoneIsUtc()} finds
     * the JVM's default zone is not UTC after {@link #pinJvmTimeZoneToUtc()}
     * attempted to pin it (nexus-9gaj7). {@code Main.java} catches this at its
     * own top-of-{@code main} pin call and calls {@code System.exit(1)}; inside
     * {@link #migrate(DataSource)} it is wrapped as a {@link MigrationException}
     * so that method's throws-contract stays uniform for its other callers
     * (migration rehearsals, the test suite).
     */
    public static final class TimeZonePinFailedException extends RuntimeException {
        public TimeZonePinFailedException(String message) {
            super(message);
        }
    }

    /**
     * Unchecked exception thrown when {@link #migrate(DataSource)} cannot
     * complete. {@code Main.java} catches this and calls {@code System.exit(1)}.
     */
    public static final class MigrationException extends RuntimeException {
        public MigrationException(String message, Throwable cause) {
            super(message, cause);
        }
    }
}
