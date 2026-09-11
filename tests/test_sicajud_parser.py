# pyright: reportPrivateUsage=false

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from mcp_pje_tjpe.errors import ServicoIndisponivelError, ValidacaoError
from mcp_pje_tjpe.models import ClasseCustas
from mcp_pje_tjpe.sicajud import (
    NATUREZA_SIMULACAO,
    SicajudClient,
    _parse_suggestion_cells,
    parse_result_rows,
    parse_simulation_snapshot,
)

ROWS = [
    "Código da Classe 7",
    "Descrição da Classe PROCEDIMENTO COMUM CÍVEL",
    "Item de Preparo Valor",
    "Valor Total:R$ 258,60",
    "Custas 1% sobre Valor da Causa R$ 214,06",
    "Taxa Judiciária 1% R$ 44,54",
    "Sistemas Web | Tribunal de Justiça de Pernambuco | Versão 1.67.1",
]


def test_parse_structured_official_simulation() -> None:
    captured_at = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    result = parse_simulation_snapshot(
        classe_codigo="7",
        classe_descricao="PROCEDIMENTO COMUM CÍVEL",
        valor_causa=Decimal("50000.00"),
        item_rows=[
            (
                "Custas 1% sobre Valor da Causa",
                "R$ 500,00",
                "Regra Custas. 1% sobre Valor da Causa Fundamentação Legal "
                "Art. 11, I, da Lei Estadual n° 17.116/20.",
            ),
            (
                "Taxa Judiciária 1%",
                "R$ 500,00",
                "Regra Taxa judiciária. 1% do valor da causa Fundamentação Legal "
                "Art. 3°, I, da Lei Estadual n° 17.116/20.",
            ),
        ],
        total_text="Valor Total: R$ 1.000,00",
        source_url=("https://www.tjpe.jus.br/custasjudiciais/xhtml/simulacao/simularCustas.xhtml"),
        page_text="Sistemas Web | TJPE | Versão 1.67.1",
        captured_at=captured_at,
    )

    assert result.classe == ClasseCustas(codigo="7", descricao="PROCEDIMENTO COMUM CÍVEL")
    assert result.valor_total == Decimal("1000.00")
    assert result.versao_sicajud == "1.67.1"
    assert result.natureza == NATUREZA_SIMULACAO
    assert result.capturado_em == captured_at
    assert [item.fundamento_legal for item in result.itens] == [
        "Art. 11, I, da Lei Estadual n° 17.116/20.",
        "Art. 3°, I, da Lei Estadual n° 17.116/20.",
    ]


def test_parse_result_rows_remains_compatible() -> None:
    result = parse_result_rows(
        ROWS,
        classe_fallback="Procedimento Comum Cível",
        valor_causa=Decimal("1000.00"),
        source_url="https://www.tjpe.jus.br/custasjudiciais",
    )

    assert result.classe.codigo == "7"
    assert result.classe.descricao == "PROCEDIMENTO COMUM CÍVEL"
    assert result.valor_total == Decimal("258.60")
    assert result.versao_sicajud == "1.67.1"
    assert [(item.descricao, item.valor) for item in result.itens] == [
        ("Custas 1% sobre Valor da Causa", Decimal("214.06")),
        ("Taxa Judiciária 1%", Decimal("44.54")),
    ]


def test_parse_requires_total() -> None:
    with pytest.raises(ServicoIndisponivelError, match="valor total"):
        parse_result_rows(
            ["resultado inesperado"],
            classe_fallback="Classe",
            valor_causa=Decimal("10.00"),
            source_url="https://example.invalid",
        )


def test_parse_rejects_inconsistent_sum() -> None:
    with pytest.raises(ServicoIndisponivelError, match="soma dos itens"):
        parse_simulation_snapshot(
            classe_codigo="7",
            classe_descricao="PROCEDIMENTO COMUM CÍVEL",
            valor_causa=Decimal("100.00"),
            item_rows=[("Custas", "R$ 10,00", None)],
            total_text="R$ 11,00",
            source_url="https://example.invalid",
        )


def test_parses_richfaces_suggestion_with_hidden_description() -> None:
    result = _parse_suggestion_cells(["PROCEDIMENTO COMUM CÍVEL", "7", "PROCEDIMENTO COMUM CÍVEL"])
    assert result == ClasseCustas(codigo="7", descricao="PROCEDIMENTO COMUM CÍVEL")
    assert _parse_suggestion_cells(["Nenhuma Classe CNJ encontrada"]) is None


def test_selects_exact_description_ignoring_accents_and_case() -> None:
    candidates = [
        ClasseCustas(codigo="7", descricao="PROCEDIMENTO COMUM CÍVEL"),
        ClasseCustas(codigo="1706", descricao="PROCEDIMENTO COMUM INFÂNCIA E JUVENTUDE"),
    ]
    selected = SicajudClient._select_class(candidates, "procedimento comum civel", None)
    assert selected.codigo == "7"


def test_requires_code_for_ambiguous_search() -> None:
    candidates = [
        ClasseCustas(codigo="7", descricao="PROCEDIMENTO COMUM CÍVEL"),
        ClasseCustas(codigo="1706", descricao="PROCEDIMENTO COMUM INFÂNCIA E JUVENTUDE"),
    ]
    with pytest.raises(ValidacaoError, match="codigo_classe"):
        SicajudClient._select_class(candidates, "procedimento comum", None)


def test_rejects_stale_selected_class() -> None:
    expected = ClasseCustas(codigo="7", descricao="PROCEDIMENTO COMUM CÍVEL")
    stale = ClasseCustas(codigo="1706", descricao="PROCEDIMENTO COMUM INFÂNCIA")
    with pytest.raises(ServicoIndisponivelError, match="classe diferente"):
        SicajudClient._assert_selected_class(stale, expected)
