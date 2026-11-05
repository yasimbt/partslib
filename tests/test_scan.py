"""Every board check in scan.CHECKS needs a case that makes it fire on a broken board (spec 7)."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from boards import SHEET_A, SHEET_B, pcb, pcb_footprint, root_sheet, sub_sheet, write_board
from partslib.board import BoardEntry, Source, load_board
from partslib.cli import main
from partslib.scan import CHECKS, KICAD_CLI, Environment, scan, scan_board
from test_check import RESISTOR, edit, library, write  # noqa: F401 - library is a fixture


@pytest.fixture
def setup(library, tmp_path):
    board = write_board(tmp_path / "demo")
    config = tmp_path / "config"
    config.mkdir()
    return library, board, config


def run_scan(root: Path, board: Path, config: Path, status: str = "in_design", commit=None, fresh=False):
    entry = BoardEntry("demo", board, "", "", status)
    return scan(root, entry, commit, config, fresh=fresh)


def codes(result) -> set[str]:
    return {f.code for f in result.findings}


def test_the_fixture_board_is_clean(setup):
    result = run_scan(*setup)
    assert result.findings == []
    assert [c["reference"] for c in result.components] == ["R1", "R2"]


def test_the_fixture_board_is_clean_at_release(setup):
    assert run_scan(*setup, status="released").findings == []


def test_reused_sheets_are_expanded_per_instance(setup):
    _root, board, _config = setup
    placements = [p for p in load_board("demo", Source(board, None)).placements if not p.power]
    assert {(p.reference, p.sheet) for p in placements} == {("R1", "/Half_A/"), ("R2", "/Half_B/")}
    assert placements[0].fields["Reference"] == "R2", "the Reference property alone would give R2 twice"


# --- One function per check. Each breaks the clean board so that its check fires. ---


def no_ipn(root, board, config):
    (board / "sub.kicad_sch").write_text(sub_sheet(ipn=""), encoding="utf-8")


def unknown_ipn(root, board, config):
    (board / "sub.kicad_sch").write_text(sub_sheet(ipn="RES-999999"), encoding="utf-8")


def instance_missing(root, board, config):
    edit(board, "sub.kicad_sch", f'(path "/00000000-0000-4000-8000-000000000001/{SHEET_B}" (reference "R2") (unit 1))', "")


def missing_on_pcb(root, board, config):
    (board / "demo.kicad_pcb").write_text(pcb(pcb_footprint(SHEET_A, "R1")), encoding="utf-8")


def missing_in_schematic(root, board, config):
    extra = pcb_footprint("00000000-0000-4000-8000-000000000099", "R9", x=60)
    (board / "demo.kicad_pcb").write_text(
        pcb(pcb_footprint(SHEET_A, "R1") + pcb_footprint(SHEET_B, "R2", x=54) + extra), encoding="utf-8"
    )


def footprint_mismatch(root, board, config):
    write(board, "lib/PL_Resistor.pretty/R_0805.kicad_mod", '(footprint "R_0805")\n')
    (board / "demo.kicad_pcb").write_text(
        pcb(pcb_footprint(SHEET_A, "R1") + pcb_footprint(SHEET_B, "R2", "PL_Resistor:R_0805", x=54)), encoding="utf-8"
    )


def no_footprint(root, board, config):
    (board / "sub.kicad_sch").write_text(sub_sheet(footprint=""), encoding="utf-8")


def no_model(root, board, config):
    (board / "demo.kicad_pcb").write_text(
        pcb(pcb_footprint(SHEET_A, "R1") + pcb_footprint(SHEET_B, "R2", model=None, x=54)), encoding="utf-8"
    )


def model_unresolved(root, board, config):
    (board / "lib/R_0603.step").unlink()


def model_unresolved_stock(root, board, config):
    model = "${KICAD10_3DMODEL_DIR}/Resistor_SMD.3dshapes/No_Such_Model.step"
    (board / "demo.kicad_pcb").write_text(
        pcb(pcb_footprint(SHEET_A, "R1") + pcb_footprint(SHEET_B, "R2", model=model, x=54)), encoding="utf-8"
    )


def machine_specific_model(root, board, config):
    write(config, "models/r.step", "ISO-10303-21;\n")
    write(config, "kicad_common.json", json.dumps({"environment": {"vars": {"MYLIB": str(config / "models")}}}))
    (board / "demo.kicad_pcb").write_text(
        pcb(pcb_footprint(SHEET_A, "R1") + pcb_footprint(SHEET_B, "R2", model="${MYLIB}/r.step", x=54)), encoding="utf-8"
    )


def machine_specific_library(root, board, config):
    shutil.move(board / "fp-lib-table", config / "fp-lib-table")
    edit(config, "fp-lib-table", "${KIPRJMOD}/lib", str(board / "lib"))


def library_unresolved(root, board, config):
    (board / "sym-lib-table").unlink()


def library_item_missing(root, board, config):
    (board / "lib/PL_Resistor.pretty/R_0603.kicad_mod").unlink()


def draft_part(root, board, config):
    edit(root, RESISTOR, "status: released", "status: draft")


def retired_part(root, board, config):
    edit(root, RESISTOR, "status: released", "status: deprecated")


def obsolete_part(root, board, config):
    edit(root, RESISTOR, "status: released", "status: obsolete")


def no_active_mpn(root, board, config):
    edit(root, RESISTOR, "lifecycle: active", "lifecycle: eol")


def duplicate_function(root, board, config):
    similar = (root / RESISTOR).read_text(encoding="utf-8")
    similar = similar.replace("ipn: RES-000123", "ipn: RES-000124\nnear_match_ack: [RES-000123]")
    similar = similar.replace("tolerance_pct: 1", "tolerance_pct: 5").replace("0603WAF1001T5E", "OTHER-MPN")
    write(root, "parts/resistor/RES-000124.yaml", similar)
    (board / "sub2.kicad_sch").write_text(sub_sheet(ipn="RES-000124"), encoding="utf-8")
    (board / "demo.kicad_sch").write_text(root_sheet(file_b="sub2.kicad_sch"), encoding="utf-8")


def fresh_checkout(root, board, config):
    (board / "sym-lib-table").unlink()
    (board / "fp-lib-table").unlink()


CASES = {
    "no-ipn": [no_ipn],
    "unknown-ipn": [unknown_ipn],
    "instance-missing": [instance_missing],
    "missing-on-pcb": [missing_on_pcb],
    "missing-in-schematic": [missing_in_schematic],
    "footprint-mismatch": [footprint_mismatch],
    "no-footprint": [no_footprint],
    "no-model": [no_model],
    "model-unresolved": [model_unresolved, model_unresolved_stock],
    "machine-specific": [machine_specific_model, machine_specific_library],
    "library-unresolved": [library_unresolved],
    "library-item-missing": [library_item_missing],
    "draft-part": [draft_part],
    "retired-part": [retired_part],
    "obsolete-part": [obsolete_part],
    "no-active-mpn": [no_active_mpn],
    "duplicate-function": [duplicate_function],
    "fresh-checkout": [fresh_checkout],
}


def test_every_check_has_a_failing_case():
    assert set(CASES) == set(CHECKS)


@pytest.mark.parametrize(
    "code, breaker",
    [(code, breaker) for code, breakers in CASES.items() for breaker in breakers],
    ids=lambda value: value if isinstance(value, str) else value.__name__,
)
def test_check_fires(setup, code, breaker):
    fresh = code == "fresh-checkout"
    if fresh and not Path(KICAD_CLI).exists():
        pytest.skip("needs kicad-cli")
    breaker(*setup)
    assert code in codes(run_scan(*setup, fresh=fresh))


def test_a_clean_board_passes_the_fresh_checkout(setup):
    if not Path(KICAD_CLI).exists():
        pytest.skip("needs kicad-cli")
    assert run_scan(*setup, fresh=True).findings == []


@pytest.mark.parametrize("code, design, release", [("no-ipn", "warning", "error"), ("machine-specific", "info", "error")])
def test_severity_rises_at_release(setup, code, design, release):
    {"no-ipn": no_ipn, "machine-specific": machine_specific_model}[code](*setup)
    for status, severity in (("in_design", design), ("released", release)):
        assert {f.severity for f in run_scan(*setup, status=status).findings if f.code == code} == {severity}


def test_obsolete_is_an_error_even_in_design(setup):
    obsolete_part(*setup)
    assert [f.severity for f in run_scan(*setup).findings if f.code == "obsolete-part"] == ["error"]


def git(directory: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(directory), *args],
        check=True,
        capture_output=True,
    )


def test_a_commit_is_scanned_as_committed_not_as_on_disk(setup):
    root, board, config = setup
    git(board, "init", "-q")
    git(board, "add", "-A")
    git(board, "commit", "-qm", "board")
    no_ipn(root, board, config)
    assert run_scan(root, board, config, commit="HEAD").findings == []
    assert "no-ipn" in codes(run_scan(root, board, config))


def test_a_model_left_out_of_the_commit_is_unresolved_at_that_commit(setup):
    root, board, config = setup
    git(board, "init", "-q")
    git(board, "add", "-A")
    git(board, "rm", "-q", "--cached", "lib/R_0603.step")
    git(board, "commit", "-qm", "board")
    assert "model-unresolved" in codes(run_scan(root, board, config, commit="HEAD"))
    assert run_scan(root, board, config).findings == [], "the file is on disk, so the working tree is fine"


def test_cli_scans_registered_boards_saves_results_and_fails_on_errors(setup, capsys):
    root, board, config = setup
    write(root, "boards.yaml", f"- name: demo\n  path: {board}\n  status: in_design\n")
    assert main(["--root", str(root), "scan", "--worktree", "--config-dir", str(config)]) == 0
    saved = json.loads((root / "build/scans/demo/worktree.json").read_text(encoding="utf-8"))
    assert [c["reference"] for c in saved["components"]] == ["R1", "R2"]
    no_footprint(*setup)
    assert main(["--root", str(root), "scan", "--worktree", "--config-dir", str(config)]) == 1
    assert "no-footprint" in capsys.readouterr().out


def test_cli_migrate_lists_the_plan(setup, capsys):
    root, board, config = setup
    write(root, "boards.yaml", f"- name: demo\n  path: {board}\n  status: in_design\n")
    assert main(["--root", str(root), "migrate", "demo"]) == 0
    assert "2 of 2 parts have an IPN" in capsys.readouterr().out


def test_cli_rejects_an_unknown_board(setup, capsys):
    root, board, config = setup
    write(root, "boards.yaml", f"- name: demo\n  path: {board}\n  status: in_design\n")
    assert main(["--root", str(root), "scan", "nope", "--worktree"]) == 2
    assert "unknown board" in capsys.readouterr().err


# --- M3 acceptance: the real board, at the commit the section 1 audit was made against. ---

DEV1 = Path.home() / "code/sgc/hardware/DEV1_50_2_SGC-LP"
PINNED = "daebef2eef3251398ece349d8100f60592e1a1ca"
REAL_CONFIG = Path.home() / "Library/Preferences/kicad/10.0"


@pytest.fixture
def dev1():
    if not (DEV1 / "DEV1_50_2_SGC-LP.kicad_pro").exists() or not REAL_CONFIG.is_dir():
        pytest.skip("needs the SGC repository and this machine's KiCad config")
    repo = Path(__file__).resolve().parents[1]
    entry = BoardEntry("DEV1_50_2_SGC-LP", DEV1, "", "SGC", "in_design")
    return scan(repo, entry, PINNED, REAL_CONFIG)


def by_code(result, code):
    return [f for f in result.findings if f.code == code]


def test_acceptance_dev1_reports_the_section_1_problems(dev1):
    assert len(dev1.components) == 147
    assert by_code(dev1, "no-footprint")[0].references == ("FB2", "FB3", "FB4", "FB7", "FB8")
    assert len(by_code(dev1, "no-model")[0].references) == 11
    stock = [f for f in by_code(dev1, "model-unresolved") if "EPC_QFN-13-3EP" in f.message]
    assert len(stock) == 5
    models = [f for f in by_code(dev1, "machine-specific") if "${KICAD8_3RD_PARTY}" in f.message]
    assert len(models[0].references) == 68


def test_acceptance_dev1_has_no_false_results_from_per_instance_references(dev1):
    references = [c["reference"] for c in dev1.components]
    assert len(references) == len(set(references)), "every instance has its own designator"
    assert by_code(dev1, "instance-missing") == []
    assert by_code(dev1, "footprint-mismatch") == []
    halfbridges = {c["sheet"] for c in dev1.components if "HalfBridge" in c["sheet"]}
    assert len(halfbridges) == 5, "the half-bridge sheet is used five times"
