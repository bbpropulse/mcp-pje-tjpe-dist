from __future__ import annotations

# pyright: reportPrivateUsage=false
from dataclasses import dataclass, field
from typing import cast

import pytest
from playwright.async_api import Route

from mcp_pje_tjpe.errors import InterfacePjeAlteradaError
from mcp_pje_tjpe.models import Grau
from mcp_pje_tjpe.pje_search import (
    _AUDITED_TERM_COLLISION,
    _FORBIDDEN_FORM_TERM,
    _allowed_search_get,
    _fold,
    _is_seam_no_selection,
    _sanitized_link_shape,
    _SearchGate,
    _validate_safe_form_fields,
)

_SEARCH_URL = "https://pje.cloud.tjpe.jus.br/1g/Processo/ConsultaProcesso/listView.seam"
_AUTOS_URL = (
    "https://pje.cloud.tjpe.jus.br/1g/Processo/ConsultaProcesso/Detalhe/"
    "listProcessoCompletoAdvogado.seam?id=20"
)


def _synthetic_npu(
    sequence: str = "9999999",
    *,
    year: str = "2099",
    origin: str = "9999",
) -> str:
    base = sequence + year + "8" + "17" + origin + "00"
    check_digits = 98 - (int(base) % 97)
    return f"{sequence}-{check_digits:02d}.{year}.8.17.{origin}"


@dataclass(slots=True)
class _FakeRequest:
    url: str
    method: str = "GET"
    resource_type: str = "document"
    post_data: str | None = None


@dataclass(slots=True)
class _FakeRoute:
    request: _FakeRequest
    continued: bool = False
    aborted: str | None = field(default=None)

    async def continue_(self) -> None:
        self.continued = True

    async def abort(self, error_code: str) -> None:
        self.aborted = error_code


async def _route(gate: _SearchGate, request: _FakeRequest) -> _FakeRoute:
    route = _FakeRoute(request)
    await gate.handle(cast(Route, route))
    return route


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_is_seam_no_selection_only_matches_the_sentinel() -> None:
    assert _is_seam_no_selection("org.jboss.seam.ui.NoSelectionConverter.selectionSentinel")
    assert _is_seam_no_selection("  org.jboss.seam.ui.NoSelectionConverter.x  ")
    assert not _is_seam_no_selection("")
    assert not _is_seam_no_selection("Cível")
    assert not _is_seam_no_selection("prefixo org.jboss.seam.ui.NoSelectionConverter.x")


@pytest.mark.anyio
async def test_a4j_posts_to_the_search_route_are_allowed_only_inside_the_window() -> None:
    gate = _SearchGate(Grau.PRIMEIRO)
    fora = await _route(gate, _FakeRequest(_SEARCH_URL, method="POST", resource_type="xhr"))
    assert fora.aborted == "blockedbyclient"
    assert gate.blocked_mutation is True
    assert gate.search_page_post_count == 0

    aberto = _SearchGate(Grau.PRIMEIRO)
    aberto.permit_search_page_posts()
    for url in (_SEARCH_URL, f"{_SEARCH_URL}?cid=4821"):
        permitido = await _route(aberto, _FakeRequest(url, method="POST", resource_type="xhr"))
        assert permitido.continued is True
        assert permitido.aborted is None
    assert aberto.search_page_post_count == 2
    assert aberto.blocked_mutation is False

    aberto.close_search_page_posts()
    depois = await _route(aberto, _FakeRequest(_SEARCH_URL, method="POST", resource_type="xhr"))
    assert depois.aborted == "blockedbyclient"


@pytest.mark.anyio
async def test_open_a4j_window_still_pins_route_grau_and_blocks_other_posts() -> None:
    gate = _SearchGate(Grau.PRIMEIRO)
    gate.permit_search_page_posts()
    outras = (
        "https://pje.cloud.tjpe.jus.br/2g/Processo/ConsultaProcesso/listView.seam",
        "https://pje.cloud.tjpe.jus.br/1g/Processo/Peticionar/peticionar.seam",
        "https://evil.example/1g/Processo/ConsultaProcesso/listView.seam",
        f"{_SEARCH_URL}?cid=4821&acao=protocolar",
        _AUTOS_URL,
    )
    for url in outras:
        route = await _route(gate, _FakeRequest(url, method="POST", resource_type="xhr"))
        assert route.aborted == "blockedbyclient", url
        assert route.continued is False, url
    assert gate.search_page_post_count == 0
    assert gate.blocked_mutation is True


@pytest.mark.anyio
async def test_open_a4j_window_does_not_open_autos_gets() -> None:
    gate = _SearchGate(Grau.PRIMEIRO)
    gate.permit_search_page_posts()
    route = await _route(gate, _FakeRequest(_AUTOS_URL))
    assert route.aborted == "blockedbyclient"
    assert gate.autos_get_count == 0


@pytest.mark.anyio
async def test_blocked_detail_names_the_route_without_leaking_identifiers() -> None:
    numero = _synthetic_npu()
    gate = _SearchGate(Grau.PRIMEIRO)
    await _route(
        gate,
        _FakeRequest(
            f"https://pje.cloud.tjpe.jus.br/1g/Processo/{numero}/id/20250917?npu={numero}",
            method="POST",
            resource_type="xhr",
        ),
    )
    detail = gate.blocked_detail
    assert detail is not None
    assert detail.startswith("POST xhr /1g/Processo/")
    assert numero not in detail
    assert numero.replace("-", "").replace(".", "") not in detail
    assert "npu=" not in detail
    assert "20250917" not in detail

    # Só a primeira barreira é registrada: o diagnóstico não vira um log de tráfego.
    await _route(gate, _FakeRequest(_AUTOS_URL, method="POST", resource_type="xhr"))
    assert gate.blocked_detail == detail
    assert detail not in repr(gate)


@pytest.mark.parametrize(
    ("url", "resource_type", "allowed"),
    [
        ("https://pje.cloud.tjpe.jus.br/1g/a4j/g/3_3_3.Finalorg/app.js", "xhr", True),
        ("https://pje.cloud.tjpe.jus.br/1g/rfres/org.richfaces/skin.css", "fetch", True),
        ("https://pje.cloud.tjpe.jus.br/1g/rfres/org.richfaces/tab.png", "image", True),
        ("https://pje.cloud.tjpe.jus.br/1g/a4j/s/estilo.css", "stylesheet", True),
        (_SEARCH_URL, "xhr", False),
        ("https://pje.cloud.tjpe.jus.br/1g/Processo/dados.json", "xhr", False),
        ("https://pje.cloud.tjpe.jus.br/2g/rfres/org.richfaces/skin.css", "fetch", False),
        ("https://evil.example/1g/rfres/org.richfaces/skin.css", "fetch", False),
    ],
)
def test_allowed_search_get_admits_only_richfaces_resource_paths(
    url: str,
    resource_type: str,
    allowed: bool,
) -> None:
    assert _allowed_search_get(url, resource_type, Grau.PRIMEIRO) is allowed


def _validate(fields: dict[str, str], *, empty_criteria: set[str] | None = None) -> None:
    _validate_safe_form_fields(
        fields,
        npu_names={"fPP:numeroProcesso"},
        form_identifiers={"fPP"},
        empty_criteria_names=empty_criteria if empty_criteria is not None else set(),
        marker="fPP:pesquisar",
    )


def test_seam_no_selection_counts_as_an_unfilled_criterion() -> None:
    sentinel = "org.jboss.seam.ui.NoSelectionConverter.selectionSentinel"
    _validate(
        {"fPP:numeroProcesso": _synthetic_npu(), "fPP:classeJudicial": sentinel},
        empty_criteria={"fPP:classeJudicial"},
    )

    with pytest.raises(InterfacePjeAlteradaError, match="fora da lista consultiva"):
        _validate(
            {"fPP:numeroProcesso": _synthetic_npu(), "fPP:classeJudicial": "Execução Fiscal"},
            empty_criteria={"fPP:classeJudicial"},
        )


def test_richfaces_widget_state_is_tolerated_within_its_audited_shape() -> None:
    _validate(
        {
            "fPP:numeroProcesso": _synthetic_npu(),
            "fPP:tipoMascaraDocumento": "cpf",
            "fPP:habilitarMascaraCpf": "true",
            "fPP:dtAutuacaoDecoration:dtAutuacaoInputCurrentDate": "9/2026",
            "fPP:classeJudicial_selection": "",
            "autoScroll": "",
            "j_id479": "",
        }
    )

    # Com valor, estes voltam a ser critério de busca de fato e continuam barrados.
    for name, value in (
        ("fPP:classeJudicial_selection", "12"),
        ("autoScroll", "180"),
        ("j_id479", "algo"),
        ("fPP:dtAutuacaoDecoration:dtAutuacaoInputDate", "01/01/2026"),
    ):
        with pytest.raises(InterfacePjeAlteradaError, match="fora da lista consultiva"):
            _validate({"fPP:numeroProcesso": _synthetic_npu(), name: value})


@pytest.mark.parametrize(
    "component",
    ["habilitarMascaraCpf", "habilitarMascaraCnpj", "numeroProtocoloPolicia"],
)
def test_audited_collisions_are_excused_but_still_flagged_by_the_term_guard(
    component: str,
) -> None:
    # Estes nomes disparam o guarda de ação processual só por substring; a lista de
    # colisões auditadas é exaustiva e nominal, nunca um padrão genérico.
    assert _FORBIDDEN_FORM_TERM.search(_fold(component)) is not None
    assert _AUDITED_TERM_COLLISION.fullmatch(_fold(component)) is not None


@pytest.mark.parametrize(
    "component",
    [
        "habilitar",
        "habilitacaoNosAutos",
        "protocolarPeticao",
        "numeroProtocoloPoliciaExcluir",
        "tomarCiencia",
        "assinarDocumento",
    ],
)
def test_real_action_fields_are_not_excused_by_the_collision_list(component: str) -> None:
    assert _FORBIDDEN_FORM_TERM.search(_fold(component)) is not None
    assert _AUDITED_TERM_COLLISION.fullmatch(_fold(component)) is None


def test_sanitized_link_shape_masks_digit_runs_and_drops_free_text() -> None:
    numero = _synthetic_npu()
    shape = _sanitized_link_shape(
        {
            "href": "/1g/Processo/Detalhe/listProcessoCompletoAdvogado.seam?id=20250917",
            "onclick": f"abrirProcesso('{numero}')",
            "text": numero,
            "context": f"linha do {numero}",
        }
    )

    assert numero not in shape
    assert "20250917" not in shape
    assert "href=" in shape and "onclick=" in shape
    assert "linha do" not in shape
    assert _sanitized_link_shape({}) == "href='vazio' onclick='vazio'"
