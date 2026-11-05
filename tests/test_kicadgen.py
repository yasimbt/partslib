import pytest

from partslib.kicadgen import (
    Part,
    part_symbol,
    extract_symbol,
    footprint_file,
    quote,
    symbol_library,
)

FIELD_LAYOUT = '(at 0 0 0)(effects (font (size 1.27 1.27)))'
BASE = (
    '(symbol "R"\n'
    f'\t\t(property "Reference" "R" {FIELD_LAYOUT})\n'
    f'\t\t(property "Value" "R" {FIELD_LAYOUT})\n'
    f'\t\t(property "Footprint" "" {FIELD_LAYOUT})\n'
    f'\t\t(property "Datasheet" "" {FIELD_LAYOUT})\n'
    f'\t\t(property "Description" "Resistor" {FIELD_LAYOUT})\n'
    '\t\t(symbol "R_1_1" (pin passive line (at 0 3.81 270)))\n'
    "\t)"
)
LIBRARY = f'(kicad_symbol_lib\n\t{BASE}\n\t(symbol "R_Small" (extends "R"))\n)\n'
PART = Part(
    ipn="RES-000123",
    value="1k",
    description='1 kΩ "precision"',
    datasheet="https://example.com/d.pdf",
    footprint="PL_Resistor:R_0603",
    manufacturer="ACME",
    mpn="A1",
    lcsc="C1",
)
FOOTPRINT = (
    '(footprint "R_0603_1608Metric"\n'
    '\t(property "Value" "R_0603_1608Metric")\n'
    '\t(model "${KICAD10_3DMODEL_DIR}/Resistor_SMD.3dshapes/R_0603_1608Metric.step")\n'
    ")\n"
)


def test_quote_escapes_quotes_and_backslashes():
    assert quote('a"b\\c') == '"a\\"b\\\\c"'


def test_extract_symbol_returns_the_whole_balanced_block():
    assert extract_symbol(LIBRARY, "R") == BASE


def test_extract_symbol_does_not_match_a_name_prefix():
    with pytest.raises(KeyError):
        extract_symbol(LIBRARY, "R_")


def test_extract_symbol_rejects_missing_and_derived_symbols():
    with pytest.raises(KeyError):
        extract_symbol(LIBRARY, "C")
    with pytest.raises(ValueError, match="derived"):
        extract_symbol(LIBRARY, "R_Small")


def test_part_symbol_is_complete_and_named_by_ipn():
    block = part_symbol("R", BASE, PART)
    assert block.startswith('(symbol "RES-000123"\n')
    assert "(extends" not in block
    assert '(symbol "RES-000123_1_1"' in block, "KiCad requires unit names to start with the symbol name"
    assert '(symbol "R_1_1"' not in block
    assert block.count("(pin passive") == 1


def test_embedded_copy_keeps_the_ipn_in_unit_names():
    block = part_symbol("R", BASE, PART, name="PL_Resistor:RES-000123")
    assert block.startswith('(symbol "PL_Resistor:RES-000123"\n')
    assert '(symbol "RES-000123_1_1"' in block


def test_part_symbol_keeps_other_base_fields_and_lets_part_fields_win():
    base = BASE.replace(
        '\t\t(symbol "R_1_1"',
        f'\t\t(property "Sim.Device" "R" {FIELD_LAYOUT})\n'
        f'\t\t(property "MPN" "base value" {FIELD_LAYOUT})\n\t\t(symbol "R_1_1"',
    )
    block = part_symbol("R", base, PART)
    assert '(property "Sim.Device" "R"' in block
    assert block.count('(property "MPN"') == 1
    assert '(property "MPN" "A1"' in block


def test_parens_inside_strings_are_not_children():
    base = BASE.replace('"Resistor"', '"Resistor (thick film)"').replace('(symbol "R"', '(symbol "R(x)"')
    base = base.replace('"R_1_1"', '"R(x)_1_1"')
    block = part_symbol("R(x)", base, PART)
    assert block.startswith('(symbol "RES-000123"')
    assert '(symbol "RES-000123_1_1"' in block


def test_properties_split_by_other_entries_are_rejected():
    base = BASE.replace('\t\t(property "Datasheet"', '\t\t(in_bom yes)\n\t\t(property "Datasheet"')
    with pytest.raises(ValueError, match="between its properties"):
        part_symbol("R", base, PART)


def test_symbol_names_are_matched_whole():
    with pytest.raises(ValueError):
        part_symbol("R_", BASE, PART)


def test_part_symbol_fills_every_field():
    block = part_symbol("R", BASE, PART)
    for expected in (
        '(property "Value" "1k"',
        '(property "Footprint" "PL_Resistor:R_0603"',
        '(property "Datasheet" "https://example.com/d.pdf"',
        '(property "Description" "1 kΩ \\"precision\\""',
        '(property "IPN" "RES-000123"',
        '(property "Manufacturer" "ACME"',
        '(property "MPN" "A1"',
        '(property "LCSC" "C1"',
    ):
        assert expected in block


def test_custom_fields_come_before_search_metadata_as_kicad_writes_them():
    base = BASE.replace(
        '\t\t(symbol "R_1_1"', f'\t\t(property "ki_keywords" "res" {FIELD_LAYOUT})\n\t\t(symbol "R_1_1"'
    )
    block = part_symbol("R", base, PART)
    assert block.index('"Description"') < block.index('"IPN"') < block.index('"ki_keywords"')


def test_part_symbol_requires_the_standard_fields():
    with pytest.raises(ValueError, match="Datasheet"):
        part_symbol("R", BASE.replace('"Datasheet"', '"Other"'), PART)


def test_library_output_is_stable_whatever_the_part_order():
    other = Part("RES-000124", "2k", "2 kΩ", "", "PL_Resistor:R_0603", "ACME", "A2")
    first = symbol_library({"R": BASE}, {"R": [PART, other]})
    second = symbol_library({"R": BASE}, {"R": [other, PART]})
    assert first == second
    assert first.index('"RES-000123"') < first.index('"RES-000124"')
    assert '(symbol "R"' not in first, "the base must not be pickable without an IPN"


def test_footprint_is_renamed_and_uses_only_the_portable_model_path():
    uri = "${PARTSLIB}/3dmodels/PL_Resistor.3dshapes/R_0603.step"
    text = footprint_file(FOOTPRINT, "R_0603", uri)
    assert text.startswith('(footprint "R_0603"')
    assert '(property "Value" "R_0603"' in text
    assert f'(model "{uri}"' in text
    assert "KICAD10_3DMODEL_DIR" not in text


def test_footprint_without_a_model_is_rejected():
    with pytest.raises(ValueError, match="3D model"):
        footprint_file('(footprint "X"\n)\n', "X", "${PARTSLIB}/x.step")
