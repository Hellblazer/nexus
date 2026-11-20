// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.CollectionNameSqlLiveBodyGateTest.*;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * Falsification tests for {@link CollectionNameSqlLiveBodyGateTest} (RDR-229 P1.3, nexus-p2m1x.3): each one feeds the gate's
 * helpers an in-memory changelog and checks that the mechanism it names detects what it must, and fails loud on what it
 * cannot read. Split out of the gate class to stay under the test file-size cap.
 */
class CollectionNameSqlLiveBodyGateFalsificationTest {

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
        assertThat(r.live.get("nexus.f(text)").file()).isEqualTo("b.xml");
        assertThat(census(r.live)).as("the old body is not live").isEmpty();

        var newHasParse = new java.util.LinkedHashMap<String, String>();
        newHasParse.put("a.xml", changelog(fn("nexus.f", "p"), null));
        newHasParse.put("b.xml", changelog(fn("nexus.f", "split_part(p, '__', 3)"), null));
        assertThat(census(resolve(newHasParse).live)).containsExactly(Map.entry("nexus.f|" + DUNDER_LITERAL, 1));
    }

    @Test
    void aBodyInsideARollbackBlockIsNotLive() {
        var files = new java.util.LinkedHashMap<String, String>();
        files.put("a.xml", changelog(fn("nexus.f", "p"), fn("nexus.f", "split_part(p, '__', 3)")));
        files.put("b.xml", changelog(fn("nexus.g", "p"), null));  // <rollback/> self-closing
        Resolution r = resolve(files);
        assertThat(census(r.live)).as("the rollback copy of a body is the OLD body, never counted").isEmpty();
        assertThat(r.live.get("nexus.f(text)").text()).doesNotContain("split_part");
    }

    @Test
    void aDroppedFunctionIsNotLive_andRecreatingItBringsItBack() {
        var files = new java.util.LinkedHashMap<String, String>();
        files.put("a.xml", changelog(fn("nexus.f", "split_part(p, '__', 3)"), null));
        files.put("b.xml", changelog("DROP FUNCTION IF EXISTS nexus.f(text);", null));
        Resolution dropped = resolve(files);
        assertThat(dropped.live).isEmpty();
        assertThat(census(dropped.live)).isEmpty();

        files.put("c.xml", changelog(fn("nexus.f", "string_to_array(p, '__')"), null));
        assertThat(census(resolve(files).live)).containsExactly(Map.entry("nexus.f|" + DUNDER_LITERAL, 1));
    }

    @Test
    void theTwoShapesCount_andLookalikesDoNot() {
        String body = fn("nexus.f",
            "split_part(p, '__', 3) || split_part(lower(p), '__', 1) || split_part(p, ',', 1)"
            + " || (string_to_array(p, '__'))[1] || string_to_array(p, ',')::text"
            + " || CASE WHEN starts_with(p, 'quarantine-') THEN 'quarantine-%' ELSE 'quarantine' END");
        Map<String, Integer> found = expressions(blankSqlComments(body));
        assertThat(found).containsEntry(DUNDER_LITERAL, 3).containsEntry(QUARANTINE_LITERAL, 2);
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
        assertThat(r.live.values().stream().map(Live::name)).doesNotContain("nexus.ghost");
        assertThat(census(r.live)).containsExactly(Map.entry("nexus.f|" + DUNDER_LITERAL, 1));
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
        Map<String, Integer> actual = Map.of("nexus.f|" + DUNDER_LITERAL, 3);
        Map<String, Integer> declared = Map.of("nexus.f|" + DUNDER_LITERAL, 2);
        assertThat(checkCensus(actual, declared)).anyMatch(v -> v.contains("REGRESSION"));
        assertThat(checkCensus(Map.of("nexus.new_fn|" + QUARANTINE_LITERAL, 1), Map.of()))
            .as("a function the census has never seen").anyMatch(v -> v.contains("REGRESSION"));
    }

    @Test
    void anExpressionRemovedWithoutLoweringThePin_failsAsStaleFingerprint() {
        Map<String, Integer> actual = Map.of("nexus.f|" + DUNDER_LITERAL, 1);
        Map<String, Integer> declared = Map.of("nexus.f|" + DUNDER_LITERAL, 2);
        assertThat(checkCensus(actual, declared)).anyMatch(v -> v.contains("STALE FINGERPRINT"));
        assertThat(checkCensus(Map.of(), declared)).as("the function itself is gone").anyMatch(v -> v.contains("STALE FINGERPRINT"));
    }

    @Test
    void anEmptyScanFails_andSoDoesAMisresolvedLastDefiner() {
        assertThatThrownBy(() -> assertNonVacuous(resolve(Map.of()), Map.of(), 1, 1))
            .as("no includes, no blocks, no functions").isInstanceOf(AssertionError.class);

        var empty = new java.util.LinkedHashMap<String, String>();
        empty.put("a.xml", "<databaseChangeLog/>");
        assertThatThrownBy(() -> assertNonVacuous(resolve(empty), Map.of(), 1, 1))
            .as("an include with no forward <sql> is a scan that found nothing").isInstanceOf(AssertionError.class);

        var files = new java.util.LinkedHashMap<String, String>();
        files.put("a.xml", changelog(fn("nexus.f", "p"), null));
        files.put("b.xml", changelog(fn("nexus.f", "p"), null));
        assertNonVacuous(resolve(files), Map.of("nexus.f", "b.xml"), 1, 1);
        assertThatThrownBy(() -> assertNonVacuous(resolve(files), Map.of("nexus.f", "a.xml"), 1, 1))
            .as("the last definer is b.xml, so a pin naming a.xml is a misresolution").isInstanceOf(AssertionError.class);
        assertThatThrownBy(() -> assertNonVacuous(resolve(files), Map.of("nexus.missing", "a.xml"), 1, 1))
            .as("a required function that is not live fails").isInstanceOf(AssertionError.class);
    }

    // ── RDR-229 P1.3 review round 1 ─────────────────────────────────────

    private static int total(Map<String, Integer> c) {
        return c.values().stream().mapToInt(Integer::intValue).sum();
    }

    private static String fnWith(String name, String args, String expr) {
        return "CREATE OR REPLACE FUNCTION " + name + "(" + args + ") RETURNS text LANGUAGE plpgsql AS $$\n"
            + "BEGIN RETURN " + expr + "; END $$;";
    }

    private static Resolution one(String sql) {
        var files = new java.util.LinkedHashMap<String, String>();
        files.put("a.xml", changelog(sql, null));
        return resolve(files);
    }

    @Test
    void overloads_areSeparateLiveFunctions_andOneReplacementLeavesTheOther() {
        String both = fnWith("nexus.f", "p integer", "split_part('a', '__', 1)") + "\n"
            + fnWith("nexus.f", "p text", "split_part('a', '__', 2)");
        Resolution r = one(both);
        assertThat(r.live).as("two overloads of one name").hasSize(2);
        assertThat(total(census(r.live))).as("each overload's expression counts").isEqualTo(2);

        Resolution replaced = one(both + "\n" + fnWith("nexus.f", "q integer", "q::text"));
        assertThat(replaced.live).hasSize(2);
        assertThat(total(census(replaced.live))).as("CREATE OR REPLACE of the integer overload leaves the text one").isEqualTo(1);
    }

    @Test
    void aDropNamesItsArgumentList_soOnlyThatOverloadGoes() {
        String both = fnWith("nexus.f", "p integer", "split_part('a', '__', 1)") + "\n"
            + fnWith("nexus.f", "p text", "split_part('a', '__', 2)");
        Resolution r = one(both + "\nDROP FUNCTION nexus.f(integer);");
        assertThat(r.live).hasSize(1);
        assertThat(total(census(r.live))).isEqualTo(1);

        assertThat(one(both + "\nDROP FUNCTION IF EXISTS nexus.f(p text);").live).as("a named argument in the DROP").hasSize(1);
        assertThat(one(both + "\nDROP FUNCTION nexus.f;").live).as("no argument list drops every overload").isEmpty();
        assertThat(one(both + "\nDROP FUNCTION nexus.f(bigint);").live).as("a signature nothing has drops nothing").hasSize(2);
    }

    @Test
    void oneDropStatementCanNameSeveralFunctions() {
        String sql = fnWith("nexus.a", "p integer", "split_part('a', '__', 1)") + "\n"
            + fnWith("nexus.b", "p int", "split_part('a', '__', 1)") + "\n"
            + fnWith("nexus.c", "p int", "split_part('a', '__', 1)") + "\n"
            + "DROP FUNCTION nexus.a(int), nexus.b(integer);";
        Resolution r = one(sql);
        assertThat(r.live.values().stream().map(Live::name)).containsExactly("nexus.c");
    }

    @Test
    void aDropByTypesMatchesACreateByNamedParameters_theWayVectors055DoesForSearchAspectScoped() {
        String create = fnWith("nexus.s", "p_query nexus.vector, p_collections text[], p_min double precision, p_n integer, p_tenant text",
            "split_part('a', '__', 1)");
        assertThat(one(create).live).hasSize(1);
        assertThat(one(create + "\nDROP FUNCTION IF EXISTS nexus.s(nexus.vector,text[],double precision,integer,text);").live)
            .as("the exact signature drops it").isEmpty();
        assertThat(one(create + "\nDROP FUNCTION IF EXISTS nexus.s(nexus.vector,text[],double precision,integer);").live)
            .as("one argument short is a different function: it stays live").hasSize(1);
        assertThat(one(create + "\nDROP FUNCTION IF EXISTS nexus.s(nexus.vector, text[], float8, int, varchar(10));").live)
            .as("type aliases: float8 = double precision, int = integer; varchar is not text, so it stays").hasSize(1);
        assertThat(one(create + "\nDROP FUNCTION IF EXISTS nexus.s(nexus.vector, text[], float8, int, text);").live)
            .as("type aliases: float8 = double precision, int = integer").isEmpty();
    }

    private static int bodyCount(String expr) {
        return total(census(one(fnWith("nexus.f", "p text", expr)).live));
    }

    @Test
    void everyDunderRespellingCounts_notOnlyInsideSplitPartOrStringToArray() {
        assertThat(bodyCount("split_part(p, $x$__$x$, 3)")).as("a dollar-quoted separator").isEqualTo(1);
        assertThat(bodyCount("string_to_table(p, '__')::text")).as("string_to_table").isEqualTo(1);
        assertThat(bodyCount("(regexp_split_to_array(p, '__'))[1]")).as("regexp_split_to_array").isEqualTo(1);
        assertThat(bodyCount("strpos(p, '__')::text")).as("strpos").isEqualTo(1);
        assertThat(bodyCount("position('__' in p)::text")).as("position").isEqualTo(1);
        assertThat(bodyCount("CASE WHEN p LIKE '%\\_\\_%' THEN p END")).as("an escaped LIKE").isEqualTo(1);
        assertThat(bodyCount("CASE WHEN p LIKE 'code__%' THEN p END")).as("a content-type prefix literal").isEqualTo(1);
        assertThat(bodyCount("CASE WHEN p LIKE ANY (ARRAY['rdr__%', 'docs__%']) THEN p END")).as("two prefix literals").isEqualTo(2);
        assertThat(bodyCount("p || '_' || '_'")).as("an assembled separator").isEqualTo(1);
        assertThat(bodyCount("repeat('_', 2)")).as("repeat('_', n)").isEqualTo(1);
        assertThat(bodyCount("p || 'a_b'")).as("a single underscore is not a separator").isZero();

        String declared = "CREATE OR REPLACE FUNCTION nexus.g(p text) RETURNS text LANGUAGE plpgsql AS $$\n"
            + "DECLARE v_sep text := '__';\nBEGIN RETURN p; END $$;";
        assertThat(total(census(one(declared).live))).as("a separator held in a variable").isEqualTo(1);
    }

    @Test
    void aLabelThatMerelyContainsTheWord_isNotAQuarantinePrefix() {
        assertThat(bodyCount("'gc-quarantine-orphans'")).as("an audit label").isZero();
        assertThat(bodyCount("'quarantine-%'")).as("a LIKE pattern on the prefix").isEqualTo(1);
    }

    @Test
    void theOuterBodyQuoteIsNotALiteral_butAnInnerDollarQuotedStringIs() {
        String text = "CREATE OR REPLACE FUNCTION nexus.f(p text) RETURNS text LANGUAGE plpgsql AS $body$\n"
            + "BEGIN RETURN split_part(p, $q$__$q$, 1) || $q$quarantine-$q$; END $body$;";
        assertThat(census(one(text).live).values()).containsExactlyInAnyOrder(1, 1);
    }

    @Test
    void theIncludeReader_acceptsAttributesInAnyOrder_andRefusesWhatItCannotRead() {
        assertThat(includes("<databaseChangeLog><include author=\"a\" file=\"db/changelog/a.xml\" relativeToChangelogFile=\"false\"/>"
            + "<!-- <include file=\"db/changelog/ghost.xml\"/> --><include file=\"db/changelog/b.xml\"/></databaseChangeLog>"))
            .containsExactly("a.xml", "b.xml");
        assertThatThrownBy(() -> includes("<databaseChangeLog><include file=\"elsewhere/x.xml\"/></databaseChangeLog>"))
            .as("an include the reader cannot place is a failure, never a silent skip").isInstanceOf(IllegalStateException.class);
        assertThatThrownBy(() -> includes("<databaseChangeLog><includeAll path=\"db/changelog/\"/></databaseChangeLog>"))
            .as("includeAll names no file").isInstanceOf(IllegalStateException.class);
    }

    @Test
    void aForwardChangesetTheScanCannotReadFailsLoud_butARollbackOfOneDoesNot() {
        for (String tag : List.of("<sqlFile path=\"x.sql\"/>", "<createProcedure>CREATE FUNCTION f()</createProcedure>",
                "<customChange class=\"x\"/>", "<executeCommand executable=\"x\"/>")) {
            assertThatThrownBy(() -> forwardSqlBlocks("<databaseChangeLog><changeSet id=\"x\">" + tag + "</changeSet></databaseChangeLog>"))
                .as(tag).isInstanceOf(IllegalStateException.class);
        }
        assertThat(forwardSqlBlocks("<databaseChangeLog><changeSet id=\"x\"><sql>SELECT 1</sql><rollback><sqlFile path=\"x.sql\"/></rollback>"
            + "</changeSet><!-- <sqlFile path=\"y.sql\"/> --></databaseChangeLog>")).hasSize(1);
    }

    @Test
    void everyChangelogInTheDirectoryMustBeIncluded() {
        assertEveryChangelogIncluded(List.of("a.xml", "b.xml", MASTER), List.of("a.xml", "b.xml"));
        assertThatThrownBy(() -> assertEveryChangelogIncluded(List.of("a.xml", "orphan.xml", MASTER), List.of("a.xml")))
            .as("a changelog nothing includes is never scanned").isInstanceOf(AssertionError.class);
        assertThatThrownBy(() -> assertEveryChangelogIncluded(List.of("a.xml"), List.of("a.xml", "gone.xml")))
            .as("an include with no file").isInstanceOf(AssertionError.class);
    }

    @Test
    void theFloorsFailAScanThatFoundTooLittle() {
        var files = new java.util.LinkedHashMap<String, String>();
        files.put("a.xml", changelog(fn("nexus.f", "p"), null));
        Resolution r = resolve(files);
        assertNonVacuous(r, Map.of(), 1, 1);
        assertThatThrownBy(() -> assertNonVacuous(r, Map.of(), 2, 1)).as("include floor").isInstanceOf(AssertionError.class);
        assertThatThrownBy(() -> assertNonVacuous(r, Map.of(), 1, 2)).as("live-function floor").isInstanceOf(AssertionError.class);
    }

    @Test
    void theCommentBlanker_honoursBackslashEscapesInEStringsAndDoubledQuotes() {
        String e = "SELECT E'it\\'s -- not a comment' || split_part(p, '__', 1); -- a real comment\n";
        String blankedE = blankSqlComments(e);
        assertThat(blankedE).contains("-- not a comment").contains("split_part(p, '__', 1)").doesNotContain("a real comment");

        String doubled = "SELECT 'it''s -- not a comment' || split_part(p, '__', 1); -- a real comment\n";
        String blankedD = blankSqlComments(doubled);
        assertThat(blankedD).contains("-- not a comment").contains("split_part(p, '__', 1)").doesNotContain("a real comment");
    }

    // ── RDR-229 P1.3 confirmation round: each mechanism has a test that fails without it ─

    @Test
    void anUnqualifiedTypeInADrop_matchesTheSchemaQualifiedTypeInTheCreate() {
        String create = fnWith("nexus.f", "p_q nexus.vector, p_n integer", "p_n::text");
        assertThat(one(create).live).hasSize(1);
        assertThat(one(create + "\nDROP FUNCTION nexus.f(vector, int);").live)
            .as("vector and nexus.vector are one type (vectors-007 wrote the first, vectors-055 the second)").isEmpty();
        assertThat(one(create + "\nDROP FUNCTION nexus.f(public.vector, int);").live).as("public. folds too").isEmpty();
    }

    @Test
    void aDefaultInTheArgumentList_isNotPartOfTheSignature() {
        String create = fnWith("nexus.catalog_reconcile_reap", "p_tenant text, p_interval interval DEFAULT interval '24 hours'",
            "p_tenant");
        assertThat(one(create + "\nDROP FUNCTION nexus.catalog_reconcile_reap(text, interval);").live)
            .as("the DEFAULT is dropped from the CREATE's identity").isEmpty();
        String assigned = fnWith("nexus.g", "p_n integer := 5, p_t text = 'a,b'", "p_t");
        assertThat(one(assigned + "\nDROP FUNCTION nexus.g(integer, text);").live).as(":= and = defaults").isEmpty();
    }

    @Test
    void anOutArgument_isNotPartOfTheSignature() {
        String create = fnWith("nexus.f", "p integer, OUT r text", "p::text");
        assertThat(one(create + "\nDROP FUNCTION nexus.f(integer);").live).isEmpty();
        assertThat(one(create + "\nDROP FUNCTION nexus.f(integer, text);").live)
            .as("a DROP that lists the OUT argument names a different function").hasSize(1);
    }

    @Test
    void anUnqualifiedFunctionName_failsTheNonVacuityGuard() {
        Resolution r = one(fnWith("f", "p integer", "p::text"));
        assertThat(r.live).hasSize(1);
        assertThatThrownBy(() -> assertNonVacuous(r, Map.of(), 1, 1))
            .as("an unqualified name cannot be matched to a schema-qualified DROP").isInstanceOf(AssertionError.class);
    }

    @Test
    void aDropInsideAFunctionBodyIsText_notAStatement() {
        String sql = fnWith("nexus.g", "p integer", "p::text") + "\n"
            + fnWith("nexus.f", "p integer", "(SELECT 'DROP FUNCTION nexus.g(integer)')");
        assertThat(one(sql).live.values().stream().map(Live::name)).containsExactlyInAnyOrder("nexus.f", "nexus.g");
    }

    @Test
    void aDuplicateInclude_failsTheCoverageAssertion() {
        assertThatThrownBy(() -> assertEveryChangelogIncluded(List.of("a.xml", MASTER), List.of("a.xml", "a.xml")))
            .as("an include listed twice").isInstanceOf(AssertionError.class);
    }

    @Test
    void literals_honourBackslashEscapesInEStrings() {
        assertThat(bodyCount("E'it\\'s' || split_part(p, '__', 1)")).as("E-string backslash escape").isEqualTo(1);
        assertThat(literals(blankSqlComments("BEGIN RETURN E'a\\'b__' ; END")))
            .as("the escaped quote stays inside the literal").containsExactly("a\\'b__");
    }

    @Test
    void literals_honourDoubledQuotes() {
        assertThat(bodyCount("'x__''y__'")).as("a doubled quote does not end the literal: one literal, not two").isEqualTo(1);
        assertThat(literals("'it''s'")).containsExactly("it''s");
    }

    // ── the signature normaliser ────────────────────────────────────────

    @Test
    void aTypeModifierIsDroppedBeforeTokenising_soNumericWithPrecisionIsNotAnEmptySignature() {
        assertThat(signature("p numeric (10, 2)")).isEqualTo("(numeric)");
        assertThat(signature("numeric (10, 2)")).as("unnamed, with a space before the modifier: not an empty signature")
            .isEqualTo("(numeric)");
        assertThat(signature("numeric (10, 2)")).isNotEqualTo(signature(""));
        assertThat(signature("numeric(10,2), p_x varchar (20)")).isEqualTo("(numeric,character varying)");
        assertThat(signature("p timestamp (3) with time zone")).isEqualTo("(timestamp with time zone)");
    }

    @Test
    void typeSpellingsFoldToOneName() {
        assertThat(signature("timestamp")).isEqualTo(signature("p timestamp without time zone"));
        assertThat(signature("time")).isEqualTo(signature("p time without time zone"));
        assertThat(signature("char")).isEqualTo(signature("p character"));
        assertThat(signature("p char(3)")).isEqualTo(signature("character"));
        assertThat(signature("p float(24)")).isEqualTo("(real)");
        assertThat(signature("p float(53)")).isEqualTo("(double precision)");
        assertThat(signature("p float8, q float4")).isEqualTo("(double precision,real)");
        assertThat(signature("p text ARRAY")).isEqualTo("(text[])");
        assertThat(signature("p text[3]")).isEqualTo("(text[])");
        assertThat(signature("p integer[][]")).isEqualTo("(integer[])");
        assertThat(signature("p int2, q int8, r int4, s bool, t decimal")).isEqualTo("(smallint,bigint,integer,boolean,numeric)");
        assertThat(signature("p char varying, q varbit")).isEqualTo("(character varying,bit varying)");
    }

    @Test
    void modesAndQuotedNames() {
        assertThat(signature("IN OUT p integer")).as("IN OUT is INOUT: an input").isEqualTo("(integer)");
        assertThat(signature("INOUT p integer")).isEqualTo("(integer)");
        assertThat(signature("IN p integer, OUT r text")).isEqualTo("(integer)");
        assertThat(signature("VARIADIC p text[]")).isEqualTo("(text[])");
        assertThat(signature("\"p x\" integer, \"q\" text")).as("quoted parameter names").isEqualTo("(integer,text)");
        assertThat(signature("")).isEqualTo("()");
    }

    @Test
    void anArgumentTheNormaliserCannotReduce_failsLoud() {
        assertThatThrownBy(() -> signature("p integer,, q text")).as("an empty argument").isInstanceOf(IllegalStateException.class);
        assertThatThrownBy(() -> signature("DEFAULT 1")).as("nothing left after the default").isInstanceOf(IllegalStateException.class);
        assertThatThrownBy(() -> signature("IN")).as("a mode and no type").isInstanceOf(IllegalStateException.class);
        assertThatThrownBy(() -> signature("p mystery type here")).as("whitespace outside the known multi-word types")
            .isInstanceOf(IllegalStateException.class);
    }
}
