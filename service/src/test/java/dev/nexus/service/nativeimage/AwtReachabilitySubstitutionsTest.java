package dev.nexus.service.nativeimage;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.lang.reflect.Method;
import java.util.List;
import org.junit.jupiter.api.Test;

/**
 * Structural guard for {@link AwtReachabilitySubstitutions} (nexus-223oj).
 *
 * <p>This is a GUARD, not proof of the fix. What the substitutions actually do —
 * keep {@code java.awt} out of the native image's reachable set, so the build stops
 * emitting {@code libawt.so}, {@code libawt_headless.so}, {@code libawt_xawt.so},
 * {@code libjavajpeg.so} and {@code liblcms.so} beside an executable that never ships
 * them — is observable ONLY in a {@code -Pnative} build's Build-artifacts block. No
 * JVM test can reproduce it. The real gate is the native build plus
 * {@code tests/e2e/migration-rehearsal/run.sh --shakeout} Phase F reaching
 * {@code NATIVE SMOKE PASS} with a real 768-dim embed.
 *
 * <p>What this test DOES catch cheaply is the failure mode that would otherwise
 * surface only at tag time: {@code @Delete} requires its target to EXIST, so a JNA or
 * DJL upgrade that renames or removes any substituted member turns the next native
 * build red. Nothing on the PR path builds a native image — {@code service-ci} has no
 * native job (removed 2026-07-06 for cost) — so without this test that breakage is
 * discovered by hand, pre-tag, by whoever is cutting an engine release. Here it fails
 * in the ordinary Java suite instead.
 *
 * <p>Deliberately asserts the SAME five signatures the substitution declares, by
 * reflection against the resolved jar. If this test and the substitution ever
 * disagree, the substitution is the one that stops the build.
 */
class AwtReachabilitySubstitutionsTest {

    /**
     * The AWT-typed members of {@code com.sun.jna.Native} deleted by
     * {@code Target_com_sun_jna_Native}, as {@code name(paramTypes...)}. Enumerated
     * with {@code javap -p} against jna-5.14.0. These are JNA's public helpers for
     * getting a native handle from an AWT component; nexus calls none of them (JNA is
     * present only as a transitive dependency of the DJL HuggingFace tokenizer), but
     * their presence in a JNI-registered class is what dragged {@code java.awt} in.
     */
    private static final List<String[]> DELETED_JNA_MEMBERS = List.of(
            new String[] {"getWindowID", "java.awt.Window"},
            new String[] {"getComponentID", "java.awt.Component"},
            new String[] {"getWindowPointer", "java.awt.Window"},
            new String[] {"getComponentPointer", "java.awt.Component"},
            new String[] {"getWindowHandle0", "java.awt.Component"});

    @Test
    void everyDeletedJnaMemberStillExists() throws Exception {
        Class<?> native_ = Class.forName("com.sun.jna.Native");
        for (String[] spec : DELETED_JNA_MEMBERS) {
            String name = spec[0];
            Class<?> param = Class.forName(spec[1]);
            Method m = assertDoesNotThrow(
                    () -> native_.getDeclaredMethod(name, param),
                    () -> "com.sun.jna.Native." + name + "(" + spec[1] + ") is gone — JNA upgraded? "
                            + "AwtReachabilitySubstitutions @Delete's it, and @Delete requires the "
                            + "target to exist, so the next -Pnative build will fail. Update the "
                            + "substitution and this list together.");
            assertTrue(java.lang.reflect.Modifier.isStatic(m.getModifiers()), name + " is no longer static");
        }
    }

    @Test
    void jnaAwtHolderStillExists() {
        assertDoesNotThrow(
                () -> Class.forName("com.sun.jna.Native$AWT"),
                "com.sun.jna.Native$AWT is gone — JNA upgraded? Target_com_sun_jna_Native_AWT "
                        + "@Delete's the whole class and will fail the next -Pnative build.");
    }

    @Test
    void djlBufferedImageFactoryStillExists() {
        assertDoesNotThrow(
                () -> Class.forName("ai.djl.modality.cv.BufferedImageFactory"),
                "ai.djl.modality.cv.BufferedImageFactory is gone — DJL upgraded? "
                        + "Target_ai_djl_modality_cv_BufferedImageFactory @Delete's it and will fail "
                        + "the next -Pnative build.");
    }

    /**
     * Non-vacuity: a typo in {@link #DELETED_JNA_MEMBERS} would make
     * {@link #everyDeletedJnaMemberStillExists} iterate over nothing and pass while
     * checking nothing. Pin the count so the list cannot silently empty out.
     */
    @Test
    void guardListIsNotEmpty() {
        assertEquals(5, DELETED_JNA_MEMBERS.size(), "expected exactly the 5 AWT-typed members javap reports");
    }
}
