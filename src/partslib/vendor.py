"""`partslib vendor`: copy a board's library parts into its own lib/ folder (spec section 6).

A vendored board opens on a machine without partslib. Into <board>/lib/partslib/ go the symbols,
footprints and 3D models the board uses, plus a provenance file for their licences. The board's
own library tables point its PL_ libraries there, and its .kicad_pro defines the text variable
PARTSLIB as ${KIPRJMOD}/lib/partslib, so the ${PARTSLIB}/... model paths already in the PCB file
resolve without editing it. KiCad gives a project text variable precedence over the environment
and its own config, so a vendored board uses its own copies even where partslib is installed.

This is the one command that writes to a board: lib/partslib/, the two library tables and the
.kicad_pro. It never commits there; the board's repository is the user's.
"""

from collections import defaultdict
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import shutil
import subprocess

import yaml

from partslib import kicadgen
from partslib.board import BoardEntry, Source, load_board
from partslib.build import BUILD_DIR, read_manifest
from partslib.check import RETIRED, check_library, check_records, load_provenance
from partslib.install import InstallError, running_kicad, update_table
from partslib.library import load_categories, load_yaml, part_paths

VENDOR_DIR = "lib/partslib"
OWNED_PREFIX = "${KIPRJMOD}/" + VENDOR_DIR + "/"
TEXT_VARIABLE_VALUE = "${KIPRJMOD}/" + VENDOR_DIR


class VendorError(Exception):
    pass


@dataclass
class VendorResult:
    board: Path
    written: list[str] = field(default_factory=list)  # paths relative to the board
    changed: list[str] = field(default_factory=list)  # board files that differ from before
    symbols: int = 0
    footprints: int = 0
    models: int = 0
    not_vendored: list[str] = field(default_factory=list)  # libraries the board uses that partslib does not own


def _pl(lib_id: str) -> tuple[str, str] | None:
    if not lib_id.startswith("PL_") or ":" not in lib_id:
        return None
    library, name = lib_id.split(":", 1)
    return library, name


def _asset_map(root: Path) -> dict[str, dict]:
    """For each generated file, the source asset it came from and which parts use it."""
    categories = load_categories(root)
    sources = {p.as_posix(): (root / p).read_bytes() for p in part_paths(root)}
    records, _ = check_records(root, sources, categories)
    origins: dict[str, dict] = {}
    for record in records:
        if record.status not in ("released", *RETIRED) or record.category is None:
            continue
        raw = record.raw
        footprint_library, footprint_name = raw["footprint"].split(":", 1)
        model_library, model_file = raw["model"].split("/", 1)
        for generated, kind, asset in (
            (f"symbols/{record.category.library}.kicad_sym#{record.ipn}", "symbol", raw["symbol"]),
            (f"footprints/PL_{footprint_library}.pretty/{footprint_name}.kicad_mod", "footprint", raw["footprint"]),
            (f"3dmodels/PL_{model_library}.3dshapes/{model_file}", "model", raw["model"]),
        ):
            entry = origins.setdefault(generated, {"kind": kind, "asset": asset, "parts": set()})
            entry["parts"].add(record.ipn)
    return origins


def _uncommitted(board: Path, paths: list[str]) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(board), "status", "--porcelain", "--", *paths], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        return []  # not a git repository: nothing to protect
    return [line[3:] for line in result.stdout.splitlines() if line.strip()]


def plan(root: Path, entry: BoardEntry) -> tuple[dict[str, bytes], VendorResult]:
    """Every file vendoring would write, relative to the board, and a summary."""
    result = VendorResult(entry.path)
    _count, findings = check_library(root)
    if findings:
        raise VendorError(f"`partslib check` reports {len(findings)} problems; fix them and run `partslib build` first")
    if read_manifest(root) is None:  # an empty build is fine here: the missing parts are named below
        raise VendorError("nothing is built; run `partslib build` first")
    board = load_board(entry.name, Source(entry.path, None))
    build = root / BUILD_DIR

    symbols: dict[str, set[str]] = defaultdict(set)
    footprints: set[tuple[str, str]] = set()
    models: set[str] = set()
    others: set[str] = set()
    for placement in board.placements:
        if placement.power:
            continue
        for lib_id, kind in ((placement.lib_id, "symbol"), (placement.footprint, "footprint")):
            owned = _pl(lib_id)
            if owned:
                (symbols[owned[0]].add(owned[1]) if kind == "symbol" else footprints.add(owned))
            elif ":" in lib_id:
                others.add(f"{kind} library {lib_id.split(':', 1)[0]}")
    for footprint in board.footprints:
        owned = _pl(footprint.lib_id)
        if owned:
            footprints.add(owned)
        elif ":" in footprint.lib_id:
            others.add(f"footprint library {footprint.lib_id.split(':', 1)[0]}")
        for model in footprint.models:
            if model.startswith("${PARTSLIB}/"):
                models.add(model.removeprefix("${PARTSLIB}/"))
    result.not_vendored = sorted(others)

    files: dict[str, bytes] = {}
    missing: list[str] = []
    for library, names in sorted(symbols.items()):
        path = build / "symbols" / f"{library}.kicad_sym"
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        blocks = []
        for name in sorted(names):
            try:
                blocks.append(kicadgen.extract_symbol(text, name))
            except KeyError:
                missing.append(f"symbol {library}:{name}")
        if blocks:
            header = text[: text.index("\t(symbol ")] if "\t(symbol " in text else "(kicad_symbol_lib\n"
            files[f"{VENDOR_DIR}/symbols/{library}.kicad_sym"] = (header + "".join(f"\t{b}\n" for b in blocks) + ")\n").encode()
            result.symbols += len(blocks)
    for library, name in sorted(footprints):
        path = build / "footprints" / f"{library}.pretty" / f"{name}.kicad_mod"
        if not path.is_file():
            missing.append(f"footprint {library}:{name}")
            continue
        data = path.read_bytes()
        files[f"{VENDOR_DIR}/footprints/{library}.pretty/{name}.kicad_mod"] = data
        result.footprints += 1
        models |= {m.removeprefix("${PARTSLIB}/") for m in re.findall(r'\(model "(\$\{PARTSLIB\}/[^"]*)"', data.decode("utf-8"))}
    for model in sorted(models):
        path = build / model
        if not path.is_file():
            missing.append(f"3D model {model}")
            continue
        files[f"{VENDOR_DIR}/{model}"] = path.read_bytes()
        result.models += 1
    if missing:
        raise VendorError(
            "not in the built libraries, so the part is not released or the build is old: " + ", ".join(missing)
        )
    if not files:
        raise VendorError("the board uses no partslib parts yet; migrate it first (`partslib migrate`)")

    # Provenance: the licences of what is copied travel with it.
    origins = _asset_map(root)
    known = {(e["kind"], e["id"]): e for e in _provenance_entries(root)}
    entries = []
    for path in sorted(files):
        generated = path.removeprefix(f"{VENDOR_DIR}/")
        keys = [generated] if not generated.startswith("symbols/") else [
            f"{generated}#{m}" for m in re.findall(r'^\t\(symbol "([^"]+)"', files[path].decode("utf-8"), re.MULTILINE)
        ]
        for key in keys:
            origin = origins.get(key)
            if origin is None:
                continue
            source = known.get((origin["kind"], origin["asset"]), {})
            entries.append({
                "file": key.replace("#", " symbol "),
                "parts": " ".join(sorted(origin["parts"])),
                "asset": origin["asset"],
                "source": source.get("source", "unknown"),
                "licence": source.get("licence", "NOASSERTION"),
                "version": source.get("version", "unknown"),
            })
    commit = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False).stdout.strip()
    files[f"{VENDOR_DIR}/provenance.yaml"] = (
        "# Library parts copied into this board by `partslib vendor`, with their sources and licences.\n"
        f"# From partslib at {commit or 'an uncommitted state'}. Do not edit: run `partslib vendor` again instead.\n"
        + yaml.safe_dump({"vendored": entries}, sort_keys=False, allow_unicode=True, width=1000)
    ).encode()

    # Library tables and the project's PARTSLIB text variable.
    vendored_libraries = {
        "sym-lib-table": {Path(p).stem for p in files if p.startswith(f"{VENDOR_DIR}/symbols/")},
        "fp-lib-table": {Path(p).parent.name.removesuffix(".pretty") for p in files if p.startswith(f"{VENDOR_DIR}/footprints/")},
    }
    for table, kind, folder, suffix in (
        ("sym-lib-table", "sym_lib_table", "symbols", ".kicad_sym"),
        ("fp-lib-table", "fp_lib_table", "footprints", ".pretty"),
    ):
        libraries = {name: f"{OWNED_PREFIX}{folder}/{name}{suffix}" for name in sorted(vendored_libraries[table])}
        current = (entry.path / table).read_text(encoding="utf-8") if (entry.path / table).is_file() else None
        try:
            files[table] = update_table(current, kind, libraries, None, owned_prefix=OWNED_PREFIX).encode()
        except InstallError as error:
            raise VendorError(str(error)) from None
    project_file = f"{board.project}.kicad_pro"
    files[project_file] = _with_text_variable(entry.path / project_file).encode()
    return files, result


def _provenance_entries(root: Path) -> list[dict]:
    path = root / "assets/provenance.yaml"
    if not path.is_file():
        return []
    entries = load_yaml(path.read_text(encoding="utf-8")) or []
    load_provenance(root, [])  # malformed entries are check's to report
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def _with_text_variable(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    variables = data.get("text_variables")
    if not isinstance(variables, dict):
        variables = {}
    current = variables.get("PARTSLIB")
    if current not in (None, TEXT_VARIABLE_VALUE):
        raise VendorError(f"{path.name} already sets PARTSLIB to {current!r}; remove it first")
    variables["PARTSLIB"] = TEXT_VARIABLE_VALUE
    data["text_variables"] = variables
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def vendor(root: Path, entry: BoardEntry, dry_run: bool = False, force: bool = False,
           kicad_processes=running_kicad) -> VendorResult:
    running = kicad_processes()
    if running and not dry_run:
        raise VendorError(f"close KiCad first ({', '.join(running)} running): it would overwrite the project file")
    files, result = plan(root, entry)
    board = entry.path
    touched = [p for p in files if not p.startswith(f"{VENDOR_DIR}/")]
    dirty = _uncommitted(board, [*touched, VENDOR_DIR])
    if dirty and not force:
        raise VendorError(
            f"uncommitted changes in {', '.join(dirty)}; commit them first so vendoring is its own change, "
            "or pass --force"
        )
    vendored = board / VENDOR_DIR
    existing = {p.relative_to(board).as_posix(): p.read_bytes() for p in vendored.rglob("*") if p.is_file()} if vendored.is_dir() else {}
    for path, data in files.items():
        current = existing.get(path) if path.startswith(f"{VENDOR_DIR}/") else ((board / path).read_bytes() if (board / path).is_file() else None)
        if current != data:
            result.changed.append(path)
    result.changed += sorted(p for p in existing if p not in files)  # vendored files no longer used
    result.written = sorted(files)
    if dry_run:
        return result
    if vendored.is_dir():
        shutil.rmtree(vendored)  # rebuilt whole, so parts no longer used go away
    for path, data in files.items():
        target = board / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return result
