"""Read a KiCad board, as committed or as on disk, into the placements the scanner checks (spec 7).

Reused sheets are expanded per instance. A symbol on a sheet used five times is five parts, and
its Reference property holds only one of their designators; the real designator for each instance
is in the symbol's (instances) record for that sheet path. Footprints on the PCB link back by the
same path.
"""

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
import subprocess

from partslib import sexpr
from partslib.library import load_yaml

BOARD_STATUSES = ("in_design", "released")


class BoardError(Exception):
    pass


@dataclass(frozen=True)
class BoardEntry:
    name: str
    path: Path
    remote: str
    project: str
    status: str


def load_boards(root: Path) -> list[BoardEntry]:
    path = root / "boards.yaml"
    if not path.is_file():
        return []
    try:
        raw = load_yaml(path.read_text(encoding="utf-8")) or []
    except ValueError as error:
        raise BoardError(f"boards.yaml: {error}") from None
    if not isinstance(raw, list):
        raise BoardError("boards.yaml must be a list of boards")
    boards, names = [], set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise BoardError(f"boards.yaml entry {index} must be a mapping")
        missing = [k for k in ("name", "path", "status") if not isinstance(entry.get(k), str)]
        if missing:
            raise BoardError(f"boards.yaml entry {index} needs {', '.join(missing)}")
        if entry["status"] not in BOARD_STATUSES:
            raise BoardError(f"board {entry['name']}: status must be one of {', '.join(BOARD_STATUSES)}")
        if entry["name"] in names:
            raise BoardError(f"board {entry['name']} is listed twice")
        names.add(entry["name"])
        boards.append(
            BoardEntry(
                entry["name"],
                Path(entry["path"]).expanduser(),
                str(entry.get("remote", "")),
                str(entry.get("project", "")),
                entry["status"],
            )
        )
    return boards


class Source:
    """Files of a board directory, either on disk or at a git commit."""

    def __init__(self, directory: Path, commit: str | None):
        self.directory = directory
        self.commit = commit
        if commit is not None:
            top = self._git("rev-parse", "--show-toplevel").strip()
            self.top = Path(top)
            self.prefix = PurePosixPath(directory.resolve().relative_to(Path(top).resolve()).as_posix())
            self.commit = self._git("rev-parse", "--verify", f"{commit}^{{commit}}").strip()
            # --full-tree: paths relative to the repository root, whatever directory git runs in.
            listing = self._git("ls-tree", "-r", "--name-only", "--full-tree", self.commit, "--", str(self.prefix))
            self._files = {
                PurePosixPath(line).relative_to(self.prefix).as_posix() for line in listing.splitlines()
            }

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.directory), *args], capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            raise BoardError(f"git {' '.join(args)}: {result.stderr.strip()}")
        return result.stdout

    def label(self) -> str:
        return self.commit or "working tree"

    def exists(self, relative: str) -> bool:
        relative = PurePosixPath(relative).as_posix()
        if self.commit is None:
            return (self.directory / relative).is_file()
        return relative in self._files

    def read(self, relative: str) -> str:
        relative = PurePosixPath(relative).as_posix()
        if self.commit is None:
            try:
                return (self.directory / relative).read_text(encoding="utf-8")
            except OSError as error:
                raise BoardError(f"cannot read {relative}: {error.strerror}") from None
        if relative not in self._files:
            raise BoardError(f"{relative} is not in commit {self.commit[:12]}")
        result = subprocess.run(
            ["git", "-C", str(self.directory), "show", f"{self.commit}:{self.prefix / relative}"],
            capture_output=True,
            check=False,
        )
        return result.stdout.decode("utf-8")

    def names(self, suffix: str) -> list[str]:
        if self.commit is None:
            return sorted(p.name for p in self.directory.glob(f"*{suffix}"))
        return sorted(f for f in self._files if "/" not in f and f.endswith(suffix))


@dataclass(frozen=True)
class Placement:
    """One unit of one symbol on one sheet instance."""

    path: str  # as on the PCB: sheet uuids below the root, then the symbol uuid
    reference: str
    unit: int
    lib_id: str
    value: str
    footprint: str
    fields: dict[str, str]
    sheet: str  # human sheet path, such as /Pwr_Inverter/HalfBridge_U/
    sheet_file: str
    power: bool
    on_board: bool
    in_bom: bool
    dnp: bool
    instance_found: bool  # False if the symbol had no instance record for this sheet


@dataclass(frozen=True)
class Footprint:
    path: str
    reference: str
    lib_id: str
    models: tuple[str, ...]
    board_only: bool
    dnp: bool


@dataclass
class Board:
    name: str
    source: Source
    project: str  # project name, the .kicad_pro stem
    placements: list[Placement] = field(default_factory=list)
    footprints: list[Footprint] = field(default_factory=list)
    tables: dict[str, list[tuple[str, str, str]]] = field(default_factory=dict)  # (name, type, uri)


def _parse(source: Source, relative: str) -> sexpr.Node:
    try:
        return sexpr.parse(source.read(relative))
    except ValueError as error:
        raise BoardError(f"{relative}: {error}") from None


def parse_table(text: str) -> list[tuple[str, str, str]]:
    entries = []
    for lib in sexpr.children(sexpr.parse(text), "lib"):
        name, kind, uri = sexpr.value(lib, "name"), sexpr.value(lib, "type"), sexpr.value(lib, "uri")
        if name is not None:
            entries.append((name, kind or "", uri or ""))
    return entries


def load_board(name: str, source: Source) -> Board:
    projects = source.names(".kicad_pro")
    if len(projects) != 1:
        raise BoardError(f"{name}: expected one .kicad_pro in {source.directory}, found {len(projects)}")
    project = projects[0].removesuffix(".kicad_pro")
    board = Board(name, source, project)

    root_file = f"{project}.kicad_sch"
    root = _parse(source, root_file)
    root_uuid = sexpr.value(root, "uuid")
    if root_uuid is None:
        raise BoardError(f"{root_file} has no uuid")
    _walk_sheet(board, root, root_file, f"/{root_uuid}", "", "/", set())

    pcb_file = f"{project}.kicad_pcb"
    if source.exists(pcb_file):
        for node in sexpr.children(_parse(source, pcb_file), "footprint"):
            attributes = sexpr.child(node, "attr") or []
            board.footprints.append(
                Footprint(
                    path=sexpr.value(node, "path") or "",
                    reference=sexpr.properties(node).get("Reference", ""),
                    lib_id=node[1] if len(node) > 1 and isinstance(node[1], str) else "",
                    models=tuple(m[1] for m in sexpr.children(node, "model") if len(m) > 1),
                    board_only="board_only" in attributes,
                    dnp="dnp" in attributes,
                )
            )
    for table in ("sym-lib-table", "fp-lib-table"):
        board.tables[table] = parse_table(source.read(table)) if source.exists(table) else []
    return board


def _walk_sheet(
    board: Board,
    sheet: sexpr.Node,
    sheet_file: str,
    instance_path: str,
    pcb_prefix: str,
    sheet_name: str,
    visiting: set[str],
) -> None:
    if sheet_file in visiting:
        raise BoardError(f"sheet {sheet_file} contains itself")
    lib_symbols = sexpr.child(sheet, "lib_symbols") or []
    power_symbols = {
        entry[1] for entry in sexpr.children(lib_symbols, "symbol") if sexpr.child(entry, "power") is not None
    }

    for symbol in sexpr.children(sheet, "symbol"):
        lib_id = sexpr.value(symbol, "lib_id") or ""
        symbol_uuid = sexpr.value(symbol, "uuid") or ""
        fields = sexpr.properties(symbol)
        reference, unit, found = fields.get("Reference", ""), int(sexpr.value(symbol, "unit") or 1), False
        for project in sexpr.children(sexpr.child(symbol, "instances") or [], "project"):
            for path in sexpr.children(project, "path"):
                if len(path) > 1 and path[1] == instance_path:
                    reference = sexpr.value(path, "reference") or reference
                    unit = int(sexpr.value(path, "unit") or unit)
                    found = True
        board.placements.append(
            Placement(
                path=f"{pcb_prefix}/{symbol_uuid}",
                reference=reference,
                unit=unit,
                lib_id=lib_id,
                value=fields.get("Value", ""),
                footprint=fields.get("Footprint", ""),
                fields=fields,
                sheet=sheet_name,
                sheet_file=sheet_file,
                power=lib_id in power_symbols or reference.startswith("#"),
                on_board=sexpr.flag(symbol, "on_board") is not False,
                in_bom=sexpr.flag(symbol, "in_bom") is not False,
                dnp=sexpr.flag(symbol, "dnp") is True,
                instance_found=found,
            )
        )

    directory = PurePosixPath(sheet_file).parent
    for child in sexpr.children(sheet, "sheet"):
        fields = sexpr.properties(child)
        child_file = fields.get("Sheetfile") or fields.get("Sheet file")
        child_uuid = sexpr.value(child, "uuid")
        if not child_file or not child_uuid:
            raise BoardError(f"{sheet_file}: a sheet has no file or uuid")
        relative = (directory / child_file).as_posix()
        _walk_sheet(
            board,
            _parse(board.source, relative),
            relative,
            f"{instance_path}/{child_uuid}",
            f"{pcb_prefix}/{child_uuid}",
            f"{sheet_name}{fields.get('Sheetname') or fields.get('Sheet name', child_uuid)}/",
            visiting | {sheet_file},
        )
