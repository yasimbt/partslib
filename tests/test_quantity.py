from fractions import Fraction

import pytest

from partslib.quantity import parse_quantity, plain_number


@pytest.mark.parametrize(
    "written", ["1k", "1000", 1000, 1000.0, "1.0 kΩ", "1K0", "1k0", "1 k", "1000R", "1000 ohm",
                "1e3", "1kohm", "1 KOhm", "1Ω".replace("1", "1000")]
)
def test_spellings_of_one_kilohm_compare_equal(written):
    assert parse_quantity(written, "ohm") == 1000


@pytest.mark.parametrize(
    "written, unit, expected",
    [
        ("4k7", "ohm", 4700),
        ("4R7", "ohm", Fraction(47, 10)),
        ("R10", "ohm", Fraction(1, 10)),
        ("0R", "ohm", 0),
        ("1M", "ohm", 10**6),
        ("1M0", "ohm", 10**6),
        ("1MEG", "ohm", 10**6),
        ("1m", "ohm", Fraction(1, 1000)),
        ("100n", "F", Fraction(1, 10**7)),
        ("100nF", "F", Fraction(1, 10**7)),
        ("100NF", "F", Fraction(1, 10**7)),
        ("0.1uF", "F", Fraction(1, 10**7)),
        ("0.1µF", "F", Fraction(1, 10**7)),
        ("0.1μF", "F", Fraction(1, 10**7)),
        ("4u7", "F", Fraction(47, 10**7)),
        ("4p7", "F", Fraction(47, 10**13)),
        ("10uH", "H", Fraction(1, 10**5)),
        ("100mW", "W", Fraction(1, 10)),
        ("0.1 W", "W", Fraction(1, 10)),
        ("1/10W", "W", Fraction(1, 10)),
        (0.1, "W", Fraction(1, 10)),
        ("75V", "V", 75),
        ("500mA", "A", Fraction(1, 2)),
        ("100MHz", "Hz", 10**8),
        ("1%", "pct", 1),
        ("0.5 %", "pct", Fraction(1, 2)),
        (1, "pct", 1),
    ],
)
def test_parses_engineering_notation(written, unit, expected):
    assert parse_quantity(written, unit) == expected


def test_milli_and_mega_are_different():
    assert parse_quantity("1m", "ohm") != parse_quantity("1M", "ohm")


@pytest.mark.parametrize(
    "written, unit",
    [
        ("", "ohm"),
        ("abc", "ohm"),
        ("1x", "ohm"),
        ("-1k", "ohm"),
        (-1, "ohm"),
        (True, "ohm"),
        (None, "ohm"),
        ("1,5k", "ohm"),
        ("1/0", "W"),
        ("1kF", "ohm"),
        ("10pF", "ohm"),
        ("1kΩ", "F"),
        ("2R2", "F"),
        ("1k%", "pct"),
        ("1k0", "pct"),
    ],
)
def test_rejects_invalid_values(written, unit):
    with pytest.raises(ValueError):
        parse_quantity(written, unit)


def test_plain_number_keeps_whole_values_as_integers():
    assert plain_number(Fraction(1000)) == 1000
    assert isinstance(plain_number(Fraction(1000)), int)
    assert plain_number(Fraction(1, 10)) == 0.1


@pytest.mark.parametrize(
    "value", [Fraction(1, 3), Fraction(1, 7), Fraction(1, 10), Fraction(47, 10**13), Fraction(5)]
)
def test_plain_number_parses_back_to_exactly_the_same_value(value):
    assert parse_quantity(plain_number(value), "W") == value
