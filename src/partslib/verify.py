"""Check a part against its datasheet automatically, and release it if every check passes.

Claude only *extracts*: what the datasheet states about this part, each with a page and a quote.
partslib then *compares*, deterministically, against the part file, the symbol's pins and the
footprint's pad geometry. A fact the datasheet does not state is "unknown", which never passes,
so a part is released only when every check has positive evidence. The evidence is committed with
the release in verification/<IPN>.yaml, and the part records `checked_by`, so it stays clear which
parts a tool checked rather than a person.
"""

from dataclasses import asdict, dataclass, field
from datetime import date
import base64
import hashlib
import json
import math
from pathlib import Path
import re
import urllib.error
import urllib.request

import yaml

from partslib import edit, kicadgen, sexpr
from partslib.check import Assets, check_records
from partslib.library import Category, load_categories, part_paths

MODEL = "claude-opus-5-5"
PAD_TOLERANCE_MM = 0.25  # pad size and pad-to-pad spacing: manufacturer and IPC patterns differ a little
PITCH_TOLERANCE_MM = 0.02  # pitch must match: a wrong pitch means a wrong package
MAX_PDF_BYTES = 24 * 1024 * 1024  # base64 grows it by a third; the request limit is 32 MB

SYSTEM = (
    "You read electronic component datasheets and report exactly what they state about one part. "
    "Report only what the datasheet itself states. If it does not state something, mark it as not "
    "stated rather than inferring it from typical values, the part number or other parts. Give the "
    "page number and a short verbatim quote for everything you report."
)

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["covers_part", "covers_part_page", "covers_part_quote", "values", "pins", "land_pattern"],
    "properties": {
        "covers_part": {"type": "boolean", "description": "Does this datasheet cover the exact manufacturer part number asked about?"},
        "covers_part_page": {"type": "integer"},
        "covers_part_quote": {"type": "string"},
        "values": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["param", "stated", "value", "page", "quote"],
                "properties": {
                    "param": {"type": "string"},
                    "stated": {"type": "boolean"},
                    "value": {"type": "string", "description": "The value with its unit, as the datasheet gives it for this part"},
                    "page": {"type": "integer"},
                    "quote": {"type": "string"},
                },
            },
        },
        "pins": {
            "type": "object",
            "additionalProperties": False,
            "required": ["stated", "page", "pins"],
            "properties": {
                "stated": {"type": "boolean"},
                "page": {"type": "integer"},
                "pins": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["number", "name"],
                        "properties": {"number": {"type": "string"}, "name": {"type": "string"}},
                    },
                },
            },
        },
        "land_pattern": {
            "type": "object",
            "additionalProperties": False,
            "required": ["stated", "page", "pads", "spacing_stated", "center_spacing_mm", "pitch_stated", "pitch_mm", "notes"],
            "properties": {
                "stated": {"type": "boolean", "description": "Does the datasheet give a recommended land pattern (PCB pads) for this package?"},
                "page": {"type": "integer"},
                "pads": {
                    "type": "array",
                    "description": "Each distinct pad size in the recommended land pattern, in millimetres",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["count", "width_mm", "length_mm"],
                        "properties": {"count": {"type": "integer"}, "width_mm": {"type": "number"}, "length_mm": {"type": "number"}},
                    },
                },
                "spacing_stated": {"type": "boolean"},
                "center_spacing_mm": {"type": "number", "description": "For two-pad parts: distance between the two pad centres"},
                "pitch_stated": {"type": "boolean"},
                "pitch_mm": {"type": "number", "description": "For multi-pin parts: centre-to-centre distance between adjacent pins"},
                "notes": {"type": "string"},
            },
        },
    },
}


class VerifyError(Exception):
    pass


@dataclass
class Check:
    name: str
    outcome: str  # pass | fail | unknown
    detail: str
    page: int | None = None
    quote: str = ""


@dataclass
class Result:
    ipn: str
    checks: list[Check] = field(default_factory=list)
    model: str = ""
    datasheet: str = ""
    datasheet_sha256: str = ""
    released: str | None = None  # commit, if it was released

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.outcome == "pass" for c in self.checks)


# --- Datasheet download ---------------------------------------------------------------------------

def fetch_datasheet(root: Path, url: str) -> bytes:
    """Download a datasheet PDF, following LCSC's viewer page to the file itself. Cached by URL."""
    cache = root / "build/datasheets" / f"{hashlib.sha256(url.encode()).hexdigest()[:24]}.pdf"
    if cache.is_file():
        return cache.read_bytes()
    data = _get(url)
    if not data.startswith(b"%PDF"):
        link = re.search(rb'https://datasheet\.lcsc\.com/datasheet/pdf/[0-9a-f]+\.pdf(?:\?productCode=C\d+)?', data)
        if link is None:
            raise VerifyError(f"the datasheet link gives a web page, not a PDF: {url}")
        data = _get(link.group(0).decode())
        if not data.startswith(b"%PDF"):
            raise VerifyError(f"the datasheet link does not lead to a PDF: {url}")
    if len(data) > MAX_PDF_BYTES:
        raise VerifyError(f"the datasheet is {len(data) // 1_000_000} MB, over the {MAX_PDF_BYTES // 1_000_000} MB that fits in one request")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(data)
    return data


def _get(url: str) -> bytes:
    if not url.startswith(("https://", "http://")):
        raise VerifyError(f"not a web link: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (partslib datasheet check)"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - http(s) checked above
            return response.read()
    except (urllib.error.URLError, TimeoutError) as error:
        raise VerifyError(f"could not download the datasheet: {error}") from None


# --- What the library says ------------------------------------------------------------------------

@dataclass
class Pad:
    number: str
    x: float
    y: float
    width: float
    length: float  # sorted: width <= length, so rotation does not matter


def footprint_pads(footprint_text: str) -> list[Pad]:
    pads = []
    for node in sexpr.children(sexpr.parse(footprint_text), "pad"):
        number = node[1] if len(node) > 1 and isinstance(node[1], str) else ""
        at, size = sexpr.child(node, "at"), sexpr.child(node, "size")
        if not number or at is None or size is None:
            continue
        a, b = float(size[1]), float(size[2])
        pads.append(Pad(number, float(at[1]), float(at[2]), min(a, b), max(a, b)))
    return pads


def _question(record, category: Category, pin_count: int) -> str:
    mpn = record.mpns[0]
    lines = [
        f"Part: {mpn.get('manufacturer', '')} {mpn.get('mpn', '')}",
        f"Category: {category.name}. Package: {record.raw.get('params', {}).get('package', 'not given')}.",
        "",
        "Report what this datasheet states for exactly this part number:",
        "1. Whether the datasheet covers this exact part number (decode the part-numbering scheme if it is a series datasheet).",
        "2. These values, one entry each, using these names for `param`:",
    ]
    for name, param in category.params.items():
        if name in record.raw.get("params", {}):
            hint = {"quantity": f"a value in {param.unit}", "choice": f"one of {', '.join(param.choices)}"}.get(param.kind, "text")
            lines.append(f"   - {name} ({hint})")
    if pin_count > 2:
        lines.append(f"3. The pin table: every pin number and name for this package ({pin_count} pins expected).")
    else:
        lines.append("3. Pins: report stated=false; a two-terminal part has no pin table to check.")
    lines.append(
        "4. The recommended land pattern (PCB pad layout) for this package, if the datasheet gives one: "
        "each distinct pad size in mm, and either the centre-to-centre spacing of the two pads or the pin pitch."
    )
    return "\n".join(lines)


def extract(client, pdf: bytes, question: str) -> tuple[dict, str]:
    """Ask Claude what the datasheet states. Returns the facts and the model that answered."""
    import anthropic

    try:
        with client.beta.messages.stream(
            model=MODEL,
            max_tokens=16000,
            # On a policy decline the API reroutes to a fallback model inside the same call.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": SCHEMA}},
            system=SYSTEM,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {"type": "base64", "media_type": "application/pdf",
                                   "data": base64.standard_b64encode(pdf).decode("ascii")},
                        # Several parts often share a datasheet; checking them in a row reuses it.
                        "cache_control": {"type": "ephemeral"},
                    },
                    {"type": "text", "text": question},
                ],
            }],
        ) as stream:
            message = stream.get_final_message()
    except anthropic.AuthenticationError:
        raise VerifyError("the Claude API rejected the credentials: set ANTHROPIC_API_KEY") from None
    except anthropic.PermissionDeniedError as error:
        raise VerifyError(f"the API key cannot use {MODEL}: {error.message}") from None
    except anthropic.BadRequestError as error:
        raise VerifyError(f"the Claude API refused the request: {error.message}") from None
    except anthropic.RateLimitError:
        raise VerifyError("the Claude API is rate limiting; try again in a minute") from None
    except anthropic.APIStatusError as error:
        raise VerifyError(f"the Claude API failed ({error.status_code}); try again later") from None
    except anthropic.APIConnectionError:
        raise VerifyError("could not reach the Claude API; check the network") from None
    if message.stop_reason == "refusal":
        raise VerifyError("Claude declined to read this datasheet")
    if message.stop_reason == "max_tokens":
        raise VerifyError("the answer was cut off; the datasheet may be too large to check in one go")
    text = next((block.text for block in message.content if block.type == "text"), None)
    if text is None:
        raise VerifyError("Claude returned no answer")
    try:
        return json.loads(text), message.model
    except ValueError:
        raise VerifyError("Claude's answer was not valid JSON") from None


# --- Comparing ------------------------------------------------------------------------------------

def _clean(value: str) -> str:
    return value.replace("±", "").replace("+/-", "").strip()


def compare_values(record, category: Category, facts: dict) -> list[Check]:
    stated = {v.get("param"): v for v in facts.get("values", []) if isinstance(v, dict)}
    checks = []
    for name, param in category.params.items():
        if name not in record.normalised:
            continue
        ours = record.normalised[name]
        fact = stated.get(name)
        if not fact or not fact.get("stated"):
            checks.append(Check(f"value {name}", "unknown", "the datasheet does not state it"))
            continue
        page, quote, theirs_text = fact.get("page"), fact.get("quote", ""), str(fact.get("value", ""))
        try:
            if param.kind == "text":
                theirs = theirs_text.strip().upper()
                same = str(ours) == theirs or re.search(rf"(?<![0-9A-Z]){re.escape(str(ours))}(?![0-9A-Z])", theirs) is not None
            elif param.kind == "choice":
                theirs = param.normalise(re.sub(r"[\s-]+", "_", theirs_text.strip()))
                same = theirs == ours
            else:
                theirs = param.normalise(_clean(theirs_text))
                same = theirs == ours
        except ValueError:
            checks.append(Check(f"value {name}", "unknown", f"cannot read the datasheet's {theirs_text!r}", page, quote))
            continue
        shown = record.raw.get("params", {}).get(name)
        if same:
            checks.append(Check(f"value {name}", "pass", f"{shown} matches {theirs_text!r}", page, quote))
        else:
            checks.append(Check(f"value {name}", "fail", f"part says {shown}, datasheet says {theirs_text!r}", page, quote))
    return checks


def compare_pins(pins: set[str], facts: dict) -> Check | None:
    if len(pins) <= 2:
        return None  # two-terminal parts: nothing to get wrong that the footprint check misses
    table = facts.get("pins") or {}
    if not table.get("stated"):
        return Check("pins", "unknown", "the datasheet has no pin table for this package")
    theirs = {str(p.get("number", "")).strip() for p in table.get("pins", []) if str(p.get("number", "")).strip()}
    page = table.get("page")
    if theirs == pins:
        return Check("pins", "pass", f"all {len(pins)} pin numbers match", page)
    missing, extra = sorted(pins - theirs), sorted(theirs - pins)
    return Check("pins", "fail", f"symbol pins not in the datasheet: {missing or 'none'}; datasheet pins not in the symbol: {extra or 'none'}", page)


def compare_footprint(pads: list[Pad], facts: dict) -> list[Check]:
    land = facts.get("land_pattern") or {}
    if not land.get("stated"):
        return [Check("footprint", "unknown", "the datasheet gives no recommended land pattern")]
    page, checks = land.get("page"), []
    sizes = [(min(float(p["width_mm"]), float(p["length_mm"])), max(float(p["width_mm"]), float(p["length_mm"])), int(p.get("count", 1)))
             for p in land.get("pads", []) if isinstance(p, dict)]
    if not sizes:
        return [Check("footprint", "unknown", "the land pattern gives no pad sizes", page)]
    numbered = len({p.number for p in pads})
    expected = sum(count for _w, _l, count in sizes)
    if expected not in (len(pads), numbered):
        checks.append(Check("footprint pad count", "fail", f"footprint has {len(pads)} pads, datasheet {expected}", page))
    else:
        checks.append(Check("footprint pad count", "pass", f"{expected} pads", page))
    for width, length, count in sizes:
        near = [p for p in pads if abs(p.width - width) <= PAD_TOLERANCE_MM and abs(p.length - length) <= PAD_TOLERANCE_MM]
        if len(near) >= count:
            checks.append(Check("footprint pad size", "pass", f"{count} × {width}×{length} mm within {PAD_TOLERANCE_MM} mm", page))
        else:
            ours = sorted({(round(p.width, 3), round(p.length, 3)) for p in pads})
            checks.append(Check("footprint pad size", "fail", f"datasheet {count} × {width}×{length} mm; footprint pads {ours}", page))
    if len(pads) == 2:
        if land.get("spacing_stated"):
            ours = math.dist((pads[0].x, pads[0].y), (pads[1].x, pads[1].y))
            theirs = float(land.get("center_spacing_mm", 0))
            outcome = "pass" if abs(ours - theirs) <= PAD_TOLERANCE_MM else "fail"
            checks.append(Check("footprint pad spacing", outcome, f"footprint {ours:.3f} mm, datasheet {theirs} mm", page))
        else:
            checks.append(Check("footprint pad spacing", "unknown", "the datasheet does not give the pad spacing", page))
    elif land.get("pitch_stated"):
        distances = [math.dist((a.x, a.y), (b.x, b.y)) for i, a in enumerate(pads) for b in pads[i + 1:]]
        ours = min(d for d in distances if d > 0.01)
        theirs = float(land.get("pitch_mm", 0))
        outcome = "pass" if abs(ours - theirs) <= PITCH_TOLERANCE_MM else "fail"
        checks.append(Check("footprint pitch", outcome, f"footprint {ours:.3f} mm, datasheet {theirs} mm", page))
    else:
        checks.append(Check("footprint pitch", "unknown", "the datasheet does not give the pin pitch", page))
    return checks


# --- The whole check ------------------------------------------------------------------------------

def verify(root: Path, ipn: str, client=None, release: bool = True) -> Result:
    categories = load_categories(root)
    sources = {p.as_posix(): (root / p).read_bytes() for p in part_paths(root)}
    records, _ = check_records(root, sources, categories)
    record = next((r for r in records if r.ipn == ipn), None)
    if record is None:
        raise VerifyError(f"no part {ipn}")
    if record.status != "draft":
        raise VerifyError(f"{ipn} is {record.status}; only drafts are checked for release")
    if record.category is None or not record.mpns:
        raise VerifyError(f"{ipn} needs a category and an MPN first")
    raw = record.raw
    for key in ("datasheet", "symbol", "footprint"):
        if not raw.get(key):
            raise VerifyError(f"{ipn} has no {key}")
    assets = Assets(root)
    try:
        pins = kicadgen.pin_numbers(assets.symbol_block(raw["symbol"]))
        pads = footprint_pads(assets.footprint_text(raw["footprint"]))
    except (KeyError, ValueError) as error:
        raise VerifyError(f"{ipn}: {error}") from None

    pdf = fetch_datasheet(root, raw["datasheet"])
    if client is None:
        try:
            import anthropic
        except ImportError:
            raise VerifyError("datasheet checks need the Anthropic SDK: .venv/bin/pip install -e '.[verify]'") from None
        client = anthropic.Anthropic()
    facts, model = extract(client, pdf, _question(record, record.category, len(pins)))

    result = Result(ipn, model=model, datasheet=raw["datasheet"], datasheet_sha256=hashlib.sha256(pdf).hexdigest())
    covers = facts.get("covers_part") is True
    result.checks.append(Check("datasheet covers the MPN", "pass" if covers else "fail",
                               f"{record.mpns[0].get('mpn')}", facts.get("covers_part_page"), facts.get("covers_part_quote", "")))
    result.checks += compare_values(record, record.category, facts)
    pin_check = compare_pins(pins, facts)
    if pin_check:
        result.checks.append(pin_check)
    result.checks += compare_footprint(pads, facts)

    if release and result.passed:
        evidence_path = f"verification/{ipn}.yaml"
        evidence = {
            "ipn": ipn,
            "checked": date.today(),
            "method": "datasheet-check: Claude extracts, partslib compares",
            "model": model,
            "datasheet": result.datasheet,
            "datasheet_sha256": result.datasheet_sha256,
            "tolerances_mm": {"pad": PAD_TOLERANCE_MM, "pitch": PITCH_TOLERANCE_MM},
            "checks": [asdict(c) for c in result.checks],
        }
        text = yaml.safe_dump(evidence, sort_keys=False, allow_unicode=True, width=1000).encode("utf-8")
        result.released = edit.release(
            root, ipn, str(record.mpns[0].get("lifecycle") or "unknown"), date.today(),
            checked_by=f"datasheet-check ({model}), evidence in {evidence_path}",
            extra={evidence_path: text},
        )
    return result
