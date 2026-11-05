# partslib

Personal, single-source KiCad 10 parts library with a local dashboard. `spec.md` is the source of truth for scope and design; read the relevant section before implementing, and change the spec first if a decision changes.

## Status

M0 and M1 are done. M2 (`build`, `install`) is built, but its acceptance is pending: the 12 imported 0603 passives stay drafts until the user checks them through the M5 dashboard, so `build` generates nothing from the real library yet. M3 and M4 must work with draft parts. M3 (`scan`) is built; its acceptance test runs against the real board in `~/code/sgc` at commit `daebef2`. M4 (`serve`, the dashboard in `src/partslib/web/`) and M5 (editing: new part, edit, release, each one commit via `src/partslib/edit.py`) are built. DEV1 shows no where-used until M6 puts IPNs on its symbols. `vendor` exits 1 until M7. `src/partslib/kicadgen.py` holds all KiCad output. `spikes/m0/run.py` and `spikes/m2/acceptance.py` check output against KiCad without the GUI; both run on copies or in their own directories. Work follows the milestones in spec section 12, each finished when its acceptance test passes.

## Commands

```sh
nix develop                # python, git-lfs, kicad-cli; flake files must be tracked by git to be seen
git lfs install --local    # once per clone, before committing any 3D model
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/partslib --help
.venv/bin/partslib serve   # http://127.0.0.1:8765/
```

Git LFS hooks exit with an error when `git-lfs` is not on `PATH`, which makes `git switch` report failure after it has already switched. Run git commands inside `nix develop`.

nixpkgs marks KiCad as broken on Darwin, so on macOS the dev shell uses `/Applications/KiCad/KiCad.app` and its version is not pinned by the flake. On Linux the flake provides KiCad.

## Rules that are easy to break

- Part YAML files in git are the record. The SQLite index and `build/` are generated and can be deleted at any time.
- IPNs and generated library names never change and are never reused. Parts are never deleted.
- A released part's identity parameters cannot change: create a new part and set `superseded_by`.
- Designing in KiCad must not need the dashboard or any server running.
- v1 reads boards and never writes to them, from the dashboard or the scanner.
- 3D model paths use only `${PARTSLIB}`: no absolute paths or versioned variables like `KICAD8_3RD_PARTY`.
- Generated output must be stable and diff cleanly. All KiCad file generation goes through one module.
- Generated symbols are flattened, one complete symbol per part, with no base symbols in the output: a base could be placed without an IPN. Don't switch to `extends`.
- Copy third-party parts in individually and record source, licence and version in `assets/provenance.yaml`. Never bulk-import a library.
- The scanner must expand per-instance references: on reused sheets the `Reference` property is not the real designator. Match schematic parts to PCB footprints by sheet-instance path, never by designator.
- `scan` reads the user's real KiCad config and must never write to it. Board repositories are read-only too: don't commit or change files there.
- Run `git archive` and `git ls-tree` from the repository root, or with `--full-tree`. From a subdirectory they silently restrict output to that directory.
- Every validation rule and board check needs a test with a fixture showing that it can fail. `tests/test_check.py` enforces this for every code in `check.RULES`.
- `check` must report problems in hand-edited files, never crash on them.
- A stored value must parse back to exactly the same value, or duplicate detection stops working: compare exact Fractions, never floats.
- Never mark a part released, or set `verified` or `footprint_checked`, without the user having checked it against the datasheet. Those fields are the user's attestation.
- Never run `partslib install` against the user's real KiCad config without asking: test with `--config-dir` on a copy.
- The dashboard binds to 127.0.0.1 only. Every POST route reads its form through `posted()`, which checks the per-run token; keep it that way for new routes. Edits go through `edit.save`, which checks, writes and commits exactly one file.
- `build/` is what KiCad loads after `install`. Don't delete it from scripts or tests; spikes write to their own subdirectories.
- Commit STEP files from inside `nix develop`, so the git LFS filter runs.
- Section 10 of the spec waits until M6 is finished. Don't add it early.
