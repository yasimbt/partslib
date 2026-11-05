"""`partslib install`: register the generated libraries with KiCad and set ${PARTSLIB} (spec section 6).

Only entries this tool owns are touched: library-table entries named PL_* whose URI starts with
${PARTSLIB}/. Every file is backed up before it changes, and nothing runs while KiCad is open.
"""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from partslib import kicadgen
from partslib.build import BUILD_DIR, read_manifest
from partslib.check import check_library

KICAD_APPS = ("kicad", "eeschema", "pcbnew")
OWNED_URI_PREFIX = "${PARTSLIB}/"
TABLES = {"sym-lib-table": "sym_lib_table", "fp-lib-table": "fp_lib_table"}
TEMPLATE_DIRS = (
    Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/template"),
    Path("/usr/share/kicad/template"),
)
MAC_KICAD_CLI = Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")


class InstallError(Exception):
    pass


@dataclass(frozen=True)
class InstallResult:
    config_dir: Path
    partslib: Path
    libraries: dict[str, list[str]]  # table file -> library names
    backups: list[Path]
    previous_partslib: str | None


def running_kicad() -> list[str]:
    running = []
    for name in KICAD_APPS:
        result = subprocess.run(["pgrep", "-x", name], capture_output=True, check=False)
        if result.returncode == 0:
            running.append(name)
    return running


def kicad_version() -> str:
    """Return KiCad's major.minor version, which names its config directory."""
    cli = shutil.which("kicad-cli") or (str(MAC_KICAD_CLI) if MAC_KICAD_CLI.exists() else None)
    if cli is None:
        raise InstallError("kicad-cli not found; pass --config-dir")
    output = subprocess.run([cli, "version"], capture_output=True, text=True, check=False).stdout
    match = re.search(r"(\d+)\.(\d+)", output)
    if match is None:
        raise InstallError(f"cannot read KiCad version from {output!r}")
    return f"{match.group(1)}.{match.group(2)}"


def default_config_dir() -> Path:
    version = kicad_version()
    if os.environ.get("KICAD_CONFIG_HOME"):
        return Path(os.environ["KICAD_CONFIG_HOME"]) / version
    if sys.platform == "darwin":
        return Path.home() / "Library/Preferences/kicad" / version
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "kicad" / version


def _entry(name: str, uri: str) -> str:
    return (
        f"(lib (name {kicadgen.quote(name)}) (type \"KiCad\") (uri {kicadgen.quote(uri)}) "
        '(options "") (descr "partslib"))'
    )


def update_table(text: str | None, kind: str, libraries: dict[str, str], template: Path | None) -> str:
    """Return a library table with exactly `libraries` (name -> URI) as its partslib entries.

    Other entries are kept byte for byte. A new table also gets KiCad's default libraries, as
    KiCad itself would create on first run.
    """
    if text is None:
        default = ""
        if template is not None and (template / kind.replace("_", "-")).is_file():
            default = "\t" + _entry_raw("KiCad", "Table", template / kind.replace("_", "-")) + "\n"
        text = f"({kind}\n\t(version 7)\n{default})\n"
    if not text.lstrip().startswith(f"({kind}"):
        raise InstallError(f"not a {kind} file")
    start = text.index("(")
    end = kicadgen.block_end(text, start)
    table = text[start:end]

    removals = []
    for child_start, child_end in kicadgen.child_spans(table):
        child = table[child_start:child_end]
        if not child.startswith("(lib"):
            continue
        name = re.search(r'\(name "((?:[^"\\]|\\.)*)"', child)
        uri = re.search(r'\(uri "((?:[^"\\]|\\.)*)"', child)
        if name is None or not name.group(1).startswith("PL_"):
            continue
        if uri is None or not uri.group(1).startswith(OWNED_URI_PREFIX):
            raise InstallError(
                f"{kind} already has a library named {name.group(1)} that partslib did not create; "
                "rename or remove it first"
            )
        line_start = table.rfind("\n", 0, child_start)
        removals.append((line_start if line_start >= 0 else child_start, child_end))
    for removal_start, removal_end in reversed(removals):
        table = table[:removal_start] + table[removal_end:]

    close = table.rindex(")")
    body = table[:close].rstrip("\t ")
    if not body.endswith("\n"):
        body += "\n"
    body += "".join(f"\t{_entry(name, uri)}\n" for name, uri in sorted(libraries.items()))
    return text[:start] + body + ")" + text[end:]


def _entry_raw(name: str, kind: str, uri: Path) -> str:
    return (
        f"(lib (name {kicadgen.quote(name)}) (type {kicadgen.quote(kind)}) "
        f'(uri {kicadgen.quote(str(uri))}) (options "") (descr "KiCad Default Libraries"))'
    )


def update_common(text: str | None, partslib: Path) -> tuple[str, str | None]:
    """Return kicad_common.json with ${PARTSLIB} set, and the value it had before."""
    data = json.loads(text) if text else {}
    if not isinstance(data, dict):
        raise InstallError("kicad_common.json is not a JSON object")
    environment = data.setdefault("environment", {})
    if not isinstance(environment.get("vars"), dict):
        environment["vars"] = {}  # KiCad writes null when no variables are set
    previous = environment["vars"].get("PARTSLIB")
    environment["vars"]["PARTSLIB"] = str(partslib)
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n", previous


def generated_libraries(root: Path) -> dict[str, dict[str, str]]:
    manifest = read_manifest(root)
    if not manifest:
        raise InstallError("nothing to install; run `partslib build` first")
    symbols, footprints = {}, {}
    for path in manifest:
        parts = path.split("/")
        if parts[0] == "symbols" and path.endswith(".kicad_sym"):
            name = parts[1].removesuffix(".kicad_sym")
            symbols[name] = f"{OWNED_URI_PREFIX}symbols/{parts[1]}"
        elif parts[0] == "footprints" and parts[1].endswith(".pretty"):
            name = parts[1].removesuffix(".pretty")
            footprints[name] = f"{OWNED_URI_PREFIX}footprints/{parts[1]}"
    return {"sym-lib-table": symbols, "fp-lib-table": footprints}


def _write_atomically(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.partslib-tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def install(root: Path, config_dir: Path | None = None, kicad_processes=running_kicad) -> InstallResult:
    running = kicad_processes()
    if running:
        raise InstallError(f"close KiCad first ({', '.join(running)} running): it rewrites its config on exit")
    _count, findings = check_library(root)
    if findings:
        raise InstallError(f"`partslib check` reports {len(findings)} problems; fix them and rebuild first")
    libraries = generated_libraries(root)
    config_dir = config_dir or default_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    template = next((d for d in TEMPLATE_DIRS if d.is_dir()), None)
    partslib = (root / BUILD_DIR).resolve()

    files = {name: config_dir / name for name in (*TABLES, "kicad_common.json")}
    current = {name: (p.read_text(encoding="utf-8") if p.is_file() else None) for name, p in files.items()}
    updated = {
        name: update_table(current[name], kind, libraries[name], template) for name, kind in TABLES.items()
    }
    updated["kicad_common.json"], previous = update_common(current["kicad_common.json"], partslib)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backups = []
    for name, path in files.items():
        if current[name] is not None and current[name] != updated[name]:
            backup = path.with_name(f"{path.name}.pre-partslib-{stamp}")
            shutil.copy2(path, backup)
            backups.append(backup)
    for name, path in files.items():
        if current[name] != updated[name]:
            _write_atomically(path, updated[name])
    return InstallResult(
        config_dir,
        partslib,
        {name: sorted(libs) for name, libs in libraries.items()},
        backups,
        previous,
    )
