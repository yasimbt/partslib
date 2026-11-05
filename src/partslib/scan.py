"""`partslib scan`: check registered boards against the library (spec section 7).

Results are saved per commit under build/scans/, so later milestones can compare commits and show
where each part is used.
"""

from collections import defaultdict
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from partslib.board import Board, BoardEntry, Placement, Source, load_board, parse_table
from partslib.check import RETIRED, check_records
from partslib.library import load_categories, near_key, part_paths

# code -> (severity while in design, severity at release, description)
CHECKS = {
    "no-ipn": ("warning", "error", "part has no IPN field"),
    "unknown-ipn": ("warning", "error", "IPN is not in the library"),
    "instance-missing": ("warning", "warning", "symbol has no designator for a sheet instance"),
    "missing-on-pcb": ("warning", "warning", "schematic part has no footprint on the PCB"),
    "missing-in-schematic": ("warning", "warning", "PCB footprint has no schematic part"),
    "footprint-mismatch": ("error", "error", "schematic and PCB footprints differ"),
    "no-footprint": ("error", "error", "non-power symbol has no footprint"),
    "no-model": ("warning", "warning", "footprint has no 3D model"),
    "model-unresolved": ("warning", "warning", "3D model path does not resolve"),
    "machine-specific": ("info", "error", "library or model resolves only on this machine"),
    "library-unresolved": ("warning", "error", "symbol or footprint library is in no library table"),
    "library-item-missing": ("warning", "error", "library exists but no longer has the symbol or footprint"),
    "retired-part": ("warning", "warning", "part is deprecated"),
    "obsolete-part": ("error", "error", "part is obsolete"),
    "no-active-mpn": ("warning", "warning", "no MPN of the part is active"),
    "duplicate-function": ("warning", "warning", "two IPNs with the same function"),
    "fresh-checkout": ("warning", "error", "board does not open cleanly from a fresh checkout"),
}
SEVERITY_ORDER = ("error", "warning", "info")

APP = Path(os.environ.get("KICAD_APP", "/Applications/KiCad/KiCad.app"))
SHARED = APP / "Contents/SharedSupport" if sys.platform == "darwin" else Path("/usr/share/kicad")
KICAD_CLI = APP / "Contents/MacOS/kicad-cli" if sys.platform == "darwin" else Path("kicad-cli")
VARIABLE = re.compile(r"\$\{([^}]+)\}")


@dataclass(frozen=True)
class BoardFinding:
    code: str
    severity: str
    message: str
    references: tuple[str, ...] = ()


@dataclass
class Environment:
    """What this machine's KiCad knows: built-in variables, user variables and library tables."""

    builtin: dict[str, str]
    user: dict[str, str]
    stock_libraries: dict[str, dict[str, str]]  # table -> name -> uri, from KiCad's default libraries
    global_libraries: dict[str, dict[str, str]]  # table -> name -> uri

    @classmethod
    def load(cls, config_dir: Path | None, kicad_major: str = "10") -> "Environment":
        builtin = {
            f"KICAD{kicad_major}_3DMODEL_DIR": str(SHARED / "3dmodels"),
            f"KICAD{kicad_major}_FOOTPRINT_DIR": str(SHARED / "footprints"),
            f"KICAD{kicad_major}_SYMBOL_DIR": str(SHARED / "symbols"),
            f"KICAD{kicad_major}_TEMPLATE_DIR": str(SHARED / "template"),
        }
        user: dict[str, str] = {}
        stock: dict[str, dict[str, str]] = {"sym-lib-table": {}, "fp-lib-table": {}}
        global_libraries: dict[str, dict[str, str]] = {"sym-lib-table": {}, "fp-lib-table": {}}
        if config_dir is not None:
            common = config_dir / "kicad_common.json"
            if common.is_file():
                variables = (json.loads(common.read_text(encoding="utf-8")).get("environment") or {}).get("vars")
                user = {k: str(v) for k, v in (variables or {}).items()}
            for table in stock:
                path = config_dir / table
                if not path.is_file():
                    continue
                for name, kind, uri in parse_table(path.read_text(encoding="utf-8")):
                    if kind == "Table":
                        nested = Path(_expand(uri, {**builtin, **user}) or "")
                        if nested.is_file():
                            for n, _k, u in parse_table(nested.read_text(encoding="utf-8")):
                                stock[table][n] = u
                    else:
                        global_libraries[table][name] = uri
        return cls(builtin, user, stock, global_libraries)


def _expand(path: str, variables: dict[str, str]) -> str | None:
    """Expand ${VAR}s, or None if one is undefined."""
    missing = [name for name in VARIABLE.findall(path) if name not in variables]
    if missing:
        return None
    return VARIABLE.sub(lambda m: variables[m.group(1)], path)


def classify_path(path: str, environment: Environment) -> str:
    """project | stock | partslib | machine | undefined, by what the path depends on."""
    names = VARIABLE.findall(path)
    if not names:
        return "machine" if os.path.isabs(path) else "project"
    first = names[0]
    if first == "KIPRJMOD":
        return "project"
    if first in environment.builtin:
        return "stock"
    if first == "PARTSLIB":
        return "partslib"
    return "machine" if first in environment.user else "undefined"


@dataclass
class ScanResult:
    board: str
    status: str
    source: str
    components: list[dict] = field(default_factory=list)
    findings: list[BoardFinding] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        counts = dict.fromkeys(SEVERITY_ORDER, 0)
        for finding in self.findings:
            counts[finding.severity] += 1
        return counts


def _library_nickname(lib_id: str) -> str | None:
    return lib_id.split(":", 1)[0] if ":" in lib_id else None


def _components(placements: list[Placement]) -> dict[str, list[Placement]]:
    """Group the units of each real part by designator."""
    grouped: dict[str, list[Placement]] = defaultdict(list)
    for placement in placements:
        if not placement.power:
            grouped[placement.reference].append(placement)
    return dict(sorted(grouped.items(), key=lambda kv: _natural(kv[0])))


def _natural(reference: str) -> tuple:
    return tuple(int(t) if t.isdigit() else t for t in re.split(r"(\d+)", reference))


def scan_board(board: Board, status: str, root: Path, environment: Environment) -> ScanResult:
    result = ScanResult(board.name, status, board.source.label())
    release = status == "released"
    found: dict[str, list[str]] = defaultdict(list)  # code -> references
    notes: dict[str, list[str]] = defaultdict(list)  # code -> messages for grouped findings

    def add(code: str, message: str, references: tuple[str, ...] = (), severity: str | None = None) -> None:
        design, at_release, _ = CHECKS[code]
        result.findings.append(
            BoardFinding(code, severity or (at_release if release else design), message, references)
        )

    categories = load_categories(root)
    sources = {p.as_posix(): (root / p).read_bytes() for p in part_paths(root)}
    records, _ = check_records(root, sources, categories)
    parts = {record.ipn: record for record in records if record.ipn}

    components = _components(board.placements)
    footprints_by_path = {fp.path: fp for fp in board.footprints}
    schematic_paths = set()

    ipns_by_function: dict[tuple, set[str]] = defaultdict(set)
    for reference, units in components.items():
        first = units[0]
        ipn = first.fields.get("IPN", "").strip()
        result.components.append(
            {
                "reference": reference,
                "ipn": ipn,
                "lib_id": first.lib_id,
                "value": first.value,
                "footprint": first.footprint,
                "sheet": first.sheet,
                "dnp": first.dnp,
            }
        )
        if any(not u.instance_found for u in units):
            found["instance-missing"].append(reference)
        if not first.in_bom and not first.on_board:
            continue  # graphics, not a part
        if not ipn:
            found["no-ipn"].append(reference)
        elif ipn not in parts:
            found["unknown-ipn"].append(reference)
        else:
            record = parts[ipn]
            if record.status == "obsolete":
                found["obsolete-part"].append(reference)
            elif record.status in RETIRED:
                found["retired-part"].append(reference)
            if not any(m.get("lifecycle") == "active" for m in record.mpns):
                found["no-active-mpn"].append(reference)
            key = near_key(record.category, record.normalised) if record.category else None
            if key is not None:
                ipns_by_function[key].add(ipn)
        if not first.on_board:
            continue
        if not first.footprint:
            found["no-footprint"].append(reference)
        for unit in units:
            schematic_paths.add(unit.path)
        on_pcb = [footprints_by_path[u.path] for u in units if u.path in footprints_by_path]
        if not on_pcb:
            if first.footprint:
                found["missing-on-pcb"].append(reference)
        elif first.footprint and on_pcb[0].lib_id != first.footprint:
            add(
                "footprint-mismatch",
                f"{reference}: schematic {first.footprint}, PCB {on_pcb[0].lib_id}",
                (reference,),
            )

    orphans = [
        fp for fp in sorted(board.footprints, key=lambda f: _natural(f.reference))
        if fp.path not in schematic_paths and not fp.board_only
    ]
    found["missing-in-schematic"] = [fp.reference for fp in orphans]
    elsewhere = [fp.reference for fp in orphans if fp.reference in components]
    if elsewhere:
        notes["missing-in-schematic"].append(
            f"also in the schematic, at another sheet path: {', '.join(elsewhere)}; "
            "the PCB probably needs updating from the schematic"
        )
    for ipns in ipns_by_function.values():
        if len(ipns) > 1:
            references = tuple(c["reference"] for c in result.components if c["ipn"] in ipns)
            add("duplicate-function", f"{', '.join(sorted(ipns))} have the same function", references)

    _check_models(board, environment, found, notes, add)
    _check_libraries(board, components, environment, add)

    for code, references in found.items():
        if references:
            add(code, f"{CHECKS[code][2]}: {len(references)}", tuple(sorted(references, key=_natural)))
    for code, messages in notes.items():
        for message in messages:
            add(code, message)
    result.findings.sort(key=lambda f: (SEVERITY_ORDER.index(f.severity), list(CHECKS).index(f.code), f.message))
    return result


def _check_models(board: Board, environment: Environment, found, notes, add) -> None:
    variables = {**environment.builtin, **environment.user}
    machine: dict[str, list[str]] = defaultdict(list)
    for footprint in board.footprints:
        if footprint.board_only:
            continue
        if not footprint.models:
            found["no-model"].append(footprint.reference)
            continue
        for model in footprint.models:
            kind = classify_path(model, environment)
            if kind == "project":
                relative = model.split("}", 1)[1].lstrip("/") if model.startswith("${KIPRJMOD}") else model
                resolved = board.source.exists(relative)
            else:
                expanded = _expand(model, variables)
                resolved = expanded is not None and Path(expanded).is_file()
            if not resolved:
                where = {
                    "stock": "KiCad's library has no such model",
                    "project": f"not in the project at {board.source.label()[:12]}",
                    "undefined": "uses a variable this machine does not define",
                }.get(kind, "file not found")
                notes["model-unresolved"].append(f"{footprint.reference}: {model} ({where})")
            if kind in ("machine", "partslib", "undefined"):
                names = VARIABLE.findall(model)
                machine[f"${{{names[0]}}}" if names else "absolute paths"].append(footprint.reference)
    for variable, references in sorted(machine.items()):
        add(
            "machine-specific",
            f"{len(references)} 3D models use {variable}, which a fresh machine does not have",
            tuple(sorted(references, key=_natural)),
        )


class _LibraryContents:
    """Answers whether a library still has a symbol or footprint, reading each library once."""

    def __init__(self, board: Board, environment: Environment):
        self.board = board
        self.variables = {**environment.builtin, **environment.user}
        self._symbols: dict[str, str | None] = {}

    def _local(self, uri: str) -> tuple[str, str | None]:
        """("project", relative path) or ("disk", absolute path); None if it cannot be expanded."""
        if uri.startswith("${KIPRJMOD}"):
            return "project", uri.split("}", 1)[1].lstrip("/")
        return "disk", _expand(uri, self.variables)

    def has(self, table: str, uri: str, name: str) -> bool | None:
        """True or False, or None if the library itself cannot be read here."""
        where, path = self._local(uri)
        if path is None:
            return None
        if table == "fp-lib-table":
            target = f"{path}/{name}.kicad_mod"
            if where == "project":
                return self.board.source.exists(target)
            return Path(target).is_file() if Path(path).is_dir() else None
        if uri not in self._symbols:
            text = None
            if where == "project" and self.board.source.exists(path):
                text = self.board.source.read(path)
            elif where == "disk" and Path(path).is_file():
                text = Path(path).read_text(encoding="utf-8")
            self._symbols[uri] = text
        text = self._symbols[uri]
        return None if text is None else f'(symbol "{name}"' in text


def _check_libraries(board: Board, components, environment: Environment, add) -> None:
    contents = _LibraryContents(board, environment)
    for table, label in (("sym-lib-table", "symbol"), ("fp-lib-table", "footprint")):
        project = {name: uri for name, _kind, uri in board.tables.get(table, [])}
        users: dict[str, list[str]] = defaultdict(list)  # lib_id -> references
        if table == "sym-lib-table":
            for reference, units in components.items():
                users[units[0].lib_id].append(reference)
        else:
            for footprint in board.footprints:
                users[footprint.lib_id].append(footprint.reference)
        by_library: dict[str, dict[str, list[str]]] = defaultdict(dict)
        for lib_id, references in users.items():
            nickname = _library_nickname(lib_id)
            if nickname:
                by_library[nickname][lib_id.split(":", 1)[1]] = references

        for nickname, items in sorted(by_library.items()):
            references = tuple(sorted((r for refs in items.values() for r in refs), key=_natural))
            if nickname in project:
                uri, kind = project[nickname], classify_path(project[nickname], environment)
                kind = "portable" if kind == "project" else kind
            elif nickname in environment.stock_libraries.get(table, {}):
                uri, kind = environment.stock_libraries[table][nickname], "portable"
            elif nickname in environment.global_libraries.get(table, {}):
                uri = environment.global_libraries[table][nickname]
                kind = "partslib" if classify_path(uri, environment) == "partslib" else "machine"
            else:
                add(
                    "library-unresolved",
                    f"{label} library {nickname!r} ({len(references)} parts) is in no library table; "
                    "KiCad only has the copies embedded in the design",
                    references,
                )
                continue
            if kind != "portable":
                add(
                    "machine-specific",
                    f"{label} library {nickname!r} ({len(references)} parts) is only in this machine's config",
                    references,
                )
            for name, item_references in sorted(items.items()):
                if contents.has(table, uri, name) is False:
                    add(
                        "library-item-missing",
                        f"{label} {nickname}:{name} ({len(item_references)} parts) is no longer in library "
                        f"{nickname}; KiCad only has the copies embedded in the design",
                        tuple(sorted(item_references, key=_natural)),
                    )


def fresh_checkout(board: Board, entry: BoardEntry, work: Path) -> list[str]:
    """Open the board as a fresh machine would, and return what KiCad cannot find."""
    copy = work / "board"
    if board.source.commit is None:
        shutil.copytree(entry.path, copy, ignore=shutil.ignore_patterns(".history", "*-backups"))
    else:
        # From the repository root: run in a subdirectory, git archive only includes that directory.
        archive = subprocess.run(
            ["git", "-C", str(board.source.top), "archive", f"{board.source.commit}:{board.source.prefix}"],
            capture_output=True,
            check=True,
        ).stdout
        copy.mkdir()
        subprocess.run(["tar", "-x", "-C", str(copy)], input=archive, check=True)
    config = work / "config/10.0"
    config.mkdir(parents=True)
    for table, kind in (("sym-lib-table", "sym_lib_table"), ("fp-lib-table", "fp_lib_table")):
        template = SHARED / "template" / table
        (config / table).write_text(
            f'({kind}\n\t(version 7)\n\t(lib (name "KiCad") (type "Table") (uri "{template}") (options "") (descr ""))\n)\n',
            encoding="utf-8",
        )
    env = {k: v for k, v in os.environ.items() if k != "PARTSLIB"}
    env["KICAD_CONFIG_HOME"] = str(work / "config")
    report = work / "erc.json"
    subprocess.run(
        [str(KICAD_CLI), "sch", "erc", "--format", "json", "--severity-all", "-o", str(report),
         str(copy / f"{board.project}.kicad_sch")],
        env=env,
        capture_output=True,
        check=False,
    )
    if not report.is_file():
        return ["KiCad could not open the schematic"]
    problems: dict[str, int] = defaultdict(int)
    for sheet in json.loads(report.read_text(encoding="utf-8"))["sheets"]:
        for violation in sheet["violations"]:
            if violation["type"] in ("lib_symbol_issues", "footprint_link_issues"):
                problems[violation["description"]] += 1
    return [f"{description} ({count}x)" for description, count in sorted(problems.items())]


def scan(
    root: Path,
    entry: BoardEntry,
    commit: str | None,
    config_dir: Path | None,
    fresh: bool = False,
) -> ScanResult:
    source = Source(entry.path, commit)
    board = load_board(entry.name, source)
    environment = Environment.load(config_dir)
    result = scan_board(board, entry.status, root, environment)
    if fresh:
        with tempfile.TemporaryDirectory(prefix="partslib-fresh-") as work:
            for problem in fresh_checkout(board, entry, Path(work)):
                design, at_release, _ = CHECKS["fresh-checkout"]
                result.findings.append(
                    BoardFinding("fresh-checkout", at_release if entry.status == "released" else design, problem)
                )
    return result


def save(root: Path, result: ScanResult) -> Path:
    name = "worktree" if result.source == "working tree" else result.source
    path = root / "build/scans" / result.board / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = asdict(result)
    data["counts"] = result.counts()
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def format_result(result: ScanResult, limit: int = 12) -> str:
    counts = result.counts()
    lines = [
        f"{result.board} at {result.source[:12]} ({result.status}): {len(result.components)} parts, "
        f"{counts['error']} errors, {counts['warning']} warnings, {counts['info']} notes"
    ]
    for finding in result.findings:
        refs = ""
        if finding.references:
            shown = ", ".join(finding.references[:limit])
            more = f" and {len(finding.references) - limit} more" if len(finding.references) > limit else ""
            refs = f"\n      {shown}{more}"
        lines.append(f"  {finding.severity:7} [{finding.code}] {finding.message}{refs}")
    return "\n".join(lines)

