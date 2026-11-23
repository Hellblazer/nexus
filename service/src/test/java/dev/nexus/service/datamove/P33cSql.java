// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import java.io.IOException;
import java.io.InputStream;
import java.util.Properties;

/**
 * The statements of the per-leaf FK tests (nexus-p2m1x.21) that have no typed jOOQ form, read from {@code datamove/p33c-statements.properties}
 * so the test sources hold no SQL literal (SqlStringLiteralGateTest). {@link #stmt} fills a template with {@link String#format}; the values are
 * identifiers and numbers the test built, never user input.
 */
final class P33cSql {

    private static final Properties STATEMENTS = load();

    private P33cSql() { /* static */ }

    private static Properties load() {
        Properties p = new Properties();
        try (InputStream in = P33cSql.class.getClassLoader().getResourceAsStream("datamove/p33c-statements.properties")) {
            if (in == null) {
                throw new IllegalStateException("datamove/p33c-statements.properties is not on the test classpath");
            }
            p.load(in);
        } catch (IOException e) {
            throw new IllegalStateException(e);
        }
        return p;
    }

    /** The statement {@code key} names, with its {@code %s} filled by {@code args}. */
    static String stmt(String key, Object... args) {
        String template = STATEMENTS.getProperty(key);
        if (template == null) {
            throw new IllegalArgumentException("no statement named " + key);
        }
        return String.format(template, args);
    }
}
