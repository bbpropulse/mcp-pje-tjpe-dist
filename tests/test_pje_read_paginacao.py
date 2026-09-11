from __future__ import annotations

# pyright: reportPrivateUsage=false
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import pytest
from playwright.async_api import Browser, Page, async_playwright

from mcp_pje_tjpe.config import Settings
from mcp_pje_tjpe.errors import ValidacaoError
from mcp_pje_tjpe.models import Grau
from mcp_pje_tjpe.pje_auth import PjeSessionManager
from mcp_pje_tjpe.pje_read import PjeReadService

# Datascroller como o Acervo real o entrega: os botões disparam um evento próprio, e
# o de avançar recebe -dsbld quando a página atual é a última.
_SCROLLER = """
<!doctype html><html><body>
  <div id="divListaAcervo"></div>
  <!-- O histórico de movimentação vem antes no DOM e nunca tem próxima habilitada.
       Pegar o primeiro div.rich-datascr parava a paginação na página 1. -->
  <div class="rich-datascr" id="formLogHistMov:tbHistoricoMov:logTable">
    <table><tr>
      <td class="rich-datascr-button rich-datascr-button-dsbld"
          onclick="Event.fire(this, 'rich:datascroller:onscroll', {'page': 'next'});"></td>
    </tr></table>
  </div>
  <div class="rich-datascr" id="formAcervo:tbProcessos:scPendentes">
    <table><tr>
      <td class="rich-datascr-button rich-datascr-button-dsbld"
          onclick="Event.fire(this, 'rich:datascroller:onscroll', {'page': 'previous'});">«</td>
      <td class="rich-datascr-act">1</td>
      <td class="rich-datascr-button"
          onclick="Event.fire(this, 'rich:datascroller:onscroll', {'page': 'next'});"></td>
    </tr></table>
  </div>
  <script>
    window.paginaAtual = 1;
    for (const td of document.querySelectorAll('td.rich-datascr-button')) {
      td.addEventListener('click', () => {
        if (td.classList.contains('rich-datascr-button-dsbld')) return;
        if (!(td.getAttribute('onclick') || '').includes("'next'")) return;
        window.paginaAtual += 1;
        const lista = document.getElementById('divListaAcervo');
        lista.innerHTML = Array.from({length: window.paginaAtual}, (_, i) =>
          `<a href="/1g/p?id=${i}">processo ${i}</a>`).join('');
        if (window.paginaAtual >= 3) td.classList.add('rich-datascr-button-dsbld');
      });
    }
  </script>
</body></html>
"""


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@asynccontextmanager
async def _pagina() -> AsyncGenerator[Page]:
    async with async_playwright() as playwright:
        browser: Browser = await playwright.chromium.launch(headless=True)
        try:
            yield await browser.new_page()
        finally:
            await browser.close()


def _service(tmp_path: Path) -> PjeReadService:
    return PjeReadService(
        cast(PjeSessionManager, object()),
        Settings(
            data_dir=tmp_path / "data",
            downloads_dir=tmp_path / "downloads",
            timeout_ms=3_000,
        ),
    )


@pytest.mark.anyio
async def test_advancing_walks_until_the_next_button_is_disabled(tmp_path: Path) -> None:
    async with _pagina() as page:
        await page.set_content(_SCROLLER)
        service = _service(tmp_path)

        assert await service._avancar_pagina_acervo(page) is True
        assert await page.evaluate("window.paginaAtual") == 2
        assert await service._avancar_pagina_acervo(page) is True
        assert await page.evaluate("window.paginaAtual") == 3

        # Na última, o botão vem -dsbld: avançar precisa dizer que acabou.
        assert await service._avancar_pagina_acervo(page) is False
        assert await page.evaluate("window.paginaAtual") == 3


@pytest.mark.anyio
async def test_a_disabled_control_is_never_clicked(tmp_path: Path) -> None:
    """O «« desabilitado fica na mesma barra; clicar nele seria voltar sem querer."""
    async with _pagina() as page:
        await page.set_content(
            """
            <!doctype html><html><body>
              <div id="divListaAcervo"></div>
              <div class="rich-datascr" id="formAcervo:tbProcessos:scPendentes">
                <table><tr>
                  <td class="rich-datascr-button rich-datascr-button-dsbld"
                      onclick="window.tocado = true;">«</td>
                </tr></table>
              </div>
            </body></html>
            """
        )
        service = _service(tmp_path)

        assert await service._avancar_pagina_acervo(page) is False
        assert await page.evaluate("window.tocado === undefined") is True


@pytest.mark.anyio
async def test_a_list_without_a_scroller_reports_no_next_page(tmp_path: Path) -> None:
    async with _pagina() as page:
        await page.set_content(
            '<!doctype html><html><body><div id="divListaAcervo"></div></body></html>'
        )

        assert await _service(tmp_path)._avancar_pagina_acervo(page) is False


@pytest.mark.anyio
async def test_the_page_budget_is_bounded(tmp_path: Path) -> None:
    """Percorrer as 38 páginas de Recife por distração seriam 38 round-trips a4j."""
    service = _service(tmp_path)
    for invalido in (0, -1, 21, 100):
        with pytest.raises(ValidacaoError, match="paginas deve estar entre 1 e 20"):
            await service.listar_acervo(
                Grau.PRIMEIRO, jurisdicao="Recife - Varas", paginas=invalido
            )


@pytest.mark.anyio
async def test_the_history_scroller_is_never_mistaken_for_the_list(tmp_path: Path) -> None:
    """Só o datascroller da tabela de processos pagina o Acervo.

    O histórico de movimentação também é um div.rich-datascr e aparece antes no DOM.
    Enquanto o seletor foi posicional, avançar página tocava o scroller errado e
    devolvia False sem erro — a listagem parava em 40 de 1650.
    """
    async with _pagina() as page:
        await page.set_content(
            """
            <!doctype html><html><body>
              <div id="divListaAcervo"></div>
              <div class="rich-datascr" id="formLogHistMov:tbHistoricoMov:logTable">
                <table><tr>
                  <td class="rich-datascr-button"
                      onclick="Event.fire(this, 'rich:datascroller:onscroll', {'page': 'next'});"
                      ></td>
                </tr></table>
              </div>
              <div class="rich-datascr" id="formAcervo:tbProcessos:scPendentes">
                <table><tr>
                  <td class="rich-datascr-button"
                      onclick="Event.fire(this, 'rich:datascroller:onscroll', {'page': 'next'});"
                      ></td>
                </tr></table>
              </div>
              <script>
                for (const div of document.querySelectorAll('div.rich-datascr')) {
                  const daLista = div.id.includes('tbProcessos');
                  for (const td of div.querySelectorAll('td')) {
                    td.addEventListener('click', () => {
                      if (daLista) {
                        document.getElementById('divListaAcervo').innerHTML =
                          '<a href="/1g/p?id=1">processo</a>';
                        window.listaTocada = true;
                      } else {
                        window.historicoTocado = true;
                      }
                    });
                  }
                }
              </script>
            </body></html>
            """
        )

        assert await _service(tmp_path)._avancar_pagina_acervo(page) is True
        assert await page.evaluate("window.listaTocada === true") is True
        assert await page.evaluate("window.historicoTocado === undefined") is True
