"""The local dashboard (spec section 8): read-only in M4. Serves only 127.0.0.1."""

from collections import Counter, defaultdict
import csv
from datetime import date
from fractions import Fraction
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from partslib import edit, importer, migrate, sources, verify
from partslib.board import BoardError, load_boards
from partslib.check import LIFECYCLES
from partslib.index import _natural, connect, where_used
from partslib.library import LibraryError, load_categories
from partslib.quantity import format_engineering
from partslib.scan import CHECKS, KICAD_CLI, SEVERITY_ORDER
from partslib.search import Candidate, matches, parse_query

HERE = Path(__file__).parent
SHARED_SYMBOLS = Path(os.environ.get("KICAD_APP", "/Applications/KiCad/KiCad.app")) / "Contents/SharedSupport/symbols"
STATUSES = ("draft", "released", "deprecated", "obsolete")


def _quantity_text(value: str, unit: str) -> str:
    symbol = {"ohm": "Ω", "pct": "%"}.get(unit, unit)
    try:
        return format_engineering(Fraction(value)) + symbol
    except (ValueError, ZeroDivisionError):
        return value


LOCAL_HOSTS = ("127.0.0.1", "localhost")


def create_app(root: Path, allowed_hosts: tuple[str, ...] = LOCAL_HOSTS) -> FastAPI:
    root = root.resolve()
    app = FastAPI(title="partslib", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals["severity_order"] = SEVERITY_ORDER
    # A secret per server run. Any web page the user visits can make their browser post to
    # 127.0.0.1, but it cannot read this token, so it cannot make edits or commits.
    token = secrets.token_urlsafe(32)
    templates.env.globals["csrf_token"] = token

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in allowed_hosts:
            # Refusing other Host names stops DNS rebinding: a hostile domain resolving to 127.0.0.1.
            return PlainTextResponse("partslib only answers on 127.0.0.1", status_code=403)
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin and origin.split("://", 1)[-1].rsplit(":", 1)[0] not in allowed_hosts:
                return PlainTextResponse("cross-site request refused", status_code=403)
        return await call_next(request)

    async def posted(request: Request):
        """The submitted form, if it carries this run's token. Every POST route reads its form here."""
        form = await request.form()
        if not secrets.compare_digest(str(form.get("csrf", "")), token):
            raise HTTPException(403, "missing or stale form token; reload the page")
        return form

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

    def form_lists(form) -> tuple[dict[str, str], dict[str, str], list[dict[str, str]], list[str]]:
        fields = {k: str(v) for k, v in form.items() if k in ("description", "datasheet", "notes", "symbol", "footprint", "model")}
        params = {k.removeprefix("param."): str(v) for k, v in form.items() if k.startswith("param.")}
        rows: dict[int, dict[str, str]] = defaultdict(dict)
        for key, value in form.items():
            if key.startswith("mpn."):
                _prefix, index, name = key.split(".", 2)
                rows[int(index)][name] = str(value)
        mpns = [rows[i] for i in sorted(rows)]
        return fields, params, mpns, [str(v) for v in form.getlist("ack")]

    def category_fields(category_name: str) -> dict:
        categories = load_categories(root)
        category = categories.get(category_name) or categories[sorted(categories)[0]]
        return {"category": category, "categories": sorted(categories)}

    def similar_parts(db, category, record) -> list[dict]:
        """Existing parts in the category sharing the first identity value, for the new-part form."""
        if record is None or not category.identity_params():
            return []
        first = category.identity_params()[0].name
        if first not in record.normalised:
            return []
        wanted = str(record.normalised[first])
        found = []
        for row in db.execute("select * from parts where category = ? order by ipn", (category.name,)):
            if json.loads(row["normalised"]).get(first) == wanted:
                found.append(dict(row))
        return found

    @app.get("/parts/new", response_class=HTMLResponse)
    def new_part_form(request: Request, category: str = "resistor"):
        context = category_fields(category)
        values = {k: v for k, v in request.query_params.items() if k != "category"}  # prefill, as from migration
        if request.headers.get("HX-Request"):
            return page(request, "_new_fields.html", values={}, **context)
        return page(request, "new_part.html", values=values, findings=[], **context)

    @app.post("/parts/new/check", response_class=HTMLResponse)
    async def new_part_check(request: Request):
        form = await posted(request)
        fields, params, mpns, acks = form_lists(form)
        try:
            path, part = edit.draft_part(root, str(form.get("category", "")), fields, params, mpns, acks)
        except (edit.EditError, LibraryError) as error:
            return page(request, "_new_check.html", error=str(error), findings=[], part=None, similar=[])
        findings, record = edit.evaluate(root, path, part)
        category = load_categories(root)[part["category"]]
        with connect(root) as db:
            similar = similar_parts(db, category, record)
        value = ""
        if record is not None:
            try:
                value = category.render_value(record.normalised, part["mpns"][0]["mpn"] if part["mpns"] else "")
            except (KeyError, IndexError, ValueError):
                value = ""
        return page(request, "_new_check.html", error=None, findings=findings, part=part, value=value, similar=similar)

    @app.post("/parts/new", response_class=HTMLResponse)
    async def new_part_create(request: Request):
        form = await posted(request)
        fields, params, mpns, acks = form_lists(form)
        values = {k: str(v) for k, v in form.items()}
        try:
            path, part = edit.draft_part(root, str(form.get("category", "")), fields, params, mpns, acks)
            commit = edit.create(root, path, part)
        except edit.Rejected as rejected:
            return page(request, "new_part.html", status_code=422, values=values, findings=rejected.findings,
                        **category_fields(str(form.get("category", ""))))
        except (edit.EditError, LibraryError) as error:
            return page(request, "new_part.html", status_code=422, values=values, findings=[], error=str(error),
                        **category_fields(str(form.get("category", ""))))
        return RedirectResponse(f"/parts/{part['ipn']}?saved={commit[:12]}", status_code=303)

    @app.get("/parts/{ipn}/release", response_class=HTMLResponse)
    def release_form(request: Request, ipn: str):
        return release_page(request, ipn, findings=[], error=None)

    def release_page(request: Request, ipn: str, findings, error, status_code: int = 200):
        with connect(root) as db:
            row = db.execute("select * from parts where ipn = ?", (ipn,)).fetchone()
            if row is None:
                raise HTTPException(404, f"no part {ipn}")
            mpns = [dict(r) for r in db.execute("select * from mpns where ipn = ? order by position", (ipn,))]
        part = dict(row)
        return page(request, "release.html", status_code=status_code, part=part, mpns=mpns,
                    params=_params(root, part), lifecycles=LIFECYCLES, findings=findings, error=error)

    @app.post("/parts/{ipn}/release", response_class=HTMLResponse)
    async def release_submit(request: Request, ipn: str):
        form = await posted(request)
        confirmations = ("values", "footprint", "mpn")
        missing = [c for c in confirmations if form.get(f"confirm.{c}") != "yes"]
        if missing:
            return release_page(request, ipn, findings=[], status_code=422,
                                error="Tick all three checks to release; they record that you compared the part with its datasheet.")
        try:
            commit = edit.release(root, ipn, str(form.get("lifecycle") or "unknown"), date.today())
        except edit.Rejected as rejected:
            return release_page(request, ipn, findings=rejected.findings, error=None, status_code=422)
        except edit.EditError as error:
            return release_page(request, ipn, findings=[], error=str(error), status_code=422)
        return RedirectResponse(f"/parts/{ipn}?saved={commit[:12]}", status_code=303)

    @app.post("/parts/{ipn}/verify", response_class=HTMLResponse)
    async def verify_submit(request: Request, ipn: str):
        await posted(request)
        try:
            result = verify.verify(root, ipn)
        except verify.VerifyError as error:
            return page(request, "verify.html", status_code=422, ipn=ipn, result=None, error=str(error))
        except (edit.Rejected, edit.EditError) as problem:
            findings = getattr(problem, "findings", None)
            message = "; ".join(f.message for f in findings) if findings else str(problem)
            return page(request, "verify.html", status_code=422, ipn=ipn, result=None, error=f"passed, but could not release: {message}")
        return page(request, "verify.html", ipn=ipn, result=result, error=None)

    @app.get("/parts/{ipn}/edit", response_class=HTMLResponse)
    def edit_form(request: Request, ipn: str):
        return edit_page(request, ipn, findings=[], error=None)

    def edit_page(request: Request, ipn: str, findings, error, status_code: int = 200):
        with connect(root) as db:
            row = db.execute("select * from parts where ipn = ?", (ipn,)).fetchone()
            if row is None:
                raise HTTPException(404, f"no part {ipn}")
            mpns = [dict(r) for r in db.execute("select * from mpns where ipn = ? order by position", (ipn,))]
        part = dict(row)
        category = load_categories(root).get(part["category"])
        return page(request, "edit.html", status_code=status_code, part=part, mpns=mpns,
                    raw_params=json.loads(part["params"]), category=category, lifecycles=LIFECYCLES,
                    findings=findings, error=error)

    @app.post("/parts/{ipn}/edit", response_class=HTMLResponse)
    async def edit_submit(request: Request, ipn: str):
        form = await posted(request)
        fields, params, mpns, _acks = form_lists(form)
        try:
            commit = edit.update(root, ipn, fields, params or None, mpns, str(form.get("status") or "") or None)
        except edit.Rejected as rejected:
            return edit_page(request, ipn, findings=rejected.findings, error=None, status_code=422)
        except edit.EditError as error:
            return edit_page(request, ipn, findings=[], error=str(error), status_code=422)
        return RedirectResponse(f"/parts/{ipn}?saved={commit[:12]}", status_code=303)

    @app.get("/import", response_class=HTMLResponse)
    def import_search(request: Request, q: str = ""):
        hits, problems = sources.search(q, root / "build") if q.strip() else ([], [])
        return page(request, "import.html", q=q, hits=hits, problems=problems)

    @app.post("/import/fetch", response_class=HTMLResponse)
    async def import_fetch(request: Request):
        form = await posted(request)
        try:
            stage_id = importer.stage(root, str(form.get("source", "")), str(form.get("id", "")), str(form.get("footprint", "")))
        except sources.SourceError as error:
            return page(request, "import.html", status_code=422, q=str(form.get("q", "")), hits=[], problems=[str(error)])
        return RedirectResponse(f"/import/{stage_id}", status_code=303)

    def import_page(request: Request, stage_id: str, category: str = "", values: dict | None = None,
                    findings=(), error: str | None = None, status_code: int = 200):
        try:
            _directory, fetched = importer.load_stage(root, stage_id)
        except sources.SourceError as problem:
            raise HTTPException(404, str(problem)) from None
        suggestion = fetched.suggestion
        context = category_fields(category or suggestion.get("category") or "ic")
        if values is None:
            values = {f"param.{k}": v for k, v in (suggestion.get("params") or {}).items()}
            values.update({
                "mpn.0.manufacturer": suggestion.get("manufacturer", ""),
                "mpn.0.mpn": suggestion.get("mpn", ""),
                "mpn.0.lcsc": suggestion.get("lcsc", ""),
                "datasheet": suggestion.get("datasheet", ""),
                "description": suggestion.get("description", ""),
            })
        return page(request, "import_confirm.html", status_code=status_code, stage=stage_id, fetched=fetched,
                    values=values, findings=list(findings), error=error, **context)

    @app.get("/import/{stage_id}", response_class=HTMLResponse)
    def import_confirm(request: Request, stage_id: str, category: str = ""):
        return import_page(request, stage_id, category)

    @app.get("/import/{stage_id}/preview/{kind}.svg")
    def import_preview(stage_id: str, kind: str):
        try:
            directory, fetched = importer.load_stage(root, stage_id)
        except sources.SourceError:
            raise HTTPException(404) from None
        if kind == "symbol":
            svg = _render(root, "symbol", directory / "symbol.kicad_sym", fetched.symbol_name)
        elif kind == "footprint":
            pretty = directory / "preview.pretty"
            pretty.mkdir(exist_ok=True)
            shutil.copyfile(directory / "footprint.kicad_mod", pretty / f"{fetched.footprint_name}.kicad_mod")
            svg = _render(root, "footprint", pretty, fetched.footprint_name)
        else:
            svg = None
        if svg is None:
            raise HTTPException(404, "no preview")
        return Response(svg, media_type="image/svg+xml")

    @app.post("/import/{stage_id}", response_class=HTMLResponse)
    async def import_add(request: Request, stage_id: str):
        form = await posted(request)
        fields, params, mpns, acks = form_lists(form)
        category = str(form.get("category", ""))
        try:
            ipn, commit = importer.add(root, stage_id, category, fields, params, mpns, acks)
        except edit.Rejected as rejected:
            return import_page(request, stage_id, category, {k: str(v) for k, v in form.items()},
                               rejected.findings, status_code=422)
        except (edit.EditError, sources.SourceError, LibraryError) as problem:
            return import_page(request, stage_id, category, {k: str(v) for k, v in form.items()},
                               error=str(problem), status_code=422)
        return RedirectResponse(f"/parts/{ipn}?saved={commit[:12]}", status_code=303)

    @app.get("/parts/{ipn}", response_class=HTMLResponse)
    def part(request: Request, ipn: str, saved: str = ""):
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
        params = _params(root, data)
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
            saved=saved,
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

    def board_entry(name: str):
        try:
            boards = {b.name: b for b in load_boards(root)}
        except BoardError as error:
            raise HTTPException(404, str(error)) from None
        if name not in boards:
            raise HTTPException(404, f"no board {name}")
        return boards[name]

    def migration(name: str, commit: str):
        entry = board_entry(name)
        try:
            board, groups = migrate.load(root, entry, commit or None)
        except BoardError as error:
            raise HTTPException(404, str(error)) from None
        categories = load_categories(root)
        with connect(root) as db:
            released = {
                r["ipn"]: f"{categories[r['category']].library}:{r['ipn']}"
                for r in db.execute("select ipn, category from parts where status = 'released'")
                if r["category"] in categories
            }
        return entry, board, groups, released

    @app.get("/boards/{name}/migrate", response_class=HTMLResponse)
    def migrate_page(request: Request, name: str, commit: str = "", saved: str = "", error: str = ""):
        entry, board, groups, released = migration(name, commit)
        counts = Counter(g.status for g in groups)
        stock = {p.stem for p in (SHARED_SYMBOLS.glob("*.kicad_sym") if SHARED_SYMBOLS.is_dir() else [])}
        return page(
            request, "migrate.html", board=entry, source=board.source.label(), commit=commit, groups=groups,
            counts=counts, changes=migrate.changes(groups, released), stock_libraries=stock,
            parts_total=sum(len(g.references) for g in groups),
            parts_done=sum(len(g.references) for g in groups if g.status == "done"),
            saved=saved, error=error,
        )

    @app.post("/boards/{name}/migrate")
    async def migrate_confirm(request: Request, name: str):
        form = await posted(request)
        commit = str(form.get("commit", ""))
        entry, _board, groups, _released = migration(name, commit)
        by_key = {g.key: g for g in groups}
        chosen = []
        if form.get("all") == "same-mpn":
            chosen = [(g, g.suggestion["ipn"]) for g in groups
                      if g.status == "suggested" and g.suggestion and g.suggestion["how"] == "same MPN"]
        else:
            group = by_key.get(str(form.get("group", "")))
            ipn = str(form.get("ipn", "")).strip().upper()
            if group is None or not ipn:
                raise HTTPException(422, "choose a group and an IPN")
            chosen = [(group, ipn)]
        if not chosen:
            return RedirectResponse(f"/boards/{name}/migrate?commit={commit}", status_code=303)
        try:
            path, data = migrate.confirm(root, entry.name, chosen)
            summary = ", ".join(f"{ipn} for {g.references[0]}{'…' if len(g.references) > 1 else ''}" for g, ipn in chosen)
            sha = edit.write_and_commit(root, {path: data}, f"Migrate {entry.name}: confirm {summary}")
        except (ValueError, edit.EditError) as problem:
            return RedirectResponse(f"/boards/{name}/migrate?commit={commit}&error={problem}", status_code=303)
        return RedirectResponse(f"/boards/{name}/migrate?commit={commit}&saved={sha[:12]}", status_code=303)

    @app.get("/boards/{name}/migrate/changes.csv")
    def migrate_changes(name: str, commit: str = ""):
        _entry, _board, groups, released = migration(name, commit)
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["Sheet", "Designators", "Symbol", "Value", "Set IPN to", "Then change symbol to"])
        for row in migrate.changes(groups, released):
            writer.writerow([row["sheet_file"], " ".join(row["designators"]), row["lib_id"], row["value"], row["ipn"], row["symbol"]])
        return Response(out.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}-migration.csv"'})

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


def _params(root: Path, part: dict) -> list[dict]:
    category = load_categories(root).get(part["category"])
    normalised = json.loads(part["normalised"])
    params = []
    for name, raw in json.loads(part["params"]).items():
        param = category.params.get(name) if category else None
        shown = raw
        if param is not None and param.kind == "quantity" and name in normalised:
            shown = _quantity_text(normalised[name], param.unit)
        params.append({"name": name, "value": shown, "identity": bool(param and param.identity)})
    return params


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
    return _render(root, kind, source, name)


def _render(root: Path, kind: str, source: Path, name: str) -> bytes | None:
    """Render one symbol from a .kicad_sym, or one footprint from a .pretty, as SVG."""
    target = source / f"{name}.kicad_mod" if kind == "footprint" else source
    if not target.is_file() or not Path(KICAD_CLI).exists():
        return None
    key = hashlib.sha256(target.read_bytes() + f"{kind}:{name}".encode()).hexdigest()[:20]
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
