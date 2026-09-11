from __future__ import annotations

# pyright: reportPrivateUsage=false
from pathlib import Path
from typing import Any, cast

import pytest
from playwright.async_api import Error as PlaywrightError

import mcp_pje_tjpe.browser as browser_module
from mcp_pje_tjpe.browser import CHANNEL, NAVEGADOR_AUSENTE, BrowserManager, _is_missing_channel
from mcp_pje_tjpe.config import Settings
from mcp_pje_tjpe.credentials import CredentialStore
from mcp_pje_tjpe.diagnostics import run_diagnostics
from mcp_pje_tjpe.errors import ServicoIndisponivelError

# Mensagens reais do Playwright 1.62 observadas nesta máquina.
CHROME_AUSENTE = (
    "BrowserType.launch: Chromium distribution 'chrome' is not found at "
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome\n"
    'Run "playwright install chrome"'
)
CANAL_INVALIDO = "BrowserType.launch: Unsupported chromium channel \"chrome-inexistente\""


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", downloads_dir=tmp_path / "downloads")


class _FakeChromium:
    def __init__(self, error: Exception | None) -> None:
        self.error = error
        self.channels: list[str | None] = []

    async def launch(self, *, headless: bool, channel: str | None = None) -> object:
        _ = headless
        self.channels.append(channel)
        if self.error is not None:
            raise self.error
        return object()


class _FakePlaywright:
    def __init__(self, chromium: _FakeChromium) -> None:
        self.chromium = chromium
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


def _install_fake(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception | None,
) -> _FakeChromium:
    chromium = _FakeChromium(error)

    class _Starter:
        async def start(self) -> _FakePlaywright:
            return _FakePlaywright(chromium)

    monkeypatch.setattr(browser_module, "async_playwright", lambda: _Starter())
    return chromium


def test_missing_channel_is_told_apart_from_other_launch_failures() -> None:
    assert _is_missing_channel(PlaywrightError(CHROME_AUSENTE)) is True
    assert _is_missing_channel(PlaywrightError(CANAL_INVALIDO)) is True
    assert _is_missing_channel(PlaywrightError("BrowserType.launch: Target page crashed")) is False
    assert _is_missing_channel(PlaywrightError("net::ERR_CONNECTION_REFUSED")) is False


@pytest.mark.anyio
async def test_absent_chrome_becomes_an_actionable_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake(monkeypatch, PlaywrightError(CHROME_AUSENTE))
    manager = BrowserManager(_settings(tmp_path))

    with pytest.raises(ServicoIndisponivelError) as reported:
        await manager.start()

    mensagem = str(reported.value)
    assert mensagem == NAVEGADOR_AUSENTE
    assert "playwright install chrome" in mensagem
    # O texto cru do Playwright não vaza para o advogado.
    assert "BrowserType.launch" not in mensagem
    assert "/Applications/" not in mensagem


@pytest.mark.anyio
async def test_other_launch_failures_do_not_claim_chrome_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake(monkeypatch, PlaywrightError("BrowserType.launch: Target page crashed"))
    manager = BrowserManager(_settings(tmp_path))

    with pytest.raises(ServicoIndisponivelError, match="não foi possível iniciar o navegador"):
        await manager.start()


@pytest.mark.anyio
async def test_failed_start_does_not_leak_a_playwright_per_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    iniciados: list[_FakePlaywright] = []
    chromium = _FakeChromium(PlaywrightError(CHROME_AUSENTE))

    class _Starter:
        async def start(self) -> _FakePlaywright:
            criado = _FakePlaywright(chromium)
            iniciados.append(criado)
            return criado

    monkeypatch.setattr(browser_module, "async_playwright", lambda: _Starter())
    manager = BrowserManager(_settings(tmp_path))

    for _ in range(3):
        with pytest.raises(ServicoIndisponivelError):
            await manager.start()

    # A instância do Playwright é reaproveitada entre tentativas.
    assert len(iniciados) == 1
    assert chromium.channels == [CHANNEL, CHANNEL, CHANNEL]


@pytest.mark.anyio
async def test_doctor_reports_the_browser_first_and_stops_when_it_is_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake(monkeypatch, PlaywrightError(CHROME_AUSENTE))
    config = _settings(tmp_path)
    manager = BrowserManager(config)

    def nunca_chamado(*_: object, **__: object) -> Any:
        raise AssertionError("o diagnóstico não deve abrir páginas sem navegador")

    monkeypatch.setattr(manager, "page", nunca_chamado)

    diagnostico = await run_diagnostics(manager, config, cast(CredentialStore, object()))

    assert diagnostico.sucesso is False
    assert len(diagnostico.itens) == 1
    item = diagnostico.itens[0]
    assert CHANNEL in item.nome
    assert item.sucesso is False
    assert "playwright install chrome" in item.detalhe
