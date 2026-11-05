import json
from pathlib import Path

import pytest

from partslib.build import build_library
from partslib.install import InstallError, install, update_common, update_table
from test_check import RESISTOR, edit, library  # noqa: F401 - library is a fixture

OURS = {"PL_Resistor": "${PARTSLIB}/symbols/PL_Resistor.kicad_sym"}
EXISTING = """(sym_lib_table
\t(version 7)
\t(lib (name "KiCad") (type "Table") (uri "/x/template/sym-lib-table") (options "") (descr "KiCad Default Libraries"))
\t(lib (name "Mine") (type "KiCad") (uri "/home/me/Mine.kicad_sym") (options "") (descr "keep me"))
)
"""


def not_running():
    return []


def test_entries_are_added_and_everything_else_is_kept_byte_for_byte():
    updated = update_table(EXISTING, "sym_lib_table", OURS, None)
    assert updated.startswith(EXISTING.removesuffix(")\n"))
    assert '(lib (name "PL_Resistor") (type "KiCad") (uri "${PARTSLIB}/symbols/PL_Resistor.kicad_sym")' in updated
    assert updated.endswith(")\n")


def test_updating_twice_gives_the_same_table():
    once = update_table(EXISTING, "sym_lib_table", OURS, None)
    assert update_table(once, "sym_lib_table", OURS, None) == once


def test_libraries_no_longer_built_are_removed():
    with_two = update_table(EXISTING, "sym_lib_table", {**OURS, "PL_Old": "${PARTSLIB}/symbols/PL_Old.kicad_sym"}, None)
    assert update_table(with_two, "sym_lib_table", OURS, None) == update_table(EXISTING, "sym_lib_table", OURS, None)


def test_a_pl_library_partslib_did_not_create_is_refused():
    foreign = EXISTING.replace('(name "Mine")', '(name "PL_Resistor")')
    with pytest.raises(InstallError, match="did not create"):
        update_table(foreign, "sym_lib_table", OURS, None)


def test_a_new_table_includes_kicads_default_libraries(tmp_path):
    (tmp_path / "sym-lib-table").write_text("(sym_lib_table)\n", encoding="utf-8")
    table = update_table(None, "sym_lib_table", OURS, tmp_path)
    assert f'(lib (name "KiCad") (type "Table") (uri "{tmp_path / "sym-lib-table"}")' in table
    assert '(name "PL_Resistor")' in table


def test_partslib_variable_is_set_when_kicad_wrote_null_vars():
    text, previous = update_common('{"environment": {"vars": null}, "other": 1}', Path("/lib/build"))
    data = json.loads(text)
    assert data == {"environment": {"vars": {"PARTSLIB": "/lib/build"}}, "other": 1}
    assert previous is None


def test_install_writes_tables_and_variable_then_changes_nothing_on_a_second_run(library, tmp_path):
    build_library(library)
    config = tmp_path / "config"
    first = install(library, config, kicad_processes=not_running)
    assert first.backups == [], "nothing existed to back up"
    snapshot = {p.name: p.read_bytes() for p in config.iterdir()}
    assert '(name "PL_Resistor")' in snapshot["sym-lib-table"].decode()
    assert '(name "PL_Resistor")' in snapshot["fp-lib-table"].decode()
    assert json.loads(snapshot["kicad_common.json"])["environment"]["vars"]["PARTSLIB"] == str(
        (library / "build").resolve()
    )
    second = install(library, config, kicad_processes=not_running)
    assert second.backups == []
    assert {p.name: p.read_bytes() for p in config.iterdir()} == snapshot


def test_install_backs_up_files_it_changes(library, tmp_path):
    build_library(library)
    config = tmp_path / "config"
    config.mkdir()
    (config / "sym-lib-table").write_text(EXISTING, encoding="utf-8")
    result = install(library, config, kicad_processes=not_running)
    backups = [b.name for b in result.backups]
    assert len(backups) == 1 and backups[0].startswith("sym-lib-table.pre-partslib-")
    assert (config / backups[0]).read_text(encoding="utf-8") == EXISTING


def test_install_refuses_while_kicad_is_running(library, tmp_path):
    build_library(library)
    with pytest.raises(InstallError, match="close KiCad"):
        install(library, tmp_path / "config", kicad_processes=lambda: ["kicad"])
    assert not (tmp_path / "config").exists()


def test_install_refuses_without_a_build(library, tmp_path):
    with pytest.raises(InstallError, match="partslib build"):
        install(library, tmp_path / "config", kicad_processes=not_running)


def test_install_refuses_a_stale_build(library, tmp_path):
    build_library(library)
    edit(library, RESISTOR, "description: 1 kΩ", "description: One kΩ")
    with pytest.raises(InstallError, match="problems"):
        install(library, tmp_path / "config", kicad_processes=not_running)
    assert not (tmp_path / "config").exists()
