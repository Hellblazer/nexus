// SPDX-License-Identifier: AGPL-3.0-or-later
package dev.nexus.service.db;

import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.Scope;
import liquibase.changelog.ChangeSet;
import liquibase.database.Database;
import liquibase.exception.LiquibaseException;
import liquibase.resource.ClassLoaderResourceAccessor;
import liquibase.ui.LoggerUIService;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;

import java.io.PrintStream;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * Test-scope tool (nexus-f3yqd): builds the schema an install had BEFORE a named changeset, by applying the
 * real master changelog up to, but not including, that changeset. The engine has no way to stop Liquibase
 * early and must not grow one; this is the stop, kept out of {@code src/main}.
 *
 * <p>Built for the end-to-end test of the client guard that refuses the first engine boot on an install whose
 * schema has not yet applied {@code hygiene-001-5} (which DELETEs every non-32-byte chash row of
 * {@code nexus.chunks} and {@code nexus.catalog_document_chunks}). The default stop is that changeset.
 *
 * <p><strong>SCRATCH DATABASES ONLY. THIS IS DESTRUCTIVE.</strong> It runs real changesets, several with data
 * effects (hygiene-001-1..3 DELETE rows, catalog-029-1, catalog-025-0, fk-001-* and others). Pointed at a real
 * store it performs exactly the deletes the guard exists to prevent. So it refuses any database it did not
 * make: it proceeds only when {@code public.databasechangelog} holds no rows (and no {@code nexus.chunks}
 * exists), or when the marker table {@code public.premigration_schema_scratch}, which this tool creates on
 * its first run, is present. The marker is what lets a LATER run apply real changesets (with their data effects)
 * to that database, so it is a licence to destroy data there: never create or leave it on a database that holds
 * anything you want to keep. It stays on the database through later real walks.
 *
 * <p><strong>Fidelity limits.</strong> The result is a schema of the right SHAPE, not an old install's path to
 * it:
 * <ul>
 *   <li>The walk runs over EMPTY tables, so every data-dependent changeset before the stop (the vectors-004-1
 *       copy and its cross-shard guard, catalog-029-1, catalog-025-0, taxonomy-012-2) ran on zero rows. Legacy
 *       rows are seeded after the tool returns.</li>
 *   <li>The state is mid-walk, not any released version's. At the default stop, hygiene-001-1..4 and
 *       service-tokens-006-1 are already applied, which a real pre-0.1.90 store lacks; and the runAlways
 *       grants at the tail of the master changelog are NOT applied, which a real store has.</li>
 *   <li>The history is fresh: new checksums, no old-engine rows, one orderexecuted run.</li>
 *   <li>One era shape per stop point. The default stop yields the unified, post-vectors-004 shape: plain
 *       {@code nexus.chunks}, bytea chash, {@code chunks_chash_octet_check} NOT VALID. It cannot yield the
 *       dim-sharded era (text chash in {@code chunks_<dim>}). For that, stop earlier, for example
 *       {@code --stop-before rdr180-3-convert-chunks-384}, seed the legacy rows into {@code chunks_<dim>},
 *       then run the tool again with a later stop (or a real walk) so the changesets that decode, unify and
 *       delete run over data. A second run is allowed because the marker is present.</li>
 *   <li>Seeding a legacy row needs the octet CHECKs dropped and re-added NOT VALID (a new 16-byte row is
 *       otherwise rejected); the end state matches an aged store's.</li>
 *   <li>hygiene-001-5 deletes rows whose chash is not 32 BYTES. A 32-byte non-hex legacy value survives it
 *       by design; only 16-byte (hex-decoded) and longer (UTF-8 carry-over) values are deleted.</li>
 * </ul>
 *
 * <p><strong>Same setup as a walk.</strong> The session and Liquibase {@link Database} come from the very
 * helpers {@link SchemaMigrator#migrate} uses ({@code pinJvmTimeZoneToUtc}, {@code pinSessionTimeZoneToUtc},
 * {@code pinSearchPathToPublic}, {@code openLiquibaseDatabase}, {@code refuseSplitHistory},
 * {@code MASTER_CHANGELOG}, a {@link ClassLoaderResourceAccessor}), and it holds the walk's advisory migration
 * lock ({@link SchemaMigrator#MIGRATION_ADVISORY_LOCK_KEY}) for the run, so the bookkeeping lands in
 * {@code public.databasechangelog}, no engine or second tool walks beside it, and a later real boot resumes
 * from the history. Deliberately NOT done: the walk's chash-constraint, disk and lock-table preflights, its
 * stale changelog-lock release, and the post-walk repair. The target database is expected to be provisioned as
 * the engine expects (roles, extensions, the SECURITY DEFINER relocation pair), and the connecting role should
 * be the one the engine migrates as, so object ownership matches.
 *
 * <p>Refuses (exit {@value #EXIT_REFUSED}) when: the changeset id is not in the changelog, appears in more
 * than one place, or is already applied; another migrator holds the advisory lock; the database was not made by
 * this tool (above); or a role-named schema holds a Liquibase history while {@code public} does not (split
 * history). A run that applies nothing new is allowed and still reports. Exit {@value #EXIT_FAILED} if the stop
 * changeset turns out to be applied after the update.
 *
 * <p>Run (the test classpath is needed, and {@code exec:java} does not compile, so compile first). Credentials
 * come from the environment, which keeps the password out of the process list and the build log:
 * <pre>
 * PREMIG_JDBC_URL=jdbc:postgresql://localhost:5432/nexus PREMIG_USER=nexus_admin PREMIG_PASSWORD=... \
 *   scripts/mvnw-leased.sh test-compile exec:java -Dexec.classpathScope=test \
 *     -Dexec.mainClass=dev.nexus.service.db.PreMigrationSchema \
 *     -Dexec.args="--stop-before hygiene-001-5"
 * </pre>
 * Flags {@code --jdbc-url}, {@code --user} and {@code --password} also work and win over the environment. Do
 * not pass {@code -f}: the wrapper runs from {@code service/}. {@code test-compile} runs the jOOQ code
 * generation only when its input hash changed (the {@code nexus.jooq.*.skip} properties are computed, never
 * set by hand; {@code -Pprebuilt-jooq} skips generation on purpose); when it does run it boots a Docker
 * container. Once compiled, {@code exec:java} alone is enough.
 */
public final class PreMigrationSchema {

    /** The changeset whose first boot deletes legacy-width chash rows (hygiene-001-not-null.xml, step 5). */
    public static final String DEFAULT_STOP_BEFORE = "hygiene-001-5";

    /** Created by the tool's first run; its presence is what lets a later run proceed on a database with history. */
    public static final String SCRATCH_MARKER = "premigration_schema_scratch";

    public static final int EXIT_OK = 0;
    /** The walk or a query failed, or the stop changeset ended up applied. */
    public static final int EXIT_FAILED = 1;
    /** Bad arguments. */
    public static final int EXIT_USAGE = 2;
    /** Refused before changing anything; see the class javadoc. */
    public static final int EXIT_REFUSED = 3;

    /**
     * The state the tool reports after it stops. {@code pendingAheadOfStop} is how many unrun changesets Liquibase
     * planned ahead of the stop, runAlways re-runs and self-skipping ones included: it is a plan size, not a count
     * of rows applied, and a re-run on an already-built database reports the runAlways ones again.
     */
    public record Report(String stopBefore, int pendingAheadOfStop, String lastChangesetApplied,
                         boolean stopChangesetApplied, String chunksChashDataType,
                         String chunksKind, String manifestKind) {

        /** One {@code key=value} per line, stable for a parser. */
        public String render() {
            return "stop_before=" + stopBefore + "\n"
                + "pending_ahead_of_stop=" + pendingAheadOfStop + "\n"
                + "last_changeset_applied=" + lastChangesetApplied + "\n"
                + "stop_changeset_applied=" + stopChangesetApplied + "\n"
                + "nexus.chunks.chash.data_type=" + chunksChashDataType + "\n"
                + "nexus.chunks=" + chunksKind + "\n"
                + "nexus.catalog_document_chunks=" + manifestKind + "\n";
        }
    }

    /** The run is refused before it changes anything; the message says why. */
    public static final class Refusal extends Exception {
        Refusal(String message) {
            super(message);
        }
    }

    private PreMigrationSchema() { /* tool */ }

    /**
     * Applies every pending changeset ordered before {@code stopBefore}, on {@code conn}.
     *
     * @throws Refusal see the class javadoc: unknown, ambiguous or already-applied id, lock held elsewhere,
     *     database not made by this tool, split history
     * @throws IllegalStateException when the stop changeset is applied after the update (a concurrent walker)
     */
    public static Report apply(Connection conn, String stopBefore)
            throws Refusal, LiquibaseException, SQLException {
        return apply(conn, stopBefore, c -> { });
    }

    /**
     * As {@link #apply(Connection, String)}, with a test seam: {@code afterRelease} is called with the still-open
     * connection right after the advisory lock was released on it, so a test can read the lock state from the
     * tool's own session. It is not called when the connection is already closed (Liquibase closes it last).
     */
    static Report apply(Connection conn, String stopBefore, java.util.function.Consumer<Connection> afterRelease)
            throws Refusal, LiquibaseException, SQLException {
        SchemaMigrator.pinJvmTimeZoneToUtc();
        SchemaMigrator.pinSessionTimeZoneToUtc(conn);
        SchemaMigrator.pinSearchPathToPublic(conn);
        // Same order as a walk: the checks run on the plain autocommit session, before Liquibase takes the
        // connection over (it turns autocommit off, and one failed statement would then poison every later one).
        if (!tryAdvisoryLock(conn)) {
            throw new Refusal("another migrator holds the migration advisory lock (key "
                + SchemaMigrator.MIGRATION_ADVISORY_LOCK_KEY + "); a walk is in progress on this database");
        }
        try {
            refuseSplitHistory(conn);
            refuseUnlessScratch(conn);
            Database database = SchemaMigrator.openLiquibaseDatabase(conn);
            // Liquibase.close() closes the connection, so the report is read before the try block ends.
            try (Liquibase liquibase = new Liquibase(
                    SchemaMigrator.MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                try {
                    List<ChangeSet> declared = liquibase.getDatabaseChangeLog().getChangeSets().stream()
                        .filter(cs -> stopBefore.equals(cs.getId())).toList();
                    if (declared.isEmpty()) {
                        throw new Refusal("changeset '" + stopBefore + "' is not in " + SchemaMigrator.MASTER_CHANGELOG);
                    }
                    if (declared.size() > 1) {
                        throw new Refusal("changeset id '" + stopBefore + "' appears " + declared.size()
                            + " times in " + SchemaMigrator.MASTER_CHANGELOG + " (files: "
                            + declared.stream().map(ChangeSet::getFilePath).toList() + "); a stop point must be unique");
                    }
                    markScratch(conn);
                    conn.commit();
                    List<ChangeSet> unrun = liquibase.listUnrunChangeSets(new Contexts(), new LabelExpression());
                    int idx = -1;
                    for (int i = 0; i < unrun.size(); i++) {
                        if (stopBefore.equals(unrun.get(i).getId())) {
                            idx = i;
                            break;
                        }
                    }
                    if (idx < 0) {
                        throw new Refusal("changeset '" + stopBefore + "' is already applied to this database; "
                            + "a pre-migration schema cannot be built on top of it");
                    }
                    final int count = idx;
                    try {
                        // Liquibase's per-changeset "Running Changeset" lines go to its logger so stdout stays the report.
                        Scope.child(Scope.Attr.ui, new LoggerUIService(),
                            () -> liquibase.update(count, new Contexts(), new LabelExpression()));
                    } catch (LiquibaseException | RuntimeException e) {
                        throw e;
                    } catch (Exception e) {
                        throw new LiquibaseException(e);
                    }
                    Report report = report(conn, stopBefore, count);
                    if (report.stopChangesetApplied()) {
                        throw new IllegalStateException("changeset '" + stopBefore + "' is applied after the update; "
                            + "the plan changed under the tool, so this database is not at the requested stop");
                    }
                    return report;
                } finally {
                    release(conn, afterRelease); // before close(), which ends the session anyway
                }
            }
        } finally {
            release(conn, afterRelease); // covers a refusal before Liquibase took the connection
        }
    }

    private static boolean tryAdvisoryLock(Connection conn) {
        return Boolean.TRUE.equals(DSL.using(conn, SQLDialect.POSTGRES)
            .select(DSL.function("pg_try_advisory_lock", SQLDataType.BOOLEAN,
                DSL.val(SchemaMigrator.MIGRATION_ADVISORY_LOCK_KEY)))
            .fetchOne(0, Boolean.class));
    }

    private static void release(Connection conn, java.util.function.Consumer<Connection> afterRelease) {
        if (advisoryUnlock(conn)) {
            afterRelease.accept(conn);
        }
    }

    /**
     * Best effort: the lock is session-level and ends with the session, so a failure here changes nothing.
     * Returns whether the unlock statement ran (false on a closed or failed connection).
     */
    private static boolean advisoryUnlock(Connection conn) {
        try {
            DSL.using(conn, SQLDialect.POSTGRES)
                .select(DSL.function("pg_advisory_unlock", SQLDataType.BOOLEAN,
                    DSL.val(SchemaMigrator.MIGRATION_ADVISORY_LOCK_KEY)))
                .fetch();
            return true;
        } catch (RuntimeException ignored) {
            return false; // session ends with the connection
        }
    }

    /** The walk's own split-history refusal, as a {@link Refusal}. */
    private static void refuseSplitHistory(Connection conn) throws Refusal, SQLException {
        try {
            SchemaMigrator.refuseSplitHistory(conn);
        } catch (SchemaMigrator.MigrationException e) {
            throw new Refusal(e.getMessage());
        }
    }

    /** Proceed only on an empty database or one this tool already marked; see the class javadoc. */
    private static void refuseUnlessScratch(Connection conn) throws Refusal {
        DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
        if (relationExists(ctx, "public", SCRATCH_MARKER)) {
            return;
        }
        int historyRows = relationExists(ctx, "public", "databasechangelog")
            ? ctx.fetchCount(DSL.table(DSL.name("public", "databasechangelog"))) : 0;
        if (historyRows > 0) {
            throw new Refusal("public.databasechangelog holds " + historyRows + " rows and public." + SCRATCH_MARKER
                + " is absent: this database was not made by this tool. It runs real changesets with data effects "
                + "and is for scratch databases only");
        }
        if (relationExists(ctx, "nexus", "chunks")) {
            throw new Refusal("nexus.chunks exists with no Liquibase history: not a database this tool can "
                + "build on; use an empty scratch database");
        }
    }

    private static void markScratch(Connection conn) {
        DSL.using(conn, SQLDialect.POSTGRES)
            .createTableIfNotExists(DSL.name("public", SCRATCH_MARKER))
            .column("created_at", SQLDataType.TIMESTAMPWITHTIMEZONE.nullable(false).defaultValue(DSL.currentOffsetDateTime()))
            .execute();
    }

    private static boolean relationExists(DSLContext ctx, String schema, String name) {
        return ctx.select(DSL.function("to_regclass", SQLDataType.VARCHAR, DSL.val(schema + "." + name)))
            .fetchOne(0, String.class) != null;
    }

    private static Report report(Connection conn, String stopBefore, int pendingAheadOfStop) {
        DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
        var dcl = DSL.table(DSL.name("public", "databasechangelog"));
        var id = DSL.field(DSL.name("id"), String.class);
        // RERAN rows are runAlways changesets re-stamped by a later run; they would mask the real frontier.
        String last = ctx.select(id).from(dcl)
            .where(DSL.field(DSL.name("exectype"), String.class).ne("RERAN"))
            .orderBy(DSL.field(DSL.name("orderexecuted"), Integer.class).desc()).limit(1)
            .fetchOne(id);
        boolean stopApplied = ctx.fetchExists(dcl, id.eq(stopBefore));
        String chashType = ctx.select(DSL.field(DSL.name("data_type"), String.class))
            .from(DSL.table(DSL.name("information_schema", "columns")))
            .where(DSL.field(DSL.name("table_schema"), String.class).eq("nexus"))
            .and(DSL.field(DSL.name("table_name"), String.class).eq("chunks"))
            .and(DSL.field(DSL.name("column_name"), String.class).eq("chash"))
            .fetchOne(DSL.field(DSL.name("data_type"), String.class));
        return new Report(stopBefore, pendingAheadOfStop, last == null ? "none" : last, stopApplied,
            chashType == null ? "absent" : chashType,
            relationKind(ctx, "chunks"), relationKind(ctx, "catalog_document_chunks"));
    }

    /** {@code plain}, {@code partitioned}, {@code other:<relkind>} or {@code absent} for {@code nexus.<name>}. */
    private static String relationKind(DSLContext ctx, String name) {
        var relkind = DSL.field(DSL.name("c", "relkind"), String.class);
        String kind = ctx.select(relkind)
            .from(DSL.table(DSL.name("pg_catalog", "pg_class")).as("c"))
            .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n"))
            .on(DSL.field(DSL.name("c", "relnamespace")).eq(DSL.field(DSL.name("n", "oid"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
            .and(DSL.field(DSL.name("c", "relname"), String.class).eq(name))
            .fetchOne(relkind);
        if (kind == null) {
            return "absent";
        }
        return switch (kind) {
            case "r" -> "plain";
            case "p" -> "partitioned";
            default -> "other:" + kind;
        };
    }

    /** CLI core, separated from {@code System.exit} so a test can read the exit code and both streams. */
    static int run(String[] args, Map<String, String> env, PrintStream out, PrintStream err) {
        String url = env.get("PREMIG_JDBC_URL");
        String user = env.get("PREMIG_USER");
        String password = env.get("PREMIG_PASSWORD");
        String stopBefore = DEFAULT_STOP_BEFORE;
        for (int i = 0; i < args.length; i++) {
            if (i + 1 >= args.length) {
                err.println("missing value for " + keyOnly(args[i]));
                return usage(err);
            }
            String value = args[i + 1];
            switch (args[i]) {
                case "--jdbc-url" -> url = value;
                case "--user" -> user = value;
                case "--password" -> password = value;
                case "--stop-before" -> stopBefore = value;
                default -> {
                    err.println("unknown argument " + keyOnly(args[i]));
                    return usage(err);
                }
            }
            i++;
        }
        List<String> missing = new ArrayList<>();
        if (url == null || url.isBlank()) missing.add("--jdbc-url");
        if (user == null || user.isBlank()) missing.add("--user");
        if (!missing.isEmpty()) {
            err.println("missing " + String.join(", ", missing));
            return usage(err);
        }
        try (Connection conn = DriverManager.getConnection(url, user, password == null ? "" : password)) {
            out.print(apply(conn, stopBefore).render());
            return EXIT_OK;
        } catch (Refusal e) {
            err.println("REFUSED: " + e.getMessage());
            return EXIT_REFUSED;
        } catch (LiquibaseException | SQLException | RuntimeException e) {
            err.println("FAILED: " + e);
            return EXIT_FAILED;
        }
    }

    /** Only the flag name of an argument: a {@code --password=secret} or a stray value is never echoed. */
    private static String keyOnly(String arg) {
        if (!arg.startsWith("--")) {
            return "(not a flag)";
        }
        int eq = arg.indexOf('=');
        return eq < 0 ? arg : arg.substring(0, eq);
    }

    private static int usage(PrintStream err) {
        err.println("usage: PreMigrationSchema --jdbc-url <url> --user <user> [--password <pw>] "
            + "[--stop-before <changesetId, default " + DEFAULT_STOP_BEFORE + ">]");
        err.println("env fallbacks: PREMIG_JDBC_URL PREMIG_USER PREMIG_PASSWORD");
        return EXIT_USAGE;
    }

    public static void main(String[] args) {
        int rc = run(args, System.getenv(), System.out, System.err);
        System.out.flush();
        // exec:java runs main on a thread inside Maven's JVM, so a non-zero status must be an explicit exit.
        if (rc != EXIT_OK) {
            System.exit(rc);
        }
    }
}
