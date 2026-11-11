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
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * nexus-f3yqd, engine 0.1.190: the chash width CHECKs are validated by a user-started job and a constraint that a table
 * REBUILD adds again (vectors-004, vectors-030, vectors-036 each did) comes back NOT VALID, which the conformance route reads
 * as not validated and the doctor answers by scanning. A changeset that adds a {@code *chash_octet_check} NOT VALID after the
 * job's release therefore needs a named reason here (the next VALIDATE job run covers it, or it is a different table), never
 * a silent regression of what the job achieved.
 *
 * <p>"After" is the order of {@code db.changelog-master.xml}: every include after {@link #BASELINE} (the last schema changelog
 * at the job's release), not counting the {@code grants-*} runAlways blocks that close the master.
 */
class ChashCheckNotValidChangelogLintTest {

    /** The last schema changelog at the VALIDATE job's release; the lint reads every include after it. */
    static final String BASELINE = "vectors-058-short-lexical-substring-gate.xml";

    /** Changelog file to the reason it may add a width CHECK NOT VALID. Empty today: none after the baseline does. */
    static final Map<String, String> EXEMPT = Map.of();

    private static final Path CHANGELOG_DIR = Path.of("src", "main", "resources", "db", "changelog");
    private static final Pattern INCLUDE = Pattern.compile("<include\\s+file=\"db/changelog/([^\"]+)\"");
    /** {@code ADD CONSTRAINT <anything>chash_octet_check ... NOT VALID} within one statement. */
    static final Pattern ADDS_NOT_VALID_WIDTH_CHECK =
        Pattern.compile("(?is)ADD\\s+CONSTRAINT\\s+\\w*chash_octet_check\\b[^;<]*?NOT\\s+VALID");

    private static List<String> includes() throws IOException {
        String master = Files.readString(CHANGELOG_DIR.resolve("db.changelog-master.xml"));
        Matcher m = INCLUDE.matcher(master.replaceAll("(?s)<!--.*?-->", ""));
        List<String> out = new ArrayList<>();
        while (m.find()) out.add(m.group(1));
        return out;
    }

    private static boolean addsNotValidWidthCheck(String file) throws IOException {
        return ADDS_NOT_VALID_WIDTH_CHECK.matcher(Files.readString(CHANGELOG_DIR.resolve(file))).find();
    }

    @Test
    void noChangesetAfterTheJobsReleaseAddsAWidthCheckNotValid_withoutANamedExemption() throws Exception {
        List<String> all = includes();
        int base = all.indexOf(BASELINE);
        assertThat(base).as("the baseline is an include of the master (a lint that found no baseline proves nothing)").isNotNegative();

        List<String> offenders = new ArrayList<>();
        int examined = 0;
        for (String file : all.subList(base + 1, all.size())) {
            if (file.startsWith("grants-")) continue;
            examined++;
            if (addsNotValidWidthCheck(file) && !EXEMPT.containsKey(file)) offenders.add(file);
        }
        System.out.println("chash NOT VALID lint: includes=" + all.size() + " baselineAt=" + base + " examinedAfterBaseline=" + examined
            + " offenders=" + offenders);
        assertThat(offenders).as("changelog(s) after " + BASELINE + " that add a *chash_octet_check NOT VALID: add the file to "
            + "EXEMPT with the reason, or have the changeset leave the constraint VALID").isEmpty();
        for (String exempt : EXEMPT.keySet()) {
            assertThat(all.subList(base + 1, all.size())).as("EXEMPT names a changelog after the baseline: " + exempt).contains(exempt);
            assertThat(addsNotValidWidthCheck(exempt)).as("EXEMPT entry still adds one: " + exempt).isTrue();
        }
    }

    @Test
    void theDetectorSeesTheChangelogsThatDidItBeforeTheBaseline_andFlagsASyntheticNewOne() throws Exception {
        List<String> all = includes();
        List<String> before = all.subList(0, all.indexOf(BASELINE) + 1);
        List<String> found = new ArrayList<>();
        for (String file : before) if (addsNotValidWidthCheck(file)) found.add(file);
        assertThat(found).as("non-vacuity: the rebuilds the lint is about are found").anyMatch(f -> f.startsWith("vectors-004"))
            .anyMatch(f -> f.startsWith("vectors-036")).hasSizeGreaterThanOrEqualTo(3);

        String synthetic = "<sql>ALTER TABLE nexus.chunks_new ADD CONSTRAINT chunks_chash_octet_check CHECK (octet_length(chash) = 32) NOT VALID;</sql>";
        assertThat(ADDS_NOT_VALID_WIDTH_CHECK.matcher(synthetic).find()).isTrue();
        String validOne = "<sql>ALTER TABLE nexus.chunks_new ADD CONSTRAINT chunks_chash_octet_check CHECK (octet_length(chash) = 32);</sql>";
        assertThat(ADDS_NOT_VALID_WIDTH_CHECK.matcher(validOne).find()).as("a VALID add is fine").isFalse();
        String other = "<sql>ALTER TABLE nexus.x ADD CONSTRAINT x_other_check CHECK (a > 0) NOT VALID;</sql>";
        assertThat(ADDS_NOT_VALID_WIDTH_CHECK.matcher(other).find()).as("another constraint is not this lint's").isFalse();
    }
}
