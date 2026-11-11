// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Pattern;
import java.util.stream.Stream;

/**
 * nexus-f3yqd, engine 0.1.190, Sam's rule (2026-11-11): the chash VALIDATE CONSTRAINT runs only as a job a user starts, never
 * at boot, never on a schedule. {@code Main} binds the validator (so the route can run it) and {@code NexusService} holds it, so
 * the rule would be prose only; this gate makes it fail: in {@code src/main}, the only code that calls
 * {@code ChashConstraintValidator.run} is {@code ValidateConstraintsHandler}, from the job a POST starts.
 *
 * <p><b>Limits.</b> It is textual, so it does not follow a validator passed as a method argument ({@code hook(chashConstraintValidator)})
 * or reached through a call chain ({@code holder.get().run(d, l)}). The files that may name the validator are few and listed,
 * and a change that adds a user of it lands on this list in review; the gate catches the plain and the aliased calls, not a
 * determined indirection. It is not extended further: a regex arms race against indirection would only pretend to be airtight.
 *
 * <p>The scan is by text over every main source: a file that names the validator (the class, or a field or variable of it)
 * must be on {@link #MAY_MENTION}, and only the handler may call {@code run} on one.
 */
class ChashValidateNeverAtBootGateTest {

    private static final Path MAIN = Path.of("src", "main", "java");
    private static final String HANDLER = "ValidateConstraintsHandler.java";

    /** Files that may name the validator, each with why; only {@link #HANDLER} may call its {@code run}. */
    private static final java.util.Map<String, String> MAY_MENTION = java.util.Map.of(
        "ChashConstraintValidator.java", "the class itself",
        HANDLER, "the route: its POST starts the job that calls run",
        "ValidateConstraintsJobs.java", "the job's listener type and progress; it never constructs or runs a validator",
        "NexusService.java", "holds the validator Main binds and hands it to the handler; cancelRunning at shutdown, never run",
        "Main.java", "binds the validator once at boot (binding runs nothing)",
        "SchemaMigrator.java", "names the validator's application_name prefix so a walk can end its backends; never runs it");

    /** A call of {@code run} on something named like the validator, or a method reference to it. */
    private static final Pattern RUN_ON_VALIDATOR =
        Pattern.compile("(?is)((chashConstraintValidator\\w*|validator\\w*|ChashConstraintValidator[\\w.]*)\\s*(\\(\\s*\\))?"
            + "|new\\s+[\\w.]*ChashConstraintValidator\\s*\\([^;]*?\\))\\s*(\\.\\s*run\\s*\\(|::\\s*run\\b)");

    /**
     * The shapes that evade {@link #RUN_ON_VALIDATOR} by an alias, which no file but the handler and the class may have: a
     * {@code run(} whose first argument is a dry-run boolean or whose arguments name a Listener (the validator's
     * {@code run(boolean, Listener)}), and any assignment whose right-hand side is the validator (a local or field alias).
     */
    private static final Pattern RUN_SHAPE = Pattern.compile(
        "(?is)\\.\\s*run\\s*\\(\\s*(true|false|\\w*dry\\w*)\\s*,|\\.\\s*run\\s*\\([^;]*listener");
    private static final Pattern ALIAS_OF_VALIDATOR = Pattern.compile(
        "(?is)=\\s*(this\\s*\\.\\s*)?chashConstraintValidator\\w*\\s*(\\(\\s*\\))?\\s*[;,)]"
            + "|\\bvar\\s+\\w+\\s*=\\s*[^;]*ChashConstraintValidator|\\b(\\w+\\.)*ChashConstraintValidator\\s+\\w+\\s*=");

    private static String stripComments(String src) {
        return src.replaceAll("(?s)/\\*.*?\\*/", "").replaceAll("//[^\\n]*", "");
    }

    @Test
    void onlyTheHandlerCallsTheValidatorsRun_andNothingElseNamesItButTheListedFiles() throws IOException {
        List<String> problems = new ArrayList<>();
        int files = 0;
        int mentioning = 0;
        boolean handlerCallsRun = false;
        try (Stream<Path> walk = Files.walk(MAIN)) {
            for (Path p : walk.filter(x -> x.toString().endsWith(".java")).toList()) {
                files++;
                String name = p.getFileName().toString();
                String code = stripComments(Files.readString(p));
                boolean mentions = code.contains("ChashConstraintValidator") || code.contains("chashConstraintValidator");
                if (!mentions) continue;
                mentioning++;
                if (!MAY_MENTION.containsKey(name)) {
                    problems.add(name + " names ChashConstraintValidator and is not on MAY_MENTION: a new user of it needs a reason here");
                    continue;
                }
                boolean calls = RUN_ON_VALIDATOR.matcher(code).find();
                if (name.equals(HANDLER)) {
                    handlerCallsRun = calls;
                } else if (!name.equals("ChashConstraintValidator.java")) {
                    if (calls) {
                        problems.add(name + " calls the validator's run, which only " + HANDLER + " may (never at boot)");
                    }
                    if (RUN_SHAPE.matcher(code).find()) {
                        problems.add(name + " has a run(...) taking the validator's dry-run/Listener arguments, which only " + HANDLER
                            + " may call (an alias of the validator?)");
                    }
                    // the setter may store a validator it is handed (this.f = param); a new one built and assigned here, or a
                    // local alias of the stored one, is how a boot hook would hold it
                    if (ALIAS_OF_VALIDATOR.matcher(code).find()) {
                        problems.add(name + " assigns the validator to a variable or field (an alias a boot hook could run)");
                    }
                }
            }
        }
        System.out.println("never-at-boot gate: files=" + files + " mentioning=" + mentioning + " handlerCallsRun=" + handlerCallsRun);
        assertThat(files).as("non-vacuity: the scan walked the real source tree").isGreaterThan(100);
        assertThat(mentioning).as("non-vacuity: it found the files that name the validator").isGreaterThanOrEqualTo(MAY_MENTION.size() - 1);
        assertThat(handlerCallsRun).as("non-vacuity: the handler does call run (the pattern matches the real call)").isTrue();
        assertThat(problems).isEmpty();
    }

    @Test
    void theDetectorFlagsABootHookCallingRun() {
        for (String bad : List.of(
            "service.chashConstraintValidator.run(false, l);",
            "validator.run(true, listener);",
            "new ChashConstraintValidator(admin).run(false, l);",
            "executor.submit(chashConstraintValidator::run);",
            "chashConstraintValidator().run(false, l)")) {
            assertThat(RUN_ON_VALIDATOR.matcher(bad).find()).as(bad).isTrue();
        }
        assertThat(RUN_ON_VALIDATOR.matcher("validator.cancelRunning();").find()).isFalse();
        // the aliases RUN_ON_VALIDATOR does not see: the shape of the call, and the assignment that makes the alias
        for (String alias : List.of("cv.run(false, listener);", "v.run(true, l);", "x.run(dryRun, new Recorder());",
            "exec.submit(() -> cv.run(isDry, myListener));")) {
            assertThat(RUN_SHAPE.matcher(alias).find()).as(alias).isTrue();
        }
        for (String assign : List.of("var v = chashConstraintValidator;", "ChashConstraintValidator cv = this.chashConstraintValidator;", "dev.nexus.service.db.ChashConstraintValidator cv = make();",
            "var v = chashConstraintValidator();", "var v = new ChashConstraintValidator(admin);",
            "foo(a, b = chashConstraintValidator)")) {
            assertThat(ALIAS_OF_VALIDATOR.matcher(assign).find()).as(assign).isTrue();
        }
        assertThat(ALIAS_OF_VALIDATOR.matcher("this.chashConstraintValidator = validator;").find()).as("storing a handed validator").isFalse();
        assertThat(ALIAS_OF_VALIDATOR.matcher("this.chashConstraintValidator = new dev.nexus.service.db.ChashConstraintValidator(a, b, c);").find())
            .as("the service building the one it holds").isFalse();
        assertThat(ALIAS_OF_VALIDATOR.matcher("() -> chashConstraintValidator, validateConstraintsJobs").find()).isFalse();
        assertThat(RUN_SHAPE.matcher("retry.run(() -> work())").find()).as("another run( is not it").isFalse();
        assertThat(RUN_ON_VALIDATOR.matcher("() -> chashConstraintValidator").find()).isFalse();
    }
}
