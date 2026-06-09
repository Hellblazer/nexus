package dev.nexus.service.codegen;

import dev.nexus.service.PgContainerHelper;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.DirectoryResourceAccessor;
import org.jooq.codegen.GenerationTool;
import org.jooq.meta.jaxb.Configuration;
import org.jooq.meta.jaxb.Generate;
import org.jooq.meta.jaxb.Generator;
import org.jooq.meta.jaxb.Jdbc;
import org.jooq.meta.jaxb.SchemaMappingType;
import org.jooq.meta.jaxb.Target;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.File;
import java.sql.Connection;

/**
 * RDR-152 bead nexus-gmiaf.6 — jOOQ codegen bootstrap.
 *
 * <p>Starts a pgvector/pgvector:pg17 Testcontainers container (RDR-155 P1.0,
 * nexus-22man), applies the Liquibase changelog to create the nexus schema, then
 * runs the jOOQ GenerationTool with PostgresDatabase to generate typed classes for
 * the nexus schema into {@code src/main/generated/jooq}.
 *
 * <p>Invoked via the {@code -Pcodegen} Maven profile:
 * {@code mvn -Pcodegen process-test-classes} (from the service/ directory).
 * The profile uses exec-maven-plugin {@code exec:java} in the
 * {@code process-test-classes} phase with {@code classpathScope=test}, so all
 * test-scope dependencies (testcontainers, postgresql, jooq-codegen, jooq-meta,
 * liquibase) are already on the classpath — no manual jar enumeration required.
 *
 * <p>WHY NOT OPTION 1 (LiquibaseDatabase/H2): the changelog uses Postgres-only DDL
 * (TSVECTOR GENERATED ALWAYS AS STORED, ENABLE/FORCE ROW LEVEL SECURITY,
 * CREATE POLICY, PL/pgSQL DO blocks) that H2 cannot simulate. The PG features
 * are load-bearing; contorting the changelog is explicitly forbidden.
 *
 * <p>WHY NOT GMaven Plus: GMaven Plus 1.13.1 uses Groovy 4.0.21 which does not
 * support Java 25 class file format (major version 69). The exec:java approach
 * resolves deps through Maven and is fully self-contained.
 */
public class JooqCodegenBootstrap {

    public static void main(String[] args) throws Exception {
        String outputDir = args.length > 0 ? args[0]
            : "target/generated-sources/jooq";
        // src/main/resources directory — resolved relative to process working dir
        // (must be the Maven project base dir, i.e. service/)
        String resourcesDir = args.length > 1 ? args[1]
            : "src/main/resources";

        System.out.println("[jooq-codegen] Starting pgvector container...");
        PostgreSQLContainer<?> pg = PgContainerHelper.start();
        boolean success = false;
        try {
            String url  = pg.getJdbcUrl();
            String user = PgContainerHelper.USERNAME;
            String pass = PgContainerHelper.PASSWORD;

            // Apply Liquibase changelog via superuser connection.
            // DirectoryResourceAccessor resolves paths relative to the given directory,
            // so "db/changelog/db.changelog-master.xml" resolves from src/main/resources.
            System.out.println("[jooq-codegen] Applying Liquibase changelog...");
            try (Connection conn = pg.createConnection("")) {
                // The master changelog's runAlways grants-nexus-svc changeset
                // (RDR-152 nexus-net63) is fail-loud: it GRANTs to nexus_svc and
                // errors if the role is absent. Codegen only needs the schema,
                // not grants, but Liquibase applies the whole master changelog —
                // so create the role first (idempotent) to satisfy the grant.
                try (var st = conn.createStatement()) {
                    st.execute(
                        "DO $$ BEGIN "
                        + "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='nexus_svc') THEN "
                        + "CREATE ROLE nexus_svc NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS LOGIN; "
                        + "END IF; END $$");
                }
                liquibase.database.Database lbDb = DatabaseFactory.getInstance()
                    .findCorrectDatabaseImplementation(new JdbcConnection(conn));
                Liquibase liquibase = new Liquibase(
                    "db/changelog/db.changelog-master.xml",
                    new DirectoryResourceAccessor(new File(resourcesDir).getCanonicalFile().toPath()),
                    lbDb);
                liquibase.update(new Contexts());
            }
            System.out.println("[jooq-codegen] Liquibase changelog applied");

            // Run jOOQ GenerationTool against the live PostgresDatabase
            System.out.println("[jooq-codegen] Running GenerationTool (PostgresDatabase)...");
            new File(outputDir).mkdirs();

            Configuration config = new Configuration()
                .withJdbc(new Jdbc()
                    .withDriver("org.postgresql.Driver")
                    .withUrl(url)
                    .withUser(user)
                    .withPassword(pass))
                .withGenerator(new Generator()
                    .withDatabase(new org.jooq.meta.jaxb.Database()
                        .withName("org.jooq.meta.postgres.PostgresDatabase")
                        .withIncludes(".*")
                        .withExcludes("databasechangelog|databasechangeloglock")
                        .withSchemata(
                            new SchemaMappingType().withInputSchema("nexus"),
                            new SchemaMappingType().withInputSchema("t1")))
                    .withGenerate(new Generate()
                        .withPojos(true)
                        .withDaos(false)
                        .withFluentSetters(true)
                        .withJavaTimeTypes(true))
                    .withTarget(new Target()
                        .withPackageName("dev.nexus.service.jooq")
                        .withDirectory(outputDir)));

            GenerationTool.generate(config);
            System.out.println("[jooq-codegen] Done: " + outputDir);
            success = true;
        } finally {
            System.out.println("[jooq-codegen] Stopping pgvector container...");
            pg.stop();
            // Force JVM exit so Testcontainers background threads (Ryuk reaper) do not
            // keep the Maven exec:java goal alive and trigger an InterruptedException on
            // shutdown. Exit NON-ZERO if generation threw — otherwise a codegen failure
            // would exit 0, the build would pass, and the drift guard would pass vacuously
            // against stale committed sources (AC-3 review, nexus-22man).
            System.exit(success ? 0 : 1);
        }
    }
}
