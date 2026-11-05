"""Import a part found by `sources`: stage what was fetched, then add it to the library.

Staging keeps the fetched files under build/import-staging/ until the user confirms, so nothing
reaches assets/ or parts/ without a look at it first. Adding writes the assets, their provenance
and a draft part, checked together and committed as one commit.

Existing assets are never changed: a released part may already use them. A fetched asset with
the same name and the same content is reused; with different content it gets a new name.
"""

from dataclasses import asdict
import json
from pathlib import Path
import re
import secrets
import shutil

from partslib import edit
from partslib.check import load_provenance
from partslib.sources import Fetched, SourceError, fetch_kicad, fetch_lcsc

STAGING = "build/import-staging"
SYMBOL_HEADER = '(kicad_symbol_lib\n\t(version 20251024)\n\t(generator "partslib")\n\t(generator_version "0.0")\n'


def stage(root: Path, source: str, item: str, footprint: str = "") -> str:
    """Fetch a part into staging and return the staging id."""
    if source == "lcsc":
        fetched = fetch_lcsc(item)
    elif source == "kicad":
        fetched = fetch_kicad(item, footprint)
    else:
        raise SourceError(f"unknown source {source!r}")
    stage_id = secrets.token_hex(8)
    directory = root / STAGING / stage_id
    directory.mkdir(parents=True)
    data = asdict(fetched)
    model = data.pop("model_bytes")
    if model is not None:
        (directory / "model.step").write_bytes(model)
    (directory / "fetched.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (directory / "symbol.kicad_sym").write_text(SYMBOL_HEADER + "\t" + fetched.symbol_block + "\n)\n", encoding="utf-8")
    (directory / "footprint.kicad_mod").write_text(fetched.footprint_text, encoding="utf-8")
    return stage_id


def load_stage(root: Path, stage_id: str) -> tuple[Path, Fetched]:
    if not re.fullmatch(r"[0-9a-f]{16}", stage_id or ""):
        raise SourceError("unknown import; search again")
    directory = root / STAGING / stage_id
    if not (directory / "fetched.json").is_file():
        raise SourceError("this import has expired; search again")
    data = json.loads((directory / "fetched.json").read_text(encoding="utf-8"))
    model = directory / "model.step"
    return directory, Fetched(**data, model_bytes=model.read_bytes() if model.is_file() else None)


def _symbol_names(text: str) -> dict[str, str]:
    from partslib import kicadgen

    names = re.findall(r'^\t\(symbol "((?:[^"\\]|\\.)*)"', text, re.MULTILINE)
    return {name: kicadgen.extract_symbol(text, name) for name in names}


def _unused(name: str, taken, suffix: str) -> str:
    candidate, counter = f"{name}_{suffix}", 2
    while candidate in taken:
        candidate, counter = f"{name}_{suffix}_{counter}", counter + 1
    return candidate


def plan_assets(root: Path, fetched: Fetched, suffix: str) -> tuple[dict[str, str], dict[str, bytes]]:
    """Return the asset references the part should use, and the files to write for them."""
    from partslib import kicadgen

    files: dict[str, bytes] = {}
    references: dict[str, str] = {}
    provenance = load_provenance(root, [])
    new_entries = []

    # Symbol: one library file per source library, with this symbol added if it is new.
    library_path = f"assets/symbols/{fetched.symbol_library}.kicad_sym"
    existing_text = (root / library_path).read_text(encoding="utf-8") if (root / library_path).is_file() else None
    existing = _symbol_names(existing_text) if existing_text else {}
    name, block = fetched.symbol_name, fetched.symbol_block
    if name in existing and existing[name] != block:
        new_name = _unused(name, existing, suffix)
        block = re.sub(rf'\(symbol "{re.escape(name)}(_\d+_\d+)?"', lambda m: f'(symbol "{new_name}{m.group(1) or ""}"', block)
        name = new_name
    if name not in existing:
        text = existing_text or SYMBOL_HEADER + ")\n"
        close = text.rstrip().rindex(")")
        files[library_path] = (text[:close].rstrip("\n") + "\n\t" + block + "\n)\n").encode("utf-8")
        kicadgen.extract_symbol(files[library_path].decode("utf-8"), name)  # it must read back
    references["symbol"] = f"{fetched.symbol_library}:{name}"

    # Footprint.
    pretty = f"assets/footprints/{fetched.footprint_library}.pretty"
    footprint, text = fetched.footprint_name, fetched.footprint_text
    taken = {p.stem for p in (root / pretty).glob("*.kicad_mod")} if (root / pretty).is_dir() else set()
    if footprint in taken and (root / pretty / f"{footprint}.kicad_mod").read_text(encoding="utf-8") != text:
        new_name = _unused(footprint, taken, suffix)
        text = re.sub(r'^\(footprint "[^"]*"', f'(footprint "{new_name}"', text, count=1)
        footprint = new_name
    if footprint not in taken:
        files[f"{pretty}/{footprint}.kicad_mod"] = text.encode("utf-8")
    references["footprint"] = f"{fetched.footprint_library}:{footprint}"

    # 3D model, if the source had one.
    if fetched.model_name and fetched.model_bytes is not None:
        directory = f"assets/3dmodels/{fetched.footprint_library}"
        stem = Path(fetched.model_name).stem
        model_name = f"{stem}.step"
        path = root / directory / model_name
        if path.is_file() and path.read_bytes() != fetched.model_bytes:
            taken_models = {p.stem for p in (root / directory).glob("*.step")}
            model_name = f"{_unused(stem, taken_models, suffix)}.step"
            path = root / directory / model_name
        if not path.is_file():
            files[f"{directory}/{model_name}"] = fetched.model_bytes
        references["model"] = f"{fetched.footprint_library}/{model_name}"

    for kind in ("symbol", "footprint", "model"):
        if kind in references and (kind, references[kind]) not in provenance:
            new_entries.append({"kind": kind, "id": references[kind], **fetched.provenance})
    if new_entries:
        path = root / "assets/provenance.yaml"
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        text = re.sub(r"^\[\]\s*$", "", text, flags=re.MULTILINE).rstrip("\n")
        lines = [
            "- {" + ", ".join(f"{key}: {json.dumps(entry[key], ensure_ascii=False)}" for key in ("kind", "id", "source", "licence", "version")) + "}"
            for entry in new_entries
        ]
        files["assets/provenance.yaml"] = ((text + "\n" if text else "") + "\n".join(lines) + "\n").encode("utf-8")
    return references, files


def add(root: Path, stage_id: str, category: str, fields: dict[str, str], params: dict[str, str],
        mpns: list[dict[str, str]], acknowledge: list[str]) -> tuple[str, str]:
    """Add a staged part as a draft. Returns (IPN, commit)."""
    directory, fetched = load_stage(root, stage_id)
    path, part = edit.draft_part(root, category, fields, params, mpns, acknowledge)
    suffix = (fetched.suggestion.get("lcsc") or part["ipn"]).upper()
    references, files = plan_assets(root, fetched, suffix)
    part.update(references)
    if "model" not in references:
        part.pop("model", None)
    source_note = {"lcsc": f"Imported from LCSC {fetched.suggestion.get('lcsc', '')} via EasyEDA",
                   "kicad": "Imported from KiCad's libraries"}[fetched.source]
    notes = "; ".join(n for n in (part.get("notes", ""), source_note) if n)
    part["notes"] = notes
    commit = edit.save(root, path, part, f"Import {part['ipn']}: {part['description']}", extra=files)
    shutil.rmtree(directory, ignore_errors=True)
    return part["ipn"], commit
