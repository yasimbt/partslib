"""`partslib add`: create a draft part, refusing anything `check` would reject."""

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
import re

import yaml

from partslib.check import Finding, check_records
from partslib.library import Category, LibraryError, load_categories, part_paths
from partslib.quantity import plain_number


@dataclass(frozen=True)
class AddResult:
    ipn: str
    path: str | None  # None if the part was refused
    findings: tuple[Finding, ...]


def next_ipn(root: Path, category: Category) -> str:
    """Allocate one past the highest number in use. Parts are never deleted, so none is reused."""
    pattern = re.compile(rf"{re.escape(category.prefix)}-(\d{{6}})")
    numbers = [0]
    for path in part_paths(root):
        match = pattern.fullmatch(path.stem)
        if match:
            numbers.append(int(match.group(1)))
    return f"{category.prefix}-{max(numbers) + 1:06d}"


def _canonical(value: Fraction | str) -> int | float | str:
    return plain_number(value) if isinstance(value, Fraction) else value


def add_part(
    root: Path,
    category_name: str,
    description: str,
    params: dict[str, str],
    mpns: list[tuple[str, str]],
    acknowledge: list[str],
) -> AddResult:
    categories = load_categories(root)
    category = categories.get(category_name)
    if category is None:
        raise LibraryError(f"unknown category {category_name!r}; choose from {', '.join(sorted(categories))}")

    ipn = next_ipn(root, category)
    path = f"parts/{category.name}/{ipn}.yaml"

    def render(rendered_params: dict[str, object]) -> str:
        part = {
            "ipn": ipn,
            "status": "draft",
            "category": category.name,
            "description": description,
            "params": rendered_params,
            "mpns": [{"manufacturer": m, "mpn": n, "lifecycle": "unknown"} for m, n in mpns],
        }
        if acknowledge:
            part["near_match_ack"] = acknowledge
        return yaml.safe_dump(part, sort_keys=False, allow_unicode=True)

    sources: dict[str, str | bytes] = {p.as_posix(): (root / p).read_bytes() for p in part_paths(root)}
    sources[path] = render(params)
    records, findings = check_records(root, sources, categories)

    # Problems elsewhere in the library are for `check`; only this part's can block adding it.
    own = tuple(f for f in sorted(findings) if f.path == path or ipn in f.related)
    if own:
        return AddResult(ipn, None, own)

    # Store values as plain numbers, so the file records 1000 however it was typed.
    record = next(r for r in records if r.path == path)
    text = render({name: _canonical(record.normalised[name]) for name in params})
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return AddResult(ipn, path, ())
