// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
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
 */
class NothingToWriteSourceScanTest {

    private static final Path MAIN = Path.of("src/main/java");
    private static final Pattern CALL = Pattern.compile("ExpectedCollectionIds\\s*\\.\\s*nothingToWrite\\s*\\(");
    /** A member declaration at class level (four spaces): a signature that may span lines, ending in an opening brace. */
    private static final Pattern MEMBER = Pattern.compile(
        "\\n    (?!return |if |for |while |switch |try|else|new |throw |\\}|//|\\*|/\\*)[A-Za-z@][^;{}=\\n]*(?:\\n[^;{}=\\n]*)*?\\)\\s*"
            + "(?:throws[^{;]*)?\\{");
    private static final Pattern A_TRANSACTION = Pattern.compile("withTenant\\s*\\(|manifestWriteTxn\\s*\\(|\\.transaction\\s*\\(|->\\s*\\{");
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
                    while (member.find()) {
                        headEnd = member.end();
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
