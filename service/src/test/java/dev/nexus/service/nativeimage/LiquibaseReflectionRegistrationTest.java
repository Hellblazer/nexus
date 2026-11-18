// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.nativeimage;

import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.beans.PropertyDescriptor;
import java.io.File;
import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.TreeSet;
import liquibase.change.Change;
import liquibase.change.ChangeFactory;
import liquibase.change.ChangeMetaData;
import liquibase.change.ChangeParameterMetaData;
import liquibase.changelog.ChangeLogParameters;
import liquibase.changelog.ChangeSet;
import liquibase.changelog.DatabaseChangeLog;
import liquibase.parser.ChangeLogParser;
import liquibase.parser.ChangeLogParserFactory;
import liquibase.precondition.Precondition;
import liquibase.precondition.PreconditionLogic;
import liquibase.resource.ClassLoaderResourceAccessor;
import liquibase.resource.ResourceAccessor;
import liquibase.util.ObjectUtil;
import org.junit.jupiter.api.Test;

/**
 * nexus-f3yqd.2.22: every Liquibase method the engine reaches by reflection while it applies its
 * own and the vendored changelogs must be registered in the native-image metadata.
 *
 * <p>The 0.1.195 release smoke failed with {@code MissingReflectionRegistrationError ...
 * SQLFileChange.setPath}: the traced metadata held {@code SQLFileChange} and
 * {@code CreateProcedureChange} with {@code <init>} only. A JVM run cannot see that, because
 * reflection always succeeds there; this test replays the lookup against the metadata files.
 *
 * <p>It loads the three master changelogs (the engine's own and the two vendored libraries)
 * through Liquibase's own {@link ChangeLogParser}, collects every change class actually
 * instantiated (changeset changes and rollback changes) and every precondition class, and asks
 * Liquibase's own metadata what it will call:
 * <ul>
 *   <li>For each {@link ChangeParameterMetaData} of a change, the read AND the write method, found
 *       the way {@code ChangeParameterMetaData.getReadMethod/getWriteMethod} find them
 *       ({@link ObjectUtil#getDescriptors}, with the {@code is} fallback for a {@code Boolean}
 *       getter). The write side runs at parse time ({@code AbstractChange.load}). The read side
 *       runs at apply time: {@code MarkChangeSetRanGenerator} calls {@code getDescription()}, which
 *       reads every parameter named {@code *name} or {@code path}, and a change that does not
 *       override {@code generateCheckSum} (CreateProcedureChange on the V8 path) serialises every
 *       parameter through its getter.</li>
 *   <li>For each serializable field of a precondition, its setter(s) ({@code ObjectUtil.setProperty}
 *       sets each XML attribute and the body through {@code set<Field>}).</li>
 * </ul>
 * A method counts as registered when the metadata has an entry for the class that DECLARES it
 * (an override needs its own entry) naming it with its exact parameter types, or marks that class
 * {@code allDeclaredMethods} / {@code allPublicMethods}. The method comes from Liquibase's own
 * introspection, not a hand-kept list, so a Liquibase upgrade that adds a parameter fails here.
 *
 * <p>Not covered: the community reachability-metadata repository the native build also merges
 * (it is not in this repo, so a method only it registers shows as missing: register it here too,
 * which is harmless), and reflection Liquibase does outside these two lookups.
 */
class LiquibaseReflectionRegistrationTest {

    private static final File NATIVE_DIR =
        new File("src/main/resources/META-INF/native-image");
    private static final List<String> MASTERS = List.of(
        "db/changelog/db.changelog-master.xml",
        "db/vendor/ddl_utils/db.changelog-master.xml",
        "db/vendor/dml_utils/db.changelog-master.xml");

    /** class -> registered (name + parameter types) signatures, and the classes registered with all methods. */
    private record Registered(Map<String, Set<String>> signatures, Set<String> allMethods) {
        boolean has(Method m) {
            String cls = m.getDeclaringClass().getName();
            return allMethods.contains(cls)
                || signatures.getOrDefault(cls, Set.of()).contains(signature(m));
        }
    }

    /** One entry per declaring class and signature: RawSQLChange and SQLFileChange share AbstractSQLChange's methods. */
    private static String key(Method m) {
        return m.getDeclaringClass().getName() + "." + signature(m);
    }

    private static String signature(Method m) {
        return m.getName() + Arrays.stream(m.getParameterTypes())
            .map(Class::getName).toList();
    }

    private static Registered loadRegistered() throws Exception {
        ObjectMapper mapper = new ObjectMapper();
        Map<String, Set<String>> signatures = new HashMap<>();
        Set<String> all = new HashSet<>();
        JsonNode traced = mapper.readTree(new File(NATIVE_DIR, "traced/reachability-metadata.json"));
        for (JsonNode entry : traced.get("reflection")) {
            if (entry.get("type").isTextual()) {
                absorb(entry, entry.get("type").asText(), signatures, all);
            }
        }
        for (JsonNode entry : mapper.readTree(new File(NATIVE_DIR, "reflect-config.json"))) {
            absorb(entry, entry.get("name").asText(), signatures, all);
        }
        return new Registered(signatures, all);
    }

    private static void absorb(JsonNode entry, String cls, Map<String, Set<String>> signatures, Set<String> all) {
        if (entry.path("allDeclaredMethods").asBoolean() || entry.path("allPublicMethods").asBoolean()) {
            all.add(cls);
        }
        for (JsonNode method : entry.path("methods")) {
            List<String> types = new ArrayList<>();
            method.path("parameterTypes").forEach(t -> types.add(t.asText()));
            signatures.computeIfAbsent(cls, k -> new HashSet<>()).add(method.get("name").asText() + types);
        }
    }

    private static void collectPreconditions(Precondition p, Set<Class<?>> into) {
        if (p == null) {
            return;
        }
        // DatabaseChangeLog builds its root container as a private inner subclass with `new`; the
        // reflective path (PreconditionFactory) only ever sees the public class it extends.
        Class<?> cls = p.getClass();
        while (cls.getName().contains("$")) {
            cls = cls.getSuperclass();
        }
        into.add(cls);
        if (p instanceof PreconditionLogic logic) {
            logic.getNestedPreconditions().forEach(n -> collectPreconditions(n, into));
        }
    }

    @Test
    void everyLiquibaseMethodTheChangelogsReachIsRegisteredForNative() throws Exception {
        ResourceAccessor accessor = new ClassLoaderResourceAccessor();
        Set<Class<?>> changeClasses = new TreeSet<>(java.util.Comparator.comparing(Class::getName));
        Set<Class<?>> preconditionClasses = new TreeSet<>(java.util.Comparator.comparing(Class::getName));
        int changesets = 0;
        for (String master : MASTERS) {
            ChangeLogParser parser = ChangeLogParserFactory.getInstance().getParser(master, accessor);
            DatabaseChangeLog log = parser.parse(master, new ChangeLogParameters(), accessor);
            collectPreconditions(log.getPreconditions(), preconditionClasses);
            for (ChangeSet cs : log.getChangeSets()) {
                changesets++;
                collectPreconditions(cs.getPreconditions(), preconditionClasses);
                for (Change c : cs.getChanges()) {
                    changeClasses.add(c.getClass());
                }
                if (cs.getRollback() != null) {
                    for (Change c : cs.getRollback().getChanges()) {
                        changeClasses.add(c.getClass());
                    }
                }
            }
        }

        // Liquibase's own metadata, evaluated on one instance per class.
        TreeMap<String, Method> methods = new TreeMap<>();
        int parameters = 0;
        int readOnly = 0;
        for (Class<?> cls : changeClasses) {
            Change instance = (Change) cls.getConstructor().newInstance();
            ChangeMetaData metaData = ChangeFactory.getInstance().getChangeMetaData(instance);
            for (ChangeParameterMetaData param : metaData.getParameters().values()) {
                parameters++;
                for (PropertyDescriptor d : ObjectUtil.getDescriptors(cls)) {
                    if (!d.getDisplayName().equals(param.getParameterName())) {
                        continue;
                    }
                    Method read = d.getReadMethod();
                    if (read == null) {
                        read = cls.getMethod("is" + Character.toUpperCase(d.getName().charAt(0)) + d.getName().substring(1));
                    }
                    methods.put(key(read), read);
                    Method write = d.getWriteMethod();
                    if (write != null) {
                        methods.put(key(write), write);
                    } else {
                        readOnly++;
                    }
                }
            }
        }
        for (Class<?> cls : preconditionClasses) {
            Precondition instance = (Precondition) cls.getConstructor().newInstance();
            for (String field : instance.getSerializableFields()) {
                String setter = "set" + Character.toUpperCase(field.charAt(0)) + field.substring(1);
                for (Method m : cls.getMethods()) {
                    if (m.getName().equals(setter) && m.getParameterCount() == 1) {
                        methods.put(key(m), m);
                    }
                }
            }
        }

        Registered registered = loadRegistered();
        List<String> missing = new ArrayList<>();
        for (Map.Entry<String, Method> e : methods.entrySet()) {
            if (!registered.has(e.getValue())) {
                missing.add(e.getValue().getDeclaringClass().getName() + "." + signature(e.getValue()));
            }
        }
        System.err.printf("liquibase reflection: %d changesets, %d change classes, %d precondition classes, "
            + "%d change parameters (%d without a write method), %d distinct methods examined, %d not registered%n",
            changesets, changeClasses.size(), preconditionClasses.size(), parameters, readOnly,
            methods.size(), missing.size());

        assertThat(changesets).as("changesets parsed").isGreaterThan(500);
        assertThat(methods).as("methods examined: the scan read nothing").isNotEmpty();
        assertThat(changeClasses.stream().map(Class::getSimpleName))
            .as("the vendored libraries' change classes must be among those examined")
            .contains("SQLFileChange", "CreateProcedureChange", "RawSQLChange");
        assertThat(preconditionClasses.stream().map(Class::getSimpleName))
            .contains("PreconditionContainer", "SqlPrecondition", "DBMSPrecondition");
        assertThat(missing)
            .as("native image would fail with MissingReflectionRegistrationError on these methods; "
                + "register each in reflect-config.json (the traced file loses hand edits on a re-trace)")
            .isEmpty();
    }
}
