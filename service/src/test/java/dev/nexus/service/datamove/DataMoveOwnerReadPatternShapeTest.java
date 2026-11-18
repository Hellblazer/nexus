// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;
import org.w3c.dom.Element;
import org.w3c.dom.Node;

import javax.xml.XMLConstants;
import javax.xml.parsers.DocumentBuilderFactory;
import java.io.IOException;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Pattern;

/**
 * nexus-f3yqd.2.16: the owner-read pattern keeps the shape RDR-246 states, which the integration tests prove by running it
 * and this test pins by reading it, so an edit that moves the policy into the registering changeset, drops the guard or adds
 * row DML is red here before a database is involved. Also what a future {@code DataMoveChangesetGateTest} (rule 1: row
 * DML) reads of the policy changeset: nothing.
 */
class DataMoveOwnerReadPatternShapeTest {

    private static final Pattern ROW_DML = Pattern.compile("(?is)\\b(?:update|delete\\s+from|insert\\s+into|merge\\s+into)\\b");

    private static Element changelog(String resource) throws Exception {
        try (InputStream in = DataMoveOwnerReadPatternShapeTest.class.getClassLoader().getResourceAsStream(resource)) {
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

    /** {@code sql} with every single-quoted string and dollar-quoted body blanked: what is a value is not a statement. */
    private static String withoutLiterals(String sql) {
        return sql.replaceAll("(?s)\\$(\\w*)\\$.*?\\$\\1\\$", " ").replaceAll("(?s)'(?:[^']|'')*'", "''");
    }

    /** The text of the changeset's forward {@code <sql>} elements (not its rollback). */
    private static String forwardSql(Element changeSet) {
        StringBuilder sb = new StringBuilder();
        for (Element sql : children(changeSet, "sql")) sb.append(sql.getTextContent()).append('\n');
        return sb.toString();
    }

    @Test
    void theRegisteringChangesetIsOneInsertWithNoGuardAndNoPolicy() throws Exception {
        var sets = children(changelog("datamove/owner-read-policy/register.xml"), "changeSet");
        assertThat(sets).as("one changeset").hasSize(1);
        var cs = sets.get(0);
        assertThat(children(cs, "preConditions")).as("a guard here would skip the INSERT in the single-role posture").isEmpty();
        String sql = forwardSql(cs);
        assertThat(sql).containsPattern("(?is)insert\\s+into\\s+nexus\\.data_moves\\b");
        assertThat(sql).doesNotContainIgnoringCase("policy");
        assertThat(ROW_DML.matcher(withoutLiterals(sql)).results().count())
            .as("the one INSERT and no other row DML (the template text inside it is a value)").isEqualTo(1);
    }

    @Test
    void thePolicyChangesetIsAlone_guardedMarkRan_selectOnly_toCurrentUser_withAPostCondition_andNoRowDml() throws Exception {
        var sets = children(changelog("datamove/owner-read-policy/policy.xml"), "changeSet");
        assertThat(sets).as("its own changeset, nothing else beside it").hasSize(1);
        var cs = sets.get(0);
        var guards = children(cs, "preConditions");
        assertThat(guards).hasSize(1);
        assertThat(guards.get(0).getAttribute("onFail")).isEqualTo("MARK_RAN");
        String sql = forwardSql(cs);
        assertThat(sql).containsPattern("(?is)create\\s+policy\\s+\\S+\\s+on\\s+\\S+\\s+for\\s+select\\s+to\\s+current_user\\s+using\\s*\\(\\s*true\\s*\\)");
        assertThat(sql).as("the post-condition: policy roles and exposed roles").contains("RAISE EXCEPTION")
            .contains("polroles").contains("pg_has_role");
        assertThat(sql).doesNotContainIgnoringCase("nexus.data_moves");
        assertThat(ROW_DML.matcher(withoutLiterals(sql)).find()).as("DDL only: nothing for the gate's row-DML rule to read").isFalse();
    }

    /** The guard's predicate is vectors-029's, word for word: a drifted copy would protect a different set of roles. */
    @Test
    void theGuardIsVectors029sPredicate() throws Exception {
        var original = checkOf(changelog("db/changelog/vectors-029-text-gate-probe-definer.xml"), "vectors-029-1");
        var copy = checkOf(changelog("datamove/owner-read-policy/policy.xml"), "owner-read-policy-1");
        assertThat(original).as("non-vacuity: the predicate was found").contains("pg_has_role").contains("nexus_svc");
        assertThat(copy).isEqualTo(original);
    }

    private static String checkOf(Element root, String changeSetId) throws IOException {
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
