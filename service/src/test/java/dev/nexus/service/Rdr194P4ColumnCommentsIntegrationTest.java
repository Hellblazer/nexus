package dev.nexus.service;

import org.testcontainers.containers.PostgreSQLContainer;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.sql.ResultSet;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-194 P4 (bead nexus-tk070.p4, Decisions D7/D8) — non-vacuity guard for
 * the recorded-reason column comments.
 *
 * <p>Comments are otherwise unverifiable by the suite (no other test reads
 * {@code pg_description}), so this is the cheap TDD tripwire the bead's
 * execution notes call for: RED before {@code fk-005-deliberately-loose-
 * edge-comments.xml} exists (or is wired into the master changelog), GREEN
 * after. Mirrors {@link TaxonomySchemaLiquibaseTest#docCountTrigger_functionsTriggersAndComment}'s
 * {@code pg_description} join pattern.
 *
 * <p>Asserts each of the eleven columns named in D7 (seven) / D8 (four) has
 * a non-empty comment carrying a keyword specific to its own reason. Each
 * needle below was hand-verified NOT to occur in any sibling column's
 * comment text (checked pairwise for the near-identical groups: the two
 * BIGSERIAL-PK id columns, the four D8 accepted-sentinel columns, and the
 * frecency/relevance_log grain-argument pair whose cross-references to each
 * other made a naive shared keyword — e.g. "GRAIN" — insufficient), so a
 * copy-paste swap of comment text between any two of the eleven columns
 * fails this test, not merely a blank or missing comment. Falsified by
 * direct execution (code-review-expert / substantive-critic round,
 * 2026-08-20): the SQL comments for {@code gc_audit.id} and
 * {@code claude_assisted_remediation_consents.id} were deliberately
 * swapped, the gate jar rebuilt, and this test went RED on the
 * {@code gc_audit.id} assertion before the swap was reverted — see T2
 * {@code nexus/tk070-p4-dev-notes-2026-08-20} for the transcript.
 */
class Rdr194P4ColumnCommentsIntegrationTest {

    @Test
    void everyDeliberatelyLooseEdgeColumn_hasANonEmptyReasonComment() throws Exception {
        try (PostgreSQLContainer<?> pg = PgContainerHelper.start()) {

            try (Connection su = pg.createConnection("")) {
                su.createStatement().execute(
                    "DO $$ BEGIN " +
                    "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                    "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; " +
                    "  END IF; " +
                    "END $$");

                Database db = DatabaseFactory.getInstance()
                    .findCorrectDatabaseImplementation(new JdbcConnection(su));
                Liquibase lb = new Liquibase(
                    "db/changelog/db.changelog-master.xml",
                    new ClassLoaderResourceAccessor(), db);
                lb.update(new Contexts());
            }

            try (Connection c = pg.createConnection("")) {
                // D7 (seven targets — chash_alias.old_bytes is NOT among them:
                // the table was dropped 2026-08-16, see fk-005's own header).
                // Needle "idx_hook_failures_etl_dedup" appears ONLY here.
                assertColumnComment(c, "hook_failures", "doc_id",
                    "THREE identity spaces", "idx_hook_failures_etl_dedup");
                // "frecency has no collection column" does not occur in
                // relevance_log.chunk_id's comment (which says "relevance_log
                // has no collection column" instead) — asymmetric on purpose,
                // this is the pair a naive shared "GRAIN" needle missed.
                assertColumnComment(c, "frecency", "chunk_id",
                    "frecency has no collection column", "ACROSS collections by design");
                assertColumnComment(c, "relevance_log", "chunk_id",
                    "relevance_log has no collection column, so an FK is not expressible");
                assertColumnComment(c, "pdf_chunks", "chunk_id",
                    "pdf pipeline", "pipeline-001-baseline.xml");
                assertColumnComment(c, "chash_remap", "old_id",
                    "FOREIGN identity space", "tautological");
                // Distinct self-referential sentences — "gc_audit is an
                // append-only audit trail" does not occur in the consents
                // column's comment (which names itself instead).
                assertColumnComment(c, "gc_audit", "id",
                    "gc_audit is an append-only audit trail");
                assertColumnComment(c, "claude_assisted_remediation_consents", "id",
                    "claude_assisted_remediation_consents is an append-only consent audit trail");

                // D8 (four accepted-sentinel targets) — each needle below is
                // exclusive to its own column, including across the two
                // relevance_log.* comments that cross-reference each other
                // ("unlike relevance_log.collection above" appears inside
                // session_id's own text, so a bare "relevance_log.collection"
                // needle would NOT have been safe for session_id).
                assertColumnComment(c, "gc_audit", "collection",
                    "free-text label describing what was reaped");
                assertColumnComment(c, "gc_audit", "actor",
                    "server-driven producers wired at catalog-033");
                assertColumnComment(c, "relevance_log", "collection",
                    "Named trigger:", "the collection registry");
                assertColumnComment(c, "relevance_log", "session_id",
                    "client session identifier", "no sessions table and none is planned");
            }
        }
    }

    private static void assertColumnComment(
            Connection c, String table, String column, String... mustContain) throws Exception {
        ResultSet rs = c.createStatement().executeQuery(
            "SELECT pgd.description FROM pg_description pgd " +
            "JOIN pg_class cl ON cl.oid = pgd.objoid " +
            "JOIN pg_namespace n ON n.oid = cl.relnamespace " +
            "JOIN pg_attribute a ON a.attrelid = cl.oid AND a.attnum = pgd.objsubid " +
            "WHERE n.nspname = 'nexus' AND cl.relname = '" + table + "' " +
            "AND a.attname = '" + column + "'");
        assertThat(rs.next())
            .as("nexus.%s.%s must carry a COMMENT ON COLUMN (RDR-194 P4)", table, column)
            .isTrue();
        String description = rs.getString("description");
        assertThat(description)
            .as("nexus.%s.%s comment must be non-empty", table, column)
            .isNotBlank();
        for (String needle : mustContain) {
            assertThat(description)
                .as("nexus.%s.%s comment must record its OWN reason (found: %s)",
                    table, column, description)
                .contains(needle);
        }
    }
}
