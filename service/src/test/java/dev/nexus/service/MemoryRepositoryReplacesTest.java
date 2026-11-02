// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.MemoryRepository;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.nexus.tables.records.MemoryRecord;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-238 Phase 1 Step 1 (nexus-qo7ie.6): {@link MemoryRepository#replaceMemory} deletes the row
 * named by {@code replaces} and writes the new row in ONE transaction, on real Postgres.
 *
 * <p>Refusals write nothing and delete nothing. Every refusal test snapshots ALL rows of the project
 * (live and quarantined) before and after and requires the two to be equal, so "both rows unchanged
 * and the new row not written" is one assertion over the whole project, not a pair of spot checks.
 *
 * <p>The audit tags and title patterns are typed out here (not read from {@code AuditTags}) so the
 * test fails when the table changes without the decision changing; {@code AuditTagsTest} holds the
 * table to the same lists.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class MemoryRepositoryReplacesTest {

    private static final String SVC_ROLE = "svc_replaces_test";
    private static final String SVC_PASS = "svc_replaces_test_pass";

    PostgreSQLContainer<?> pg;
    MemoryRepository repo;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        repo = new MemoryRepository(new TenantScope(svcDs));
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private static String tenant() { return "tenant-r-" + System.nanoTime(); }
    private static String project() { return "proj-" + System.nanoTime(); }
    private static final OffsetDateTime OLD = OffsetDateTime.now(ZoneOffset.UTC).minusDays(30);

    /** Everything about one row that a refusal must leave alone. */
    private record Row(long id, String title, String content, String tags, String session, String agent,
                       Integer ttl, OffsetDateTime timestamp, int accessCount, OffsetDateTime quarantinedAt) {
        static Row of(MemoryRecord r) {
            return new Row(r.getId(), r.getTitle(), r.getContent(), r.getTags(), r.getSession(), r.getAgent(),
                r.getTtlDays(), r.getTimestamp(), r.getAccessCount(), r.getQuarantinedAt());
        }
    }

    /** Every row of the project, live and quarantined, ordered by title. */
    private List<Row> snapshot(String t, String p) {
        List<Row> rows = new ArrayList<>();
        repo.getAll(t, p).forEach(r -> rows.add(Row.of(r)));
        repo.listQuarantined(t, p).forEach(r -> rows.add(Row.of(r)));
        rows.sort((a, b) -> a.title().compareTo(b.title()));
        return rows;
    }

    private Optional<MemoryRecord> live(String t, String p, String title) {
        return repo.getAll(t, p).stream().filter(r -> r.getTitle().equals(title)).findFirst();
    }

    private long put(String t, String p, String title, String content, String tags) {
        return repo.upsert(t, p, title, content, tags, null, null, 90);
    }

    private MemoryRepository.ReplaceResult replace(String t, String p, String newTitle, String newContent,
                                                   String newTags, String target) {
        return repo.replaceMemory(t, p, newTitle, newContent, newTags, "sess-new", "agent-new", 45, target);
    }

    /** Seed target and an incumbent at the new title, refuse, and require nothing at all changed. */
    private void assertRefused(String t, String p, String target, String expectedFragment,
                               Class<? extends RuntimeException> type, String... mustName) {
        // An incumbent at the new title: a refused replace must not overwrite it either.
        put(t, p, "new-title", "incumbent content", "keep");
        List<Row> before = snapshot(t, p);
        assertThat(before).as("non-vacuity: the project holds rows to compare").hasSizeGreaterThanOrEqualTo(2);

        assertThatThrownBy(() -> replace(t, p, "new-title", "REPLACEMENT", "x", target))
            .isInstanceOf(type)
            .hasMessageContaining(expectedFragment)
            .hasMessageContaining(target)
            .satisfies(e -> {
                for (String s : mustName) assertThat(e.getMessage()).contains(s);
            });

        assertThat(snapshot(t, p)).as("a refusal writes nothing and deletes nothing").isEqualTo(before);
    }

    // ── Case 1: replace in one transaction ────────────────────────────────────

    @Test
    void replace_deletesTheOldRow_writesTheNewOneAsSent_andEchoesTheOldContent() {
        String t = tenant(); String p = project();
        long oldId = put(t, p, "old-note", "old body uniqueoldword", "a,b");
        put(t, p, "bystander", "untouched", "z");

        var res = replace(t, p, "new-note", "new body\nreplaces: old-note", "n1,n2", "old-note");

        assertThat(res.replacedTitle()).isEqualTo("old-note");
        assertThat(res.replacedContent()).isEqualTo("old body uniqueoldword");
        assertThat(res.id()).isNotEqualTo(oldId).isPositive();

        assertThat(live(t, p, "old-note")).as("old row gone from getAll").isEmpty();
        assertThat(repo.findByTitle(t, p, "old-note")).as("old row gone from findByTitle").isEmpty();
        assertThat(repo.findById(t, oldId)).as("old row gone by id").isEmpty();
        assertThat(repo.search(t, "uniqueoldword", p)).as("old row gone from search").isEmpty();

        MemoryRecord n = repo.findByTitle(t, p, "new-note").orElseThrow();
        assertThat(n.getId()).isEqualTo(res.id());
        assertThat(n.getContent()).as("stored exactly as sent").isEqualTo("new body\nreplaces: old-note");
        assertThat(n.getTags()).isEqualTo("n1,n2");
        assertThat(n.getSession()).isEqualTo("sess-new");
        assertThat(n.getAgent()).isEqualTo("agent-new");
        assertThat(n.getTtlDays()).isEqualTo(45);

        assertThat(live(t, p, "bystander")).as("a neighbour is untouched").isPresent();
        assertThat(repo.getAll(t, p)).hasSize(2);
    }

    @Test
    void replace_whenTheNewTitleAlreadyExists_overwritesIt_likeAPlainPut() {
        String t = tenant(); String p = project();
        put(t, p, "old", "old body", "a");
        long incumbent = put(t, p, "new", "incumbent", "a");

        var res = replace(t, p, "new", "fresh", "b", "old");

        assertThat(res.id()).as("the upsert path keeps the incumbent id").isEqualTo(incumbent);
        assertThat(live(t, p, "old")).isEmpty();
        assertThat(live(t, p, "new").orElseThrow().getContent()).isEqualTo("fresh");
    }

    // ── Case 2: refusals ──────────────────────────────────────────────────────

    @Test
    void refuses_aMissingTarget_naming_it() {
        String t = tenant(); String p = project();
        put(t, p, "something", "else", "a");
        assertRefused(t, p, "no-such-title", "no row titled", IllegalStateException.class, p);
    }

    @Test
    void refuses_theRowBeingWritten_self() {
        String t = tenant(); String p = project();
        put(t, p, "same", "body", "a");
        // The incumbent helper writes "new-title"; self-replace names the title being written.
        put(t, p, "new-title", "incumbent content", "keep");
        List<Row> before = snapshot(t, p);
        assertThatThrownBy(() -> replace(t, p, "same", "REPLACEMENT", "x", "same"))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("same")
            .hasMessageContaining("title being written");
        assertThat(snapshot(t, p)).isEqualTo(before);
    }

    @Test
    void refuses_aQuarantinedTarget_asMissingWouldHideIt() {
        String t = tenant(); String p = project();
        long id = repo.importRow(t, p, "expired-note", "expired body", "a", null, null, 1, OLD, 0, null);
        assertThat(repo.expire(t).quarantinedIds()).containsExactly(id);
        assertThat(live(t, p, "expired-note")).as("hidden from reads").isEmpty();
        assertRefused(t, p, "expired-note", "quarantined", IllegalStateException.class);
        assertThat(repo.listQuarantined(t, p)).as("still quarantined, not deleted").hasSize(1);
    }

    @ParameterizedTest
    @ValueSource(strings = {"kind:gate", "gate", "critique", "research", "fix-check", "commit-review"})
    void refuses_aTargetCarryingEachAuditTag(String tag) {
        String t = tenant(); String p = project();
        // Clean title on purpose: only the TAG can make this refusal.
        put(t, p, "plain-title", "body", tag);
        assertRefused(t, p, "plain-title", "audit tag", IllegalStateException.class, tag);
    }

    @Test
    void refuses_aTargetTaggedKindGateAmongOtherTags() {
        String t = tenant(); String p = project();
        put(t, p, "plain-title", "body", "kind:gate,bead:nexus-x");
        assertRefused(t, p, "plain-title", "audit tag", IllegalStateException.class, "kind:gate");
    }

    @Test
    void refuses_aTagWrappedInSpacesAndCommas() {
        String t = tenant(); String p = project();
        put(t, p, "plain-title", "body", "bead:x , research ,rdr:238");
        assertRefused(t, p, "plain-title", "audit tag", IllegalStateException.class, "research");
    }

    @Test
    void doesNotRefuse_nearMissTags_asWholeTagsOnly() {
        for (String tags : List.of("Gate", "gates", "research-notes", "kind:gate-extra", "pre-critique",
                                   "kind:gate bead:x", "code-review", "review")) {
            String t = tenant(); String p = project();
            put(t, p, "plain-title", "body", tags);
            var res = replace(t, p, "next", "n", "x", "plain-title");
            assertThat(res.replacedContent()).as("tags %s replaceable", tags).isEqualTo("body");
            assertThat(live(t, p, "plain-title")).as("tags %s: old row gone", tags).isEmpty();
        }
    }

    // ── Title backstop (K4 option a): one refusal per pattern, clean tags ─────

    /** One real-shaped title per pattern, in table order, with the pattern it must be refused by. */
    static Stream<org.junit.jupiter.params.provider.Arguments> auditTitlePerPattern() {
        return Stream.of(
            args("nexus-5b9v0-collision-guard-review-round4", "(^|[-_])review([-_]|$)"),
            args("pr539-a2-failure-handling-code-review-round2-2026-10-12", "(^|[-_])review([-_]|$)"),
            args("abc-commit-review", "(^|[-_])review([-_]|$)"),
            args("test-suite-compression-P2-reduced-round2-critique", "(^|[-_])critique([-_]|$)"),
            args("210-gate-r1-answers-sam-2026-10-17", "(^|[-_])gate([-_]|$)"),
            args("215-fix-check-dd95743fa", "(^|[-_])fix-check([-_]|$)"),
            args("232-research-24", "-research-[0-9]+$"),
            args("confirm-dsr6a-431c62543-2026-10-17", "(^|[-_])confirm([-_]|$)"),
            args("225-plan-audit-r1", "(^|[-_])audit([-_]|$)"));
    }

    private static org.junit.jupiter.params.provider.Arguments args(String title, String firstPattern) {
        return org.junit.jupiter.params.provider.Arguments.of(title, firstPattern);
    }

    @ParameterizedTest
    @org.junit.jupiter.params.provider.MethodSource("auditTitlePerPattern")
    void refuses_aTargetWhoseTitleMatchesAnAuditPattern_evenWithCleanTags(String title, String pattern) {
        String t = tenant(); String p = project();
        put(t, p, title, "body of " + title, "bead:nexus-x");
        assertRefused(t, p, title, "audit-record title pattern", IllegalStateException.class, pattern);
    }

    /** The code-review and commit-review patterns are reached when the review pattern is not first. */
    @Test
    void codeReviewAndCommitReviewPatternsAreShadowedByTheReviewToken() {
        // The first-match message names the earliest pattern. code-review and commit-review titles
        // contain the token review, so "review" names them; they stay in the table as explicit entries.
        assertThat(dev.nexus.service.db.AuditTags.titlePatternOf("x-code-review-y"))
            .contains("(^|[-_])review([-_]|$)");
        assertThat(dev.nexus.service.db.AuditTags.TITLE_PATTERN_SOURCES).hasSize(9);
    }

    @ParameterizedTest
    @ValueSource(strings = {
        "code-review-nexus-d9xt2", "critique-wbfpw28-r3", "nexus-1si7z-critique-round2-2026-07-12",
        "Critique-X", "x_review", "review"})
    void refuses_theRealUntaggedAuditTitles(String title) {
        String t = tenant(); String p = project();
        put(t, p, title, "body", "");
        assertRefused(t, p, title, "audit-record title pattern", IllegalStateException.class);
    }

    @ParameterizedTest
    @ValueSource(strings = {"reviewer-notes", "research-notes", "preview", "gates-notes", "238-research-notes",
                            "continuation-state", "rdr-238-a2-read-sample", "chas-test-host-howto",
                            "confirmation-plan", "auditor-notes"})
    void doesNotRefuse_nearMissTitles(String title) {
        String t = tenant(); String p = project();
        put(t, p, title, "body of " + title, "bead:nexus-x");
        var res = replace(t, p, "next", "n", "x", title);
        assertThat(res.replacedTitle()).isEqualTo(title);
        assertThat(live(t, p, title)).as("old row gone").isEmpty();
    }

    @Test
    void titleMatchingIsCaseInsensitive() {
        String t = tenant(); String p = project();
        put(t, p, "CODE-REVIEW-ROUND2", "body", "");
        assertRefused(t, p, "CODE-REVIEW-ROUND2", "audit-record title pattern", IllegalStateException.class);
    }

    @Test
    void anAuditTagAndAQuarantineAndAMatchingTitle_areCheckedInOrder_andOneMessageIsGiven() {
        String t = tenant(); String p = project();
        long id = repo.importRow(t, p, "critique-q", "b", "gate", null, null, 1, OLD, 0, null);
        assertThat(repo.expire(t).quarantinedIds()).containsExactly(id);
        // Quarantine is reported first: restoring is the writer's next step, not an audit lecture.
        assertRefused(t, p, "critique-q", "quarantined", IllegalStateException.class);
    }

    // ── Case 4: body mentions kind:gate, tags clean ───────────────────────────

    @Test
    void doesNotRefuse_aBodyThatMentionsKindGate_whenTheTagsAreClean() {
        String t = tenant(); String p = project();
        put(t, p, "howto-note", "this note explains kind:gate and critique tags in prose", "howto,bead:nexus-x");

        var res = replace(t, p, "howto-note-v2", "v2", "howto", "howto-note");

        assertThat(res.replacedContent()).contains("kind:gate");
        assertThat(live(t, p, "howto-note")).isEmpty();
        assertThat(live(t, p, "howto-note-v2")).isPresent();
    }

    // ── Case 5: a numeric title is a title, never an id ───────────────────────

    @Test
    void aNumericTitleIsReadAsATitle_neverAsAnId() {
        String t = tenant(); String p = project();
        long decoy = put(t, p, "decoy", "decoy body", "a");
        // The decoy's id as a string must be MISSING, not resolved to the decoy by id.
        List<Row> before = snapshot(t, p);
        assertThatThrownBy(() -> replace(t, p, "next", "n", "x", String.valueOf(decoy)))
            .isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("no row titled")
            .hasMessageContaining(String.valueOf(decoy));
        assertThat(snapshot(t, p)).isEqualTo(before);

        put(t, p, "238", "the row titled 238", "a");
        var res = replace(t, p, "next", "n", "x", "238");
        assertThat(res.replacedTitle()).isEqualTo("238");
        assertThat(res.replacedContent()).isEqualTo("the row titled 238");
        assertThat(live(t, p, "decoy")).as("the decoy was never touched").isPresent();
    }

    // ── Case 6: another project, another tenant ───────────────────────────────

    @Test
    void aTitleThatExistsOnlyInAnotherProject_isMissing() {
        String t = tenant(); String p = project(); String other = project();
        put(t, other, "elsewhere", "other project body", "a");
        put(t, p, "here", "here body", "a");
        List<Row> beforeP = snapshot(t, p);
        List<Row> beforeOther = snapshot(t, other);

        assertThatThrownBy(() -> replace(t, p, "next", "n", "x", "elsewhere"))
            .isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("no row titled")
            .hasMessageContaining("elsewhere");

        assertThat(snapshot(t, p)).isEqualTo(beforeP);
        assertThat(snapshot(t, other)).as("the other project's row survives").isEqualTo(beforeOther);
    }

    @Test
    void aTitleThatExistsOnlyInAnotherTenant_isMissing() {
        String t1 = tenant(); String t2 = tenant(); String p = project();
        put(t2, p, "theirs", "tenant two body", "a");
        assertThatThrownBy(() -> replace(t1, p, "next", "n", "x", "theirs"))
            .isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("no row titled");
        assertThat(snapshot(t2, p)).hasSize(1);
    }

    // ── Case 7: a chain A, B, C leaves only C ─────────────────────────────────

    @Test
    void aChainOfReplacesLeavesOnlyTheLast_withItsLineage() {
        String t = tenant(); String p = project();
        put(t, p, "A", "A body", "x");
        // Each put sends its content with the lineage line already appended, as the client will.
        replace(t, p, "B", "B body\nreplaces: A", "x", "A");
        var last = replace(t, p, "C", "C body\nreplaces: B", "x", "B");

        assertThat(last.replacedTitle()).isEqualTo("B");
        assertThat(last.replacedContent()).isEqualTo("B body\nreplaces: A");
        List<MemoryRecord> rows = repo.getAll(t, p);
        assertThat(rows).extracting(MemoryRecord::getTitle).containsExactly("C");
        assertThat(rows.get(0).getContent()).endsWith("replaces: B");
    }

    // ── Atomicity: a failing write after the delete rolls the delete back ─────

    @Test
    void aFailureWritingTheNewRow_rollsBackTheDelete() {
        String t = tenant(); String p = project();
        put(t, p, "old-note", "old body", "a");
        List<Row> before = snapshot(t, p);

        // ttl 0 violates memory_ttl_days_positive_chk at the INSERT, after the target was deleted.
        assertThatThrownBy(() -> repo.replaceMemory(t, p, "new-note", "n", "x", null, null, 0, "old-note"))
            .isInstanceOf(RuntimeException.class);

        assertThat(snapshot(t, p)).as("the delete rolled back with the failed insert").isEqualTo(before);
    }
}
