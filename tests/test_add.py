"""The M1 acceptance test: 1k, 1000 and 1K0 versions of the same resistor are rejected."""

from pathlib import Path
import shutil

import pytest
import yaml

from partslib.check import check_library
from partslib.cli import main

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    shutil.copytree(REPO / "categories", tmp_path / "categories")
    return tmp_path


def add_resistor(root: Path, resistance: str, tolerance: str = "1%", *extra: str) -> int:
    return main(
        [
            "--root", str(root), "add", "resistor",
            "-d", "test resistor",
            "-p", f"resistance_ohm={resistance}",
            "-p", f"tolerance_pct={tolerance}",
            "-p", "power_w=0.1W",
            "-p", "package=0603",
            "-p", "technology=thick_film",
            *extra,
        ]
    )  # fmt: skip


def parts(root: Path) -> list[str]:
    return sorted(path.stem for path in (root / "parts").rglob("*.yaml"))


def test_acceptance_every_spelling_of_the_same_resistor_is_rejected(root, capsys):
    assert add_resistor(root, "1k") == 0
    capsys.readouterr()
    for spelling in ("1000", "1K0", "1.0 kΩ", "1k"):
        assert add_resistor(root, spelling) == 1, spelling
        output = capsys.readouterr().out
        assert "duplicate-part" in output
        assert "RES-000001" in output, "the error must name the existing IPN"
    assert parts(root) == ["RES-000001"]


def test_added_part_passes_check_and_stores_a_plain_value(root):
    assert add_resistor(root, "4k7") == 0
    assert check_library(root) == (1, [])
    part = yaml.safe_load((root / "parts/resistor/RES-000001.yaml").read_text(encoding="utf-8"))
    assert part["status"] == "draft"
    assert part["params"]["resistance_ohm"] == 4700
    assert part["params"]["power_w"] == 0.1
    assert part["params"]["package"] == "0603", "must survive YAML as a string, not octal 387"


@pytest.mark.parametrize(
    "first, second",
    [("100n", "0.1uF"), ("4p7", "4.7pF"), ("1u", "1000n"), ("1/3", "1/3"), ("1/7", "2/14")],
)
def test_stored_values_still_match_when_typed_again(root, capsys, first, second):
    """A stored value must parse back exactly, or duplicate detection silently stops working."""

    def add_capacitor(value):
        return main(
            [
                "--root", str(root), "add", "capacitor", "-d", "x",
                "-p", f"capacitance_f={value}",
                "-p", "tolerance_pct=10",
                "-p", "voltage_v=50",
                "-p", "dielectric=X7R",
                "-p", "package=0603",
            ]
        )  # fmt: skip

    assert add_capacitor(first) == 0
    capsys.readouterr()
    assert add_capacitor(second) == 1
    assert "duplicate-part" in capsys.readouterr().out
    assert check_library(root)[1] == []


def test_numbers_are_allocated_in_sequence(root):
    assert add_resistor(root, "1k") == 0
    assert add_resistor(root, "2k") == 0
    assert parts(root) == ["RES-000001", "RES-000002"]


def test_near_match_is_refused_until_acknowledged(root, capsys):
    assert add_resistor(root, "1k") == 0
    capsys.readouterr()
    assert add_resistor(root, "1k", "5%") == 1
    assert "near-match" in capsys.readouterr().out
    assert parts(root) == ["RES-000001"]
    assert add_resistor(root, "1k", "5%", "--ack", "RES-000001") == 0
    assert check_library(root)[1] == []


def test_same_mpn_is_refused_across_categories(root, capsys):
    assert add_resistor(root, "1k", "1%", "--mpn", "ACME:A1") == 0
    capsys.readouterr()
    assert main(["--root", str(root), "add", "ic", "-d", "x", "--mpn", "OTHER:a1"]) == 1
    assert "duplicate-mpn" in capsys.readouterr().out
    assert parts(root) == ["RES-000001"]


def test_invalid_value_is_refused(root, capsys):
    assert add_resistor(root, "10pF") == 1
    assert "param-invalid" in capsys.readouterr().out
    assert not (root / "parts").exists()


def test_problem_elsewhere_in_the_library_does_not_block_adding(root):
    broken = root / "parts/resistor/RES-000050.yaml"
    broken.parent.mkdir(parents=True)
    broken.write_text("ipn: [broken\n", encoding="utf-8")
    assert add_resistor(root, "1k") == 0
    assert "RES-000051" in parts(root), "numbers already in use are never reallocated"


def test_unknown_category_is_a_usage_error(root, capsys):
    assert main(["--root", str(root), "add", "resistors", "-d", "x"]) == 2
    assert "unknown category" in capsys.readouterr().err
