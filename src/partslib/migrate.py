"""Migrate an existing board onto library parts (spec section 9). Board files are never written.

1. Group the board's parts by symbol, value, footprint and the MPN and LCSC fields they carry.
2. Infer each group's category and values from its fields, symbol name and footprint.
3. Suggest an IPN: same MPN, then same LCSC number, then same identity; list similar parts too.
4. A person confirms each match, recorded in migrations/<board>.yaml and committed, and the tool
   lists the changes to make in KiCad: the IPN field to set, symbol by symbol and sheet by sheet.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
import hashlib
from pathlib import Path
import re

import yaml

from partslib.board import Board, BoardEntry, Placement, Source, load_board
from partslib.check import Record, check_records
from partslib.library import Category, fingerprint, load_categories, load_yaml, near_key, normalise_mpn, part_paths

REFERENCE_CATEGORIES = [  # designator prefix -> category, longest first
    ("FB", "ferrite"), ("FL", "ferrite"), ("R", "resistor"), ("C", "capacitor"), ("L", "inductor"),
    ("D", "diode"), ("Q", "fet"), ("U", "ic"), ("IC", "ic"), ("J", "connector"), ("P", "connector"),
]
MPN_FIELDS = ("MPN", "Part", "Manufacturer Part Number", "Manufacturer_Part_Number", "MFR")
LCSC_FIELDS = ("LCSC", "LCSC Part", "LCSC Part #", "JLCPCB Part #")
FIELD_PARAMS = {  # board field -> partslib param
    "Resistance": "resistance_ohm",
    "Capacitance": "capacitance_f",
    "Inductance": "inductance_h",
    "Tolerance": "tolerance_pct",
    "Power(Watts)": "power_w",
    "Voltage Rated": "voltage_v",
    "Voltage rating": "voltage_v",
    "Overload Voltage (Max)": "voltage_v",
    "Temperature Coefficient": "dielectric",
    "Current Rating": "current_a",
    "Rated Current": "current_a",
}
VALUE_PARAMS = {"resistor": "resistance_ohm", "capacitor": "capacitance_f", "inductor": "inductance_h"}
GENERIC_VALUES = {"R", "C", "L", "D", "FB", "Ferrite_Bead", "Ferrite Bead", "~", ""}


@dataclass
class Group:
    key: str
    lib_id: str
    value: str
    footprint: str
    mpn: str
    lcsc: str
    manufacturer: str
    references: list[str]
    symbols: list[dict]  # {sheet_file, uuid, designators}
    current_ipns: set[str]
    category: str
    params: dict[str, str]
    suggestion: dict | None = None  # {ipn, how}
    similar: list[str] = field(default_factory=list)
    confirmed: str | None = None
    status: str = "unmatched"  # done, confirmed, suggested, unmatched, conflict

    @property
    def match_fields(self) -> dict[str, str]:
        return {"lib_id": self.lib_id, "value": self.value, "footprint": self.footprint, "mpn": self.mpn, "lcsc": self.lcsc}


def _first(fields: dict[str, str], names: tuple[str, ...]) -> str:
    return next((fields[n].strip() for n in names if fields.get(n, "").strip()), "")


def _brand(brand: str) -> str:
    return re.sub(r"\s*\(.*\)\s*$", "", brand).strip() or brand.strip()


def _category(references: list[str], fields: dict[str, str], categories: dict[str, Category]) -> str:
    prefix = re.match(r"[A-Za-z]+", references[0] if references else "")
    letters = prefix.group(0).upper() if prefix else ""
    for start, category in REFERENCE_CATEGORIES:
        if letters == start and category in categories:
            return category
    text = fields.get("Category", "").casefold()
    for word, category in (("resistor", "resistor"), ("capacitor", "capacitor"), ("inductor", "inductor"),
                           ("ferrite", "ferrite"), ("diode", "diode")):
        if word in text and category in categories:
            return category
    return "other" if "other" in categories else sorted(categories)[0]


def _package(lib_id: str, footprint: str) -> str:
    for text in (footprint.split(":")[-1], lib_id.split(":")[-1]):
        match = re.search(r"(?<!\d)(0201|0402|0603|0805|1206|1210|1812|2010|2512)(?!\d)", text)
        if match:
            return match.group(1)
    return ""


def infer(units: list[Placement], category: str, categories: dict[str, Category]) -> tuple[dict[str, str], str]:
    """(params as text, MPN) for a group, from the fields its symbols carry."""
    fields = units[0].fields
    definition = categories[category]
    params: dict[str, str] = {}
    for name, param in FIELD_PARAMS.items():
        if fields.get(name, "").strip() and param in definition.params and param not in params:
            params[param] = fields[name].replace("±", "").strip()
    value_param = VALUE_PARAMS.get(category)
    if value_param and value_param not in params and units[0].value not in GENERIC_VALUES:
        params[value_param] = units[0].value
    package = _package(units[0].lib_id, units[0].footprint)
    if package and "package" in definition.params:
        params["package"] = package
    kind = fields.get("Type", "").casefold()
    if "technology" in definition.params:
        for words, technology in (("thick film", "thick_film"), ("thin film", "thin_film"), ("metal film", "metal_film")):
            if words in kind:
                params["technology"] = technology
    mpn = _first(fields, MPN_FIELDS)
    if not mpn and definition.identity == "mpn" and units[0].value not in GENERIC_VALUES and re.search(r"\d", units[0].value):
        mpn = units[0].value  # ICs and diodes usually carry their part number as the value
    return params, mpn


def _normalise(category: Category, params: dict[str, str]) -> dict:
    normalised = {}
    for name, value in params.items():
        param = category.params.get(name)
        if param is None:
            continue
        try:
            normalised[name] = param.normalise(value)
        except ValueError:
            pass
    return normalised


def load_confirmations(root: Path, board: str) -> list[dict]:
    path = root / "migrations" / f"{board}.yaml"
    if not path.is_file():
        return []
    data = load_yaml(path.read_text(encoding="utf-8")) or {}
    matches = data.get("matches") if isinstance(data, dict) else None
    return [m for m in matches or [] if isinstance(m, dict)]


def group_key(fields: dict[str, str]) -> str:
    text = "|".join(fields[k] for k in ("lib_id", "value", "footprint", "mpn", "lcsc"))
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


def plan(root: Path, board: Board) -> list[Group]:
    categories = load_categories(root)
    sources = {p.as_posix(): (root / p).read_bytes() for p in part_paths(root)}
    records, _ = check_records(root, sources, categories)
    parts: dict[str, Record] = {r.ipn: r for r in records if r.ipn}
    by_mpn, by_lcsc, by_identity, by_near = defaultdict(set), defaultdict(set), defaultdict(set), defaultdict(set)
    for record in records:
        for mpn in record.mpns:
            if isinstance(mpn.get("mpn"), str):
                by_mpn[normalise_mpn(mpn["mpn"])].add(record.ipn)
            if mpn.get("lcsc"):
                by_lcsc[str(mpn["lcsc"]).upper()].add(record.ipn)
        if record.category is not None:
            identity = fingerprint(record.category, record.normalised)
            if identity:
                by_identity[identity].add(record.ipn)
            near = near_key(record.category, record.normalised)
            if near:
                by_near[near].add(record.ipn)

    units_by_reference: dict[str, list[Placement]] = defaultdict(list)
    for placement in board.placements:
        if not placement.power and (placement.on_board or placement.in_bom):
            units_by_reference[placement.reference].append(placement)
    grouped: dict[str, list[list[Placement]]] = defaultdict(list)
    meta: dict[str, dict[str, str]] = {}
    for units in units_by_reference.values():
        first = units[0]
        match = {
            "lib_id": first.lib_id,
            "value": first.value,
            "footprint": first.footprint,
            "mpn": _first(first.fields, MPN_FIELDS),
            "lcsc": _first(first.fields, LCSC_FIELDS).upper(),
        }
        key = group_key(match)
        grouped[key].append(units)
        meta[key] = match

    confirmations = {group_key({k: str(c.get(k, "")) for k in ("lib_id", "value", "footprint", "mpn", "lcsc")}): c
                     for c in load_confirmations(root, board.name)}
    groups = []
    for key, members in grouped.items():
        references = sorted((u[0].reference for u in members), key=_natural)
        first = members[0][0]
        category = _category(references, first.fields, categories)
        params, mpn = infer(members[0], category, categories)
        symbols: dict[tuple[str, str], list[str]] = defaultdict(list)
        for units in members:
            for unit in units:
                symbols[(unit.sheet_file, unit.path.rsplit("/", 1)[-1])].append(unit.reference)
        group = Group(
            key=key, lib_id=first.lib_id, value=first.value, footprint=first.footprint, mpn=mpn,
            lcsc=meta[key]["lcsc"], manufacturer=_brand(first.fields.get("Manufacturer", "")),
            references=references,
            symbols=[{"sheet_file": s, "uuid": u, "designators": sorted(set(d), key=_natural)} for (s, u), d in sorted(symbols.items())],
            current_ipns={u[0].fields.get("IPN", "").strip() for u in members},
            category=category, params=params,
        )
        normalised = _normalise(categories[category], params)
        candidates: list[tuple[set[str], str]] = []
        if mpn:
            candidates.append((by_mpn.get(normalise_mpn(mpn), set()), "same MPN"))
        if group.lcsc:
            candidates.append((by_lcsc.get(group.lcsc, set()), "same LCSC number"))
        identity = fingerprint(categories[category], normalised)
        if identity:
            candidates.append((by_identity.get(identity, set()), "same identity"))
        for ipns, how in candidates:
            if len(ipns) == 1:
                group.suggestion = {"ipn": next(iter(ipns)), "how": how}
                break
        near = near_key(categories[category], normalised)
        if near:
            group.similar = sorted(by_near.get(near, set()) - {(group.suggestion or {}).get("ipn")})
        confirmed = confirmations.get(key)
        group.confirmed = str(confirmed["ipn"]) if confirmed and confirmed.get("ipn") else None
        on_board = group.current_ipns - {""}
        if on_board and len(group.current_ipns) == 1:
            ipn = next(iter(on_board))
            group.status = "done" if ipn in parts and (group.confirmed in (None, ipn)) else "conflict"
        elif on_board:
            group.status = "conflict"  # some designators have an IPN and some do not, or they differ
        elif group.confirmed:
            group.status = "confirmed"
        elif group.suggestion:
            group.status = "suggested"
        groups.append(group)
    order = {"conflict": 0, "suggested": 1, "unmatched": 2, "confirmed": 3, "done": 4}
    return sorted(groups, key=lambda g: (order[g.status], g.category, _natural(g.references[0])))


def _natural(reference: str) -> tuple:
    return tuple(int(t) if t.isdigit() else t for t in re.split(r"(\d+)", reference or ""))


def load(root: Path, entry: BoardEntry, commit: str | None) -> tuple[Board, list[Group]]:
    board = load_board(entry.name, Source(entry.path, commit))
    return board, plan(root, board)


def confirm(root: Path, board_name: str, matches: list[tuple[Group, str]]) -> tuple[str, bytes]:
    """The migrations file with these confirmations, as (path, contents). The caller commits it."""
    categories = load_categories(root)
    sources = {p.as_posix(): (root / p).read_bytes() for p in part_paths(root)}
    records, _ = check_records(root, sources, categories)
    known = {r.ipn for r in records}
    for _group, ipn in matches:
        if ipn not in known:
            raise ValueError(f"no part {ipn}")
    path = f"migrations/{board_name}.yaml"
    replaced = {group.key for group, _ipn in matches}
    existing = [m for m in load_confirmations(root, board_name)
                if group_key({k: str(m.get(k, "")) for k in ("lib_id", "value", "footprint", "mpn", "lcsc")}) not in replaced]
    for group, ipn in matches:
        existing.append({**group.match_fields, "ipn": ipn, "designators": " ".join(group.references), "confirmed": date.today()})
    existing.sort(key=lambda m: (str(m["ipn"]), str(m["lib_id"]), str(m["value"])))
    text = (
        f"# Board parts matched to library parts for {board_name}, confirmed by hand (spec section 9).\n"
        "# Written by the dashboard; the board itself is changed in KiCad.\n"
        + yaml.safe_dump({"board": board_name, "matches": existing}, sort_keys=False, allow_unicode=True, width=1000)
    )
    return path, text.encode("utf-8")


def changes(groups: list[Group], released: dict[str, str]) -> list[dict]:
    """What to change in KiCad, one row per symbol on a sheet: every confirmed group not yet done.

    `released` maps each released IPN to its generated symbol, such as PL_Resistor:RES-000002.
    """
    rows = []
    for group in groups:
        if group.status != "confirmed":
            continue
        for symbol in group.symbols:
            rows.append(
                {
                    "sheet_file": symbol["sheet_file"],
                    "designators": symbol["designators"],
                    "lib_id": group.lib_id,
                    "value": group.value,
                    "ipn": group.confirmed,
                    "symbol": released.get(group.confirmed, ""),
                }
            )
    return sorted(rows, key=lambda r: (r["sheet_file"], _natural(r["designators"][0])))
