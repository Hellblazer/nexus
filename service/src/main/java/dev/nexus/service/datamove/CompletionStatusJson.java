// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import dev.nexus.service.http.HttpUtil;

/**
 * The {@code rdr229_completion} field of {@code GET /v1/status}: RDR-229 P3.1's completion check ({@link CompletionCheck.Result})
 * as the wire shape {@code nx doctor} reads ({@code nexus/rdr229_completion_health.py}). [additive] Absent = an engine that predates
 * the check, or one that has not read it yet; a client must not read an absent field as "every gate holds".
 *
 * <p>It is a status field and not a {@code boot_work} entry because it is not work in progress (fix round A, critique B1; see
 * {@link CompletionBootWork}). <b>{@code /v1/status} is unauthenticated, so the field carries no number that describes tenants</b>
 * (confirmation review N2, linda's ruling): no registry row count, no tombstone count, no per-table orphan or unstamped count, and
 * no count in its text. Those are served on the authenticated {@code GET /v1/catalog/rdr229_completion_counts}, scoped to the
 * caller's tenant ({@link CompletionTenantCounts}). Keys, in order:
 * <pre>
 * {"gate1":bool,"gate2":bool,"gate3":bool,"gates_holding":0..3,"gates_total":3,"summary":text,
 *  "gate3_why":text ("" when gate 3 holds),
 *  "gate3_latched":bool (gate 3 has held once on this install: the engine's readers go by this, not by the live "gate3"),
 *  "id_index_invalid":bool (some id-predicate PCI index is INVALID: building, killed or mid-rebuild),
 *  "gate3_latch_pci_off":bool (the latch row says gate 3 latched while NX_SEARCH_PCI was off: no id index was required then),
 *  "labels_total":n,"labels_incomplete":[{"label","table","why"}],
 *  "checks_missing":[{"table","constraint","what"}],
 *  "validate_waits":[{"table","constraints":[names],"what":operator text}],
 *  "rename_missing":"none"|"name_key_drop_move"|"restart"}
 * </pre>
 * {@code rename_missing} (RDR-229 P3.7, [additive]) is what a collection rename waits for, spelled as the 409 {@code rename_refused}'s
 * {@code missing}: {@code none} before gate 3 (the rename is the name path) and once the one-row rename can run, {@code name_key_drop_move}
 * from gate 3 until that move has {@code completed_at}, then {@code restart} until every P3.5b changeset has applied.
 * {@code labels_total} and {@code gates_holding} count the engine's own moves and gates, not any tenant's data. {@code labels_incomplete}
 * lists each stamp label whose move is not complete (a label the queue allowlist marks not applicable is not listed).
 * {@code validate_waits[].what} is the operator text: it names the {@code nexus.rdr229_restamp} step.
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
            .append(",\"gate3_latched\":").append(r.gate3Latched())
            .append(",\"id_index_invalid\":").append(r.idIndexInvalid())
            .append(",\"gate3_latch_pci_off\":").append(r.gate3LatchPciOff())
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
        b.append("],\"validate_waits\":[");
        first = true;
        for (CompletionBootWork.TableWait w : CompletionBootWork.byTable(r.violators())) {
            b.append(first ? "" : ",").append("{\"table\":").append(HttpUtil.jsonString(w.table())).append(",\"constraints\":[");
            for (int i = 0; i < w.constraints().size(); i++) {
                b.append(i == 0 ? "" : ",").append(HttpUtil.jsonString(w.constraints().get(i)));
            }
            b.append("],\"what\":").append(HttpUtil.jsonString(CompletionBootWork.validateWaits(w))).append('}');
            first = false;
        }
        RenameReadiness.Missing missing = r.renameMissing();
        return b.append("],\"rename_missing\":").append(HttpUtil.jsonString(missing == RenameReadiness.Missing.NONE ? "none"
            : dev.nexus.service.db.RenameRefusedException.wireValue(missing))).append('}').toString();
    }
}
