"""Generate KiCad library files. All KiCad file output goes through this module (spec section 13).

Text from base symbols and footprints is copied verbatim wherever possible, so output stays close
to what KiCad writes and diffs cleanly. Output depends only on the inputs: no timestamps, no random
UUIDs.
"""

from dataclasses import dataclass
from pathlib import Path
import re
import shutil

SYMBOL_LIB_VERSION = "20251024"
GENERATOR = "partslib"
GENERATOR_VERSION = "0.0"

# Fields inherited from the base symbol's layout, in KiCad's own order.
BASE_FIELDS = ("Reference", "Value", "Footprint", "Datasheet", "Description")
# Optional search metadata copied from the base symbol when present.
METADATA_FIELDS = ("ki_keywords", "ki_fp_filters")


@dataclass(frozen=True)
class Part:
    ipn: str
    value: str
    description: str
    datasheet: str
    footprint: str  # library id, such as PL_Resistor:R_0603
    manufacturer: str
    mpn: str
    lcsc: str = ""

    def custom_fields(self) -> dict[str, str]:
        return {
            "IPN": self.ipn,
            "Manufacturer": self.manufacturer,
            "MPN": self.mpn,
            "LCSC": self.lcsc,
        }

    def base_field_values(self) -> dict[str, str]:
        return {
            "Value": self.value,
            "Footprint": self.footprint,
            "Datasheet": self.datasheet,
            "Description": self.description,
        }


def quote(value: str) -> str:
    """Quote a string as a KiCad s-expression atom."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def block_end(text: str, start: int) -> int:
    """Return the index just past the balanced s-expression that opens at text[start]."""
    if text[start] != "(":
        raise ValueError(f"expected '(' at offset {start}")
    depth = 0
    in_string = False
    i = start
    while i < len(text):
        char = text[i]
        if in_string:
            if char == "\\":
                i += 1
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("unbalanced s-expression")


def _find_block(text: str, opening: str) -> str | None:
    match = re.search(re.escape(opening) + r"(?=[\s)])", text)
    return text[match.start() : block_end(text, match.start())] if match else None


def extract_symbol(library_text: str, name: str) -> str:
    """Return a top-level symbol's block, which must be a complete symbol and not a derived one."""
    block = _find_block(library_text, f"(symbol {quote(name)}")
    if block is None:
        raise KeyError(f"symbol {name!r} not found")
    if re.match(r'\(symbol "[^"]*"\s*\(extends ', block):
        raise ValueError(f"symbol {name!r} is itself derived; a base symbol must be complete")
    return block


def pin_numbers(symbol_block: str) -> set[str]:
    """Return the pin numbers of a complete symbol."""
    return set(re.findall(r'\(number "((?:[^"\\]|\\.)*)"', symbol_block))


def pad_numbers(footprint_text: str) -> set[str]:
    """Return the numbered pads of a footprint. Unnumbered mechanical pads don't count."""
    return {number for number in re.findall(r'\(pad "((?:[^"\\]|\\.)*)"', footprint_text) if number}


def _set_property_value(property_block: str, value: str) -> str:
    return re.sub(
        r'^(\(property "(?:[^"\\]|\\.)*" )"(?:[^"\\]|\\.)*"',
        lambda match: match.group(1) + quote(value),
        property_block,
        count=1,
    )


def _hidden_property(name: str, value: str, indent: str) -> str:
    lines = [
        f"(property {quote(name)} {quote(value)}",
        "\t(at 0 0 0)",
        "\t(show_name no)",
        "\t(do_not_autoplace no)",
        "\t(hide yes)",
        "\t(effects",
        "\t\t(font",
        "\t\t\t(size 1.27 1.27)",
        "\t\t)",
        "\t)",
        ")",
    ]
    return lines[0] + "".join(f"\n{indent}{line}" for line in lines[1:])


def child_spans(block: str) -> list[tuple[int, int]]:
    """Return the spans of a block's direct child s-expressions, ignoring parens inside strings."""
    spans = []
    in_string = False
    i = 1
    while i < len(block) - 1:
        char = block[i]
        if in_string:
            if char == "\\":
                i += 1
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "(":
            end = block_end(block, i)
            spans.append((i, end))
            i = end
            continue
        i += 1
    return spans


def _property_name(child: str) -> str | None:
    match = re.match(r'\(property "((?:[^"\\]|\\.)*)"', child)
    return match.group(1) if match else None


def part_properties(base_block: str, part: Part, reference: str | None = None) -> list[str]:
    """Return a part's property blocks in the order KiCad writes them, so re-saves diff cleanly.

    Standard fields keep the base symbol's layout, other base fields follow, then the part's own
    fields, then KiCad's search metadata. A part's own field replaces a base field of that name.
    """
    values = part.base_field_values()
    if reference is not None:
        values["Reference"] = reference
    custom = part.custom_fields()
    base = {}
    for start, end in child_spans(base_block):
        name = _property_name(base_block[start:end])
        if name is not None:
            base[name] = base_block[start:end]
    properties = []
    for field in BASE_FIELDS:
        if field not in base:
            raise ValueError(f"base symbol has no {field} property")
        properties.append(_set_property_value(base[field], values[field]) if field in values else base[field])
    reserved = {*BASE_FIELDS, *METADATA_FIELDS, *custom}
    properties += [block for name, block in base.items() if name not in reserved]
    properties += [_hidden_property(name, value, "\t\t") for name, value in custom.items()]
    properties += [base[field] for field in METADATA_FIELDS if field in base]
    return properties


def part_symbol(base_name: str, base_block: str, part: Part, name: str | None = None) -> str:
    """Return a complete symbol for a part: the base's graphics and pins, with every field filled in.

    Parts are flattened rather than using `extends`, because `extends` only resolves within one
    file, so the base would have to ship in the library and could be placed without an IPN.
    `name` overrides the symbol name, as for the `<library>:<IPN>` copy a schematic embeds.
    """
    children = child_spans(base_block)
    is_property = [_property_name(base_block[start:end]) is not None for start, end in children]
    if True not in is_property:
        raise ValueError(f"base symbol {base_name!r} has no properties")
    first = is_property.index(True)
    last = len(is_property) - 1 - is_property[::-1].index(True)
    if not all(is_property[first : last + 1]):
        raise ValueError(f"base symbol {base_name!r} has other entries between its properties")
    head = base_block[: children[first][0]]
    tail = base_block[children[last][1] :]
    head, renamed = re.subn(
        rf"^\(symbol {re.escape(quote(base_name))}", lambda m: f"(symbol {quote(name or part.ipn)}", head
    )
    if renamed != 1:
        raise ValueError(f"block is not symbol {base_name!r}")
    # KiCad requires unit names to start with the symbol name, without any library nickname.
    tail = re.sub(
        rf'\(symbol {re.escape(quote(base_name))[:-1]}_(\d+_\d+)"',
        lambda m: f"(symbol {quote(part.ipn)[:-1]}_{m.group(1)}\"",
        tail,
    )
    return head + "\n\t\t".join(part_properties(base_block, part)) + tail


def _split_properties(block: str, name: str) -> tuple[str, list[str], str]:
    """(text before the properties, property blocks, text after), for a symbol block."""
    children = child_spans(block)
    is_property = [_property_name(block[start:end]) is not None for start, end in children]
    if True not in is_property:
        raise ValueError(f"symbol {name!r} has no properties")
    first = is_property.index(True)
    last = len(is_property) - 1 - is_property[::-1].index(True)
    if not all(is_property[first : last + 1]):
        raise ValueError(f"symbol {name!r} has other entries between its properties")
    properties = [block[start:end] for start, end in children[first : last + 1]]
    return block[: children[first][0]], properties, block[children[last][1] :]


def complete_symbol(library_text: str, name: str) -> str:
    """Return a symbol as one complete block, resolving `extends` from the same library.

    A derived symbol takes its parent's graphics, pins and flags, with its own properties.
    """
    block = _find_block(library_text, f"(symbol {quote(name)}")
    if block is None:
        raise KeyError(f"symbol {name!r} not found")
    parent = re.match(r'\(symbol "[^"]*"\s*\(extends "((?:[^"\\]|\\.)*)"\)', block)
    if parent is None:
        return block
    base = complete_symbol(library_text, parent.group(1))
    head, _base_properties, tail = _split_properties(base, parent.group(1))
    _own_head, own_properties, _own_tail = _split_properties(block, name)
    head = re.sub(rf"^\(symbol {re.escape(quote(parent.group(1)))}", lambda m: f"(symbol {quote(name)}", head)
    tail = re.sub(
        rf'\(symbol {re.escape(quote(parent.group(1)))[:-1]}_(\d+_\d+)"',
        lambda m: f"(symbol {quote(name)[:-1]}_{m.group(1)}\"",
        tail,
    )
    return head + "\n\t\t".join(own_properties) + tail


def symbol_library(entries: list[tuple[str, str, Part]]) -> str:
    """Return a .kicad_sym file with one complete symbol per part, in IPN order. Bases are not included.

    Each entry is (base symbol name, base symbol block, part).
    """
    ordered = sorted(entries, key=lambda entry: entry[2].ipn)
    blocks = [part_symbol(base_name, base_block, part) for base_name, base_block, part in ordered]
    body = "".join(f"\t{block}\n" for block in blocks)
    return (
        "(kicad_symbol_lib\n"
        f"\t(version {SYMBOL_LIB_VERSION})\n"
        f"\t(generator {quote(GENERATOR)})\n"
        f"\t(generator_version {quote(GENERATOR_VERSION)})\n"
        f"{body})\n"
    )


def footprint_file(source_text: str, name: str, model_uri: str) -> str:
    """Return a .kicad_mod renamed to `name`, with its 3D model pointing at `model_uri`."""
    text, renamed = re.subn(r'^\(footprint "[^"]*"', f"(footprint {quote(name)}", source_text, count=1)
    if renamed != 1:
        raise ValueError("not a footprint file")
    text = re.sub(
        r'\(property "Value" "[^"]*"',
        lambda match: f'(property "Value" {quote(name)}',
        text,
        count=1,
    )
    text, models = re.subn(r'\(model "[^"]*"', lambda match: f"(model {quote(model_uri)}", text)
    if models != 1:
        raise ValueError(f"expected one 3D model in footprint source, found {models}")
    return text


def write_category(
    out: Path,
    library: str,
    base_blocks: dict[str, str],
    parts_by_base: dict[str, list[Part]],
    footprints: dict[str, str],
    models: dict[str, Path],
) -> None:
    """Write one category's libraries under `out`, using the layout in spec section 6.

    `footprints` maps footprint name to source .kicad_mod text; `models` maps it to a STEP file.
    """
    symbols_dir = out / "symbols"
    footprints_dir = out / "footprints" / f"{library}.pretty"
    models_dir = out / "3dmodels" / f"{library}.3dshapes"
    for directory in (symbols_dir, footprints_dir, models_dir):
        directory.mkdir(parents=True, exist_ok=True)

    entries = [(name, base_blocks[name], part) for name, parts in parts_by_base.items() for part in parts]
    (symbols_dir / f"{library}.kicad_sym").write_text(symbol_library(entries), encoding="utf-8")
    for name, source_text in footprints.items():
        model_name = f"{name}.step"
        uri = "${PARTSLIB}/3dmodels/" + f"{library}.3dshapes/{model_name}"
        (footprints_dir / f"{name}.kicad_mod").write_text(
            footprint_file(source_text, name, uri), encoding="utf-8"
        )
        shutil.copyfile(models[name], models_dir / model_name)
