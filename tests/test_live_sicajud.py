import os
from decimal import Decimal

import pytest

from mcp_pje_tjpe.browser import BrowserManager
from mcp_pje_tjpe.config import Settings
from mcp_pje_tjpe.sicajud import NATUREZA_SIMULACAO, SicajudClient

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("RUN_TJPE_LIVE_TESTS") != "1",
        reason="defina RUN_TJPE_LIVE_TESTS=1 para acessar o SICAJUD real",
    ),
]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_public_sicajud_class_search() -> None:
    config = Settings(headless=True)
    browser = BrowserManager(config)
    try:
        classes = await SicajudClient(browser, config).pesquisar_classes("procedimento comum")
        assert ("7", "PROCEDIMENTO COMUM CÍVEL") in {
            (item.codigo, item.descricao) for item in classes
        }
    finally:
        await browser.close()


async def test_public_sicajud_simulation() -> None:
    config = Settings(headless=True)
    browser = BrowserManager(config)
    try:
        result = await SicajudClient(browser, config).simular("Procedimento Comum Cível", "1000.00")
        assert result.classe.codigo == "7"
        assert result.valor_causa == Decimal("1000.00")
        assert result.valor_total > 0
        assert (
            sum((item.valor for item in result.itens), start=Decimal("0.00")) == result.valor_total
        )
        assert result.url_fonte == config.urls.sicajud_simulacao
        assert result.versao_sicajud
        assert result.natureza == NATUREZA_SIMULACAO
        assert result.capturado_em is not None
        assert any(item.fundamento_legal for item in result.itens)
    finally:
        await browser.close()
