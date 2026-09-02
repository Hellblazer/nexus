package dev.nexus.service.nativeimage;

import com.oracle.svm.core.annotate.Delete;
import com.oracle.svm.core.annotate.TargetClass;
import com.sun.jna.Pointer;
import java.awt.Component;
import java.awt.HeadlessException;
import java.awt.Window;

/**
 * GraalVM native-image substitutions that cut AWT out of the image's reachable set
 * (nexus-223oj).
 *
 * <p>Symptom: every native build emits {@code libawt.so}, {@code libawt_headless.so},
 * {@code libawt_xawt.so}, {@code libjavajpeg.so} and {@code liblcms.so} beside the
 * executable, and the build's Recommendations block says "AWT: Use the tracing agent
 * to collect metadata for AWT" — i.e. AWT is REACHABLE. The release workflow uploads
 * only {@code dist/$ASSET} and its signatures, so those libraries ship to nobody. On
 * Linux GraalVM links AWT DYNAMICALLY and expects the {@code .so} files on disk, so
 * any AWT-reachable path would fail at runtime far from its cause. Nothing in
 * {@code service/src/main/java} references {@code java.awt} or {@code javax.imageio} —
 * the reachability is entirely transitive.
 *
 * <p>Two transitive roots were identified by probing with
 * {@code -H:AbortOnTypeReachable}, which aborts during analysis and writes a causal
 * trace to {@code service/target/reports/trace_types_*.txt}:
 *
 * <ul>
 *   <li>{@code com.sun.jna.Native} declares five AWT-typed members. It is registered
 *       for JNI (type-level {@code "jniAccessible": true}) by BOTH the community
 *       reachability metadata and this project's traced metadata, so native-image
 *       reconstructs {@code Executable} objects for its declared methods — which
 *       materialises {@code java.awt.Window} and {@code java.awt.Component}, runs
 *       their class initializers, and reaches {@code GraphicsEnvironment}. Confirmed
 *       by measurement: stripping that flag moved the trace off {@code Window}.
 *       JNA's core class cannot simply be excluded, so the five members are deleted
 *       instead. Nothing in nexus calls them; JNA uses DJL's tokenizer only.</li>
 *   <li>{@code ai.djl.modality.cv.BufferedImageFactory} is the only class in
 *       {@code ai.djl:api} naming {@code java.awt} or {@code javax.imageio}, and the
 *       api jar's own {@code reflect-config.json} registers it with
 *       {@code allDeclaredMethods} + {@code allPublicMethods}. It is unreachable in
 *       practice: DJL is used here only for the HuggingFace tokenizer (OnnxEmbedder,
 *       Bge768Embedder, CrossEncoderReranker), and {@code ImageFactory.newInstance()}
 *       resolves its factories through a fixed {@code Class.forName} table, so
 *       deleting the leaf cannot break the parent by linkage. If one were somehow
 *       reached, the failure is a named {@code IllegalStateException} at class-init,
 *       not a {@code System.loadLibrary} miss far from the cause.</li>
 * </ul>
 *
 * <p>Why substitutions and not an exclusion flag: there is no such flag. An option to
 * exclude packages or classes from a native image is an OPEN GraalVM feature request
 * (oracle/graal#3225, filed 2021 by the Quarkus team for this exact symptom — JAXB
 * pulling {@code java.awt}/{@code javax.imageio}/{@code sun.java2d} into images that
 * never use them), and substituting the members that cause the undesirable classes to
 * be reached is the approach that issue names as the current one. A maven-shade
 * {@code <filter>} is NOT an alternative: native-image runs against the full runtime
 * classpath, in which {@code api-0.30.0.jar} and {@code jna-5.14.0.jar} appear
 * separately from the shaded jar, so excluding a class from the shaded jar leaves it
 * resolvable one classpath entry later.
 *
 * <p>Verification is the build's own Build-artifacts block: a fixed build lists only
 * {@code nexus-service} and emits none of the five AWT libraries, and the AWT
 * recommendation is absent. {@code libjava.so} and {@code libjvm.so} SURVIVE by
 * design — they are JNI shims from the tokenizer and onnxruntime, not AWT. Note that
 * {@code service-ci} has no native job, so nothing on the PR path rebuilds a native
 * image; this must be checked by hand pre-tag, or via {@code --shakeout} Phase F.
 */
public final class AwtReachabilitySubstitutions {

    private AwtReachabilitySubstitutions() {}
}

/**
 * Deletes the AWT interop surface of {@code com.sun.jna.Native}. These are JNA's
 * public helpers for obtaining a native handle from an AWT {@code Window} or
 * {@code Component}; nexus never calls them, and their mere presence in a
 * JNI-registered class is what drags {@code java.awt} into the image.
 */
@TargetClass(className = "com.sun.jna.Native")
final class Target_com_sun_jna_Native {

    @Delete
    static native long getWindowID(Window w) throws HeadlessException;

    @Delete
    static native long getComponentID(Component c) throws HeadlessException;

    @Delete
    static native Pointer getWindowPointer(Window w) throws HeadlessException;

    @Delete
    static native Pointer getComponentPointer(Component c) throws HeadlessException;

    @Delete
    static native long getWindowHandle0(Component c);
}

/** Deletes JNA's lazy AWT holder, which resolves {@code GraphicsEnvironment}. */
@TargetClass(className = "com.sun.jna.Native", innerClass = "AWT")
@Delete
final class Target_com_sun_jna_Native_AWT {}

/** Deletes DJL's {@code BufferedImage} factory — the {@code javax.imageio} root. */
@TargetClass(className = "ai.djl.modality.cv.BufferedImageFactory")
@Delete
final class Target_ai_djl_modality_cv_BufferedImageFactory {}
