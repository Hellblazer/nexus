// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.function.Function;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * RDR-229 Technical Design item 4, engine bullet (bead nexus-p2m1x.3): a census gate over the LIVE bodies of the
 * nexus schema's plpgsql functions, pinning the expressions that parse a collection NAME (the content type, the
 * embedding model or the lifecycle state read back out of the name) so the RDR's later phases have a number to drive
 * down and a new parse site cannot appear unseen. {@link dev.nexus.service.vectors.CollectionParseGateTest} cannot see
 * SQL at all and {@link PlpgsqlCheckGateTest} checks bodies for plpgsql errors, not for what they parse.
 *
 * <p>A static scan, no database, no Docker. It follows the research census (T2 {@code nexus_rdr/229-research-23}):
 * <ol>
 *   <li>walk {@code db.changelog-master.xml} in include order;</li>
 *   <li>in each changelog read only the {@code <sql>} blocks that run forward: XML comments, {@code <rollback>}
 *       blocks, SQL comments are blanked first (a rollback block holds the OLD body of a function, which is not
 *       live);</li>
 *   <li>resolve every {@code CREATE [OR REPLACE] FUNCTION|PROCEDURE} to its LAST definer, a later definition
 *       replacing an earlier one and a {@code DROP FUNCTION|PROCEDURE} removing it;</li>
 *   <li>count in each live body the three name-parsing shapes: {@code split_part(... '__' ...)}, {@code
 *       string_to_array(... '__' ...)} and a {@code 'quarantine-...'} literal.</li>
 * </ol>
 * The pin is per function and shape, checked in both directions like {@code CollectionParseGateTest.checkCensus}: more
 * than the pin is a REGRESSION, fewer is a STALE FINGERPRINT (a site removed without lowering the pin).
 *
 * <p><b>Non-vacuity</b> (RDR-229 Failure Modes: "an empty scan fails rather than passing"). The scan must see a
 * non-zero number of includes, {@code <sql>} blocks and live functions, and must resolve the three functions this RDR
 * rewrites to their expected last definer. A scan that found nothing, or misresolved one of the three, fails.
 *
 * <p><b>What it does not see.</b> A function created by a changeset's {@code EXECUTE format(...)} with a computed name
 * is seen only when the {@code CREATE FUNCTION} text is a literal in the block (vectors-031's {@code
 * text_gate_probe_*} are), not when the name is assembled at run time; a function created in a database before the
 * changelog is invisible; a {@code DROP FUNCTION} is matched by name, not by argument list (an overload dropped by
 * signature drops the name). The live {@code pg_proc} is the authority; {@link PlpgsqlCheckGateTest} walks it.
 */
class CollectionNameSqlLiveBodyGateTest {

    private static final Path CHANGELOG_DIR = Path.of("src", "main", "resources", "db", "changelog");
    private static final String MASTER = "db.changelog-master.xml";

    static final String SPLIT_PART = "split_part";
    static final String STRING_TO_ARRAY = "string_to_array";
    static final String QUARANTINE_LITERAL = "quarantine-literal";

    /**
     * The measured census: {@code function|shape} to expression count, at develop {@code 1d341b77f} (RDR-229 P1.3,
     * the first live run). Equal to research-23's 11 expressions in 3 functions. Reduce-only: a phase that removes a
     * site lowers its entry in the same edit, and an entry that reaches zero is removed.
     */
    static final Map<String, Integer> NAME_PARSING_CENSUS = Map.of(
        "nexus.chash_conformance_report|" + SPLIT_PART, 9,
        "nexus.quarantine_restore_chunks|" + QUARANTINE_LITERAL, 1,
        "nexus.reaper_expire_client_quarantine|" + QUARANTINE_LITERAL, 1);

    /** Pinned total; lowered with {@link #NAME_PARSING_CENSUS}. */
    static final int NAME_PARSING_TOTAL_CEILING = 11;

    /** The functions the RDR rewrites, each with the changelog that last defines it: the scan must resolve all three. */
    static final Map<String, String> REQUIRED_LIVE = Map.of(
        "nexus.chash_conformance_report", "vectors-005-repoint-functions-views.xml",
        "nexus.quarantine_restore_chunks", "vectors-053-quarantine-restore-chunk-keys-only.xml",
        "nexus.reaper_expire_client_quarantine", "vectors-056-gc-quarantine-expires-at.xml");

    // ── model ───────────────────────────────────────────────────────────

    /** One live function: its schema-qualified lower-case name, the changelog that last defines it and its text. */
    record Live(String name, String file, String text) {}

    /** What the walk saw, so a vacuous pass is visible in the numbers. */
    static final class Resolution {
        final Map<String, Live> live = new LinkedHashMap<>();
        int includes;
        int sqlBlocks;
        int creates;
        int drops;
    }

    // ── changelog reading ───────────────────────────────────────────────

    private static final Pattern XML_COMMENT = Pattern.compile("<!--.*?-->", Pattern.DOTALL);
    private static final Pattern ROLLBACK = Pattern.compile("<rollback\\s*/>|<rollback>.*?</rollback>", Pattern.DOTALL);
    private static final Pattern SQL_BLOCK = Pattern.compile("<sql[^>]*>(.*?)</sql>", Pattern.DOTALL);
    private static final Pattern INCLUDE = Pattern.compile("<include\\s+file=\"db/changelog/([^\"]+)\"");
    private static final Pattern CREATE_UNIT = Pattern.compile(
        "(?i)\\bCREATE\\s+(?:OR\\s+REPLACE\\s+)?(?:FUNCTION|PROCEDURE)\\s+([^\\s(]+)");
    private static final Pattern DROP_UNIT = Pattern.compile(
        "(?i)\\bDROP\\s+(?:FUNCTION|PROCEDURE)\\s+(?:IF\\s+EXISTS\\s+)?([^\\s(;,]+)");
    private static final Pattern DOLLAR_TAG = Pattern.compile("\\$[A-Za-z_]*\\$");

    static List<String> includes(String masterXml) {
        Matcher m = INCLUDE.matcher(XML_COMMENT.matcher(masterXml).replaceAll(""));
        List<String> out = new ArrayList<>();
        while (m.find()) out.add(m.group(1));
        return out;
    }

    /** The forward-running {@code <sql>} bodies of one changelog: comments and rollback blocks removed first. */
    static List<String> forwardSqlBlocks(String changelogXml) {
        String xml = ROLLBACK.matcher(XML_COMMENT.matcher(changelogXml).replaceAll("")).replaceAll("");
        Matcher m = SQL_BLOCK.matcher(xml);
        List<String> out = new ArrayList<>();
        while (m.find()) out.add(m.group(1));
        return out;
    }

    private static boolean identChar(char c) {
        return Character.isLetterOrDigit(c) || c == '_' || c == '$';
    }

    /**
     * Blanks SQL {@code --} and block comments to spaces, leaving single-quoted literals intact (a {@code '--'} inside
     * a string must not eat the code after it). An {@code E'...'} string honours backslash escapes.
     */
    static String blankSqlComments(String s) {
        StringBuilder out = new StringBuilder(s.length());
        int i = 0;
        int n = s.length();
        while (i < n) {
            char c = s.charAt(i);
            if (c == '\'') {
                boolean escapes = i > 0 && (s.charAt(i - 1) == 'E' || s.charAt(i - 1) == 'e')
                    && (i < 2 || !identChar(s.charAt(i - 2)));
                int j = i + 1;
                while (j < n && s.charAt(j) != '\'') j += (escapes && s.charAt(j) == '\\') ? 2 : 1;
                int end = Math.min(j + 1, n);
                out.append(s, i, end);
                i = end;
            } else if (c == '-' && i + 1 < n && s.charAt(i + 1) == '-') {
                while (i < n && s.charAt(i) != '\n') { out.append(' '); i++; }
            } else if (c == '/' && i + 1 < n && s.charAt(i + 1) == '*') {
                int end = s.indexOf("*/", i + 2);
                end = end < 0 ? n : end + 2;
                for (int j = i; j < end; j++) out.append(s.charAt(j) == '\n' ? '\n' : ' ');
                i = end;
            } else {
                out.append(c);
                i++;
            }
        }
        return out.toString();
    }

    private static String normName(String raw) {
        return raw.replace("\"", "").toLowerCase();
    }

    /**
     * The end of the unit that starts at {@code from} (just past its name): the matching close of the dollar-quoted
     * body when one opens before the statement's first {@code ;}, else that {@code ;}, else the end of the block.
     */
    private static int unitEnd(String sql, int from) {
        int semi = sql.indexOf(';', from);
        int stop = semi < 0 ? sql.length() : semi;
        Matcher d = DOLLAR_TAG.matcher(sql);
        if (d.find(from) && d.start() < stop) {
            int close = sql.indexOf(d.group(), d.end());
            if (close >= 0) return close + d.group().length();
            return sql.length();
        }
        return stop;
    }

    private record Event(int pos, boolean create, String name, int end) {}

    private static void applyBlock(String file, String sqlRaw, Resolution r) {
        String sql = blankSqlComments(sqlRaw);
        List<Event> events = new ArrayList<>();
        Matcher c = CREATE_UNIT.matcher(sql);
        while (c.find()) events.add(new Event(c.start(), true, normName(c.group(1)), unitEnd(sql, c.end())));
        Matcher d = DROP_UNIT.matcher(sql);
        while (d.find()) events.add(new Event(d.start(), false, normName(d.group(1)), d.end()));
        events.sort(Comparator.comparingInt(Event::pos));
        int skipUntil = 0;
        for (Event e : events) {
            if (e.pos() < skipUntil) continue;  // inside an earlier unit's body: text, not a statement
            if (e.create()) {
                r.creates++;
                r.live.put(e.name(), new Live(e.name(), file, sql.substring(e.pos(), e.end())));
                skipUntil = e.end();
            } else {
                r.drops++;
                r.live.remove(e.name());
            }
        }
    }

    /** Resolves every function to its last definer over {@code includes} in order, reading each file with {@code read}. */
    static Resolution resolveLive(List<String> includes, Function<String, String> read) {
        Resolution r = new Resolution();
        r.includes = includes.size();
        for (String file : includes) {
            for (String sql : forwardSqlBlocks(read.apply(file))) {
                r.sqlBlocks++;
                applyBlock(file, sql, r);
            }
        }
        return r;
    }

    // ── the census ──────────────────────────────────────────────────────

    private static final Pattern SPLIT_PART_CALL = Pattern.compile("(?i)\\bsplit_part\\s*\\(");
    private static final Pattern STRING_TO_ARRAY_CALL = Pattern.compile("(?i)\\bstring_to_array\\s*\\(");
    private static final Pattern DUNDER_LITERAL = Pattern.compile("'__'");
    private static final Pattern QUARANTINE_LIT = Pattern.compile("'quarantine-[^']*'");

    /** The text between the parenthesis at {@code open} and its match, quote-aware; the rest of the text when unbalanced. */
    private static String balanced(String s, int open) {
        int depth = 0;
        boolean quoted = false;
        for (int i = open; i < s.length(); i++) {
            char ch = s.charAt(i);
            if (ch == '\'') quoted = !quoted;
            else if (!quoted && ch == '(') depth++;
            else if (!quoted && ch == ')' && --depth == 0) return s.substring(open + 1, i);
        }
        return s.substring(open + 1);
    }

    private static int callsWithDunder(String body, Pattern call) {
        int n = 0;
        Matcher m = call.matcher(body);
        while (m.find()) {
            if (DUNDER_LITERAL.matcher(balanced(body, m.end() - 1)).find()) n++;
        }
        return n;
    }

    /** Name-parsing expressions in one body, by shape (a shape that is absent has no entry). Comments are already blank. */
    static Map<String, Integer> expressions(String body) {
        Map<String, Integer> out = new TreeMap<>();
        int split = callsWithDunder(body, SPLIT_PART_CALL);
        int toArray = callsWithDunder(body, STRING_TO_ARRAY_CALL);
        int q = 0;
        Matcher m = QUARANTINE_LIT.matcher(body);
        while (m.find()) q++;
        if (split > 0) out.put(SPLIT_PART, split);
        if (toArray > 0) out.put(STRING_TO_ARRAY, toArray);
        if (q > 0) out.put(QUARANTINE_LITERAL, q);
        return out;
    }

    /** {@code function|shape} to count over every live function; functions and shapes with none are absent. */
    static Map<String, Integer> census(Map<String, Live> live) {
        Map<String, Integer> out = new TreeMap<>();
        for (Live f : live.values()) {
            expressions(f.text()).forEach((shape, n) -> out.put(f.name() + "|" + shape, n));
        }
        return out;
    }

    /** Both directions, as {@code CollectionParseGateTest.checkCensus}: more than the pin regresses, fewer is stale. */
    static List<String> checkCensus(Map<String, Integer> actual, Map<String, Integer> declared) {
        List<String> violations = new ArrayList<>();
        for (var e : actual.entrySet()) {
            int decl = declared.getOrDefault(e.getKey(), 0);
            if (e.getValue() > decl) {
                violations.add(e.getKey() + ": REGRESSION -- " + e.getValue() + " name-parsing expression(s) in the live body, "
                    + "census declares " + decl + " (a new parse site, or a function the census has never seen)");
            }
        }
        for (var e : declared.entrySet()) {
            int found = actual.getOrDefault(e.getKey(), 0);
            if (found < e.getValue()) {
                violations.add(e.getKey() + ": STALE FINGERPRINT -- declares " + e.getValue() + ", only " + found
                    + " found -- lower this entry (and the total ceiling), or remove it if it reached zero");
            }
        }
        return violations;
    }

    /**
     * The non-vacuity assertions as a callable: includes, sql blocks and live functions all non-zero, every live name
     * schema-qualified (an unqualified name cannot be matched to its DROP), and each required function live at its
     * expected last definer. Throws {@link AssertionError} with the reason.
     */
    static void assertNonVacuous(Resolution r, Map<String, String> required) {
        assertThat(r.includes).as("the walk saw includes (zero means the master was not read)").isGreaterThan(0);
        assertThat(r.sqlBlocks).as("the walk saw forward <sql> blocks").isGreaterThan(0);
        assertThat(r.creates).as("the walk saw CREATE FUNCTION|PROCEDURE statements").isGreaterThan(0);
        assertThat(r.live).as("the walk resolved live functions").isNotEmpty();
        assertThat(r.live.keySet()).as("every live function name is schema-qualified, so a DROP can be matched to it")
            .allMatch(n -> n.contains("."));
        for (var e : required.entrySet()) {
            Live f = r.live.get(e.getKey());
            assertThat(f).as("the scan must find " + e.getKey() + " live (an empty scan or a dropped function fails here)").isNotNull();
            assertThat(f.file()).as("last definer of " + e.getKey()).isEqualTo(e.getValue());
        }
    }

    // ── the real-tree gate ──────────────────────────────────────────────

    private static Resolution resolveRealTree() throws IOException {
        List<String> includes = includes(Files.readString(CHANGELOG_DIR.resolve(MASTER)));
        return resolveLive(includes, file -> {
            try {
                return Files.readString(CHANGELOG_DIR.resolve(file));
            } catch (IOException e) {
                throw new IllegalStateException("an include of the master that does not exist: " + file, e);
            }
        });
    }

    @Test
    void liveBodyNameParsingCensus_pinnedAtMeasuredCount() throws IOException {
        Resolution r = resolveRealTree();
        assertNonVacuous(r, REQUIRED_LIVE);
        Map<String, Integer> actual = census(r.live);
        int total = actual.values().stream().mapToInt(Integer::intValue).sum();
        System.out.println("collection-name SQL live-body gate: includes=" + r.includes + " sqlBlocks=" + r.sqlBlocks
            + " creates=" + r.creates + " drops=" + r.drops + " liveFunctions=" + r.live.size()
            + " expressions=" + total + " functionsWithExpressions="
            + actual.keySet().stream().map(k -> k.substring(0, k.indexOf('|'))).distinct().count()
            + " census=" + actual);

        assertThat(checkCensus(actual, NAME_PARSING_CENSUS))
            .as("RDR-229 item 4: live plpgsql bodies that parse a collection name. REGRESSION = a new site; "
                + "STALE FINGERPRINT = a site removed without lowering the pin")
            .isEmpty();
        assertThat(total).as("NAME_PARSING_TOTAL_CEILING is lowered in the same edit as any census reduction")
            .isLessThanOrEqualTo(NAME_PARSING_TOTAL_CEILING);
    }

    // ── falsification: the detector must actually detect ────────────────

    private static String changelog(String sql, String rollbackSql) {
        return "<databaseChangeLog><changeSet id=\"x\" author=\"y\">"
            + "<sql splitStatements=\"false\"><![CDATA[" + sql + "]]></sql>"
            + (rollbackSql == null ? "<rollback/>"
                : "<rollback><sql><![CDATA[" + rollbackSql + "]]></sql></rollback>")
            + "</changeSet></databaseChangeLog>";
    }

    private static String fn(String name, String expr) {
        return "CREATE OR REPLACE FUNCTION " + name + "(p text) RETURNS text LANGUAGE plpgsql AS $$\n"
            + "BEGIN RETURN " + expr + "; END $$;";
    }

    private static Resolution resolve(Map<String, String> files) {
        return resolveLive(new ArrayList<>(files.keySet()), files::get);
    }

    @Test
    void aLaterDefinitionReplacesTheEarlierBody_inBothDirections() {
        var oldHasParse = new java.util.LinkedHashMap<String, String>();
        oldHasParse.put("a.xml", changelog(fn("nexus.f", "split_part(p, '__', 3)"), null));
        oldHasParse.put("b.xml", changelog(fn("nexus.f", "p"), null));
        Resolution r = resolve(oldHasParse);
        assertThat(r.live.get("nexus.f").file()).isEqualTo("b.xml");
        assertThat(census(r.live)).as("the old body is not live").isEmpty();

        var newHasParse = new java.util.LinkedHashMap<String, String>();
        newHasParse.put("a.xml", changelog(fn("nexus.f", "p"), null));
        newHasParse.put("b.xml", changelog(fn("nexus.f", "split_part(p, '__', 3)"), null));
        assertThat(census(resolve(newHasParse).live)).containsExactly(Map.entry("nexus.f|" + SPLIT_PART, 1));
    }

    @Test
    void aBodyInsideARollbackBlockIsNotLive() {
        var files = new java.util.LinkedHashMap<String, String>();
        files.put("a.xml", changelog(fn("nexus.f", "p"), fn("nexus.f", "split_part(p, '__', 3)")));
        files.put("b.xml", changelog(fn("nexus.g", "p"), null));  // <rollback/> self-closing
        Resolution r = resolve(files);
        assertThat(census(r.live)).as("the rollback copy of a body is the OLD body, never counted").isEmpty();
        assertThat(r.live.get("nexus.f").text()).doesNotContain("split_part");
    }

    @Test
    void aDroppedFunctionIsNotLive_andRecreatingItBringsItBack() {
        var files = new java.util.LinkedHashMap<String, String>();
        files.put("a.xml", changelog(fn("nexus.f", "split_part(p, '__', 3)"), null));
        files.put("b.xml", changelog("DROP FUNCTION IF EXISTS nexus.f(text);", null));
        Resolution dropped = resolve(files);
        assertThat(dropped.live).doesNotContainKey("nexus.f");
        assertThat(census(dropped.live)).isEmpty();

        files.put("c.xml", changelog(fn("nexus.f", "string_to_array(p, '__')"), null));
        assertThat(census(resolve(files).live)).containsExactly(Map.entry("nexus.f|" + STRING_TO_ARRAY, 1));
    }

    @Test
    void theThreeShapesCount_andLookalikesDoNot() {
        String body = fn("nexus.f",
            "split_part(p, '__', 3) || split_part(lower(p), '__', 1) || split_part(p, ',', 1)"
            + " || (string_to_array(p, '__'))[1] || string_to_array(p, ',')::text"
            + " || CASE WHEN starts_with(p, 'quarantine-') THEN 'quarantine-%' ELSE 'quarantine' END");
        Map<String, Integer> found = expressions(blankSqlComments(body));
        assertThat(found).containsEntry(SPLIT_PART, 2).containsEntry(STRING_TO_ARRAY, 1).containsEntry(QUARANTINE_LITERAL, 2);
    }

    @Test
    void commentedOutExpressionsAreNotCounted_andADashDashInAStringDoesNotEatTheCode() {
        var files = new java.util.LinkedHashMap<String, String>();
        files.put("a.xml",
            "<databaseChangeLog><!-- <sql>CREATE FUNCTION nexus.ghost() split_part(x,'__',1)</sql> -->"
            + "<changeSet id=\"x\"><sql><![CDATA["
            + "-- split_part(p, '__', 3) in a line comment\n"
            + "/* string_to_array(p, '__') in a block comment */\n"
            + "CREATE OR REPLACE FUNCTION nexus.f(p text) RETURNS text LANGUAGE plpgsql AS $$\n"
            + "BEGIN -- 'quarantine-' in a trailing comment\n"
            + "RETURN '--' || split_part(p, '__', 2); END $$;"
            + "]]></sql></changeSet></databaseChangeLog>");
        Resolution r = resolve(files);
        assertThat(r.live).doesNotContainKey("nexus.ghost");
        assertThat(census(r.live)).containsExactly(Map.entry("nexus.f|" + SPLIT_PART, 1));
    }

    @Test
    void aFunctionBodyEndsAtItsDollarQuote_soTheNextStatementIsNotItsText() {
        var files = new java.util.LinkedHashMap<String, String>();
        files.put("a.xml", changelog(fn("nexus.f", "p")
            + "\nCREATE TABLE nexus.t AS SELECT split_part(name, '__', 3) FROM nexus.catalog_collections;", null));
        assertThat(census(resolve(files).live)).as("a statement after the function is not part of its body").isEmpty();
    }

    @Test
    void aNewExpressionBeyondThePin_failsAsRegression() {
        Map<String, Integer> actual = Map.of("nexus.f|" + SPLIT_PART, 3);
        Map<String, Integer> declared = Map.of("nexus.f|" + SPLIT_PART, 2);
        assertThat(checkCensus(actual, declared)).anyMatch(v -> v.contains("REGRESSION"));
        assertThat(checkCensus(Map.of("nexus.new_fn|" + QUARANTINE_LITERAL, 1), Map.of()))
            .as("a function the census has never seen").anyMatch(v -> v.contains("REGRESSION"));
    }

    @Test
    void anExpressionRemovedWithoutLoweringThePin_failsAsStaleFingerprint() {
        Map<String, Integer> actual = Map.of("nexus.f|" + SPLIT_PART, 1);
        Map<String, Integer> declared = Map.of("nexus.f|" + SPLIT_PART, 2);
        assertThat(checkCensus(actual, declared)).anyMatch(v -> v.contains("STALE FINGERPRINT"));
        assertThat(checkCensus(Map.of(), declared)).as("the function itself is gone").anyMatch(v -> v.contains("STALE FINGERPRINT"));
    }

    @Test
    void anEmptyScanFails_andSoDoesAMisresolvedLastDefiner() {
        assertThatThrownBy(() -> assertNonVacuous(resolve(Map.of()), Map.of()))
            .as("no includes, no blocks, no functions").isInstanceOf(AssertionError.class);

        var empty = new java.util.LinkedHashMap<String, String>();
        empty.put("a.xml", "<databaseChangeLog/>");
        assertThatThrownBy(() -> assertNonVacuous(resolve(empty), Map.of()))
            .as("an include with no forward <sql> is a scan that found nothing").isInstanceOf(AssertionError.class);

        var files = new java.util.LinkedHashMap<String, String>();
        files.put("a.xml", changelog(fn("nexus.f", "p"), null));
        files.put("b.xml", changelog(fn("nexus.f", "p"), null));
        assertNonVacuous(resolve(files), Map.of("nexus.f", "b.xml"));
        assertThatThrownBy(() -> assertNonVacuous(resolve(files), Map.of("nexus.f", "a.xml")))
            .as("the last definer is b.xml, so a pin naming a.xml is a misresolution").isInstanceOf(AssertionError.class);
        assertThatThrownBy(() -> assertNonVacuous(resolve(files), Map.of("nexus.missing", "a.xml")))
            .as("a required function that is not live fails").isInstanceOf(AssertionError.class);
    }
}
