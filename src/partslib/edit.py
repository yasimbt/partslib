"""Write part files from the dashboard (spec section 8): each change is checked, then one git commit.

A change is refused if `check` would report any problem with the part being changed, including the
history rules, so a released part's identity cannot be changed here either. Problems elsewhere in
the library are left to `check` and do not block unrelated edits.
"""

from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from pathlib import Path
import subprocess
import threading

import yaml

from partslib.add import next_ipn
from partslib.check import Finding, Record, check_history, check_records, default_revision
from partslib.library import Category, load_categories, load_yaml, part_paths
from partslib.quantity import plain_number

KEY_ORDER = (
    "ipn", "status", "category", "description", "params", "symbol", "footprint", "footprint_checked",
    "checked_by", "model", "datasheet", "mpns", "supersedes", "superseded_by", "near_match_ack", "notes",
)
IMPORT_NOTE_START = "Not yet checked against the datasheet"
_write_lock = threading.Lock()


class EditError(Exception):
    pass


@dataclass
class Rejected(Exception):
    findings: list[Finding]


class _PlainDumper(yaml.SafeDumper):
    """Writes repeated values out in full: part files are edited by hand, so no &anchors."""

    def ignore_aliases(self, data):
        return True


def dump_part(part: dict) -> str:
    ordered = {key: part[key] for key in KEY_ORDER if key in part}
    ordered.update({key: value for key, value in part.items() if key not in ordered})
    return yaml.dump(ordered, Dumper=_PlainDumper, sort_keys=False, allow_unicode=True, width=1000)


def _sources(root: Path) -> dict[str, bytes | str]:
    return {p.as_posix(): (root / p).read_bytes() for p in part_paths(root)}


def evaluate(root: Path, path: str, part: dict) -> tuple[list[Finding], Record | None]:
    """The problems `check` would report for this part if it were saved, and its parsed record."""
    categories = load_categories(root)
    sources = _sources(root)
    sources[path] = dump_part(part)
    records, findings = check_records(root, sources, categories)
    revision = default_revision(root)
    if revision is not None:
        check_history(root, revision, records, categories, findings)
    ipn = part.get("ipn")
    own = sorted(f for f in findings if f.path == path or (ipn and ipn in f.related))
    record = next((r for r in records if r.path == path), None)
    return own, record


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise EditError(f"git {args[0]} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def commit_files(root: Path, paths: list[str], message: str) -> str:
    """Commit exactly these files, leaving anything else staged or changed alone. Returns the commit."""
    _git(root, "add", "--", *paths)
    # --only commits just these paths, whatever else is staged. Git LFS's post-commit hook warns
    # outside the dev shell, but git ignores post-commit failures.
    _git(root, "commit", "--only", "-q", "-m", message, "--", *paths)
    return _git(root, "rev-parse", "HEAD")


def save(root: Path, path: str, part: dict, message: str, extra: dict[str, bytes] | None = None) -> str:
    """Check, write and commit a part file, plus any assets it needs, as one commit.

    The extra files are written first, so the part is checked against them. If check rejects the
    part, or the commit fails, every file goes back to how it was. Raises Rejected with problems.
    """
    with _write_lock:
        if _git(root, "rev-parse", "--show-toplevel") != str(root.resolve()):
            raise EditError(f"{root} is not the top of a git repository")
        files = {**(extra or {}), path: dump_part(part).encode("utf-8")}
        if any(_uses_lfs(root, p) for p in files) and not lfs_available(root):
            raise EditError(
                "adding a 3D model needs git LFS: run `partslib serve` inside `nix develop`, "
                "after `git lfs install --local` once in this clone"
            )
        previous = {p: (root / p).read_bytes() if (root / p).exists() else None for p in files}
        try:
            for relative, data in extra.items() if extra else ():
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                (root / relative).write_bytes(data)
            findings, _record = evaluate(root, path, part)
            if findings:
                raise Rejected(findings)
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_bytes(files[path])
            return commit_files(root, list(files), message)
        except (Rejected, EditError):
            for relative, data in previous.items():
                if data is None:
                    (root / relative).unlink(missing_ok=True)
                else:
                    (root / relative).write_bytes(data)
            raise


def _uses_lfs(root: Path, path: str) -> bool:
    """Whether .gitattributes sends this file to git LFS."""
    result = subprocess.run(["git", "-C", str(root), "check-attr", "filter", "--", path], capture_output=True, text=True, check=False)
    return result.stdout.strip().endswith(": filter: lfs")


def write_and_commit(root: Path, files: dict[str, bytes], message: str) -> str:
    """Write files that need no part check, such as a migration record, and commit them."""
    with _write_lock:
        if _git(root, "rev-parse", "--show-toplevel") != str(root.resolve()):
            raise EditError(f"{root} is not the top of a git repository")
        previous = {p: (root / p).read_bytes() if (root / p).exists() else None for p in files}
        try:
            for relative, data in files.items():
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                (root / relative).write_bytes(data)
            return commit_files(root, list(files), message)
        except EditError:
            for relative, data in previous.items():
                if data is None:
                    (root / relative).unlink(missing_ok=True)
                else:
                    (root / relative).write_bytes(data)
            raise


def lfs_available(root: Path) -> bool:
    """git-lfs is on PATH and this clone uses it. Without `git lfs install`, git has no LFS filter
    and would silently commit the file itself instead of a pointer."""
    version = subprocess.run(["git", "-C", str(root), "lfs", "version"], capture_output=True, check=False)
    clean = subprocess.run(["git", "-C", str(root), "config", "--get", "filter.lfs.clean"], capture_output=True, check=False)
    return version.returncode == 0 and clean.returncode == 0 and bool(clean.stdout.strip())


def load_part(root: Path, ipn: str) -> tuple[str, dict]:
    for path in part_paths(root):
        if path.stem == ipn:
            raw = load_yaml((root / path).read_bytes())
            if not isinstance(raw, dict):
                raise EditError(f"{path} is not a valid part file; fix it by hand")
            return path.as_posix(), raw
    raise EditError(f"no part {ipn}")


def _canonical_params(category: Category, params: dict[str, str]) -> dict[str, object]:
    """Store values as plain numbers, as `partslib add` does, so 4k7 is written 4700."""
    result: dict[str, object] = {}
    for name, value in params.items():
        param = category.params.get(name)
        try:
            normalised = param.normalise(value) if param else value
        except ValueError:
            result[name] = value  # left as typed, so check reports it
            continue
        result[name] = plain_number(normalised) if isinstance(normalised, Fraction) else normalised
    return result


def draft_part(
    root: Path,
    category_name: str,
    fields: dict[str, str],
    params: dict[str, str],
    mpns: list[dict[str, str]],
    acknowledge: list[str],
) -> tuple[str, dict]:
    """Build a new draft part from the form, with defaults from its category. Not saved."""
    categories = load_categories(root)
    category = categories.get(category_name)
    if category is None:
        raise EditError(f"unknown category {category_name!r}")
    params = {name: value.strip() for name, value in params.items() if value.strip()}
    ipn = next_ipn(root, category)
    part: dict = {
        "ipn": ipn,
        "status": "draft",
        "category": category.name,
        "description": fields.get("description", "").strip(),
        "params": _canonical_params(category, params),
    }
    package = str(params.get("package", "")).strip()
    defaults = category.package_defaults.get(package, {})
    for key, default in (
        ("symbol", category.default_symbol),
        ("footprint", defaults.get("footprint")),
        ("model", defaults.get("model")),
    ):
        value = fields.get(key, "").strip() or default
        if value:
            part[key] = value
    if fields.get("datasheet", "").strip():
        part["datasheet"] = fields["datasheet"].strip()
    part["mpns"] = [
        {
            "manufacturer": m["manufacturer"].strip(),
            "mpn": m["mpn"].strip(),
            **({"lcsc": m["lcsc"].strip()} if m.get("lcsc", "").strip() else {}),
            "lifecycle": "unknown",
        }
        for m in mpns
        if m.get("manufacturer", "").strip() or m.get("mpn", "").strip()
    ]
    if acknowledge:
        part["near_match_ack"] = sorted(set(acknowledge))
    if fields.get("notes", "").strip():
        part["notes"] = fields["notes"].strip()
    if not part["description"]:
        _findings, record = evaluate(root, f"parts/{category.name}/{ipn}.yaml", part)
        suggestion = None
        if record is not None and all(p.name in record.normalised for p in category.identity_params()):
            try:
                suggestion = category.render_description(record.normalised)
            except (KeyError, ValueError):
                suggestion = None
        if suggestion:
            part["description"] = suggestion
    return f"parts/{category.name}/{ipn}.yaml", part


def create(root: Path, path: str, part: dict) -> str:
    return save(root, path, part, f"Add {part['ipn']}: {part['description']}")


def release(root: Path, ipn: str, lifecycle: str, checked: date, checked_by: str | None = None,
            extra: dict[str, bytes] | None = None) -> str:
    """Record that the part was checked against its datasheet today, and release it.

    `checked_by` records a tool that did the check instead of a person, with `extra` its evidence.
    """
    path, part = load_part(root, ipn)
    if part.get("status") != "draft":
        raise EditError(f"{ipn} is {part.get('status')}, not a draft")
    mpns = part.get("mpns")
    if not isinstance(mpns, list) or not mpns or not isinstance(mpns[0], dict):
        raise EditError(f"{ipn} has no MPN to verify")
    mpns[0]["verified"] = checked
    mpns[0]["lifecycle"] = lifecycle
    part["footprint_checked"] = checked
    part["status"] = "released"
    if checked_by:
        part["checked_by"] = checked_by
    notes = part.get("notes")
    if isinstance(notes, str) and IMPORT_NOTE_START in notes:
        kept = notes[: notes.index(IMPORT_NOTE_START)].strip()
        part["notes"] = kept
    how = " (datasheet check)" if checked_by else ""
    return save(root, path, part, f"Release {ipn}{how}: {part.get('description', '')}", extra=extra)


def update(root: Path, ipn: str, fields: dict[str, str], params: dict[str, str] | None, mpns: list[dict[str, str]], status: str | None) -> str:
    """Edit a part's text fields and MPNs, its params while a draft, and retire it."""
    path, part = load_part(root, ipn)
    for key in ("description", "datasheet", "notes"):
        if key in fields:
            value = fields[key].strip()
            if value or key == "description":  # an empty description stays, so check reports it
                part[key] = value
            else:
                part.pop(key, None)
    if params is not None:
        if part.get("status") != "draft":
            raise EditError(f"{ipn} is {part.get('status')}; its parameters can no longer change")
        category = load_categories(root).get(part.get("category"))
        if category is None:
            raise EditError(f"{ipn} has an unknown category")
        part["params"] = _canonical_params(category, {k: v.strip() for k, v in params.items() if v.strip()})
    old = {(m.get("manufacturer"), m.get("mpn")): m for m in part.get("mpns") or [] if isinstance(m, dict)}
    new_mpns = []
    for entry in mpns:
        manufacturer, mpn = entry.get("manufacturer", "").strip(), entry.get("mpn", "").strip()
        if not manufacturer and not mpn:
            continue
        kept = dict(old.get((manufacturer, mpn), {}))  # an unchanged MPN keeps its verified date
        kept.update({"manufacturer": manufacturer, "mpn": mpn, "lifecycle": entry.get("lifecycle") or kept.get("lifecycle", "unknown")})
        lcsc = entry.get("lcsc", "").strip()
        if lcsc:
            kept["lcsc"] = lcsc
        else:
            kept.pop("lcsc", None)
        new_mpns.append(kept)
    part["mpns"] = new_mpns
    if status and status != part.get("status"):
        allowed = {"released": ("deprecated", "obsolete"), "deprecated": ("obsolete",)}
        if status not in allowed.get(part.get("status"), ()):
            raise EditError(f"{ipn} cannot go from {part.get('status')} to {status} here")
        part["status"] = status
    return save(root, path, part, f"Edit {ipn}: {part.get('description', '')}")

