// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.vectors.PgVectorRepository;
import org.junit.jupiter.api.Test;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashSet;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-156 Decision 5 (bead nexus-ubnwk), round-1 review fix (code-review-expert +
 * substantive-critic Critical): {@code PgVectorRepository.ASPECT_SCOPED_FIELD_ALLOWLIST}
 * previously drifted from {@code vectors-008-aspect-scoped.xml}'s SQL CASE — the Java
 * set still listed {@code extras}/{@code salient_sentences} (jsonb since
 * aspects-003-type-hygiene.xml) after the SQL CASE, the Python client, and the MCP
 * docstring had all correctly narrowed to five fields. {@code field="extras"} then
 * passed the Java 400-guard, reached the SQL function, and silently fell through the
 * CASE to NULL (matches nothing, no error) instead of being rejected.
 *
 * <p>This test PARSES the actual changeset file's {@code WHEN '<name>' THEN} branches
 * out of the SQL CASE — it does not duplicate the field list as a second hand-typed
 * constant here (a second hand-typed list is exactly the shape that already drifted
 * once) — and asserts it is byte-identical to
 * {@link PgVectorRepository#ASPECT_SCOPED_FIELD_ALLOWLIST}. A future edit to either
 * side without the other now fails this test instead of shipping a silent-empty-result
 * 200 at the HTTP boundary.
 */
class AspectScopedFieldAllowlistCrossCheckTest {

    private static final Path CHANGESET = Path.of(
        "src", "main", "resources", "db", "changelog", "vectors-008-aspect-scoped.xml");

    // Matches `WHEN 'name' THEN` inside the p_field CASE expression. Field names are
    // lowercase_with_underscores column names (aspects-001-baseline.xml), so a simple
    // [a-z_]+ character class is sufficient and does not need to special-case quoting.
    private static final Pattern WHEN_BRANCH = Pattern.compile("WHEN\\s+'([a-z_]+)'\\s+THEN");

    @Test
    void javaAllowlistMatchesSqlCaseWhenBranches() throws Exception {
        assertThat(CHANGESET).as("vectors-008-aspect-scoped.xml must exist at %s", CHANGESET).exists();
        String source = Files.readString(CHANGESET);

        Set<String> sqlFields = new LinkedHashSet<>();
        Matcher m = WHEN_BRANCH.matcher(source);
        while (m.find()) {
            sqlFields.add(m.group(1));
        }

        assertThat(sqlFields)
            .as("sanity: the CASE must actually declare some WHEN branches — an empty "
                + "extraction means the regex or the file drifted and this test is "
                + "checking nothing")
            .isNotEmpty();
        assertThat(sqlFields)
            .as("PgVectorRepository.ASPECT_SCOPED_FIELD_ALLOWLIST must be byte-identical "
                + "to the SQL CASE's WHEN branch names in vectors-008-aspect-scoped.xml — "
                + "a field the Java 400-guard accepts but the CASE has no branch for "
                + "silently matches nothing instead of being rejected")
            .isEqualTo(PgVectorRepository.ASPECT_SCOPED_FIELD_ALLOWLIST);
    }

    @Test
    void extrasAndSalientSentencesAreNotInTheAllowlist() {
        // Explicit regression pin for the exact round-1 defect: both are jsonb since
        // aspects-003-type-hygiene.xml and cannot appear in a CASE alongside the five
        // TEXT branches ("CASE types text and jsonb cannot be matched").
        assertThat(PgVectorRepository.ASPECT_SCOPED_FIELD_ALLOWLIST)
            .doesNotContain("extras", "salient_sentences");
    }
}
