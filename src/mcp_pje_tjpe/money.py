from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from mcp_pje_tjpe.errors import ValidacaoError

CENTAVOS = Decimal("0.01")


def _decimal_from_text(value: str) -> Decimal:
    normalized = value.strip().replace("R$", "").replace(" ", "")
    if not normalized:
        raise ValidacaoError("valor monetário vazio")
    if not re.fullmatch(r"\+?[0-9][0-9.,]*", normalized):
        raise ValidacaoError(f"valor monetário inválido: {value!r}")
    normalized = normalized.removeprefix("+")

    def strip_grouping(integer: str, separator: str | None) -> str:
        if separator is None or separator not in integer:
            return integer
        groups = integer.split(separator)
        if not groups[0] or len(groups[0]) > 3 or any(len(group) != 3 for group in groups[1:]):
            raise ValidacaoError(f"valor monetário inválido: {value!r}")
        return "".join(groups)

    decimal_separator: str | None = None
    grouping_separator: str | None = None
    if "," in normalized and "." in normalized:
        decimal_separator = "," if normalized.rfind(",") > normalized.rfind(".") else "."
        grouping_separator = "." if decimal_separator == "," else ","
        if normalized.count(decimal_separator) != 1:
            raise ValidacaoError(f"valor monetário inválido: {value!r}")
    elif "," in normalized:
        parts = normalized.split(",")
        if len(parts) == 2 and 0 < len(parts[1]) <= 2:
            decimal_separator = ","
        elif all(len(group) == 3 for group in parts[1:]):
            grouping_separator = ","
        else:
            raise ValidacaoError(f"valor monetário inválido: {value!r}")
    elif "." in normalized:
        parts = normalized.split(".")
        if len(parts) == 2 and 0 < len(parts[1]) <= 2:
            decimal_separator = "."
        elif all(len(group) == 3 for group in parts[1:]):
            grouping_separator = "."
        else:
            raise ValidacaoError(f"valor monetário inválido: {value!r}")

    if decimal_separator:
        integer, fraction = normalized.rsplit(decimal_separator, 1)
        if not fraction or len(fraction) > 2:
            raise ValidacaoError("o valor monetário aceita no máximo duas casas decimais")
        integer = strip_grouping(integer, grouping_separator)
        normalized = f"{integer}.{fraction}"
    else:
        normalized = strip_grouping(normalized, grouping_separator)

    try:
        return Decimal(normalized)
    except InvalidOperation as exc:
        raise ValidacaoError(f"valor monetário inválido: {value!r}") from exc


def _validate_precision(value: Decimal) -> None:
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -2:
        raise ValidacaoError("o valor monetário aceita no máximo duas casas decimais")


def parse_decimal(value: str | int | float | Decimal) -> Decimal:
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        result = Decimal(str(value))
    else:
        result = _decimal_from_text(value)
    if not result.is_finite() or result < 0:
        raise ValidacaoError("o valor deve ser um número finito maior ou igual a zero")
    _validate_precision(result)
    return result.quantize(CENTAVOS)


def format_brl_input(value: str | int | float | Decimal) -> str:
    amount = parse_decimal(value)
    formatted = f"{amount:,.2f}"
    result = formatted.replace(",", "_").replace(".", ",").replace("_", ".")
    if len(result) > 16:
        raise ValidacaoError("o valor excede o limite aceito pelo SICAJUD")
    return result
