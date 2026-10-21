package dev.nexus.service;

import org.junit.jupiter.api.Test;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Container-free pins on {@link PgContainerHelper}'s changeset-text extraction (nexus-cbo4a batch 10): it is the
 * entry point of {@code replayChangeset}, {@code replayChangesetRollback}, {@code changesetForwardText} and
 * {@code changesetRollbackText}, and every replay test trusts that it picks the right text and refuses the wrong
 * input. Reads the real product changelog XML; no Docker.
 *
 * <p>Not pinned here, because no product changeset has the shape (scanned 2026-10-19 over db/changelog): a
 * {@code <rollback>} with a non-{@code <sql>} child, one mixing {@code <sql>} children with loose text, and a
 * changeset with several {@code <rollback>} elements. Those refusals and the newline join are unexercised by real
 * data, and a synthetic changelog cannot be fed through the helper, which accepts product changelogs only.
 * Likewise the test-classes shadow refusal needs a shadowing resource, which would pollute the test resources.
 */
class PgContainerHelperChangesetTextTest {

    private static final String TRIGGER_CHANGELOG = "db/changelog/taxonomy-003-doc-count-trigger.xml";

    @Test
    void forwardText_isTheChangesetsOwnSql_andNotItsRollback() throws Exception {
        String text = PgContainerHelper.changesetForwardText(TRIGGER_CHANGELOG, "taxonomy-003-1");
        assertThat(text)
            .contains("CREATE OR REPLACE FUNCTION nexus.topics_doc_count_recount_ins()")
            .contains("FROM new_rows")
            .as("the forward body of 003-1 is the INSERT-path function, not the DELETE-path one")
            .doesNotContain("recount_del")
            .as("a forward body never picks up the changeset's <rollback>")
            .doesNotContain("DROP FUNCTION");
    }

    @Test
    void rollbackText_isWhatTheChangesetsRollbackSays() throws Exception {
        assertThat(PgContainerHelper.changesetRollbackText(TRIGGER_CHANGELOG, "taxonomy-003-1").strip())
            .isEqualTo("DROP FUNCTION IF EXISTS nexus.topics_doc_count_recount_ins()");
        assertThat(PgContainerHelper.changesetRollbackText(TRIGGER_CHANGELOG, "taxonomy-003-3"))
            .as("a multi-statement loose-text rollback comes back whole, in document order")
            .containsSubsequence(
                "DROP TRIGGER IF EXISTS trg_topic_assignments_doc_count_del ON nexus.topic_assignments;",
                "DROP TRIGGER IF EXISTS trg_topic_assignments_doc_count_ins ON nexus.topic_assignments;");
    }

    @Test
    void emptyRollback_throwsRatherThanReplayNothing() {
        // taxonomy-003-4 declares <rollback/>
        assertThatThrownBy(() -> PgContainerHelper.changesetRollbackText(TRIGGER_CHANGELOG, "taxonomy-003-4"))
            .isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("taxonomy-003-4")
            .hasMessageContaining("ROLLBACK");
    }

    @Test
    void missingChangesetId_throws() {
        assertThatThrownBy(() -> PgContainerHelper.changesetForwardText(TRIGGER_CHANGELOG, "taxonomy-003-999"))
            .isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("changeset not found")
            .hasMessageContaining("taxonomy-003-999");
    }

    @Test
    void missingChangelog_throws() {
        assertThatThrownBy(() -> PgContainerHelper.changesetForwardText("db/changelog/no-such-file.xml", "x"))
            .isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("changelog not found");
    }

    @Test
    void testChangelogPath_isRefused() {
        assertThatThrownBy(() -> PgContainerHelper.changesetForwardText(
                "db/changelog-test/db.changelog-test-reindex-index.xml", "anything"))
            .isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("PRODUCT changelogs");
    }

    @Test
    void pathOutsideTheProductPrefix_orWithDotDot_isRefused() {
        assertThatThrownBy(() -> PgContainerHelper.changesetForwardText("taxonomy-003-doc-count-trigger.xml", "taxonomy-003-1"))
            .isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("PRODUCT changelogs");
        assertThatThrownBy(() -> PgContainerHelper.changesetForwardText(
                "db/changelog/../changelog-test/db.changelog-test-reindex-index.xml", "anything"))
            .isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("PRODUCT changelogs");
    }

    @Test
    void substitutingReplay_whoseTextIsAbsent_throwsBeforeAnythingIsSent() {
        // The edit runs before a statement is created, so no connection is needed to see the refusal.
        assertThatThrownBy(() -> PgContainerHelper.replayChangesetSubstituting(
                null, TRIGGER_CHANGELOG, "taxonomy-003-1", "no such text in the changeset", "x"))
            .isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("no longer contains the substituted text");
    }

    @Test
    void aChangesetCarryingDbmsOrEndDelimiter_isRefused() throws Exception {
        String ns = "http://www.liquibase.org/xml/ns/dbchangelog";
        for (String changeSet : List.of(
                "<changeSet xmlns='" + ns + "' id='a' dbms='postgresql'><sql>x</sql></changeSet>",
                "<changeSet xmlns='" + ns + "' id='b'><sql dbms='h2'>x</sql></changeSet>",
                "<changeSet xmlns='" + ns + "' id='c'><sql endDelimiter='/'>x</sql></changeSet>")) {
            var factory = javax.xml.parsers.DocumentBuilderFactory.newInstance();
            factory.setNamespaceAware(true);
            var cs = factory.newDocumentBuilder()
                .parse(new org.xml.sax.InputSource(new java.io.StringReader(changeSet))).getDocumentElement();
            assertThatThrownBy(() -> PgContainerHelper.refuseUnreplayable(cs, "db/changelog/x.xml"))
                .as(changeSet).isInstanceOf(IllegalStateException.class).hasMessageContaining("dbms or endDelimiter");
        }
        var factory = javax.xml.parsers.DocumentBuilderFactory.newInstance();
        factory.setNamespaceAware(true);
        var plain = factory.newDocumentBuilder().parse(new org.xml.sax.InputSource(new java.io.StringReader(
            "<changeSet xmlns='" + ns + "' id='d'><sql splitStatements='false'>x</sql></changeSet>"))).getDocumentElement();
        PgContainerHelper.refuseUnreplayable(plain, "db/changelog/x.xml");
    }
}
