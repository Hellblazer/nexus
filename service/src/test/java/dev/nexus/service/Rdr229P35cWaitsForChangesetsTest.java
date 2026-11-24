// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.datamove.ArbiterTwins;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25; linda's P3.4 review item): every changeset id gate 3 waits for, an id twin's
 * ({@link ArbiterTwins#WAITS_FOR_CHANGESETS}; today {@code vectors-078-8} for the canary twin) or vectors-084's
 * ({@link ArbiterTwins#GATE3_WAITS_FOR_CHANGESETS}, the by-id ANN overloads annQuery calls from gate 3), is a real changeset of the
 * changelog, is included from the master changelog, and is in the P3.5b CONTINUE list {@value P35bContinueChangesets#LIST}.
 * A renumbered or renamed changeset would otherwise leave the twin, and with it gate 3, waiting for an id that never records.
 */
class Rdr229P35cWaitsForChangesetsTest {

    private static final Path CHANGELOGS = Path.of("src/main/resources/db/changelog");
    private static final Pattern CHANGESET = Pattern.compile("<changeSet\\s+id=\"([^\"]+)\"");

    private static String blankComments(String xml) {
        return xml.replaceAll("(?s)<!--.*?-->", "");
    }

    @Test
    void everyChangesetATwinWaitsForIsInTheChangelog_theMasterIncludesItsFile_andTheContinueList() throws IOException {
        List<String> waited = Stream.concat(ArbiterTwins.WAITS_FOR_CHANGESETS.values().stream().flatMap(List::stream),
            ArbiterTwins.GATE3_WAITS_FOR_CHANGESETS.stream()).distinct().toList();
        assertThat(waited).as("non-vacuity: a twin and gate 3 wait for changesets").isNotEmpty()
            .contains("vectors-078-8", "vectors-084-1", "vectors-084-2", "vectors-084-3");
        assertThat(ArbiterTwins.WAITS_FOR_CHANGESETS).as("the canary twin is the one that waits").containsKey("idx_canary_query_ring_id_digest_uq");

        String master = blankComments(Files.readString(CHANGELOGS.resolve("db.changelog-master.xml")));
        List<String> continueList = P35bContinueChangesets.ids();
        assertThat(continueList).as("non-vacuity: the CONTINUE list is read").isNotEmpty();

        List<Path> files;
        try (Stream<Path> s = Files.list(CHANGELOGS)) {
            files = s.filter(p -> p.toString().endsWith(".xml") && !p.getFileName().toString().equals("db.changelog-master.xml")).toList();
        }
        for (String id : waited) {
            List<String> holders = new ArrayList<>();
            for (Path f : files) {
                Matcher m = CHANGESET.matcher(blankComments(Files.readString(f)));
                while (m.find()) {
                    if (m.group(1).equals(id)) holders.add(f.getFileName().toString());
                }
            }
            assertThat(holders).as("changeset %s is declared by exactly one changelog file", id).hasSize(1);
            assertThat(master).as("the master changelog includes %s's file", id).contains(holders.get(0));
            assertThat(continueList).as("%s is in the CONTINUE list (a fresh walk skips it until the stamps complete)", id).contains(id);
        }
    }
}
