// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static org.assertj.core.api.Assertions.assertThat;

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

/**
 * RDR-229 P3.5c (nexus-p2m1x.25): the three {@link QuarantineOrigin} overloads that delegate with {@code Gate3.never()} stay only
 * for callers in another group's files and for tests (ReembedSourceReader, ChashRepository: the assembly threads the gate there).
 * A production caller that is not on that list fails here, so a name-path read cannot be added unseen. The list can only shrink:
 * an entry whose call is gone fails too, so it is removed when the caller takes the gate.
 */
class Rdr229P35cGateOverloadLintTest {

    /** The delegating forms: method name and the argument count that selects the name-path overload. */
    private static final Pattern CALL = Pattern.compile("QuarantineOrigin\\.(countRowsOf|isLiveRegistered|retagRowsOf)\\(([^;]*?)\\)\\s*[;)\\s,?:&|]");

    private static final Set<String> KNOWN = Set.of(
        "db/ReembedSourceReader.java countRowsOf",
        "db/ChashRepository.java isLiveRegistered",
        "db/ChashRepository.java retagRowsOf");

    private static int arguments(String list) {
        int depth = 0, count = 1;
        for (char c : list.toCharArray()) {
            if (c == '(') depth++;
            else if (c == ')') depth--;
            else if (c == ',' && depth == 0) count++;
        }
        return count;
    }

    private static int gateless(String method) {
        return method.equals("retagRowsOf") ? 4 : 3;
    }

    @Test
    void onlyTheKnownProductionCallersUseTheNeverDelegatingOverloads() throws IOException {
        Path root = Path.of("src", "main", "java", "dev", "nexus", "service");
        Set<String> found = new TreeSet<>();
        int scanned = 0;
        try (Stream<Path> files = Files.walk(root)) {
            for (Path p : files.filter(f -> f.toString().endsWith(".java")).toList()) {
                scanned++;
                String text = Files.readString(p);
                Matcher m = CALL.matcher(text);
                while (m.find()) {
                    if (arguments(m.group(2)) == gateless(m.group(1))) {
                        found.add(root.relativize(p).toString().replace('\\', '/') + " " + m.group(1));
                    }
                }
            }
        }
        assertThat(scanned).as("non-vacuity: the walk saw the main tree").isGreaterThan(100);
        assertThat(found).as("production callers of a Gate3.never() delegating overload (thread the gate, or list the file with a reason)")
            .isEqualTo(new TreeSet<>(KNOWN));
    }

    @Test
    void theLintSeesAGatelessCallAndIgnoresAGatedOne() {
        List<String> seen = new ArrayList<>();
        for (String source : List.of("QuarantineOrigin.countRowsOf(ctx, t, o);", "QuarantineOrigin.countRowsOf(ctx, t, o, gate3);")) {
            Matcher m = CALL.matcher(source);
            if (m.find() && arguments(m.group(2)) == gateless(m.group(1))) seen.add(source);
        }
        assertThat(seen).containsExactly("QuarantineOrigin.countRowsOf(ctx, t, o);");
    }
}
