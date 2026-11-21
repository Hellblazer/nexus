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
 *   <li><b>Waiting for a move</b>: dropping a column (DROP [COLUMN] x, with or without COLUMN and IF EXISTS), DROP TABLE, or a
 *       rename (of the table or a column, RENAME [COLUMN] x TO y) touching a table a registered move drives needs a
 *       precondition with {@code onFail="CONTINUE"} that reads {@code nexus.data_moves.completed_at} for one of that table's
 *       moves. DROP CONSTRAINT, RENAME CONSTRAINT and ALTER COLUMN ... DROP DEFAULT / DROP NOT NULL are not destructive here.
 *       A superseded move's label and its replacement's label both count as a move that drives the table (RDR-246, Finishing a
 *       move). Registered means by an INSERT in this or an earlier changeset, in the order of {@code db.changelog-master.xml}.
 *       A table that grows is, when it is a chunk-family table, refused outright (with or without the precondition) by
 *       {@code tests/test_changelog_continue_precondition_chunks_lint.py}; this gate does not repeat that.</li>
 * </ol>
 *
 * <p><b>Exemption line.</b> A line {@code data-move-exempt: <table>: <reason>} in the changeset's {@code <comment>}, naming a table
 * the changeset touches whose size does not grow with user data, lifts the rules for that table in that changeset; a changeset touching two small tables carries one line for each. The RDR fixes
 * the meaning, not the spelling; this is the spelling. The line lifts only the findings whose table is the named one: a finding on
 * any other table stays, and so does one whose statement also names a table that grows or that a move drives (a subquery, a FROM).
 * That scope is narrower than the RDR sentence (which says the line lifts all three rules for the changeset); it is the review's
 * recommendation to the RDR owner and is isolated in {@code DataMoveChangesetRules.lifts}. A line that names a table rule 2 lists, a
 * chunk table or a driven table, or a table the changeset never names, is itself a finding ({@code exemption}) and lifts nothing.
 *
 * <p><b>The frozen list</b> ({@code src/test/resources/data-move-gate/frozen-changesets.txt}) holds the shipped changesets the
 * rules find, as {@code file::id::author}, derived at this test's revision. The test checks that every entry names a changeset,
 * that every entry still has a finding (a stale entry fails, so the list is the offenders and no more), and that no entry sits in
 * the {@code freeze-point} include (the last one at freeze, taxonomy-023, which has none) or after it. So a changeset added to a
 * newer include cannot be frozen. It does not check a new changeset added to an include before the freeze point; review does.
 * The list only shrinks.
 *
 * <p><b>Limits</b>, as the RDR states them. The check reads changelog text: DML inside a {@code sqlFile}, a function body or the
 * dynamic SQL of a DO block passes it (all are masked or outside {@code <sql>}), and so does a non-concurrent CREATE INDEX.
 * nexus-f3yqd.1's write gate, which measures the WAL an upgrade boot writes, is the backstop for DML this cannot see. A
 * read-only scan writes little and is not caught; rule 2 covers those scans on the listed tables. Rule 3 matches by table name,
 * so it cannot see which move a changeset waits for: it requires the precondition to name the label of one move that drives the
 * table, and the review checks it is the right one. The exemption line is checked for form, not for truth (that the table does
 * not grow is the review's). Not covered either: ADD COLUMN with an inline constraint, MATERIALIZED VIEW, TRUNCATE, a rename of
 * a table that a registered move names only in its template, and {@code INSERT INTO t TABLE x} (it copies rows with no SELECT keyword
 * for the rule to see; INSERT ... SELECT is what rule 1 names). The rules read {@code <sql>} only: a changeset whose child is a native
 * Liquibase change type ({@code update}, {@code modifyDataType}, {@code addNotNullConstraint}, {@code customChange} and the like)
 * is itself a finding ({@code opaque.native-change}) unless frozen or exempted for that change's {@code tableName}; a
 * {@code sqlFile} child passes, as the RDR says, and is counted and printed ({@code sqlFiles}) but never read. So do the children
 * that cannot write or scan a table ({@code empty}, {@code output}, {@code tagDatabase}, {@code stop}, besides {@code rollback} and
 * {@code validCheckSum}). The shipped tree has no native change and the test asserts it.
 *
 * <p>Reads {@code db/changelog} only. {@code db/vendor} holds the vendored library changelogs (ddl_utils, dml_utils), which are
 * not nexus changesets and never run as nexus's own schema.
 *
 * <p>Non-vacuity: the gate must read as many changesets as the files hold {@code <changeSet>} elements and more than zero, the
 * frozen list must be non-empty, and the shipped tree must show each kind of rule 1 and 2 finding at least once (a rule that
 * stopped matching would otherwise freeze nothing and pass). Rule 3 sees three registered moves in the shipped tree (vectors-064,
 * nexus-f3yqd.2.23, the first; {@code rdr229-reown-repo-stems-from-curator}, vectors-067; and
 * {@code rdr229-residue-catalog-documents}, catalog-049-2), and its cases are the fixture scenarios under
 * {@code src/test/resources/data-move-gate/scenarios}. SQL for
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
        int sqlFiles = 0;
        for (Entry e : entries) {
            if (!e.findings().isEmpty()) withFindings++;
            if (e.exempt()) exempt++;
            opaque += e.changeset().opaque().size();
            sqlFiles += e.changeset().sqlFiles();
            for (String k : e.kinds()) kinds.merge(k, 1, Integer::sum);
        }
        System.out.println("data-move changeset gate: files=" + files.size() + " examined=" + entries.size() + " declared=" + declared
            + " withFindings=" + withFindings + " frozen=" + frozen.keys().size() + " exempt=" + exempt + " offenders=" + offenders.size()
            + " nativeChanges=" + opaque + " sqlFiles=" + sqlFiles + " freezePoint=" + frozen.freezePoint() + " kinds=" + kinds);

        assertThat(entries.size()).as("non-vacuity: the gate read every changeset the files declare").isEqualTo(declared);
        assertThat(entries.size()).as("non-vacuity: it read changesets").isGreaterThan(0);
        assertThat(opaque).as("the shipped changelogs hold only <sql> changes (a native change type is invisible to the rules)").isZero();
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
        List<Path> vendored = new ArrayList<>();
        try (Stream<Path> walk = Files.walk(VENDOR_DIR)) {
            walk.filter(p -> p.toString().endsWith(".xml")).sorted().forEach(vendored::add);
        }
        List<Changeset> vendoredChangesets = DataMoveChangesetRules.read(vendored);
        assertThat(vendoredChangesets).as("non-vacuity: the vendored libraries carry changesets of their own").isNotEmpty();

        List<Changeset> examined = DataMoveChangesetRules.read(shippedFiles(DataMoveChangesetRules.includes(MASTER)));
        Set<String> examinedKeys = new TreeSet<>();
        for (Changeset c : examined) examinedKeys.add(c.key());
        assertThat(vendoredChangesets).as("no vendored changeset is among the ones the gate examined")
            .noneMatch(c -> examinedKeys.contains(c.key()))
            .noneMatch(c -> examined.stream().anyMatch(n -> n.id().equals(c.id())));
        assertThat(examined).as("no examined changeset comes from a file under db/vendor")
            .noneMatch(c -> vendored.stream().anyMatch(v -> v.getFileName().toString().equals(c.file())));
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
    void anExemptionLineForASmallTable_liftsTheFindingsOnThatTable() throws Exception {
        List<Entry> entries = scenario("exemption-green");
        assertThat(entries).filteredOn(Entry::exempt).as("the exemption was applied, not merely absent a finding").hasSize(3);
        assertThat(entries).filteredOn(e -> !e.findings().isEmpty() && e.unlifted().isEmpty())
            .as("each exempt changeset has findings the line lifts").hasSize(3);
    }

    @Test
    void anExemptionLineNamingAGrowingTableADrivenTableOrAnUntouchedTable_liftsNothing() throws Exception {
        scenario("exemption-red");
    }

    @Test
    void aNativeChangeType_isRed_unlessExemptedForItsTable_andASqlFileIsNotRead() throws Exception {
        scenario("opaque-native");
    }

    @Test
    void aSupersessionChain_countsEveryLaterLabelForTheEarlierTables() throws Exception {
        scenario("rule3-supersession-chain");
    }

    @Test
    void aRegisteringInsertsVerification_withAQuestionMarkOrAPlaceholderBeyondDollarOne_isRed() throws Exception {
        scenario("verification-placeholder");
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
        List<String> includes = List.of("01-row-dml.xml", "99-freeze-point.xml");

        Frozen some = new Frozen(Set.of("01-row-dml.xml::r1-update::fixture", "01-row-dml.xml::r1-delete::fixture"), "99-freeze-point.xml");
        assertThat(DataMoveChangesetRules.frozenProblems(some, includes, entries)).isEmpty();
        assertThat(actual(DataMoveChangesetRules.offenders(entries, some))).doesNotContainKeys("01-row-dml.xml::r1-update::fixture", "01-row-dml.xml::r1-delete::fixture")
            .containsKey("01-row-dml.xml::r1-merge::fixture");

        List<Entry> green = DataMoveChangesetRules.evaluate(DataMoveChangesetRules.read(scenarioFiles("rule2-green")));
        Frozen stale = new Frozen(Set.of("01-not-boot-scans.xml::g-small-table::fixture", "01-not-boot-scans.xml::no-such-id::fixture"), "99-freeze-point.xml");
        assertThat(DataMoveChangesetRules.frozenProblems(stale, List.of("01-not-boot-scans.xml", "99-freeze-point.xml"), green))
            .anyMatch(p -> p.startsWith("stale frozen entry") && p.endsWith("g-small-table::fixture"))
            .anyMatch(p -> p.startsWith("frozen entry names no changeset") && p.endsWith("no-such-id::fixture"));
    }

    @Test
    void aFrozenEntryInTheFreezePointIncludeOrAfterIt_isRefused() throws Exception {
        List<Entry> entries = DataMoveChangesetRules.evaluate(DataMoveChangesetRules.read(scenarioFiles("rule3-red")));
        List<String> includes = List.of("01-register.xml", "02-destructive.xml");
        Frozen late = new Frozen(Set.of("02-destructive.xml::r3-drop-column::fixture"), "01-register.xml");
        assertThat(DataMoveChangesetRules.frozenProblems(late, includes, entries))
            .singleElement().asString().contains("in or after the freeze-point").contains("r3-drop-column");
        Frozen inThePoint = new Frozen(Set.of("02-destructive.xml::r3-drop-column::fixture"), "02-destructive.xml");
        assertThat(DataMoveChangesetRules.frozenProblems(inThePoint, includes, entries))
            .singleElement().asString().contains("in or after the freeze-point");
        Frozen before = new Frozen(Set.of("01-register.xml::reg-gadgets-and-drops-its-own-table::fixture"), "02-destructive.xml");
        assertThat(DataMoveChangesetRules.frozenProblems(before, includes, entries)).isEmpty();
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
        for (Entry e : offenders) out.put(e.changeset().key(), String.join(",", new TreeSet<>(e.unliftedKinds())));
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
