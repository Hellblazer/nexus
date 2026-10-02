// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

/**
 * Makes tenant- and client-controlled text safe to put inside the {@code ownerless_chunk_write_*}
 * WARN line (RDR-223 Phase 3 Step 2, nexus-z0o2p.24). The line is one {@code key=value} record that
 * an operator greps and a script splits, so a value must not be able to end the record, close its
 * own quotes or brackets, or start a field of its own. Four inputs reach it: the collection name
 * (any text a tenant registered; the engine does not validate it), the {@code User-Agent} and
 * {@code X-Nexus-Client-Version} headers, and the metadata values of the first unowned chunk. The
 * tenant, route, phase and sample are the engine's own (token-derived, fixed, canonical hex).
 *
 * <p>Two layers. {@link #plain} removes everything that can break a line or fool a reader: control
 * characters (ESC, NUL, CR, LF), the Unicode line and paragraph separators (U+2028, U+2029, U+0085),
 * format characters (bidi overrides, zero-width), every kind of space (collapsed to one ASCII space),
 * and unpaired surrogates or unassigned code points. The delimiter layers, {@link #quoted} and
 * {@link #metaValue}, then neutralise the delimiter the value sits inside.
 */
final class OwnerlessLogText {

    private OwnerlessLogText() {
    }

    /** True when the code point may appear in a log value as itself. */
    private static boolean isPlain(int cp) {
        if (cp == ' ') {
            return false;   // spaces are collapsed by the caller, never copied
        }
        return switch (Character.getType(cp)) {
            case Character.CONTROL, Character.FORMAT, Character.LINE_SEPARATOR,
                 Character.PARAGRAPH_SEPARATOR, Character.SPACE_SEPARATOR, Character.SURROGATE,
                 Character.UNASSIGNED, Character.PRIVATE_USE -> false;
            default -> true;
        };
    }

    /**
     * {@code value} with every non-plain code point replaced by one space, runs of spaces collapsed,
     * ends trimmed, and the result cut to at most {@code max} code points. Never null.
     */
    static String plain(String value, int max) {
        if (value == null) {
            return "";
        }
        StringBuilder out = new StringBuilder(Math.min(value.length(), max + 4));
        boolean pendingSpace = false;
        int copied = 0;
        for (int i = 0; i < value.length() && copied < max; ) {
            int cp = value.codePointAt(i);
            i += Character.charCount(cp);
            if (!isPlain(cp)) {
                pendingSpace = out.length() > 0;
                continue;
            }
            if (pendingSpace) {
                out.append(' ');
                copied++;
                pendingSpace = false;
                if (copied >= max) {
                    break;
                }
            }
            out.appendCodePoint(cp);
            copied++;
        }
        return out.toString();
    }

    /**
     * A value the caller wraps in double quotes: {@link #plain}, with {@code "} and {@code \} turned
     * into {@code '} and {@code /} so it cannot close the quote or escape the closing one. Blank
     * becomes {@code "absent"}.
     */
    static String quoted(String value, int max) {
        String text = plain(value, max).replace('"', '\'').replace('\\', '/');
        return text.isEmpty() ? "absent" : text;
    }

    private static final java.util.regex.Pattern URL_WITH_USERINFO =
        java.util.regex.Pattern.compile("^([a-zA-Z][a-zA-Z0-9+.-]*://)[^/?#\\s]*@");
    private static final java.util.regex.Pattern URL_SCHEME =
        java.util.regex.Pattern.compile("^[a-zA-Z][a-zA-Z0-9+.-]*://");

    /**
     * A metadata value that is a URL loses its userinfo ({@code https://user:token@host/} becomes
     * {@code https://REDACTED@host/}) and its query string and fragment. The engine does not know
     * what a client puts in {@code source_path} or {@code title}: no current client path was found
     * writing a credentialed URL there (chunk metadata has carried no {@code source_path} since
     * RDR-102 D2, and a title is a document title), but a web-ingested document or a hand-written
     * {@code store_put} can, and the log line outlives the request.
     */
    static String redactUrl(String value) {
        if (value == null) {
            return null;
        }
        String v = value.strip();
        if (!URL_SCHEME.matcher(v).find()) {
            return value;
        }
        var m = URL_WITH_USERINFO.matcher(v);
        if (m.find()) {
            v = m.group(1) + "REDACTED@" + v.substring(m.end());
        }
        int cut = -1;
        for (int i = 0; i < v.length(); i++) {
            char c = v.charAt(i);
            if (c == '?' || c == '#') {
                cut = i;
                break;
            }
        }
        return cut < 0 ? v : v.substring(0, cut) + "?REDACTED";
    }

    /**
     * A metadata value inside {@code first_chunk_meta=[key=value;key=value;]}: {@link #plain}, with
     * the three delimiters of that notation ({@code ;} {@code =} and the bracket pair) and the quote
     * characters turned into look-alikes that carry no meaning in the line, so a value cannot end its
     * pair, start a pair of its own, or close the bracket.
     */
    static String metaValue(String value, int max) {
        return plain(redactUrl(value), max)
            .replace(';', ',').replace('=', ':')
            .replace('[', '(').replace(']', ')')
            .replace('"', '\'').replace('\\', '/');
    }
}
