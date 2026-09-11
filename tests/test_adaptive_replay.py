from __future__ import annotations

# pyright: reportPrivateUsage=false
import json
from pathlib import Path
from typing import cast

import pytest
from playwright.async_api import Page

from mcp_pje_tjpe.adaptive.adapters import AdapterRegistry
from mcp_pje_tjpe.adaptive.controller import AdaptiveNavigation
from mcp_pje_tjpe.adaptive.events import ObservationStore
from mcp_pje_tjpe.adaptive.models import ControleEstrutural, ObservacaoNavegacao
from mcp_pje_tjpe.config import ModoAdaptativo
from mcp_pje_tjpe.errors import ServicoIndisponivelError
from mcp_pje_tjpe.tribunals import TribunalCodigo


def _control() -> ControleEstrutural:
    return ControleEstrutural(
        tag="button",
        role="button",
        tipo="submit",
        visivel=True,
        habilitado=True,
        submit_nativo=True,
        possui_formulario=True,
        tokens_semanticos=("pesquisar",),
    )


def _observation(
    store: ObservationStore,
    index: int,
    *,
    controls: tuple[ControleEstrutural, ...],
    flow: str = "pesquisa_geral",
) -> ObservacaoNavegacao:
    origin = "https://pje.cloud.tjpe.jus.br"
    path_shape = "/:dynamic/processo/consultaprocesso/listview.seam"
    unsigned = ObservacaoNavegacao(
        referencia=f"replay-observation-{index:04d}",
        tribunal=TribunalCodigo.TJPE,
        instancia="tjpe_1g",
        fluxo=flow,
        etapa="search_submit",
        modo=ModoAdaptativo.SHADOW,
        origem=origin,
        formato_caminho=path_shape,
        assinatura_pagina="0" * 64,
        codigo_falha="unique_control_not_found",
        controles=controls,
    )
    return store.seal(unsigned)


class _PageMustNotBeTouched:
    def is_closed(self) -> bool:
        raise AssertionError("a página não pode ser inspecionada apó o limite de efeito")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_offline_replay_uses_only_persisted_structure(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path)
    navigation = AdaptiveNavigation(AdapterRegistry(), store, ModoAdaptativo.SHADOW)
    assert await store.append(_observation(store, 1, controls=(_control(),))) is True

    replay = await navigation.validate_offline(limit=10)

    assert replay.observacoes_avaliadas == 1
    assert replay.rede_utilizada is False
    assert len(replay.resultados) == 1
    result = replay.resultados[0]
    assert result.executou_navegacao is False
    assert result.candidato_unico is True
    assert result.estrategias[0].correspondencias == 1
    assert result.estrategias[0].quantidade_aceita is True


@pytest.mark.anyio
async def test_offline_replay_rejects_ambiguous_controls(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path)
    navigation = AdaptiveNavigation(AdapterRegistry(), store, ModoAdaptativo.SHADOW)
    assert await store.append(_observation(store, 1, controls=(_control(), _control()))) is True

    replay = await navigation.validate_offline(limit=10)

    assert replay.observacoes_avaliadas == 1
    assert replay.resultados[0].candidato_unico is False
    assert replay.resultados[0].estrategias[0].correspondencias == 2
    assert replay.resultados[0].estrategias[0].quantidade_aceita is False


@pytest.mark.anyio
async def test_offline_replay_rejects_unknown_flow_as_invalid_local_data(
    tmp_path: Path,
) -> None:
    store = ObservationStore(tmp_path)
    navigation = AdaptiveNavigation(AdapterRegistry(), store, ModoAdaptativo.SHADOW)
    assert await store.append(
        _observation(store, 1, controls=(_control(),), flow="fluxo_desconhecido")
    )

    with pytest.raises(ServicoIndisponivelError, match="integridade"):
        await navigation.validate_offline(limit=10)


@pytest.mark.anyio
async def test_offline_replay_on_empty_store_is_deterministically_empty(tmp_path: Path) -> None:
    navigation = AdaptiveNavigation(
        AdapterRegistry(),
        ObservationStore(tmp_path),
        ModoAdaptativo.OBSERVE,
    )

    replay = await navigation.validate_offline(limit=10)

    assert replay.observacoes_avaliadas == 0
    assert replay.resultados == []
    assert replay.rede_utilizada is False


@pytest.mark.anyio
async def test_offline_replay_rejects_tampered_signed_structure(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path)
    navigation = AdaptiveNavigation(AdapterRegistry(), store, ModoAdaptativo.SHADOW)
    observation = _observation(store, 1, controls=(_control(),))
    assert await store.append(observation) is True
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["controles"][0]["visivel"] = False
    store.path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(ServicoIndisponivelError, match="integridade"):
        await navigation.validate_offline(limit=10)


@pytest.mark.anyio
async def test_capture_is_refused_after_side_effect_boundary_without_touching_page(
    tmp_path: Path,
) -> None:
    store = ObservationStore(tmp_path)
    navigation = AdaptiveNavigation(AdapterRegistry(), store, ModoAdaptativo.ACTIVE)

    result = await navigation.record_discovery_failure(
        cast(Page, _PageMustNotBeTouched()),
        tribunal=TribunalCodigo.TJPE,
        instancia="tjpe_1g",
        flow="pesquisa_geral",
        step="search_submit",
        failure_code="unique_control_not_found",
        side_effect_boundary_crossed=True,
    )

    assert result is None
    assert await store.count() == 0
    status = await navigation.status()
    assert status.ultimo_erro_local == "captura recusada após limite de efeito"
    assert status.observacoes == 0


def test_path_shape_removes_dynamic_identifiers_and_rejects_traversal() -> None:
    shape = AdaptiveNavigation._path_shape(
        "/1g/Processo/1234567890123456/ConsultaProcesso/listView.seam"
    )

    assert shape == "/:dynamic/processo/:dynamic/consultaprocesso/listview.seam"
    assert "1234567890123456" not in shape
    with pytest.raises(ValueError, match="sanitizado"):
        AdaptiveNavigation._path_shape("/1g/../segredo")
