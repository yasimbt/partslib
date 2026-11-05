"""Parse engineering values so that 1k, 1000, 1.0 kΩ and 1K0 compare equal (spec section 5.3).

Values become exact Fractions: floats would make equal values compare unequal. The case of `m` and
`M` is significant (milli and mega). Other prefixes and units accept either case, as parts lists
write 100NF and 100nf as often as 100nF.
"""

from decimal import Decimal, InvalidOperation
from fractions import Fraction
import re

PREFIXES = {
    "p": Fraction(1, 10**12),
    "n": Fraction(1, 10**9),
    "u": Fraction(1, 10**6),
    "m": Fraction(1, 10**3),
    "k": Fraction(10**3),
    "M": Fraction(10**6),
    "G": Fraction(10**9),
}
PREFIX_ALIASES = {"P": "p", "N": "n", "U": "u", "K": "k", "g": "G"}

# Unit name -> spellings accepted after the number, compared case-insensitively.
UNITS = {
    "ohm": ("ohms", "ohm", "Ω", "r"),
    "F": ("f",),
    "H": ("h",),
    "V": ("v",),
    "W": ("w",),
    "A": ("a",),
    "Hz": ("hz",),
    "pct": ("%",),
}
UNITLESS_PREFIX_FREE = {"pct"}  # "1m%" is not a tolerance

_NUMBER = r"\d+(?:\.\d*)?|\.\d+"
_PLAIN = re.compile(rf"^({_NUMBER})(?:[eE]([+-]?\d+))?(.*)$")
_FRACTION = re.compile(r"^(\d+)/(\d+)(.*)$")
_INFIX = re.compile(r"^(\d*)([pPnNuUmkKMGgRr])(\d+)$")


def _normalise_text(text: str) -> str:
    # U+2126 OHM SIGN and U+03A9 GREEK OMEGA look identical, as do U+00B5 and U+03BC.
    text = text.replace("Ω", "Ω").replace("µ", "u").replace("μ", "u")
    return re.sub(r"\s+", "", text)


def _prefix_factor(letter: str) -> Fraction:
    return PREFIXES[PREFIX_ALIASES.get(letter, letter)]


def _suffix_factor(suffix: str, unit: str) -> Fraction:
    """Return the multiplier for what follows the number, such as k, kΩ, nF or an empty string."""
    spellings = UNITS[unit]
    if suffix[:3].lower() == "meg":
        prefix, rest = "M", suffix[3:]
    else:
        prefix, rest = "", suffix
    if not prefix:
        for spelling in spellings:
            if rest.lower() == spelling.lower():
                return Fraction(1)
        if not rest:
            return Fraction(1)
        prefix, rest = rest[0], rest[1:]
    if unit in UNITLESS_PREFIX_FREE or (prefix not in PREFIXES and prefix not in PREFIX_ALIASES):
        raise ValueError
    if rest and not any(rest.lower() == spelling.lower() for spelling in spellings):
        raise ValueError
    return _prefix_factor(prefix)


def parse_quantity(value: object, unit: str) -> Fraction:
    """Return `value` in base units, or raise ValueError if it isn't a valid `unit` value."""
    message = f"{value!r} is not a valid value in {unit}"
    if isinstance(value, bool):
        raise ValueError(message)
    if isinstance(value, int):
        number = Fraction(value)
    elif isinstance(value, float):
        number = Fraction(Decimal(repr(value)))  # repr keeps 0.1 as 1/10, not the nearest binary
    elif isinstance(value, str):
        number = _parse_text(_normalise_text(value), unit, message)
    else:
        raise ValueError(message)
    if number < 0:
        raise ValueError(message)
    return number


def _parse_text(text: str, unit: str, message: str) -> Fraction:
    try:
        infix = _INFIX.match(text)
        if infix:  # 1K0, 4u7, 2R2, R10
            whole, marker, decimals = infix.groups()
            number = Fraction(Decimal(f"{whole or '0'}.{decimals}"))
            if marker in "Rr":
                if unit != "ohm":
                    raise ValueError
                return number
            if not whole or unit in UNITLESS_PREFIX_FREE:
                raise ValueError
            return number * _prefix_factor(marker)

        fraction = _FRACTION.match(text)
        if fraction:  # 1/10W
            numerator, denominator, suffix = fraction.groups()
            return Fraction(int(numerator), int(denominator)) * _suffix_factor(suffix, unit)

        plain = _PLAIN.match(text)
        if not plain:
            raise ValueError
        number_text, exponent, suffix = plain.groups()
        number = Fraction(Decimal(number_text + (f"e{exponent}" if exponent else "")))
        return number * _suffix_factor(suffix, unit)
    except (ValueError, ZeroDivisionError, InvalidOperation):
        raise ValueError(message) from None


def plain_number(value: Fraction) -> int | float | str:
    """Return a value as written in a part file, always parsing back to exactly the same value.

    A value with no exact decimal form, such as 1/3 W, stays a fraction: stored as 0.333... it
    would stop matching 1/3 W, and duplicate detection would miss it.
    """
    if value.denominator == 1:
        return int(value)
    as_float = float(value)
    if Fraction(Decimal(repr(as_float))) == value:
        return as_float
    return f"{value.numerator}/{value.denominator}"
