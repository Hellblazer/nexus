// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeSet;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * The data moves the product changelog registers at boot, read from the master changelog itself, so a test that needs "every
 * registering include" or "every registered label" derives it and a new registration cannot silently break it.
 *
 * <p>A <em>registering include</em> is an {@code <include>} of the master whose file holds a top-level
 * {@code INSERT INTO nexus.data_moves (...) VALUES ('<label>', ...}. "Top-level" matters: a move's {@code on_complete} holds
 * the INSERT of a chained move (catalog-049-2 registers {@code rdr229-residue-catalog-documents}, whose on_complete inserts
 * {@code rdr229-residue-document-aspects} when the first finishes), and that second one is registered by the worker, not at
 * boot. The scan therefore drops XML comments and every dollar-quoted body ({@code $t$...$t$}, {@code $oc$...$oc$}) first.
 */
public final class ShippedMoveRegistrations {

    private static final String MASTER = "db/changelog/db.changelog-master.xml";
    private static final Pattern COMMENT = Pattern.compile("<!--.*?-->", Pattern.DOTALL);
    private static final Pattern INCLUDE = Pattern.compile("<include\\s+file=\"([^\"]+)\"\\s*/>");
    private static final Pattern DOLLAR_QUOTED = Pattern.compile("\\$(\\w*)\\$.*?\\$\\1\\$", Pattern.DOTALL);
    private static final Pattern REGISTER = Pattern.compile(
        "INSERT\\s+INTO\\s+nexus\\.data_moves\\s*\\([^)]*\\)\\s*VALUES\\s*\\(\\s*'([^']+)'", Pattern.CASE_INSENSITIVE);

    private ShippedMoveRegistrations() {}

    /** Every master include (path as the master spells it) that registers a move, with the labels it registers, in master order. */
    public static Map<String, List<String>> byInclude() {
        Map<String, List<String>> out = new LinkedHashMap<>();
        Matcher inc = INCLUDE.matcher(COMMENT.matcher(read(MASTER)).replaceAll(""));
        int includes = 0;
        while (inc.find()) {
            includes++;
            String code = DOLLAR_QUOTED.matcher(COMMENT.matcher(read(inc.group(1))).replaceAll("")).replaceAll("");
            Matcher reg = REGISTER.matcher(code);
            List<String> labels = new ArrayList<>();
            while (reg.find()) {
                labels.add(reg.group(1));
            }
            if (!labels.isEmpty()) {
                out.put(inc.group(1), labels);
            }
        }
        assertThat(includes).as("the scan read the master's includes").isGreaterThan(100);
        assertThat(out).as("the scan found registering includes (a scan that found none read nothing)").isNotEmpty();
        return out;
    }

    /** Every label the product changelog registers at boot. */
    public static TreeSet<String> labels() {
        TreeSet<String> all = new TreeSet<>();
        byInclude().values().forEach(all::addAll);
        return all;
    }

    private static String read(String resource) {
        try (InputStream in = ShippedMoveRegistrations.class.getClassLoader().getResourceAsStream(resource)) {
            assertThat(in).as(resource).isNotNull();
            return new String(in.readAllBytes(), StandardCharsets.UTF_8);
        } catch (IOException e) {
            throw new IllegalStateException(resource, e);
        }
    }
}
