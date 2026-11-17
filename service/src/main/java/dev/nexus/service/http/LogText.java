// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

/**
 * Makes client-supplied text safe to write into one log line (nexus-dngtz.1.8, RDR-167 Step 3).
 * {@code HttpExchange.getRequestURI().getPath()} is the decoded path, so a request to
 * {@code /v1/x%0Aevent=forged} would otherwise put a real newline into the line and forge a second one.
 *
 * <p>The escape is injective: a backslash is doubled, so typed {@code \x0a} text cannot pass for the
 * escape of a newline. Use it only inside the log call; routing and comparisons keep the raw text.
 */
public final class LogText {

    private LogText() {
    }

    /**
     * A backslash becomes two backslashes; a control char (C0, 0x7F, C1 0x80 to 0x9F), U+2028 or U+2029 becomes
     * {@code \xNN} (lowercase hex) when below 0x100, else a backslash, {@code u} and four hex digits; {@code null} becomes
     * {@code -}. Returns the same instance when nothing needs escaping.
     */
    static String escapeControl(String s) {
        return escape(s, false);
    }

    /**
     * For a value printed inside the log prefix {@code [cid=... tenant=... session=...]} (nexus-dngtz.1.11):
     * {@link #escapeControl} plus a space, {@code [}, {@code ]} and {@code =}, each as {@code \xNN}, so a
     * client-chosen session id cannot end the prefix early or add fields to it. A backslash is still doubled.
     * Returns the same instance when nothing needs escaping; {@code null} becomes {@code -}.
     */
    public static String mdcValue(String s) {
        return escape(s, true);
    }

    private static String escape(String s, boolean prefixDelimiters) {
        if (s == null) {
            return "-";
        }
        int n = s.length();
        int first = 0;
        while (first < n && !needsEscape(s.charAt(first), prefixDelimiters)) {
            first++;
        }
        if (first == n) {
            return s;
        }
        StringBuilder out = new StringBuilder(n + 8).append(s, 0, first);
        for (int i = first; i < n; i++) {
            char c = s.charAt(i);
            if (c == '\\') {
                out.append("\\\\");
            } else if (needsEscape(c, prefixDelimiters)) {
                out.append(c < 0x100 ? "\\x" : "\\u");
                String hex = Integer.toHexString(c);
                for (int pad = (c < 0x100 ? 2 : 4) - hex.length(); pad > 0; pad--) {
                    out.append('0');
                }
                out.append(hex);
            } else {
                out.append(c);
            }
        }
        return out.toString();
    }

    private static boolean needsEscape(char c, boolean prefixDelimiters) {
        if (prefixDelimiters && (c == ' ' || c == '[' || c == ']' || c == '=')) {
            return true;
        }
        return c == '\\' || c == 0x2028 || c == 0x2029 || Character.getType(c) == Character.CONTROL;
    }
}
