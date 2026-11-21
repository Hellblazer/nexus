// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import dev.nexus.service.http.HttpUtil;

import java.util.Map;

/**
 * The {@code rdr229_completion} field of {@code GET /v1/status}: RDR-229 P3.1's completion check ({@link CompletionCheck.Result})
 * as the wire shape {@code nx doctor} reads ({@code nexus/rdr229_completion_health.py}). [additive] Absent = an engine that predates
 * the check, or one that has not read it yet; a client must not read an absent field as "every gate holds".
 *
 * <p>It is a status field and not a {@code boot_work} entry because it is not work in progress (fix round A, critique B1; see
 * {@link CompletionBootWork}). It names no collection or tenant: the route is unauthenticated. Keys, in order:
 * <pre>
 * {"gate1":bool,"gate2":bool,"gate3":bool,"gates_holding":0..3,"gates_total":3,"summary":text,
 *  "gate3_why":text ("" when gate 3 holds),
 *  "labels_total":n,"labels_incomplete":[{"label","table","why"}],
 *  "checks_missing":[{"table","constraint","what"}],"superseded_by_waiting":n,
 *  "validate_waits":[{"table","constraints":[names],"orphans":n,"unstamped":n,"counted":bool,"what":operator text}],
 *  "dangling_tombstones":{"read":bool,"registry_rows":n,"total":n,"by_content_type":{type:n},
 *                         "self_id":{"total":n,"by_content_type":{type:n}}}}
 * </pre>
 * {@code labels_incomplete} lists each stamp label whose move is not complete (a label the queue allowlist marks not applicable is
 * not listed). {@code validate_waits[].what} is the operator text: it names the {@code nexus.rdr229_restamp} step.
 * {@code dangling_tombstones} is R7's pre-flight: tombstones whose {@code superseded_by} names no row of their tenant, by content type,
 * and those the stamp resolved to its own id ({@code self_id}); {@code registry_rows} is what was read, so a zero is not a registry that
 * was not read.
 */
final class CompletionStatusJson {

    private CompletionStatusJson() { /* static */ }

    static String render(CompletionCheck.Result r) {
        long holding = (r.gate1() ? 1 : 0) + (r.gate2() ? 1 : 0) + (r.gate3() ? 1 : 0);
        StringBuilder b = new StringBuilder(512);
        b.append("{\"gate1\":").append(r.gate1()).append(",\"gate2\":").append(r.gate2()).append(",\"gate3\":").append(r.gate3())
            .append(",\"gates_holding\":").append(holding).append(",\"gates_total\":3")
            .append(",\"summary\":").append(HttpUtil.jsonString(CompletionBootWork.summary(r)))
            .append(",\"gate3_why\":").append(HttpUtil.jsonString(r.gate3() ? "" : r.gate3Why()))
            .append(",\"labels_total\":").append(r.labels().size())
            .append(",\"labels_incomplete\":[");
        boolean first = true;
        for (CompletionCheck.LabelState l : r.incomplete()) {
            b.append(first ? "" : ",").append("{\"label\":").append(HttpUtil.jsonString(l.label()))
                .append(",\"table\":").append(l.table() == null ? "null" : HttpUtil.jsonString(l.table()))
                .append(",\"why\":").append(HttpUtil.jsonString(l.why())).append('}');
            first = false;
        }
        b.append("],\"checks_missing\":[");
        first = true;
        for (CompletionCheck.CheckState c : r.checks()) {
            if (c.present()) {
                continue;
            }
            b.append(first ? "" : ",").append("{\"table\":").append(HttpUtil.jsonString(c.table()))
                .append(",\"constraint\":").append(HttpUtil.jsonString(c.constraint()))
                .append(",\"what\":").append(HttpUtil.jsonString(CompletionBootWork.missing(c, r))).append('}');
            first = false;
        }
        b.append("],\"superseded_by_waiting\":").append(r.supersededByWaiting()).append(",\"validate_waits\":[");
        first = true;
        for (CompletionBootWork.TableWait w : CompletionBootWork.byTable(r.violators())) {
            b.append(first ? "" : ",").append("{\"table\":").append(HttpUtil.jsonString(w.table())).append(",\"constraints\":[");
            for (int i = 0; i < w.constraints().size(); i++) {
                b.append(i == 0 ? "" : ",").append(HttpUtil.jsonString(w.constraints().get(i)));
            }
            b.append("],\"orphans\":").append(w.orphans()).append(",\"unstamped\":").append(w.unstamped())
                .append(",\"counted\":").append(w.counted())
                .append(",\"what\":").append(HttpUtil.jsonString(CompletionBootWork.validateWaits(w))).append('}');
            first = false;
        }
        CompletionCheck.Dangling d = r.dangling();
        b.append("],\"dangling_tombstones\":{\"read\":").append(d.read()).append(",\"registry_rows\":").append(d.registryRows())
            .append(",\"total\":").append(d.total()).append(",\"by_content_type\":");
        counts(b, d.byContentType());
        b.append(",\"self_id\":{\"total\":").append(d.selfIdTotal()).append(",\"by_content_type\":");
        counts(b, d.selfIdByContentType());
        b.append("}}}");
        return b.toString();
    }

    private static void counts(StringBuilder b, Map<String, Long> byType) {
        b.append('{');
        boolean first = true;
        for (var e : new java.util.TreeMap<>(byType).entrySet()) {
            b.append(first ? "" : ",").append(HttpUtil.jsonString(e.getKey())).append(':').append(e.getValue());
            first = false;
        }
        b.append('}');
    }
}
