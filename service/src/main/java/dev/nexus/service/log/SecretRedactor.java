// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.log;

import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.Collection;
import java.util.Comparator;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/**
 * Replaces credentials in a text with {@value #REDACTED}. Built once from the secrets, immutable, and thread-safe.
 *
 * <p>Each secret is matched case-insensitively in the forms a driver or a server echoes it: as is, with a quote doubled
 * ({@code ''}, {@code ""}), with a quote backslash-escaped, and URL-encoded (a space as {@code +} or {@code %20}; the hex
 * case does not matter). A blank secret is ignored. A secret of any other length is redacted, short ones included: over-
 * redaction mangles a diagnostic and under-redaction leaks the credential, so a short secret errs on the safe side.
 *
 * <p>The scan is a plain {@link String#regionMatches} walk over the text with the longest form first, so it compiles no
 * regex and allocates nothing for a text that holds no secret (the common case on a log line). The matches are cut out
 * of the whole text, so call it before the text is truncated: a secret that straddles a cut leaves no prefix behind.
 */
public final class SecretRedactor {

    /** What a secret becomes. */
    public static final String REDACTED = "<redacted>";

    /** A redactor that changes nothing. */
    public static final SecretRedactor NONE = new SecretRedactor(List.of());

    private final String[] forms;
    /** The lower and upper case of each form's first character, to skip a position without calling regionMatches. */
    private final char[] lower;
    private final char[] upper;

    public SecretRedactor(Collection<String> secrets) {
        Set<String> all = new LinkedHashSet<>();
        for (String secret : secrets) {
            if (secret == null || secret.isBlank()) {
                continue;
            }
            all.add(secret);
            all.add(secret.replace("'", "''"));
            all.add(secret.replace("\"", "\"\""));
            all.add(secret.replace("'", "\\'"));
            all.add(secret.replace("\"", "\\\""));
            String encoded = URLEncoder.encode(secret, StandardCharsets.UTF_8);
            all.add(encoded);
            all.add(encoded.replace("+", "%20"));
        }
        String[] sorted = all.stream().filter(s -> !s.isEmpty())
            .sorted(Comparator.comparingInt(String::length).reversed().thenComparing(Comparator.naturalOrder()))
            .toArray(String[]::new);
        this.forms = sorted;
        this.lower = new char[sorted.length];
        this.upper = new char[sorted.length];
        for (int i = 0; i < sorted.length; i++) {
            lower[i] = Character.toLowerCase(sorted[i].charAt(0));
            upper[i] = Character.toUpperCase(sorted[i].charAt(0));
        }
    }

    /** Whether this redactor has anything to redact. */
    public boolean active() {
        return forms.length > 0;
    }

    /** {@code text} with every secret replaced; the same instance when it holds none. */
    public String redact(String text) {
        if (text == null || forms.length == 0 || text.isEmpty()) {
            return text;
        }
        int n = text.length();
        StringBuilder out = null;
        int copied = 0;
        int i = 0;
        while (i < n) {
            char c = text.charAt(i);
            int hit = 0;
            for (int f = 0; f < forms.length; f++) {
                if ((c == lower[f] || c == upper[f] || Character.toLowerCase(c) == lower[f])
                        && forms[f].length() <= n - i && text.regionMatches(true, i, forms[f], 0, forms[f].length())) {
                    hit = forms[f].length();
                    break;
                }
            }
            if (hit == 0) {
                i++;
                continue;
            }
            if (out == null) {
                out = new StringBuilder(n + 16);
            }
            out.append(text, copied, i).append(REDACTED);
            i += hit;
            copied = i;
        }
        if (out == null) {
            return text;
        }
        return out.append(text, copied, n).toString();
    }
}
