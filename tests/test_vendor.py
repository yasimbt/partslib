"""`partslib vendor` (spec section 6). M7 acceptance: a vendored board opens correctly on a machine
without partslib installed."""

import json
from pathlib import Path
import subprocess

import pytest
import yaml

from boards import SHEET_A, SHEET_B, pcb, pcb_footprint, sub_sheet, write_board
from partslib.board import BoardEntry
from partslib.build import build_library
from partslib.cli import main
from partslib.scan import KICAD_CLI, scan
from partslib.vendor import VENDOR_DIR, VendorError, vendor
from test_check import RESISTOR, edit, library, write  # noqa: F401 - library is a fixture
from test_edit import git

MODEL = "${PARTSLIB}/3dmodels/PL_Resistor.3dshapes/R_0603.step"


def not_running():
    return []


@pytest.fixture
def board(library, tmp_path):
    """A board migrated onto library parts, in its own git repository, with no library tables."""
    assert build_library(library)[0] == []
    directory = write_board(tmp_path / "demo")
    (directory / "sub.kicad_sch").write_text(sub_sheet(), encoding="utf-8")
    (directory / "demo.kicad_pcb").write_text(
        pcb(pcb_footprint(SHEET_A, "R1", model=MODEL) + pcb_footprint(SHEET_B, "R2", model=MODEL, x=54)), encoding="utf-8"
    )
    for leftover in ("sym-lib-table", "fp-lib-table", "lib/R_0603.step"):
        (directory / leftover).unlink()
    git(directory, "init", "-q")
    git(directory, "add", "-A")
    git(directory, "commit", "-qm", "board on library parts")
    write(library, "boards.yaml", f"- name: demo\n  path: {directory}\n  status: released\n")
    return directory


def entry(board: Path) -> BoardEntry:
    return BoardEntry("demo", board, "", "", "released")


def commit_board(board: Path) -> None:
    git(board, "add", "-A")
    git(board, "commit", "-qm", "vendor")


def test_vendor_copies_what_the_board_uses_and_points_the_board_at_it(library, board):
    result = vendor(library, entry(board), kicad_processes=not_running)
    assert (result.symbols, result.footprints, result.models) == (1, 1, 1)
    assert (board / VENDOR_DIR / "symbols/PL_Resistor.kicad_sym").read_text(encoding="utf-8").count('\n\t(symbol "') == 1
    assert (board / VENDOR_DIR / "footprints/PL_Resistor.pretty/R_0603.kicad_mod").is_file()
    assert (board / VENDOR_DIR / "3dmodels/PL_Resistor.3dshapes/R_0603.step").read_bytes() == (library / "build/3dmodels/PL_Resistor.3dshapes/R_0603.step").read_bytes()
    assert '(uri "${KIPRJMOD}/lib/partslib/symbols/PL_Resistor.kicad_sym")' in (board / "sym-lib-table").read_text(encoding="utf-8")
    assert '(uri "${KIPRJMOD}/lib/partslib/footprints/PL_Resistor.pretty")' in (board / "fp-lib-table").read_text(encoding="utf-8")
    project = json.loads((board / "demo.kicad_pro").read_text(encoding="utf-8"))
    assert project["text_variables"]["PARTSLIB"] == "${KIPRJMOD}/lib/partslib"
    assert (board / "demo.kicad_pcb").read_text(encoding="utf-8").count(MODEL) == 2, "the PCB file is not edited"


def test_provenance_travels_with_the_copies(library, board):
    vendor(library, entry(board), kicad_processes=not_running)
    entries = yaml.safe_load((board / VENDOR_DIR / "provenance.yaml").read_text(encoding="utf-8"))["vendored"]
    assert {e["asset"] for e in entries} == {"Resistor:R", "Resistor:R_0603", "Resistor/R_0603.step"}
    assert all(e["licence"] == "CC-BY-SA-4.0" and e["parts"] == "RES-000123" for e in entries)


def test_after_vendoring_nothing_on_the_board_depends_on_this_machine(library, board):
    before = {f.code for f in scan(library, entry(board), None, None).findings}
    assert "machine-specific" in before or "library-unresolved" in before
    vendor(library, entry(board), kicad_processes=not_running)
    assert scan(library, entry(board), None, None).findings == []


@pytest.mark.skipif(not Path(KICAD_CLI).exists(), reason="needs KiCad")
def test_acceptance_a_vendored_board_opens_on_a_machine_without_partslib(library, board):
    bare_before = scan(library, entry(board), "HEAD", None, fresh="bare")
    problems = [f.message for f in bare_before.findings if f.code == "fresh-checkout"]
    assert any("PL_Resistor" in p for p in problems) and any("3D model" in p for p in problems), "control"
    vendor(library, entry(board), kicad_processes=not_running)
    commit_board(board)
    bare_after = scan(library, entry(board), "HEAD", None, fresh="bare")
    assert [f for f in bare_after.findings if f.code == "fresh-checkout"] == []


@pytest.mark.skipif(not Path(KICAD_CLI).exists(), reason="needs KiCad")
def test_the_vendored_copies_win_where_partslib_is_also_installed(library, board, tmp_path):
    vendor(library, entry(board), kicad_processes=not_running)
    commit_board(board)
    edit(library, RESISTOR, "description: 1 kΩ", "description: changed later in the library")
    assert build_library(library)[0] == []
    result = scan(library, entry(board), "HEAD", None, fresh="partslib")
    assert [f for f in result.findings if f.code == "fresh-checkout"] == []


def test_vendoring_twice_changes_nothing(library, board):
    vendor(library, entry(board), kicad_processes=not_running)
    commit_board(board)
    snapshot = {p: p.read_bytes() for p in board.rglob("*") if p.is_file() and ".git" not in p.parts}
    again = vendor(library, entry(board), kicad_processes=not_running)
    assert again.changed == []
    assert {p: p.read_bytes() for p in board.rglob("*") if p.is_file() and ".git" not in p.parts} == snapshot


def test_other_library_entries_and_files_in_lib_are_kept(library, board):
    write(board, "sym-lib-table", '(sym_lib_table\n\t(version 7)\n\t(lib (name "Mine") (type "KiCad") (uri "${KIPRJMOD}/lib/Mine.kicad_sym") (options "") (descr ""))\n)\n')
    write(board, "lib/Mine.kicad_sym", "(kicad_symbol_lib)\n")
    commit_board(board)
    vendor(library, entry(board), kicad_processes=not_running)
    table = (board / "sym-lib-table").read_text(encoding="utf-8")
    assert '(name "Mine")' in table and '(name "PL_Resistor")' in table
    assert (board / "lib/Mine.kicad_sym").is_file()


def test_dry_run_writes_nothing(library, board):
    result = vendor(library, entry(board), dry_run=True, kicad_processes=not_running)
    assert "sym-lib-table" in result.changed and not (board / VENDOR_DIR).exists()


def test_refuses_while_kicad_is_open(library, board):
    with pytest.raises(VendorError, match="close KiCad"):
        vendor(library, entry(board), kicad_processes=lambda: ["kicad"])
    assert not (board / VENDOR_DIR).exists()


def test_refuses_over_uncommitted_edits_unless_forced(library, board):
    project = board / "demo.kicad_pro"
    project.write_text(project.read_text(encoding="utf-8").replace('"version": 3', '"version": 3, "x": 1'), encoding="utf-8")
    with pytest.raises(VendorError, match="uncommitted"):
        vendor(library, entry(board), kicad_processes=not_running)
    assert vendor(library, entry(board), force=True, kicad_processes=not_running).symbols == 1


def test_refuses_a_part_that_is_not_built(library, board):
    edit(library, RESISTOR, "status: released", "status: draft")
    assert build_library(library)[0] == []
    with pytest.raises(VendorError, match="not in the built libraries"):
        vendor(library, entry(board), kicad_processes=not_running)


def test_refuses_a_stale_build(library, board):
    edit(library, RESISTOR, "description: 1 kΩ", "description: One kΩ")
    with pytest.raises(VendorError, match="partslib build"):
        vendor(library, entry(board), kicad_processes=not_running)


def test_refuses_to_replace_a_partslib_variable_set_by_someone_else(library, board):
    project = board / "demo.kicad_pro"
    data = json.loads(project.read_text(encoding="utf-8"))
    data["text_variables"] = {"PARTSLIB": "/somewhere/else"}
    project.write_text(json.dumps(data), encoding="utf-8")
    commit_board(board)
    with pytest.raises(VendorError, match="already sets PARTSLIB"):
        vendor(library, entry(board), kicad_processes=not_running)


def test_libraries_not_from_partslib_are_reported(library, board):
    (board / "sub.kicad_sch").write_text(sub_sheet(footprint="Other:R_0603"), encoding="utf-8")
    commit_board(board)
    result = vendor(library, entry(board), kicad_processes=not_running)
    assert result.not_vendored == ["footprint library Other"]


def test_cli_vendor(library, board, capsys, monkeypatch):
    monkeypatch.setattr("partslib.vendor.running_kicad", not_running)
    assert main(["--root", str(library), "vendor", "demo", "--dry-run"]) == 0
    assert "would vendor 1 symbols, 1 footprints and 1 3D models" in capsys.readouterr().out
    assert not (board / VENDOR_DIR).exists()
