// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.log;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * nexus-dngtz.1.11: logback makes every class a {@code <conversionRule>} names by reflection, and the engine is a native
 * image whose reflection set is the hand-listed {@code reflect-config.json}. A converter missing there is found only by a
 * native build, and it would break every log line, so this reads both files and checks each rule has an entry with its
 * constructors. It cannot prove the native image works; it stops the entry being forgotten.
 */
class LogbackNativeConfigTest {

    private static String resource(String path) throws IOException {
        try (var in = LogbackNativeConfigTest.class.getResourceAsStream(path)) {
            assertThat(in).as(path).isNotNull();
            return new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
    }

    @Test
    void everyConversionRuleClassInLogbackXmlHasAConstructorEntryInTheNativeReflectConfig() throws IOException {
        String logback = resource("/logback.xml");
        String reflect = resource("/META-INF/native-image/reflect-config.json");
        List<String> classes = new ArrayList<>();
        Matcher m = Pattern.compile("<conversionRule[^>]*class=\"([^\"]+)\"").matcher(logback);
        while (m.find()) {
            classes.add(m.group(1));
        }
        assertThat(classes).as("non-vacuity: the shipped config names the three safe converters")
            .containsExactlyInAnyOrder("dev.nexus.service.log.SafeMessageConverter", "dev.nexus.service.log.SafeThrowableConverter",
                "dev.nexus.service.log.SafeThreadConverter");
        for (String cls : classes) {
            Matcher entry = Pattern.compile("\\{[^{}]*\"name\":\\s*\"" + Pattern.quote(cls) + "\"[^{}]*\\}").matcher(reflect);
            assertThat(entry.find()).as("reflect-config.json has an entry for %s", cls).isTrue();
            assertThat(entry.group()).as("%s needs its constructors", cls).contains("\"allDeclaredConstructors\": true");
        }
    }
}
