"""Command-line entry point. Commands are stubs until their milestone (spec.md section 12)."""

import argparse
import sys

# Command name -> (help text, milestone that delivers it).
COMMANDS = {
    "check": ("Validate part files and detect duplicates", "M1"),
    "add": ("Add a new part", "M1"),
    "build": ("Generate KiCad libraries into build/", "M2"),
    "install": ("Register the libraries with KiCad and set ${PARTSLIB}", "M2"),
    "scan": ("Scan registered boards and run board checks", "M3"),
    "serve": ("Start the local dashboard", "M4"),
    "vendor": ("Copy a board's parts into its project lib/ folder", "M7"),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="partslib", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, (help_text, _milestone) in COMMANDS.items():
        subparsers.add_parser(name, help=help_text)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    milestone = COMMANDS[args.command][1]
    print(f"partslib {args.command}: not implemented yet ({milestone})", file=sys.stderr)
    return 1
