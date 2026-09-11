from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import Page

from mcp_pje_tjpe.browser import BrowserManager
from mcp_pje_tjpe.config import Settings
from mcp_pje_tjpe.models import Grau
from mcp_pje_tjpe.pje_office import (
    PJE_OFFICE_HOST,
    PJE_OFFICE_PORT,
    PjeOfficePortStatus,
    PjeOfficeSsoStatus,
    check_local_pje_office_port,
    diagnose_pje_office,
    inspect_pje_office_sso,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_port_check_only_opens_and_closes_tcp_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = MagicMock(spec=asyncio.StreamWriter)
    writer.wait_closed = AsyncMock()
    open_connection = AsyncMock(return_value=(asyncio.StreamReader(), writer))
    monkeypatch.setattr(asyncio, "open_connection", open_connection)

    result = await check_local_pje_office_port()

    open_connection.assert_awaited_once_with(PJE_OFFICE_HOST, PJE_OFFICE_PORT)
    writer.write.assert_not_called()
    writer.writelines.assert_not_called()
    writer.close.assert_called_once_with()
    writer.wait_closed.assert_awaited_once_with()
    assert result.conexao_aceita is True
    assert result.dados_aplicacao_enviados is False
    assert result.identidade_processo_verificada is False
    assert "não autentica a identidade do processo" in result.detalhe


@pytest.mark.anyio
async def test_port_check_reports_refusal_without_sending_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    open_connection = AsyncMock(side_effect=ConnectionRefusedError("recusada"))
    monkeypatch.setattr(asyncio, "open_connection", open_connection)

    result = await check_local_pje_office_port()

    assert result.conexao_aceita is False
    assert result.dados_aplicacao_enviados is False
    assert "Nenhum challenge, cookie, CPF, senha, PIN" in result.detalhe


@pytest.mark.anyio
async def test_port_check_falls_back_to_ipv6_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = MagicMock(spec=asyncio.StreamWriter)
    writer.wait_closed = AsyncMock()
    open_connection = AsyncMock(
        side_effect=[
            ConnectionRefusedError("IPv4 recusado"),
            (asyncio.StreamReader(), writer),
        ]
    )
    monkeypatch.setattr(asyncio, "open_connection", open_connection)

    result = await check_local_pje_office_port()

    assert result.conexao_aceita is True
    assert result.host == "::1"
    assert result.hosts_testados == ["127.0.0.1", "::1"]
    writer.write.assert_not_called()
    writer.close.assert_called_once_with()


def _locator(count: int) -> MagicMock:
    locator = MagicMock()
    locator.count = AsyncMock(return_value=count)
    locator.click = AsyncMock()
    return locator


def _browser_with_page(page: MagicMock) -> BrowserManager:
    @asynccontextmanager
    async def page_context() -> AsyncGenerator[Page]:
        yield cast(Page, page)

    browser = MagicMock(spec=BrowserManager)
    browser.page.side_effect = page_context
    return cast(BrowserManager, browser)


@pytest.mark.anyio
async def test_sso_inspection_finds_selector_without_clicking() -> None:
    page = MagicMock()
    page.url = (
        "https://sso.cloud.pje.jus.br/auth/realms/pje/protocol/openid-connect/auth"
        "?state=nao-deve-ser-retornado"
    )
    response = MagicMock(status=200)
    page.goto = AsyncMock(return_value=response)
    id_locator = _locator(1)
    empty_button = _locator(0)
    empty_link = _locator(0)
    page.locator.return_value = id_locator
    page.get_by_role.side_effect = [empty_button, empty_link]

    result = await inspect_pje_office_sso(_browser_with_page(page), Settings(), Grau.PRIMEIRO)

    assert result.botao_certificado_presente is True
    assert result.seletor_kc_pje_office_presente is True
    assert result.clique_realizado is False
    assert result.url_final is not None
    assert "state=" not in result.url_final
    page.goto.assert_awaited_once_with(
        "https://pje.cloud.tjpe.jus.br/1g/login.seam",
        wait_until="domcontentloaded",
    )
    id_locator.click.assert_not_awaited()
    empty_button.click.assert_not_awaited()
    empty_link.click.assert_not_awaited()


@pytest.mark.anyio
async def test_sso_inspection_accepts_certificate_button_label_without_id() -> None:
    page = MagicMock()
    page.url = "https://sso.cloud.pje.jus.br/auth/realms/pje/login-actions/authenticate"
    page.goto = AsyncMock(return_value=MagicMock(status=200))
    missing_id = _locator(0)
    labeled_button = _locator(1)
    empty_link = _locator(0)
    page.locator.return_value = missing_id
    page.get_by_role.side_effect = [labeled_button, empty_link]

    result = await inspect_pje_office_sso(_browser_with_page(page), Settings(), Grau.SEGUNDO)

    assert result.botao_certificado_presente is True
    assert result.seletor_kc_pje_office_presente is False
    assert result.rotulo_certificado_digital_presente is True
    labeled_button.click.assert_not_awaited()


@pytest.mark.anyio
async def test_sso_inspection_does_not_echo_playwright_error_urls() -> None:
    page = MagicMock()
    page.goto = AsyncMock(
        side_effect=RuntimeError("falha em https://sso.cloud.pje.jus.br/?session_code=segredo")
    )

    result = await inspect_pje_office_sso(_browser_with_page(page), Settings(), Grau.PRIMEIRO)

    assert result.botao_certificado_presente is False
    assert "session_code" not in result.model_dump_json()
    assert "segredo" not in result.model_dump_json()


@pytest.mark.anyio
async def test_aggregate_is_ready_only_with_open_port_and_both_sso_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_pje_tjpe.pje_office as module

    port_check = AsyncMock(
        return_value=PjeOfficePortStatus(
            conexao_aceita=True,
            detalhe="porta aberta não autentica a identidade do processo",
        )
    )

    async def inspect(
        _browser: BrowserManager, _config: Settings, grau: Grau
    ) -> PjeOfficeSsoStatus:
        return PjeOfficeSsoStatus(
            grau=grau,
            url_solicitada=f"https://example.test/{grau.value}/login.seam",
            botao_certificado_presente=grau is Grau.PRIMEIRO,
            detalhe="inspeção simulada",
        )

    monkeypatch.setattr(module, "check_local_pje_office_port", port_check)
    monkeypatch.setattr(module, "inspect_pje_office_sso", inspect)

    result = await diagnose_pje_office(
        cast(BrowserManager, MagicMock(spec=BrowserManager)), Settings()
    )

    assert result.pronto_para_login is False
    assert len(result.sso) == 2
    assert "porta aberta não autentica a identidade do processo" in result.mensagem
    assert result.aviso_seguranca.startswith("Nenhum challenge, cookie, CPF, senha, PIN")


@pytest.mark.anyio
async def test_aggregate_reports_ready_when_all_passive_signals_are_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_pje_tjpe.pje_office as module

    monkeypatch.setattr(
        module,
        "check_local_pje_office_port",
        AsyncMock(
            return_value=PjeOfficePortStatus(
                conexao_aceita=True,
                detalhe="porta aberta não autentica a identidade do processo",
            )
        ),
    )

    async def inspect(
        _browser: BrowserManager, _config: Settings, grau: Grau
    ) -> PjeOfficeSsoStatus:
        return PjeOfficeSsoStatus(
            grau=grau,
            url_solicitada=f"https://example.test/{grau.value}/login.seam",
            botao_certificado_presente=True,
            detalhe="inspeção simulada",
        )

    monkeypatch.setattr(module, "inspect_pje_office_sso", inspect)

    result = await diagnose_pje_office(
        cast(BrowserManager, MagicMock(spec=BrowserManager)), Settings()
    )

    assert result.pronto_para_login is True
    assert "não autentica a identidade do processo" in result.mensagem
