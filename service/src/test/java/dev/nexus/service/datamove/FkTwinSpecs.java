// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import org.w3c.dom.Element;
import org.w3c.dom.Node;

import javax.xml.XMLConstants;
import javax.xml.parsers.DocumentBuilderFactory;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

/**
 * RDR-229 P3.3a (nexus-p2m1x.19): what the seven id-twin changesets and their validation moves are registered with, written down once
 * for the tests. The twins come from the one list {@code db/rdr229/p33a-fk-twins.tsv} ({@link FkTwins}); the values are P3.2's
 * (T2 nexus_rdr/229-p32-fork-results-2026-11-20; Sam's ask C ruling, T2 nexus/sam-ruling-rdr229-ask-c-2026-11-20, and the bead's
 * "Registering-INSERT values": topic_assignments 1000 rows and 2 s, the others 10000 rows and 8 s, statement_timeout_ms 60000 on all).
 */
final class FkTwinSpecs {

    private FkTwinSpecs() {}

    static final String CHANGELOG = "db/changelog/vectors-072-collection-id-fk-twins.xml";
    static final Path CHANGELOG_FILE = Path.of("src", "main", "resources", CHANGELOG);
    static final String QUEUE = "aspect_extraction_queue";
    static final int STATEMENT_TIMEOUT_MS = 60_000;

    /** The changeset of the twin in row {@code i} of the list (0-based): vectors-072-1 to -7. */
    static String ddlChangeset(int i) {
        return "vectors-072-" + (i + 1);
    }

    /** The changeset of the validation move in row {@code i} of the list: vectors-072-11 to -17. */
    static String moveChangeset(int i) {
        return "vectors-072-" + (i + 11);
    }

    static List<FkTwins.Row> twins() {
        return FkTwins.load();
    }

    static int indexOf(String table) {
        List<FkTwins.Row> rows = twins();
        for (int i = 0; i < rows.size(); i++) {
            if (rows.get(i).table().equals(table)) return i;
        }
        throw new IllegalArgumentException(table + " has no twin");
    }

    static FkTwins.Row twin(String table) {
        return twins().get(indexOf(table));
    }

    /** The twins of tables every install has: the queue's is conditional (R3). */
    static List<FkTwins.Row> alwaysThere() {
        return twins().stream().filter(r -> !r.table().equals(QUEUE)).toList();
    }

    static int chunkSize(String table) {
        return table.equals("topic_assignments") ? 1000 : 10_000;
    }

    static int sleepMs(String table) {
        return table.equals("topic_assignments") ? 2000 : 8000;
    }

    /** The {@code confdeltype} letter of an ON DELETE action. */
    static String deleteType(String onDelete) {
        return switch (onDelete) {
            case "RESTRICT" -> "r";
            case "CASCADE" -> "c";
            default -> throw new IllegalArgumentException(onDelete);
        };
    }

    // ── the changelog file, read as XML ──────────────────────────────────────────────────────────────────────

    /** One changeset: its id, comment, forward SQL (the direct {@code <sql>} children), rollback SQL, and its precondition. */
    record Changeset(String id, String comment, String sql, String rollback, Element preConditions) {}

    static List<Changeset> changesets() throws Exception {
        return changesets(CHANGELOG_FILE);
    }

    /** The forward SQL of P3.1's stamp-move changeset of each table that has one, by table: the text this step's templates must copy. */
    static java.util.Map<String, String> stampChangesets() throws Exception {
        java.util.Map<String, String> out = new java.util.LinkedHashMap<>();
        for (Changeset cs : changesets(StampMoveSpecs.CHANGELOG_FILE)) {
            for (FkTwins.Row r : twins()) {
                if (cs.sql().toLowerCase(java.util.Locale.ROOT).contains("values ('" + StampMoveSpecs.LABEL_PREFIX + r.table() + "'")) out.put(r.table(), cs.sql());
            }
        }
        assertThat(out).as("every twin's table has a stamp move in vectors-070").hasSameSizeAs(twins());
        return out;
    }

    static List<Changeset> changesets(Path file) throws Exception {
        var dbf = DocumentBuilderFactory.newInstance();
        dbf.setNamespaceAware(true);
        dbf.setFeature(XMLConstants.FEATURE_SECURE_PROCESSING, true);
        dbf.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true);
        List<Changeset> out = new ArrayList<>();
        try (InputStream in = Files.newInputStream(file)) {
            Element root = dbf.newDocumentBuilder().parse(in).getDocumentElement();
            for (Node cs = root.getFirstChild(); cs != null; cs = cs.getNextSibling()) {
                if (!(cs instanceof Element e) || !"changeSet".equals(e.getLocalName())) continue;
                StringBuilder sql = new StringBuilder();
                StringBuilder rollback = new StringBuilder();
                String comment = "";
                Element pre = null;
                for (Node n = e.getFirstChild(); n != null; n = n.getNextSibling()) {
                    if (!(n instanceof Element c)) continue;
                    switch (c.getLocalName()) {
                        case "sql" -> sql.append(c.getTextContent());
                        case "comment" -> comment = c.getTextContent();
                        case "preConditions" -> pre = c;
                        case "rollback" -> rollback.append(c.getTextContent());
                        default -> { }
                    }
                }
                out.add(new Changeset(e.getAttribute("id"), comment, sql.toString(), rollback.toString(), pre));
            }
        }
        assertThat(out).as(file + " holds changesets").isNotEmpty();
        return out;
    }

    static Changeset changeset(String id) throws Exception {
        return changesets().stream().filter(c -> c.id().equals(id)).findFirst()
            .orElseThrow(() -> new AssertionError("no changeset " + id + " in " + CHANGELOG));
    }
}
