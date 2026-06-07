package dev.nexus.service.codegen;

import io.zonky.test.db.postgres.embedded.EmbeddedPostgres;
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

import java.io.File;
import java.sql.Connection;

/**
 * RDR-152 bead nexus-gmiaf.6 — jOOQ codegen bootstrap.
 *
 * <p>Starts an io.zonky EmbeddedPostgres instance (no Docker required), applies
 * the Liquibase changelog to create the nexus schema, then runs the jOOQ
 * GenerationTool with PostgresDatabase to generate typed classes for the nexus
 * schema into {@code target/generated-sources/jooq}.
 *
 * <p>Invoked via the {@code -Pcodegen} Maven profile:
 * {@code mvn -Pcodegen process-test-classes} (from the service/ directory).
 * The profile uses exec-maven-plugin {@code exec:java} in the
 * {@code process-test-classes} phase with {@code classpathScope=test}, so all
 * test-scope dependencies (embedded-postgres, jooq-codegen, jooq-meta, liquibase)
 * are already on the classpath — no manual jar enumeration required.
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

        System.out.println("[jooq-codegen] Starting embedded Postgres...");
        EmbeddedPostgres pg = EmbeddedPostgres.builder().start();
        try {
            String url  = "jdbc:postgresql://localhost:" + pg.getPort() + "/postgres";
            String user = "postgres";
            String pass = "";

            // Apply Liquibase changelog via superuser connection.
            // DirectoryResourceAccessor resolves paths relative to the given directory,
            // so "db/changelog/db.changelog-master.xml" resolves from src/main/resources.
            System.out.println("[jooq-codegen] Applying Liquibase changelog...");
            try (Connection conn = pg.getPostgresDatabase().getConnection()) {
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
                        .withSchemata(new SchemaMappingType()
                            .withInputSchema("nexus")))
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
        } finally {
            pg.close();
            System.out.println("[jooq-codegen] Embedded Postgres stopped");
        }
    }
}
