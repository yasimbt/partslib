# partslib: specification

Status: Draft v0.1, 2026-11-04. Nothing is built yet. Working name only.

A personal, single-source KiCad parts library, with a local dashboard for browsing parts and tracking every board that uses them.

## 1. Problem

Each KiCad project currently collects parts from wherever was convenient. That causes problems that have already happened on `DEV1_50_2_SGC-LP` (the first board, 100 footprints on the PCB and 147 non-power parts in the schematic):

| Problem | What happened |
|---|---|
| **The same part exists many times** | Resistors and capacitors come from JLCPCB, AKL and KiCad's stock libraries. Nothing stops two different "1 kΩ" parts being used |
| **Third-party libraries change underneath you** | JLCPCB library release 2026.09.21 dropped `C_0603`, `R_0603` and all 11 of the board's `0603,…` symbols, which were present in 2026.08.08 |
| **Machine-specific setup** | The board depends on environment variables and library nicknames that exist on one machine. 68 of its 100 3D models failed to load until they were configured |
| **Silent gaps** | Five ferrite beads had no footprint, 11 footprints had no 3D model, and five referenced a stock model that does not exist. Nobody was told; it took a manual audit |
| **No view across boards** | There is no way to ask "which boards use this part?" or "what is out of sync on this board?" |

## 2. Goals

1. **One part, one identity.** Every physical part has one permanent internal part number (IPN), used by all projects.
2. **Duplicates are blocked.** A second 1 kΩ 1% 0603 resistor cannot be added by accident.
3. **Complete parts only.** A part is released only with a symbol, footprint, 3D model, datasheet and at least one orderable manufacturer part number.
4. **Works offline in KiCad.** Designing a board must not need the dashboard or any server to be running.
5. **Dashboard over all parts and boards.** Search parts, see where each is used, and see the health of each board.
6. **Boards stay reproducible.** A released board opens correctly from a fresh checkout on any machine.

## 3. Non-goals for v1

- Team use, user accounts or hosting. This is one person, on one machine, through localhost.
- Other EDA tools. KiCad 10 only.
- Editing symbols or footprints in the dashboard. KiCad's editors do that.
- Changing board files from the dashboard. v1 reads boards and reports; it never writes to them.
- Purchasing, stock control and live pricing. See section 10.

## 4. Key decisions

Proposed defaults. Change them here before building.

| # | Decision | Choice | Why | Reconsider if |
|---|---|---|---|---|
| D1 | Source of truth | Plain text files in git, one per part | Reviewable diffs and full history, with no database to back up | Over about 20,000 parts, or more than one simultaneous editor |
| D2 | Dashboard data | SQLite index, rebuilt from the files | Fast search and joins. It can be deleted and rebuilt at any time, so it is never the record | Rebuilds become too slow |
| D3 | KiCad connection | **Generated standard libraries** (section 6) | See below | KiCad changes its library model |
| D4 | Part model | Two levels: a function part and its approved manufacturer parts | "1 kΩ 1% 0603" is one part, even if three manufacturers can supply it | |
| D5 | Dashboard | Local web app: Python, FastAPI, HTMX, SQLite | Small, no frontend build, and Python is already in the workflow | The UI outgrows server rendering |
| D6 | 3D models | STEP files in git LFS | Models are large binaries and should not bloat history | |
| D7 | Third-party content | Copy individual parts in, recording their source | Protects against upstream removals such as the one in section 1 | |

**Why not KiCad's database or HTTP libraries (D3).** Per the KiCad 10 docs, both hold only part data and reference symbols and footprints in other libraries, so standard libraries are needed regardless. Database libraries need an ODBC driver on each machine, and HTTP libraries are read-only and need a server reachable while designing, which conflicts with goal 4. The generator can add an HTTP or database output later without changing the part data.

## 5. Data model

### 5.1 Part

The file `parts/<category>/<IPN>.yaml`:

```yaml
ipn: RES-000123              # permanent, never reused or renamed
status: released             # draft | released | deprecated | obsolete
category: resistor
description: 1 kΩ 1% 0.1 W thick film, 0603

params:                      # fixed per category; see 5.3
  resistance_ohm: 1000
  tolerance_pct: 1
  power_w: 0.1
  voltage_v: 75
  package: "0603"
  technology: thick_film

symbol: Resistor:R           # base symbol in this library
footprint: Resistor:R_0603   # footprint in this library
model: Resistor/R_0603.step
datasheet: https://example.com/datasheet.pdf

mpns:                        # approved manufacturer parts, in preference order
  - manufacturer: UNI-ROYAL
    mpn: 0603WAF1001T5E
    lcsc: C21190
    lifecycle: active
    verified: 2026-11-04     # date checked against the datasheet

supersedes: null
superseded_by: null
notes: ""
```

The example values show the format and must be checked against a datasheet before real use.

### 5.2 IPN format

`<CAT>-<6 digits>`. The number is an allocated counter and carries no meaning. Values live in `params`, so correcting a value never changes an IPN.

### 5.3 Duplicate prevention

Each category defines its **identity parameters**, and a part's fingerprint is its category plus their normalised values.

| Category | Identity parameters |
|---|---|
| Resistor | resistance, tolerance, package, power, technology |
| Capacitor | capacitance, tolerance, voltage, dielectric, package |
| Inductor / ferrite | inductance or impedance at frequency, current, package |
| Diode | MPN-based: one part per device |
| IC, FET, connector and other non-commodity parts | MPN-based: one part per device |

- **Exact fingerprint match:** the part is rejected, and the error names the existing IPN.
- **Near match** (same value and package, different tolerance, say): a warning that must be acknowledged.
- **Normalisation:** `1k`, `1000`, `1.0 kΩ` and `1K0` must all compare equal.
- **Same MPN on two parts:** rejected.
- **Enforcement:** `partslib check` runs in CI, so hand-edited files cannot bypass it.

### 5.4 Lifecycle

```
draft -> released -> deprecated -> obsolete
```

- **`draft`:** not generated into KiCad libraries.
- **`released`:** requires a symbol, footprint, 3D model, datasheet, and at least one verified MPN. Pins must map to pads, and the footprint must be marked as checked against the datasheet.
- **`deprecated`:** stays available so old boards keep working, is hidden from new designs where possible, and is flagged on boards that use it.
- **Parts are never deleted.** A released part's identity parameters cannot change; create a new part and set `superseded_by`.

### 5.5 Assets and provenance

Every symbol, footprint and 3D model records where it came from, its licence, and the upstream version.

- **Why:** the current sources have different terms. Alternate KiCad Library is CC-BY-SA and the JLCPCB library is MIT. Copying CC-BY-SA content can carry attribution and share-alike obligations, so the licence must stay attached.
- **Stored as:** `assets/provenance.yaml`, with a licence report generated from it.

## 6. KiCad integration

`partslib build` generates:

```
build/
  symbols/PL_Resistor.kicad_sym    # one symbol per IPN, with all fields filled in
  symbols/PL_Capacitor.kicad_sym   # ...one file per category
  footprints/PL_*.pretty/
  3dmodels/PL_*.3dshapes/
```

- **Symbols:** one derived symbol per part, named by IPN, with the footprint and these fields filled in: `IPN`, `Manufacturer`, `MPN`, `LCSC`, `Datasheet`, `Description`, and the value. Pick a part once and it is fully defined.
- **Portable paths:** 3D model paths use one variable, `${PARTSLIB}`. No machine-specific absolute paths or versioned variables like `KICAD8_3RD_PARTY`.
- **Setup:** `partslib install` registers the libraries and sets `${PARTSLIB}`. It refuses to run while KiCad is open and backs up the config first.
- **Stable names:** a generated library name or IPN never changes, so existing schematics keep resolving.
- **Generated files stay out of normal editing:** a change made to `build/` directly is overwritten, and `partslib check` reports it.
- **Stock KiCad parts:** allowed by importing them as normal parts, so they get an IPN and stop depending on KiCad's bundled versions.

**Boards at release.** `partslib vendor <board>` copies the board's parts into its project `lib/` folder and rewrites its library table, so the board is self-contained (goal 6). This matches the rule in the SGC SOP that libraries a design depends on live in `hardware/<designator>/lib/`.

## 7. Board tracking

Boards are registered in `boards.yaml`, by path and git remote:

```yaml
- name: DEV1_50_2_SGC-LP
  path: ~/code/sgc/hardware/DEV1_50_2_SGC-LP
  remote: github.com/yasimbt/DEV1-SGC-Starter-Generator-Controller
  project: SGC
  status: in_design
```

The scanner reads each board's `.kicad_sch` and `.kicad_pcb` files, plus `kicad-cli sch export bom` where useful. It records results by git commit, so history can be compared. It must expand per-instance references: on reused sheets the `Reference` property is not the real designator, which produced false results in the manual audit of this board.

Checks per board:

| Check | Severity |
|---|---|
| Part has no IPN, or an unknown IPN | error at release, warning in design |
| Schematic part missing from the PCB, or the reverse | warning |
| Footprint differs between schematic and PCB | error |
| Non-power symbol has no footprint | error |
| 3D model path does not resolve, or no model is assigned | warning |
| Library reference resolves only on this machine | error at release |
| Part is `deprecated` or `obsolete`, or no MPN is `active` | warning; error if `obsolete` |
| Two IPNs with the same function on one board | warning |
| Board can be opened from a fresh checkout | required for release |

Each check also needs a test showing that it can fail on a bad board.

## 8. Dashboard

Local web app at `http://127.0.0.1:<port>`, started with `partslib serve`.

| Page | Contents |
|---|---|
| **Overview** | Part counts by status and category, board health, and parts needing attention |
| **Parts** | Search and filter by any parameter, such as "1 kΩ, 0603, ≤ 1%". Rendered symbol and footprint previews (`kicad-cli sym export svg`, `fp export svg`) |
| **Part detail** | Parameters, MPNs, datasheet, provenance, history from git, and **where used**: boards, quantities, designators |
| **New part** | Form that runs the duplicate check as you type, and shows existing matches first |
| **Boards** | All boards, with status, part count, percentage of parts with an IPN, and check results |
| **Board detail** | BOM grouped by IPN, failed checks with the designators involved, and changes between commits. CSV export |
| **Migration** | Unmatched parts on a board, with suggested IPNs (section 9) |

Dashboard edits write the YAML files and create a git commit, so the history stays complete. It binds to localhost only.

## 9. Migrating existing boards

Existing parts are matched, not forced. Editing live board files is out of scope for v1.

1. Scan the board and group its parts by value, footprint and source.
2. Parse values from existing symbols. For example, `JLCPCB_SYMBOL_DIR:0603,100kΩ` gives a 100 kΩ 0603 resistor; other parameters are still needed.
3. Suggest an existing IPN, or a draft part if none exists.
4. A person confirms each match, and the tool prints the changes to make in KiCad.

**First import:** the parts on `DEV1_50_2_SGC-LP`, about 22 symbol types. Priorities are the 0603 passives, then C0805, `D_SMP`, EPC23108, STM32G474 and MCS1823. Do not bulk-import whole third-party libraries; that would bring back the problem in section 1.

The power stage is the EPC23108 (SGC DL-25), but the board draws it with KiCad's EPC23102 symbol. It cannot be `released` until pin compatibility (SGC T-36) and its footprint (SGC T-12) are checked against the datasheet.

## 10. Later

- Live stock and pricing from LCSC, DigiKey and Mouser APIs, with lifecycle alerts.
- Build costing for a quantity, and JLCPCB assembly files.
- Inventory of parts on the shelf.
- Writing IPN changes back to schematics, once there is a safe workflow.
- HTTP library output, if browsing live data in KiCad becomes useful.
- Other generators, if another EDA tool is ever needed.

## 11. Repository layout

```
partslib/
  spec.md
  parts/<category>/<IPN>.yaml
  categories/<category>.yaml       # parameters, identity rules, units
  assets/symbols/                  # base symbols
  assets/footprints/
  assets/3dmodels/                 # git LFS
  assets/provenance.yaml
  boards.yaml
  build/                           # generated; not committed
  src/partslib/                    # CLI, generator, scanner, web app
  tests/fixtures/                  # small KiCad projects, including deliberately broken ones
  flake.nix                        # pinned environment
```

CLI: `check`, `build`, `install`, `add`, `scan`, `vendor`, `serve`.

## 12. Milestones

Each one is finished when its acceptance test passes.

| # | Deliverable | Acceptance test |
|---|---|---|
| M0 | Spike: generate one derived symbol and footprint, and use them in KiCad 10 | A generated part can be placed from KiCad, with all fields present, and shows in the 3D viewer on a clean config |
| M1 | Part schema, `check`, and duplicate detection | Adding `1k`, `1000` and `1K0` versions of the same resistor is rejected; each validation rule has a failing fixture |
| M2 | `build` and `install`, with 10 real passives | A new KiCad project can use them with no manual library setup |
| M3 | Board scanner and checks, command line only | Scanning `DEV1_50_2_SGC-LP` at a pinned commit reports the known problems in section 1, without the false results from per-instance references |
| M4 | Dashboard, read-only: parts, boards, where-used | Finding "which boards use this part?" takes under 10 seconds |
| M5 | Dashboard editing and the new-part flow | A part added in the browser produces one git commit and passes `check` |
| M6 | Migrate `DEV1_50_2_SGC-LP` | Every part on the board has an IPN, and the board opens from a fresh clone with no extra setup |
| M7 | `vendor` | A vendored board opens correctly on a machine without partslib installed |

M0 comes first because D3 depends on it. If generated libraries prove unworkable, revisit D3 before building more.

## 13. Risks

| Risk | Mitigation |
|---|---|
| KiCad file format changes between major versions | Generate through one module, pin the KiCad version in the flake, and test with `kicad-cli sym upgrade` |
| Adding parts is too slow, so it gets bypassed | New-part flow takes under 2 minutes for a passive; the commodity categories can be templated |
| Scope grows into a full PLM system | Keep to the milestones. Section 10 waits until M6 is finished and the tool has been used on a real board |
| A wrong footprint spreads to every project | `released` requires a datasheet check, and a fix reaches every board that uses it. A single library concentrates this risk as well as the benefit |
| Regenerating libraries changes existing boards | KiCad keeps embedded copies in the design files, so changes only arrive through an explicit library update; generated output must be stable and diff cleanly |

## 14. Open questions

1. Should the repo be private on GitHub? Check licence obligations for third-party parts if it is ever public.
2. Should passives be pre-populated from a standard series (E24 at 0402, 0603 and 0805) or added only as needed? Default: as needed.
3. What are the default part preferences, for example JLCPCB basic parts first?
4. Should symbols follow one visual style, AKL or KiCad stock? Default: KiCad stock style.
5. Is `partslib` the right name?
