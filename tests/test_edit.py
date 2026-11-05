"""Editing in the dashboard (spec section 8). M5 acceptance: a part added in the browser produces one
git commit and passes check."""

from datetime import date
from pathlib import Path
import re
import subprocess

import pytest
import yaml
from fastapi.testclient import TestClient

from partslib.check import check_library
from partslib.web.app import create_app
from test_check import RESISTOR, edit, library, write  # noqa: F401 - library is a fixture


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(root), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def repo(library):
    git(library, "init", "-q")
    git(library, "config", "user.name", "Test")
    git(library, "config", "user.email", "test@example.com")
    git(library, "add", "-A")
    git(library, "commit", "-qm", "base")
    return library


@pytest.fixture
def client(repo):
    client = TestClient(create_app(repo, allowed_hosts=("testserver",)), follow_redirects=False)
    token = re.search(r'name="csrf" value="([^"]+)"', client.get("/parts/new").text).group(1)
    client.token = token
    return client


def commits(root: Path) -> int:
    return int(git(root, "rev-list", "--count", "HEAD"))


def last_commit_files(root: Path) -> list[str]:
    return git(root, "show", "--name-only", "--format=", "HEAD").splitlines()


def form(client, **values) -> dict:
    return {"csrf": client.token, **values}


def resistor(client, resistance="4k7", tolerance="1%", **extra) -> dict:
    return form(
        client,
        category="resistor",
        **{
            "param.resistance_ohm": resistance,
            "param.tolerance_pct": tolerance,
            "param.power_w": "0.1W",
            "param.package": "0603",
            "param.technology": "thick_film",
            "mpn.0.manufacturer": "UNI-ROYAL",
            "mpn.0.mpn": f"TEST-{resistance}-{tolerance}",
            "mpn.0.lcsc": "",
        },
        **extra,
    )


def test_acceptance_a_part_added_in_the_browser_is_one_commit_and_passes_check(repo, client):
    before = commits(repo)
    response = client.post("/parts/new", data=resistor(client))
    assert response.status_code == 303
    assert response.headers["location"].startswith("/parts/RES-000124?saved=")
    assert commits(repo) == before + 1
    assert last_commit_files(repo) == ["parts/resistor/RES-000124.yaml"]
    assert check_library(repo)[1] == []
    page = client.get(response.headers["location"]).text
    assert "Saved and committed" in page


def test_new_part_gets_defaults_a_suggested_description_and_plain_values(repo, client):
    client.post("/parts/new", data=resistor(client))
    part = yaml.safe_load((repo / "parts/resistor/RES-000124.yaml").read_text(encoding="utf-8"))
    assert part["status"] == "draft"
    assert part["description"] == "4.7kΩ 1% 100mW thick film, 0603"
    assert part["params"]["resistance_ohm"] == 4700
    assert part["symbol"] == "Device:R"
    assert part["footprint"] == "Resistor_SMD:R_0603_1608Metric"
    assert part["model"] == "Resistor_SMD/R_0603_1608Metric.step"


def test_a_duplicate_is_refused_naming_the_existing_part(repo, client):
    before = commits(repo)
    response = client.post("/parts/new", data=resistor(client, resistance="1K0"))
    assert response.status_code == 422
    assert "duplicate-part" in response.text and "RES-000123" in response.text
    assert commits(repo) == before
    assert not (repo / "parts/resistor/RES-000124.yaml").exists()


def test_the_live_check_shows_existing_matches_first(repo, client):
    page = client.post("/parts/new/check", data=resistor(client, resistance="1000")).text
    assert "Already in the library" in page and 'href="/parts/RES-000123"' in page
    clean = client.post("/parts/new/check", data=resistor(client)).text
    assert "No duplicates" in clean and "RES-000124" in clean


def test_a_near_match_needs_acknowledging(repo, client):
    near = resistor(client, resistance="1k", tolerance="5%")
    assert 'name="ack" value="RES-000123"' in client.post("/parts/new/check", data=near).text
    assert client.post("/parts/new", data=near).status_code == 422
    assert client.post("/parts/new", data={**near, "ack": "RES-000123"}).status_code == 303
    assert check_library(repo)[1] == []


def test_only_the_new_file_is_committed_and_other_staged_changes_stay_staged(repo, client):
    write(repo, "notes.txt", "unrelated work in progress\n")
    git(repo, "add", "notes.txt")
    client.post("/parts/new", data=resistor(client))
    assert last_commit_files(repo) == ["parts/resistor/RES-000124.yaml"]
    assert "notes.txt" in git(repo, "diff", "--cached", "--name-only")


def make_draft(repo: Path) -> None:
    edit(repo, RESISTOR, "status: released", "status: draft")
    edit(repo, RESISTOR, "footprint_checked: 2026-11-04\n", "")
    edit(repo, RESISTOR, "    verified: 2026-11-04\n", "")
    edit(repo, RESISTOR, "lifecycle: active", "lifecycle: unknown")
    git(repo, "commit", "-qam", "make a draft")


CONFIRMED = {"confirm.values": "yes", "confirm.footprint": "yes", "confirm.mpn": "yes", "lifecycle": "active"}


def test_release_records_the_check_and_commits(repo, client):
    make_draft(repo)
    before = commits(repo)
    response = client.post("/parts/RES-000123/release", data=form(client, **CONFIRMED))
    assert response.status_code == 303
    part = yaml.safe_load((repo / RESISTOR).read_text(encoding="utf-8"))
    assert part["status"] == "released"
    assert part["footprint_checked"] == date.today()
    assert part["mpns"][0]["verified"] == date.today()
    assert part["mpns"][0]["lifecycle"] == "active"
    assert commits(repo) == before + 1 and last_commit_files(repo) == [RESISTOR]
    assert check_library(repo)[1] == []
    text = (repo / RESISTOR).read_text(encoding="utf-8")
    assert "&" not in text and "*" not in text, "no YAML anchors in a file people edit by hand"
    assert text.count(f"{date.today().isoformat()}") == 2


def test_release_needs_every_confirmation(repo, client):
    make_draft(repo)
    before = commits(repo)
    data = form(client, **{**CONFIRMED, "confirm.footprint": ""})
    assert client.post("/parts/RES-000123/release", data=data).status_code == 422
    assert commits(repo) == before
    assert "status: draft" in (repo / RESISTOR).read_text(encoding="utf-8")


def test_release_is_refused_when_an_asset_is_missing(repo, client):
    make_draft(repo)
    (repo / "assets/3dmodels/Resistor/R_0603.step").unlink()
    git(repo, "commit", "-qam", "lose the model")
    before = commits(repo)
    response = client.post("/parts/RES-000123/release", data=form(client, **CONFIRMED))
    assert response.status_code == 422 and "asset-missing" in response.text
    assert commits(repo) == before
    assert "status: draft" in (repo / RESISTOR).read_text(encoding="utf-8")


def test_release_removes_the_import_note(repo, client):
    make_draft(repo)
    edit(repo, RESISTOR, 'notes: ""', "notes: 'Imported from the board. Not yet checked against the datasheet: set it.'")
    git(repo, "commit", "-qam", "note")
    client.post("/parts/RES-000123/release", data=form(client, **CONFIRMED))
    assert yaml.safe_load((repo / RESISTOR).read_text(encoding="utf-8"))["notes"] == "Imported from the board."


def edit_form(client, ipn="RES-000123", **changes) -> dict:
    page = client.get(f"/parts/{ipn}/edit").text
    values = dict(re.findall(r'name="((?:mpn\.\d+\.\w+)|description|datasheet|param\.\w+)" value="([^"]*)"', page))
    values.update({f"mpn.{i}.lifecycle": v for i, v in re.findall(r'name="mpn\.(\d+)\.lifecycle">.*?<option selected>(\w+)', page)})
    return form(client, **{**values, **changes})


def test_editing_a_description_is_one_commit(repo, client):
    before = commits(repo)
    assert client.post("/parts/RES-000123/edit", data=edit_form(client, description="1 kΩ precision")).status_code == 303
    assert "description: 1 kΩ precision" in (repo / RESISTOR).read_text(encoding="utf-8")
    assert commits(repo) == before + 1 and check_library(repo)[1] == []


def test_a_released_parts_params_cannot_change(repo, client):
    response = client.post("/parts/RES-000123/edit", data=edit_form(client, **{"param.tolerance_pct": "5"}))
    assert response.status_code == 422 and "can no longer change" in response.text


def test_changing_the_only_verified_mpn_of_a_released_part_is_refused(repo, client):
    response = client.post("/parts/RES-000123/edit", data=edit_form(client, **{"mpn.0.mpn": "SOMETHING-ELSE"}))
    assert response.status_code == 422 and "released-incomplete" in response.text


def test_a_released_part_can_be_deprecated(repo, client):
    assert client.post("/parts/RES-000123/edit", data=edit_form(client, status="deprecated")).status_code == 303
    assert "status: deprecated" in (repo / RESISTOR).read_text(encoding="utf-8")


def test_posts_without_the_token_are_refused(repo, client):
    data = resistor(client)
    data["csrf"] = "wrong"
    assert client.post("/parts/new", data=data).status_code == 403
    assert not (repo / "parts/resistor/RES-000124.yaml").exists()


def test_cross_site_posts_are_refused(repo, client):
    response = client.post("/parts/new", data=resistor(client), headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_other_host_names_are_refused(repo, client):
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403


def test_the_real_server_only_allows_local_host_names(repo):
    local = TestClient(create_app(repo), base_url="http://127.0.0.1:8765")
    assert local.get("/").status_code == 200
    assert TestClient(create_app(repo)).get("/").status_code == 403  # Host: testserver
