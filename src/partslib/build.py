"""`partslib build`: generate KiCad libraries from released parts (spec section 6).

Output goes to build/symbols, build/footprints and build/3dmodels, with build/manifest.json recording
a hash of every generated file, so `check` can tell when one has been edited by hand.
"""

from collections import defaultdict
import hashlib
import json
from pathlib import Path
import shutil
import tempfile

from partslib import kicadgen
from partslib.check import RETIRED, Assets, Finding, Record, check_library, check_records
from partslib.library import load_categories, part_paths
from partslib.kicadgen import Part

BUILD_DIR = "build"
GENERATED_DIRS = ("symbols", "footprints", "3dmodels")
MANIFEST = "manifest.json"
BUILT_STATUSES = ("released", *RETIRED)  # retired parts stay available so old boards keep working
LFS_POINTER = b"version https://git-lfs.github.com/spec/"


class BuildError(Exception):
    pass


def _part(record: Record) -> Part:
    raw = record.raw
    mpn = record.mpns[0]  # preference order
    description = raw["description"]
    if record.status in RETIRED:
        replacement = raw.get("superseded_by")
        note = record.status.upper() + (f", use {replacement}" if replacement else "")
        description = f"{note}: {description}"
    footprint_library, footprint_name = raw["footprint"].split(":", 1)
    return Part(
        ipn=record.ipn,
        value=record.category.render_value(record.normalised, mpn["mpn"]),
        description=description,
        datasheet=raw["datasheet"],
        footprint=f"PL_{footprint_library}:{footprint_name}",
        manufacturer=mpn["manufacturer"],
        mpn=mpn["mpn"],
        lcsc=str(mpn.get("lcsc") or ""),
    )


def generate(root: Path, records: list[Record]) -> dict[str, bytes]:
    """Return every generated file, keyed by its path under build/. Records must already pass check."""
    assets = Assets(root)
    symbols: dict[str, list[tuple[str, str, Part]]] = defaultdict(list)
    footprint_models: dict[str, str] = {}
    for record in sorted(records, key=lambda r: r.ipn):
        if record.status not in BUILT_STATUSES:
            continue
        symbol = record.raw["symbol"]
        symbols[record.category.library].append(
            (symbol.split(":", 1)[1], assets.symbol_block(symbol), _part(record))
        )
        footprint, model = record.raw["footprint"], record.raw["model"]
        if footprint_models.setdefault(footprint, model) != model:
            raise BuildError(
                f"{record.ipn} gives footprint {footprint} model {model}, "
                f"but another part gives it {footprint_models[footprint]}"
            )

    files: dict[str, bytes] = {}
    for library, entries in symbols.items():
        try:
            files[f"symbols/{library}.kicad_sym"] = kicadgen.symbol_library(entries).encode()
        except ValueError as error:
            raise BuildError(f"{library}: {error}") from None
    for footprint, model in footprint_models.items():
        footprint_library, footprint_name = footprint.split(":", 1)
        model_library, model_file = model.split("/", 1)
        model_path = f"3dmodels/PL_{model_library}.3dshapes/{model_file}"
        try:
            text = kicadgen.footprint_file(
                assets.footprint_text(footprint), footprint_name, "${PARTSLIB}/" + model_path
            )
        except ValueError as error:
            raise BuildError(f"footprint {footprint}: {error}") from None
        files[f"footprints/PL_{footprint_library}.pretty/{footprint_name}.kicad_mod"] = text.encode()
        data = (root / "assets/3dmodels" / model).read_bytes()
        if data.startswith(LFS_POINTER):
            raise BuildError(f"model {model} is a git LFS pointer, not the file; run `git lfs pull`")
        files[model_path] = data
    return dict(sorted(files.items()))


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def manifest_text(files: dict[str, bytes]) -> str:
    hashes = {path: digest(data) for path, data in files.items()}
    return json.dumps({"files": hashes}, indent=2) + "\n"


def write(root: Path, files: dict[str, bytes]) -> Path:
    """Replace the generated directories under build/, leaving anything else there alone."""
    build = root / BUILD_DIR
    build.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=build, prefix=".partslib-") as staging_name:
        staging = Path(staging_name)
        for path, data in files.items():
            target = staging / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (build / MANIFEST).unlink(missing_ok=True)  # an interrupted build must not look current
        for name in GENERATED_DIRS:
            shutil.rmtree(build / name, ignore_errors=True)
            if (staging / name).exists():
                (staging / name).rename(build / name)
    (build / MANIFEST).write_text(manifest_text(files), encoding="utf-8")
    return build


def read_manifest(root: Path) -> dict[str, str] | None:
    path = root / BUILD_DIR / MANIFEST
    if not path.is_file():
        return None
    try:
        files = json.loads(path.read_text(encoding="utf-8"))["files"]
    except (ValueError, KeyError, TypeError):
        return {}
    return files if isinstance(files, dict) else {}


def built_files(root: Path) -> dict[str, str]:
    """Hash every file currently in the generated directories."""
    build = root / BUILD_DIR
    hashes = {}
    for name in GENERATED_DIRS:
        for path in sorted((build / name).rglob("*")):
            if path.is_file():
                hashes[path.relative_to(build).as_posix()] = digest(path.read_bytes())
    return hashes


def build_library(root: Path) -> tuple[list[Finding], dict[str, bytes]]:
    """Check the library and, only if it passes, regenerate build/. Returns findings and files written."""
    _count, findings = check_library(root, include_build_dir=False)
    if findings:
        return findings, {}
    categories = load_categories(root)
    sources = {path.as_posix(): (root / path).read_bytes() for path in part_paths(root)}
    records, findings = check_records(root, sources, categories)
    assert not findings, "check_library passed, so the records must too"
    files = generate(root, records)
    write(root, files)
    return [], files
