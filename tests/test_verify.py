"""Automatic datasheet checks: Claude extracts, partslib compares, and only positive evidence passes.

The Claude API is replaced by a fake that returns fixed extractions, so no test spends money."""

import json
from pathlib import Path
import re
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from partslib import verify
from partslib.check import check_library
from partslib.cli import main
from partslib.web.app import create_app
from test_check import RESISTOR, edit, library, write  # noqa: F401 - library is a fixture
from test_edit import commits, git, last_commit_files, make_draft, repo  # noqa: F401 - repo is a fixture

PDF = b"%PDF-1.7 fake datasheet"


def facts(**overrides) -> dict:
    """What a correct reading of the fixture resistor's datasheet would return."""
    base = {
        "covers_part": True, "covers_part_page": 1, "covers_part_quote": "0603WAF1001T5E",
        "values": [
            {"param": "resistance_ohm", "stated": True, "value": "1kΩ", "page": 2, "quote": "1001 = 1kΩ"},
            {"param": "tolerance_pct", "stated": True, "value": "±1%", "page": 2, "quote": "F = ±1%"},
            {"param": "power_w", "stated": True, "value": "1/10W", "page": 3, "quote": "0603 1/10W"},
            {"param": "voltage_v", "stated": True, "value": "75V", "page": 3, "quote": "Max working voltage 75V"},
            {"param": "package", "stated": True, "value": "0603", "page": 1, "quote": "0603"},
            {"param": "technology", "stated": True, "value": "Thick film", "page": 1, "quote": "Thick Film Chip Resistors"},
        ],
        "pins": {"stated": False, "page": 0, "pins": []},
        "land_pattern": {"stated": True, "page": 5, "pads": [{"count": 2, "width_mm": 0.9, "length_mm": 0.9}],
                         "spacing_stated": True, "center_spacing_mm": 1.6, "pitch_stated": False, "pitch_mm": 0, "notes": ""},
    }
    base.update(overrides)
    return base


class FakeClaude:
    def __init__(self, answer: dict | None = None, stop_reason: str = "end_turn", model: str = verify.MODEL):
        self.requests = []
        self.answer, self.stop_reason, self.model = answer or facts(), stop_reason, model
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **request):
        self.requests.append(request)
        message = SimpleNamespace(stop_reason=self.stop_reason, model=self.model,
                                  content=[SimpleNamespace(type="text", text=json.dumps(self.answer))])

        class Stream:
            def __enter__(self):
                return SimpleNamespace(get_final_message=lambda: message)

            def __exit__(self, *exc):
                return False

        return Stream()


@pytest.fixture
def draft(repo, monkeypatch):
    make_draft(repo)
    monkeypatch.setattr(verify, "fetch_datasheet", lambda root, url: PDF)
    return repo


def outcomes(result) -> dict[str, str]:
    return {c.name: c.outcome for c in result.checks}


def test_a_part_that_matches_its_datasheet_is_released_with_its_evidence(draft):
    before = commits(draft)
    result = verify.verify(draft, "RES-000123", client=FakeClaude())
    assert result.passed and result.released
    assert commits(draft) == before + 1
    assert sorted(last_commit_files(draft)) == [RESISTOR, "verification/RES-000123.yaml"]
    part = yaml.safe_load((draft / RESISTOR).read_text(encoding="utf-8"))
    assert part["status"] == "released" and part["checked_by"].startswith("datasheet-check (claude-opus-5-5)")
    evidence = yaml.safe_load((draft / "verification/RES-000123.yaml").read_text(encoding="utf-8"))
    assert evidence["model"] == "claude-opus-5-5" and all(c["outcome"] == "pass" for c in evidence["checks"])
    assert check_library(draft, include_build_dir=False)[1] == []


def test_the_request_asks_for_structured_facts_with_fallback_and_caching(draft):
    claude = FakeClaude()
    verify.verify(draft, "RES-000123", client=claude, release=False)
    [request] = claude.requests
    assert request["model"] == "claude-opus-5-5"
    assert request["fallbacks"] == "default" and request["betas"] == ["server-side-fallback-2026-07-01"]
    assert request["output_config"]["format"]["type"] == "json_schema"
    document, question = request["messages"][0]["content"]
    assert document["type"] == "document" and document["cache_control"] == {"type": "ephemeral"}
    assert "0603WAF1001T5E" in question["text"] and "tolerance_pct" in question["text"]


def test_a_value_that_differs_is_a_mismatch_and_nothing_is_released(draft):
    wrong = facts(values=[*facts()["values"][:1], {"param": "tolerance_pct", "stated": True, "value": "5%", "page": 2, "quote": "J"}, *facts()["values"][2:]])
    before = commits(draft)
    result = verify.verify(draft, "RES-000123", client=FakeClaude(wrong))
    assert outcomes(result)["value tolerance_pct"] == "fail" and not result.released
    assert commits(draft) == before and "status: draft" in (draft / RESISTOR).read_text(encoding="utf-8")


def test_a_value_the_datasheet_does_not_state_is_unknown_and_blocks_release(draft):
    missing = facts(values=[v if v["param"] != "power_w" else {**v, "stated": False} for v in facts()["values"]])
    result = verify.verify(draft, "RES-000123", client=FakeClaude(missing))
    assert outcomes(result)["value power_w"] == "unknown" and not result.released


def test_no_land_pattern_means_the_footprint_cannot_be_confirmed(draft):
    result = verify.verify(draft, "RES-000123", client=FakeClaude(facts(land_pattern={**facts()["land_pattern"], "stated": False})))
    assert outcomes(result)["footprint"] == "unknown" and not result.released


@pytest.mark.parametrize("change, check", [
    ({"pads": [{"count": 2, "width_mm": 1.4, "length_mm": 1.6}]}, "footprint pad size"),
    ({"center_spacing_mm": 2.5}, "footprint pad spacing"),
    ({"pads": [{"count": 3, "width_mm": 0.9, "length_mm": 0.9}]}, "footprint pad count"),
])
def test_a_footprint_that_differs_from_the_land_pattern_is_a_mismatch(draft, change, check):
    result = verify.verify(draft, "RES-000123", client=FakeClaude(facts(land_pattern={**facts()["land_pattern"], **change})))
    assert outcomes(result)[check] == "fail" and not result.released


def test_a_datasheet_for_another_part_fails(draft):
    result = verify.verify(draft, "RES-000123", client=FakeClaude(facts(covers_part=False)))
    assert outcomes(result)["datasheet covers the MPN"] == "fail" and not result.released


def test_a_refusal_is_an_error_not_a_pass(draft):
    with pytest.raises(verify.VerifyError, match="declined"):
        verify.verify(draft, "RES-000123", client=FakeClaude(stop_reason="refusal"))


def test_a_released_part_is_not_checked_again(repo):
    with pytest.raises(verify.VerifyError, match="only drafts"):
        verify.verify(repo, "RES-000123", client=FakeClaude())


def test_pins_are_compared_for_parts_with_more_than_two():
    pins = {str(n) for n in range(1, 9)}
    good = {"pins": {"stated": True, "page": 4, "pins": [{"number": str(n), "name": f"P{n}"} for n in range(1, 9)]}}
    assert verify.compare_pins(pins, good).outcome == "pass"
    short = {"pins": {"stated": True, "page": 4, "pins": [{"number": str(n), "name": ""} for n in range(1, 8)]}}
    assert verify.compare_pins(pins, short).outcome == "fail"
    assert verify.compare_pins(pins, {"pins": {"stated": False, "page": 0, "pins": []}}).outcome == "unknown"
    assert verify.compare_pins({"1", "2"}, good) is None


def test_pitch_is_compared_for_multi_pin_footprints():
    pads = [verify.Pad(str(i + 1), i * 0.5, 0, 0.3, 1.5) for i in range(8)]
    land = {"stated": True, "page": 9, "pads": [{"count": 8, "width_mm": 0.3, "length_mm": 1.5}],
            "spacing_stated": False, "center_spacing_mm": 0, "pitch_stated": True, "pitch_mm": 0.5, "notes": ""}
    assert {c.name: c.outcome for c in verify.compare_footprint(pads, {"land_pattern": land})}["footprint pitch"] == "pass"
    wrong = {**land, "pitch_mm": 0.65}
    assert {c.name: c.outcome for c in verify.compare_footprint(pads, {"land_pattern": wrong})}["footprint pitch"] == "fail"


def test_an_lcsc_viewer_page_is_followed_to_the_pdf(tmp_path, monkeypatch):
    pages = {
        "https://www.lcsc.com/datasheet/x.pdf": b'<html><a href="https://datasheet.lcsc.com/datasheet/pdf/0a97.pdf?productCode=C22939">pdf</a></html>',
        "https://datasheet.lcsc.com/datasheet/pdf/0a97.pdf?productCode=C22939": PDF,
    }
    monkeypatch.setattr(verify, "_get", lambda url: pages[url])
    assert verify.fetch_datasheet(tmp_path, "https://www.lcsc.com/datasheet/x.pdf") == PDF
    monkeypatch.setattr(verify, "_get", lambda url: pytest.fail("cached, so not downloaded again"))
    assert verify.fetch_datasheet(tmp_path, "https://www.lcsc.com/datasheet/x.pdf") == PDF


def test_a_page_without_a_pdf_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "_get", lambda url: b"<html>no file here</html>")
    with pytest.raises(verify.VerifyError, match="web page"):
        verify.fetch_datasheet(tmp_path, "https://example.com/d")


def test_the_dashboard_button_checks_and_shows_the_evidence(draft, monkeypatch):
    claude = FakeClaude()
    monkeypatch.setattr("anthropic.Anthropic", lambda: claude)
    client = TestClient(create_app(draft, allowed_hosts=("testserver",)))
    page = client.get("/parts/RES-000123").text
    assert "Check datasheet automatically" in page
    token = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    result = client.post("/parts/RES-000123/verify", data={"csrf": token}).text
    assert "is released" in result and "p.5" in result
    assert "Checked by datasheet-check" in client.get("/parts/RES-000123").text


def test_cli_checks_every_draft_and_says_what_needs_you(draft, monkeypatch, capsys):
    monkeypatch.setattr("anthropic.Anthropic", lambda: FakeClaude(facts(covers_part=False)))
    assert main(["--root", str(draft), "verify"]) == 1
    out = capsys.readouterr().out
    assert "RES-000123: needs you" in out and "0 released, 1 need you" in out
