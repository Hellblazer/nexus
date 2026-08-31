// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.util.function.UnaryOperator;

/**
 * nexus-ogccs — resolve the onnx_models root the way the Python provisioner does.
 *
 * <p>The client provisions the bge / cross-encoder artifacts under
 * {@code $HOME/.cache/nexus/onnx_models} ({@code Path.home()} reads the HOME
 * env var), while this class's consumers previously hardcoded
 * {@code System.getProperty("user.home")} — the passwd entry, not HOME. Any
 * process tree where the two differ (containers with a custom HOME, the
 * release-sandbox HOME, CI runners) got a green provision and then an engine
 * that looked somewhere else entirely.
 *
 * <p>Resolution, first hit wins — mirroring
 * {@code nexus.db.onnx_model_root.service_onnx_models_root()} rung for rung
 * (the cross-language parity test {@code tests/db/test_onnx_model_root.py}
 * pins both sides to this order):
 * <ol>
 *   <li>{@code NX_ONNX_MODEL_DIR} — the onnx_models root, passed explicitly by
 *       the supervisor's spawn env so both sides agree by construction</li>
 *   <li>{@code $HOME/.cache/nexus/onnx_models} — the provisioner's own default</li>
 *   <li>{@code user.home} + {@code /.cache/nexus/onnx_models} — last resort for
 *       a process with no HOME at all (matches the pre-fix behaviour there)</li>
 * </ol>
 *
 * <p>No XDG_CACHE_HOME rung, deliberately: the Python provisioner does not
 * honour it, and a rung only one side reads re-creates the divergence this
 * class exists to end.
 */
public final class OnnxModelPaths {

    /** Spawn-env override naming the onnx_models ROOT (not a per-model dir). */
    public static final String MODEL_DIR_ENV = "NX_ONNX_MODEL_DIR";

    /** Path under the home directory rungs (2 and 3). */
    static final String HOME_SUFFIX = "/.cache/nexus/onnx_models";

    /** Resolved root plus the rung that produced it (for the boot log). */
    public record Resolved(String root, String source) {}

    /** Pure resolver — env and user.home injected (no real env mutation in tests). */
    static Resolved resolve(UnaryOperator<String> env, String userHome) {
        String explicit = env.apply(MODEL_DIR_ENV);
        if (explicit != null && !explicit.isBlank()) {
            return new Resolved(explicit.trim(), MODEL_DIR_ENV);
        }
        String home = env.apply("HOME");
        if (home != null && !home.isBlank()) {
            return new Resolved(home.trim() + HOME_SUFFIX, "HOME");
        }
        return new Resolved(userHome + HOME_SUFFIX, "user.home");
    }

    /** Resolve from the real environment. */
    public static Resolved resolved() {
        return resolve(System::getenv, System.getProperty("user.home"));
    }

    /** The resolved onnx_models root as a plain path string. */
    public static String root() {
        return resolved().root();
    }

    private OnnxModelPaths() {}
}
