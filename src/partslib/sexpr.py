"""Read KiCad's s-expression files into nested lists, for the board scanner.

Quoted strings and bare atoms both become str; KiCad's files never need the distinction for the
fields read here. Reading only: KiCad output is written by kicadgen, which copies text verbatim.
"""

import re

_TOKEN = re.compile(r'\(|\)|"((?:[^"\\]|\\.)*)"|[^\s()"]+')
_ESCAPES = {"n": "\n", "t": "\t", "\\": "\\", '"': '"'}

Node = list  # [head, child, ...], where each child is a str or a Node


def _unescape(text: str) -> str:
    return re.sub(r"\\(.)", lambda m: _ESCAPES.get(m.group(1), m.group(1)), text)


def parse(text: str) -> Node:
    """Parse one top-level expression. Raises ValueError if the text is not balanced."""
    stack: list[Node] = [[]]
    for match in _TOKEN.finditer(text):
        token = match.group(0)
        if token == "(":
            stack.append([])
        elif token == ")":
            if len(stack) == 1:
                raise ValueError("unexpected ')'")
            node = stack.pop()
            stack[-1].append(node)
        elif match.group(1) is not None:
            stack[-1].append(_unescape(match.group(1)))
        else:
            stack[-1].append(token)
    if len(stack) != 1 or len(stack[0]) != 1 or not isinstance(stack[0][0], list):
        raise ValueError("expected exactly one balanced top-level expression")
    return stack[0][0]


def children(node: Node, head: str) -> list[Node]:
    return [child for child in node[1:] if isinstance(child, list) and child and child[0] == head]


def child(node: Node, head: str) -> Node | None:
    found = children(node, head)
    return found[0] if found else None


def value(node: Node, head: str, default: str | None = None) -> str | None:
    """The first atom of a child, such as the uuid in (uuid "...")."""
    found = child(node, head)
    return found[1] if found is not None and len(found) > 1 and isinstance(found[1], str) else default


def properties(node: Node) -> dict[str, str]:
    """A symbol's or footprint's (property "name" "value" ...) children."""
    result = {}
    for prop in children(node, "property"):
        if len(prop) >= 3 and isinstance(prop[1], str) and isinstance(prop[2], str):
            result[prop[1]] = prop[2]
    return result


def flag(node: Node, head: str) -> bool | None:
    """A yes/no child such as (on_board yes). None if absent."""
    found = value(node, head)
    return None if found is None else found == "yes"
