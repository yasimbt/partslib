"""Builds a small KiCad project for scanner tests: one sub-sheet used twice, so the same resistor
symbol is two parts, R1 and R2, plus a power symbol. Every path is portable, so it scans clean."""

from pathlib import Path

ROOT_UUID = "00000000-0000-4000-8000-000000000001"
SHEET_A = "00000000-0000-4000-8000-0000000000a0"
SHEET_B = "00000000-0000-4000-8000-0000000000b0"
RESISTOR = "00000000-0000-4000-8000-0000000000c1"
POWER = "00000000-0000-4000-8000-0000000000d1"
FONT = "(effects (font (size 1.27 1.27)))"

LIB_RESISTOR = f"""(symbol "PL_Resistor:RES-000123"
\t\t\t(pin_numbers (hide yes)) (exclude_from_sim no) (in_bom yes) (on_board yes)
\t\t\t(property "Reference" "R" (at 0 0 0) {FONT})
\t\t\t(property "Value" "1kΩ" (at 0 0 0) {FONT})
\t\t\t(property "Footprint" "PL_Resistor:R_0603" (at 0 0 0) {FONT})
\t\t\t(property "Datasheet" "" (at 0 0 0) {FONT})
\t\t\t(property "Description" "" (at 0 0 0) {FONT})
\t\t\t(symbol "RES-000123_1_1"
\t\t\t\t(pin passive line (at 0 3.81 270) (length 1.27) (name "" {FONT}) (number "1" {FONT}))
\t\t\t\t(pin passive line (at 0 -3.81 90) (length 1.27) (name "" {FONT}) (number "2" {FONT}))
\t\t\t)
\t\t)"""
LIB_POWER = f"""(symbol "power:GND"
\t\t\t(power) (pin_numbers (hide yes)) (exclude_from_sim no) (in_bom yes) (on_board yes)
\t\t\t(property "Reference" "#PWR" (at 0 0 0) {FONT})
\t\t\t(property "Value" "GND" (at 0 0 0) {FONT})
\t\t\t(property "Footprint" "" (at 0 0 0) {FONT})
\t\t\t(property "Datasheet" "" (at 0 0 0) {FONT})
\t\t\t(property "Description" "" (at 0 0 0) {FONT})
\t\t\t(symbol "GND_0_1" (pin power_in line (at 0 0 270) (length 0) (name "GND" {FONT}) (number "1" {FONT})))
\t\t)"""


def sub_sheet(ipn: str = "RES-000123", footprint: str = "PL_Resistor:R_0603") -> str:
    return f"""(kicad_sch
\t(version 20260306)
\t(generator "eeschema")
\t(generator_version "10.0")
\t(uuid "00000000-0000-4000-8000-0000000000e1")
\t(paper "A4")
\t(lib_symbols
\t\t{LIB_RESISTOR}
\t\t{LIB_POWER}
\t)
\t(symbol
\t\t(lib_id "PL_Resistor:RES-000123")
\t\t(at 100 100 0) (unit 1) (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)
\t\t(uuid "{RESISTOR}")
\t\t(property "Reference" "R2" (at 102 100 0) {FONT})
\t\t(property "Value" "1kΩ" (at 98 100 0) {FONT})
\t\t(property "Footprint" "{footprint}" (at 100 100 0) {FONT})
\t\t(property "Datasheet" "" (at 100 100 0) {FONT})
\t\t(property "Description" "" (at 100 100 0) {FONT})
\t\t(property "IPN" "{ipn}" (at 100 100 0) {FONT})
\t\t(pin "1" (uuid "00000000-0000-4000-8000-0000000000c2"))
\t\t(pin "2" (uuid "00000000-0000-4000-8000-0000000000c3"))
\t\t(instances
\t\t\t(project "demo"
\t\t\t\t(path "/{ROOT_UUID}/{SHEET_A}" (reference "R1") (unit 1))
\t\t\t\t(path "/{ROOT_UUID}/{SHEET_B}" (reference "R2") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "power:GND")
\t\t(at 100 110 0) (unit 1) (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)
\t\t(uuid "{POWER}")
\t\t(property "Reference" "#PWR01" (at 100 110 0) {FONT})
\t\t(property "Value" "GND" (at 100 110 0) {FONT})
\t\t(property "Footprint" "" (at 100 110 0) {FONT})
\t\t(property "Datasheet" "" (at 100 110 0) {FONT})
\t\t(property "Description" "" (at 100 110 0) {FONT})
\t\t(pin "1" (uuid "00000000-0000-4000-8000-0000000000d2"))
\t\t(instances
\t\t\t(project "demo"
\t\t\t\t(path "/{ROOT_UUID}/{SHEET_A}" (reference "#PWR01") (unit 1))
\t\t\t\t(path "/{ROOT_UUID}/{SHEET_B}" (reference "#PWR02") (unit 1))
\t\t\t)
\t\t)
\t)
)
"""


def _sheet(uuid: str, name: str, page: str, y: int) -> str:
    return f"""\t(sheet
\t\t(at 50 {y}) (size 20 10) (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)
\t\t(stroke (width 0.1524) (type solid)) (fill (color 0 0 0 0.0000))
\t\t(uuid "{uuid}")
\t\t(property "Sheetname" "{name}" (at 50 {y - 1} 0) {FONT})
\t\t(property "Sheetfile" "sub.kicad_sch" (at 50 {y + 11} 0) {FONT})
\t\t(instances (project "demo" (path "/{ROOT_UUID}" (page "{page}"))))
\t)
"""


def root_sheet(file_b: str = "sub.kicad_sch") -> str:
    return f"""(kicad_sch
\t(version 20260306)
\t(generator "eeschema")
\t(generator_version "10.0")
\t(uuid "{ROOT_UUID}")
\t(paper "A4")
\t(lib_symbols)
{_sheet(SHEET_A, "Half_A", "2", 50)}{_sheet(SHEET_B, "Half_B", "3", 80).replace("sub.kicad_sch", file_b)}\t(sheet_instances (path "/" (page "1")))
)
"""


FOOTPRINT_BODY = """\t\t(attr smd)
\t\t(pad "1" smd roundrect (at -0.8 0) (size 0.9 0.9) (layers "F.Cu"))
\t\t(pad "2" smd roundrect (at 0.8 0) (size 0.9 0.9) (layers "F.Cu"))
"""


def pcb_footprint(sheet: str, reference: str, lib_id: str = "PL_Resistor:R_0603",
                  model: str | None = "${KIPRJMOD}/lib/R_0603.step", x: int = 50) -> str:
    model_line = f'\t\t(model "{model}" (offset (xyz 0 0 0)) (scale (xyz 1 1 1)) (rotate (xyz 0 0 0)))\n' if model else ""
    return f"""\t(footprint "{lib_id}"
\t\t(layer "F.Cu")
\t\t(uuid "{sheet[:-2]}f{reference[-1]}")
\t\t(at {x} 50)
\t\t(property "Reference" "{reference}" (at 0 -1.4 0) (layer "F.SilkS"))
\t\t(property "Value" "1kΩ" (at 0 1.4 0) (layer "F.Fab"))
\t\t(path "/{sheet}/{RESISTOR}")
\t\t(sheetname "/x/")
\t\t(sheetfile "sub.kicad_sch")
{FOOTPRINT_BODY}{model_line}\t)
"""


def pcb(footprints: str | None = None) -> str:
    if footprints is None:
        footprints = pcb_footprint(SHEET_A, "R1") + pcb_footprint(SHEET_B, "R2", x=54)
    return f"""(kicad_pcb
\t(version 20260206)
\t(generator "pcbnew")
\t(generator_version "10.0")
\t(general (thickness 1.6))
\t(paper "A4")
\t(layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
\t(net 0 "")
{footprints})
"""


def write_board(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    files = {
        "demo.kicad_pro": '{"meta": {"filename": "demo.kicad_pro", "version": 3}}\n',
        "demo.kicad_sch": root_sheet(),
        "sub.kicad_sch": sub_sheet(),
        "demo.kicad_pcb": pcb(),
        "sym-lib-table": '(sym_lib_table\n\t(version 7)\n\t(lib (name "PL_Resistor") (type "KiCad") (uri "${KIPRJMOD}/lib/PL_Resistor.kicad_sym") (options "") (descr ""))\n)\n',
        "fp-lib-table": '(fp_lib_table\n\t(version 7)\n\t(lib (name "PL_Resistor") (type "KiCad") (uri "${KIPRJMOD}/lib/PL_Resistor.pretty") (options "") (descr ""))\n)\n',
        "lib/PL_Resistor.kicad_sym": "(kicad_symbol_lib\n\t(version 20251024)\n\t(generator \"partslib\")\n\t"
        + LIB_RESISTOR.replace('"PL_Resistor:RES-000123"', '"RES-000123"', 1)
        + "\n)\n",
        "lib/PL_Resistor.pretty/R_0603.kicad_mod": '(footprint "R_0603"\n\t(layer "F.Cu")\n' + FOOTPRINT_BODY + ")\n",
        "lib/R_0603.step": "ISO-10303-21;\n",
    }
    for relative, text in files.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return directory
