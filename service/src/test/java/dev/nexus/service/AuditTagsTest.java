// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.AuditTags;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Optional;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-238 Phase 1 Step 1 (nexus-qo7ie.6): pins {@link AuditTags} to the table it is generated from,
 * {@code src/nexus/tables/audit-tags.toml}. The test reads the TOML itself with a line parser (one
 * {@code match = { tag = "..." }} per row, one quoted string per line in the {@code title_patterns}
 * array), so a hand edit to the generated file, or a table edit without regeneration, fails here and
 * not in review. It runs from the repo root's {@code service/} module directory.
 *
 * <p>Parity is proved both ways: the Java sets equal the TOML sets, and the TOML parse is held to the
 * six tags and seven patterns the decision names, typed out here, so a parser that read nothing cannot
 * pass on two empty sets.
 */
class AuditTagsTest {

    private static final Path TABLE =
        Path.of("..", "src", "nexus", "tables", "audit-tags.toml").normalize();

    private static final Pattern STRING = Pattern.compile("\"((?:[^\"\\\\]|\\\\.)*)\"");
    private static final Pattern MATCH_TAG = Pattern.compile("^match\\s*=\\s*\\{\\s*tag\\s*=\\s*(\".*\")\\s*\\}\\s*$");

    /** Typed out on purpose: the table must equal the decision's list, not itself. */
    private static final Set<String> DECISION_TAGS =
        Set.of("kind:gate", "gate", "critique", "research", "fix-check", "commit-review");
    private static final List<String> DECISION_PATTERNS = List.of(
        "(^|[-_])review([-_]|$)",
        "(^|[-_])code-review([-_]|$)",
        "(^|[-_])commit-review([-_]|$)",
        "(^|[-_])critique([-_]|$)",
        "(^|[-_])gate([-_]|$)",
        "(^|[-_])fix-check([-_]|$)",
        "-research-[0-9]+$",
        "(^|[-_])confirm([-_]|$)",
        "(^|[-_])audit([-_]|$)");

    private record Parsed(Set<String> tags, List<String> patterns) {}

    private static String unescape(String s) {
        return s.replace("\\\"", "\"").replace("\\\\", "\\");
    }

    private static Parsed parse() throws IOException {
        assertThat(TABLE).as("the audit table must exist at %s", TABLE).exists();
        Set<String> tags = new HashSet<>();
        List<String> patterns = new ArrayList<>();
        boolean inPatterns = false;
        for (String raw : Files.readAllLines(TABLE)) {
            String line = raw.strip();
            if (line.startsWith("#") || line.isEmpty()) {
                continue;
            }
            if (inPatterns) {
                if (line.equals("]")) {
                    inPatterns = false;
                    continue;
                }
                Matcher s = STRING.matcher(line);
                assertThat(s.find()).as("a title_patterns line must be one quoted string: %s", line).isTrue();
                patterns.add(unescape(s.group(1)));
                continue;
            }
            if (line.startsWith("title_patterns")) {
                assertThat(line).as("the array opens on its own line").endsWith("[");
                inPatterns = true;
                continue;
            }
            Matcher mt = MATCH_TAG.matcher(line);
            if (mt.matches()) {
                Matcher s = STRING.matcher(mt.group(1));
                assertThat(s.find()).isTrue();
                tags.add(unescape(s.group(1)));
            }
        }
        return new Parsed(tags, patterns);
    }

    @Test
    void theParserExaminedTheWholeTable() throws IOException {
        Parsed p = parse();
        assertThat(p.tags()).as("tags examined").isEqualTo(DECISION_TAGS);
        assertThat(p.patterns()).as("title patterns examined").isEqualTo(DECISION_PATTERNS);
    }

    @Test
    void generatedSetsEqualTheTable() throws IOException {
        Parsed p = parse();
        assertThat(AuditTags.AUDIT_TAGS)
            .as("AuditTags.java tags must equal audit-tags.toml; regenerate with "
                + "scripts/gen_audit_tags_java.py --write")
            .isEqualTo(p.tags());
        assertThat(AuditTags.TITLE_PATTERN_SOURCES)
            .as("AuditTags.java title patterns must equal audit-tags.toml, in order")
            .isEqualTo(p.patterns());
    }

    @Test
    void parityFailsWhenOneSideGainsATagOrAPattern() throws IOException {
        Parsed p = parse();
        Set<String> moreTags = new HashSet<>(p.tags());
        moreTags.add("review");
        assertThat(AuditTags.AUDIT_TAGS).isNotEqualTo(moreTags);
        List<String> morePatterns = new ArrayList<>(p.patterns());
        morePatterns.add("(^|[-_])audit([-_]|$)");
        assertThat(AuditTags.TITLE_PATTERN_SOURCES).isNotEqualTo(morePatterns);
        Set<String> fewerTags = new HashSet<>(p.tags());
        fewerTags.remove("research");
        assertThat(AuditTags.AUDIT_TAGS).isNotEqualTo(fewerTags);
    }

    // ── Tag normalisation (mirrors tests/test_audit_tags_table.py) ─────────────

    @Test
    void tagsAreSplitOnCommaTrimmedAndMatchedWhole() {
        assertThat(AuditTags.auditTagsOf("kind:gate,bead:nexus-x")).containsExactly("kind:gate");
        assertThat(AuditTags.auditTagsOf(" bead:x , critique ,, rdr:238 ")).containsExactly("critique");
        assertThat(AuditTags.auditTagsOf("gate,research")).containsExactly("gate", "research");
        assertThat(AuditTags.auditTagsOf("\tfix-check\n")).containsExactly("fix-check");
        for (String tag : DECISION_TAGS) {
            assertThat(AuditTags.auditTagsOf(tag)).containsExactly(tag);
        }
    }

    @Test
    void nearMissTagsAreNotAuditTags() {
        for (String raw : new String[] {
            null, "", ",", "bead:nexus-x,rdr:238", "Gate", "KIND:GATE", "gates", "research-notes",
            "kind:gate-extra", "pre-critique", "kind:", "kind:gate bead:x", "review", "code-review"}) {
            assertThat(AuditTags.auditTagsOf(raw)).as("tags %s", raw).isEmpty();
        }
    }

    // ── Title rule ────────────────────────────────────────────────────────────

    @Test
    void realAuditTitlesMatch() {
        for (String title : new String[] {
            "code-review-nexus-d9xt2", "critique-wbfpw28-r3", "nexus-1si7z-critique-round2-2026-07-12",
            "nexus-5b9v0-collision-guard-review-round4",
            "pr539-a2-failure-handling-code-review-round2-2026-10-12",
            "test-suite-compression-P2-reduced-round2-critique", "210-gate-r1-answers-sam-2026-10-17",
            "215-fix-check-dd95743fa", "232-research-24", "x_review", "review", "Critique-X",
            "CODE-REVIEW-1", "abc-commit-review"}) {
            assertThat(AuditTags.titlePatternOf(title)).as("title %s", title).isPresent();
        }
    }

    @Test
    void nearMissTitlesAreNotMatched() {
        for (String title : new String[] {
            null, "", "reviewer-notes", "research-notes", "preview", "previewer-plan", "gates-notes",
            "gated-release", "critiques-index", "238-research-notes", "238-research-9-notes",
            "continuation-state", "rdr-238-a2-read-sample", "project_art_is_the_founding_use_case",
            "chas-test-host-howto"}) {
            Optional<String> hit = AuditTags.titlePatternOf(title);
            assertThat(hit).as("title %s", title).isEmpty();
        }
    }

    @Test
    void firstMatchingPatternIsNamed() {
        assertThat(AuditTags.titlePatternOf("critique-x")).contains("(^|[-_])critique([-_]|$)");
        assertThat(AuditTags.titlePatternOf("232-research-24")).contains("-research-[0-9]+$");
    }

    @Test
    void aLongTitleDoesNotOverflowTheRegexStack() {
        String longTitle = "a-".repeat(20_000) + "notes";
        assertThat(AuditTags.titlePatternOf(longTitle)).isEmpty();
        assertThat(AuditTags.titlePatternOf(longTitle + "-review")).isPresent();
    }
}
