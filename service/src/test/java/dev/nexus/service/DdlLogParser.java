// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Counts the DDL in a Postgres server log (nexus-sz651). The Java twin of {@code parse_log} in
 * {@code tests/_ddl_counter.py}; both read {@code ddl/golden-pg-log.txt} and must produce
 * {@code ddl/golden.json} (a Python test keeps the two copies byte-identical). The rule, and why
 * two signals are needed, is in that module's docstring: {@code total = logged + nested}, where
 * {@code logged} counts {@code statement:} / {@code execute ...:} lines
 * ({@code log_statement=ddl}) and {@code nested} counts {@code nx_ddl:} event-trigger lines not
 * claimed by a logged statement on the same backend.
 */
final class DdlLogParser {

    static final String EVENT_PREFIX = "nx_ddl: ";
    static final String MARK_PREFIX = "nx_ddl_mark: ";
    static final String READY_LABEL = "ready";
    static final List<String> KINDS = List.of("create_table", "create_index", "drop", "alter", "other");

    /** One counted command. {@code beforeMark}: logged before the {@code ready} mark. */
    record Entry(String db, String kind, String tag, boolean nested, boolean beforeMark) {}

    record Parsed(List<Entry> entries, List<String> marks) {}

    private static final Pattern LINE = Pattern.compile(
        "^(\\d{4}-\\d\\d-\\d\\d) (\\d\\d:\\d\\d:\\d\\d)(?:\\.(\\d+))? (\\S+) \\[(\\d+)\\] db=(\\S*) ([A-Z]+[0-9]*):  (.*)$");
    private static final Pattern LOGGED = Pattern.compile("^(?:statement|execute [^:]*): (.*)$", Pattern.DOTALL);
    private static final java.util.Set<String> CREATE_MODIFIERS = java.util.Set.of(
        "OR", "REPLACE", "UNIQUE", "TEMP", "TEMPORARY", "UNLOGGED", "GLOBAL", "LOCAL");
    private static final Pattern NO_EVENT = Pattern.compile(
        "^(?:CREATE|ALTER|DROP)\\s+(?:DATABASE|ROLE|USER|GROUP|TABLESPACE|EVENT\\s+TRIGGER)\\b|^ALTER\\s+SYSTEM\\b",
        Pattern.CASE_INSENSITIVE);
    private static final Pattern COMMENT = Pattern.compile("\\A(?:\\s+|--[^\\n]*\\n?|/\\*.*?\\*/)+", Pattern.DOTALL);
    private static final Pattern WORD = Pattern.compile("[A-Za-z_]+");

    private DdlLogParser() {}

    static String head(String sql) {
        Matcher m = COMMENT.matcher(sql);
        String rest = m.find() ? sql.substring(m.end()) : sql;
        int i = 0;
        while (i < rest.length() && Character.isWhitespace(rest.charAt(i))) {
            i++;
        }
        return rest.substring(i);
    }

    private static List<String> words(String text) {
        String h = head(text);
        Matcher m = WORD.matcher(h.length() > 200 ? h.substring(0, 200) : h);
        List<String> out = new ArrayList<>();
        while (m.find()) {
            out.add(m.group().toUpperCase(java.util.Locale.ROOT));
        }
        return out;
    }

    static String classify(String text) {
        List<String> w = words(text);
        if (w.isEmpty()) {
            return "other";
        }
        switch (w.get(0)) {
            case "DROP":
                return "drop";
            case "ALTER":
                return "alter";
            case "CREATE":
                for (String x : w.subList(1, w.size())) {
                    if (CREATE_MODIFIERS.contains(x)) {
                        continue;
                    }
                    return x.equals("TABLE") ? "create_table" : x.equals("INDEX") ? "create_index" : "other";
                }
                return "other";
            default:
                return "other";
        }
    }

    static String tagOf(String text) {
        List<String> w = new ArrayList<>();
        for (String x : words(text)) {
            if (!CREATE_MODIFIERS.contains(x)) {
                w.add(x);
            }
        }
        if (w.isEmpty()) {
            return "UNKNOWN";
        }
        String first = w.get(0);
        if (first.equals("GRANT") || first.equals("REVOKE") || first.equals("COMMENT")) {
            return first;
        }
        return w.size() > 1 ? first + " " + w.get(1) : first;
    }

    static Parsed parse(String log) {
        List<Entry> entries = new ArrayList<>();
        List<String> marks = new ArrayList<>();
        Map<Integer, Boolean> claim = new HashMap<>();
        boolean seenMark = false;
        String[] lines = log.split("\\R", -1);
        for (int i = 0; i < lines.length; i++) {
            Matcher m = LINE.matcher(lines[i]);
            if (!m.matches()) {
                continue;
            }
            String level = m.group(7);
            String msg = m.group(8);
            int pid = Integer.parseInt(m.group(5));
            String db = m.group(6);
            if (level.equals("ERROR") || level.equals("FATAL") || level.equals("PANIC")) {
                claim.remove(pid);
                continue;
            }
            if (!level.equals("LOG")) {
                continue;
            }
            if (msg.startsWith(MARK_PREFIX)) {
                String label = msg.substring(MARK_PREFIX.length()).strip();
                marks.add(label);
                seenMark = seenMark || label.equals(READY_LABEL);
                continue;
            }
            boolean before = !seenMark;
            if (msg.startsWith(EVENT_PREFIX)) {
                String tag = msg.substring(EVENT_PREFIX.length()).strip();
                if (claim.remove(pid) != null) {
                    continue; // the command the client sent; already counted from its statement line
                }
                entries.add(new Entry(db, classify(tag), tagOf(tag), true, before));
                continue;
            }
            Matcher logged = LOGGED.matcher(msg);
            if (!logged.matches()) {
                continue;
            }
            String sql = logged.group(1);
            if (head(sql).isEmpty()) {
                // The statement starts on a later line (psql -c with a leading newline, or a
                // changeset whose first line is a comment).
                StringBuilder sb = new StringBuilder(sql);
                for (int j = i + 1; j < Math.min(lines.length, i + 61) && lines[j].startsWith("\t"); j++) {
                    sb.append('\n').append(lines[j], 1, lines[j].length());
                }
                sql = sb.toString();
            }
            if (NO_EVENT.matcher(head(sql)).lookingAt()) {
                claim.remove(pid);
            } else {
                claim.put(pid, Boolean.TRUE);
            }
            entries.add(new Entry(db, classify(sql), tagOf(sql), false, before));
        }
        return new Parsed(entries, marks);
    }

    /** {@code total/toplevel/nested/by_kind}, the same shape the Python tally writes. */
    static Map<String, Object> tally(Iterable<Entry> entries) {
        long total = 0;
        long nested = 0;
        Map<String, Long> byKind = new LinkedHashMap<>();
        KINDS.forEach(k -> byKind.put(k, 0L));
        for (Entry e : entries) {
            total++;
            if (e.nested()) {
                nested++;
            }
            byKind.merge(e.kind(), 1L, Long::sum);
        }
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("total", total);
        out.put("toplevel", total - nested);
        out.put("nested", nested);
        out.put("by_kind", byKind);
        return out;
    }
}
