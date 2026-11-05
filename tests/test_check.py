"""Every rule in check.RULES needs a case here that makes it fire (spec section 12, M1)."""

import copy
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from partslib.check import RULES, check_library
from partslib.cli import main

REPO = Path(__file__).resolve().parents[1]

GOOD_RESISTOR = """\
ipn: RES-000123
status: released
category: resistor
description: 1 kΩ 1% 0.1 W thick film, 0603

params:
  resistance_ohm: 1000
  tolerance_pct: 1
  power_w: 0.1
  voltage_v: 75
  package: "0603"
  technology: thick_film

symbol: Resistor:R
footprint: Resistor:R_0603
footprint_checked: 2026-11-04
model: Resistor/R_0603.step
datasheet: https://example.com/datasheet.pdf

mpns:
  - manufacturer: UNI-ROYAL
    mpn: 0603WAF1001T5E
    lcsc: C21190
    lifecycle: active
    verified: 2026-11-04

supersedes: null
superseded_by: null
notes: ""
"""
PROVENANCE = """\
- {kind: symbol, id: "Resistor:R", source: KiCad, licence: CC-BY-SA-4.0, version: "10.0.8"}
- {kind: footprint, id: "Resistor:R_0603", source: KiCad, licence: CC-BY-SA-4.0, version: "10.0.8"}
- {kind: model, id: "Resistor/R_0603.step", source: KiCad, licence: CC-BY-SA-4.0, version: "10.0.8"}
"""
SYMBOL_LIBRARY = """\
(kicad_symbol_lib
\t(symbol "R"
\t\t(symbol "R_1_1"
\t\t\t(pin passive line (at 0 3.81 270) (length 1.27) (name "") (number "1"))
\t\t\t(pin passive line (at 0 -3.81 90) (length 1.27) (name "") (number "2"))
\t\t)
\t)
)
"""
FOOTPRINT = """\
(footprint "R_0603"
\t(pad "1" smd roundrect (at -0.8 0) (size 0.9 0.9) (layers "F.Cu"))
\t(pad "2" smd roundrect (at 0.8 0) (size 0.9 0.9) (layers "F.Cu"))
)
"""


def write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def edit(root: Path, relative: str, old: str, new: str) -> None:
    path = root / relative
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} is not in {relative}"
    path.write_text(text.replace(old, new), encoding="utf-8")


RESISTOR = "parts/resistor/RES-000123.yaml"


def draft_resistor(ipn: str, **params: object) -> str:
    values = {
        "resistance_ohm": 2000,
        "tolerance_pct": 1,
        "power_w": 0.1,
        "package": "0603",
        "technology": "thick_film",
        **params,
    }
    return yaml.safe_dump(
        {"ipn": ipn, "status": "draft", "category": "resistor", "description": "x", "params": values},
        allow_unicode=True,
    )


@pytest.fixture
def library(tmp_path: Path) -> Path:
    shutil.copytree(REPO / "categories", tmp_path / "categories")
    write(tmp_path, RESISTOR, GOOD_RESISTOR)
    write(tmp_path, "assets/provenance.yaml", PROVENANCE)
    write(tmp_path, "assets/symbols/Resistor.kicad_sym", SYMBOL_LIBRARY)
    write(tmp_path, "assets/footprints/Resistor.pretty/R_0603.kicad_mod", FOOTPRINT)
    write(tmp_path, "assets/3dmodels/Resistor/R_0603.step", "ISO-10303-21;\n")
    return tmp_path


def git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(root), *args],
        check=True,
        capture_output=True,
    )


def commit_all(root: Path) -> None:
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")


def codes(root: Path) -> set[str]:
    return {finding.code for finding in check_library(root)[1]}


def test_the_spec_example_passes(library):
    count, findings = check_library(library)
    assert (count, findings) == (1, [])


def test_the_repository_itself_passes():
    assert check_library(REPO, since="HEAD")[1] == []


def test_an_empty_library_passes(library):
    (library / RESISTOR).unlink()
    assert check_library(library) == (0, [])


# --- One function per rule. Each breaks the good library so that its rule fires. ---


def yaml_invalid(root):
    write(root, RESISTOR, "ipn: [unclosed\n")


def yaml_duplicate_key(root):
    edit(root, RESISTOR, "notes: \"\"", "notes: \"\"\nstatus: draft")


def yaml_not_utf8(root):
    (root / RESISTOR).write_bytes(b"ipn: \xff\xfe\n")


def yaml_unhashable_key(root):
    write(root, RESISTOR, "? [a]\n: 1\n")


def yaml_impossible_date(root):
    edit(root, RESISTOR, "verified: 2026-11-04", "verified: 2026-13-45")


def not_mapping(root):
    write(root, RESISTOR, "- a\n- b\n")


def unknown_key(root):
    edit(root, RESISTOR, "superseded_by: null", "superseeded_by: null")


def missing_key(root):
    edit(root, RESISTOR, "description: 1 kΩ 1% 0.1 W thick film, 0603\n", "")


def ipn_format(root):
    edit(root, RESISTOR, "ipn: RES-000123", "ipn: RES-123")


def ipn_prefix(root):
    edit(root, RESISTOR, "ipn: RES-000123", "ipn: CAP-000123")


def path_mismatch(root):
    shutil.move(root / RESISTOR, root / "parts/resistor/RES-000124.yaml")


def duplicate_ipn(root):
    write(root, "parts/capacitor/RES-000123.yaml", (root / RESISTOR).read_text(encoding="utf-8"))


def unknown_category(root):
    edit(root, RESISTOR, "category: resistor", "category: resistors")


def status_invalid(root):
    edit(root, RESISTOR, "status: released", "status: approved")


def param_unknown(root):
    edit(root, RESISTOR, "voltage_v: 75", "voltage: 75")


def param_missing(root):
    edit(root, RESISTOR, "  tolerance_pct: 1\n", "")


def param_invalid_value(root):
    edit(root, RESISTOR, "resistance_ohm: 1000", "resistance_ohm: 10pF")


def param_invalid_octal_package(root):
    # An unquoted 0603 is the octal integer 387 in YAML.
    edit(root, RESISTOR, 'package: "0603"', "package: 0603")


def param_invalid_choice(root):
    edit(root, RESISTOR, "technology: thick_film", "technology: magic")


def mpn_invalid_entry(root):
    edit(root, RESISTOR, "manufacturer: UNI-ROYAL\n", "maker: UNI-ROYAL\n")


def mpn_invalid_lifecycle(root):
    edit(root, RESISTOR, "lifecycle: active", "lifecycle: fine")


def mpn_required(root):
    write(
        root,
        "parts/ic/IC-000001.yaml",
        "ipn: IC-000001\nstatus: draft\ncategory: ic\ndescription: x\nparams: {}\n",
    )


def duplicate_mpn(root):
    write(
        root,
        "parts/ic/IC-000001.yaml",
        "ipn: IC-000001\nstatus: draft\ncategory: ic\ndescription: x\nparams: {}\n"
        "mpns: [{manufacturer: ACME, mpn: 0603waf1001t5e}]\n",
    )


def duplicate_part(root):
    write(root, "parts/resistor/RES-000124.yaml", draft_resistor("RES-000124", resistance_ohm="1K0"))


def near_match(root):
    write(
        root,
        "parts/resistor/RES-000124.yaml",
        draft_resistor("RES-000124", resistance_ohm="1k", tolerance_pct=5),
    )


def ack_invalid(root):
    edit(root, RESISTOR, 'notes: ""', 'notes: ""\nnear_match_ack: [RES-999999]')


def asset_ref_invalid(root):
    edit(root, RESISTOR, "symbol: Resistor:R", "symbol: R")


def asset_ref_model_escapes(root):
    edit(root, RESISTOR, "model: Resistor/R_0603.step", "model: ../../secret.step")


def released_incomplete_datasheet(root):
    edit(root, RESISTOR, "datasheet: https://example.com/datasheet.pdf\n", "")


def released_incomplete_footprint_check(root):
    edit(root, RESISTOR, "footprint_checked: 2026-11-04\n", "")


def released_incomplete_unverified_mpn(root):
    edit(root, RESISTOR, "    verified: 2026-11-04\n", "")


def asset_missing_symbol(root):
    edit(root, RESISTOR, "symbol: Resistor:R", "symbol: Resistor:R_Missing")


def asset_missing_footprint(root):
    (root / "assets/footprints/Resistor.pretty/R_0603.kicad_mod").unlink()


def asset_missing_model(root):
    (root / "assets/3dmodels/Resistor/R_0603.step").unlink()


def pin_pad_mismatch(root):
    edit(root, "assets/footprints/Resistor.pretty/R_0603.kicad_mod", '(pad "2"', '(pad "3"')


def provenance_invalid(root):
    write(root, "assets/provenance.yaml", PROVENANCE + '- {kind: symbol, id: "x:y"}\n')


def provenance_missing(root):
    write(root, "assets/provenance.yaml", PROVENANCE.splitlines(True)[0] + PROVENANCE.splitlines(True)[1])


def supersession_unknown(root):
    edit(root, RESISTOR, "supersedes: null", "supersedes: RES-999999")


def supersession_not_retired(root):
    write(
        root,
        "parts/resistor/RES-000124.yaml",
        draft_resistor("RES-000124") + "supersedes: RES-000123\n",
    )
    edit(root, RESISTOR, "superseded_by: null", "superseded_by: RES-000124")


def history_deleted(root):
    commit_all(root)
    (root / RESISTOR).unlink()


def history_identity(root):
    commit_all(root)
    edit(root, RESISTOR, "tolerance_pct: 1", "tolerance_pct: 5")


def history_status(root):
    commit_all(root)
    edit(root, RESISTOR, "status: released", "status: draft")


CASES = {
    "yaml-invalid": [yaml_invalid, yaml_duplicate_key, yaml_not_utf8, yaml_unhashable_key, yaml_impossible_date],
    "not-mapping": [not_mapping],
    "unknown-key": [unknown_key],
    "missing-key": [missing_key],
    "ipn-format": [ipn_format],
    "ipn-prefix": [ipn_prefix],
    "path-mismatch": [path_mismatch],
    "duplicate-ipn": [duplicate_ipn],
    "unknown-category": [unknown_category],
    "status-invalid": [status_invalid],
    "param-unknown": [param_unknown],
    "param-missing": [param_missing],
    "param-invalid": [param_invalid_value, param_invalid_octal_package, param_invalid_choice],
    "mpn-invalid": [mpn_invalid_entry, mpn_invalid_lifecycle],
    "mpn-required": [mpn_required],
    "duplicate-mpn": [duplicate_mpn],
    "duplicate-part": [duplicate_part],
    "near-match": [near_match],
    "ack-invalid": [ack_invalid],
    "asset-ref-invalid": [asset_ref_invalid, asset_ref_model_escapes],
    "released-incomplete": [
        released_incomplete_datasheet,
        released_incomplete_footprint_check,
        released_incomplete_unverified_mpn,
    ],
    "asset-missing": [asset_missing_symbol, asset_missing_footprint, asset_missing_model],
    "pin-pad-mismatch": [pin_pad_mismatch],
    "provenance-invalid": [provenance_invalid],
    "provenance-missing": [provenance_missing],
    "supersession-invalid": [supersession_unknown, supersession_not_retired],
    "history-deleted": [history_deleted],
    "history-identity": [history_identity],
    "history-status": [history_status],
}


def test_every_rule_has_a_failing_case():
    assert set(CASES) == set(RULES)


@pytest.mark.parametrize(
    "code, breaker",
    [(code, breaker) for code, breakers in CASES.items() for breaker in breakers],
    ids=lambda value: value if isinstance(value, str) else value.__name__,
)
def test_rule_fires(library, code, breaker):
    breaker(library)
    assert code in codes(library)
    assert main(["--root", str(library), "check"]) == 1


# --- Behaviour around the rules. ---


@pytest.mark.parametrize("written", ["1k", "1000", "1.0 kΩ", "1K0"])
def test_spellings_of_the_same_value_are_duplicates(library, written):
    write(library, "parts/resistor/RES-000124.yaml", draft_resistor("RES-000124", resistance_ohm=written))
    findings = check_library(library)[1]
    assert [f.code for f in findings] == ["duplicate-part"]
    assert "RES-000123" in findings[0].message


def test_a_different_value_is_not_a_duplicate(library):
    write(library, "parts/resistor/RES-000124.yaml", draft_resistor("RES-000124", resistance_ohm="1k2"))
    assert check_library(library)[1] == []


def test_np0_and_c0g_are_the_same_dielectric(library):
    for ipn, dielectric in (("CAP-000001", "C0G"), ("CAP-000002", "NP0")):
        write(
            library,
            f"parts/capacitor/{ipn}.yaml",
            yaml.safe_dump(
                {
                    "ipn": ipn,
                    "status": "draft",
                    "category": "capacitor",
                    "description": "x",
                    "params": {
                        "capacitance_f": "100n",
                        "tolerance_pct": 5,
                        "voltage_v": 50,
                        "dielectric": dielectric,
                        "package": "0603",
                    },
                }
            ),
        )
    assert codes(library) == {"duplicate-part"}


def test_acknowledging_a_near_match_clears_it(library):
    near_match(library)
    edit(library, "parts/resistor/RES-000124.yaml", "ipn: RES-000124", "ipn: RES-000124\nnear_match_ack: [RES-000123]")
    assert check_library(library)[1] == []


def test_a_replacement_may_share_identity_and_mpn_with_the_part_it_supersedes(library):
    edit(library, RESISTOR, "status: released", "status: deprecated")
    edit(library, RESISTOR, "superseded_by: null", "superseded_by: RES-000124")
    text = (library / RESISTOR).read_text(encoding="utf-8")
    text = (
        text.replace("ipn: RES-000123", "ipn: RES-000124")
        .replace("status: deprecated", "status: released")
        .replace("superseded_by: RES-000124", "superseded_by: null")
        .replace("supersedes: null", "supersedes: RES-000123")
    )
    write(library, "parts/resistor/RES-000124.yaml", text)
    assert check_library(library)[1] == []


def test_a_draft_may_change_identity(library):
    write(library, "parts/resistor/RES-000124.yaml", draft_resistor("RES-000124"))
    commit_all(library)
    edit(library, "parts/resistor/RES-000124.yaml", "tolerance_pct: 1", "tolerance_pct: 2")
    assert check_library(library)[1] == []


def test_respelling_a_released_value_is_not_an_identity_change(library):
    commit_all(library)
    edit(library, RESISTOR, "resistance_ohm: 1000", "resistance_ohm: 1K0")
    assert check_library(library)[1] == []


def test_moving_forward_through_the_lifecycle_is_allowed(library):
    commit_all(library)
    edit(library, RESISTOR, "status: released", "status: deprecated")
    assert check_library(library)[1] == []


WEIRD_VALUES = [None, 1, 1.5, True, [], {}, ["a"], [["a"]], [{"a": 1}], {"a": 1}, ""]


def _value_paths(value, prefix=()):
    yield prefix
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _value_paths(child, (*prefix, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _value_paths(child, (*prefix, index))


def test_no_value_in_any_field_crashes_check(library):
    """A crash would hide every other problem in the library, so hand edits must only ever report."""
    good = yaml.safe_load(GOOD_RESISTOR)
    targets = [p for p in _value_paths(good) if p] + [("near_match_ack",), ("footprint_checked",)]
    for target in targets:
        for weird in WEIRD_VALUES:
            part = copy.deepcopy(good)
            container = part
            for key in target[:-1]:
                container = container[key]
            container[target[-1]] = weird
            write(library, RESISTOR, yaml.safe_dump(part, allow_unicode=True))
            check_library(library)


def test_an_unknown_revision_is_a_usage_error(library, capsys):
    commit_all(library)
    assert main(["--root", str(library), "check", "--since", "no-such-rev"]) == 2
    assert "no-such-rev" in capsys.readouterr().err
