// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.db.DataMoveChangesetRules.Changeset;
import dev.nexus.service.db.DataMoveChangesetRules.Entry;
import dev.nexus.service.db.DataMoveChangesetRules.Frozen;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.TreeSet;
import java.util.regex.Pattern;
import java.util.stream.Stream;

/**
 * RDR-246 Phase 1 Step 5 (nexus-f3yqd.2.19): the check that stops a new boot-time data move. An upgrade boot must not rewrite
 * or scan the rows of a table that grows with user data, so a changeset that does is refused, and the move belongs in
 * {@code nexus.data_moves} (registered by one INSERT; the engine's worker runs it after boot). Three rules:
 *
 * <ol>
 *   <li><b>Row DML</b>: UPDATE, DELETE, MERGE, INSERT ... SELECT, ALTER COLUMN TYPE, a generated column added, CREATE TABLE AS
 *       (and a data-modifying WITH, SELECT INTO). A registering INSERT into {@code nexus.data_moves} passes: its template,
 *       verification and on_complete are string values, masked.</li>
 *   <li><b>Boot scans</b> on {@code nexus.chunks} and its per-model tables ({@code chunks_*}), {@code catalog_document_chunks},
 *       {@code catalog_documents}, {@code catalog_links} and {@code topic_assignments}: ADD CONSTRAINT without NOT VALID,
 *       VALIDATE CONSTRAINT and SET NOT NULL.</li>
 *   <li><b>Waiting for a move</b>: DROP COLUMN, DROP TABLE or a rename touching a table a registered move drives needs a
 *       precondition with {@code onFail="CONTINUE"} that reads {@code nexus.data_moves.completed_at} for one of that table's
 *       moves. Registered means by an INSERT in this or an earlier changeset, in the order of {@code db.changelog-master.xml}.
 *       A table that grows is, when it is a chunk-family table, refused outright (with or without the precondition) by
 *       {@code tests/test_changelog_continue_precondition_chunks_lint.py}; this gate does not repeat that.</li>
 * </ol>
 *
 * <p><b>Exemption line.</b> A line {@code data-move-exempt: <table>: <reason>} in the changeset's {@code <comment>}, naming a table
 * the changeset touches whose size does not grow with user data, lifts all three rules for that changeset. The RDR fixes the
 * meaning, not the spelling; this is the spelling. A line that names a table rule 2 lists, a chunk table or a driven table, or a
 * table the changeset's statements never name, is itself a finding ({@code exemption}) and lifts nothing.
 *
 * <p><b>The frozen list</b> ({@code src/test/resources/data-move-gate/frozen-changesets.txt}) holds the shipped changesets the
 * rules find, as {@code file::id}, derived at this test's revision. It is exactly the offenders: an entry the rules no longer
 * find is stale and fails, and an entry in a changelog after the {@code freeze-point} include fails, so a new changeset cannot be
 * added to it. The list only shrinks (a changeset leaves it when the rules stop finding it, never when it is edited to pass).
 *
 * <p><b>Limits</b>, as the RDR states them. The check reads changelog text: DML inside a {@code sqlFile}, a function body or the
 * dynamic SQL of a DO block passes it (all are masked or outside {@code <sql>}), and so does a non-concurrent CREATE INDEX.
 * nexus-f3yqd.1's write gate, which measures the WAL an upgrade boot writes, is the backstop for DML this cannot see. A
 * read-only scan writes little and is not caught; rule 2 covers those scans on the listed tables. Rule 3 matches by table name,
 * so it cannot see which move a changeset waits for: it requires the precondition to name the label of one move that drives the
 * table, and the review checks it is the right one. The exemption line is checked for form, not for truth (that the table does
 * not grow is the review's). Not covered either: ADD COLUMN with an inline constraint, MATERIALIZED VIEW, TRUNCATE, a rename of
 * a table that a registered move names only in its template.
 *
 * <p>Reads {@code db/changelog} only. {@code db/vendor} holds the vendored library changelogs (ddl_utils, dml_utils), which are
 * not nexus changesets and never run as nexus's own schema.
 *
 * <p>Non-vacuity: the gate must read as many changesets as the files hold {@code <changeSet>} elements and more than zero, the
 * frozen list must be non-empty, and the shipped tree must show each kind of rule 1 and 2 finding at least once (a rule that
 * stopped matching would otherwise freeze nothing and pass). Rule 3 sees no registered move in the shipped tree (none is
 * registered yet), so its cases are the fixture scenarios under {@code src/test/resources/data-move-gate/scenarios}. SQL for
 * the cases lives there, as XML, not in this file.
 */
class DataMoveChangesetGateTest {

    private static final Path CHANGELOG_DIR = Path.of("src", "main", "resources", "db", "changelog");
    private static final Path VENDOR_DIR = Path.of("src", "main", "resources", "db", "vendor");
    private static final Path MASTER = CHANGELOG_DIR.resolve("db.changelog-master.xml");
    private static final Path DATA = Path.of("src", "test", "resources", "data-move-gate");
    private static final Path FROZEN = DATA.resolve("frozen-changesets.txt");
    private static final Path SCENARIOS = DATA.resolve("scenarios");

    private static final Frozen NONE = new Frozen(Set.of(), null);

    // ---- the gate over the shipped changelogs ------------------------------------------------------------------------

    @Test
    void theGateOverTheShippedChangelogs_isGreenUnderTheFrozenList() throws Exception {
        List<String> includes = DataMoveChangesetRules.includes(MASTER);
        List<Path> files = shippedFiles(includes);
        List<Entry> entries = DataMoveChangesetRules.evaluate(DataMoveChangesetRules.read(files));

        int declared = declaredChangesets(files);
        Frozen frozen = Frozen.parse(Files.readAllLines(FROZEN));
        List<String> problems = DataMoveChangesetRules.frozenProblems(frozen, includes, entries);
        List<Entry> offenders = DataMoveChangesetRules.offenders(entries, frozen);

        Map<String, Integer> kinds = new TreeMap<>();
        int withFindings = 0;
        int exempt = 0;
        int opaque = 0;
        for (Entry e : entries) {
            if (!e.findings().isEmpty()) withFindings++;
            if (e.exempt()) exempt++;
            opaque += e.changeset().opaqueChildren();
            for (String k : e.kinds()) kinds.merge(k, 1, Integer::sum);
        }
        System.out.println("data-move changeset gate: files=" + files.size() + " examined=" + entries.size() + " declared=" + declared
            + " withFindings=" + withFindings + " frozen=" + frozen.keys().size() + " exempt=" + exempt + " offenders=" + offenders.size()
            + " opaqueChildren=" + opaque + " freezePoint=" + frozen.freezePoint() + " kinds=" + kinds);

        assertThat(entries.size()).as("non-vacuity: the gate read every changeset the files declare").isEqualTo(declared);
        assertThat(entries.size()).as("non-vacuity: it read changesets").isGreaterThan(0);
        assertThat(frozen.keys()).as("non-vacuity: the frozen list holds the shipped offenders").isNotEmpty();
        assertThat(kinds.keySet()).as("non-vacuity: every kind of row-DML and boot-scan finding shows up in the shipped tree")
            .contains("r1.update", "r1.delete", "r1.insert-select", "r1.alter-type", "r1.generated", "r1.ctas",
                "r2.add-constraint", "r2.validate", "r2.set-not-null");
        assertThat(problems).as("the frozen list is exactly today's offenders and never grows").isEmpty();
        assertThat(offenders.stream().map(e -> e.changeset().key() + "  " + e.findings()).toList())
            .as("changesets that rewrite or scan a table that grows with user data at boot. Register the move through nexus.data_moves "
                + "(RDR-246) instead; the frozen list is closed and an exemption line names only a table that does not grow")
            .isEmpty();
    }

    @Test
    void onlyTheNexusChangelogsAreRead_notTheVendoredLibraries() throws Exception {
        List<Path> files = shippedFiles(DataMoveChangesetRules.includes(MASTER));
        assertThat(Files.isDirectory(VENDOR_DIR)).as("the vendored changelogs exist beside the nexus ones").isTrue();
        assertThat(files).as("no file under db/vendor is read").noneMatch(p -> p.startsWith(VENDOR_DIR));
        assertThat(files).allMatch(p -> p.getParent().equals(CHANGELOG_DIR));
    }

    @Test
    void aNewOffenderAfterTheShippedChangelogs_isRed_andRegisteringTheSameMoveIsGreen() throws Exception {
        List<String> includes = new ArrayList<>(DataMoveChangesetRules.includes(MASTER));
        List<Path> files = new ArrayList<>(shippedFiles(includes));
        List<Path> fixture = scenarioFiles("appended-offender");
        for (Path f : fixture) {
            files.add(f);
            includes.add(f.getFileName().toString());
        }
        List<Entry> entries = DataMoveChangesetRules.evaluate(DataMoveChangesetRules.read(files));
        Frozen frozen = Frozen.parse(Files.readAllLines(FROZEN));

        assertThat(DataMoveChangesetRules.frozenProblems(frozen, includes, entries)).isEmpty();
        assertThat(actual(DataMoveChangesetRules.offenders(entries, frozen)))
            .as("the frozen list admits no new changeset: the validating ADD CONSTRAINT and the UPDATE on chunks are red, the INSERT "
                + "into nexus.data_moves that registers the same kind of move is not")
            .isEqualTo(expected("appended-offender"));
    }

    // ---- the rules, over fixture changelogs --------------------------------------------------------------------------

    @Test
    void rule1_rowDml_isRed() throws Exception {
        scenario("rule1-red");
    }

    @Test
    void rule1_aRegisteringInsert_andDmlWordsInText_areNotFlagged() throws Exception {
        scenario("rule1-green");
    }

    @Test
    void rule2_bootScansOnATableThatGrows_areRed() throws Exception {
        scenario("rule2-red");
    }

    @Test
    void rule2_notValidAndSmallTables_areGreen() throws Exception {
        scenario("rule2-green");
    }

    @Test
    void rule3_aDestructiveStatementOnARegisteredMovesTable_withoutThePrecondition_isRed() throws Exception {
        scenario("rule3-red");
    }

    @Test
    void rule3_thePreconditionAndAChangesetBeforeTheRegistration_areGreen() throws Exception {
        scenario("rule3-green");
    }

    @Test
    void anExemptionLineForASmallTable_liftsAllThreeRules() throws Exception {
        List<Entry> entries = scenario("exemption-green");
        assertThat(entries).filteredOn(Entry::exempt).as("the exemption was applied, not merely absent a finding").hasSize(1);
        assertThat(entries).filteredOn(e -> !e.findings().isEmpty()).as("the exempt changeset has findings the line lifts").hasSize(1);
    }

    @Test
    void anExemptionLineNamingAGrowingTableADrivenTableOrAnUntouchedTable_liftsNothing() throws Exception {
        scenario("exemption-red");
    }

    @Test
    void aRegisteringInsertTheGateCannotRead_isRed() throws Exception {
        scenario("registering-unreadable");
    }

    // ---- the frozen list ---------------------------------------------------------------------------------------------

    @Test
    void aFrozenListEntryExemptsItsChangesetOnly_andOnlyWhileTheRulesStillFindIt() throws Exception {
        List<Path> files = scenarioFiles("rule1-red");
        List<Entry> entries = DataMoveChangesetRules.evaluate(DataMoveChangesetRules.read(files));
        List<String> includes = List.of("01-row-dml.xml");

        Frozen some = new Frozen(Set.of("01-row-dml.xml::r1-update", "01-row-dml.xml::r1-delete"), "01-row-dml.xml");
        assertThat(DataMoveChangesetRules.frozenProblems(some, includes, entries)).isEmpty();
        assertThat(actual(DataMoveChangesetRules.offenders(entries, some))).doesNotContainKeys("01-row-dml.xml::r1-update", "01-row-dml.xml::r1-delete")
            .containsKey("01-row-dml.xml::r1-merge");

        List<Entry> green = DataMoveChangesetRules.evaluate(DataMoveChangesetRules.read(scenarioFiles("rule2-green")));
        Frozen stale = new Frozen(Set.of("01-not-boot-scans.xml::g-small-table", "01-not-boot-scans.xml::no-such-id"), "01-not-boot-scans.xml");
        assertThat(DataMoveChangesetRules.frozenProblems(stale, List.of("01-not-boot-scans.xml"), green))
            .anyMatch(p -> p.startsWith("stale frozen entry") && p.endsWith("g-small-table"))
            .anyMatch(p -> p.startsWith("frozen entry names no changeset") && p.endsWith("no-such-id"));
    }

    @Test
    void aFrozenEntryAfterTheFreezePoint_isRefused() throws Exception {
        List<Entry> entries = DataMoveChangesetRules.evaluate(DataMoveChangesetRules.read(scenarioFiles("rule3-red")));
        List<String> includes = List.of("01-register.xml", "02-destructive.xml");
        Frozen late = new Frozen(Set.of("02-destructive.xml::r3-drop-column"), "01-register.xml");
        assertThat(DataMoveChangesetRules.frozenProblems(late, includes, entries))
            .singleElement().asString().contains("after the freeze point").contains("r3-drop-column");
        Frozen unknownPoint = new Frozen(Set.of(), "nowhere.xml");
        assertThat(DataMoveChangesetRules.frozenProblems(unknownPoint, includes, entries)).singleElement().asString().contains("not an include");
    }

    // ---- helpers -----------------------------------------------------------------------------------------------------

    /** The master's includes as files of {@code db/changelog}; every file there but the master must be one of them. */
    private static List<Path> shippedFiles(List<String> includes) throws IOException {
        List<Path> files = new ArrayList<>();
        for (String name : includes) {
            Path p = CHANGELOG_DIR.resolve(name);
            assertThat(p).as("an include of the master that exists").exists();
            files.add(p);
        }
        Set<String> onDisk = new TreeSet<>();
        try (Stream<Path> walk = Files.list(CHANGELOG_DIR)) {
            walk.filter(p -> p.toString().endsWith(".xml")).forEach(p -> onDisk.add(p.getFileName().toString()));
        }
        onDisk.remove(MASTER.getFileName().toString());
        assertThat(includes).as("the master includes each changelog once, so the gate reads every one").doesNotHaveDuplicates()
            .containsExactlyInAnyOrderElementsOf(onDisk);
        return files;
    }

    private static int declaredChangesets(List<Path> files) throws IOException {
        Pattern changeSet = Pattern.compile("<changeSet\\s");
        int n = 0;
        for (Path f : files) {
            n += (int) changeSet.matcher(Files.readString(f).replaceAll("(?s)<!--.*?-->", "")).results().count();
        }
        return n;
    }

    private static List<Path> scenarioFiles(String scenario) throws IOException {
        try (Stream<Path> list = Files.list(SCENARIOS.resolve(scenario))) {
            return list.filter(p -> p.toString().endsWith(".xml")).sorted().toList();
        }
    }

    /** Offending changesets as {@code file::id} to the sorted kinds of their findings. */
    private static Map<String, String> actual(List<Entry> offenders) {
        Map<String, String> out = new TreeMap<>();
        for (Entry e : offenders) out.put(e.changeset().key(), String.join(",", new TreeSet<>(e.kinds())));
        return out;
    }

    private static Map<String, String> expected(String scenario) throws IOException {
        Map<String, String> out = new TreeMap<>();
        for (String line : Files.readAllLines(SCENARIOS.resolve(scenario).resolve("expect.txt"))) {
            if (line.isBlank() || line.startsWith("#")) continue;
            String[] parts = line.trim().split("\\s+");
            assertThat(parts).as("expect.txt line: " + line).hasSize(2);
            out.put(parts[0], String.join(",", new TreeSet<>(List.of(parts[1].split(",")))));
        }
        return out;
    }

    /** Runs a scenario with no frozen list and compares the offenders, with their kinds, to its expect.txt exactly. */
    private static List<Entry> scenario(String name) throws Exception {
        List<Changeset> changesets = DataMoveChangesetRules.read(scenarioFiles(name));
        assertThat(changesets).as("non-vacuity: scenario " + name + " read changesets").isNotEmpty();
        List<Entry> entries = DataMoveChangesetRules.evaluate(changesets);
        assertThat(actual(DataMoveChangesetRules.offenders(entries, NONE))).as("scenario " + name).isEqualTo(expected(name));
        return entries;
    }
}
