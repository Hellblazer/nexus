// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.function.Function;

/**
 * The rows of one collection-import batch whose label the P3.6 rule refuses (RDR-229 item 10: a label problem on
 * import "refuses that collection; the rest of the bundle imports"). One instance per attempt of one batch. A row is
 * refused by its label, so a name repeated in the batch is one refusal; the engine's own minted-form clause is not
 * applied on import ({@link CollectionLabel#shapeBreach}), because an import carries another install's labels.
 */
public final class ImportLabelRefusals {

    /**
     * One refused label: the label as sent, the rule it broke (a {@code CollectionLabel} rule name) and why. A lone
     * surrogate in the label or the reason is written as its {@code \\uXXXX} escape, because the response cannot carry
     * it (it would encode as {@code ?}, and two different labels would read alike).
     */
    public record Refusal(String name, String rule, String reason) {
    }

    private final Map<String, Refusal> byName = new LinkedHashMap<>();

    /**
     * The rows whose label the database can store ({@link CollectionLabel#storableBreach}); the others are refused.
     * Runs before the first read that binds a name.
     */
    List<Map<String, Object>> keepStorable(List<Map<String, Object>> rows, Function<Map<String, Object>, String> name) {
        return keep(rows, name, CollectionLabel::storableBreach);
    }

    /** The rows whose label meets the rule's shape ({@link CollectionLabel#shapeBreach}); the others are refused. */
    List<Map<String, Object>> keepShaped(List<Map<String, Object>> rows, Function<Map<String, Object>, String> name) {
        return keep(rows, name, CollectionLabel::shapeBreach);
    }

    /**
     * The rows whose label is not reserved ({@link LabelReserved}); the others are refused with the rule {@code label_reserved}. Runs on the rows that
     * would be INSERTed (an existing registration is a no-op and is not judged).
     */
    List<Map<String, Object>> keepUnreserved(List<Map<String, Object>> rows, Function<Map<String, Object>, String> name,
                                             java.util.function.Predicate<String> reserved) {
        var kept = new ArrayList<Map<String, Object>>(rows.size());
        for (var row : rows) {
            String label = name.apply(row);
            if (reserved.test(label)) {
                byName.put(label, new Refusal(printable(label), "label_reserved", printable(new LabelReservedException(label).getMessage())));
            } else {
                kept.add(row);
            }
        }
        return kept;
    }

    private List<Map<String, Object>> keep(List<Map<String, Object>> rows, Function<Map<String, Object>, String> name,
                                           Function<String, java.util.Optional<CollectionLabel.Breach>> check) {
        var kept = new ArrayList<Map<String, Object>>(rows.size());
        for (var row : rows) {
            String label = name.apply(row);
            var breach = check.apply(label);
            if (breach.isEmpty()) {
                kept.add(row);
            } else {
                byName.put(label, new Refusal(printable(label == null ? "" : label), breach.get().rule(),
                    printable(breach.get().reason())));
            }
        }
        return kept;
    }

    private static String printable(String text) {
        var out = new StringBuilder(text.length());
        for (int i = 0; i < text.length(); i++) {
            char ch = text.charAt(i);
            boolean paired = Character.isHighSurrogate(ch) && i + 1 < text.length()
                && Character.isLowSurrogate(text.charAt(i + 1));
            if (paired) {
                out.append(ch).append(text.charAt(++i));
            } else if (Character.isSurrogate(ch)) {
                out.append(String.format("\\u%04X", (int) ch));
            } else {
                out.append(ch);
            }
        }
        return out.toString();
    }

    /** Whether a row sent under {@code label} was refused. */
    boolean refuses(String label) {
        return byName.containsKey(label);
    }

    /** The refusals in the order their labels were first sent. */
    List<Refusal> inSentOrder(List<String> sentLabels) {
        var out = new ArrayList<Refusal>(byName.size());
        for (String label : new java.util.LinkedHashSet<>(sentLabels)) {
            var refusal = byName.get(label);
            if (refusal != null) out.add(refusal);
        }
        return List.copyOf(out);
    }
}
