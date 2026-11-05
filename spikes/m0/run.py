#!/usr/bin/env python3
"""M0 spike: can generated standard libraries work in KiCad 10 (spec D3, section 12)?

Generates one derived symbol and footprint into build/, makes a project in build/m0-project/ that
uses them, and checks as much of the M0 acceptance test as can run without the GUI. Everything runs
against an empty KiCad config directory; the real one is never read or written.

The part is the unverified example from spec 5.1, and assets are copied from the installed KiCad at
run time, so nothing third-party is committed. The final step, placing the part by hand, is printed.
"""

import csv
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

from partslib import kicadgen
from partslib.kicadgen import Part, quote

REPO = Path(__file__).resolve().parents[2]
BUILD = REPO / "build"
PROJECT = BUILD / "m0-project"
APP = Path(os.environ.get("KICAD_APP", "/Applications/KiCad/KiCad.app"))
KICAD_CLI = APP / "Contents/MacOS/kicad-cli"
KICAD_GUI = APP / "Contents/MacOS/kicad"
SHARED = APP / "Contents/SharedSupport"

LIBRARY = "PL_Resistor"
FOOTPRINT = "R_0603"
BASE_SYMBOL = "R"
PART = Part(
    ipn="RES-000123",
    value="1k",
    description="1 kΩ 1% 0.1 W thick film, 0603",
    datasheet="https://example.com/datasheet.pdf",
    footprint=f"{LIBRARY}:{FOOTPRINT}",
    manufacturer="UNI-ROYAL",
    mpn="0603WAF1001T5E",
    lcsc="C21190",
)
LIBRARY_ISSUE_TYPES = {"lib_symbol_issues", "lib_symbol_mismatch", "footprint_link_issues"}

# KiCad's standard layer list.
LAYERS = """(layers
\t\t(0 "F.Cu" signal)
\t\t(2 "B.Cu" signal)
\t\t(9 "F.Adhes" user "F.Adhesive")
\t\t(11 "B.Adhes" user "B.Adhesive")
\t\t(13 "F.Paste" user)
\t\t(15 "B.Paste" user)
\t\t(5 "F.SilkS" user "F.Silkscreen")
\t\t(7 "B.SilkS" user "B.Silkscreen")
\t\t(1 "F.Mask" user)
\t\t(3 "B.Mask" user)
\t\t(17 "Dwgs.User" user "User.Drawings")
\t\t(19 "Cmts.User" user "User.Comments")
\t\t(21 "Eco1.User" user "User.Eco1")
\t\t(23 "Eco2.User" user "User.Eco2")
\t\t(25 "Edge.Cuts" user)
\t\t(27 "Margin" user)
\t\t(31 "F.CrtYd" user "F.Courtyard")
\t\t(29 "B.CrtYd" user "B.Courtyard")
\t\t(35 "F.Fab" user)
\t\t(33 "B.Fab" user)
\t)"""

failures: list[str] = []


def report(name: str, passed: bool, detail: str = "") -> None:
    print(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))
    if not passed:
        failures.append(name)


def stable_uuid(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"partslib/m0/{name}"))


def base_block() -> str:
    text = (SHARED / "symbols/Device.kicad_sym").read_text(encoding="utf-8")
    return kicadgen.extract_symbol(text, BASE_SYMBOL)


def build_libraries() -> None:
    source = SHARED / "footprints/Resistor_SMD.pretty/R_0603_1608Metric.kicad_mod"
    kicadgen.write_category(
        BUILD,
        LIBRARY,
        {BASE_SYMBOL: base_block()},
        {BASE_SYMBOL: [PART]},
        {FOOTPRINT: source.read_text(encoding="utf-8")},
        {FOOTPRINT: SHARED / "3dmodels/Resistor_SMD.3dshapes/R_0603_1608Metric.step"},
    )


def library_tables() -> dict[str, str]:
    def table(kind: str, uri: str) -> str:
        entry = f'(lib (name "{LIBRARY}") (type "KiCad") (uri "{uri}") (options "") (descr ""))'
        return f"({kind}\n\t(version 7)\n\t{entry}\n)\n"

    return {
        "sym-lib-table": table("sym_lib_table", "${PARTSLIB}/symbols/" + f"{LIBRARY}.kicad_sym"),
        "fp-lib-table": table("fp_lib_table", "${PARTSLIB}/footprints/" + f"{LIBRARY}.pretty"),
    }


def schematic() -> str:
    root = stable_uuid("schematic")
    block = base_block()
    properties = "\n\t\t".join(kicadgen.part_properties(block, PART, reference="R1"))
    return f"""(kicad_sch
\t(version 20260306)
\t(generator "partslib")
\t(generator_version "0.0")
\t(uuid "{root}")
\t(paper "A4")
\t(lib_symbols
\t{kicadgen.part_symbol(BASE_SYMBOL, block, PART, name=f"{LIBRARY}:{PART.ipn}")}
\t)
\t(symbol
\t\t(lib_id "{LIBRARY}:{PART.ipn}")
\t\t(at 100 100 0)
\t\t(unit 1)
\t\t(exclude_from_sim no)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(uuid "{stable_uuid('R1')}")
\t\t{properties}
\t\t(pin "1" (uuid "{stable_uuid('R1/1')}"))
\t\t(pin "2" (uuid "{stable_uuid('R1/2')}"))
\t\t(instances
\t\t\t(project "m0"
\t\t\t\t(path "/{root}"
\t\t\t\t\t(reference "R1")
\t\t\t\t\t(unit 1)
\t\t\t\t)
\t\t\t)
\t\t)
\t)
\t(sheet_instances
\t\t(path "/"
\t\t\t(page "1")
\t\t)
\t)
)
"""


def board() -> str:
    footprint = (BUILD / f"footprints/{LIBRARY}.pretty/{FOOTPRINT}.kicad_mod").read_text("utf-8")
    footprint = footprint.replace('"REF**"', '"R1"').replace(
        f'(footprint "{FOOTPRINT}"', f'(footprint "{LIBRARY}:{FOOTPRINT}"', 1
    )
    footprint = footprint.replace(
        '(layer "F.Cu")\n',
        f'(layer "F.Cu")\n\t(uuid "{stable_uuid("R1/footprint")}")\n\t(at 50 50)\n'
        f'\t(path "/{stable_uuid("R1")}")\n',
        1,
    )
    return f"""(kicad_pcb
\t(version 20260206)
\t(generator "partslib")
\t(generator_version "0.0")
\t(general
\t\t(thickness 1.6)
\t)
\t(paper "A4")
\t{LAYERS}
\t(setup
\t\t(pad_to_mask_clearance 0)
\t)
\t(net 0 "")
{footprint}
\t(gr_rect
\t\t(start 47 48)
\t\t(end 53 52)
\t\t(stroke (width 0.05) (type default))
\t\t(fill no)
\t\t(layer "Edge.Cuts")
\t\t(uuid "{stable_uuid('outline')}")
\t)
)
"""


def write_project() -> None:
    shutil.rmtree(PROJECT, ignore_errors=True)
    PROJECT.mkdir(parents=True)
    pro = {"meta": {"filename": "m0.kicad_pro", "version": 3}}
    (PROJECT / "m0.kicad_pro").write_text(json.dumps(pro, indent=2) + "\n", encoding="utf-8")
    (PROJECT / "m0.kicad_sch").write_text(schematic(), encoding="utf-8")
    (PROJECT / "m0.kicad_pcb").write_text(board(), encoding="utf-8")
    for name, text in library_tables().items():
        (PROJECT / name).write_text(text, encoding="utf-8")


def cli(env: dict[str, str], *args: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(KICAD_CLI), *map(str, args)], env=env, capture_output=True, text=True, check=False
    )


def erc_types(env: dict[str, str], schematic_path: Path, out: Path) -> set[str]:
    result = cli(
        env, "sch", "erc", "--format", "json", "--severity-all", "-o", out, schematic_path
    )
    if not out.exists():
        raise RuntimeError(f"ERC produced no report: {result.stdout}{result.stderr}")
    report_data = json.loads(out.read_text(encoding="utf-8"))
    return {v["type"] for sheet in report_data["sheets"] for v in sheet["violations"]}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_library_files(env: dict[str, str], work: Path) -> None:
    sym_lib = BUILD / f"symbols/{LIBRARY}.kicad_sym"
    result = cli(env, "sym", "export", "svg", "-s", PART.ipn, "-o", work / "sym", sym_lib)
    svgs = list((work / "sym").glob("*.svg")) if (work / "sym").exists() else []
    report(
        "KiCad loads the part's symbol from the generated library",
        result.returncode == 0 and bool(svgs),
        ", ".join(p.name for p in svgs) or result.stderr.strip(),
    )
    names = re.findall(r'^\t\(symbol "([^"]*)"', sym_lib.read_text(encoding="utf-8"), re.MULTILINE)
    report(
        "The library holds only the part, so nothing without an IPN can be picked",
        names == [PART.ipn],
        ", ".join(names),
    )
    result = cli(env, "sym", "export", "svg", "-s", BASE_SYMBOL, "-o", work / "nobase", sym_lib)
    report(
        "  control: the base symbol cannot be exported from the library",
        result.returncode != 0 or not list((work / "nobase").glob("*.svg")),
    )
    result = cli(env, "sym", "export", "svg", "-s", "NO-SUCH-PART", "-o", work / "nosym", sym_lib)
    missing = result.returncode != 0 or not list((work / "nosym").glob("*.svg"))
    report("  control: an unknown symbol name is not exported", missing)

    pretty = BUILD / f"footprints/{LIBRARY}.pretty"
    (work / "fp").mkdir(exist_ok=True)  # unlike sym export, fp export does not create it
    result = cli(env, "fp", "export", "svg", "--footprint", FOOTPRINT, "-o", work / "fp", pretty)
    svgs = list((work / "fp").glob("*.svg")) if (work / "fp").exists() else []
    report(
        "KiCad loads the generated footprint",
        result.returncode == 0 and bool(svgs),
        result.stderr.strip() if not svgs else "",
    )

    before = sym_lib.read_text(encoding="utf-8")
    upgraded = work / "upgraded.kicad_sym"
    shutil.copyfile(sym_lib, upgraded)
    result = cli(env, "sym", "upgrade", "--force", upgraded)
    after = upgraded.read_text(encoding="utf-8")
    report(
        "KiCad reads and rewrites the symbol library, keeping the part and fields",
        result.returncode == 0
        and f'(symbol "{PART.ipn}"' in after
        and "(extends" not in after
        and all(f'(property "{name}"' in after for name in PART.custom_fields()),
    )
    changed = [
        line
        for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
        if line[:1] in "+-" and line[:3] not in ("+++", "---")
    ]
    unexpected = [line for line in changed if "generator" not in line]
    report(
        "KiCad's rewrite changes only the generator header",
        not unexpected,
        f"{len(unexpected)} other lines changed" if unexpected else "",
    )


def check_stability() -> None:
    files = sorted(p for p in BUILD.rglob("*") if p.is_file() and PROJECT not in p.parents)
    first = {p: digest(p) for p in files}
    build_libraries()
    report("Rebuilding gives byte-identical output", first == {p: digest(p) for p in files})


def check_portability() -> None:
    text = (BUILD / f"footprints/{LIBRARY}.pretty/{FOOTPRINT}.kicad_mod").read_text("utf-8")
    uris = re.findall(r'\(model "([^"]*)"', text)
    report(
        "The footprint's only 3D model path uses ${PARTSLIB}",
        len(uris) == 1 and uris[0].startswith("${PARTSLIB}/"),
        ", ".join(uris),
    )


def check_library_resolution(env: dict[str, str], work: Path) -> None:
    types = erc_types(env, PROJECT / "m0.kicad_sch", work / "erc.json")
    print(f"       note: ERC reported {sorted(types) or 'nothing'}")
    report(
        "ERC finds the library symbol and footprint via the project's library tables",
        not types & LIBRARY_ISSUE_TYPES,
        ", ".join(sorted(types & LIBRARY_ISSUE_TYPES)),
    )

    control = work / "no-tables"
    shutil.copytree(PROJECT, control)
    (control / "sym-lib-table").unlink()
    (control / "fp-lib-table").unlink()
    types = erc_types(env, control / "m0.kicad_sch", work / "erc-no-tables.json")
    report(
        "  control: ERC reports library issues once the tables are removed",
        bool(types & LIBRARY_ISSUE_TYPES),
        ", ".join(sorted(types)),
    )


def check_fields(env: dict[str, str], work: Path) -> None:
    out = work / "bom.csv"
    names = ["Reference", "Value", "Footprint", "Datasheet", "Description", "IPN", "Manufacturer",
             "MPN", "LCSC"]
    cli(env, "sch", "export", "bom", "--fields", ",".join(names), "--labels", ",".join(names),
        "--group-by", "", "-o", out, PROJECT / "m0.kicad_sch")
    rows = list(csv.DictReader(out.open(encoding="utf-8"))) if out.exists() else []
    expected = {"Reference": "R1", **PART.base_field_values(), **PART.custom_fields()}
    wrong = {k: rows[0].get(k) for k, v in expected.items() if rows[0].get(k) != v} if rows else {}
    report("BOM export has every field filled in", len(rows) == 1 and not wrong, str(wrong or ""))


def check_3d(env: dict[str, str], work: Path) -> None:
    board_path = PROJECT / "m0.kicad_pcb"
    no_var = {k: v for k, v in env.items() if k != "PARTSLIB"}

    def step(name: str, environment: dict[str, str]) -> tuple[int, str]:
        out = work / name
        result = cli(environment, "pcb", "export", "step", "--force", "-o", out, board_path)
        log = result.stdout + result.stderr
        return (out.stat().st_size if out.exists() else 0), log

    with_model, log = step("with.step", env)
    without_model, control_log = step("without.step", no_var)
    report(
        "The 3D model is loaded through ${PARTSLIB}",
        with_model > without_model > 0,
        f"STEP export {with_model} bytes with the variable, {without_model} without",
    )
    print("       with variable:    " + (log.strip().splitlines() or [""])[-1])
    print("       without variable: " + (control_log.strip().splitlines() or [""])[-1])

    image = work / "render.png"
    result = cli(env, "pcb", "render", "--rotate", "-45,0,45", "--zoom", "3",
                 "-w", "800", "-h", "600", "-o", image, board_path)
    report("3D render completes", result.returncode == 0 and image.exists(), str(image))


def main() -> int:
    if not KICAD_CLI.exists():
        print(f"kicad-cli not found at {KICAD_CLI}; set KICAD_APP", file=sys.stderr)
        return 2

    shutil.rmtree(BUILD, ignore_errors=True)
    build_libraries()
    write_project()

    keep = Path(os.environ.get("M0_WORK") or tempfile.mkdtemp(prefix="partslib-m0-"))
    keep.mkdir(parents=True, exist_ok=True)
    config = keep / "clean-config"
    shutil.rmtree(config, ignore_errors=True)
    config.mkdir()
    env = {**os.environ, "KICAD_CONFIG_HOME": str(config), "PARTSLIB": str(BUILD)}
    print(f"KiCad {cli(env, 'version').stdout.strip()}; working directory {keep}\n")

    check_library_files(env, keep)
    check_stability()
    check_portability()
    check_library_resolution(env, keep)
    check_fields(env, keep)
    check_3d(env, keep)

    print(f"\n{len(failures)} failed" if failures else "\nAll headless checks passed.")
    print("\nManual step, placing the part in KiCad on the clean config:")
    print(f"  KICAD_CONFIG_HOME={config} PARTSLIB={BUILD} \\\n    {KICAD_GUI} {PROJECT}/m0.kicad_pro")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
