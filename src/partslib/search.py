"""Parametric part search, such as "1 kΩ, 0603, ≤ 1%" (spec section 8).

A query is a list of terms that must all match. A value like 1k or 100nF matches any parameter
with that value; ≤, ≥, < and > compare instead; anything else matches text. Bare numbers such as
0603 match text too, since they are as often a package as a value.
"""

from dataclasses import dataclass
from fractions import Fraction
import operator
import re

from partslib.quantity import parse_quantity

OPERATORS = {"<=": operator.le, "≤": operator.le, ">=": operator.ge, "≥": operator.ge, "<": operator.lt, ">": operator.gt}
SUFFIX_UNITS = [  # longest first
    ("ohms", "ohm"), ("ohm", "ohm"), ("hz", "Hz"), ("Ω", "ohm"), ("%", "pct"),
    ("f", "F"), ("h", "H"), ("v", "V"), ("w", "W"), ("a", "A"), ("r", "ohm"),
]
UNIT_WORDS = {"Ω", "ohm", "ohms", "%", "F", "H", "V", "W", "A", "Hz", "pF", "nF", "uF", "µF", "kΩ", "MΩ", "mW", "mA", "kHz", "MHz"}


@dataclass(frozen=True)
class Term:
    text: str  # what was typed, for text matching
    value: Fraction | None = None
    unit: str | None = None  # None: any quantity parameter
    compare: str | None = None  # operator, or None for equality
    numeric_text: bool = False  # a bare number, which may also be a package such as 0603


def _unit_of(text: str) -> str | None:
    lowered = text.lower()
    for suffix, unit in SUFFIX_UNITS:
        if lowered.endswith(suffix.lower()):
            return unit
    return None


def _quantity(text: str) -> tuple[Fraction, str | None] | None:
    unit = _unit_of(text)
    for candidate in ([unit] if unit else ["ohm", "F", "H", "V", "W", "A", "Hz"]):
        try:
            return parse_quantity(text, candidate), unit
        except ValueError:
            continue
    return None


def _term(text: str) -> Term | None:
    text = text.strip()
    if not text:
        return None
    compare = None
    for symbol in sorted(OPERATORS, key=len, reverse=True):
        if text.startswith(symbol):
            compare, text = symbol, text[len(symbol):].strip()
            break
    quantity = _quantity(text)
    if quantity is None:
        return Term(text) if compare is None else Term(f"{compare}{text}")
    value, unit = quantity
    return Term(text, value, unit, compare, numeric_text=re.fullmatch(r"\d+", text) is not None)


def parse_query(query: str) -> list[Term]:
    terms = []
    for chunk in re.split(r"[,;]+", query):
        whole = _term(chunk)
        if whole is not None and (whole.value is not None or " " not in chunk.strip()):
            terms.append(whole)
            continue
        words = chunk.split()
        index = 0
        while index < len(words):
            word = words[index]
            # "1 kΩ" typed with a space is one value.
            if index + 1 < len(words) and words[index + 1] in UNIT_WORDS:
                joined = _term(word + words[index + 1])
                if joined is not None and joined.value is not None:
                    terms.append(joined)
                    index += 2
                    continue
            term = _term(word)
            if term is not None:
                terms.append(term)
            index += 1
    return terms


@dataclass
class Candidate:
    ipn: str
    haystack: str  # casefolded text: IPN, description, value, MPNs, package and so on
    quantities: list[tuple[str, Fraction]]  # (unit, value) of each quantity parameter


def matches(candidate: Candidate, terms: list[Term]) -> bool:
    for term in terms:
        text_match = term.text.casefold() in candidate.haystack
        if term.value is None:
            if not text_match:
                return False
            continue
        compare = OPERATORS.get(term.compare or "", operator.eq)
        value_match = any(
            (term.unit is None or unit == term.unit) and compare(value, term.value)
            for unit, value in candidate.quantities
        )
        if not (value_match or (term.compare is None and (term.numeric_text or term.unit is None) and text_match)):
            return False
    return True
