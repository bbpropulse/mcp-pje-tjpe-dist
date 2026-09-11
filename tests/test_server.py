from unittest.mock import AsyncMock

import pytest
from mcp import Client

from mcp_pje_tjpe.pje_office import PjeOfficePortStatus
from mcp_pje_tjpe.server import mcp


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_server_lists_safe_mvp_tools() -> None:
    async with Client(mcp, raise_exceptions=True) as client:
        result = await client.list_tools()
        names = {tool.name for tool in result.tools}
        assert {
            "status_servidor",
            "listar_tribunais_suportados",
            "status_navegacao_adaptativa",
            "listar_falhas_navegacao",
            "validar_adaptadores_offline",
            "listar_ambientes",
            "diagnosticar_ambiente",
            "diagnosticar_pjeoffice",
            "pesquisar_classes_custas",
            "simular_custas",
            "consultar_processo_publico",
            "testar_login",
            "abrir_login_certificado",
            "verificar_login_certificado",
            "listar_acervo",
            "preparar_acesso_pesquisa_geral",
            "abrir_autos_pesquisa_geral",
            "consultar_autos",
            "ler_documento_autos",
            "baixar_documento_autos",
            "preparar_download_pjedocs",
            "solicitar_download_pjedocs",
            "listar_downloads_pjedocs",
            "baixar_resultado_pjedocs",
            "verificar_chat_cap1g",
            "preparar_chat_cap1g",
            "iniciar_chat_cap1g",
            "ler_chat_cap1g",
            "aguardar_resposta_chat_cap1g",
            "enviar_mensagem_chat_cap1g",
            "encerrar_chat_cap1g",
        } <= names

        tools = {tool.name: tool for tool in result.tools}
        preparation = tools["preparar_acesso_pesquisa_geral"].annotations
        opening = tools["abrir_autos_pesquisa_geral"].annotations
        assert preparation is not None
        assert preparation.read_only_hint is True
        assert preparation.destructive_hint is False
        assert opening is not None
        assert opening.read_only_hint is False
        assert opening.destructive_hint is True
        for name in (
            "listar_tribunais_suportados",
            "status_navegacao_adaptativa",
            "listar_falhas_navegacao",
            "validar_adaptadores_offline",
        ):
            annotations = tools[name].annotations
            assert annotations is not None
            assert annotations.read_only_hint is True
            assert annotations.destructive_hint is False
            assert annotations.open_world_hint is False
        # O chat da CAP1G: só iniciar, enviar e encerrar tocam a conversa real.
        for name in (
            "verificar_chat_cap1g",
            "preparar_chat_cap1g",
            "ler_chat_cap1g",
            "aguardar_resposta_chat_cap1g",
        ):
            annotations = tools[name].annotations
            assert annotations is not None
            assert annotations.read_only_hint is True
            assert annotations.destructive_hint is False
        for name in ("iniciar_chat_cap1g", "enviar_mensagem_chat_cap1g", "encerrar_chat_cap1g"):
            annotations = tools[name].annotations
            assert annotations is not None
            assert annotations.read_only_hint is False
            assert annotations.destructive_hint is True


@pytest.mark.anyio
async def test_status_does_not_launch_browser() -> None:
    async with Client(mcp, raise_exceptions=True) as client:
        result = await client.call_tool("status_servidor", {})
        assert result.is_error is False
        assert result.structured_content is not None
        assert result.structured_content["tribunal"] == "TJPE"
        assert result.structured_content["versao"] == "0.7.0"
        assert result.structured_content["modo_seguro"] is True


@pytest.mark.anyio
async def test_tribunal_catalog_exposes_discovery_without_authenticated_claims() -> None:
    async with Client(mcp, raise_exceptions=True) as client:
        result = await client.call_tool("listar_tribunais_suportados", {})

    assert result.is_error is False
    assert result.structured_content is not None
    profiles = result.structured_content["result"]
    assert [profile["codigo"] for profile in profiles] == ["tjpe", "trt6", "trf5"]
    assert profiles[0]["maturidade"] == "operacional"
    for profile in profiles[1:]:
        assert profile["maturidade"] == "descoberta"
        assert profile["capacidades"]["login_assistido"] is False
        assert profile["capacidades"]["pesquisa_geral_autenticada"] is False


@pytest.mark.anyio
async def test_certificate_login_reports_closed_local_port_without_click(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_pje_tjpe.server as server

    monkeypatch.setattr(
        server,
        "check_local_pje_office_port",
        AsyncMock(
            return_value=PjeOfficePortStatus(
                conexao_aceita=False,
                detalhe="porta fechada no teste",
            )
        ),
    )

    async with Client(mcp) as client:
        result = await client.call_tool("abrir_login_certificado", {"grau": "1g"})

    assert result.is_error is True
    content = result.content[0]
    assert content.type == "text"
    assert "PJeOffice não está respondendo em localhost:8800" in content.text
