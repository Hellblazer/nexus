// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;
import org.w3c.dom.Element;
import org.w3c.dom.NodeList;

import javax.xml.parsers.DocumentBuilderFactory;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-225 (nexus-3wh8d.12): {@code vectors-030-1} runs in ONE transaction, so a failure anywhere in it leaves
 * every table unchanged. Each {@code <sql splitStatements="false">} block is already atomic on its own, which is
 * why the walk tests stay green with {@code runInTransaction="false"} (mutant W14): only a failure in a later
 * block after an earlier block committed would show the difference. This pins the attribute instead.
 */
class Vectors030TransactionalChangesetTest {

    private static final String CHANGELOG = "db/changelog/vectors-030-model-tenant-partition-functions.xml";

    @Test
    void vectors030_1_runsInOneTransaction() throws Exception {
        Element changeSet = null;
        try (var in = getClass().getClassLoader().getResourceAsStream(CHANGELOG)) {
            assertThat(in).as("changelog on classpath: " + CHANGELOG).isNotNull();
            var factory = DocumentBuilderFactory.newInstance();
            factory.setNamespaceAware(true);
            NodeList changeSets = factory.newDocumentBuilder().parse(in)
                .getElementsByTagNameNS("http://www.liquibase.org/xml/ns/dbchangelog", "changeSet");
            for (int i = 0; i < changeSets.getLength(); i++) {
                Element cs = (Element) changeSets.item(i);
                if ("vectors-030-1".equals(cs.getAttribute("id"))) {
                    changeSet = cs;
                }
            }
        }
        assertThat(changeSet).as("changeSet vectors-030-1").isNotNull();
        assertThat(changeSet.getAttribute("runInTransaction")).as("runInTransaction on vectors-030-1")
            .isIn("", "true");
    }
}
