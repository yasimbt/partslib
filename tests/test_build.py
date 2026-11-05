from pathlib import Path

import pytest

from partslib.build import BuildError, build_library, generate, read_manifest
from partslib.check import check_library
from partslib.cli import main
from test_check import RESISTOR, edit, library, write  # noqa: F401 - library is a fixture

SYMBOL_LIB = "build/symbols/PL_Resistor.kicad_sym"
FOOTPRINT = "build/footprints/PL_Resistor.pretty/R_0603.kicad_mod"
MODEL = "build/3dmodels/PL_Resistor.3dshapes/R_0603.step"


def text(root: Path, relative: str) -> str:
    return (root / relative).read_text(encoding="utf-8")


def test_build_writes_symbol_footprint_model_and_manifest(library):
    findings, files = build_library(library)
    assert findings == []
    assert sorted(files) == [
        "3dmodels/PL_Resistor.3dshapes/R_0603.step",
        "footprints/PL_Resistor.pretty/R_0603.kicad_mod",
        "symbols/PL_Resistor.kicad_sym",
    ]
    assert set(read_manifest(library)) == set(files)
    assert check_library(library)[1] == [], "a fresh build is neither modified nor stale"


def test_symbol_has_every_field_and_points_at_the_generated_footprint(library):
    build_library(library)
    symbol = text(library, SYMBOL_LIB)
    for expected in (
        '(symbol "RES-000123"',
        '(property "Value" "1kΩ"',
        '(property "Footprint" "PL_Resistor:R_0603"',
        '(property "Datasheet" "https://example.com/datasheet.pdf"',
        '(property "IPN" "RES-000123"',
        '(property "Manufacturer" "UNI-ROYAL"',
        '(property "MPN" "0603WAF1001T5E"',
        '(property "LCSC" "C21190"',
    ):
        assert expected in symbol


def test_footprint_model_uses_only_partslib(library):
    build_library(library)
    assert '(model "${PARTSLIB}/3dmodels/PL_Resistor.3dshapes/R_0603.step"' in text(library, FOOTPRINT)
    assert (library / MODEL).read_bytes() == (library / "assets/3dmodels/Resistor/R_0603.step").read_bytes()


def test_drafts_are_not_generated(library):
    edit(library, RESISTOR, "status: released", "status: draft")
    findings, files = build_library(library)
    assert (findings, files) == ([], {})
    assert not (library / SYMBOL_LIB).exists()


def test_retired_parts_stay_available_and_say_so(library):
    edit(library, RESISTOR, "status: released", "status: deprecated")
    build_library(library)
    assert '(property "Description" "DEPRECATED: 1 kΩ' in text(library, SYMBOL_LIB)


def test_rebuilding_is_byte_identical(library):
    _findings, first = build_library(library)
    _findings, second = build_library(library)
    assert first == second


def test_build_refuses_when_check_fails_and_leaves_the_old_build(library, capsys):
    build_library(library)
    before = text(library, SYMBOL_LIB)
    edit(library, RESISTOR, "resistance_ohm: 1000", "resistance_ohm: 10pF")
    assert main(["--root", str(library), "build"]) == 1
    assert "param-invalid" in capsys.readouterr().out
    assert text(library, SYMBOL_LIB) == before


def test_rebuild_overwrites_hand_edits_and_keeps_other_build_contents(library):
    build_library(library)
    write(library, "build/m0-project/keep.txt", "mine")
    edit(library, FOOTPRINT, '(pad "1"', '(pad "9"')
    assert "build-modified" in {f.code for f in check_library(library)[1]}
    assert build_library(library)[0] == []
    assert '(pad "9"' not in text(library, FOOTPRINT)
    assert text(library, "build/m0-project/keep.txt") == "mine"


def test_libraries_no_longer_generated_are_removed(library):
    build_library(library)
    write(library, "build/symbols/PL_Old.kicad_sym", "(kicad_symbol_lib)\n")
    build_library(library)
    assert not (library / "build/symbols/PL_Old.kicad_sym").exists()


def test_an_lfs_pointer_instead_of_a_model_is_an_error(library):
    write(library, "assets/3dmodels/Resistor/R_0603.step", "version https://git-lfs.github.com/spec/v1\noid sha256:0\n")
    from partslib.check import check_records
    from partslib.library import load_categories

    records, _ = check_records(library, {RESISTOR: text(library, RESISTOR)}, load_categories(library))
    with pytest.raises(BuildError, match="git lfs pull"):
        generate(library, records)
