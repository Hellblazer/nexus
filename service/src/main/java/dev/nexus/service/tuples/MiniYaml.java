// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.tuples;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * A deliberately small YAML subset parser for RDR-205 tuple templates
 * (bead nexus-em75s.3, design call: no {@code jackson-dataformat-yaml}
 * dependency for two fixed, flat templates — see the class's own module,
 * {@link TemplateRegistry}).
 *
 * <p>Supports exactly what the RDR-205 document shape needs: block
 * mappings, block sequences of scalars, flow sequences ({@code [a, b, c]}),
 * and scalar values (string / integer / boolean). No anchors, no
 * multi-line scalars, no flow mappings, no quote-escaping beyond a single
 * layer of surrounding quotes, and no inline comments — a comment is a
 * whole line whose first non-blank character is {@code #}. This is not a
 * general YAML parser and is not meant to become one: the two v1 templates
 * and whatever the test-only {@code NX_TUPLE_TEMPLATE_DIR} fixtures declare
 * are the only documents it will ever read.
 */
final class MiniYaml {

    private MiniYaml() {
    }

    private record Line(int indent, String content, int number) {
    }

    /** Parses {@code text} (attributed to {@code source} in error messages) into a root mapping. */
    @SuppressWarnings("unchecked")
    static Map<String, Object> parse(String source, String text) {
        List<Line> lines = tokenize(source, text);
        if (lines.isEmpty()) {
            return new LinkedHashMap<>();
        }
        int[] idx = {0};
        Object root = parseBlock(source, lines, idx, lines.get(0).indent());
        if (idx[0] != lines.size()) {
            Line stray = lines.get(idx[0]);
            throw syntaxError(source, stray.number(),
                    "unexpected indentation (expected " + lines.get(0).indent() + ", found " + stray.indent() + ")");
        }
        if (!(root instanceof Map)) {
            throw syntaxError(source, lines.get(0).number(), "document root must be a mapping");
        }
        return (Map<String, Object>) root;
    }

    private static List<Line> tokenize(String source, String text) {
        List<Line> lines = new ArrayList<>();
        String[] raw = text.split("\n", -1);
        for (int i = 0; i < raw.length; i++) {
            String line = raw[i];
            if (line.endsWith("\r")) {
                line = line.substring(0, line.length() - 1);
            }
            if (line.indexOf('\t') >= 0) {
                throw syntaxError(source, i + 1, "tabs are not supported; use spaces for indentation");
            }
            int indent = 0;
            while (indent < line.length() && line.charAt(indent) == ' ') {
                indent++;
            }
            String content = line.substring(indent).stripTrailing();
            if (content.isEmpty() || content.startsWith("#")) {
                continue;
            }
            lines.add(new Line(indent, content, i + 1));
        }
        return lines;
    }

    private static Object parseBlock(String source, List<Line> lines, int[] idx, int indent) {
        Line first = lines.get(idx[0]);
        if (first.indent() != indent) {
            throw syntaxError(source, first.number(),
                    "unexpected indentation (expected " + indent + ", found " + first.indent() + ")");
        }
        if (isSequenceItem(first.content())) {
            return parseSequence(source, lines, idx, indent);
        }
        return parseMapping(source, lines, idx, indent);
    }

    private static boolean isSequenceItem(String content) {
        return content.equals("-") || content.startsWith("- ");
    }

    private static List<Object> parseSequence(String source, List<Line> lines, int[] idx, int indent) {
        List<Object> out = new ArrayList<>();
        while (idx[0] < lines.size()) {
            Line l = lines.get(idx[0]);
            if (l.indent() != indent || !isSequenceItem(l.content())) {
                break;
            }
            String rest = l.content().equals("-") ? "" : l.content().substring(2).trim();
            idx[0]++;
            if (rest.isEmpty()) {
                throw syntaxError(source, l.number(), "empty sequence item is not supported");
            }
            out.add(parseScalarOrFlow(source, rest, l.number()));
        }
        return out;
    }

    private static Map<String, Object> parseMapping(String source, List<Line> lines, int[] idx, int indent) {
        Map<String, Object> out = new LinkedHashMap<>();
        while (idx[0] < lines.size()) {
            Line l = lines.get(idx[0]);
            if (l.indent() != indent || isSequenceItem(l.content())) {
                break;
            }
            int sep = findKeySeparator(l.content());
            if (sep < 0) {
                throw syntaxError(source, l.number(), "expected 'key: value' or 'key:', found: " + l.content());
            }
            String key = l.content().substring(0, sep).trim();
            String rest = l.content().substring(sep + 1).trim();
            if (key.isEmpty()) {
                throw syntaxError(source, l.number(), "empty mapping key");
            }
            if (out.containsKey(key)) {
                throw syntaxError(source, l.number(), "duplicate mapping key '" + key + "'");
            }
            idx[0]++;
            if (!rest.isEmpty()) {
                out.put(key, parseScalarOrFlow(source, rest, l.number()));
                continue;
            }
            if (idx[0] < lines.size() && lines.get(idx[0]).indent() > indent) {
                out.put(key, parseBlock(source, lines, idx, lines.get(idx[0]).indent()));
            } else {
                out.put(key, null);
            }
        }
        return out;
    }

    /** A {@code key:} separator is {@code ": "}, or a trailing bare {@code ":"} with nothing after it. */
    private static int findKeySeparator(String content) {
        int i = content.indexOf(": ");
        if (i >= 0) {
            return i;
        }
        if (content.endsWith(":")) {
            return content.length() - 1;
        }
        return -1;
    }

    private static Object parseScalarOrFlow(String source, String s, int lineNumber) {
        if (s.startsWith("[")) {
            return parseFlowSequence(source, s, lineNumber);
        }
        return parseScalar(s);
    }

    private static List<Object> parseFlowSequence(String source, String s, int lineNumber) {
        if (!s.endsWith("]")) {
            throw syntaxError(source, lineNumber, "unterminated flow sequence: " + s);
        }
        String inner = s.substring(1, s.length() - 1).trim();
        List<Object> out = new ArrayList<>();
        if (inner.isEmpty()) {
            return out;
        }
        for (String part : inner.split(",")) {
            out.add(parseScalar(part.trim()));
        }
        return out;
    }

    private static Object parseScalar(String s) {
        if (s.length() >= 2
                && ((s.startsWith("\"") && s.endsWith("\"")) || (s.startsWith("'") && s.endsWith("'")))) {
            return s.substring(1, s.length() - 1);
        }
        if (s.equals("true")) {
            return Boolean.TRUE;
        }
        if (s.equals("false")) {
            return Boolean.FALSE;
        }
        if (s.matches("-?\\d+")) {
            return Long.parseLong(s);
        }
        return s;
    }

    private static TemplateRegistryException syntaxError(String source, int lineNumber, String detail) {
        return new TemplateRegistryException(source, "yaml", "line " + lineNumber + ": " + detail);
    }
}
