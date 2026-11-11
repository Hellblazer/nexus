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
 * <p><strong>Same setup as a walk.</strong> The session and Liquibase {@link Database} come from the very
 * helpers {@link SchemaMigrator#migrate} uses ({@code pinJvmTimeZoneToUtc}, {@code pinSessionTimeZoneToUtc},
 * {@code pinSearchPathToPublic}, {@code openLiquibaseDatabase}, {@code MASTER_CHANGELOG}, a
 * {@link ClassLoaderResourceAccessor}), so the bookkeeping lands in {@code public.databasechangelog} and a
 * later real boot resumes from it. Deliberately NOT done: the walk's preflights (split history, chash
 * constraints, disk, lock table), its advisory migration lock and the post-walk repair. The target database
 * is expected to be provisioned as the engine expects (roles, extensions, the SECURITY DEFINER relocation
 * pair), and the connecting role should be the one the engine migrates as, so object ownership matches.
 * The result is a half-migrated database that a real {@code SchemaMigrator.migrate} finishes.
 *
 * <p>Refuses (exit {@value #EXIT_REFUSED}) when the changeset id is not in the changelog, appears in more than
 * one place, or is already applied. A run that applies nothing new (target is the first changeset pending) is
 * allowed and still reports.
 *
 * <p>Run (the test classpath is needed, and {@code exec:java} does not compile, so compile first):
 * <pre>
 * scripts/mvnw-leased.sh -f service/pom.xml test-compile exec:java -Dexec.classpathScope=test \
 *   -Dexec.mainClass=dev.nexus.service.db.PreMigrationSchema \
 *   -Dexec.args="--jdbc-url jdbc:postgresql://localhost:5432/nexus --user nexus_admin --password ..."
 * </pre>
 * Credentials may instead come from {@code PREMIG_JDBC_URL}, {@code PREMIG_USER}, {@code PREMIG_PASSWORD}
 * (a flag wins), which keeps the password out of the process list.
 */
public final class PreMigrationSchema {

    /** The changeset whose first boot deletes legacy-width chash rows (hygiene-001-not-null.xml, step 5). */
    public static final String DEFAULT_STOP_BEFORE = "hygiene-001-5";

    public static final int EXIT_OK = 0;
    /** The walk or a query failed. */
    public static final int EXIT_FAILED = 1;
    /** Bad arguments. */
    public static final int EXIT_USAGE = 2;
    /** The named changeset is unknown, ambiguous, or already applied. */
    public static final int EXIT_REFUSED = 3;

    /** The state the tool reports after it stops. {@code tables} maps {@code nexus.<name>} to its kind. */
    public record Report(String stopBefore, int appliedNow, String lastChangesetApplied,
                         boolean stopChangesetApplied, String chunksChashDataType,
                         String chunksKind, String manifestKind) {

        /** One {@code key=value} per line, stable for a parser. */
        public String render() {
            return "stop_before=" + stopBefore + "\n"
                + "applied_this_run=" + appliedNow + "\n"
                + "last_changeset_applied=" + lastChangesetApplied + "\n"
                + "stop_changeset_applied=" + stopChangesetApplied + "\n"
                + "nexus.chunks.chash.data_type=" + chunksChashDataType + "\n"
                + "nexus.chunks=" + chunksKind + "\n"
                + "nexus.catalog_document_chunks=" + manifestKind + "\n";
        }
    }

    /** The named changeset cannot be a stop point; the message says why. */
    public static final class Refusal extends Exception {
        Refusal(String message) {
            super(message);
        }
    }

    private PreMigrationSchema() { /* tool */ }

    /**
     * Applies every pending changeset ordered before {@code stopBefore}, on {@code conn}.
     *
     * @throws Refusal when {@code stopBefore} is not in the changelog, is ambiguous, or is already applied
     */
    public static Report apply(Connection conn, String stopBefore)
            throws Refusal, LiquibaseException, SQLException {
        SchemaMigrator.pinJvmTimeZoneToUtc();
        SchemaMigrator.pinSessionTimeZoneToUtc(conn);
        SchemaMigrator.pinSearchPathToPublic(conn);
        Database database = SchemaMigrator.openLiquibaseDatabase(conn);
        // Liquibase.close() closes the connection, so the report is read before the try block ends.
        try (Liquibase liquibase = new Liquibase(
                SchemaMigrator.MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
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
            return report(conn, stopBefore, count);
        }
    }

    private static Report report(Connection conn, String stopBefore, int appliedNow) {
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
        return new Report(stopBefore, appliedNow, last == null ? "none" : last, stopApplied,
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
                err.println("missing value for " + args[i]);
                return usage(err);
            }
            String value = args[i + 1];
            switch (args[i]) {
                case "--jdbc-url" -> url = value;
                case "--user" -> user = value;
                case "--password" -> password = value;
                case "--stop-before" -> stopBefore = value;
                default -> {
                    err.println("unknown argument " + args[i]);
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
