// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;
import org.w3c.dom.Element;
import org.w3c.dom.Node;

import javax.xml.XMLConstants;
import javax.xml.parsers.DocumentBuilderFactory;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;

/**
 * RDR-229 P3.1 stage 3 (nexus-p2m1x.17): the registry of the per-table stamp moves, read as text. One move per CHECK-guarded table of
 * {@code db/rdr229/p31-checks.tsv} (the retired queue's is conditional: R3), each registered by one INSERT into {@code nexus.data_moves} and nothing else,
 * with the values P3.2 set ({@link StampMoveSpecs}), a template that sets exactly the table's id columns, a verification that returns the
 * rows whose name resolves and whose id is NULL, and an {@code on_complete} that validates exactly the table's listed constraints. The
 * integration test ({@link CollectionIdStampMovesIntegrationTest}) runs them; this one pins what the INSERTs say, so an edit that drops a
 * constraint from a move, changes a P3.2 value or lets a table lose its move is red before a database is involved.
 */
class CollectionIdStampMovesShapeTest {

    /** The registration columns, in the order every INSERT lists them (verify_statement_timeout_ms is not named: the default 60000 applies). */
    private static final List<String> COLUMNS = List.of("label", "template", "driving_schema", "driving_table", "chunk_size", "sleep_ms",
        "statement_timeout_ms", "verification", "on_complete", "changeset_id");

    /** What every template starts with, lowercased so this file carries no SQL statement text (SqlStringLiteralGateTest). */
    private static final String TEMPLATE_HEAD = "update <driving_table> set ";

    private static final Pattern ROW_DML = Pattern.compile("(?is)\\b(?:update|delete\\s+from|insert\\s+into|merge\\s+into)\\b");

    /** vectors-029's guard, word for word (the owner-read pattern's own fixture carries it, so the two are compared, not retyped). */
    private static final String PATTERN_POLICY = "datamove/owner-read-policy/policy.xml";

    private record Move(String changesetId, String comment, Map<String, String> values, int chunkSize, int sleepMs, int statementTimeoutMs,
                        boolean hasPreconditions, String forwardSql) {}

    private static Element changelog(InputStream in) throws Exception {
        var dbf = DocumentBuilderFactory.newInstance();
        dbf.setNamespaceAware(true);
        dbf.setFeature(XMLConstants.FEATURE_SECURE_PROCESSING, true);
        dbf.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true);
        return dbf.newDocumentBuilder().parse(in).getDocumentElement();
    }

    private static List<Element> children(Element parent, String name) {
        List<Element> out = new ArrayList<>();
        for (Node n = parent.getFirstChild(); n != null; n = n.getNextSibling()) {
            if (n instanceof Element e && name.equals(e.getLocalName())) out.add(e);
        }
        return out;
    }

    private static List<Element> shippedChangesets() throws Exception {
        try (InputStream in = Files.newInputStream(StampMoveSpecs.CHANGELOG_FILE)) {
            return children(changelog(in), "changeSet");
        }
    }

    private static String forwardSql(Element changeSet) {
        StringBuilder sb = new StringBuilder();
        for (Element sql : children(changeSet, "sql")) sb.append(sql.getTextContent()).append('\n');
        return sb.toString();
    }

    /** The comma-separated items of the first parenthesised list after {@code VALUES}, each a quoted string ('' escapes a quote) or a bare token. */
    static List<String> valuesOf(String insert) {
        int at = insert.toUpperCase().indexOf("VALUES");
        assertThat(at).as("a VALUES list in %s", insert).isPositive();
        int i = insert.indexOf('(', at) + 1;
        List<String> out = new ArrayList<>();
        StringBuilder cur = new StringBuilder();
        boolean quoted = false;
        boolean wasQuoted = false;
        for (; i < insert.length(); i++) {
            char c = insert.charAt(i);
            if (quoted) {
                if (c == '\'' && i + 1 < insert.length() && insert.charAt(i + 1) == '\'') {
                    cur.append('\'');
                    i++;
                } else if (c == '\'') {
                    quoted = false;
                } else {
                    cur.append(c);
                }
            } else if (c == '\'') {
                quoted = true;
                wasQuoted = true;
            } else if (c == ',' || c == ')') {
                out.add(wasQuoted ? cur.toString() : cur.toString().trim());
                cur.setLength(0);
                wasQuoted = false;
                if (c == ')') break;
            } else if (!Character.isWhitespace(c) || cur.length() > 0) {
                cur.append(c);
            }
        }
        return out;
    }

    /** The registering changesets, by table: the changeset whose INSERT names the move's label. */
    private static Map<String, Move> moves() throws Exception {
        Map<String, Move> out = new LinkedHashMap<>();
        for (Element cs : shippedChangesets()) {
            String sql = forwardSql(cs);
            if (!sql.toLowerCase().contains("insert into nexus.data_moves")) continue;
            List<String> v = valuesOf(sql);
            assertThat(v).as("the INSERT of %s lists the ten registration columns", cs.getAttribute("id")).hasSize(COLUMNS.size());
            Map<String, String> values = new LinkedHashMap<>();
            for (int i = 0; i < COLUMNS.size(); i++) values.put(COLUMNS.get(i), v.get(i));
            String label = values.get("label");
            assertThat(label).startsWith(StampMoveSpecs.LABEL_PREFIX);
            String table = label.substring(StampMoveSpecs.LABEL_PREFIX.length());
            assertThat(out).as("one move per table: %s", table).doesNotContainKey(table);
            out.put(table, new Move(cs.getAttribute("id"), children(cs, "comment").get(0).getTextContent(), values,
                Integer.parseInt(values.get("chunk_size")), Integer.parseInt(values.get("sleep_ms")),
                Integer.parseInt(values.get("statement_timeout_ms")), !children(cs, "preConditions").isEmpty(), sql));
        }
        return out;
    }

    // ── one move per CHECK-guarded table, in the order the worker takes them ──────────────────────────────────

    @Test
    void everyCheckGuardedTableHasExactlyOneMove_theQueuesIsConditional_theRegistryIsFirst_andChunksLast() throws Exception {
        var moves = moves();
        var expected = StampMoveSpecs.WITH_QUEUE.stream().map(StampMoveSpecs.Spec::table).toList();
        assertThat(expected).as("non-vacuity: the 15 moves a fresh walk registers and the queue's").hasSize(StampMoveSpecs.SPECS.size() + 1);
        assertThat(moves.keySet()).as("the moves, in registration order (the worker takes the oldest first)").containsExactlyElementsOf(expected);
        assertThat(StampMoveSpecs.constraintsByTable().keySet())
            .as("the list and the specs name the same tables: a table added to p31-checks.tsv gets a move").containsExactlyInAnyOrderElementsOf(expected);
        assertThat(expected.get(0)).as("the registry, a small table whose tombstones the other moves' readers want settled first").isEqualTo("catalog_collections");
        assertThat(expected.get(expected.size() - 1)).as("the 38-hour chunks stamp starts last, so the small moves finish first").isEqualTo("chunks");
    }

    /**
     * R3: the queue's move is registered only while the table AND its collection_id column exist (no row is the check's "incomplete", and a kept queue
     * would never complete), by a DO guard inside the one INSERT's changeset and, as vectors-068-18, a precondition that retries the changeset while the
     * table exists without its column. CollectionIdQueueStampMoveIntegrationTest runs both postures; this pins the text.
     */
    @Test
    void theQueuesMoveIsRegisteredOnlyWhileTheTableAndItsColumnExist_guardedAsVectors068_18Is() throws Exception {
        Move m = moves().get("aspect_extraction_queue");
        assertThat(m.hasPreconditions()).as("the retry precondition, as vectors-068-18's").isTrue();
        Element cs = shippedChangesets().stream().filter(e -> e.getAttribute("id").equals(m.changesetId())).findFirst().orElseThrow();
        Element pre = children(cs, "preConditions").get(0);
        assertThat(pre.getAttribute("onFail")).as("unrecorded, so the next boot retries it while the column is missing").isEqualTo("CONTINUE");
        assertThat(pre.getAttribute("onError")).isEqualTo("CONTINUE");
        assertThat(children(pre, "sqlCheck").get(0).getTextContent()).contains("to_regclass('nexus.aspect_extraction_queue')").contains("collection_id");
        String sql = m.forwardSql();
        assertThat(sql).as("the DO guard: nothing is registered for a table that is absent or has no id column")
            .contains("IF to_regclass('nexus.aspect_extraction_queue') IS NOT NULL").contains("a.attname = 'collection_id'");
        assertThat(count(sql.toLowerCase(), "insert into nexus.data_moves")).as("one INSERT, inside the guard").isEqualTo(1);
        assertThat(sql.toLowerCase().indexOf("insert into nexus.data_moves")).as("the INSERT follows the guard").isGreaterThan(sql.indexOf("a.attname = 'collection_id'"));
        assertThat(m.values().get("on_complete")).as("the VALIDATE runs only where the table and its CHECK exist")
            .contains("to_regclass('nexus.aspect_extraction_queue')").contains("pg_constraint");
    }

    /** The two halves of stage 3 agree on every label: the one the changesets register and the ones the completion check reads. */
    @Test
    void theLabelsTheChangesetsRegisterAreTheLabelsTheCompletionCheckReads_sixteenOfSixteen() throws Exception {
        var registered = new java.util.TreeSet<String>();
        for (var e : moves().entrySet()) {
            registered.add(e.getValue().values().get("label"));
        }
        assertThat(registered).as("non-vacuity: 15 moves and the queue's, one label per table the check reads").hasSize(StampMoveSpecs.WITH_QUEUE.size());
        assertThat(StampMoveSpecs.LABEL_PREFIX).as("one prefix in both halves").isEqualTo(StampLabels.PREFIX);
        assertThat(StampLabels.all()).as("every label a changeset registers is one the check reads").containsAll(registered);
        assertThat(registered).as("registered-versus-read pin: the 16 labels the check reads (R3: the queue's is registered, conditionally, so it is never a label with no row on an install that kept the table)")
            .hasSize(16).containsExactlyInAnyOrderElementsOf(StampLabels.all());
    }

    @Test
    void eachMoveIsOneInsertInAChangesetOfItsOwn_withNoGuard_andItsOwnIdAsTheChangesetId() throws Exception {
        var moves = moves();
        assertThat(moves).as("non-vacuity").hasSize(StampMoveSpecs.WITH_QUEUE.size());
        for (var e : moves.entrySet()) {
            Move m = e.getValue();
            String where = e.getKey();
            boolean queue = where.equals("aspect_extraction_queue");   // R3: the one conditional move, see its own test
            assertThat(m.hasPreconditions()).as("%s: a guard would skip the INSERT in a posture the move is meant to be registered in", where).isEqualTo(queue);
            assertThat(ROW_DML.matcher(withoutLiterals(m.forwardSql())).results().count())
                .as("%s: the one INSERT and no other row DML (the template and verification text are values; the queue's INSERT is inside its DO guard's dollar-quoted body)", where)
                .isEqualTo(queue ? 0 : 1);
            assertThat(m.values().get("changeset_id")).as("%s: names the changeset that registers it", where).isEqualTo(m.changesetId());
            assertThat(m.changesetId()).startsWith(StampMoveSpecs.CHANGESET_PREFIX);
            assertThat(m.values().get("label")).as("data_moves_label_check refuses a slash").doesNotContain("/");
            assertThat(m.forwardSql()).as("%s: the column list is the ten registration columns, so verify_statement_timeout_ms keeps its default", where)
                .doesNotContain("verify_statement_timeout_ms").doesNotContain("supersedes");
            assertThat(m.values().get("driving_schema")).isEqualTo("nexus");
            assertThat(m.values().get("driving_table")).isEqualTo(where);
        }
    }

    // ── the P3.2 values and the budget arithmetic ──────────────────────────────────────────────────────────────

    @Test
    void theThrottleAndTheBoundAreP32s_andTheBoundCoversTheRecheckBudgetOfTheTable() throws Exception {
        var moves = moves();
        for (var spec : StampMoveSpecs.WITH_QUEUE) {
            Move m = moves.get(spec.table());
            assertThat(m.chunkSize()).as("%s chunk_size", spec.table()).isEqualTo(spec.chunkSize());
            assertThat(m.sleepMs()).as("%s sleep_ms", spec.table()).isEqualTo(spec.sleepMs());
            assertThat(m.statementTimeoutMs()).as("%s statement_timeout_ms: Sam's 60 s on a SUE-only move", spec.table())
                .isEqualTo(StampMoveSpecs.STATEMENT_TIMEOUT_MS);
            assertThat(m.statementTimeoutMs())
                .as("%s: the completion transaction's re-check shares one statement_timeout_ms between the table-wide run and every tenant's warm run (%d ms by the arithmetic)",
                    spec.table(), spec.recheckBudgetMs())
                .isGreaterThanOrEqualTo(spec.recheckBudgetMs());
            assertThat(m.comment()).as("%s: the arithmetic is written in the move's comment, with the same figure", spec.table())
                .contains("re-check budget " + spec.recheckBudgetMs() + " ms");
        }
        assertThat(moves.get("chunks").chunkSize()).as("the MVV pair, +6.2% p99 under load").isEqualTo(100);
        assertThat(moves.get("chunks").sleepMs()).isEqualTo(20_000);
        assertThat(moves.get("taxonomy_centroids").sleepMs()).isEqualTo(2_000);
    }

    // ── template, verification, on_complete ────────────────────────────────────────────────────────────────

    @Test
    void theTemplateSetsExactlyTheIdColumnsOfItsTable_onlyForRowsWhoseNameResolves_andHasTheChunkingMarkersOnce() throws Exception {
        for (var spec : StampMoveSpecs.WITH_QUEUE) {
            Move m = moves().get(spec.table());
            String template = m.values().get("template");
            assertThat(count(template, "<driving_table>")).as("%s: <driving_table> exactly once (assert_chunking_template)", spec.table()).isEqualTo(1);
            assertThat(count(template, "<chunking_clause>")).as("%s: <chunking_clause> exactly once", spec.table()).isEqualTo(1);
            assertThat(template.toLowerCase()).as("%s: an UPDATE of the driving table", spec.table()).startsWith(TEMPLATE_HEAD);
            List<String> ids = StampMoveSpecs.idColumns(spec.table());
            assertThat(ids).as("non-vacuity: the list gives %s an id column", spec.table()).isNotEmpty();
            String setList = template.substring(TEMPLATE_HEAD.length(), topLevelWhere(template));
            List<String> set = new ArrayList<>();
            for (String part : splitTopLevel(setList)) set.add(part.substring(0, part.indexOf('=')).trim());
            assertThat(set).as("%s: the columns the template sets", spec.table()).containsExactlyInAnyOrderElementsOf(ids);
            for (String id : ids) {
                assertThat(template).as("%s: a row already stamped is not written again (COALESCE keeps the id it has)", spec.table())
                    .contains("t." + id + " IS NULL");
            }
            if (!spec.table().equals("catalog_collections")) {
                assertThat(template).as("%s: the id comes from the registry row of the same tenant and name", spec.table())
                    .contains("nexus.catalog_collections").contains(".tenant_id = t.tenant_id");
                assertThat(template).as("%s: a name with no registry row (an orphan) is not a row of the move: the twin stays NULL and nothing is rewritten",
                    spec.table()).contains("EXISTS");
            }
        }
    }

    @Test
    void theRegistryTemplateImplementsR7_aTombstoneTakesItsLiveSuccessorsId_elseItsOwn_andALiveRowKeepsNull() throws Exception {
        Move m = moves().get("catalog_collections");
        String template = m.values().get("template");
        assertThat(template).contains("superseded_by_id = COALESCE(")
            .contains("t.collection_id)")
            .contains("t.superseded_by <> ''")
            .contains("t.superseded_by_id IS NULL");
        assertThat(m.values().get("verification")).contains("t.superseded_by <> ''").contains("t.superseded_by_id IS NULL");
    }

    @Test
    void theVerificationReturnsTheRowsOfTheOldShapeThatResolve_selectsOne_andTakesTheTenantAsDollarOne() throws Exception {
        for (var spec : StampMoveSpecs.WITH_QUEUE) {
            Move m = moves().get(spec.table());
            String v = m.values().get("verification");
            assertThat(v.toLowerCase()).as("%s: one row per old-shape row, of the real table aliased t", spec.table())
                .startsWith("select 1 from nexus." + spec.table() + " t where ");
            assertThat(v).as("%s: names the real table, not <driving_table> (the worker rewrites only $1)", spec.table()).doesNotContain("<driving_table>");
            assertThat(v).as("%s: the per-tenant and the table-wide run", spec.table()).contains("(t.tenant_id = $1 OR $1 IS NULL)");
            assertThat(MoveSql.refusal(v)).as("%s: a text the worker runs", spec.table()).isEmpty();
            assertThat(MoveSql.placeholders(v)).as("%s: $1 twice", spec.table()).isEqualTo(2);
            for (String id : StampMoveSpecs.idColumns(spec.table())) {
                assertThat(v).as("%s: the NULL twin", spec.table()).contains("t." + id + " IS NULL");
            }
            if (!spec.table().equals("catalog_collections")) {
                assertThat(v).as("%s: only a name that resolves, so an orphan is not a verification failure", spec.table()).contains("EXISTS");
            }
        }
    }

    @Test
    void onCompleteValidatesExactlyTheConstraintsOfThatTable_andNothingElse() throws Exception {
        var moves = moves();
        var byTable = StampMoveSpecs.constraintsByTable();
        int validated = 0;
        for (var spec : StampMoveSpecs.WITH_QUEUE) {
            String onComplete = moves.get(spec.table()).values().get("on_complete");
            assertThat(onComplete).as("%s: the CHECK's VALIDATE finishes the move", spec.table()).isNotBlank();
            var named = new ArrayList<String>();
            var m = Pattern.compile("VALIDATE\\s+CONSTRAINT\\s+(\\w+)").matcher(onComplete);
            while (m.find()) named.add(m.group(1));
            assertThat(named).as("%s: the constraints on_complete validates are the list's (the id check, and the NOT NULL guard on chunks and centroids)", spec.table())
                .containsExactlyInAnyOrderElementsOf(byTable.get(spec.table()));
            validated += named.size();
            assertThat(onComplete.replaceAll("(?is)alter\\s+table\\s+nexus\\.\\w+\\s+validate\\s+constraint\\s+\\w+\\s*;?", "").replaceAll("(?is)\\bdo\\b.*", "").trim())
                .as("%s: on_complete holds the VALIDATEs and nothing else", spec.table()).isEmpty();
        }
        assertThat(validated).as("non-vacuity: 20 listed constraints, the queue's included").isEqualTo(20);
    }

    // ── the policy the worker needs to read the driving table ───────────────────────────────────────────────

    /** The driving tables whose owner-read policy is another changeset's (P2.10's catalog-050-1 to -3), so this file creates none. */
    private static final Set<String> OWNED_ELSEWHERE = Set.of("catalog_documents", "document_aspects", "document_highlights");

    @Test
    void everyDrivingTableButChunksAndTheRegistryHasAnOwnerReadPolicyChangesetThatFollowsThePattern_afterItsMoveIsRegistered() throws Exception {
        List<Element> all = shippedChangesets();
        List<String> order = all.stream().map(cs -> cs.getAttribute("id")).toList();
        String guard = guardOf(patternChangeset());
        var moves = moves();
        int policies = 0;
        for (var spec : StampMoveSpecs.SPECS) {
            if (spec.table().equals("chunks")) continue;   // vectors-029's chunks_gate_probe_owner_read, mirrored on the leaves
            if (spec.table().equals("catalog_collections")) continue;   // D2's vectors-065 owns catalog_collections_owner_read (linda 2026-11-21): see the last assertion
            if (OWNED_ELSEWHERE.contains(spec.table())) continue;   // P2.10's catalog-050-1 to -3 own these three (linda 2026-11-21): see the last assertion
            String policy = spec.table() + "_owner_read";
            Element cs = all.stream().filter(e -> forwardSql(e).contains("POLICY " + policy + " ON nexus." + spec.table())).findFirst().orElse(null);
            assertThat(cs).as("%s: the migrating role needs a policy to read every tenant's rows", spec.table()).isNotNull();
            policies++;
            assertThat(order.indexOf(cs.getAttribute("id"))).as("%s: the policy follows its move's registration (its guard skips the whole changeset it sits in)", spec.table())
                .isGreaterThan(order.indexOf(moves.get(spec.table()).changesetId()));
            var guards = children(cs, "preConditions");
            assertThat(guards).hasSize(1);
            assertThat(guards.get(0).getAttribute("onFail")).isEqualTo("MARK_RAN");
            assertThat(guardOf(cs)).as("%s: vectors-029's guard, word for word", spec.table()).isEqualTo(guard);
            String sql = forwardSql(cs);
            assertThat(sql).contains("ON nexus." + spec.table()).contains("FOR SELECT TO CURRENT_USER").contains("USING (true)");
            assertThat(sql).as("%s: a post-condition RAISEs unless the policy names exactly the migrating role", spec.table()).contains("RAISE EXCEPTION");
            assertThat(ROW_DML.matcher(withoutLiterals(sql)).results().count()).as("%s: no row DML", spec.table()).isZero();
        }
        assertThat(policies).as("non-vacuity: 10 policies, all but chunks', the registry's and the three catalog-050 owns").isEqualTo(StampMoveSpecs.SPECS.size() - 2 - OWNED_ELSEWHERE.size());
        // the retired queue's policy (nothing else creates one on that table, so this file does): guarded for the table's absence, after its move
        Element queuePolicy = all.stream().filter(e -> forwardSql(e).contains("POLICY aspect_extraction_queue_owner_read ON nexus.aspect_extraction_queue")).findFirst().orElse(null);
        assertThat(queuePolicy).as("aspect_extraction_queue: the migrating role needs a policy to read every tenant's rows of a kept queue").isNotNull();
        assertThat(order.indexOf(queuePolicy.getAttribute("id"))).as("the queue's policy follows its move's registration")
            .isGreaterThan(order.indexOf(moves.get("aspect_extraction_queue").changesetId()));
        Element queueGuard = children(queuePolicy, "preConditions").get(0);
        assertThat(queueGuard.getAttribute("onFail")).as("skipped, unrecorded, while the kept table has no id column or another role owns it").isEqualTo("CONTINUE");
        assertThat(guardOf(queuePolicy)).as("vectors-029's exposure guard, word for word, is the first term of the queue's condition")
            .contains(guard.substring(guard.indexOf("COUNT")));
        String queueSql = forwardSql(queuePolicy);
        assertThat(queueSql).contains("IF to_regclass('nexus.aspect_extraction_queue') IS NOT NULL").contains("FOR SELECT TO CURRENT_USER").contains("USING (true)")
            .contains("RAISE EXCEPTION");
        assertThat(ROW_DML.matcher(withoutLiterals(queueSql)).results().count()).as("aspect_extraction_queue: no row DML").isZero();
        for (String owned : List.of("catalog_collections", "catalog_documents", "document_aspects", "document_highlights")) {
            assertThat(all.stream().filter(e -> forwardSql(e).contains(owned + "_owner_read")).map(e -> e.getAttribute("id")).toList())
                .as("two changesets owning one policy break each other's rollback: this file creates none on %s, whose owner-read policy is vectors-065's or catalog-050's", owned)
                .isEmpty();
        }
    }

    private static Element patternChangeset() throws Exception {
        try (InputStream in = CollectionIdStampMovesShapeTest.class.getClassLoader().getResourceAsStream(PATTERN_POLICY)) {
            assertThat(in).as(PATTERN_POLICY).isNotNull();
            return children(changelog(in), "changeSet").get(0);
        }
    }

    private static String guardOf(Element changeSet) {
        return children(children(changeSet, "preConditions").get(0), "sqlCheck").get(0).getTextContent().trim();
    }

    // ── the placement in the master changelog ───────────────────────────────────────────────────────────────

    @Test
    void theMasterIncludesTheFileAfterTheDualWriteFunctions_andBeforeTheRunAlwaysGrants() throws Exception {
        String master = Files.readString(Path.of("src", "main", "resources", "db", "changelog", "db.changelog-master.xml"));
        int dual = master.indexOf("<include file=\"db/changelog/vectors-069-collection-id-dual-write-functions.xml\"/>");
        int mine = master.indexOf("<include file=\"" + StampMoveSpecs.CHANGELOG + "\"/>");
        int grants = master.indexOf("<include file=\"db/changelog/grants-nexus-svc.xml\"/>");
        assertThat(dual).as("the dual-write include").isPositive();
        assertThat(mine).as("the stamp moves' include").isPositive();
        assertThat(mine).as("after the dual write: a move registered before the writers set the id would race them").isGreaterThan(dual);
        assertThat(mine).as("before the runAlways grants").isLessThan(grants);
    }

    // ── helpers ─────────────────────────────────────────────────────────────────────────────────────────────

    private static int count(String text, String needle) {
        int n = 0;
        for (int i = text.indexOf(needle); i >= 0; i = text.indexOf(needle, i + needle.length())) n++;
        return n;
    }

    /** {@code sql} with every single-quoted string and dollar-quoted body blanked: what is a value is not a statement. */
    private static String withoutLiterals(String sql) {
        return sql.replaceAll("(?s)\\$(\\w*)\\$.*?\\$\\1\\$", " ").replaceAll("(?s)'(?:[^']|'')*'", "''");
    }

    /** The index of the WHERE that ends the SET list: the first one outside every parenthesis (the subselects inside the list have their own). */
    private static int topLevelWhere(String sql) {
        int depth = 0;
        boolean quoted = false;
        for (int i = 0; i < sql.length(); i++) {
            char c = sql.charAt(i);
            if (c == '\'') quoted = !quoted;
            if (quoted) continue;
            if (c == '(') depth++;
            if (c == ')') depth--;
            if (depth == 0 && sql.startsWith(" WHERE ", i)) return i;
        }
        throw new AssertionError("no top-level WHERE in " + sql);
    }

    /** The items of a SET list, split at commas that are not inside parentheses. */
    private static List<String> splitTopLevel(String list) {
        List<String> out = new ArrayList<>();
        int depth = 0;
        StringBuilder cur = new StringBuilder();
        for (char c : list.toCharArray()) {
            if (c == '(') depth++;
            if (c == ')') depth--;
            if (c == ',' && depth == 0) {
                out.add(cur.toString().trim());
                cur.setLength(0);
            } else {
                cur.append(c);
            }
        }
        out.add(cur.toString().trim());
        return out;
    }
}
