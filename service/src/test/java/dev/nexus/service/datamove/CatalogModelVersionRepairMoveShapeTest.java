// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;
import org.w3c.dom.Element;
import org.w3c.dom.Node;

import javax.xml.XMLConstants;
import javax.xml.parsers.DocumentBuilderFactory;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Pattern;

/**
 * nexus-f3yqd.2.23: the two changesets of the first registered move keep the shape RDR-246 states. DataMoveOwnerReadPatternShapeTest
 * pins the fixture files, "not your copy of them"; this reads the real ones: vectors-064 is one INSERT into nexus.data_moves, no
 * guard and no policy, with the verification the worker will accept; vectors-065 is the guarded owner-read policy alone, with
 * vectors-029's predicate word for word. The integration test runs them; this is what fails before a database is involved.
 */
class CatalogModelVersionRepairMoveShapeTest {

    private static final String REGISTER = "db/changelog/vectors-064-register-model-version-repair-move.xml";
    private static final String POLICY = "db/changelog/vectors-065-catalog-collections-owner-read.xml";
    private static final String LABEL = "repair-catalog-collection-model-version";
    private static final Pattern ROW_DML = Pattern.compile("(?is)\\b(?:update|delete\\s+from|insert\\s+into|merge\\s+into)\\b");

    private static Element changelog(String resource) throws Exception {
        try (InputStream in = CatalogModelVersionRepairMoveShapeTest.class.getClassLoader().getResourceAsStream(resource)) {
            assertThat(in).as(resource).isNotNull();
            var dbf = DocumentBuilderFactory.newInstance();
            dbf.setNamespaceAware(true);
            dbf.setFeature(XMLConstants.FEATURE_SECURE_PROCESSING, true);
            dbf.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true);
            return dbf.newDocumentBuilder().parse(in).getDocumentElement();
        }
    }

    private static List<Element> children(Element parent, String name) {
        List<Element> out = new ArrayList<>();
        for (Node n = parent.getFirstChild(); n != null; n = n.getNextSibling()) {
            if (n instanceof Element e && name.equals(e.getLocalName())) out.add(e);
        }
        return out;
    }

    private static String withoutLiterals(String sql) {
        return sql.replaceAll("(?s)\\$(\\w*)\\$.*?\\$\\1\\$", " ").replaceAll("(?s)'(?:[^']|'')*'", "''");
    }

    private static String forwardSql(Element changeSet) {
        StringBuilder sb = new StringBuilder();
        for (Element sql : children(changeSet, "sql")) sb.append(sql.getTextContent()).append('\n');
        return sb.toString();
    }

    @Test
    void theRegisteringChangesetIsOneInsertWithNoGuardAndNoPolicy_andItsLabelAndVerificationAreOnesTheWorkerAccepts() throws Exception {
        var sets = children(changelog(REGISTER), "changeSet");
        assertThat(sets).as("one changeset").hasSize(1);
        var cs = sets.get(0);
        assertThat(children(cs, "preConditions")).as("a guard here would skip the INSERT in the single-role posture").isEmpty();
        String sql = forwardSql(cs);
        assertThat(sql).containsPattern("(?is)insert\\s+into\\s+nexus\\.data_moves\\b");
        assertThat(sql).doesNotContainIgnoringCase("policy");
        assertThat(ROW_DML.matcher(withoutLiterals(sql)).results().count())
            .as("the one INSERT and no other row DML").isEqualTo(1);
        assertThat(sql).contains("('" + LABEL + "',");
        assertThat(LABEL).as("data_moves_label_check").doesNotContain("/");
        // the verification text is the last quoted value before NULL, on_complete: no question mark anywhere in the INSERT's
        // template or verification (the driver binds one), and the one bind parameter is the standalone $1
        assertThat(sql).as("no question mark in any registry text").doesNotContain("?");
        assertThat(Pattern.compile("\\$1").matcher(sql).results().count()).as("$1 twice in the verification, nowhere else").isEqualTo(2);
        assertThat(sql).doesNotContainPattern("\\$[2-9]");
        // the template and the verification name the same conformant-name rule
        var rule = Pattern.compile("'\\^\\(quarantine-\\|\\)\\(code\\|docs\\|rdr\\|knowledge\\)__\\[a-zA-Z0-9-\\]\\+\\(_\\[a-zA-Z0-9-\\]\\+\\)\\*__\\[a-z\\]\\[a-z0-9-\\]\\*__v\\[0-9\\]\\+\\$'");
        assertThat(rule.matcher(sql.replace("''", "'")).results().count()).as("the rule, in the template and in the verification").isEqualTo(2);
        assertThat(cs.getElementsByTagNameNS("*", "comment").item(0).getTextContent()).contains("DATA EFFECT:");
        // the parameters the header explains: a registry table is one chunk per tenant, so a quarantine pair repairs in one transaction
        assertThat(sql).as("driving table, chunk_size, sleep_ms, statement_timeout_ms")
            .contains("'nexus', 'catalog_collections', 100000, 0, 30000,");
    }

    /** The operator's SELECT in the header and in the changelog fragment names the rule the move uses, not a paraphrase of it. */
    @Test
    void theOperatorSelectInTheHeaderAndTheFragmentCarriesTheMovesRuleText() throws Exception {
        String sql = forwardSql(children(changelog(REGISTER), "changeSet").get(0));
        var found = Pattern.compile("''(\\^\\(quarantine-\\|\\)[^']+)''").matcher(sql).results()
            .map(r -> r.group(1)).distinct().toList();
        assertThat(found).as("one rule text, in the template and in the verification").hasSize(1);
        // not written as a statement: the SQL-string-literal gate counts a literal that opens with a SQL verb
        String select = "FROM nexus.catalog_collections WHERE model_version = '' AND name !~ '" + found.get(0) + "'";
        String header;
        try (InputStream in = CatalogModelVersionRepairMoveShapeTest.class.getClassLoader().getResourceAsStream(REGISTER)) {
            assertThat(in).isNotNull();
            header = new String(in.readAllBytes(), java.nio.charset.StandardCharsets.UTF_8);
        }
        assertThat(header).as("the header's operator SELECT").contains(select);
        // the fragment is folded into CHANGELOG.md at release and deleted: read whichever holds it
        var fragment = java.nio.file.Path.of("..", "changelog", "unreleased", "nexus-f3yqd.2.23.md");
        var holder = java.nio.file.Files.exists(fragment) ? fragment : java.nio.file.Path.of("..", "CHANGELOG.md");
        assertThat(java.nio.file.Files.readString(holder)).as("the operator SELECT in " + holder).contains(select);
    }

    @Test
    void thePolicyChangesetIsAlone_guardedMarkRan_selectOnly_toCurrentUser_withAPostCondition_andNoRowDml() throws Exception {
        var sets = children(changelog(POLICY), "changeSet");
        assertThat(sets).as("its own changeset, nothing else beside it").hasSize(1);
        var cs = sets.get(0);
        var guards = children(cs, "preConditions");
        assertThat(guards).hasSize(1);
        assertThat(guards.get(0).getAttribute("onFail")).isEqualTo("MARK_RAN");
        String sql = forwardSql(cs);
        assertThat(sql).containsPattern("(?is)create\\s+policy\\s+catalog_collections_owner_read\\s+on\\s+nexus\\.catalog_collections\\s+for\\s+select\\s+to\\s+current_user\\s+using\\s*\\(\\s*true\\s*\\)");
        assertThat(sql).contains("RAISE EXCEPTION").contains("polroles").contains("pg_has_role");
        assertThat(sql).doesNotContainIgnoringCase("nexus.data_moves");
        assertThat(ROW_DML.matcher(withoutLiterals(sql)).find()).as("DDL only").isFalse();
    }

    @Test
    void theGuardIsVectors029sPredicate() throws Exception {
        var original = checkOf(changelog("db/changelog/vectors-029-text-gate-probe-definer.xml"), "vectors-029-1");
        var copy = checkOf(changelog(POLICY), "vectors-065-1");
        assertThat(original).as("non-vacuity: the predicate was found").contains("pg_has_role").contains("nexus_svc");
        assertThat(copy).isEqualTo(original);
    }

    private static String checkOf(Element root, String changeSetId) {
        for (Element cs : children(root, "changeSet")) {
            if (changeSetId.equals(cs.getAttribute("id"))) {
                var guard = children(cs, "preConditions").get(0);
                var checks = guard.getElementsByTagNameNS("*", "sqlCheck");
                assertThat(checks.getLength()).isEqualTo(1);
                return checks.item(0).getTextContent().strip();
            }
        }
        throw new IllegalStateException("no changeset " + changeSetId);
    }
}
