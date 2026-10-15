# RDR-224 Post-Mortem: Native Windows Support

**Closed** 2026-10-15 · **Accepted** 2026-10-05 · **Epic** nexus-f9bgu (57 children, closed at close) · **Shipped** engine-service-v0.1.149 (first Windows assets) to v0.1.160, conexus 7.72.1 (first gate pass) to 7.80.0 · **Supersedes** RDR-218 (WSL2 appliance)

## What the RDR set out to do

nexus ran on Linux and macOS only. RDR-218 had proposed running the Linux
artifacts inside WSL2, which put a virtual-machine boundary between Claude
Code on Windows and nexus. RDR-224 replaced that with native artifacts: a
Windows x64 engine binary, a Windows PostgreSQL bundle, a client whose process
management works without POSIX, the plugin and hooks on native Claude Code,
and a gate that proves the whole path on a clean Windows machine. It named
seven gaps, from the engine binary (Gap 1) to the gate (Gap 7), with signing
(Gap 6) left to a Phase 0 decision.

## Implementation status

Implemented. The Phase 5 gate (`tests/e2e/windows-phase5-gate.ps1`, driven
unattended by `tests/e2e/windows-guest/run-gate.sh`) passed 5 of 5 on
published conexus 7.80.0 with engine 0.1.160 on three Hyper-V guests on
2026-10-15: Windows 11 Home and Pro as a standard user with no Git (Pro under
enforced AppLocker rules), and Enterprise as administrator (T2
`nexus_rdr/224-published-7.80.0-gate-2026-10-15`). The RDR's section
"Delivered versus the Problem Statement" lists each gap's outcome and the
residuals Sam accepted at close (T2 `nexus_rdr/224-close-decisions-2026-10-15`).
Gap 6 is not met by decision: the binaries are unsigned, and a machine with
Smart App Control on is not supported. Sam declared Windows 11 x64 supported at
close, with those residuals as its known limits; Windows 10 is not measured and
not supported.

## Implementation vs plan

### As planned

- A GraalVM native engine for Windows x64 and a PostgreSQL bundle with its
  four app-local VC++ runtime DLLs, released by the engine-service workflow on
  the self-hosted `win-release` runner (Phase 0 Step 0.1).
- A stop channel that sends the engine a console break and never kills
  PostgreSQL by default, with a cross-session stop refused.
- A logon-started supervisor through Task Scheduler, owner-only DACLs on
  credential files, and a SID-based service identity.
- The gate as Minimum Viable Validation on a clean guest, first green on
  published 7.72.1.

### Diverged

1. **Signing.** The plan treated Smart App Control as the reason signing might
   be required. Two September runs with it on blocked nothing, so signing was
   deferred (Step 0.2). On 2026-10-14 the Home guest with it on blocked the
   unsigned bundle, uv's Python and PortableGit DLLs, so signing nexus's own
   binaries would not be enough either. Sam decided no signing until a user
   requires it. On 2026-10-15 Windows blocked `nx.exe` itself on the same
   checkpoint in one run, and in a second run `nx.exe` ran and the new
   detection (nexus-f9bgu.57) refused before the download. With the same
   setting on, four runs gave four outcomes, and the cause is not known.
2. **Stop signals are per OS.** The design asked for `HUP` everywhere. HotSpot
   does not know `BREAK` on POSIX and Windows has no `HUP`, so Windows uses
   `TERM INT BREAK` and POSIX keeps `TERM INT HUP`.
3. **Task Scheduler restart-on-failure fires only when the launch fails**, not
   when the supervisor dies later, so a launcher respawns the supervisor.
4. **A plain stop also stopped PostgreSQL** (gate assertion 4 failed on
   7.77.1): PostgreSQL shared the supervisor's console and took the break. It
   now starts on a console of its own (7.78.0).
5. **Text encoding.** Python on Windows reads text and child output in the
   ANSI code page. 154 file calls and about 87 subprocess calls named no
   encoding; all now name UTF-8 except `schtasks`, which writes in the locale
   code page, and two lints keep it so (nexus-ox0c0, nexus-ikoqj).
6. **The Desktop route moved.** The `.mcpb` bundle could not be installed on
   the measured Claude Desktop build; chat and Cowork get conexus from the
   account marketplace instead.
7. **The gate ran on Hyper-V guests only.** Gap 7 names real hardware; Sam
   decided at close that a clean guest satisfies it (Step 0.5), with sleep and
   physical hardware as accepted residuals.
8. **`install-binary` did not extract the PostgreSQL bundle** into an empty
   directory while exiting 0 (nexus-f9bgu.56), found by the guest batch, fixed
   in 38c0c035d.
9. **The engine smoke chose its port before PostgreSQL bound its own** and
   both drew the same free port once (b16a58f1c).

### Added beyond the plan

- The unattended guest driver with standard-user, no-Git, AppLocker (audit,
  allow, DLL rules) and WDAC (audit, allow) modes, and Home and Pro guests
  beside the Enterprise one.
- Allow rules for AppLocker and WDAC measured and written into
  `docs/windows-install.md`: path rules suffice and nothing is signed (T2
  `224-research-34`).
- Smart App Control detection: `nx init` refuses before the download and
  `nx doctor` reports the state (nexus-f9bgu.57). Verified on the Home guest:
  the reader returned `on` and `evaluation` on the two checkpoints, and the
  refusal fired within a second (T2 `224-sac-detection-guest-runs-2026-10-15`).
  It ships in the next client release after 7.80.0.

### Planned but not done

- Authenticode signing (Gap 6), by decision (nexus-dj01b).
- Sleep, physical hardware, Microsoft and domain accounts, script enforcement
  under WDAC or AppLocker, and install, upgrade and removal under WDAC:
  accepted as unmeasured at close.

## Drift classification

| Category | Count | Examples | Preventable? |
| --- | --- | --- | --- |
| Unvalidated assumption | 2 | Smart App Control blocks nothing (1); a guest stands in for hardware (7) | Yes, spike: measure with the policy state read back, on more than one edition |
| Framework API detail | 3 | per-OS stop signals (2); Task Scheduler restart (3); encoding defaults (5) | Yes, source search for 2 and 5; spike for 3 |
| Missing failure mode | 3 | PostgreSQL took the console break (4); silent non-extraction (8); port race (9) | Yes, spike for 4; no for 8 and 9 |
| Scope underestimation | 1 | Desktop bundle not installable (6) | No: a host behaviour outside nexus |
| Internal contradiction | 1 | the RDR text kept about a dozen superseded sentences until the closure critique | Yes: replace, do not append, when evidence lands |

## What the next person should take from this

- Read back the policy state before believing a security-policy result. The
  September "Smart App Control on, nothing blocked" runs logged activation and
  still blocked nothing; the October run on another edition blocked the
  install. Record the edition, how the policy was turned on, and the registry
  value, or the result cannot be compared later.
- A measurement that falsifies an RDR premise has to be written into the RDR
  the same day, at every site that states the premise. Here the Smart App
  Control result sat in T2 and a bead for a day while the RDR and a public page
  still said the opposite, and the first closure critique failed on it.
- A check inside `nx` cannot report a policy that blocks `nx.exe`, and with
  Smart App Control the block did not land at the same point twice. When a
  failure can stop the program that would detect it, state that limit beside
  the detection.
- On Windows, every text read and every child process needs an explicit
  encoding; the lints now enforce it.

## Critique record

Three closure critiques, all against the RDR text and its T2 records:
round 1 not-justified (1 Critical: the Smart App Control premise; 3
Significant), round 2 partial (6 Significant: the Gap 7 label, Smart App
Control undetected, the evaluation-mode basis, missing residuals, no record of
Sam's acceptance, the live bundle swap text), round 3 partial (6 Significant:
the detection unmerged and its reader unobserved, the evaluation-to-on switch,
a stale revision history, Windows 10 not stated, no owner for the support
declaration). Every finding was fixed in the three 2026-10-15 closure passes in
the revision history, with the detection code, the reader probe, bead
nexus-f9bgu.58 and Sam's declaration. Sam closed after round 3 without a fourth
round (T2 `224-close-decisions-2026-10-15`).
