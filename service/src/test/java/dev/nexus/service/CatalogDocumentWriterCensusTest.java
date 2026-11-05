// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.TreeSet;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-rfvyy.6.4, RDR-243 assumption A2: the only statements that insert into
 * {@code nexus.catalog_documents} are the five named below, and each of them carries the one-way
 * {@code sensitive} rule (the write-rules test pins the rule itself; this census pins that no sixth
 * writer exists that would drop or lower the flag).
 *
 * <p>A source scan of {@code CatalogRepository.java}, no database. It finds every
 * {@code insertInto(CATALOG_DOCUMENTS ...)} (and the raw {@code INSERT INTO nexus.catalog_documents} /
 * {@code mergeInto} spellings), maps each hit to its enclosing method and asserts the set of methods equals
 * {@link #WRITERS}, in both directions: an unnamed writer fails, and so does a named method with no hit (a
 * stale entry). Non-vacuity: the scan must find at least {@link #WRITERS}{@code .size()} hits, and a
 * synthetic source with a sixth writer is flagged by the same scan.
 *
 * <p>A change that adds a writer (or renames one) edits {@link #WRITERS} in the same commit, which is the
 * review point: the new writer must OR the flag, never assign it.
 */
class CatalogDocumentWriterCensusTest {

    private static final Path SOURCE =
        Path.of("src", "main", "java", "dev", "nexus", "service", "db", "CatalogRepository.java");

    /** The methods of CatalogRepository.java that insert into catalog_documents (A2). */
    static final Set<String> WRITERS = Set.of(
        "upsertDocument",
        "registerDocumentWithOutcome",
        "registerDocumentManyWithOutcome",
        "importDocumentsBatch",
        "doImportDocument");

    private static final Pattern INSERT_HIT = Pattern.compile(
        "insertInto\\(\\s*(?:dev\\.nexus\\.service\\.jooq\\.nexus\\.Tables\\.)?CATALOG_DOCUMENTS\\b"
            + "|mergeInto\\(\\s*CATALOG_DOCUMENTS\\b"
            + "|(?i:insert\\s+into\\s+(?:nexus\\.)?catalog_documents\\b)"
            + "|insertInto\\(\\s*(?:DSL\\.)?(?:table|name)\\(\\s*\"(?:nexus\\.)?catalog_documents\"");

    /** A method declaration at class-member indent (four spaces), up to its name. */
    private static final Pattern METHOD_DECL = Pattern.compile(
        "(?m)^    (?=\\S)(?:(?:public|private|protected|static|final|synchronized)\\s+)*[\\w<>\\[\\], .?]+?\\s+(\\w+)\\s*\\(");

    /** Methods of {@code src} that contain a catalog_documents insert, with the hit count. */
    static List<String> writerHits(String src) {
        List<int[]> decls = new ArrayList<>();
        List<String> names = new ArrayList<>();
        Matcher d = METHOD_DECL.matcher(src);
        while (d.find()) {
            String n = d.group(1);
            if (Set.of("if", "for", "while", "switch", "catch", "return", "new", "else").contains(n)) continue;
            decls.add(new int[] {d.start()});
            names.add(n);
        }
        List<String> hits = new ArrayList<>();
        Matcher m = INSERT_HIT.matcher(src);
        while (m.find()) {
            String owner = "<outside any method>";
            for (int i = 0; i < decls.size(); i++) {
                if (decls.get(i)[0] <= m.start()) owner = names.get(i); else break;
            }
            hits.add(owner);
        }
        return hits;
    }

    @Test
    void everyInsertIntoCatalogDocumentsIsInTheNamedWriterSet() throws IOException {
        List<String> hits = writerHits(Files.readString(SOURCE));
        assertThat(hits).as("non-vacuity: the scan examined the five known insert statements")
            .hasSizeGreaterThanOrEqualTo(WRITERS.size());
        assertThat(new TreeSet<>(hits))
            .as("methods of CatalogRepository.java that insert into catalog_documents (found %s); an unnamed "
                + "writer must OR the sensitive flag and join WRITERS in the same commit, a stale name must go",
                hits)
            .isEqualTo(new TreeSet<>(WRITERS));
    }

    @Test
    void theScanSeesASixthWriterAndEveryRawSpelling() {
        String fake = """
            class X {
                void upsertDocument() {
                    ctx.insertInto(CATALOG_DOCUMENTS, A).values(1).execute();
                }
                void sneaky() {
                    ctx.execute("INSERT INTO nexus.catalog_documents (tenant_id) VALUES (?)", t);
                }
                void viaMerge() {
                    ctx.mergeInto(CATALOG_DOCUMENTS).using(x).execute();
                }
                void viaTable() {
                    ctx.insertInto(DSL.table("catalog_documents")).values(1).execute();
                }
            }
            """;
        assertThat(new TreeSet<>(writerHits(fake)))
            .containsExactlyInAnyOrder("upsertDocument", "sneaky", "viaMerge", "viaTable");
    }
}
