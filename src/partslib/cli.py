"""partslib: a personal, single-source KiCad parts library. See spec.md."""

import argparse
from pathlib import Path
import sys

from partslib.add import add_part
from partslib.check import check_library
from partslib.library import LibraryError

# Commands not built yet -> (help text, milestone that delivers them).
PLANNED = {
    "build": ("Generate KiCad libraries into build/", "M2"),
    "install": ("Register the libraries with KiCad and set ${PARTSLIB}", "M2"),
    "scan": ("Scan registered boards and run board checks", "M3"),
    "serve": ("Start the local dashboard", "M4"),
    "vendor": ("Copy a board's parts into its project lib/ folder", "M7"),
}


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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in PLANNED:
        milestone = PLANNED[args.command][1]
        print(f"partslib {args.command}: not implemented yet ({milestone})", file=sys.stderr)
        return 1
    try:
        return _run_check(args) if args.command == "check" else _run_add(args)
    except LibraryError as error:
        print(f"partslib: {error}", file=sys.stderr)
        return 2
