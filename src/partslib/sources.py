"""Where new parts can be imported from: search, then fetch a symbol, footprint and 3D model.

Sources:
- `kicad`: KiCad's installed libraries. Offline; CC-BY-SA-4.0 with KiCad's exception.
- `lcsc`: the JLCPCB/LCSC catalogue for search, and EasyEDA's CAD data, converted by
  easyeda2kicad (AGPL-3.0, run as a separate program, so its licence does not reach this code).
  Both are unofficial APIs that can change without notice, and the content's licence is unknown,
  so its provenance says NOASSERTION.

SnapMagic and Ultra Librarian have no self-serve download API; a source for either would follow
the same `search` and `fetch` shape once an API key is available.
"""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

from partslib import kicadgen
from partslib.scan import KICAD_CLI, SHARED

JLCPCB_SEARCH = "https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList"
KICAD_LICENCE = "CC-BY-SA-4.0 WITH KiCad-libraries-exception"
LCSC_CODE = re.compile(r"C\d{3,}", re.IGNORECASE)


class SourceError(Exception):
    pass


@dataclass
class Hit:
    source: str
    id: str  # what fetch takes: Lib:Name for KiCad, an LCSC code for LCSC
    name: str
    manufacturer: str = ""
    mpn: str = ""
    package: str = ""
    description: str = ""
    datasheet: str = ""
    footprint: str = ""  # suggested footprint, when the source has one
    extra: dict = field(default_factory=dict)


@dataclass
class Fetched:
    """A part's assets as fetched, in KiCad 10 format, plus suggestions for its part file."""

    source: str
    symbol_library: str  # asset library to put the symbol in
    symbol_name: str
    symbol_block: str  # one complete symbol
    footprint_library: str
    footprint_name: str
    footprint_text: str
    model_name: str | None  # file name, such as R0603.step
    model_bytes: bytes | None
    provenance: dict[str, str]  # source, licence, version
    suggestion: dict  # category, params, manufacturer, mpn, lcsc, datasheet, description


def _kicad_version() -> str:
    if not Path(KICAD_CLI).exists():
        return "unknown"
    output = subprocess.run([str(KICAD_CLI), "version"], capture_output=True, text=True, check=False).stdout
    return output.strip() or "unknown"


def _private_env(work: Path) -> dict[str, str]:
    config = work / ".kicad-config"
    config.mkdir(parents=True, exist_ok=True)
    return {**os.environ, "KICAD_CONFIG_HOME": str(config)}


# --- KiCad's installed libraries ---------------------------------------------------------------

_SYMBOL_START = re.compile(r'^\t\(symbol "((?:[^"\\]|\\.)*)"', re.MULTILINE)
_PROPERTY = re.compile(r'\(property "(Description|ki_keywords|Footprint|Datasheet)" "((?:[^"\\]|\\.)*)"')


def kicad_index(cache_dir: Path) -> list[dict]:
    """Every stock symbol with its description, keywords and footprint, cached per KiCad install."""
    symbols_dir = SHARED / "symbols"
    if not symbols_dir.is_dir():
        return []
    stamp = f"{_kicad_version()}-{int(symbols_dir.stat().st_mtime)}"
    cache = cache_dir / "kicad-symbols.json"
    if cache.is_file():
        data = json.loads(cache.read_text(encoding="utf-8"))
        if data.get("stamp") == stamp:
            return data["symbols"]
    symbols = []
    for path in sorted(symbols_dir.glob("*.kicad_sym")):
        text = path.read_text(encoding="utf-8")
        starts = list(_SYMBOL_START.finditer(text))
        for index, match in enumerate(starts):
            end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
            body = text[match.start() : end]
            props = dict(_PROPERTY.findall(body))
            symbols.append(
                {
                    "lib": path.stem,
                    "name": match.group(1),
                    "description": props.get("Description", ""),
                    "keywords": props.get("ki_keywords", ""),
                    "footprint": props.get("Footprint", ""),
                    "datasheet": props.get("Datasheet", ""),
                }
            )
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"stamp": stamp, "symbols": symbols}), encoding="utf-8")
    return symbols


def search_kicad(query: str, cache_dir: Path, limit: int = 25) -> list[Hit]:
    words = [w.casefold() for w in query.split() if w.strip()]
    if not words:
        return []
    scored = []
    for symbol in kicad_index(cache_dir):
        name = symbol["name"].casefold()
        haystack = f"{name} {symbol['lib']} {symbol['description']} {symbol['keywords']}".casefold()
        if not all(word in haystack for word in words):
            continue
        whole = " ".join(words)
        rank = 0 if name == whole else 1 if name.startswith(words[0]) else 2 if words[0] in name else 3
        scored.append((rank, len(name), symbol))
    scored.sort(key=lambda item: (item[0], item[1], item[2]["lib"], item[2]["name"]))
    return [
        Hit(
            source="kicad",
            id=f"{s['lib']}:{s['name']}",
            name=s["name"],
            mpn=s["name"] if s["footprint"] else "",
            description=s["description"],
            datasheet=s["datasheet"] if s["datasheet"].startswith("http") else "",
            footprint=s["footprint"],
            extra={"library": s["lib"]},
        )
        for _rank, _length, s in scored[:limit]
    ]


def fetch_kicad(symbol_id: str, footprint_id: str) -> Fetched:
    library, name = symbol_id.split(":", 1)
    path = SHARED / "symbols" / f"{library}.kicad_sym"
    if not path.is_file():
        raise SourceError(f"KiCad has no symbol library {library}")
    try:
        block = kicadgen.complete_symbol(path.read_text(encoding="utf-8"), name)
    except (KeyError, ValueError) as error:
        raise SourceError(str(error)) from None
    if ":" not in footprint_id:
        raise SourceError("choose a footprint, as <library>:<name>, such as Resistor_SMD:R_0603_1608Metric")
    footprint_library, footprint_name = footprint_id.split(":", 1)
    footprint_path = SHARED / "footprints" / f"{footprint_library}.pretty" / f"{footprint_name}.kicad_mod"
    if not footprint_path.is_file():
        raise SourceError(f"KiCad has no footprint {footprint_id}")
    footprint_text = footprint_path.read_text(encoding="utf-8")
    model_name = model_bytes = None
    for uri in re.findall(r'\(model "([^"]*)"', footprint_text):
        relative = re.sub(r"^\$\{KICAD\d+_3DMODEL_DIR\}/", "", uri)
        candidate = SHARED / "3dmodels" / relative
        step = candidate.with_suffix(".step")
        if step.is_file():
            model_name, model_bytes = step.name, step.read_bytes()
            break
    properties = dict(_PROPERTY.findall(block))
    return Fetched(
        source="kicad",
        symbol_library=library,
        symbol_name=name,
        symbol_block=block,
        footprint_library=footprint_library,
        footprint_name=footprint_name,
        footprint_text=footprint_text,
        model_name=model_name,
        model_bytes=model_bytes,
        provenance={"source": "KiCad official libraries, as installed with KiCad", "licence": KICAD_LICENCE,
                    "version": _kicad_version()},
        suggestion={
            "description": properties.get("Description", ""),
            "datasheet": properties.get("Datasheet", "") if properties.get("Datasheet", "").startswith("http") else "",
            "mpn": name if properties.get("Footprint") else "",
        },
    )


# --- LCSC / JLCPCB / EasyEDA --------------------------------------------------------------------

CATEGORY_HINTS = [  # (words in the catalogue category, partslib category)
    ("ferrite", "ferrite"), ("resistor", "resistor"), ("capacitor", "capacitor"),
    ("inductor", "inductor"), ("diode", "diode"), ("mosfet", "fet"), ("transistor", "fet"),
    ("connector", "connector"), ("header", "connector"),
]
PARAM_ATTRIBUTES = {  # partslib param -> catalogue attribute names
    "resistance_ohm": ("Resistance",),
    "tolerance_pct": ("Tolerance",),
    "power_w": ("Power(Watts)", "Power"),
    "voltage_v": ("Overload Voltage (Max)", "Voltage Rated", "Voltage Rating"),
    "capacitance_f": ("Capacitance",),
    "dielectric": ("Temperature Coefficient",),
    "inductance_h": ("Inductance",),
    "current_a": ("Rated Current", "Current Rating", "Current Rating (Max)"),
    "impedance_ohm": ("Impedance @ Frequency",),
}


def _jlcpcb_search(keyword: str, page_size: int = 10) -> list[dict]:
    payload = json.dumps({"keyword": keyword, "currentPage": 1, "pageSize": page_size}).encode()
    request = urllib.request.Request(  # noqa: S310 - fixed https URL
        JLCPCB_SEARCH,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "partslib",
                 "Origin": "https://jlcpcb.com", "Referer": "https://jlcpcb.com/parts"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            raw = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise SourceError(f"the JLCPCB parts search did not answer: {error}") from None
    return ((raw.get("data") or {}).get("componentPageInfo") or {}).get("list") or []


def _brand(brand: str) -> str:
    """UNI-ROYAL(Uniroyal Elec) -> UNI-ROYAL."""
    short = re.sub(r"\s*\(.*\)\s*$", "", brand).strip()
    return short or brand.strip()


def _lcsc_hit(item: dict) -> Hit:
    attributes = {a.get("attribute_name_en", ""): a.get("attribute_value_name", "") for a in item.get("attributes") or []}
    return Hit(
        source="lcsc",
        id=item.get("componentCode", ""),
        name=item.get("componentModelEn", ""),
        manufacturer=_brand(item.get("componentBrandEn", "")),
        mpn=item.get("componentModelEn", ""),
        package=item.get("componentSpecificationEn", ""),
        description=item.get("describe", ""),
        datasheet=item.get("dataManualUrl", "") or "",
        extra={
            "category": f"{item.get('firstSortName', '')} {item.get('componentTypeEn', '')}".strip(),
            "attributes": {k: v for k, v in attributes.items() if v and v != "-"},
            "type": "Basic" if item.get("componentLibraryType") == "base" else "Extended",
            "stock": item.get("stockCount", 0),
        },
    )


def search_lcsc(query: str, limit: int = 10) -> list[Hit]:
    query = query.strip()
    if not query:
        return []
    return [_lcsc_hit(item) for item in _jlcpcb_search(query, limit) if item.get("componentCode")]


def _suggest(hit: Hit) -> dict:
    category_text = hit.extra.get("category", "").casefold()
    category = next((c for words, c in CATEGORY_HINTS if words in category_text), "ic")
    attributes = hit.extra.get("attributes", {})
    params = {}
    for param, names in PARAM_ATTRIBUTES.items():
        for name in names:
            if attributes.get(name):
                value = attributes[name].replace("±", "").strip()
                if param == "impedance_ohm" and "@" in value:
                    value, frequency = (v.strip() for v in value.split("@", 1))
                    params["frequency_hz"] = frequency
                params[param] = value
                break
    if category != "capacitor":
        params.pop("dielectric", None)  # for resistors, "Temperature Coefficient" is ppm/°C
    if hit.package:
        params["package"] = hit.package
    description = hit.description.casefold()
    for words, technology in (("thick film", "thick_film"), ("thin film", "thin_film"), ("metal film", "metal_film")):
        if words in description:
            params["technology"] = technology
    return {
        "category": category,
        "params": params,
        "manufacturer": hit.manufacturer,
        "mpn": hit.mpn,
        "lcsc": hit.id,
        "datasheet": hit.datasheet,
        "description": "",
    }


def _safe(name: str) -> str:
    return re.sub(r"[^\w.+-]", "_", name).strip("_") or "part"


def fetch_lcsc(lcsc: str) -> Fetched:
    lcsc = lcsc.strip().upper()
    if not LCSC_CODE.fullmatch(lcsc):
        raise SourceError(f"{lcsc!r} is not an LCSC part number, such as C21190")
    hits = [h for h in search_lcsc(lcsc, 5) if h.id.upper() == lcsc]
    if not hits:
        raise SourceError(f"the JLCPCB catalogue has no part {lcsc}")
    hit = hits[0]
    import importlib.util

    if importlib.util.find_spec("easyeda2kicad") is None:
        raise SourceError("fetching from LCSC needs easyeda2kicad: .venv/bin/pip install -e '.[import]'")
    with tempfile.TemporaryDirectory(prefix="partslib-lcsc-") as work_name:
        work = Path(work_name)
        result = subprocess.run(
            [sys.executable, "-m", "easyeda2kicad", "--full", "--lcsc_id", lcsc, "--output", str(work / "lib")],
            capture_output=True, text=True, cwd=work, check=False, timeout=180,
        )
        symbol_file, pretty = work / "lib.kicad_sym", work / "lib.pretty"
        if not symbol_file.is_file() or not pretty.is_dir() or not list(pretty.glob("*.kicad_mod")):
            detail = (result.stdout + result.stderr).strip().splitlines()[-1:] or ["no output"]
            raise SourceError(f"EasyEDA has no CAD data for {lcsc}: {detail[0]}")
        # easyeda2kicad writes KiCad 5/6 formats; KiCad itself upgrades them to the current one.
        env = _private_env(work)
        for args in (["sym", "upgrade", "--force", str(symbol_file)], ["fp", "upgrade", "--force", str(pretty)]):
            subprocess.run([str(KICAD_CLI), *args], env=env, capture_output=True, check=False, timeout=120)
        text = symbol_file.read_text(encoding="utf-8")
        names = [m.group(1) for m in _SYMBOL_START.finditer(text)]
        if not names:
            raise SourceError(f"the converted symbol for {lcsc} is empty")
        block = kicadgen.complete_symbol(text, names[0])
        footprint_file = sorted(pretty.glob("*.kicad_mod"))[0]
        steps = sorted((work / "lib.3dshapes").glob("*.step")) if (work / "lib.3dshapes").is_dir() else []
        model_name = f"{_safe(steps[0].stem)}.step" if steps else None
        model_bytes = steps[0].read_bytes() if steps else None
        return Fetched(
            source="lcsc",
            symbol_library="LCSC",
            symbol_name=_safe(names[0]),
            symbol_block=block.replace(f'(symbol "{names[0]}"', f'(symbol "{_safe(names[0])}"', 1)
            .replace(f'(symbol "{names[0]}_', f'(symbol "{_safe(names[0])}_'),
            footprint_library="LCSC",
            footprint_name=_safe(footprint_file.stem),
            footprint_text=footprint_file.read_text(encoding="utf-8"),
            model_name=model_name,
            model_bytes=model_bytes,
            provenance={
                "source": f"EasyEDA CAD data for LCSC {lcsc}, converted by easyeda2kicad",
                "licence": "NOASSERTION",
                "version": f"easyeda2kicad {_easyeda2kicad_version()}",
            },
            suggestion=_suggest(hit),
        )


def _easyeda2kicad_version() -> str:
    try:
        from importlib.metadata import version

        return version("easyeda2kicad")
    except Exception:  # noqa: BLE001 - only used as a label
        return "unknown"


def search(query: str, cache_dir: Path, sources: tuple[str, ...] = ("kicad", "lcsc")) -> tuple[list[Hit], list[str]]:
    """Search every source; one failing source does not hide the others' results."""
    hits, problems = [], []
    if "lcsc" in sources:
        try:
            hits += search_lcsc(query)
        except SourceError as error:
            problems.append(f"LCSC: {error}")
    if "kicad" in sources:
        hits += search_kicad(query, cache_dir)
    return hits, problems
