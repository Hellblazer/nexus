# PDF stack on demand: rehearsal (nexus-2k66t.4.2)

Design of record: T2 `nexus/design-pdf-stack-on-demand-2026-10-23` section 8.
The unit tests prove the state machine against fakes and the real CLI in an
interpreter that cannot see the stack. These legs prove what they cannot: a real
`uv pip install` of the release-candidate wheel produces a working stack, the
re-run lands in the new generation, torch is the CPU build on Linux, and an
upgrade keeps the extra.

Run on the release-candidate wheel, never on a live install. Legs a-f, p and u are
`tests/e2e/pdf-on-demand-rehearsal.sh` (it builds the wheel from the checkout, a
successor wheel for leg f, and runs in a throwaway HOME/tools/bin with an
allow-listed environment). It has not been run by its author. The legs run in
the order a, c, d, e, b, f, p, u: c, d and e assert a REFUSAL and so need the slim
install, which leg b replaces with a stack-carrying one.

| Leg | Where | How | What a pass proves |
|---|---|---|---|
| a | chas (Linux container, `lab-run -c lab-l`), hellmini throwaway account | script, `--legs a` | A bare `--bootstrap` install (no `--extras`: the shape `--slim`, a bare `uv tool install conexus` and the .mcpb bundle arrive in; the one-line installers and the plugin's setup pass `--extras pdf` since nexus-2k66t.4.9) has no torch, mineru or pdftext; the doctor row is informational (not red); `nx doctor --check-mineru` says N/A; the on-disk size is recorded against 7.85.0. Search and `store put` on the slim install: also run `tests/e2e/fresh-install-mvv.sh` (its venv layer installs this wheel bare; its generation leg builds with `--extras pdf` as the installers do, and leg 9b builds the slim generation). |
| b | chas, hellmini | script, `--engine --legs b` | A non-TTY `nx index pdf` of a formula paper installs the stack, re-runs in the new generation and exits 0; the MinerU route was taken; no `pymupdf_normalized`; LaTeX is in search; installed versions equal `uv.lock` (`tests/e2e/lib/lock_check.py`); torch is `+cpu` on Linux; the receipt lists `pdf`; the conexus version is unchanged (the fetch is not an upgrade). |
| c | chas, hellmini | script, `--legs c` (add `--engine` for the collection snapshot) | `NX_PDF_STACK=refuse`: exit 69, the message names `nx self install --extras pdf`, no generation was built, no config file written, no collection changed. |
| d | chas, hellmini | script, `--legs d` | `UV_OFFLINE=1` with a cold cache: exit 75, no generation left behind. |
| e | chas, hellmini | script, `--engine --legs e` | A hook-shaped `NX_PDF_STACK=refuse nx index repo --since-head` over a repo with code, prose and a PDF indexes the first two, holds the PDF back (named in the log, not registered), exits 69, and the PDF support row counts it. |
| f | chas, hellmini | script, `--engine --legs b,f` | `nx self install --version <next>` from a generation that has the stack: the new receipt keeps `pdf`, a second PDF indexes with no install step, and the stack's wheels are not downloaded again. |
| u | chas, hellmini | script, `--engine --legs u` (add `--old-version X.Y.Z`; default 7.85.0; it must be below the candidate's version, since the candidate wheel would stand in for an equal one, and the script refuses that) | The upgrade rule with a real old installer: the previous release is bootstrapped in a second sandbox (pre-split receipt, stack present); ITS `nx self install --version <rc>` builds the candidate slim (receipt still pre-split, no stack); `nx upgrade` then reports the carry, and the new receipt lists `pdf` with `installer_schema` 2 at the candidate's version; a formula PDF indexes with no install step. Without `--engine` the index step is reported NOT RUN and the script exits non-zero. |
| p | chas, hellmini | script, `--legs p` (no engine) | The default install route, for real: `nx self install --bootstrap --extras pdf` (what the installers and the plugin's setup run) with real uv and the candidate wheel in a virgin HOME builds ONE generation whose receipt lists `pdf` with `installer_schema` 2, with all five stack modules, CPU torch on Linux, the candidate's version, and a working shim. Every other leg bootstraps bare; `fresh-install-mvv.sh` leg 9 drives `install_generation.sh --extras`, not this command. |
| g | Windows gate guest (qwent, `C:\build\lab\lab-run-win.cmd`) | MANUAL, legs a, b and f by hand with `install.ps1` or `uv tool run ... nx self install --bootstrap` | The Python `_WindowsBuild` path carries the extra; the re-run uses `subprocess`; the VC++ runtime check refuses with the redistributable link on a guest without it (cloud mode); torch's PyPI CPU wheels install at Windows path depths. Progress is not streamed on Windows (the in-process builder captures uv's output): expect a silent wait after the one notice line. |
| h | any machine that builds the bundle | MANUAL: build the `.mcpb`, open it in Claude Desktop | The bundle (bare `conexus`) builds smaller, starts, and search works. It has no index tool, so no PDF path is reachable from it. |

## Also run before the release is cut

- The package-upgrade rehearsal (`tests/e2e/migration-rehearsal/rehearse_package_upgrade.sh`)
  from the previous release. Its stage 6 builds the upgrade target with `--extras pdf`
  (the installers' default since nexus-2k66t.4.9), so its lock check names
  opencv-python-headless (the pdf extra's witness); `NX_REHEARSE_SLIM=1` builds it bare. The upgrade rule itself, with a real old installer, is
  leg u above; run both.
- A box with a legacy `uv tool` tree: `nx self install` moves it onto a
  generation that lists `pdf`.

## Known gaps to look at on the first run

- `install.sh` / `install.ps1` have no `--pdf` flag yet (a follow-up bead): a
  user who wants the stack before going offline runs `nx self install --extras pdf`.
- Hooks written before this change still dispatch without `NX_PDF_STACK=refuse`,
  so on a slim install they would fetch the stack in the background. `nx hooks
  update` (run by `nx upgrade`) rewrites the stanza.
