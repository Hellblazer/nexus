// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

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
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a, review C2 (nexus-p2m1x.23): {@code ExpectedCollectionIds.nothingToWrite} marks a named id as consumed WITHOUT a
 * transaction reading it, a claim nothing checks at run time. {@code write_many} marked a document with no rows as nothing to write
 * while the write still deleted the document's stored manifest rows, and a request by an id that was gone answered 200. So the claim is
 * limited to the one kind a reader can verify: an early return that comes BEFORE the first transaction of its method and returns
 * at once. This reads the source of {@code src/main} and fails on a call that is not such an early return: one with a
 * {@code withTenant}, {@code manifestWriteTxn}, {@code transaction} call or a lambda body between the head of its method and the call,
 * or whose statement is not followed by a {@code return}. It fails when it finds no call to examine.
 *
 * <p>linda's ruling (RDR-229 P3.5a): the early return is itself the defect on a writer that a request addresses by {@code collection_id}.
 * Every writer reads its collection's row {@code FOR KEY SHARE} first, empty or no-op input included, so that a 200 holds only for a live
 * row and a freed id answers 404. {@code persistDiscoveredTopics} and {@code deleteByIds} returned early on an empty request and
 * answered 200 for a freed id; this scan saw each as a sound early return and passed them, because it asked only whether the claim was
 * early, never whether the method was a writer that may claim at all. It now pins the method of every call, exactly: a call in a method not on
 * {@link #KNOWN} fails (a new early return needs a ruling, and the ruling is to read the row instead), and a {@code KNOWN} entry that no
 * longer has a call is stale and fails too. The entries are the calls that were in the tree when the ruling came: each names the same shape
 * and is listed so that it is seen, not so that it is allowed to stay.
 */
class NothingToWriteSourceScanTest {

    private static final Path MAIN = Path.of("src/main/java");
    private static final Pattern CALL = Pattern.compile("ExpectedCollectionIds\\s*\\.\\s*nothingToWrite\\s*\\(");
    /** A member declaration at class level (four spaces): a signature that may span lines, ending in an opening brace. */
    private static final Pattern MEMBER = Pattern.compile(
        "\\n    (?!return |if |for |while |switch |try|else|new |throw |\\}|//|\\*|/\\*)[A-Za-z@][^;{}=\\n]*(?:\\n[^;{}=\\n]*)*?\\)\\s*"
            + "(?:throws[^{;]*)?\\{");
    private static final Pattern A_TRANSACTION = Pattern.compile("withTenant\\s*\\(|manifestWriteTxn\\s*\\(|\\.transaction\\s*\\(|->\\s*\\{");
    /** {@code File#method} of every nothingToWrite call still in the tree. The two taxonomy writers the ruling named are gone from it. */
    static final Set<String> KNOWN = new TreeSet<>(Set.of(
        "PgVectorRepository.java#delete", "PgVectorRepository.java#markedOnly", "PgVectorRepository.java#restoreAcross",
        "PgVectorRepository.java#updateMetadataWithMissing",
        "AspectRepository.java#upsertAspect", "CatalogRepository.java#refuseRowsUnderSupersededCollection",
        "CatalogRepository.java#importChunksBatch"));
    private static final Pattern ANNOTATION = Pattern.compile("@\\w+(?:\\([^)]*\\))?\\s*");
    private static final Pattern FIRST_CALL_NAME = Pattern.compile("(\\w+)\\s*\\(");

    /** The method a member head declares: the identifier before its parameter list, annotations dropped. */
    static String methodName(String head) {
        Matcher m = FIRST_CALL_NAME.matcher(ANNOTATION.matcher(head).replaceAll(""));
        return m.find() ? m.group(1) : "?";
    }

    private static final Pattern THEN_RETURN = Pattern.compile("^[^;]*;\\s*(?:\\n\\s*)?return\\b");

    /** Comments blanked (lines kept), so a call named in a javadoc is not a call. */
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

    @Test
    void everyNothingToWriteIsAnEarlyReturnBeforeAnyTransaction() throws Exception {
        List<String> examined = new ArrayList<>();
        List<String> offenders = new ArrayList<>();
        Set<String> sites = new TreeSet<>();
        int files = 0;
        try (Stream<Path> walk = Files.walk(MAIN)) {
            for (Path f : walk.filter(p -> p.toString().endsWith(".java")).toList()) {
                files++;
                if (f.getFileName().toString().equals("ExpectedCollectionIds.java")) continue;
                String src = code(f);
                Matcher call = CALL.matcher(src);
                while (call.find()) {
                    int line = (int) src.substring(0, call.start()).chars().filter(c -> c == '\n').count() + 1;
                    String where = f.getFileName() + ":" + line;
                    examined.add(where);
                    Matcher member = MEMBER.matcher(src);
                    member.region(0, call.start());
                    int headEnd = -1;
                    int headStart = -1;
                    while (member.find()) {
                        headEnd = member.end();
                        headStart = member.start();
                    }
                    if (headEnd >= 0) {
                        sites.add(f.getFileName() + "#" + methodName(src.substring(headStart, headEnd)));
                    }
                    if (headEnd < 0) {
                        offenders.add(where + " (no enclosing method found)");
                        continue;
                    }
                    String before = src.substring(headEnd, call.start());
                    if (A_TRANSACTION.matcher(before).find()) {
                        offenders.add(where + " (a transaction or a lambda precedes it in its method)");
                    }
                    String statementAndAfter = src.substring(call.start(), Math.min(src.length(), call.start() + 300));
                    if (!THEN_RETURN.matcher(statementAndAfter).find()) {
                        offenders.add(where + " (the statement is not followed by a return)");
                    }
                }
            }
        }
        System.err.printf("nothing_to_write_scan files=%d calls_examined=%d examined=%s%n", files, examined.size(), examined);
        assertThat(files).as("non-vacuity: the main sources scanned").isGreaterThan(300);
        assertThat(examined).as("non-vacuity: the nothingToWrite calls found (the repositories' empty-batch early returns)")
            .hasSizeGreaterThanOrEqualTo(6);
        assertThat(offenders).as("a nothingToWrite that is not an early return before the first transaction of its method").isEmpty();
        assertThat(sites).as("the methods that claim nothingToWrite: a writer reads its collection's row even with nothing to write "
            + "(linda's ruling), so a new claim needs that ruling, and a listed one that is gone is removed from KNOWN").isEqualTo(KNOWN);
    }

    /** The two writers the ruling named, as they were: the pin names them, so the early return that hid them is now a finding. */
    @Test
    void theCensusNamesTheTwoWritersThatAnsweredA200ForAFreedId() {
        String discovered = "\n    @SuppressWarnings(\"unchecked\")\n    public List<Long> persistDiscoveredTopics(String tenant, String collection,\n"
            + "                                               List<Map<String, Object>> specs) {\n        if (specs == null || specs.isEmpty()) {\n"
            + "            ExpectedCollectionIds.nothingToWrite(collection);\n            return List.of();\n        }\n        return 1;\n    }\n";
        String delete = "\n    public int deleteByIds(String tenant, String collection, List<Long> topicIds) {\n        if (topicIds == null || topicIds.isEmpty()) {\n"
            + "            ExpectedCollectionIds.nothingToWrite(collection);\n            return 0;\n        }\n        return 1;\n    }\n";
        for (String[] shape : new String[][] {{discovered, "persistDiscoveredTopics"}, {delete, "deleteByIds"}}) {
            Matcher call = CALL.matcher(shape[0]);
            assertThat(call.find()).isTrue();
            Matcher member = MEMBER.matcher(shape[0]);
            member.region(0, call.start());
            int start = -1;
            int end = -1;
            while (member.find()) {
                start = member.start();
                end = member.end();
            }
            assertThat(methodName(shape[0].substring(start, end))).as("the method of the call").isEqualTo(shape[1]);
            assertThat(verdict(shape[0])).as("a sound early return, which is all the old scan asked").isTrue();
            assertThat(KNOWN).as(shape[1] + " is not a sanctioned claimant, so the call in it is a finding").noneMatch(k -> k.endsWith("#" + shape[1]));
        }
    }

    /** The scan tells a late claim from an early one: the two shapes it exists to separate. */
    @Test
    void theScanTellsALateClaimFromAnEarlyReturn() {
        String early = "\n    public int f(String c, java.util.List<String> ids) {\n        if (ids.isEmpty()) {\n"
            + "            ExpectedCollectionIds.nothingToWrite(c);\n            return 0;\n        }\n"
            + "        return scope.withTenant(t, ctx -> 1);\n    }\n";
        String late = "\n    public int f(String c, java.util.List<String> ids) {\n        return scope.withTenant(t, ctx -> {\n"
            + "            if (ids.isEmpty()) {\n                ExpectedCollectionIds.nothingToWrite(c);\n                return 0;\n"
            + "            }\n            return 1;\n        });\n    }\n";
        assertThat(verdict(early)).as("an early return before the transaction").isTrue();
        assertThat(verdict(late)).as("a claim made inside the transaction").isFalse();
    }

    private static boolean verdict(String src) {
        Matcher call = CALL.matcher(src);
        assertThat(call.find()).isTrue();
        Matcher member = MEMBER.matcher(src);
        member.region(0, call.start());
        int headEnd = -1;
        while (member.find()) {
            headEnd = member.end();
        }
        assertThat(headEnd).as("the member head is found").isNotNegative();
        return !A_TRANSACTION.matcher(src.substring(headEnd, call.start())).find()
            && THEN_RETURN.matcher(src.substring(call.start(), Math.min(src.length(), call.start() + 300))).find();
    }
}
