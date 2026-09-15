package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-207 bead nexus-l3yuc.4: every {@code ON CONFLICT (tenant_id, project, title)
 * DO UPDATE} branch in {@link MemoryRepository} clears BOTH quarantine stamps.
 *
 * <p>A write that names an existing title is a decision to keep that title with the
 * written content, so the conflict branch sets {@code quarantined_at} and
 * {@code rolled_up_at} to NULL in the same statement. The branch set is DERIVED
 * from the source, never from a list: the test slices the source from each
 * {@code onConflict(MEMORY.TENANT_ID, MEMORY.PROJECT, MEMORY.TITLE)} to the next
 * statement terminator and asserts the two clears inside it. Plain JUnit, no
 * Docker.
 *
 * <p>Non-vacuity: at least three branches ({@code doUpsert}, {@code doImport},
 * {@code importBatch}) must be found, and every {@code onConflict(} in the file
 * must be on the title key, so a future branch on another key is not missed.
 * Falsified before commit by deleting one clear and watching this go red.
 */
class MemoryRepositoryConflictBranchTest {

    private static final Path SOURCE = Path.of(
        "src", "main", "java", "dev", "nexus", "service", "db", "MemoryRepository.java");

    private static final Pattern TITLE_KEY_CONFLICT = Pattern.compile(
        "onConflict\\(\\s*MEMORY\\.TENANT_ID\\s*,\\s*MEMORY\\.PROJECT\\s*,\\s*MEMORY\\.TITLE\\s*\\)");
    private static final Pattern ANY_CONFLICT = Pattern.compile("onConflict\\(");

    private static final Pattern CLEAR_QUARANTINED = Pattern.compile(
        "\\.set\\(\\s*MEMORY\\.QUARANTINED_AT\\s*,\\s*\\(OffsetDateTime\\)\\s*null\\s*\\)");
    private static final Pattern CLEAR_ROLLED_UP = Pattern.compile(
        "\\.set\\(\\s*MEMORY\\.ROLLED_UP_AT\\s*,\\s*\\(OffsetDateTime\\)\\s*null\\s*\\)");

    @Test
    void everyTitleKeyConflictBranch_clearsBothStamps() throws IOException {
        assertThat(SOURCE).as("run from service/ (surefire's working directory)").exists();
        String src = Files.readString(SOURCE);

        int anyConflicts = 0;
        Matcher any = ANY_CONFLICT.matcher(src);
        while (any.find()) anyConflicts++;

        List<String> branches = new ArrayList<>();
        List<String> missing = new ArrayList<>();
        Matcher m = TITLE_KEY_CONFLICT.matcher(src);
        while (m.find()) {
            int end = src.indexOf(';', m.end());
            assertThat(end).as("a conflict branch must end in a statement terminator").isPositive();
            String slice = src.substring(m.start(), end);
            String where = "line " + lineOf(src, m.start());
            branches.add(where);
            if (!CLEAR_QUARANTINED.matcher(slice).find()) missing.add(where + ": quarantined_at not cleared");
            if (!CLEAR_ROLLED_UP.matcher(slice).find()) missing.add(where + ": rolled_up_at not cleared");
        }

        assertThat(branches.size())
            .as("non-vacuity: MemoryRepository carries at least three ON CONFLICT (tenant_id, "
                + "project, title) branches (doUpsert, doImport, importBatch); found %s", branches)
            .isGreaterThanOrEqualTo(3);
        assertThat(anyConflicts)
            .as("every onConflict( in MemoryRepository must be on the title key, so a branch "
                + "on another key cannot escape this gate")
            .isEqualTo(branches.size());
        assertThat(missing)
            .as("every conflict branch on the title key must set quarantined_at AND "
                + "rolled_up_at to NULL in the same statement (RDR-207: a write that names "
                + "an existing title is a decision to keep it)")
            .isEmpty();
    }

    private static int lineOf(String src, int offset) {
        int line = 1;
        for (int i = 0; i < offset; i++) if (src.charAt(i) == '\n') line++;
        return line;
    }
}
