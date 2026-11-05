"""Migrating a board onto library parts (spec section 9). The board files are never written."""

from pathlib import Path
import re

import pytest
import yaml
from fastapi.testclient import TestClient

from boards import sub_sheet, write_board
from partslib import migrate
from partslib.board import BoardEntry, Source, load_board
from partslib.build import build_library
from partslib.scan import KICAD_CLI, scan
from partslib.web.app import create_app
from test_check import RESISTOR, edit, library, write  # noqa: F401 - library is a fixture
from test_edit import commits, git, last_commit_files, repo  # noqa: F401 - repo is a fixture


def unmigrated(ipn: str = "", **fields: str) -> str:
    """The fixture sub-sheet as an old board would have it: catalogue fields, no IPN."""
    text = sub_sheet(ipn=ipn)
    extra = "".join(f'\t\t(property "{k}" "{v}" (at 100 100 0) (effects (font (size 1.27 1.27))))\n' for k, v in fields.items())
    return text.replace('\t\t(pin "1" (uuid "00000000-0000-4000-8000-0000000000c2"))', extra + '\t\t(pin "1" (uuid "00000000-0000-4000-8000-0000000000c2"))', 1)


@pytest.fixture
def board(repo, tmp_path):
    directory = write_board(tmp_path / "demo")
    (directory / "sub.kicad_sch").write_text(
        unmigrated(Part="0603WAF1001T5E", Manufacturer="UNI-ROYAL(Uniroyal Elec)", LCSC="C21190",
                   Resistance="1kΩ", Tolerance="±1%", **{"Power(Watts)": "100mW", "Type": "Thick Film Resistors"}),
        encoding="utf-8",
    )
    write(repo, "boards.yaml", f"- name: demo\n  path: {directory}\n  status: in_design\n")
    git(repo, "add", "boards.yaml")
    git(repo, "commit", "-qm", "register the board")
    return directory


def groups_of(repo, board):
    return migrate.load(repo, BoardEntry("demo", board, "", "", "in_design"), None)[1]


def test_a_part_with_the_mpn_on_the_board_is_suggested(repo, board):
    [group] = groups_of(repo, board)
    assert group.status == "suggested"
    assert group.suggestion == {"ipn": "RES-000123", "how": "same MPN"}
    assert group.references == ["R1", "R2"]


def test_a_reused_sheet_symbol_is_one_change_for_all_its_designators(repo, board):
    [group] = groups_of(repo, board)
    assert group.symbols == [{"sheet_file": "sub.kicad_sch", "uuid": "00000000-0000-4000-8000-0000000000c1",
                              "designators": ["R1", "R2"]}]


def test_values_are_inferred_from_the_catalogue_fields(repo, board):
    (board / "sub.kicad_sch").write_text(
        unmigrated(Resistance="4.7kΩ", Tolerance="±1%", **{"Power(Watts)": "100mW", "Type": "Thick Film Resistors"}),
        encoding="utf-8",
    )
    [group] = groups_of(repo, board)
    assert group.status == "unmatched" and group.category == "resistor"
    assert group.params == {"resistance_ohm": "4.7kΩ", "tolerance_pct": "1%", "power_w": "100mW",
                            "package": "0603", "technology": "thick_film"}


def test_identity_matches_when_there_is_no_mpn(repo, board):
    (board / "sub.kicad_sch").write_text(
        unmigrated(Resistance="1k", Tolerance="1%", **{"Power(Watts)": "0.1W", "Type": "Thick Film Resistors"}),
        encoding="utf-8",
    )
    [group] = groups_of(repo, board)
    assert group.suggestion == {"ipn": "RES-000123", "how": "same identity"}


@pytest.fixture
def client(repo, board):
    client = TestClient(create_app(repo, allowed_hosts=("testserver",)), follow_redirects=False)
    client.token = re.search(r'name="csrf" value="([^"]+)"', client.get("/boards/demo/migrate").text).group(1)
    return client


def test_confirming_is_one_commit_and_lists_the_kicad_change(repo, board, client):
    [group] = groups_of(repo, board)
    before = commits(repo)
    response = client.post("/boards/demo/migrate", data={"csrf": client.token, "group": group.key, "ipn": "RES-000123"})
    assert response.status_code == 303 and "saved=" in response.headers["location"]
    assert commits(repo) == before + 1 and last_commit_files(repo) == ["migrations/demo.yaml"]
    record = yaml.safe_load((repo / "migrations/demo.yaml").read_text(encoding="utf-8"))
    assert record["matches"][0]["ipn"] == "RES-000123" and record["matches"][0]["mpn"] == "0603WAF1001T5E"
    [confirmed] = groups_of(repo, board)
    assert confirmed.status == "confirmed"
    page = client.get("/boards/demo/migrate").text
    changes = page[page.index("Changes to make in KiCad"):]
    assert "sub.kicad_sch" in changes and "R1, R2" in changes and "RES-000123" in changes
    assert "PL_Resistor:RES-000123" in changes, "released, so it can move to its library symbol too"
    csv = client.get("/boards/demo/migrate/changes.csv").text.splitlines()
    assert csv[1] == "sub.kicad_sch,R1 R2,PL_Resistor:RES-000123,1kΩ,RES-000123,PL_Resistor:RES-000123"


def test_confirm_all_same_mpn_matches_at_once(repo, board, client):
    before = commits(repo)
    client.post("/boards/demo/migrate", data={"csrf": client.token, "all": "same-mpn"})
    assert commits(repo) == before + 1
    assert groups_of(repo, board)[0].status == "confirmed"


def test_confirming_an_unknown_ipn_is_refused(repo, board, client):
    [group] = groups_of(repo, board)
    before = commits(repo)
    response = client.post("/boards/demo/migrate", data={"csrf": client.token, "group": group.key, "ipn": "RES-999999"})
    assert "no part RES-999999" in client.get(response.headers["location"]).text
    assert commits(repo) == before


def test_once_the_ipn_is_on_the_board_the_group_is_done(repo, board, client):
    [group] = groups_of(repo, board)
    client.post("/boards/demo/migrate", data={"csrf": client.token, "group": group.key, "ipn": "RES-000123"})
    edit(board, "sub.kicad_sch", '(property "IPN" ""', '(property "IPN" "RES-000123"')  # what the user does in KiCad
    [done] = groups_of(repo, board)
    assert done.status == "done"
    assert "no-ipn" not in {f.code for f in scan(repo, BoardEntry("demo", board, "", "", "in_design"), None, None).findings}


def test_an_ipn_on_the_board_that_differs_from_the_confirmed_one_is_a_conflict(repo, board, client):
    [group] = groups_of(repo, board)
    write(repo, "parts/resistor/RES-000124.yaml", (repo / RESISTOR).read_text(encoding="utf-8")
          .replace("RES-000123", "RES-000124").replace("tolerance_pct: 1", "tolerance_pct: 5")
          .replace("0603WAF1001T5E", "OTHER").replace('notes: ""', "notes: ''\nnear_match_ack: [RES-000123]"))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "another part")
    client.post("/boards/demo/migrate", data={"csrf": client.token, "group": group.key, "ipn": "RES-000123"})
    edit(board, "sub.kicad_sch", '(property "IPN" ""', '(property "IPN" "RES-000124"')
    assert groups_of(repo, board)[0].status == "conflict"


def test_the_board_files_are_never_written(repo, board, client):
    before = {p: p.read_bytes() for p in board.rglob("*") if p.is_file()}
    [group] = groups_of(repo, board)
    client.post("/boards/demo/migrate", data={"csrf": client.token, "group": group.key, "ipn": "RES-000123"})
    client.get("/boards/demo/migrate")
    assert {p: p.read_bytes() for p in board.rglob("*") if p.is_file()} == before


@pytest.mark.skipif(not Path(KICAD_CLI).exists(), reason="needs KiCad")
def test_a_board_on_library_parts_opens_with_only_partslib_installed(repo, board):
    """M6 acceptance, second half: a board using only PL_ libraries opens from a fresh clone on a
    machine with partslib installed, and nothing else."""
    (board / "sub.kicad_sch").write_text(sub_sheet(), encoding="utf-8")  # IPN set, PL_ symbol and footprint
    (board / "sym-lib-table").unlink()
    (board / "fp-lib-table").unlink()
    assert build_library(repo)[0] == []
    git(board, "init", "-q")
    git(board, "add", "-A")
    git(board, "commit", "-qm", "board on library parts")
    entry = BoardEntry("demo", board, "", "", "in_design")
    with_partslib = scan(repo, entry, "HEAD", None, fresh="partslib")
    assert [f for f in with_partslib.findings if f.code == "fresh-checkout"] == []
    bare = scan(repo, entry, "HEAD", None, fresh="bare")
    assert [f for f in bare.findings if f.code == "fresh-checkout"], "without partslib KiCad cannot find PL_ libraries"


DEV1 = Path.home() / "code/sgc/hardware/DEV1_50_2_SGC-LP"


@pytest.mark.skipif(not (DEV1 / "DEV1_50_2_SGC-LP.kicad_pro").exists(), reason="needs the SGC repository")
def test_dev1_0603_passives_match_the_library_by_mpn():
    repo_root = Path(__file__).resolve().parents[1]
    board = load_board("DEV1_50_2_SGC-LP", Source(DEV1, "daebef2eef3251398ece349d8100f60592e1a1ca"))
    groups = migrate.plan(repo_root, board)
    passives = [g for g in groups if "0603" in g.lib_id and g.category in ("resistor", "capacitor")]
    assert len(passives) == 11
    assert all(g.suggestion and g.suggestion["how"] == "same MPN" for g in passives)
    assert sum(len(g.references) for g in passives) == 100
