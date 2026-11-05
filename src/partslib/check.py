"""`partslib check`: validate the library (spec sections 5.1 to 5.5).

Every rule has a code in RULES, and tests/test_check.py requires a failing case for each code.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from fractions import Fraction
from pathlib import Path
import re
import subprocess

from partslib import kicadgen
from partslib.library import (
    Category,
    LibraryError,
    fingerprint,
    load_categories,
    load_yaml,
    near_key,
    normalise_mpn,
    part_paths,
)

RULES = {
    "yaml-invalid": "file is not valid YAML, or has a duplicate key or impossible date",
    "not-mapping": "file does not contain a mapping",
    "unknown-key": "file has a key that is not in the schema",
    "missing-key": "file lacks a required key",
    "field-invalid": "description or notes is not text, or description is empty",
    "ipn-format": "IPN is not <CAT>-<6 digits>",
    "ipn-prefix": "IPN prefix does not match the category",
    "path-mismatch": "file is not at parts/<category>/<IPN>.yaml",
    "duplicate-ipn": "two files have the same IPN",
    "unknown-category": "category has no definition in categories/",
    "status-invalid": "status is not draft, released, deprecated or obsolete",
    "param-unknown": "param is not defined for the category",
    "param-missing": "identity param is missing",
    "param-invalid": "param value cannot be normalised",
    "mpn-invalid": "MPN entry is malformed",
    "mpn-required": "MPN-based part has no MPN",
    "duplicate-mpn": "the same MPN is on two parts",
    "duplicate-part": "another part has the same identity",
    "near-match": "another part is similar, and neither part acknowledges it",
    "ack-invalid": "near_match_ack does not name an existing part",
    "asset-ref-invalid": "symbol, footprint or model reference is malformed",
    "released-incomplete": "released part lacks something section 5.4 requires",
    "asset-missing": "referenced symbol, footprint or model does not exist",
    "pin-pad-mismatch": "a symbol pin has no matching footprint pad",
    "provenance-invalid": "assets/provenance.yaml is malformed",
    "provenance-missing": "a released part's asset has no provenance entry",
    "supersession-invalid": "supersedes or superseded_by is inconsistent",
    "history-deleted": "a part in the compared revision no longer exists",
    "history-identity": "a non-draft part's identity params changed",
    "history-status": "a part's status moved backwards",
    "build-invalid": "parts pass every other rule but cannot be generated",
    "build-modified": "a file under build/ differs from what `partslib build` wrote",
    "build-stale": "build/ is out of date with parts/ and assets/",
}
BUILD_RULES = ("build-modified", "build-stale")

REQUIRED_KEYS = ("ipn", "status", "category", "description", "params")
OPTIONAL_KEYS = (
    "symbol",
    "footprint",
    "footprint_checked",
    "checked_by",
    "model",
    "datasheet",
    "mpns",
    "supersedes",
    "superseded_by",
    "near_match_ack",
    "notes",
)
STATUSES = ("draft", "released", "deprecated", "obsolete")
RETIRED = ("deprecated", "obsolete")
LIFECYCLES = ("active", "nrnd", "eol", "obsolete", "unknown")
MPN_REQUIRED = ("manufacturer", "mpn")
MPN_KEYS = (*MPN_REQUIRED, "lcsc", "lifecycle", "verified")
PROVENANCE_KEYS = ("kind", "id", "source", "licence", "version")
IPN_PATTERN = re.compile(r"([A-Z]{2,4})-(\d{6})")
LIBRARY_ITEM = re.compile(r"[\w.+-]+:[\w.+-]+")
MODEL_PATH = re.compile(r"[\w.+-]+/[\w.+-]+\.(?:step|stp)")


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    code: str
    message: str
    related: tuple[str, ...] = ()

    def __str__(self) -> str:
        return f"{self.path}: [{self.code}] {self.message}"


@dataclass
class Record:
    """One part file, with whatever could be understood from it."""

    path: str
    raw: dict
    category: Category | None = None
    normalised: dict[str, Fraction | str] = field(default_factory=dict)

    @property
    def ipn(self) -> str | None:
        ipn = self.raw.get("ipn")
        return ipn if isinstance(ipn, str) else None

    @property
    def status(self) -> object:
        return self.raw.get("status")

    @property
    def mpns(self) -> list[dict]:
        mpns = self.raw.get("mpns")
        return [m for m in mpns if isinstance(m, dict)] if isinstance(mpns, list) else []


def is_date(value: object) -> bool:
    if isinstance(value, date):
        return True
    if isinstance(value, str):
        try:
            date.fromisoformat(value)
        except ValueError:
            return False
        return True
    return False


def parse_record(path: str, source: str | bytes, categories: dict[str, Category], findings: list[Finding]) -> Record | None:
    """Validate one file on its own, returning a Record if it is a mapping."""

    def add(code: str, message: str) -> None:
        findings.append(Finding(path, code, message))

    try:
        raw = load_yaml(source)
    except ValueError as error:
        add("yaml-invalid", str(error))
        return None
    if not isinstance(raw, dict):
        add("not-mapping", "expected a mapping of part fields")
        return None

    record = Record(path, raw)
    for key in raw:
        if key not in REQUIRED_KEYS and key not in OPTIONAL_KEYS:
            add("unknown-key", f"unknown key {key!r}")
    for key in REQUIRED_KEYS:
        if key not in raw:
            add("missing-key", f"missing key {key!r}")

    if "description" in raw and not (isinstance(raw["description"], str) and raw["description"].strip()):
        add("field-invalid", "description must be non-empty text")
    if raw.get("notes") is not None and not isinstance(raw["notes"], str):
        add("field-invalid", "notes must be text")
    if raw.get("checked_by") is not None and not (isinstance(raw["checked_by"], str) and raw["checked_by"].strip()):
        add("field-invalid", "checked_by must name what checked the part")

    if "status" in raw and raw["status"] not in STATUSES:
        add("status-invalid", f"status {raw['status']!r} is not one of {', '.join(STATUSES)}")

    category_name = raw.get("category")
    if "category" in raw:
        category = categories.get(category_name) if isinstance(category_name, str) else None
        if category is None:
            add("unknown-category", f"unknown category {category_name!r}")
        record.category = category

    _check_location(record, add)
    _check_params(record, add)
    _check_mpns(record, add)
    _check_asset_refs(record, add)
    return record


def _check_location(record: Record, add) -> None:
    raw = record.raw
    if "ipn" not in raw:
        return
    match = IPN_PATTERN.fullmatch(raw["ipn"]) if isinstance(raw["ipn"], str) else None
    if match is None:
        add("ipn-format", f"IPN {raw['ipn']!r} is not <CAT>-<6 digits>")
    elif record.category is not None and match.group(1) != record.category.prefix:
        add(
            "ipn-prefix",
            f"IPN {raw['ipn']} should start with {record.category.prefix}- for category {record.category.name}",
        )
    expected = f"parts/{raw.get('category')}/{raw['ipn']}.yaml"
    if record.path != expected:
        add("path-mismatch", f"file should be at {expected}")


def _check_params(record: Record, add) -> None:
    params = record.raw.get("params")
    if "params" not in record.raw:
        return
    if not isinstance(params, dict):
        add("param-invalid", "params must be a mapping")
        return
    category = record.category
    if category is None:
        return
    for name, value in params.items():
        definition = category.params.get(name)
        if definition is None:
            add("param-unknown", f"category {category.name} has no param {name!r}")
            continue
        try:
            record.normalised[name] = definition.normalise(value)
        except ValueError as error:
            add("param-invalid", f"{name}: {error}")
    for param in category.identity_params():
        if param.name not in params:
            add("param-missing", f"identity param {param.name!r} is missing")


def _check_mpns(record: Record, add) -> None:
    mpns = record.raw.get("mpns")
    if mpns is not None and not isinstance(mpns, list):
        add("mpn-invalid", "mpns must be a list")
        return
    seen = set()
    for index, entry in enumerate(mpns or []):
        where = f"mpns[{index}]"
        if not isinstance(entry, dict):
            add("mpn-invalid", f"{where} must be a mapping")
            continue
        for key in entry:
            if key not in MPN_KEYS:
                add("mpn-invalid", f"{where} has unknown key {key!r}")
        for key in MPN_REQUIRED:
            if not isinstance(entry.get(key), str) or not entry[key].strip():
                add("mpn-invalid", f"{where} needs a {key}")
        lifecycle = entry.get("lifecycle")
        if lifecycle is not None and lifecycle not in LIFECYCLES:
            add("mpn-invalid", f"{where} lifecycle {lifecycle!r} is not one of {', '.join(LIFECYCLES)}")
        verified = entry.get("verified")
        if verified is not None and not is_date(verified):
            add("mpn-invalid", f"{where} verified {verified!r} is not a date")
        if isinstance(entry.get("mpn"), str) and isinstance(entry.get("manufacturer"), str):
            key = (entry["manufacturer"].strip().casefold(), normalise_mpn(entry["mpn"]))
            if key in seen:
                add("mpn-invalid", f"{where} repeats {entry['manufacturer']} {entry['mpn']}")
            seen.add(key)
    if record.category is not None and record.category.identity == "mpn" and not record.mpns:
        add("mpn-required", f"category {record.category.name} is identified by MPN, so one is required")


def _check_asset_refs(record: Record, add) -> None:
    raw = record.raw
    for key in ("symbol", "footprint"):
        value = raw.get(key)
        if value is not None and not (isinstance(value, str) and LIBRARY_ITEM.fullmatch(value)):
            add("asset-ref-invalid", f"{key} {value!r} is not <library>:<name>")
    model = raw.get("model")
    if model is not None and not (isinstance(model, str) and MODEL_PATH.fullmatch(model)):
        add("asset-ref-invalid", f"model {model!r} is not <library>/<name>.step")
    checked = raw.get("footprint_checked")
    if checked is not None and not is_date(checked):
        add("asset-ref-invalid", f"footprint_checked {checked!r} is not a date")


def _superseded_pair(first: Record, second: Record) -> bool:
    """A retired part and its replacement may legitimately share an identity, e.g. a footprint fix."""
    for old, new in ((first, second), (second, first)):
        if old.status in RETIRED and old.raw.get("superseded_by") == new.ipn and new.ipn:
            return True
    return False


def _acknowledged(first: Record, second: Record) -> bool:
    for one, other in ((first, second), (second, first)):
        acks = one.raw.get("near_match_ack")
        if isinstance(acks, list) and other.ipn in acks:
            return True
    return False


def check_across_parts(records: list[Record], findings: list[Finding]) -> None:
    by_ipn: dict[str, Record] = {}
    for record in records:
        if record.ipn is None:
            continue
        if record.ipn in by_ipn:
            findings.append(
                Finding(record.path, "duplicate-ipn", f"{record.ipn} is also in {by_ipn[record.ipn].path}", (record.ipn,))
            )
        else:
            by_ipn[record.ipn] = record

    identities: dict[tuple, list[Record]] = defaultdict(list)
    near: dict[tuple, list[Record]] = defaultdict(list)
    mpns: dict[str, list[Record]] = defaultdict(list)
    for record in records:
        if record.category is None:
            continue
        identity = fingerprint(record.category, record.normalised)
        if identity is not None:
            identities[identity].append(record)
        key = near_key(record.category, record.normalised)
        if key is not None:
            near[key].append(record)
    for record in records:
        for mpn in {normalise_mpn(m["mpn"]) for m in record.mpns if isinstance(m.get("mpn"), str)}:
            mpns[mpn].append(record)

    for group in identities.values():
        for later_index, later in enumerate(group):
            for earlier in group[:later_index]:
                if not _superseded_pair(earlier, later):
                    findings.append(
                        Finding(
                            later.path,
                            "duplicate-part",
                            f"{later.ipn} duplicates {earlier.ipn}: same "
                            + ", ".join(p.name for p in later.category.identity_params()),
                            (str(later.ipn), str(earlier.ipn)),
                        )
                    )
    for mpn, group in mpns.items():
        for later_index, later in enumerate(group):
            for earlier in group[:later_index]:
                if not _superseded_pair(earlier, later):
                    findings.append(
                        Finding(
                            later.path,
                            "duplicate-mpn",
                            f"MPN {mpn} on {later.ipn} is already on {earlier.ipn}",
                            (str(later.ipn), str(earlier.ipn)),
                        )
                    )
    for group in near.values():
        for later_index, later in enumerate(group):
            for earlier in group[:later_index]:
                same_identity = fingerprint(later.category, later.normalised) == fingerprint(
                    earlier.category, earlier.normalised
                )
                if same_identity or _superseded_pair(earlier, later) or _acknowledged(earlier, later):
                    continue
                findings.append(
                    Finding(
                        later.path,
                        "near-match",
                        f"{later.ipn} is similar to {earlier.ipn}; if both are wanted, "
                        f"add {earlier.ipn} to near_match_ack",
                        (str(later.ipn), str(earlier.ipn)),
                    )
                )

    for record in records:
        acks = record.raw.get("near_match_ack")
        if acks is None:
            continue
        if not isinstance(acks, list):
            findings.append(Finding(record.path, "ack-invalid", "near_match_ack must be a list of IPNs"))
            continue
        for ack in acks:
            if not isinstance(ack, str) or ack not in by_ipn:
                findings.append(Finding(record.path, "ack-invalid", f"near_match_ack names unknown part {ack!r}"))
    _check_supersession(records, by_ipn, findings)


def _check_supersession(records: list[Record], by_ipn: dict[str, Record], findings: list[Finding]) -> None:
    for record in records:
        def add(message: str) -> None:
            findings.append(Finding(record.path, "supersession-invalid", message))

        for key, reverse in (("superseded_by", "supersedes"), ("supersedes", "superseded_by")):
            target = record.raw.get(key)
            if target is None:
                continue
            if not isinstance(target, str):
                add(f"{key} must be an IPN, not {target!r}")
            elif target == record.ipn:
                add(f"{key} names the part itself")
            elif target not in by_ipn:
                add(f"{key} names unknown part {target!r}")
            elif by_ipn[target].raw.get(reverse) != record.ipn:
                add(f"{key} is {target}, but {target} does not name this part in {reverse}")
        if record.raw.get("superseded_by") is not None and record.status not in RETIRED:
            add("a superseded part must be deprecated or obsolete")


class Assets:
    """Looks up the assets a part refers to, reading each symbol library once."""

    def __init__(self, root: Path):
        self.root = root
        self._symbol_libraries: dict[str, str | None] = {}

    def symbol_block(self, reference: str) -> str:
        library, name = reference.split(":", 1)
        if library not in self._symbol_libraries:
            path = self.root / "assets/symbols" / f"{library}.kicad_sym"
            self._symbol_libraries[library] = path.read_text(encoding="utf-8") if path.is_file() else None
        text = self._symbol_libraries[library]
        if text is None:
            raise KeyError(f"symbol library {library}.kicad_sym does not exist")
        return kicadgen.extract_symbol(text, name)

    def footprint_text(self, reference: str) -> str:
        library, name = reference.split(":", 1)
        path = self.root / "assets/footprints" / f"{library}.pretty" / f"{name}.kicad_mod"
        if not path.is_file():
            raise KeyError(f"footprint {reference} does not exist")
        return path.read_text(encoding="utf-8")

    def model_exists(self, reference: str) -> bool:
        return (self.root / "assets/3dmodels" / reference).is_file()


def load_provenance(root: Path, findings: list[Finding]) -> set[tuple[str, str]]:
    path = "assets/provenance.yaml"
    file = root / path
    if not file.is_file():
        return set()

    def add(message: str) -> None:
        findings.append(Finding(path, "provenance-invalid", message))

    try:
        entries = load_yaml(file.read_text(encoding="utf-8"))
    except ValueError as error:
        add(str(error))
        return set()
    if entries is None:
        return set()
    if not isinstance(entries, list):
        add("expected a list of entries")
        return set()
    known = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            add(f"entry {index} must be a mapping")
            continue
        missing = [key for key in PROVENANCE_KEYS if not isinstance(entry.get(key), str) or not entry[key].strip()]
        if missing:
            add(f"entry {index} needs {', '.join(missing)}")
            continue
        if entry["kind"] not in ("symbol", "footprint", "model"):
            add(f"entry {index} has unknown kind {entry['kind']!r}")
            continue
        known.add((entry["kind"], entry["id"]))
    return known


def check_released(record: Record, assets: Assets, provenance: set[tuple[str, str]], findings: list[Finding]) -> None:
    """Section 5.4: a released part is complete, and its assets exist and fit together."""
    raw = record.raw

    def add(code: str, message: str) -> None:
        findings.append(Finding(record.path, code, message))

    for key in ("symbol", "footprint", "model", "datasheet", "footprint_checked"):
        if not raw.get(key):
            add("released-incomplete", f"released part needs {key}")
    datasheet = raw.get("datasheet")
    if datasheet and not (isinstance(datasheet, str) and re.match(r"https?://\S+$", datasheet)):
        add("released-incomplete", f"datasheet {datasheet!r} is not an http(s) link")
    if not any(is_date(m.get("verified")) for m in record.mpns):
        add("released-incomplete", "released part needs at least one MPN with a verified date")

    pins = pads = None
    symbol = raw.get("symbol")
    if isinstance(symbol, str) and LIBRARY_ITEM.fullmatch(symbol):
        try:
            pins = kicadgen.pin_numbers(assets.symbol_block(symbol))
        except (KeyError, ValueError) as error:
            add("asset-missing", f"symbol {symbol}: {error.args[0]}")
        if ("symbol", symbol) not in provenance:
            add("provenance-missing", f"symbol {symbol} has no provenance entry")
    footprint = raw.get("footprint")
    if isinstance(footprint, str) and LIBRARY_ITEM.fullmatch(footprint):
        try:
            pads = kicadgen.pad_numbers(assets.footprint_text(footprint))
        except KeyError as error:
            add("asset-missing", str(error.args[0]))
        if ("footprint", footprint) not in provenance:
            add("provenance-missing", f"footprint {footprint} has no provenance entry")
    model = raw.get("model")
    if isinstance(model, str) and MODEL_PATH.fullmatch(model):
        if not assets.model_exists(model):
            add("asset-missing", f"model {model} does not exist")
        if ("model", model) not in provenance:
            add("provenance-missing", f"model {model} has no provenance entry")

    if pins is not None and pads is not None and pins - pads:
        add("pin-pad-mismatch", f"pins {', '.join(sorted(pins - pads))} have no pad on {footprint}")


def check_history(root: Path, since: str, records: list[Record], categories: dict[str, Category], findings: list[Finding]) -> None:
    """Rules that need the past: parts are never deleted, and released identity never changes."""
    listed = _git(root, "ls-tree", "-r", "--name-only", since, "--", "parts").decode()
    current = {record.path: record for record in records}
    current_ipns = {record.ipn for record in records}
    for path in listed.splitlines():
        if not path.endswith((".yaml", ".yml")):
            continue
        ignored: list[Finding] = []
        old = parse_record(path, _git(root, "show", f"{since}:./{path}"), categories, ignored)
        if old is None:
            continue
        new = current.get(path)
        if new is None:
            if old.ipn not in current_ipns:
                findings.append(Finding(path, "history-deleted", f"{old.ipn} exists at {since} and has been removed"))
            continue
        if old.status in STATUSES and new.status in STATUSES:
            if STATUSES.index(new.status) < STATUSES.index(old.status):
                findings.append(
                    Finding(path, "history-status", f"status moved from {old.status} back to {new.status}")
                )
        if old.status != "draft" and old.category is not None and new.category is not None:
            before, after = fingerprint(old.category, old.normalised), fingerprint(new.category, new.normalised)
            if before is not None and after is not None and before != after:
                changed = [
                    param.name
                    for param in old.category.identity_params()
                    if old.normalised.get(param.name) != new.normalised.get(param.name)
                ]
                findings.append(
                    Finding(
                        path,
                        "history-identity",
                        f"identity params changed since {since}: {', '.join(changed)}; "
                        "create a new part and set superseded_by",
                    )
                )


def _git(root: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)
    if result.returncode != 0:
        message = result.stderr.decode(errors="replace").strip()
        raise LibraryError(f"git {' '.join(args)}: {message}")
    return result.stdout


def default_revision(root: Path) -> str | None:
    """HEAD if the root is in a git repository with commits, otherwise None."""
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "-q", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return "HEAD" if result.returncode == 0 else None


def check_records(
    root: Path, sources: dict[str, str | bytes], categories: dict[str, Category]
) -> tuple[list[Record], list[Finding]]:
    """Run every rule that does not need git history over the given file contents."""
    findings: list[Finding] = []
    records = []
    for path, source in sources.items():
        record = parse_record(path, source, categories, findings)
        if record is not None:
            records.append(record)
    check_across_parts(records, findings)
    provenance = load_provenance(root, findings)
    assets = Assets(root)
    for record in records:
        # Retired parts are still generated, so they must stay as complete as when released.
        if record.status in ("released", *RETIRED):
            check_released(record, assets, provenance, findings)
    return records, findings


def check_build(root: Path, records: list[Record], findings: list[Finding], include_build_dir: bool) -> None:
    """Generation must succeed, and build/ must be exactly what the last build wrote, and current."""
    from partslib import build  # build imports this module

    manifest = build.read_manifest(root) if include_build_dir else None
    if manifest is not None:
        actual = build.built_files(root)
        for path in sorted(set(manifest) | set(actual)):
            if manifest.get(path) != actual.get(path):
                findings.append(
                    Finding(
                        f"{build.BUILD_DIR}/{path}",
                        "build-modified",
                        "differs from what `partslib build` wrote; changes here are overwritten, "
                        "so make them in parts/ or assets/",
                    )
                )
    if findings and any(f.code not in BUILD_RULES for f in findings):
        return  # generating from invalid parts would only repeat those problems
    try:
        expected = build.generate(root, records)
    except build.BuildError as error:
        findings.append(Finding("parts", "build-invalid", str(error)))
        return
    if manifest is not None:
        if {path: build.digest(data) for path, data in expected.items()} != manifest:
            findings.append(
                Finding(
                    f"{build.BUILD_DIR}/{build.MANIFEST}",
                    "build-stale",
                    "build/ is out of date with parts/ and assets/; run `partslib build`",
                )
            )


def check_library(
    root: Path, since: str | None = None, include_build_dir: bool = True
) -> tuple[int, list[Finding]]:
    """Return the number of part files and every finding. Raises LibraryError on bad configuration.

    `include_build_dir=False` skips the rules about build/'s contents, which a rebuild fixes anyway.
    """
    categories = load_categories(root)
    sources = {path.as_posix(): (root / path).read_bytes() for path in part_paths(root)}
    records, findings = check_records(root, sources, categories)
    revision = since or default_revision(root)
    if revision is not None:
        check_history(root, revision, records, categories, findings)
    check_build(root, records, findings, include_build_dir)
    return len(sources), sorted(findings)
