from decimal import Decimal

import pytest

from mcp_pje_tjpe.errors import ValidacaoError
from mcp_pje_tjpe.money import format_brl_input, parse_decimal


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("R$ 1.234,56", Decimal("1234.56")),
        ("1000.00", Decimal("1000.00")),
        ("50,000.00", Decimal("50000.00")),
        ("50.000,00", Decimal("50000.00")),
        ("50,000", Decimal("50000.00")),
        ("50.000", Decimal("50000.00")),
        (1000, Decimal("1000.00")),
        (12.3, Decimal("12.30")),
    ],
)
def test_parse_decimal(raw: object, expected: Decimal) -> None:
    assert parse_decimal(raw) == expected  # type: ignore[arg-type]


def test_format_brl_input() -> None:
    assert format_brl_input("1234.5") == "1.234,50"


@pytest.mark.parametrize("raw", ["", "invalido", "-1", "NaN", "Infinity", "12,3456", "1.2.3"])
def test_rejects_invalid_money(raw: str) -> None:
    with pytest.raises(ValidacaoError):
        parse_decimal(raw)


def test_rejects_amount_longer_than_sicajud_mask() -> None:
    with pytest.raises(ValidacaoError, match="limite"):
        format_brl_input("99999999999,99")
