"""Read category definitions and part files, and compute part identity (spec section 5)."""

from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
import re
import string

import yaml

from partslib.quantity import UNITS, format_engineering, parse_quantity


class LibraryError(Exception):
    """The library itself is misconfigured, as opposed to one part being invalid."""


class _StrictLoader(yaml.SafeLoader):
    """Rejects duplicate keys, which YAML otherwise resolves silently to the last value."""

    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _value_node in node.value:
            key = self.construct_object(key_node, deep=True)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {key!r}", key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep)


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, lambda loader, node: loader.construct_mapping(node)
)


def load_yaml(source: str | bytes) -> object:
    """Parse YAML strictly. Raises ValueError on syntax errors, bad encoding or duplicate, unhashable
    or non-scalar keys, and on impossible dates."""
    try:
        return yaml.load(source, Loader=_StrictLoader)  # noqa: S506 - subclass of SafeLoader
    except (yaml.YAMLError, ValueError, TypeError) as error:
        raise ValueError(" ".join(str(error).split())) from None


@dataclass(frozen=True)
class Param:
    name: str
    kind: str  # quantity | text | choice
    identity: bool = False
    unit: str = ""
    choices: tuple[str, ...] = ()
    aliases: dict[str, str] = field(default_factory=dict)

    def normalise(self, value: object) -> Fraction | str:
        """Return the canonical form used for comparison. Raises ValueError if invalid."""
        if self.kind == "quantity":
            return parse_quantity(value, self.unit)
        if not isinstance(value, str):
            # An unquoted 0603 reaches us as the octal integer 387.
            raise ValueError(f"{value!r} must be a quoted string")
        if self.kind == "text":
            text = value.strip().upper()
            if not text:
                raise ValueError("must not be empty")
            return text
        folded = {choice.casefold(): choice for choice in self.choices}
        aliases = {alias.casefold(): target for alias, target in self.aliases.items()}
        key = value.strip().casefold()
        canonical = folded.get(key) or aliases.get(key)
        if canonical is None:
            raise ValueError(f"{value!r} is not one of {', '.join(self.choices)}")
        return canonical


@dataclass(frozen=True)
class Category:
    name: str
    prefix: str
    identity: str  # params | mpn
    params: dict[str, Param]
    library: str  # generated symbol library name, permanent once used
    value: str  # template for the Value field, such as "{resistance_ohm}Ω" or "{mpn}"
    near_match: tuple[str, ...] = ()
    description: str | None = None  # template suggested for new parts
    default_symbol: str | None = None
    package_defaults: dict[str, dict[str, str]] = field(default_factory=dict)  # package -> footprint, model

    def identity_params(self) -> list[Param]:
        return [param for param in self.params.values() if param.identity]

    def render_description(self, normalised: dict[str, Fraction | str]) -> str | None:
        """Suggest a description from the template, or None if there is none."""
        if self.description is None:
            return None
        values = {}
        for name, value in normalised.items():
            param = self.params.get(name)
            if isinstance(value, Fraction):
                unit = param.unit if param else ""
                symbol = {"ohm": "Ω", "pct": "%"}.get(unit, unit)
                values[name] = format_engineering(value) + symbol
            else:
                values[name] = value.replace("_", " ")
        return self.description.format(**values)

    def render_value(self, normalised: dict[str, Fraction | str], mpn: str) -> str:
        """Fill in the Value template. Quantities use engineering notation, such as 4.7k or 100n."""
        values = {
            name: format_engineering(value) if isinstance(value, Fraction) else value
            for name, value in normalised.items()
        }
        return self.value.format(**values, mpn=mpn)


def load_categories(root: Path) -> dict[str, Category]:
    categories = {}
    prefixes: dict[str, str] = {}
    for path in sorted((root / "categories").glob("*.yaml")):
        try:
            raw = load_yaml(path.read_text(encoding="utf-8"))
            category = _parse_category(raw)
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise LibraryError(f"{path}: {error}") from None
        if category.name != path.stem:
            raise LibraryError(f"{path}: name {category.name!r} does not match the file name")
        if category.prefix in prefixes:
            raise LibraryError(f"{path}: prefix {category.prefix} is also used by {prefixes[category.prefix]}")
        prefixes[category.prefix] = category.name
        if any(other.library == category.library for other in categories.values()):
            raise LibraryError(f"{path}: library {category.library} is also used by another category")
        categories[category.name] = category
    if not categories:
        raise LibraryError(f"no category definitions in {root / 'categories'}")
    return categories


def _parse_category(raw: dict) -> Category:
    params = {}
    for name, definition in raw["params"].items():
        kind = definition["kind"]
        if kind not in ("quantity", "text", "choice"):
            raise ValueError(f"param {name}: unknown kind {kind!r}")
        unit = definition.get("unit", "")
        if kind == "quantity" and unit not in UNITS:
            raise ValueError(f"param {name}: unknown unit {unit!r}")
        params[name] = Param(
            name=name,
            kind=kind,
            identity=definition.get("identity", False),
            unit=unit,
            choices=tuple(definition.get("choices", ())),
            aliases=dict(definition.get("aliases", {})),
        )
    identity = raw["identity"]
    if identity not in ("params", "mpn"):
        raise ValueError(f"unknown identity {identity!r}")
    if not re.fullmatch(r"[A-Z]{2,4}", raw["prefix"]):
        raise ValueError(f"invalid prefix {raw['prefix']!r}")
    near_match = tuple(raw.get("near_match", ()))
    unknown = [name for name in near_match if name not in params]
    if unknown:
        raise ValueError(f"near_match names unknown params: {unknown}")
    library = raw["library"]
    if not re.fullmatch(r"PL_\w+", library):
        raise ValueError(f"library {library!r} must be PL_<name>")
    value = raw["value"]
    for _text, field_name, spec, conversion in string.Formatter().parse(value):
        if field_name is None:
            continue
        if spec or conversion:
            raise ValueError(f"value template {value!r} may only name fields")
        if field_name != "mpn" and not (field_name in params and params[field_name].identity):
            # Only identity params are always present, so only they can be in the value.
            raise ValueError(f"value template names {field_name!r}, which is not an identity param")
    description = raw.get("description")
    if description is not None:
        for _text, field_name, _spec, _conversion in string.Formatter().parse(description):
            if field_name is not None and not (field_name in params and params[field_name].identity):
                raise ValueError(f"description template names {field_name!r}, which is not an identity param")
    defaults = raw.get("defaults") or {}
    packages = {
        str(package): {k: str(v) for k, v in (values or {}).items() if k in ("footprint", "model")}
        for package, values in (defaults.get("packages") or {}).items()
    }
    return Category(
        raw["name"], raw["prefix"], identity, params, library, value, near_match,
        description, defaults.get("symbol"), packages,
    )


def part_paths(root: Path) -> list[Path]:
    """Return every candidate part file, relative to root, including misplaced ones."""
    parts = root / "parts"
    paths = [p for p in parts.rglob("*") if p.is_file() and p.suffix in (".yaml", ".yml")]
    return sorted(p.relative_to(root) for p in paths)


def normalise_mpn(mpn: str) -> str:
    return re.sub(r"\s+", "", mpn).upper()


def fingerprint(category: Category, normalised: dict[str, Fraction | str]) -> tuple | None:
    """Return a part's identity, or None if identity params are missing or the category is MPN-based."""
    if category.identity != "params":
        return None
    params = category.identity_params()
    if any(param.name not in normalised for param in params):
        return None
    return (category.name, *((param.name, normalised[param.name]) for param in params))


def near_key(category: Category, normalised: dict[str, Fraction | str]) -> tuple | None:
    if not category.near_match or any(name not in normalised for name in category.near_match):
        return None
    return (category.name, *((name, normalised[name]) for name in category.near_match))
