from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

from mcp_pje_tjpe.config import Settings
from mcp_pje_tjpe.errors import ServicoIndisponivelError

# O PJe do TJPE serve conteúdo diferente ao Chromium empacotado e o PJeOffice espera
# um Chrome real; o canal é requisito de funcionamento, não preferência.
CHANNEL = "chrome"

NAVEGADOR_AUSENTE = (
    "o Google Chrome não foi encontrado nesta máquina, e ele é requisito deste "
    "servidor. Instale o Chrome ou execute 'uv run playwright install chrome'"
)


def _is_missing_channel(error: PlaywrightError) -> bool:
    """Distingue 'Chrome não instalado' de qualquer outra falha de inicialização."""
    message = str(error)
    return "is not found at" in message or "Unsupported chromium channel" in message


class BrowserManager:
    """Mantém um Chrome compartilhado e cria contextos isolados por operação."""

    def __init__(self, config: Settings, *, accept_downloads: bool = True) -> None:
        self.config = config
        self.accept_downloads = accept_downloads
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._start_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._browser and self._browser.is_connected():
            return
        async with self._start_lock:
            if self._browser and self._browser.is_connected():
                return
            playwright = self._playwright or await async_playwright().start()
            self._playwright = playwright
            try:
                self._browser = await playwright.chromium.launch(
                    headless=self.config.headless, channel=CHANNEL
                )
            except PlaywrightError as exc:
                # Sem o navegador não há operação alguma; a falha precisa dizer o que
                # fazer em vez de devolver a mensagem crua do Playwright.
                if _is_missing_channel(exc):
                    raise ServicoIndisponivelError(NAVEGADOR_AUSENTE) from None
                raise ServicoIndisponivelError(
                    "não foi possível iniciar o navegador local"
                ) from None

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        self._browser = None
        self._playwright = None

    async def new_context(self) -> BrowserContext:
        await self.start()
        assert self._browser is not None
        return await self._browser.new_context(
            accept_downloads=self.accept_downloads,
            locale="pt-BR",
            viewport={"width": 1440, "height": 1000},
        )

    @asynccontextmanager
    async def page(self) -> AsyncGenerator[Page]:
        context = await self.new_context()
        page = await context.new_page()
        page.set_default_timeout(self.config.timeout_ms)
        page.set_default_navigation_timeout(self.config.timeout_ms)
        try:
            yield page
        finally:
            await context.close()
