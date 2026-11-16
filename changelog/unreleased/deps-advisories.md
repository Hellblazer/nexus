### Security

- **The install lock moves four dependencies past published advisories.** pypdf 6.21.2 -> 6.21.4 (seven advisories), dnspython 2.9.0 -> 2.9.2, opentelemetry-api 1.45.1 -> 1.45.2 and soupsieve 3.0.4 -> 4.1.1, in `uv.lock` and the shipped `src/nexus/_install/constraints.txt`. None is imported by nexus directly. The weekly install-lock audit had been red on them since 2026-11-02.
