"""The local dashboard (spec section 8): read-only in M4. Serves only 127.0.0.1."""

from collections import Counter, defaultdict
import csv
from fractions import Fraction
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from partslib.index import _natural, connect, where_used
from partslib.library import load_categories
from partslib.quantity import format_engineering
from partslib.scan import CHECKS, KICAD_CLI, SEVERITY_ORDER
from partslib.search import Candidate, matches, parse_query

HERE = Path(__file__).parent
STATUSES = ("draft", "released", "deprecated", "obsolete")


def _quantity_text(value: str, unit: str) -> str:
    symbol = {"ohm": "Ω", "pct": "%"}.get(unit, unit)
    try:
        return format_engineering(Fraction(value)) + symbol
    except (ValueError, ZeroDivisionError):
        return value


def create_app(root: Path) -> FastAPI:
    root = root.resolve()
    app = FastAPI(title="partslib", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals["severity_order"] = SEVERITY_ORDER

    def page(request: Request, name: str, status_code: int = 200, **context) -> HTMLResponse:
        return templates.TemplateResponse(request, name, context, status_code=status_code)

    @app.exception_handler(HTTPException)
    def not_found(request: Request, error: HTTPException):
        if request.url.path.startswith(("/preview/", "/static/")) or request.url.path.endswith(".csv"):
            return Response(str(error.detail), status_code=error.status_code, media_type="text/plain")
        return page(request, "error.html", status_code=error.status_code, message=error.detail)

    def candidates(db) -> list[tuple[dict, Candidate]]:
        categories = load_categories(root)
        mpns = defaultdict(list)
        for row in db.execute("select * from mpns order by ipn, position"):
            mpns[row["ipn"]].append(dict(row))
        result = []
        for row in db.execute("select * from parts order by ipn"):
            part = dict(row)
            part["mpns"] = mpns[part["ipn"]]
            normalised = json.loads(part["normalised"])
            category = categories.get(part["category"])
            quantities = []
            for name, value in normalised.items():
                param = category.params.get(name) if category else None
                if param is not None and param.kind == "quantity":
                    quantities.append((param.unit, Fraction(value)))
            haystack = " ".join(
                [part["ipn"], part["description"], part["value"], part["category"] or "", part["status"] or ""]
                + [str(v) for v in normalised.values() if not _is_number(v)]
                + [f"{m['manufacturer']} {m['mpn']} {m['lcsc']}" for m in part["mpns"]]
            ).casefold()
            result.append((part, Candidate(part["ipn"], haystack, quantities)))
        return result

    def search(db, query: str, category: str, status: str) -> list[dict]:
        terms = parse_query(query)
        usage = Counter(row[0] for row in db.execute("select ipn from components where ipn is not null"))
        found = []
        for part, candidate in candidates(db):
            if category and part["category"] != category:
                continue
            if status and part["status"] != status:
                continue
            if matches(candidate, terms):
                part["used"] = usage.get(part["ipn"], 0)
                found.append(part)
        return found

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request):
        with connect(root) as db:
            by_category = defaultdict(Counter)
            for row in db.execute("select category, status, count(*) n from parts group by category, status"):
                by_category[row["category"]][row["status"]] = row["n"]
            boards = [dict(r) for r in db.execute("select * from boards order by name")]
            findings = [dict(r) for r in db.execute("select * from library_findings order by path, code")]
            attention = [
                dict(r)
                for r in db.execute(
                    """select p.ipn, p.status, p.description,
                              (select count(*) from components c where c.ipn = p.ipn) used
                       from parts p
                       where p.status in ('deprecated', 'obsolete')
                          or not exists (select 1 from mpns m where m.ipn = p.ipn and m.lifecycle = 'active')
                       order by p.ipn"""
                )
            ]
            problem = db.execute("select value from meta where key = 'boards_problem'").fetchone()
        return page(
            request,
            "overview.html",
            statuses=STATUSES,
            by_category=dict(sorted(by_category.items())),
            boards=boards,
            findings=findings,
            attention=attention,
            boards_problem=problem[0] if problem else None,
        )

    @app.get("/parts", response_class=HTMLResponse)
    def parts(request: Request, q: str = "", category: str = "", status: str = ""):
        with connect(root) as db:
            found = search(db, q, category, status)
            categories = [r[0] for r in db.execute("select distinct category from parts order by category")]
        context = dict(parts=found, q=q, category=category, status=status, categories=categories, statuses=STATUSES)
        if request.headers.get("HX-Request"):
            return page(request, "_results.html", **context)
        return page(request, "parts.html", **context)

    @app.get("/parts/{ipn}", response_class=HTMLResponse)
    def part(request: Request, ipn: str):
        with connect(root) as db:
            row = db.execute("select * from parts where ipn = ?", (ipn,)).fetchone()
            if row is None:
                raise HTTPException(404, f"no part {ipn}")
            data = dict(row)
            mpns = [dict(r) for r in db.execute("select * from mpns where ipn = ? order by position", (ipn,))]
            used = where_used(db, ipn)
            findings = [dict(r) for r in db.execute("select * from library_findings where path = ?", (data["path"],))]
            provenance = {
                (r["kind"], r["id"]): dict(r) for r in db.execute("select * from provenance")
            }
        category = load_categories(root).get(data["category"])
        normalised = json.loads(data["normalised"])
        params = []
        for name, raw in json.loads(data["params"]).items():
            param = category.params.get(name) if category else None
            shown = raw
            if param is not None and param.kind == "quantity" and name in normalised:
                shown = _quantity_text(normalised[name], param.unit)
            params.append({"name": name, "value": shown, "identity": bool(param and param.identity)})
        assets = [
            {"kind": kind, "id": data[key], "provenance": provenance.get((kind, data[key]))}
            for kind, key in (("symbol", "symbol"), ("footprint", "footprint"), ("model", "model"))
            if data[key]
        ]
        return page(
            request,
            "part.html",
            part=data,
            params=params,
            mpns=mpns,
            used=used,
            findings=findings,
            assets=assets,
            history=_history(root, data["path"]),
        )

    @app.get("/preview/{kind}/{ipn}.svg")
    def preview(kind: str, ipn: str):
        if kind not in ("symbol", "footprint"):
            raise HTTPException(404)
        with connect(root) as db:
            row = db.execute(f"select {kind} from parts where ipn = ?", (ipn,)).fetchone()
        if row is None or not row[0] or ":" not in row[0]:
            raise HTTPException(404)
        svg = _preview(root, kind, row[0])
        if svg is None:
            raise HTTPException(404, "no preview")
        return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "max-age=3600"})

    @app.get("/boards", response_class=HTMLResponse)
    def boards(request: Request):
        with connect(root) as db:
            rows = [dict(r) for r in db.execute("select * from boards order by name")]
        return page(request, "boards.html", boards=rows)

    @app.get("/boards/{name}", response_class=HTMLResponse)
    def board(request: Request, name: str, base: str = "", head: str = ""):
        with connect(root) as db:
            row = db.execute("select * from boards where name = ?", (name,)).fetchone()
            if row is None:
                raise HTTPException(404, f"no board {name}")
            components = [dict(r) for r in db.execute("select * from components where board = ?", (name,))]
            findings = [dict(r) for r in db.execute("select * from board_findings where board = ?", (name,))]
            scans = [dict(r) for r in db.execute("select * from scans where board = ? order by scanned_at desc", (name,))]
            descriptions = {r["ipn"]: r["description"] for r in db.execute("select ipn, description from parts")}
        for finding in findings:
            finding["references"] = json.loads(finding["refs"])
        findings.sort(key=lambda f: (SEVERITY_ORDER.index(f["severity"]), list(CHECKS).index(f["code"]) if f["code"] in CHECKS else 99))
        changes = None
        files = {s["file"]: s for s in scans}
        if len(scans) >= 2:
            head = head if head in files else scans[0]["file"]
            base = base if base in files else next(s["file"] for s in scans if s["file"] != head)
            changes = _compare(root / base, root / head)
        return page(
            request,
            "board.html",
            board=dict(row),
            bom=_bom(components, descriptions),
            findings=findings,
            checks=CHECKS,
            scans=scans,
            base=base,
            head=head,
            changes=changes,
        )

    @app.get("/boards/{name}/bom.csv")
    def bom_csv(name: str):
        with connect(root) as db:
            if db.execute("select 1 from boards where name = ?", (name,)).fetchone() is None:
                raise HTTPException(404, f"no board {name}")
            components = [dict(r) for r in db.execute("select * from components where board = ?", (name,))]
            descriptions = {r["ipn"]: r["description"] for r in db.execute("select ipn, description from parts")}
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["IPN", "Description", "Value", "Footprint", "Quantity", "Designators"])
        for line in _bom(components, descriptions):
            writer.writerow([line["ipn"], line["description"], line["value"], line["footprint"], line["quantity"], " ".join(line["references"])])
        return Response(
            out.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{name}-bom.csv"'},
        )

    return app


def _is_number(value: str) -> bool:
    try:
        Fraction(value)
    except (ValueError, ZeroDivisionError):
        return False
    return True


def _bom(components: list[dict], descriptions: dict[str, str]) -> list[dict]:
    """BOM lines grouped by IPN; parts without one are grouped by value and footprint."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for component in components:
        if component["ipn"]:
            key = ("ipn", component["ipn"])
        else:
            key = ("none", component["value"] or "", component["footprint"] or "")
        groups[key].append(component)
    lines = []
    for key, members in groups.items():
        first = members[0]
        lines.append(
            {
                "ipn": first["ipn"] or "",
                "description": descriptions.get(first["ipn"], "") if first["ipn"] else "",
                "value": first["value"] or "",
                "footprint": first["footprint"] or "",
                "quantity": len(members),
                "references": sorted((m["reference"] for m in members), key=_natural),
                "known": bool(first["ipn"]) and first["ipn"] in descriptions,
            }
        )
    return sorted(lines, key=lambda l: (not l["ipn"], l["ipn"], _natural(l["references"][0])))


def _compare(base_file: Path, head_file: Path) -> dict:
    base = json.loads(base_file.read_text(encoding="utf-8"))
    head = json.loads(head_file.read_text(encoding="utf-8"))
    old = {c["reference"]: c for c in base.get("components", [])}
    new = {c["reference"]: c for c in head.get("components", [])}
    fields = ("ipn", "value", "footprint", "lib_id")
    changed = []
    for reference in sorted(old.keys() & new.keys(), key=_natural):
        differences = {f: (old[reference].get(f), new[reference].get(f)) for f in fields if old[reference].get(f) != new[reference].get(f)}
        if differences:
            changed.append({"reference": reference, "differences": differences})

    def counts(scan):
        return Counter(f["code"] for f in scan.get("findings", []))

    before, after = counts(base), counts(head)
    return {
        "base": base.get("source", ""),
        "head": head.get("source", ""),
        "added": sorted(new.keys() - old.keys(), key=_natural),
        "removed": sorted(old.keys() - new.keys(), key=_natural),
        "changed": changed,
        "findings": [
            {"code": code, "before": before.get(code, 0), "after": after.get(code, 0)}
            for code in sorted(before.keys() | after.keys())
            if before.get(code, 0) != after.get(code, 0)
        ],
    }


def _history(root: Path, path: str) -> list[dict]:
    result = subprocess.run(
        ["git", "-C", str(root), "log", "-n", "20", "--date=short", "--format=%h%x09%ad%x09%s", "--", path],
        capture_output=True,
        text=True,
        check=False,
    )
    history = []
    for line in result.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            history.append({"commit": parts[0], "date": parts[1], "subject": parts[2]})
    return history


def _preview(root: Path, kind: str, reference: str) -> bytes | None:
    """Render a base symbol or footprint from assets/ with kicad-cli, cached by source content."""
    library, name = reference.split(":", 1)
    if kind == "symbol":
        source = root / "assets/symbols" / f"{library}.kicad_sym"
    else:
        source = root / "assets/footprints" / f"{library}.pretty"
    target = source / f"{name}.kicad_mod" if kind == "footprint" else source
    if not target.is_file() or not Path(KICAD_CLI).exists():
        return None
    key = hashlib.sha256(target.read_bytes() + reference.encode()).hexdigest()[:20]
    cache = root / "build/previews" / kind / f"{key}.svg"
    if cache.is_file():
        return cache.read_bytes()
    config = root / "build/.kicad-cli-config"  # never the user's own KiCad config
    config.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "KICAD_CONFIG_HOME": str(config)}
    with tempfile.TemporaryDirectory() as out:
        if kind == "symbol":
            args = [str(KICAD_CLI), "sym", "export", "svg", "-s", name, "-o", out, str(source)]
        else:
            args = [str(KICAD_CLI), "fp", "export", "svg", "--footprint", name, "-o", out, str(source)]
        subprocess.run(args, env=env, capture_output=True, check=False, timeout=60)
        svgs = sorted(Path(out).glob("*.svg"))
        if not svgs:
            return None
        data = svgs[0].read_bytes()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(data)
    return data
