"""The dashboard (spec section 8). M4 acceptance: finding which boards use a part takes under 10 s."""

from pathlib import Path
import re
import time

import pytest
from fastapi.testclient import TestClient

from boards import write_board
from partslib.board import BoardEntry
from partslib.cli import main
from partslib.scan import save, scan
from partslib.web.app import create_app
from test_check import RESISTOR, draft_resistor, edit, library, write  # noqa: F401 - library is a fixture


@pytest.fixture
def site(library, tmp_path):
    board = write_board(tmp_path / "demo")
    config = tmp_path / "config"
    config.mkdir()
    write(library, "boards.yaml", f"- name: demo\n  path: {board}\n  status: in_design\n")
    save(library, scan(library, BoardEntry("demo", board, "", "", "in_design"), None, config))
    return library, TestClient(create_app(library))


def test_acceptance_which_boards_use_a_part_is_found_in_well_under_10_seconds(site):
    _root, client = site
    start = time.monotonic()
    results = client.get("/parts", params={"q": "1 kΩ, 0603, ≤ 1%"})  # search as a person would
    link = re.search(r'href="(/parts/RES-000123)"', results.text)
    assert link, "the part is in the search results"
    detail = client.get(link.group(1))
    elapsed = time.monotonic() - start
    used = detail.text[detail.text.index("Where used") : detail.text.index("Parameters")]
    assert 'href="/boards/demo"' in used
    assert "R1, R2" in used
    assert ">2<" in used, "quantity: the reused sheet makes it two parts"
    assert elapsed < 10


def test_every_page_renders(site):
    _root, client = site
    for url in ("/", "/parts", "/parts/RES-000123", "/boards", "/boards/demo", "/boards/demo/bom.csv"):
        assert client.get(url).status_code == 200, url


@pytest.mark.parametrize(
    "query, found",
    [
        ("", True),
        ("1k", True),
        ("1000R", True),
        ("1 kΩ", True),
        ("2k", False),
        ("≤ 1%", True),
        ("< 1%", False),
        ("0603", True),
        ("0805", False),
        ("UNI-ROYAL", True),
        ("0603waf1001", True),
        ("RES-000123", True),
        ("thick", True),
        ("thin", False),
        ("100nF", False),
        ("≥ 50V", True),
        ("≥ 100V", False),
    ],
)
def test_search(site, query, found):
    _root, client = site
    assert ("RES-000123" in client.get("/parts", params={"q": query}).text) == found


def test_live_search_returns_only_the_results(site):
    _root, client = site
    response = client.get("/parts", params={"q": "1k"}, headers={"HX-Request": "true"})
    assert "<html" not in response.text and "RES-000123" in response.text


def test_filters_by_category_and_status(site):
    _root, client = site
    assert "RES-000123" in client.get("/parts", params={"status": "released"}).text
    assert "RES-000123" not in client.get("/parts", params={"status": "draft"}).text
    assert "RES-000123" not in client.get("/parts", params={"category": "capacitor"}).text


def test_board_page_has_bom_and_checks(site):
    root, client = site
    page = client.get("/boards/demo").text
    assert "All checks pass" in page
    assert 'href="/parts/RES-000123"' in page
    csv = client.get("/boards/demo/bom.csv").text.splitlines()
    assert csv[0] == "IPN,Description,Value,Footprint,Quantity,Designators"
    assert csv[1].startswith("RES-000123,") and csv[1].endswith(",2,R1 R2")


def test_board_findings_show_with_their_designators(site, tmp_path):
    root, client = site
    (tmp_path / "demo/sub.kicad_sch").write_text(
        (tmp_path / "demo/sub.kicad_sch").read_text(encoding="utf-8").replace('"PL_Resistor:R_0603" (at 100', '"" (at 100'),
        encoding="utf-8",
    )
    save(root, scan(root, BoardEntry("demo", tmp_path / "demo", "", "", "in_design"), None, tmp_path / "config"))
    page = client.get("/boards/demo").text
    assert "no-footprint" in page and "R1, R2" in page


def test_changes_between_scans(site, tmp_path):
    root, client = site
    first = next((root / "build/scans/demo").glob("*.json"))
    first.rename(first.with_name("aaaa.json"))
    (tmp_path / "demo/sub.kicad_sch").write_text(
        (tmp_path / "demo/sub.kicad_sch").read_text(encoding="utf-8").replace('"IPN" "RES-000123"', '"IPN" ""'),
        encoding="utf-8",
    )
    save(root, scan(root, BoardEntry("demo", tmp_path / "demo", "", "", "in_design"), None, tmp_path / "config"))
    page = client.get("/boards/demo").text
    changes = page[page.index("Changes between scans") :]
    assert "2 changed" in changes and "RES-000123" in changes


def test_the_index_follows_edits_to_part_files(site):
    root, client = site
    assert "RES-000124" not in client.get("/parts").text
    write(root, "parts/resistor/RES-000124.yaml", draft_resistor("RES-000124"))
    assert "RES-000124" in client.get("/parts").text


def test_where_used_says_so_when_nothing_uses_a_part(site):
    root, client = site
    write(root, "parts/resistor/RES-000124.yaml", draft_resistor("RES-000124"))
    assert "No scanned board uses RES-000124" in client.get("/parts/RES-000124").text


def test_unknown_part_and_board_are_404_pages(site):
    _root, client = site
    for url in ("/parts/NOPE", "/boards/NOPE"):
        response = client.get(url)
        assert response.status_code == 404 and "<html" in response.text


def test_the_dashboard_is_read_only(site):
    _root, client = site
    app_routes = [r for r in client.app.routes if getattr(r, "methods", None)]
    assert app_routes and all(r.methods <= {"GET", "HEAD"} for r in app_routes)


def test_page_text_is_escaped(site):
    root, client = site
    edit(root, RESISTOR, "description: 1 kΩ 1% 0.1 W thick film, 0603", 'description: "<script>alert(1)</script>"')
    page = client.get("/parts/RES-000123").text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


def test_serve_binds_only_to_localhost(site, monkeypatch):
    root, _client = site
    calls = {}
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.update(kwargs))
    assert main(["--root", str(root), "serve", "--port", "9999"]) == 0
    assert calls["host"] == "127.0.0.1" and calls["port"] == 9999
