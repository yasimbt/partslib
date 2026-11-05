#!/usr/bin/env python3
"""M2 acceptance, headless: a new KiCad project can use the parts with no manual library setup.

Works on a temporary copy of the library in which every part is marked released. That marking is
for this test only: releasing a real part needs its datasheet checked (spec 5.4), and the parts in
the repository stay as they are. KiCad runs against an empty config directory that only
`partslib install` writes to, and the new project has no library tables of its own. ${PARTSLIB} is
removed from the environment, so it can only come from the installed config.
"""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

import yaml

from partslib import kicadgen

REPO = Path(__file__).resolve().parents[2]
APP = Path(os.environ.get("KICAD_APP", "/Applications/KiCad/KiCad.app"))
KICAD_CLI = APP / "Contents/MacOS/kicad-cli"
PARTSLIB = [sys.executable, "-m", "partslib"]
LIBRARY_ISSUES = {"lib_symbol_issues", "lib_symbol_mismatch", "footprint_link_issues"}

failures: list[str] = []


def report(name: str, passed: bool, detail: str = "") -> None:
    print(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))
    if not passed:
        failures.append(name)


def stable_uuid(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"partslib/m2/{name}"))


def released_copy(work: Path) -> Path:
    root = work / "library"
    for name in ("categories", "parts", "assets"):
        shutil.copytree(REPO / name, root / name)
    for path in (root / "parts").rglob("*.yaml"):
        part = yaml.safe_load(path.read_text(encoding="utf-8"))
        part["status"] = "released"
        part["footprint_checked"] = "2026-11-05"
        part["mpns"][0]["verified"] = "2026-11-05"
        part["notes"] = "TEST ONLY: marked released by spikes/m2/acceptance.py, not checked."
        path.write_text(yaml.safe_dump(part, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return root


def run(*args: str | Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([*map(str, args)], capture_output=True, text=True, env=env, check=False)


def placed_parts(root: Path) -> list[tuple[str, str, str, str]]:
    """(library, IPN, symbol block, footprint id) for every generated part."""
    placed = []
    for library_file in sorted((root / "build/symbols").glob("*.kicad_sym")):
        text = library_file.read_text(encoding="utf-8")
        for ipn in re.findall(r'^\t\(symbol "([^"]+)"', text, re.MULTILINE):
            block = kicadgen.extract_symbol(text, ipn)
            footprint = re.search(r'\(property "Footprint" "([^"]+)"', block).group(1)
            placed.append((library_file.stem, ipn, block, footprint))
    return placed


def schematic(placed) -> str:
    root = stable_uuid("schematic")
    embedded, instances = [], []
    for index, (library, ipn, block, _footprint) in enumerate(placed):
        embedded.append(block.replace(f'(symbol "{ipn}"', f'(symbol "{library}:{ipn}"', 1))
        reference = f"{'R' if library == 'PL_Resistor' else 'C'}{index + 1}"
        properties = "\n\t\t".join(
            re.sub(r'^\(property "Reference" "[^"]*"', f'(property "Reference" "{reference}"', p)
            for p in _properties(block)
        )
        instances.append(f"""\t(symbol
\t\t(lib_id "{library}:{ipn}")
\t\t(at {25.4 + 12.7 * index} 50.8 0)
\t\t(unit 1)
\t\t(exclude_from_sim no)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(uuid "{stable_uuid(ipn)}")
\t\t{properties}
\t\t(pin "1" (uuid "{stable_uuid(ipn + '/1')}"))
\t\t(pin "2" (uuid "{stable_uuid(ipn + '/2')}"))
\t\t(instances (project "m2" (path "/{root}" (reference "{reference}") (unit 1))))
\t)""")
    lib_symbols = "\n\t".join(embedded)
    body = "\n".join(instances)
    return f"""(kicad_sch
\t(version 20260306)
\t(generator "partslib")
\t(generator_version "0.0")
\t(uuid "{root}")
\t(paper "A4")
\t(lib_symbols
\t{lib_symbols}
\t)
{body}
\t(sheet_instances (path "/" (page "1")))
)
"""


def _properties(block: str) -> list[str]:
    return [
        block[start:end]
        for start, end in kicadgen.child_spans(block)
        if block[start:end].startswith("(property ")
    ]


def board(root: Path, placed) -> str:
    footprints = []
    for index, (_library, ipn, _block, footprint) in enumerate(placed):
        library, name = footprint.split(":", 1)
        text = (root / f"build/footprints/{library}.pretty/{name}.kicad_mod").read_text(encoding="utf-8")
        text = text.replace(f'(footprint "{name}"', f'(footprint "{footprint}"', 1)
        text = text.replace('"REF**"', f'"X{index + 1}"', 1)
        text = text.replace(
            '(layer "F.Cu")\n',
            f'(layer "F.Cu")\n\t(uuid "{stable_uuid(ipn + "/fp")}")\n\t(at {10 + 4 * index} 10)\n'
            f'\t(path "/{stable_uuid(ipn)}")\n',
            1,
        )
        footprints.append(text)
    layers = (REPO / "spikes/m0/run.py").read_text(encoding="utf-8")
    layers = layers[layers.index('LAYERS = """') + 12 : layers.index('\\t)"""') + 3]
    layers = layers.encode().decode("unicode_escape")
    return f"""(kicad_pcb
\t(version 20260206)
\t(generator "partslib")
\t(generator_version "0.0")
\t(general (thickness 1.6))
\t(paper "A4")
\t{layers}
\t(setup (pad_to_mask_clearance 0))
\t(net 0 "")
{"".join(footprints)}
\t(gr_rect (start 7 7) (end {12 + 4 * len(placed)} 13) (stroke (width 0.05) (type default)) (fill no) (layer "Edge.Cuts") (uuid "{stable_uuid('outline')}"))
)
"""


def erc_types(project: Path, env: dict[str, str], out: Path) -> set[str]:
    run(KICAD_CLI, "sch", "erc", "--format", "json", "--severity-all", "-o", out, project / "m2.kicad_sch", env=env)
    data = json.loads(out.read_text(encoding="utf-8"))
    return {v["type"] for sheet in data["sheets"] for v in sheet["violations"]}


def step_size(project: Path, env: dict[str, str], out: Path) -> int:
    run(KICAD_CLI, "pcb", "export", "step", "--force", "-o", out, project / "m2.kicad_pcb", env=env)
    return out.stat().st_size if out.exists() else 0


def main() -> int:
    work = Path(os.environ.get("M2_WORK") or tempfile.mkdtemp(prefix="partslib-m2-"))
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    root = released_copy(work)
    base_env = {k: v for k, v in os.environ.items() if k != "PARTSLIB"}
    base_env["PYTHONPATH"] = str(REPO / "src")
    print(f"working directory {work}\n")

    result = run(*PARTSLIB, "--root", root, "build", env=base_env)
    report("build succeeds", result.returncode == 0, (result.stdout + result.stderr).strip().splitlines()[0])
    placed = placed_parts(root)
    report("every part is generated", len(placed) == len(list((root / "parts").rglob("*.yaml"))), f"{len(placed)} symbols")

    installed = work / "config-installed"
    version = run(KICAD_CLI, "version").stdout.strip().rsplit(".", 1)[0]
    result = run(*PARTSLIB, "--root", root, "install", "--config-dir", installed / version, env=base_env)
    report("install succeeds", result.returncode == 0, (result.stdout + result.stderr).strip())
    again = run(*PARTSLIB, "--root", root, "install", "--config-dir", installed / version, env=base_env)
    report("installing again changes nothing", again.returncode == 0 and "backed up" not in again.stdout)

    project = work / "new-project"
    project.mkdir()
    (project / "m2.kicad_pro").write_text(json.dumps({"meta": {"filename": "m2.kicad_pro", "version": 3}}), encoding="utf-8")
    (project / "m2.kicad_sch").write_text(schematic(placed), encoding="utf-8")
    (project / "m2.kicad_pcb").write_text(board(root, placed), encoding="utf-8")
    report("the new project has no library tables of its own", not list(project.glob("*-lib-table")))

    with_install = {**base_env, "KICAD_CONFIG_HOME": str(installed)}
    empty = work / "config-empty"
    empty.mkdir()
    without = {**base_env, "KICAD_CONFIG_HOME": str(empty)}

    types = erc_types(project, with_install, work / "erc-installed.json")
    report("KiCad finds every symbol and footprint through the installed config", not types & LIBRARY_ISSUES, ", ".join(sorted(types & LIBRARY_ISSUES)))
    types = erc_types(project, without, work / "erc-empty.json")
    report("  control: with an empty config KiCad cannot find them", bool(types & LIBRARY_ISSUES), ", ".join(sorted(types & LIBRARY_ISSUES)))

    resolved = step_size(project, with_install, work / "installed.step")
    unresolved = step_size(project, without, work / "empty.step")
    report(
        "3D models resolve through ${PARTSLIB} from the installed config",
        resolved > unresolved > 0,
        f"STEP export {resolved} bytes installed, {unresolved} with an empty config",
    )

    print(f"\n{len(failures)} failed" if failures else "\nAll checks passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
