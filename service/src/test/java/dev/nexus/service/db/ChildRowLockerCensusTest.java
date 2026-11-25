// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeSet;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * The child-row-locker census (linda, RDR-229 P3.5a, nexus-p2m1x.23): every statement in {@code service/src/main} that takes a row
 * lock ({@code FOR UPDATE}, {@code FOR SHARE}, {@code FOR NO KEY UPDATE}, {@code FOR KEY SHARE}) is listed in
 * {@code rdr229/child-row-lockers.tsv} with the table it locks, its order relative to the registry read of its method, and, where it
 * takes no registry lock itself, why no registry waiter can sit behind it. A writer that locks a child row BEFORE the registry row can
 * close a cycle with a verb that holds the registry row {@code FOR UPDATE} and wants that child row; a plain unlocked read never waits
 * for a row lock and so is no link of a cycle.
 *
 * <p>The scan reads the source (comments blanked), so it does what the writer census does for write sites: a new lock site that is not
 * classified fails, a classified one that is gone fails, a verdict {@code after-registry} must be proven by a registry read in the same
 * method before the lock, and a verdict {@code no-registry-lock} must say why. It fails when it examines nothing.
 */
class ChildRowLockerCensusTest {

    private static final Path MAIN = Path.of("src/main/java");
    private static final Path TABLE = Path.of("src/test/resources/rdr229/child-row-lockers.tsv");
    private static final Pattern LOCK = Pattern.compile("\\.(forUpdate|forShare|forNoKeyUpdate|forKeyShare)\\s*\\(");
    private static final Pattern REGISTRY_READ = Pattern.compile("requireForWrite\\w*|requireWritable|requireAllForWrite|requireIdForWrite"
        + "|CollectionIds\\.require|registryFirst|lockAndRequireWritable|DocumentCollectionRef\\.(?:normalize\\w*|idOf)"
        + "|RequestedCollectionRead\\.require|consumeRequestedId|stampHistory|lockRows|ensureCollectionRegistered\\w*|labelNow"
        + "|lockCollectionsOfDocuments");
    private static final Pattern MEMBER = Pattern.compile(
        "\\n    (?!return |if |for |while |switch |try|else|new |throw |\\}|//|\\*|/\\*)[A-Za-z@][^;{}=\\n]*(?:\\n[^;{}=\\n]*)*?\\)\\s*"
            + "(?:throws[^{;]*)?\\{");
    private static final Pattern TABLE_CONSTANT = Pattern.compile("\\b([A-Z][A-Z_]{3,})\\.[A-Z_]+");

    private record Site(String key, boolean registryReadBefore) { }

    private static String code(Path file) throws IOException {
        String s = Files.readString(file);
        Matcher block = Pattern.compile("(?s)/\\*.*?\\*/").matcher(s);
        StringBuilder out = new StringBuilder();
        while (block.find()) {
            block.appendReplacement(out, Matcher.quoteReplacement(block.group().replaceAll("[^\\n]", " ")));
        }
        block.appendTail(out);
        return out.toString().replaceAll("//[^\\n]*", "");
    }

    private static List<Site> scan() throws IOException {
        List<Site> sites = new ArrayList<>();
        try (Stream<Path> walk = Files.walk(MAIN)) {
            for (Path f : walk.filter(p -> p.toString().endsWith(".java")).sorted().toList()) {
                String src = code(f);
                List<Matcher> heads = new ArrayList<>();
                Matcher m = MEMBER.matcher(src);
                List<int[]> spans = new ArrayList<>();
                List<String> decls = new ArrayList<>();
                while (m.find()) {
                    spans.add(new int[] {m.start(), m.end()});
                    decls.add(m.group());
                }
                Matcher lock = LOCK.matcher(src);
                while (lock.find()) {
                    int head = -1;
                    for (int i = 0; i < spans.size(); i++) {
                        if (spans.get(i)[1] <= lock.start()) {
                            head = i;
                        }
                    }
                    if (head < 0) {
                        continue;
                    }
                    String decl = decls.get(head).replaceAll("@\\w+(\\([^)]*\\))?", "");
                    Matcher name = Pattern.compile("(\\w+)\\s*\\(").matcher(decl);
                    String method = name.find() ? name.group(1) : "?";
                    String before = src.substring(Math.max(0, lock.start() - 500), lock.start());
                    String table = "?";
                    Matcher t = TABLE_CONSTANT.matcher(before);
                    while (t.find()) {
                        table = t.group(1);
                    }
                    boolean registryBefore = REGISTRY_READ.matcher(src.substring(spans.get(head)[1], lock.start())).find();
                    sites.add(new Site(f.getFileName() + "\t" + method + "\t" + table + "\t" + lock.group(1), registryBefore));
                }
            }
        }
        return sites;
    }

    @Test
    void everyRowLockingStatementIsClassifiedAndItsOrderProven() throws Exception {
        Map<String, String[]> table = new LinkedHashMap<>();
        Map<String, Integer> countOf = new LinkedHashMap<>();
        for (String line : Files.readAllLines(TABLE)) {
            if (line.isBlank() || line.startsWith("#")) {
                continue;
            }
            String[] c = line.split("\t", -1);
            assertThat(c).as("file, method, table, lock, count, verdict, reason: %s", line).hasSize(7);
            table.put(c[0] + "\t" + c[1] + "\t" + c[2] + "\t" + c[3], new String[] {c[5], c[6]});
            countOf.put(c[0] + "\t" + c[1] + "\t" + c[2] + "\t" + c[3], Integer.parseInt(c[4]));
        }
        List<Site> sites = scan();
        TreeSet<String> found = new TreeSet<>();
        Map<String, Integer> foundCount = new LinkedHashMap<>();
        for (Site s : sites) {
            found.add(s.key());
            foundCount.merge(s.key(), 1, Integer::sum);
        }
        // one count per STATEMENT: a second lock on the same table in the same method is a second site, and the table says how many
        assertThat(foundCount).as("the number of row-locking statements of each (file, method, table, lock): a second one in a method needs its own look")
            .containsExactlyInAnyOrderEntriesOf(countOf);
        System.err.printf("child_row_locker_census sites=%d distinct=%d classified=%d%n", sites.size(), found.size(), table.size());
        assertThat(sites.size()).as("non-vacuity: the row-locking statements found in the main sources").isGreaterThanOrEqualTo(30);
        assertThat(found).as("a row-locking statement missing from child-row-lockers.tsv, or listed there and gone: classify it")
            .containsExactlyInAnyOrderElementsOf(table.keySet());
        for (Site s : sites) {
            String[] verdict = table.get(s.key());
            assertThat(verdict[0]).as(s.key()).isIn("registry", "after-registry", "no-registry-lock");
            if (verdict[0].equals("after-registry")) {
                assertThat(s.registryReadBefore()).as("%s claims a registry read before the lock in its method; the scan finds none", s.key()).isTrue();
            }
            if (verdict[0].equals("no-registry-lock")) {
                assertThat(verdict[1]).as("%s must say why no registry waiter can sit behind it", s.key()).isNotBlank();
            }
        }
    }
}
