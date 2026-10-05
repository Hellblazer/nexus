# Windows engine fixtures (RDR-224 review finding 11, nexus-f9bgu.29/.32)

The Windows smoke and stop probe read free-text engine logs and GraalVM's embedded-resources report.
Their tests used hand-written copies of both. These are the nearest real ones.

- `engine-first-boot.log`, `engine-second-boot.log`: VERBATIM lines (selected, not edited) from the native
  `nexus-service.exe` log of a real run on qwentescence (native Windows 11), 2026-10-05, taken from
  `C:\build\f9bgu17r2-keep\storage_service_native.log` (lines 1, 2, 9, 16, 21, 2789, 2792, 2809,
  2813-2815 and 2816-2818, 2823, 2925, 2927, 2944, 2946-2948). First boot: 508 changesets applied, serving,
  stopped by CTRL_BREAK. Second boot: `new_changesets=0`, `ort_temp_sweep` removed the first run's
  onnxruntime-java directory, stopped by CTRL_BREAK. No credential or token appears in the selected lines.
- `embedded-resources-fragment.json`: DERIVED, not captured. No Windows `embedded-resources.json` survived on
  the box (the build directories were removed after the probe), and a native rebuild was not run for this
  round. Shape and field names follow the report the existing tests already model; entry sizes are the real
  sizes of those members in the pinned jars (`~/.m2`, onnxruntime 1.20.0, tokenizers 0.30.0); origins use
  the `file:///C:/...` form GraalVM printed for the same machine's jar URLs in its build log
  (`jar:file:///C:/build/f9bgu8/src/service/target/...jar!/...`). Replace it with a captured report the
  next time a Windows native build runs with `-H:+GenerateEmbeddedResourcesFile`.
