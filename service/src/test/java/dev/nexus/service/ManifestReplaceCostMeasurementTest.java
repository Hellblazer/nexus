// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.ManifestReplaceTestHooks;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * nexus-z0o2p.29 measurement, run by hand: {@code mvn test -Dtest.excluded.groups= -Dgroups=measurement
 * -Dtest=ManifestReplaceCostMeasurementTest}. The wall time of a manifest replace of one document through
 * {@code CatalogRepository.writeManifest}, with the orphaning-record triggers on, for the delete-and-insert
 * (the diff declined by its test hook) and the diff: the same rows, 10% of the rows changed, every row
 * changed. Prints {@code MEASURE} lines; asserts nothing about time.
 */
@Tag("measurement")
class ManifestReplaceCostMeasurementTest extends AtomicWriteTestBase {

    private static Map<String, Object> rich(int p, String chash, boolean changed) {
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("position", p);
        r.put("chash", chash);
        r.put("chunk_index", p);
        r.put("line_start", p * 10);
        r.put("line_end", p * 10 + 9);
        r.put("char_start", p * 100);
        r.put("char_end", p * 100 + 99);
        r.put("section_title", changed ? "changed " + p : "title " + p);
        r.put("section_type", "section");
        r.put("page_number", p);
        r.put("labels_written", true);
        return r;
    }

    private long replaceMs(Fx f, List<Map<String, Object>> rows) {
        long t0 = System.nanoTime();
        repo.writeManifest(TENANT, f.docId(), f.collection(), rows);
        return (System.nanoTime() - t0) / 1_000_000;
    }

    @Test
    void measure() {
        for (int n : List.of(4000, 5000)) {
            Fx f = fixture("cost" + n);
            List<Map<String, Object>> chunks = new ArrayList<>();
            List<String> hex = new ArrayList<>();
            for (int i = 0; i < n; i++) {
                String c = ch("cost-" + n + "-" + i);
                hex.add(c);
                chunks.add(chunk(c, "text " + i));
            }
            List<Map<String, Object>> same = new ArrayList<>(), tenth = new ArrayList<>(), all = new ArrayList<>();
            for (int i = 0; i < n; i++) {
                same.add(rich(i, hex.get(i), false));
                tenth.add(rich(i, hex.get(i), i % 10 == 0));
                all.add(rich(i, hex.get(i), true));
            }
            svc.writeManyCombined(TENANT, f.collection(), chunks, List.of(doc(f.docId(), same)), null, false, false);
            for (boolean diff : List.of(false, true, false, true)) {
                ManifestReplaceTestHooks.decline(!diff);
                for (String shape : List.of("same", "tenth_changed", "all_changed")) {
                    List<Map<String, Object>> rows = shape.equals("same") ? same : shape.equals("tenth_changed") ? tenth : all;
                    replaceMs(f, same);                       // reset to the baseline rows
                    long ms = replaceMs(f, rows);
                    System.out.println("MEASURE rows=" + n + " path=" + (diff ? "diff" : "delete+insert")
                        + " shape=" + shape + " ms=" + ms);
                }
            }
            ManifestReplaceTestHooks.decline(false);
        }
    }
}
