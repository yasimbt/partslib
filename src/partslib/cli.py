"""partslib: a personal, single-source KiCad parts library. See spec.md."""

import argparse
from pathlib import Path
import sys

from partslib.add import add_part
from partslib.board import BoardError, load_boards
from partslib.build import BuildError, build_library
from partslib.check import check_library
from partslib.install import InstallError, install
from partslib.library import LibraryError
from partslib.scan import format_result, save, scan
from partslib.vendor import VendorError

# Commands not built yet -> (help text, milestone that delivers them).
PLANNED: dict[str, tuple[str, str]] = {}


def _key_value(text: str) -> tuple[str, str]:
    key, separator, value = text.partition("=")
    if not separator or not key or not value:
        raise argparse.ArgumentTypeError(f"{text!r} is not name=value")
    return key, value


def _manufacturer_mpn(text: str) -> tuple[str, str]:
    manufacturer, separator, mpn = text.partition(":")
    if not separator or not manufacturer or not mpn:
        raise argparse.ArgumentTypeError(f"{text!r} is not MANUFACTURER:MPN")
    return manufacturer, mpn


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="partslib", description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."), help="library root (default: current directory)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="Validate part files and detect duplicates")
    check.add_argument(
        "--since",
        metavar="REV",
        help="git revision to compare against, for deleted parts and changed identities (default: HEAD)",
    )

    add = subparsers.add_parser("add", help="Add a new draft part")
    add.add_argument("category")
    add.add_argument("-d", "--description", required=True)
    add.add_argument("-p", "--param", type=_key_value, action="append", default=[], metavar="NAME=VALUE")
    add.add_argument("--mpn", type=_manufacturer_mpn, action="append", default=[], metavar="MANUFACTURER:MPN")
    add.add_argument("--ack", action="append", default=[], metavar="IPN", help="acknowledge a near match with this part")

    subparsers.add_parser("build", help="Generate KiCad libraries from released parts into build/")
    install_parser = subparsers.add_parser(
        "install", help="Register the libraries with KiCad and set ${PARTSLIB}; backs up the config first"
    )
    install_parser.add_argument(
        "--config-dir", type=Path, help="KiCad config directory (default: KiCad's own, for its version)"
    )

    scan_parser = subparsers.add_parser("scan", help="Scan registered boards and run the board checks")
    scan_parser.add_argument("boards", nargs="*", metavar="BOARD", help="boards from boards.yaml (default: all)")
    where = scan_parser.add_mutually_exclusive_group()
    where.add_argument("--commit", default="HEAD", help="board repository revision to scan (default: HEAD)")
    where.add_argument("--worktree", action="store_true", help="scan the files on disk, including uncommitted changes")
    scan_parser.add_argument(
        "--fresh", nargs="?", const="bare", choices=("bare", "partslib"),
        help="also open the board from a fresh checkout: on a machine with only KiCad (bare, the default), "
        "or with partslib installed as well",
    )
    scan_parser.add_argument("--config-dir", type=Path, help="KiCad config to resolve libraries against (default: KiCad's own)")
    scan_parser.add_argument("--all", action="store_true", help="list every designator, not just the first few")

    migrate_parser = subparsers.add_parser("migrate", help="Match a board's parts to library parts (confirm them in the dashboard)")
    migrate_parser.add_argument("board")
    migrate_parser.add_argument("--commit", help="board revision to read (default: the working tree)")

    verify_parser = subparsers.add_parser(
        "verify", help="Check draft parts against their datasheets with Claude, and release those that pass"
    )
    verify_parser.add_argument("ipns", nargs="*", metavar="IPN", help="parts to check (default: every draft)")
    verify_parser.add_argument("--no-release", action="store_true", help="report only; release nothing")

    vendor_parser = subparsers.add_parser(
        "vendor", help="Copy a board's library parts into its lib/ folder, so it opens without partslib"
    )
    vendor_parser.add_argument("board")
    vendor_parser.add_argument("--dry-run", action="store_true", help="show what would change; write nothing")
    vendor_parser.add_argument("--force", action="store_true", help="vendor even if the files it changes have uncommitted edits")

    serve = subparsers.add_parser("serve", help="Start the local dashboard on 127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)

    for name, (help_text, _milestone) in PLANNED.items():
        subparsers.add_parser(name, help=help_text)
    return parser


def _run_check(args: argparse.Namespace) -> int:
    count, findings = check_library(args.root, args.since)
    for finding in findings:
        print(finding)
    print(f"{count} parts checked, {len(findings)} problems", file=sys.stderr)
    return 1 if findings else 0


def _run_add(args: argparse.Namespace) -> int:
    result = add_part(args.root, args.category, args.description, dict(args.param), args.mpn, args.ack)
    if result.path is None:
        for finding in result.findings:
            print(finding)
        print(f"not added: {len(result.findings)} problems", file=sys.stderr)
        return 1
    print(f"added {result.ipn} at {result.path}")
    return 0


def _run_build(args: argparse.Namespace) -> int:
    findings, files = build_library(args.root)
    if findings:
        for finding in findings:
            print(finding)
        print(f"not built: `check` reports {len(findings)} problems", file=sys.stderr)
        return 1
    libraries = sorted({"/".join(path.split("/")[:2]) for path in files if not path.startswith("3dmodels/")})
    print(f"built {len(files)} files into {args.root / 'build'}")
    for library in libraries:
        print(f"  {library}")
    return 0


def _run_install(args: argparse.Namespace) -> int:
    result = install(args.root, args.config_dir)
    print(f"installed into {result.config_dir}")
    print(f"  PARTSLIB = {result.partslib}" + (f" (was {result.previous_partslib})" if result.previous_partslib and result.previous_partslib != str(result.partslib) else ""))
    for table, names in result.libraries.items():
        print(f"  {table}: {', '.join(names) or 'none'}")
    for backup in result.backups:
        print(f"  backed up to {backup}")
    return 0


def _run_scan(args: argparse.Namespace) -> int:
    boards = load_boards(args.root)
    if not boards:
        raise BoardError("no boards in boards.yaml")
    known = {board.name: board for board in boards}
    unknown = [name for name in args.boards if name not in known]
    if unknown:
        raise BoardError(f"unknown board {', '.join(unknown)}; boards.yaml has {', '.join(known)}")
    config = args.config_dir
    if config is None:
        from partslib.install import default_config_dir

        config = default_config_dir()
    failed = False
    for name in args.boards or list(known):
        result = scan(args.root, known[name], None if args.worktree else args.commit, config, args.fresh)
        print(format_result(result, limit=10**6 if args.all else 12))
        print(f"  saved {save(args.root, result)}")
        failed |= result.counts()["error"] > 0
    return 1 if failed else 0


def _run_migrate(args: argparse.Namespace) -> int:
    from collections import Counter

    from partslib import migrate

    boards = {board.name: board for board in load_boards(args.root)}
    if args.board not in boards:
        raise BoardError(f"unknown board {args.board}; boards.yaml has {', '.join(boards) or 'none'}")
    board, groups = migrate.load(args.root, boards[args.board], args.commit)
    counts = Counter(g.status for g in groups)
    done = sum(len(g.references) for g in groups if g.status == "done")
    total = sum(len(g.references) for g in groups)
    print(f"{args.board} at {board.source.label()[:12]}: {done} of {total} parts have an IPN; "
          + ", ".join(f"{n} {s}" for s, n in sorted(counts.items())))
    for group in groups:
        match = group.confirmed or (group.suggestion or {}).get("ipn", "")
        how = "confirmed" if group.confirmed else (group.suggestion or {}).get("how", "")
        shown = ", ".join(group.references[:6]) + (f" and {len(group.references) - 6} more" if len(group.references) > 6 else "")
        print(f"  {group.status:9} {match or '-':11} {how:16} {group.lib_id} {group.value}  [{shown}]")
    print(f"Confirm matches at http://127.0.0.1:8765/boards/{args.board}/migrate")
    return 0


def _run_verify(args: argparse.Namespace) -> int:
    from partslib import verify
    from partslib.library import load_yaml, part_paths

    ipns = args.ipns or sorted(
        p.stem for p in part_paths(args.root)
        if isinstance(raw := load_yaml((args.root / p).read_bytes()), dict) and raw.get("status") == "draft"
    )
    if not ipns:
        print("no draft parts to check")
        return 0
    released = needs_you = 0
    for ipn in ipns:
        try:
            result = verify.verify(args.root, ipn, release=not args.no_release)
        except verify.VerifyError as error:
            print(f"{ipn}: not checked: {error}")
            needs_you += 1
            continue
        verdict = "released" if result.released else ("passed" if result.passed else "needs you")
        print(f"{ipn}: {verdict} (read by {result.model})")
        for check in result.checks:
            page = f" p.{check.page}" if check.page else ""
            print(f"    {check.outcome:7} {check.name}: {check.detail}{page}")
        released += bool(result.released)
        needs_you += not result.passed
    print(f"{released} released, {needs_you} need you")
    return 1 if needs_you else 0


def _run_vendor(args: argparse.Namespace) -> int:
    from partslib.vendor import vendor

    boards = {board.name: board for board in load_boards(args.root)}
    if args.board not in boards:
        raise BoardError(f"unknown board {args.board}; boards.yaml has {', '.join(boards) or 'none'}")
    result = vendor(args.root, boards[args.board], dry_run=args.dry_run, force=args.force)
    verb = "would vendor" if args.dry_run else "vendored"
    print(f"{verb} {result.symbols} symbols, {result.footprints} footprints and {result.models} 3D models into {result.board}/lib/partslib")
    for path in result.changed:
        print(f"  {'would change' if args.dry_run else 'changed'} {path}")
    if not result.changed:
        print("  nothing changed: already vendored")
    for library in result.not_vendored:
        print(f"  not from partslib, so not vendored: {library}")
    if result.changed and not args.dry_run:
        print("Check it opens from a fresh clone: partslib scan " + args.board + " --worktree --fresh")
        print("Then commit the changes in the board's repository.")
    return 0


def _run_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from partslib.web.app import create_app

    # Localhost only (spec section 8): there are no accounts, so nothing else may reach it.
    print(f"partslib dashboard: http://127.0.0.1:{args.port}/")
    uvicorn.run(create_app(args.root), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


COMMAND_RUNNERS = {
    "check": _run_check,
    "add": _run_add,
    "build": _run_build,
    "install": _run_install,
    "scan": _run_scan,
    "migrate": _run_migrate,
    "verify": _run_verify,
    "vendor": _run_vendor,
    "serve": _run_serve,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in PLANNED:
        milestone = PLANNED[args.command][1]
        print(f"partslib {args.command}: not implemented yet ({milestone})", file=sys.stderr)
        return 1
    try:
        return COMMAND_RUNNERS[args.command](args)
    except (LibraryError, BuildError, InstallError, BoardError, VendorError) as error:
        print(f"partslib: {error}", file=sys.stderr)
        return 2
