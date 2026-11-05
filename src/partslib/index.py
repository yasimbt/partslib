"""The dashboard's SQLite index (spec D2): rebuilt from part files and saved scans, never the record.

`ensure_index` rebuilds only when a source file has changed, so every page sees current data.
"""

from collections import defaultdict
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import sqlite3
import threading

from partslib.board import BoardError, load_boards
from partslib.check import check_library, check_records
from partslib.library import LibraryError, load_categories, load_yaml, part_paths

INDEX = "build/index.sqlite"
SCHEMA = """
create table meta (key text primary key, value text);
create table parts (
    ipn text primary key, category text, status text, description text, value text,
    datasheet text, symbol text, footprint text, model text, notes text, path text,
    params text, normalised text, superseded_by text
);
create table mpns (ipn text, position integer, manufacturer text, mpn text, lcsc text, lifecycle text, verified text);
create table library_findings (path text, code text, message text);
create table provenance (kind text, id text, source text, licence text, version text);
create table boards (
    name text primary key, status text, path text, remote text, project text,
    source text, scanned_at text, parts integer, with_ipn integer,
    errors integer, warnings integer, infos integer, scan_file text, problem text
);
create table components (board text, reference text, ipn text, lib_id text, value text, footprint text, sheet text, dnp integer);
create table board_findings (board text, code text, severity text, message text, refs text);
create table scans (board text, source text, scanned_at text, file text);
create index components_ipn on components (ipn);
"""

_lock = threading.Lock()


def _sources(root: Path) -> list[Path]:
    paths = [root / "boards.yaml", root / "assets/provenance.yaml"]
    paths += sorted((root / "categories").glob("*.yaml"))
    paths += [root / p for p in part_paths(root)] if (root / "parts").is_dir() else []
    paths += sorted((root / "build/scans").rglob("*.json"))
    paths += [root / "build/manifest.json"]
    return paths


def fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for path in _sources(root):
        if path.is_file():
            stat = path.stat()
            digest.update(f"{path.relative_to(root)}|{stat.st_mtime_ns}|{stat.st_size}\n".encode())
    head = root / ".git/HEAD"
    if head.is_file():
        digest.update(head.read_bytes())
    return digest.hexdigest()


def _canonical(value: Fraction | str) -> str:
    return str(value)


def _latest_scans(root: Path, board: str) -> list[dict]:
    scans = []
    for path in sorted((root / "build/scans" / board).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        data["_file"] = str(path.relative_to(root))
        data.setdefault("scanned_at", "")
        scans.append(data)
    return sorted(scans, key=lambda d: (d["scanned_at"], d["_file"]), reverse=True)


def rebuild(root: Path, target: Path) -> None:
    temporary = target.with_name(target.name + ".tmp")
    temporary.unlink(missing_ok=True)
    db = sqlite3.connect(temporary)
    db.executescript(SCHEMA)

    categories = load_categories(root)
    sources = {p.as_posix(): (root / p).read_bytes() for p in part_paths(root)}
    records, _ = check_records(root, sources, categories)
    for record in records:
        if record.ipn is None:
            continue
        raw = record.raw
        mpn = record.mpns[0]["mpn"] if record.mpns and isinstance(record.mpns[0].get("mpn"), str) else ""
        try:
            value = record.category.render_value(record.normalised, mpn) if record.category else ""
        except (KeyError, IndexError, ValueError):
            value = ""
        db.execute(
            "insert or ignore into parts values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                record.ipn,
                raw.get("category"),
                raw.get("status"),
                str(raw.get("description") or ""),
                value,
                str(raw.get("datasheet") or ""),
                str(raw.get("symbol") or ""),
                str(raw.get("footprint") or ""),
                str(raw.get("model") or ""),
                str(raw.get("notes") or ""),
                record.path,
                json.dumps(raw.get("params") or {}, default=str, ensure_ascii=False),
                json.dumps({k: _canonical(v) for k, v in record.normalised.items()}, ensure_ascii=False),
                raw.get("superseded_by") if isinstance(raw.get("superseded_by"), str) else None,
            ),
        )
        for position, entry in enumerate(record.mpns):
            db.execute(
                "insert into mpns values (?,?,?,?,?,?,?)",
                (
                    record.ipn,
                    position,
                    str(entry.get("manufacturer") or ""),
                    str(entry.get("mpn") or ""),
                    str(entry.get("lcsc") or ""),
                    str(entry.get("lifecycle") or ""),
                    str(entry.get("verified") or ""),
                ),
            )

    try:
        _count, findings = check_library(root)
    except LibraryError as error:
        findings = []
        db.execute("insert into library_findings values (?,?,?)", ("categories", "library-error", str(error)))
    for finding in findings:
        db.execute("insert into library_findings values (?,?,?)", (finding.path, finding.code, finding.message))

    provenance_file = root / "assets/provenance.yaml"
    if provenance_file.is_file():
        try:
            entries = load_yaml(provenance_file.read_text(encoding="utf-8")) or []
        except ValueError:
            entries = []
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict):
                db.execute(
                    "insert into provenance values (?,?,?,?,?)",
                    tuple(str(entry.get(k, "")) for k in ("kind", "id", "source", "licence", "version")),
                )

    try:
        boards = load_boards(root)
        problem = None
    except BoardError as error:
        boards, problem = [], str(error)
    if problem:
        db.execute("insert into meta values ('boards_problem', ?)", (problem,))
    for board in boards:
        scans = _latest_scans(root, board.name)
        for scan in scans:
            db.execute("insert into scans values (?,?,?,?)", (board.name, scan.get("source", ""), scan["scanned_at"], scan["_file"]))
        latest = scans[0] if scans else None
        components = latest.get("components", []) if latest else []
        counts = latest.get("counts", {}) if latest else {}
        db.execute(
            "insert into boards values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                board.name,
                board.status,
                str(board.path),
                board.remote,
                board.project,
                latest.get("source") if latest else None,
                latest["scanned_at"] if latest else None,
                len(components),
                sum(1 for c in components if c.get("ipn")),
                counts.get("error", 0),
                counts.get("warning", 0),
                counts.get("info", 0),
                latest["_file"] if latest else None,
                None,
            ),
        )
        for component in components:
            db.execute(
                "insert into components values (?,?,?,?,?,?,?,?)",
                (
                    board.name,
                    component.get("reference"),
                    component.get("ipn") or None,
                    component.get("lib_id"),
                    component.get("value"),
                    component.get("footprint"),
                    component.get("sheet"),
                    int(bool(component.get("dnp"))),
                ),
            )
        for finding in latest.get("findings", []) if latest else []:
            db.execute(
                "insert into board_findings values (?,?,?,?,?)",
                (board.name, finding["code"], finding["severity"], finding["message"], json.dumps(finding.get("references", []))),
            )

    db.execute("insert into meta values ('fingerprint', ?)", (fingerprint(root),))
    db.commit()
    db.close()
    temporary.replace(target)


def ensure_index(root: Path) -> Path:
    """Return the index path, rebuilding it first if any source has changed."""
    target = root / INDEX
    with _lock:
        current = fingerprint(root)
        if target.is_file():
            try:
                with sqlite3.connect(target) as db:
                    row = db.execute("select value from meta where key = 'fingerprint'").fetchone()
                if row and row[0] == current:
                    return target
            except sqlite3.DatabaseError:
                pass
        target.parent.mkdir(parents=True, exist_ok=True)
        rebuild(root, target)
    return target


def connect(root: Path) -> sqlite3.Connection:
    db = sqlite3.connect(ensure_index(root))
    db.row_factory = sqlite3.Row
    return db


def where_used(db: sqlite3.Connection, ipn: str) -> list[dict]:
    """Boards that use a part, with quantity and designators, from each board's latest scan."""
    used: dict[str, list] = defaultdict(list)
    for row in db.execute(
        "select board, reference, sheet from components where ipn = ? order by board", (ipn,)
    ):
        used[row["board"]].append((row["reference"], row["sheet"]))
    boards = {row["name"]: row for row in db.execute("select * from boards")}
    return [
        {
            "board": name,
            "status": boards[name]["status"] if name in boards else "",
            "source": boards[name]["source"] if name in boards else "",
            "quantity": len(refs),
            "references": sorted((r for r, _s in refs), key=_natural),
        }
        for name, refs in sorted(used.items())
    ]


def _natural(reference: str) -> tuple:
    import re

    return tuple(int(t) if t.isdigit() else t for t in re.split(r"(\d+)", reference or ""))
