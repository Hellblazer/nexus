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
 * <p><b>Identity of a function.</b> Name plus a normalised input-type list, as PostgreSQL identifies it: parameter
 * names, modes ({@code OUT} arguments are not part of the identity), {@code DEFAULT}s and length modifiers are
 * dropped and common aliases are folded ({@code int}, {@code float8}, {@code varchar}...). A {@code CREATE OR REPLACE}
 * with the same types replaces the function; one with other types is an overload and leaves the first live (in-tree:
 * {@code nexus.search_aspect_scoped_*} had 11 arguments in vectors-049 and 12 in vectors-055). {@code DROP FUNCTION}
 * names its argument list and may name several functions; without a list it drops every overload of the name. The
 * census pin sums the overloads of one name.
 *
 * <p><b>What is counted.</b> Every single-quoted or inner dollar-quoted string literal in a live body that contains
 * {@code __} (the separator, or a {@code code__}/{@code docs__}/{@code rdr__}/{@code knowledge__} prefix) or its
 * LIKE-escaped spelling {@code \_\_}, plus the assembled spellings {@code '_' || '_'} and {@code repeat('_'},
 * under the shape {@code dunder-literal}; and every literal starting with {@code quarantine-} (a {@code LIKE 'quarantine-%'} pattern too), shape {@code
 * quarantine-literal}. Counting the literal, not the call, covers {@code split_part}, {@code string_to_array},
 * {@code string_to_table}, {@code regexp_split_to_array}, {@code strpos}, {@code position}, {@code LIKE} and a
 * separator held in a variable alike. (The first cut counted only {@code split_part}/{@code string_to_array} calls;
 * the pin is unchanged because the tree holds no other spelling.)
 *
 * <p><b>The reader fails loud on what it cannot read.</b> An {@code <include>} the reader cannot place, an
 * {@code <include>} tag count that differs from the matched count, a changelog in the directory the master does not
 * include, and a forward {@code <sqlFile>}, {@code <createProcedure>}, {@code <customChange>} or {@code
 * <executeCommand>} all fail the scan; floors on includes and live functions fail a scan that found too little.
 *
 * <p><b>Known limits</b> (the live {@code pg_proc} is the authority; {@link PlpgsqlCheckGateTest} walks it):
 * <ul>
 *   <li>a function whose body is not dollar-quoted ends at the statement's first {@code ;} ({@code BEGIN ATOMIC},
 *       {@code AS '...;...'}), and a single-quoted body counts as one literal;</li>
 *   <li>comment blanking is dollar-quote-unaware: a {@code --} inside a {@code $q$...$q$} string blanks the rest of
 *       its line;</li>
 *   <li>{@code ALTER FUNCTION ... RENAME} is not followed;</li>
 *   <li>changesets are treated as always run: {@code MARK_RAN} preconditions, {@code context}, {@code labels} and
 *       {@code dbms} filters are not evaluated (vectors-029-1 is one such), and a {@code DO} block's CREATE and DROP
 *       count unconditionally;</li>
 *   <li>names assembled at run time ({@code EXECUTE format('CREATE FUNCTION ' || ...)}) are invisible; a literal
 *       {@code EXECUTE $f$CREATE OR REPLACE FUNCTION ...$f$} is seen (vectors-031's {@code text_gate_probe_*});</li>
 *   <li>one-shot DML backfills, views, constraints and generated columns that parse a name are out of scope: this
 *       gate covers function bodies only;</li>
 *   <li>{@code E'\\_\\_'} (a backslash-escaped LIKE written inside an E-string) is not counted: the literal reads {@code
 *       \_\_} only after the string is unescaped;</li>
 *   <li>an unqualified name in a {@code DROP FUNCTION} does not fold to {@code nexus.}; and an unqualified {@code CREATE}
 *       fails the non-vacuity guard instead;</li>
 *   <li>{@code SQL_BLOCK} also matches {@code <sqlCheck>} (it begins {@code <sql}), so {@code sqlBlocks} in the run line
 *       is not a true count of {@code <sql>} blocks; it is only compared to zero;</li>
 *   <li>the {@code QuarantineOrigin} helpers that reach the prefix only through {@code isQuarantineName} or {@code PREFIX}
 *       inside that class ({@code holdsRowsOf}, {@code countRowsOf}, {@code deleteRowsOf}, {@code retagRowsOf}, {@code
 *       isLiveRegistered}, {@code deleteQuarantineCollectionRows}) are not callers the Java census counts: the
 *       reads inside them are counted once, in {@code QuarantineOrigin} itself;</li>
 *   <li>the type normaliser knows the aliases and multi-word forms in {@link #TYPE_ALIASES} and {@link #KNOWN_MULTIWORD}
 *       and fails loud on any other whitespace in a type ({@code interval day to second}, a domain with spaces);</li>
 *   <li>a quoted parameter name is dropped but a quoted TYPE keeps no case.</li>
 * </ul>
 */
class CollectionNameSqlLiveBodyGateTest {

    private static final Path CHANGELOG_DIR = Path.of("src", "main", "resources", "db", "changelog");
    static final String MASTER = "db.changelog-master.xml";

    static final String DUNDER_LITERAL = "dunder-literal";
    static final String QUARANTINE_LITERAL = "quarantine-literal";
    static final String URI_CLASSIFIER_LITERAL = "uri-classifier-literal";

    /** The frozen file-routed prefixes, as the engine's {@code NoteUri.FILE_ROUTED_PREFIXES} lists them. */
    static final List<String> FROZEN_PREFIXES = List.of("rdr__", "docs__", "code__");

    /**
     * The measured census: {@code function|shape} to expression count (overloads of one name summed), at develop
     * {@code 1d341b77f} (RDR-229 P1.3, the first live run). Equal to research-23's 11 expressions in 3 functions.
     * Reduce-only: a phase that removes a site lowers its entry in the same edit, and an entry that reaches zero is
     * removed. The first cut keyed the nine {@code chash_conformance_report} sites as {@code split_part}; review round 1
     * made the shape the literal, so the same nine sites are {@code dunder-literal}. RDR-229 P2.6 (vectors-063,
     * nexus-p2m1x.11) removed all eleven: the three functions read the collection's catalog row, so every entry
     * reached zero and was removed.
     */
    static final Map<String, Integer> NAME_PARSING_CENSUS = Map.of();

    /**
     * The URI-string classifier's literals, pinned apart from the name-parsing census (RDR-229 P2.10, nexus-p2m1x.15, as
     * the Python parse census keeps a {@code uri-classifier} class beside its routing class). A literal that is exactly one
     * of the frozen file-routed prefixes ({@code rdr__}, {@code docs__}, {@code code__}) is the classifier of
     * {@code NoteUri.isNote} on a URI segment, which RDR-229 item 12 keeps on its frozen list; it reads no content type out
     * of a collection name, so it is not a name parse and must not hold the name-parsing total above zero when P2.6 empties
     * it. {@code catalog-049-1} redefines {@code nexus.catalog_reconcile_commit} with {@code NoteUri.moveTo}'s rule, which
     * tests the old collection's segment against those three prefixes. They stay while the frozen list does.
     */
    static final Map<String, Integer> URI_CLASSIFIER_CENSUS = Map.of(
        "nexus.catalog_reconcile_commit|" + URI_CLASSIFIER_LITERAL, 3);

    /** Pinned total; lowered with {@link #NAME_PARSING_CENSUS} (a test asserts the two agree). 11 -> 0 at P2.6. */
    static final int NAME_PARSING_TOTAL_CEILING = 0;

    /** The functions the RDR rewrites, each with the changelog that last defines it: the scan must resolve all three. */
    static final Map<String, String> REQUIRED_LIVE = Map.of(
        // RDR-229 P3.1 stage 2b: vectors-069 redefined the last two once more to set collection_id; RDR-229 P3.5b (vectors-077,
        // nexus-p2m1x.24) redefines all three so their matches read the id and the name (a change of the matches only: the census
        // below is unchanged because no name-parsing expression moved). RDR-229 P3.7 (vectors-083, nexus-p2m1x.27) redefines
        // quarantine_restore_chunks once more, so its quarantine origin is matched by id from gate 3.
        "nexus.chash_conformance_report", "vectors-077-rdr229-p35b-readers-both.xml",
        "nexus.quarantine_restore_chunks", "vectors-083-restore-by-origin-id.xml",
        "nexus.reaper_expire_client_quarantine", "vectors-077-rdr229-p35b-readers-both.xml");

    /** Floors on the real tree (234 includes and 120 live functions: 233 at the first run, 234 at develop 118701321; catalog-051-1a added nexus.collection_tuple_lock_key, 119 to 120; vectors-071-1 added nexus.rdr229_restamp, 120 to 121; RDR-229 P3.8's collections-001-1 and collections-002 added nexus.catalog_collection_tuple_duplicates_unscoped, nexus.collection_id_seq_heal and nexus.collection_id_taken, 121 to 124); a scan below them read too little. */
    static final int INCLUDES_FLOOR = 234;
    static final int LIVE_FUNCTIONS_FLOOR = 100;

    /**
     * The live functions the real tree resolves to (name plus signature), pinned exactly, with no live overload today: the
     * key count equals the name count. A resolver that stops folding a type spelling or a DEFAULT reads more
     * here (stale signatures that were really dropped), so this pin is what makes the normaliser load-bearing. A function
     * added or dropped moves it, in the same edit. 120 -> 121 at RDR-229 P3.1 fix round A: vectors-071-1 adds
     * {@code nexus.rdr229_restamp(text)}. It is a live function but not a name-parsing site: it matches move LABELS with
     * {@code starts_with(m.label, ...)} and compares collection names only by equality against {@code catalog_collections.name}, so
     * {@link #NAME_PARSING_CENSUS} is unchanged. 124 -> 127 at RDR-229 P3.7: vectors-079-1 adds the three child-row name trigger functions
     * ({@code nexus.catalog_document_chunks_name_from_chunk}, {@code nexus.chunk_orphaned_at_name_from_chunk},
     * {@code nexus.topic_assignments_name_from_chunk}). They read the chunk's STORED name column by
     * equality and parse no name (the log line is a RAISE LOG of the names, no split_part), so the census is unchanged.
     */
    // 124 -> 162 (+3 -> 165, P3.7 vectors-079: three child-row name triggers): nexus-p2m1x.24 (RDR-229 P3.5b), vectors-077 adds 38 id twins (<name>_by_id: 37 search functions, no
    // hybrid_search twin, and chunk_live_owners_by_id); the name functions they twin stay live until P4.1.
    static final int LIVE_FUNCTIONS_PIN = 165;

    // ── model ───────────────────────────────────────────────────────────

    /** One live function: lower-case schema-qualified name, normalised input types, last-defining changelog, text. */
    record Live(String name, String signature, String file, String text) {}

    /** What the walk saw, so a vacuous pass is visible in the numbers. */
    static final class Resolution {
        /** Keyed {@code name + signature}. */
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
    private static final Pattern INCLUDE_TAG = Pattern.compile("<include(?:All)?\\b[^>]*>");
    private static final Pattern INCLUDE_FILE = Pattern.compile("^<include\\b[^>]*?\\bfile\\s*=\\s*\"db/changelog/([^\"/]+)\"");
    private static final Pattern UNREADABLE_FORWARD = Pattern.compile("<(sqlFile|createProcedure|customChange|executeCommand)\\b");
    private static final Pattern CREATE_UNIT = Pattern.compile(
        "(?i)\\bCREATE\\s+(?:OR\\s+REPLACE\\s+)?(?:FUNCTION|PROCEDURE)\\s+([^\\s(]+)");
    private static final Pattern DROP_HEAD = Pattern.compile("(?i)\\bDROP\\s+(?:FUNCTION|PROCEDURE)\\s+(?:IF\\s+EXISTS\\s+)?");
    private static final Pattern DOLLAR_TAG = Pattern.compile("\\$[A-Za-z_]*\\$");

    /**
     * The master's includes in order. Every {@code <include>} and {@code <includeAll>} tag either yields a file or fails
     * the scan (a different path, an {@code includeAll}), so the matched count equals the tag count by construction;
     * attributes may come in any order.
     */
    static List<String> includes(String masterXml) {
        String xml = XML_COMMENT.matcher(masterXml).replaceAll("");
        Matcher t = INCLUDE_TAG.matcher(xml);
        List<String> out = new ArrayList<>();
        while (t.find()) {
            Matcher f = INCLUDE_FILE.matcher(t.group());
            if (!f.find()) {
                throw new IllegalStateException("an <include> the changelog reader cannot place as db/changelog/<file>: " + t.group());
            }
            out.add(f.group(1));
        }
        return out;
    }

    /**
     * Every changelog file in the directory but the master must be included, and every include must exist in it: a file
     * nothing includes is never scanned.
     */
    static void assertEveryChangelogIncluded(java.util.Collection<String> dirXmlFiles, List<String> includes) {
        java.util.Set<String> dir = new java.util.TreeSet<>(dirXmlFiles);
        dir.remove(MASTER);
        assertThat(includes).as("the master includes each changelog once").doesNotHaveDuplicates();
        assertThat(dir).as("changelog files in the directory the master does not include (they would never be scanned)")
            .isSubsetOf(includes);
        assertThat(includes).as("includes with no file in the directory").isSubsetOf(dir);
    }

    /**
     * The forward-running {@code <sql>} bodies of one changelog: comments and rollback blocks removed first. Fails on a
     * forward changeset this scan cannot read ({@code sqlFile}, {@code createProcedure}, {@code customChange},
     * {@code executeCommand}): its function bodies would be invisible, and a pass over them would be vacuous.
     */
    static List<String> forwardSqlBlocks(String changelogXml) {
        String xml = ROLLBACK.matcher(XML_COMMENT.matcher(changelogXml).replaceAll("")).replaceAll("");
        Matcher u = UNREADABLE_FORWARD.matcher(xml);
        if (u.find()) {
            throw new IllegalStateException("a forward <" + u.group(1) + "> changeset: this gate reads <sql> blocks only, "
                + "so a function it defines would go unseen");
        }
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
     * a string must not eat the code after it). An {@code E'...'} string honours backslash escapes; a doubled {@code ''}
     * ends and reopens the literal.
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

    // ── function identity: name + normalised input types ────────────────

    /** The text between the parenthesis at {@code open} and its match, quote-aware; the rest when unbalanced. */
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

    /** Top-level comma split: commas inside parentheses, brackets or quotes do not split. */
    private static List<String> topLevel(String args) {
        List<String> out = new ArrayList<>();
        int depth = 0;
        boolean quoted = false;
        StringBuilder cur = new StringBuilder();
        for (char ch : args.toCharArray()) {
            if (ch == '\'') quoted = !quoted;
            if (!quoted && (ch == '(' || ch == '[')) depth++;
            if (!quoted && (ch == ')' || ch == ']')) depth--;
            if (!quoted && depth == 0 && ch == ',') {
                out.add(cur.toString().trim());
                cur.setLength(0);
            } else {
                cur.append(ch);
            }
        }
        if (!cur.toString().isBlank()) out.add(cur.toString().trim());
        return out;
    }

    private static final java.util.Set<String> MODES = java.util.Set.of("in", "out", "inout", "variadic");
    /** First words of multi-word type names: an argument that starts with one is an unnamed type. */
    private static final java.util.Set<String> MULTIWORD_TYPE_START =
        java.util.Set.of("double", "character", "char", "timestamp", "time", "bit", "national", "interval");
    /** The multi-word type names left after folding; any other whitespace in a normalised type fails loud. */
    private static final java.util.Set<String> KNOWN_MULTIWORD = java.util.Set.of(
        "double precision", "character varying", "bit varying", "timestamp with time zone", "time with time zone");
    private static final Map<String, String> TYPE_ALIASES = Map.ofEntries(
        Map.entry("int", "integer"), Map.entry("int4", "integer"), Map.entry("int8", "bigint"),
        Map.entry("int2", "smallint"), Map.entry("bool", "boolean"), Map.entry("float8", "double precision"),
        Map.entry("float4", "real"), Map.entry("float", "double precision"), Map.entry("varchar", "character varying"),
        Map.entry("char varying", "character varying"), Map.entry("char", "character"), Map.entry("bpchar", "character"),
        Map.entry("varbit", "bit varying"), Map.entry("timestamptz", "timestamp with time zone"),
        Map.entry("timetz", "time with time zone"), Map.entry("timestamp without time zone", "timestamp"),
        Map.entry("time without time zone", "time"), Map.entry("decimal", "numeric"));
    private static final Pattern TYPE_MODIFIER = Pattern.compile("\\(\\s*\\d+(?:\\s*,\\s*\\d+)?\\s*\\)");
    private static final Pattern FLOAT_PRECISION = Pattern.compile("(?i)\\bfloat\\s*\\(\\s*(\\d+)\\s*\\)");
    private static final Pattern DEFAULT_CLAUSE = Pattern.compile("(?i)(?:^|\\s+)default(?:\\s+|$)|\\s*:?=\\s*");
    private static final Pattern QUOTED_NAME_THEN_TYPE = Pattern.compile("^\"(?:[^\"]|\"\")*\"\\s+(.+)$", Pattern.DOTALL);

    /** {@code float(n)} is {@code real} up to 24 bits and {@code double precision} above: folded before modifiers are dropped. */
    private static String foldFloat(String lowerCase) {
        return FLOAT_PRECISION.matcher(lowerCase).replaceAll(m -> Integer.parseInt(m.group(1)) <= 24 ? "real" : "double precision");
    }

    /**
     * One type name, lower-case, whitespace-collapsed, length modifiers dropped, {@code pg_catalog.}, {@code public.} and
     * {@code nexus.} schema qualifiers dropped, array spellings ({@code ARRAY}, {@code [3]}, {@code [][]}) folded to
     * one {@code []}, aliases folded. Fails loud on a type that is empty or still holds whitespace outside {@link
     * #KNOWN_MULTIWORD}: an identity that cannot be reduced is never guessed.
     */
    static String normType(String raw) {
        String t = foldFloat(raw.toLowerCase());
        t = TYPE_MODIFIER.matcher(t).replaceAll("").replaceAll("\\s+", " ").trim();
        // The vector type was created unqualified (vectors-007) and later written nexus.vector (vectors-055): one type.
        t = t.replace("pg_catalog.", "").replace("public.", "").replace("nexus.", "");
        boolean array = t.matches(".*(?:\\s+array\\s*(?:\\[\\s*\\d*\\s*])?|(?:\\[\\s*\\d*\\s*])+)$");
        if (array) t = t.replaceAll("(?:\\s+array\\s*(?:\\[\\s*\\d*\\s*])?|(?:\\[\\s*\\d*\\s*])+)$", "").trim();
        t = TYPE_ALIASES.getOrDefault(t, t);
        if (t.isEmpty()) throw new IllegalStateException("an argument that normalises to an empty type: '" + raw + "'");
        if (t.contains(" ") && !KNOWN_MULTIWORD.contains(t)) {
            throw new IllegalStateException("an argument type the normaliser cannot reduce (whitespace outside the known multi-word types): '"
                + raw + "' -> '" + t + "'");
        }
        return array ? t + "[]" : t;
    }

    /**
     * The identity of an argument list: the input types in order, {@code (t1,t2,...)}. Accepts {@code CREATE}'s
     * {@code [mode] [name] type [DEFAULT ...]} and {@code DROP}'s bare types alike. {@code OUT} arguments are not part
     * of the identity; {@code IN OUT}, {@code INOUT} and {@code VARIADIC} are; a quoted parameter name is dropped. An
     * argument it cannot reduce throws, so a signature never silently collapses to {@code ()}.
     */
    static String signature(String argList) {
        List<String> types = new ArrayList<>();
        for (String arg : topLevel(argList)) {
            Matcher d = DEFAULT_CLAUSE.matcher(arg);
            String a = (d.find() ? arg.substring(0, d.start()) : arg).trim();
            Matcher q = QUOTED_NAME_THEN_TYPE.matcher(a);
            if (q.matches()) a = q.group(1).trim();
            List<String> tokens = new ArrayList<>(List.of(a.split("\\s+")));
            if (tokens.get(0).isEmpty()) throw new IllegalStateException("an empty argument in the list '" + argList + "'");
            String mode = "in";
            if (tokens.size() >= 2 && tokens.get(0).equalsIgnoreCase("in") && tokens.get(1).equalsIgnoreCase("out")) {
                tokens.remove(0);
                tokens.set(0, "inout");
            }
            if (MODES.contains(tokens.get(0).toLowerCase())) mode = tokens.remove(0).toLowerCase();
            if (tokens.isEmpty()) throw new IllegalStateException("an argument with a mode and no type: '" + arg + "'");
            if (mode.equals("out")) continue;
            // Type modifiers go before the name test: "numeric (10, 2)" must not read numeric as the name.
            String rest = TYPE_MODIFIER.matcher(foldFloat(String.join(" ", tokens).toLowerCase())).replaceAll("");
            tokens = new ArrayList<>(List.of(rest.trim().split("\\s+")));
            if (tokens.size() >= 2 && !MULTIWORD_TYPE_START.contains(tokens.get(0).toLowerCase())
                    && !tokens.get(0).contains(".") && !tokens.get(0).contains("(")) {
                tokens.remove(0);  // the parameter name
            }
            types.add(normType(String.join(" ", tokens)));
        }
        return "(" + String.join(",", types) + ")";
    }

    // ── resolving the last definer ──────────────────────────────────────

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

    /** {@code kind}: 0 create, 1 drop of one signature, 2 drop of every overload of the name. */
    private record Event(int pos, int kind, String name, String signature, int end) {}

    /** The argument list that starts at the first non-blank character at or after {@code from}, or null when none opens there. */
    private static String argsAt(String sql, int from) {
        int i = from;
        while (i < sql.length() && Character.isWhitespace(sql.charAt(i))) i++;
        return i < sql.length() && sql.charAt(i) == '(' ? balanced(sql, i) : null;
    }

    private static int argsEnd(String sql, int from) {
        int i = from;
        while (i < sql.length() && Character.isWhitespace(sql.charAt(i))) i++;
        if (i >= sql.length() || sql.charAt(i) != '(') return from;
        String inner = balanced(sql, i);
        return i + inner.length() + 2;
    }

    private static void collectDrops(String sql, List<Event> events) {
        Matcher h = DROP_HEAD.matcher(sql);
        while (h.find()) {
            int i = h.end();
            while (true) {
                while (i < sql.length() && Character.isWhitespace(sql.charAt(i))) i++;
                int nameStart = i;
                while (i < sql.length() && !Character.isWhitespace(sql.charAt(i)) && "(,;".indexOf(sql.charAt(i)) < 0) i++;
                if (i == nameStart) break;
                String name = normName(sql.substring(nameStart, i));
                String args = argsAt(sql, i);
                int after = args == null ? i : argsEnd(sql, i);
                events.add(new Event(h.start(), args == null ? 2 : 1, name, args == null ? "" : signature(args), after));
                i = after;
                while (i < sql.length() && Character.isWhitespace(sql.charAt(i))) i++;
                if (i < sql.length() && sql.charAt(i) == ',') { i++; continue; }
                break;
            }
        }
    }

    private static void applyBlock(String file, String sqlRaw, Resolution r) {
        String sql = blankSqlComments(sqlRaw);
        List<Event> events = new ArrayList<>();
        Matcher c = CREATE_UNIT.matcher(sql);
        while (c.find()) {
            String args = argsAt(sql, c.end());
            events.add(new Event(c.start(), 0, normName(c.group(1)), args == null ? "()" : signature(args), unitEnd(sql, c.end())));
        }
        collectDrops(sql, events);
        events.sort(Comparator.comparingInt(Event::pos));
        int skipUntil = 0;
        for (Event e : events) {
            if (e.pos() < skipUntil) continue;  // inside an earlier unit's body: text, not a statement
            switch (e.kind()) {
                case 0 -> {
                    r.creates++;
                    r.live.put(e.name() + e.signature(), new Live(e.name(), e.signature(), file, sql.substring(e.pos(), e.end())));
                    skipUntil = e.end();
                }
                case 1 -> {
                    r.drops++;
                    r.live.remove(e.name() + e.signature());
                }
                default -> {
                    r.drops++;
                    r.live.values().removeIf(f -> f.name().equals(e.name()));
                }
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

    private static final Pattern ASSEMBLED_SEPARATOR = Pattern.compile("'_'\\s*\\|\\|\\s*'_'|(?i:\\brepeat\\s*\\(\\s*'_')");

    /**
     * Every string literal in a live body: single-quoted (a doubled {@code ''} stays inside; an {@code E''} string
     * honours backslash escapes) and inner dollar-quoted, never the function body's own outer quote. Comments are
     * already blank. A single-quoted body is one literal.
     */
    static List<String> literals(String body) {
        List<String> out = new ArrayList<>();
        String outer = null;
        int i = 0;
        int n = body.length();
        while (i < n) {
            char c = body.charAt(i);
            if (c == '\'') {
                boolean escapes = i > 0 && (body.charAt(i - 1) == 'E' || body.charAt(i - 1) == 'e')
                    && (i < 2 || !identChar(body.charAt(i - 2)));
                int j = i + 1;
                while (j < n) {
                    char d = body.charAt(j);
                    if (escapes && d == '\\') { j += 2; continue; }
                    if (d == '\'') {
                        if (j + 1 < n && body.charAt(j + 1) == '\'') { j += 2; continue; }
                        break;
                    }
                    j++;
                }
                out.add(body.substring(i + 1, Math.min(j, n)));
                i = j + 1;
            } else if (c == '$') {
                Matcher d = DOLLAR_TAG.matcher(body);
                if (d.find(i) && d.start() == i) {
                    String tag = d.group();
                    if (outer == null) outer = tag;
                    if (tag.equals(outer)) { i = d.end(); continue; }  // the body's own quote: transparent
                    int close = body.indexOf(tag, d.end());
                    int end = close < 0 ? n : close;
                    out.add(body.substring(d.end(), end));
                    i = close < 0 ? n : close + tag.length();
                } else {
                    i++;
                }
            } else {
                i++;
            }
        }
        return out;
    }

    /** Name-parsing expressions in one body, by shape (a shape that is absent has no entry). Comments are already blank. */
    private static boolean hasDunder(String lit) {
        return lit.contains("__") || lit.contains("\\_\\_");
    }

    static Map<String, Integer> expressions(String body) {
        int dunder = 0;
        int quarantine = 0;
        int classifier = 0;
        for (String lit : literals(body)) {
            if (hasDunder(lit)) {
                // a literal whose only "__" are the frozen prefixes, bare or quoted inside a dollar-quoted statement body,
                // is the URI-string classifier (counted apart); any other "__" keeps it a name parse
                String rest = FROZEN_PREFIXES.contains(lit) ? "" : lit;
                for (String p : FROZEN_PREFIXES) rest = rest.replace("'" + p + "'", "");
                if (hasDunder(rest)) dunder++; else classifier++;
            }
            if (lit.startsWith("quarantine-")) quarantine++;
        }
        Matcher a = ASSEMBLED_SEPARATOR.matcher(body);
        while (a.find()) dunder++;
        Map<String, Integer> out = new TreeMap<>();
        if (dunder > 0) out.put(DUNDER_LITERAL, dunder);
        if (quarantine > 0) out.put(QUARANTINE_LITERAL, quarantine);
        if (classifier > 0) out.put(URI_CLASSIFIER_LITERAL, classifier);
        return out;
    }

    /** {@code function|shape} to count over every live function (overloads summed); functions and shapes with none are absent. */
    static Map<String, Integer> census(Map<String, Live> live) {
        Map<String, Integer> out = new TreeMap<>();
        for (Live f : live.values()) {
            expressions(f.text()).forEach((shape, n) -> out.merge(f.name() + "|" + shape, n, Integer::sum));
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
     * The non-vacuity assertions as a callable: includes, sql blocks and live functions at or above the floors, every
     * live name schema-qualified (an unqualified name cannot be matched to its DROP), and each required function live at
     * its expected last definer. Throws {@link AssertionError} with the reason.
     */
    static void assertNonVacuous(Resolution r, Map<String, String> required, int minIncludes, int minLive) {
        assertThat(r.includes).as("the walk saw at least " + minIncludes + " includes (fewer means the master was not read)")
            .isGreaterThanOrEqualTo(Math.max(1, minIncludes));
        assertThat(r.sqlBlocks).as("the walk saw forward <sql> blocks").isGreaterThan(0);
        assertThat(r.creates).as("the walk saw CREATE FUNCTION|PROCEDURE statements").isGreaterThan(0);
        assertThat(r.live.size()).as("the walk resolved at least " + minLive + " live functions").isGreaterThanOrEqualTo(Math.max(1, minLive));
        assertThat(r.live.values().stream().map(Live::name)).as("every live function name is schema-qualified, so a DROP can be matched to it")
            .allMatch(n -> n.contains("."));
        for (var e : required.entrySet()) {
            List<Live> overloads = r.live.values().stream().filter(f -> f.name().equals(e.getKey())).toList();
            assertThat(overloads).as("the scan must find " + e.getKey() + " live (an empty scan or a dropped function fails here)")
                .isNotEmpty();
            assertThat(overloads.stream().map(Live::file)).as("last definer of " + e.getKey()).containsOnly(e.getValue());
        }
    }

    // ── the real-tree gate ──────────────────────────────────────────────

    private static Resolution resolveRealTree(List<String> includes) {
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
        List<String> includes = includes(Files.readString(CHANGELOG_DIR.resolve(MASTER)));
        List<String> dirFiles;
        try (var ls = Files.list(CHANGELOG_DIR)) {
            dirFiles = ls.map(p -> p.getFileName().toString()).filter(n -> n.endsWith(".xml")).toList();
        }
        assertEveryChangelogIncluded(dirFiles, includes);
        Resolution r = resolveRealTree(includes);
        assertNonVacuous(r, REQUIRED_LIVE, INCLUDES_FLOOR, LIVE_FUNCTIONS_FLOOR);
        Map<String, Integer> everything = census(r.live);
        Map<String, Integer> actual = new TreeMap<>(everything);
        actual.keySet().removeIf(k -> k.endsWith("|" + URI_CLASSIFIER_LITERAL));
        Map<String, Integer> classifier = new TreeMap<>(everything);
        classifier.keySet().removeIf(k -> !k.endsWith("|" + URI_CLASSIFIER_LITERAL));
        int total = actual.values().stream().mapToInt(Integer::intValue).sum();
        long names = r.live.values().stream().map(Live::name).distinct().count();
        Map<String, List<String>> overloaded = new TreeMap<>();
        r.live.values().forEach(f -> overloaded.computeIfAbsent(f.name(), k -> new ArrayList<>()).add(f.signature() + "@" + f.file()));
        overloaded.values().removeIf(l -> l.size() < 2);
        System.out.println("collection-name SQL live-body gate overloads (" + overloaded.size() + " names): " + overloaded);
        System.out.println("collection-name SQL live-body gate: includes=" + r.includes + " dirChangelogs=" + dirFiles.size()
            + " sqlBlocks=" + r.sqlBlocks + " creates=" + r.creates + " drops=" + r.drops + " liveFunctions=" + r.live.size()
            + " liveNames=" + names + " expressions=" + total + " functionsWithExpressions="
            + actual.keySet().stream().map(k -> k.substring(0, k.indexOf('|'))).distinct().count() + " census=" + actual);

        assertThat(overloaded).as("no live overloads today: a name with two live signatures means a DROP or a CREATE was not matched, "
            + "or a real overload arrived (then pin it and say so)").isEmpty();
        assertThat(r.live.size()).as("live keys equal live names").isEqualTo((int) names);
        assertThat(r.live.size()).as("LIVE_FUNCTIONS_PIN: the exact live function count").isEqualTo(LIVE_FUNCTIONS_PIN);
        assertThat(checkCensus(actual, NAME_PARSING_CENSUS))
            .as("RDR-229 item 4: live plpgsql bodies that parse a collection name. REGRESSION = a new site; "
                + "STALE FINGERPRINT = a site removed without lowering the pin")
            .isEmpty();
        assertThat(checkCensus(classifier, URI_CLASSIFIER_CENSUS))
            .as("the URI-string classifier's frozen-prefix literals, pinned apart from the name-parsing census")
            .isEmpty();
        assertThat(NAME_PARSING_CENSUS.values().stream().mapToInt(Integer::intValue).sum())
            .as("NAME_PARSING_TOTAL_CEILING equals the declared census: lowered in the same edit as any reduction")
            .isEqualTo(NAME_PARSING_TOTAL_CEILING);
        assertThat(total).as("the scanned total equals the ceiling").isEqualTo(NAME_PARSING_TOTAL_CEILING);
    }
}
