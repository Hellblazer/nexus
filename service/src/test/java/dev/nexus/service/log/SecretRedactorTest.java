// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.log;

import static org.assertj.core.api.Assertions.assertThat;

import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.slf4j.LoggerFactory;

import java.util.List;

/**
 * nexus-f3yqd.2.18: what {@link SecretRedactor} matches and what {@link SecretMask} does with a secret of any length. Pure:
 * no engine, no database.
 */
class SecretRedactorTest {

    private static final String PW = "Hunter2-s3cret";

    @AfterEach
    void forgetTheSecrets() {
        SecretMask.reset();
    }

    private static String redact(String secret, String text) {
        return new SecretRedactor(List.of(secret)).redact(text);
    }

    @Test
    void theRawSecret_isRedacted_whateverTheCase() {
        assertThat(redact(PW, "FATAL: password authentication failed (password=" + PW + ")"))
            .isEqualTo("FATAL: password authentication failed (password=<redacted>)");
        assertThat(redact(PW, "echo " + PW.toUpperCase() + " and " + PW.toLowerCase()))
            .isEqualTo("echo <redacted> and <redacted>");
    }

    @Test
    void aQuotedEcho_aQuoteDoubledEcho_anEscapedEcho_andAUrlEncodedEcho_areAllRedacted() {
        String secret = "it's \"p@ss w/rd\"";
        assertThat(redact(secret, "quoted '" + secret + "'")).isEqualTo("quoted '<redacted>'");
        assertThat(redact(secret, "single quote doubled it''s \"p@ss w/rd\" end")).isEqualTo("single quote doubled <redacted> end");
        assertThat(redact(secret, "double quote doubled it's \"\"p@ss w/rd\"\" end")).isEqualTo("double quote doubled <redacted> end");
        assertThat(redact(secret, "single quote escaped it\\'s \"p@ss w/rd\" end")).isEqualTo("single quote escaped <redacted> end");
        assertThat(redact(secret, "double quote escaped it's \\\"p@ss w/rd\\\" end")).isEqualTo("double quote escaped <redacted> end");
        assertThat(redact(secret, "url=jdbc:postgresql://db/n?password=it%27s+%22p%40ss+w%2Frd%22&x=1"))
            .isEqualTo("url=jdbc:postgresql://db/n?password=<redacted>&x=1");
        assertThat(redact(secret, "url=jdbc:postgresql://db/n?password=it%27s%20%22p%40ss%20w%2frd%22&x=1"))
            .as("%20 for a space, and lower-case hex")
            .isEqualTo("url=jdbc:postgresql://db/n?password=<redacted>&x=1");
    }

    @Test
    void everyOccurrenceIsRedacted_andTheLongestFormWins() {
        assertThat(redact("ab12", "ab12 and AB12 and ab12ab12")).isEqualTo("<redacted> and <redacted> and <redacted><redacted>");
        var both = new SecretRedactor(List.of("secret", "secret-longer"));
        assertThat(both.redact("a secret-longer b secret")).isEqualTo("a <redacted> b <redacted>");
    }

    @Test
    void aTextWithNoSecret_comesBackAsTheSameInstance_soAHotLogLineAllocatesNothingHere() {
        var redactor = new SecretRedactor(List.of(PW));
        String line = "2026-11-17 INFO event=data_move_call label=\"m\" run=\"m/acme\" pid=42";

        assertThat(redactor.redact(line)).isSameAs(line);
        assertThat(redactor.redact(null)).isNull();
        assertThat(redactor.redact("")).isEmpty();
    }

    @Test
    void aBlankOrNullSecret_isNoOp() {
        for (String blank : new String[] {null, "", " ", "   ", "\t\n"}) {
            var redactor = new SecretRedactor(java.util.Arrays.asList(blank));
            assertThat(redactor.active()).as("blank secret %s", blank == null ? "null" : "'" + blank.strip() + "'").isFalse();
            assertThat(redactor.redact("a   b \t c")).isEqualTo("a   b \t c");
        }
        assertThat(SecretRedactor.NONE.redact("anything")).isEqualTo("anything");
    }

    @Test
    void aShortSecret_isRedactedNotSkipped_evenWhereItMasksUnrelatedText() {
        assertThat(redact("abc", "pass=abc and cabcd")).isEqualTo("pass=<redacted> and c<redacted>d");
        assertThat(redact("x", "box")).as("a one-character secret too: the safe side is to over-redact")
            .isEqualTo("bo<redacted>");
    }

    @Test
    void secretMask_warnsOnceWhenASecretIsUnderFourCharacters_andStillRedactsIt() {
        Logger logger = (Logger) LoggerFactory.getLogger(SecretMask.class);
        ListAppender<ILoggingEvent> logs = new ListAppender<>();
        logs.start();
        logger.addAppender(logs);
        try {
            SecretMask.add("abc");
            SecretMask.add("abc");   // already added: no second warning
            SecretMask.add("a-long-enough-secret");   // not short: none
            SecretMask.add("  ");    // blank: nothing at all
            SecretMask.add(null);
        } finally {
            logger.detachAppender(logs);
            logs.stop();
        }

        assertThat(logs.list).hasSize(1);
        assertThat(logs.list.get(0).getFormattedMessage()).contains("event=secret_mask_short_secret").contains("length=3")
            .contains("unrelated text").doesNotContain("abc");
        assertThat(SecretMask.mask("is abc in a-long-enough-secret?")).isEqualTo("is <redacted> in <redacted>?");
    }
}
