# partslib

Personal, single-source KiCad 10 parts library with a local dashboard. `spec.md` is the source of truth for scope and design; read the relevant section before implementing, and change the spec first if a decision changes.

## Status

M0 is done: generated libraries work in KiCad 10, so D3 stands. All other milestones are open, and CLI commands exit 1 until their milestone is built. `src/partslib/kicadgen.py` holds the generator, and `.venv/bin/python spikes/m0/run.py` checks its output against KiCad without the GUI. Work follows the milestones in spec section 12, each finished when its acceptance test passes.

## Commands

```sh
nix develop                # python, git-lfs, kicad-cli; flake files must be tracked by git to be seen
git lfs install --local    # once per clone, before committing any 3D model
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/partslib --help
```

nixpkgs marks KiCad as broken on Darwin, so on macOS the dev shell uses `/Applications/KiCad/KiCad.app` and its version is not pinned by the flake. On Linux the flake provides KiCad.

## Rules that are easy to break

- Part YAML files in git are the record. The SQLite index and `build/` are generated and can be deleted at any time.
- IPNs and generated library names never change and are never reused. Parts are never deleted.
- A released part's identity parameters cannot change: create a new part and set `superseded_by`.
- Designing in KiCad must not need the dashboard or any server running.
- v1 reads boards and never writes to them, from the dashboard or the scanner.
- 3D model paths use only `${PARTSLIB}`: no absolute paths or versioned variables like `KICAD8_3RD_PARTY`.
- Generated output must be stable and diff cleanly. All KiCad file generation goes through one module.
- KiCad resolves `extends` only within one file, so each generated `.kicad_sym` carries its base symbols.
- Copy third-party parts in individually and record source, licence and version in `assets/provenance.yaml`. Never bulk-import a library.
- The scanner must expand per-instance references: on reused sheets the `Reference` property is not the real designator.
- Every validation rule and board check needs a test with a fixture showing that it can fail.
- Section 10 of the spec waits until M6 is finished. Don't add it early.
