// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P2.6 (nexus-p2m1x.11): the forward bodies of vectors-063 read the collection's catalog row and never its name.
 * Pure text over the changeset file, no database: the live-body census that P1.3 adds counts these expressions across
 * every live body; this holds the three functions this file redefines at zero where they are written, and holds each
 * rollback to the text it restores (the previous last definer still parses the name, which is what a rollback is for).
 */
class Vectors063NoNameParseSqlPinTest {

    private static final String CHANGESET = "db/changelog/vectors-063-live-functions-read-collection-row.xml";
    private static final List<String> IDS = List.of("vectors-063-1", "vectors-063-2", "vectors-063-3");

    private static String xml() throws IOException {
        try (var in = Vectors063NoNameParseSqlPinTest.class.getClassLoader().getResourceAsStream(CHANGESET)) {
            assertThat(in).as("the changeset is on the classpath").isNotNull();
            return new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
    }

    /** The forward sql of one changeset, comment lines dropped. */
    private static String forward(String xml, String id) {
        int own = xml.indexOf("<changeSet id=\"" + id + "\"");
        assertThat(own).as(id + " is in the file").isGreaterThanOrEqualTo(0);
        int sql = xml.indexOf("<sql ", own);
        int rollback = xml.indexOf("<rollback>", own);
        assertThat(rollback).isGreaterThan(sql);
        return xml.substring(sql, rollback).lines().filter(l -> !l.trim().startsWith("--"))
            .reduce("", (a, b) -> a + b + "\n");
    }

    private static String rollback(String xml, String id) {
        int own = xml.indexOf("<changeSet id=\"" + id + "\"");
        int rb = xml.indexOf("<rollback>", own);
        return xml.substring(rb, xml.indexOf("</changeSet>", rb));
    }

    @Test
    void noForwardBodyParsesANameOrTestAPrefix() throws IOException {
        String xml = xml();
        for (String id : IDS) {
            String body = forward(xml, id);
            assertThat(body).as(id + " forward body").doesNotContain("split_part(").doesNotContain("'quarantine-'")
                .doesNotContain("starts_with(").doesNotContain("left(p_origin_collection");
        }
    }

    @Test
    void theRowIsWhatTheFunctionsRead() throws IOException {
        String xml = xml();
        assertThat(forward(xml, "vectors-063-1")).contains("nexus.catalog_collections cc")
            .contains("em.dimension = chash_conformance_report.dim").contains("(no catalog row)");
        assertThat(forward(xml, "vectors-063-2")).contains("cc.lifecycle_state = 'live'")
            .contains("cc.lifecycle_state = 'quarantine'");
        assertThat(forward(xml, "vectors-063-3")).contains("q.lifecycle_state = 'quarantine'")
            .contains("o.lifecycle_state = 'live'");
    }

    @Test
    void eachRollbackRestoresThePreviousLastDefinersNameRead() throws IOException {
        String xml = xml();
        assertThat(rollback(xml, "vectors-063-1")).as("vectors-005-11 body, nine name-parsing lists")
            .contains("split_part(collection, '__', 3) IN ('voyage-code-3', 'voyage-context-3', 'voyage-3')");
        assertThat(rollback(xml, "vectors-063-2")).contains("left(p_origin_collection, 11) = 'quarantine-'");
        assertThat(rollback(xml, "vectors-063-3")).contains("starts_with(p_quarantine_collection, 'quarantine-')");
        assertThat(xml.split("split_part\\(collection, '__', 3\\)", -1).length - 1)
            .as("the nine literals of vectors-005-11, all of them in the rollback").isEqualTo(9);
    }
}
