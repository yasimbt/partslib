"""Importing a part from a source: fetched files are staged, then added in one commit."""

from pathlib import Path
import re

import pytest
import yaml
from fastapi.testclient import TestClient

from partslib import edit, importer, kicadgen, sources
from partslib.check import check_library
from partslib.scan import KICAD_CLI
from partslib.sources import Fetched, SourceError
from partslib.web.app import create_app
from test_check import FOOTPRINT, SYMBOL_LIBRARY, library, write  # noqa: F401 - library is a fixture
from test_edit import commits, git, last_commit_files, repo  # noqa: F401 - repo is a fixture

MODEL = b"ISO-10303-21;\nHEADER;\nENDSEC;\n"


def fake_fetched(mpn="ACME-R1", lcsc="C999001", footprint_text=FOOTPRINT, model=MODEL) -> Fetched:
    block = kicadgen.extract_symbol(SYMBOL_LIBRARY, "R").replace('(symbol "R"', f'(symbol "{mpn}"', 1)
    block = block.replace('(symbol "R_1_1"', f'(symbol "{mpn}_1_1"')
    return Fetched(
        source="lcsc", symbol_library="LCSC", symbol_name=mpn, symbol_block=block,
        footprint_library="LCSC", footprint_name="R0603", footprint_text=footprint_text,
        model_name="R0603.step", model_bytes=model,
        provenance={"source": f"EasyEDA CAD data for LCSC {lcsc}, converted by easyeda2kicad",
                    "licence": "NOASSERTION", "version": "easyeda2kicad 1.0.1"},
        suggestion={"category": "resistor", "manufacturer": "ACME", "mpn": mpn, "lcsc": lcsc,
                    "datasheet": "https://example.com/r.pdf", "description": "",
                    "params": {"resistance_ohm": "4.7kΩ", "tolerance_pct": "1%", "power_w": "100mW",
                               "package": "0603", "technology": "thick_film"}},
    )


@pytest.fixture
def client(repo, monkeypatch):
    fetches = {}
    monkeypatch.setattr(importer, "fetch_lcsc", lambda code: fetches.get(code) or fake_fetched(lcsc=code))
    client = TestClient(create_app(repo, allowed_hosts=("testserver",)), follow_redirects=False)
    client.token = re.search(r'name="csrf" value="([^"]+)"', client.get("/parts/new").text).group(1)
    client.fetches = fetches
    return client


def stage(client, code="C999001") -> str:
    response = client.post("/import/fetch", data={"csrf": client.token, "source": "lcsc", "id": code})
    assert response.status_code == 303
    return response.headers["location"]


def confirm(client, location: str, **changes) -> object:
    page = client.get(location).text
    values = dict(re.findall(r'name="((?:param|mpn)\.[\w.]+|description|datasheet)" value="([^"]*)"', page))
    values.update(re.findall(r'<select name="(param\.\w+)"><option value=""></option>.*?<option selected>(\w+)</option>', page))
    return client.post(location, data={"csrf": client.token, "category": "resistor", **values, **changes})


def test_an_imported_part_is_one_commit_with_its_assets_and_passes_check(repo, client):
    before = commits(repo)
    location = stage(client)
    assert not (repo / "assets/symbols/LCSC.kicad_sym").exists(), "nothing is written before confirming"
    response = confirm(client, location)
    assert response.status_code == 303 and response.headers["location"].startswith("/parts/RES-000124?saved=")
    assert commits(repo) == before + 1
    assert sorted(last_commit_files(repo)) == [
        "assets/3dmodels/LCSC/R0603.step",
        "assets/footprints/LCSC.pretty/R0603.kicad_mod",
        "assets/provenance.yaml",
        "assets/symbols/LCSC.kicad_sym",
        "parts/resistor/RES-000124.yaml",
    ]
    part = yaml.safe_load((repo / "parts/resistor/RES-000124.yaml").read_text(encoding="utf-8"))
    assert (part["symbol"], part["footprint"], part["model"]) == ("LCSC:ACME-R1", "LCSC:R0603", "LCSC/R0603.step")
    assert part["status"] == "draft" and part["params"]["resistance_ohm"] == 4700
    assert part["mpns"][0] == {"manufacturer": "ACME", "mpn": "ACME-R1", "lcsc": "C999001", "lifecycle": "unknown"}
    assert check_library(repo)[1] == []
    assert not list((repo / importer.STAGING).iterdir()), "staging is cleared after adding"


def test_provenance_records_the_source_and_an_unknown_licence(repo, client):
    confirm(client, stage(client))
    entries = yaml.safe_load((repo / "assets/provenance.yaml").read_text(encoding="utf-8"))
    imported = [e for e in entries if e["id"].startswith("LCSC")]
    assert {e["kind"] for e in imported} == {"symbol", "footprint", "model"}
    assert all(e["licence"] == "NOASSERTION" and "C999001" in e["source"] for e in imported)
    assert len(entries) == 3 + 3, "the existing entries are kept"


def test_the_confirm_page_is_prefilled_and_shows_previews(repo, client):
    page = client.get(stage(client)).text
    assert 'value="4.7kΩ"' in page and 'value="ACME-R1"' in page and 'value="C999001"' in page
    assert "/preview/symbol.svg" in page and "/preview/footprint.svg" in page
    assert "NOASSERTION" in page


def test_a_second_part_reuses_an_identical_footprint(repo, client):
    confirm(client, stage(client))
    client.fetches["C999002"] = fake_fetched(mpn="ACME-R2", lcsc="C999002")
    response = confirm(client, stage(client, "C999002"), **{"param.resistance_ohm": "10k", "mpn.0.mpn": "ACME-R2"})
    assert response.status_code == 303
    assert "assets/footprints/LCSC.pretty/R0603.kicad_mod" not in last_commit_files(repo)
    part = yaml.safe_load((repo / "parts/resistor/RES-000125.yaml").read_text(encoding="utf-8"))
    assert part["footprint"] == "LCSC:R0603"
    symbols = (repo / "assets/symbols/LCSC.kicad_sym").read_text(encoding="utf-8")
    assert '(symbol "ACME-R1"' in symbols and '(symbol "ACME-R2"' in symbols


def test_a_different_footprint_with_the_same_name_gets_a_new_name(repo, client):
    confirm(client, stage(client))
    before = (repo / "assets/footprints/LCSC.pretty/R0603.kicad_mod").read_bytes()
    different = FOOTPRINT.replace("(at -0.8 0)", "(at -0.9 0)")
    client.fetches["C999002"] = fake_fetched(mpn="ACME-R2", lcsc="C999002", footprint_text=different)
    confirm(client, stage(client, "C999002"), **{"param.resistance_ohm": "10k", "mpn.0.mpn": "ACME-R2"})
    assert (repo / "assets/footprints/LCSC.pretty/R0603.kicad_mod").read_bytes() == before, "never changed"
    part = yaml.safe_load((repo / "parts/resistor/RES-000125.yaml").read_text(encoding="utf-8"))
    assert part["footprint"] == "LCSC:R0603_C999002"
    assert '(footprint "R0603_C999002"' in (repo / "assets/footprints/LCSC.pretty/R0603_C999002.kicad_mod").read_text(encoding="utf-8")
    assert check_library(repo)[1] == []


def test_a_rejected_import_writes_nothing(repo, client):
    before = commits(repo)
    response = confirm(client, stage(client), **{"mpn.0.mpn": "0603WAF1001T5E"})  # RES-000123's MPN
    assert response.status_code == 422 and "duplicate-mpn" in response.text
    assert commits(repo) == before
    assert not (repo / "assets/symbols/LCSC.kicad_sym").exists()
    assert not (repo / "assets/footprints/LCSC.pretty").exists() or not list((repo / "assets/footprints/LCSC.pretty").iterdir())
    assert "LCSC" not in (repo / "assets/provenance.yaml").read_text(encoding="utf-8")


def test_a_model_needs_git_lfs_when_the_repository_uses_it(repo, client, monkeypatch):
    write(repo, ".gitattributes", "assets/3dmodels/**/*.step filter=lfs diff=lfs merge=lfs -text\n")
    git(repo, "add", ".gitattributes")
    git(repo, "commit", "-qm", "lfs")
    monkeypatch.setattr(edit, "lfs_available", lambda root: False)
    before = commits(repo)
    response = confirm(client, stage(client))
    assert response.status_code == 422 and "nix develop" in response.text
    assert commits(repo) == before and not (repo / "assets/3dmodels/LCSC").exists()


def test_an_unknown_staging_id_is_404(client):
    assert client.get("/import/../../etc").status_code == 404
    assert client.get("/import/0123456789abcdef").status_code == 404


def test_search_shows_kicad_results_when_lcsc_is_unreachable(repo, client, monkeypatch):
    def offline(query, limit=10):
        raise SourceError("the JLCPCB parts search did not answer: offline")

    monkeypatch.setattr(sources, "search_lcsc", offline)
    monkeypatch.setattr(sources, "search_kicad", lambda q, cache, limit=25: [sources.Hit("kicad", "Device:R", "R", extra={"library": "Device"})])
    page = client.get("/import", params={"q": "R"}).text
    assert "offline" in page and "Device" in page


@pytest.mark.skipif(not Path(KICAD_CLI).exists(), reason="needs KiCad")
def test_import_from_kicads_own_libraries(repo):
    hits = sources.search_kicad("STM32G474RETx", repo / "build")
    assert hits and hits[0].id == "MCU_ST_STM32G4:STM32G474RETx"
    stage_id = importer.stage(repo, "kicad", hits[0].id, hits[0].footprint)
    ipn, _commit = importer.add(
        repo, stage_id, "ic", {"description": "STM32G474RET6 MCU", "datasheet": "https://example.com/st.pdf"},
        {"package": "LQFP-64"}, [{"manufacturer": "STMicroelectronics", "mpn": "STM32G474RET6"}], [],
    )
    part = yaml.safe_load((repo / f"parts/ic/{ipn}.yaml").read_text(encoding="utf-8"))
    assert part["symbol"] == "MCU_ST_STM32G4:STM32G474RETx"
    assert part["footprint"] == "Package_QFP:LQFP-64_10x10mm_P0.5mm"
    assert part["model"] == "Package_QFP/LQFP-64_10x10mm_P0.5mm.step"
    block = kicadgen.extract_symbol((repo / "assets/symbols/MCU_ST_STM32G4.kicad_sym").read_text(encoding="utf-8"), "STM32G474RETx")
    assert len(kicadgen.pin_numbers(block)) == 64, "the derived symbol is complete"
    assert check_library(repo)[1] == []
