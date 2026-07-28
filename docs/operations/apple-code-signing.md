# Apple Code-Signing Provisioning Runbook (mac-arm64 engine binary)

Operational narrative for provisioning the six `APPLE_*` credentials that turn
the published `nexus-service-mac-arm64` binary from ad-hoc-signed into
Developer-ID-signed and notarized. Once-a-year work at most, which is exactly
why it is written down: `nexus-1e2eh` is the standing observation that
release-only procedures rot silently.

The workflow arm is already built and tested — this document is only the human
half. Beads: `nexus-27i0m` (the signing arm), `nexus-2oh5q` (the runtime hazard
and its gate).

## 0. What this buys, and what it does NOT

**Buys:** a browser-downloaded copy of the release asset stops being
Gatekeeper-blocked, and the artifact carries your developer identity.

**Does not change:** `nx daemon service install-binary` already works today and
is unaffected. It fetches over the API, which sets no `com.apple.quarantine`
xattr, so Gatekeeper never adjudicates the binary at all. That is precisely why
this has never bitten anyone and why there is no urgency — but also why the only
honest test of the signed configuration is one that applies the xattr by hand
(see §5).

> **⚠ Signing makes things WORSE until §5 passes.** Developer-ID signing enables
> Hardened Runtime, which enables macOS Library Validation, which refuses any
> dylib not signed by the same Team ID. The engine `System.load()`s two bundled
> third-party natives (onnxruntime, DJL HuggingFace tokenizers) whenever
> local-mode embedding initializes. `service/deploy/mac-entitlements.plist`
> carries `com.apple.security.cs.disable-library-validation` to permit them, but
> a shipped entitlement proves only that the flag was passed to `codesign` — not
> that the loads succeed. **Do not set `APPLE_SIGNING_REQUIRED=true` until §5
> passes on real hardware.**

## 1. Prerequisites

- Active Apple Developer Program membership.
- **Account Holder** role. The `Developer ID Application` certificate type is
  Account-Holder-only; if you do not see it in §2 you are signed in as something
  else.
- An arm64 Mac (you will need it again for §5).

Two different Apple sites are involved, which is the usual source of confusion:

| site | what you get there |
|---|---|
| `developer.apple.com` | the Developer ID **certificate** (signing identity) |
| `appstoreconnect.apple.com` | the **notary API key** (notarization credential) |

## 2. Developer ID certificate → three secrets

### 2.1 Generate the CSR locally, first

The private key is created on your Mac and never leaves it. Do this before
touching the portal.

Keychain Access → menu **Certificate Assistant → Request a Certificate From a
Certificate Authority…**

- **User Email Address**: your Apple ID
- **Common Name**: `Hal Hildebrand`
- **CA Email Address**: leave blank
- **Select "Saved to disk"** — not "Emailed to the CA"

Save the `.certSigningRequest`.

### 2.2 Create the certificate

`developer.apple.com/account` → **Certificates, Identifiers & Profiles** →
**Certificates** → **+**

- Type: **Developer ID Application** (under Software)
- If prompted for a Sub-CA, choose **G2**
- Upload the CSR, download the resulting `.cer`

### 2.3 Install and export

Double-click the `.cer` to install it into the login keychain, where it pairs
with the private key from §2.1.

In Keychain Access → **login → My Certificates**, find
`Developer ID Application: Hal Hildebrand (TEAMID)` and **expand the disclosure
triangle**. There MUST be a private key nested under it.

> No private key means the CSR never paired — usually a CSR generated on a
> different Mac or under a different login keychain. Start over at §2.1; there
> is no way to recover the pairing after the fact.

Right-click the **certificate** (not the key) → **Export** → format **Personal
Information Exchange (.p12)** → set a strong passphrase.

### 2.4 The three values

```bash
base64 -i ~/Desktop/devid.p12          # APPLE_DEV_ID_CERT_P12
                                        # APPLE_DEV_ID_CERT_PASSWORD = the .p12 passphrase
security find-identity -v -p codesigning
# copy the quoted string verbatim, e.g.
#   "Developer ID Application: Hal Hildebrand (AB12CD34EF)"
#                                        # APPLE_DEV_ID_IDENTITY
```

## 3. Notary API key → three secrets

`appstoreconnect.apple.com` → **Users and Access** → **Integrations** →
**App Store Connect API** → **Team Keys** → **+**

- Name: `nexus notarization`
- Access role: **Developer** is the least-privilege choice that works for
  `notarytool`. If notarization later returns 403, escalate to App Manager.

> **The `.p8` downloads exactly ONCE.** There is no second download and no
> recovery — losing it means revoking the key and issuing a new one. Put it
> somewhere durable before you close the tab.

- **Key ID** is shown on the key's row.
- **Issuer ID** is at the TOP of the Keys page, not on the row. It is shared
  across all your team's keys.

```bash
base64 -i ~/Downloads/AuthKey_XXXXXXXXXX.p8   # APPLE_NOTARY_KEY_P8
                                               # APPLE_NOTARY_KEY_ID   = the row's Key ID
                                               # APPLE_NOTARY_ISSUER_ID = the page-top Issuer ID
```

## 4. Load them into the `apple-signing` environment

**Environment secrets, not repository secrets.** Repo-level secrets are readable
by any job in any workflow that names them, including one added later. The
Developer ID private key is the one credential here whose compromise is not
cleanly recoverable: Apple's remedy is revoking the certificate, and Gatekeeper
checks revocation *online*, so binaries you have already published can start
failing. The `apple-signing` environment adds a required reviewer and restricts
deployment to the `engine-service-v*` tag.

Use `gh` rather than the web UI — it avoids clipboard truncation on the base64
blobs:

```bash
R=Hellblazer/nexus
E=apple-signing

base64 -i ~/Desktop/devid.p12 | gh secret set APPLE_DEV_ID_CERT_P12 --env $E --repo $R
gh secret set APPLE_DEV_ID_CERT_PASSWORD --env $E --repo $R      # prompts
gh secret set APPLE_DEV_ID_IDENTITY      --env $E --repo $R      # prompts

base64 -i ~/Downloads/AuthKey_*.p8 | gh secret set APPLE_NOTARY_KEY_P8 --env $E --repo $R
gh secret set APPLE_NOTARY_KEY_ID        --env $E --repo $R
gh secret set APPLE_NOTARY_ISSUER_ID     --env $E --repo $R

gh api repos/$R/environments/$E/secrets --jq '.secrets[].name'   # expect all six
```

**All-three-or-none per group.** The workflow hard-fails on partial
configuration rather than silently shipping ad-hoc — provision a whole group or
none of it.

### Pre-flight: prove the certificate signs, before trusting CI

Ten seconds, and it splits the failure space cleanly. If this works, any later
CI failure is secrets plumbing, not Apple:

```bash
cp /bin/echo /tmp/testsign
codesign --force --options runtime --timestamp \
  --sign "Developer ID Application: Hal Hildebrand (TEAMID)" /tmp/testsign
codesign -dv /tmp/testsign 2>&1 | grep TeamIdentifier   # must NOT say "not set"
```

## 5. Cut a tag, then GATE IT on real hardware

Cut the next engine tag per [the engine-release skill](../../.claude/skills/engine-release/SKILL.md).

> **The tag build now PAUSES for approval.** `build-publish` declares the
> `apple-signing` environment, so the run waits on you. One approval releases
> all three matrix legs. This is the gate working, not a hang.

Then, on your arm64 Mac:

```bash
NEXUS_SERVICE_TAG=engine-service-vX.Y.Z tests/e2e/mac-signed-binary-gate.sh
```

Must end `MAC SIGNED-BINARY GATE PASSED`. It downloads the published artifact,
applies the quarantine xattr a browser download would set, asserts Developer-ID
signature + Hardened Runtime + the entitlement + `spctl` acceptance, then boots
the SIGNED binary and asserts a real bge-768 embed executed — the JNI
`System.load()`s that Library Validation would refuse. A skipped embed is a
FAILURE there, not a pass.

This cannot be a CI job: mac-arm64 is `smoke: false` in the release workflow
(macos-14 runners have no Docker), and `codesign` runs after the linux-only
smoke, so CI never boots the signed mac bytes at all. `codesign --verify` cannot
see a runtime dlopen refusal.

## 6. Arm the regression guard

Only after §5 passes:

```bash
gh variable set APPLE_SIGNING_REQUIRED --body true --repo Hellblazer/nexus
```

This is a repository **variable**, not a secret. With it set, absent `APPLE_*`
secrets become a hard build failure instead of a warn-and-ship-ad-hoc — so a
later credential expiry or accidental deletion cannot silently regress you to
unsigned binaries.

## 7. Renewal and failure modes

| symptom | cause | remedy |
|---|---|---|
| build fails "secrets PARTIALLY configured" | one of a group of three missing | provision the whole group |
| build warns "UNSIGNED (ad-hoc)" | no secrets at all, `APPLE_SIGNING_REQUIRED` unset | expected pre-provisioning |
| build fails "APPLE_SIGNING_REQUIRED=true but secrets absent" | credentials deleted or expired after arming | restore them, or consciously unset the variable |
| notarization 403 | notary key role too narrow | escalate the key's role to App Manager |
| §5 gate fails on library validation | the entitlement is not reaching the signature | check `service/deploy/mac-entitlements.plist` and the `--entitlements` flag |

**Certificate expiry is a real gap worth a calendar entry.** Developer ID
Application certificates are valid five years. `APPLE_SIGNING_REQUIRED=true`
fails loudly when secrets *vanish* but NOT when the certificate merely expires —
that surfaces as a codesign-time error on a release you were trying to cut.
Because the workflow signs with `--timestamp`, already-signed binaries keep
validating past expiry, so this is a build-breaks problem rather than a
shipped-artifact problem.

Rotation asymmetry, which should drive how carefully you treat each: the notary
`.p8` is cheap — revoke and reissue in App Store Connect, already-notarized
artifacts are unaffected. The `.p12` is the crown jewel; treat it as
irreplaceable.

## 8. What is actually secret

Only three of the six are credentials. This matters for incident response —
leaking the identity string is a non-event, leaking the `.p12` is not.

| sensitive | not sensitive |
|---|---|
| `APPLE_DEV_ID_CERT_P12` (private key) | `APPLE_DEV_ID_IDENTITY` — `codesign -dv` prints it on every signed binary |
| `APPLE_DEV_ID_CERT_PASSWORD` | `APPLE_NOTARY_KEY_ID` |
| `APPLE_NOTARY_KEY_P8` (private key) | `APPLE_NOTARY_ISSUER_ID` — your team's, shared across keys |

## Accuracy note

Apple reshuffles both portals periodically, so exact menu labels may drift from
what is written here; the mechanics (CSR → cert → `.p12`; API key → `.p8`) have
been stable for years. The least certain item is the minimum notary key role in
§3 — Developer is the least-privilege choice believed to work, with App Manager
as the documented fallback.
