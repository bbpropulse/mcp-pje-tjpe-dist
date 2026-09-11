from __future__ import annotations

# pyright: reportPrivateUsage=false
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import pytest
from playwright.async_api import Browser, Page, async_playwright

from mcp_pje_tjpe.config import Settings
from mcp_pje_tjpe.pje_auth import PjeSessionManager
from mcp_pje_tjpe.pje_read import PjeReadService

# Estrutura colhida dos Autos reais do TJPE em 05/09/2026. Dois pontos que
# quebravam o extrator: o elemento clicável tem [title] e texto vazio, e as linhas
# da timeline são descendentes, não filhas diretas de #divTimeLine.
_TIMELINE = """
<!doctype html><html><body>
  <form id="divTimeLine">
    <div class="envolve">
      <div class="media data"><span class="data-interna">19/02/2025</span></div>
      <div class="media">
        <div class="media-body">Publicado Despacho em 19/02/2025.</div>
      </div>
      <div class="media interno tipo-D">
        <div class="media-body box">
          <div class="anexos">
            <a title="Abrir documento em outra aba/página"
               onclick="abrirLinkDocumento('196750071');"><i class="fa"></i></a>
            <a onclick="abrirLinkDocumento('196750071');">
              <span>196750071 - Petição (Outras)</span>
            </a>
            <ul class="tree">
              <li>
                <a title="Abrir documento em outra aba/página"
                   onclick="abrirLinkDocumento('196751584');"><i class="fa"></i></a>
                <a onclick="abrirLinkDocumento('196751584');">
                  <span>196751584 - Anexo (WhatsApp Image)</span>
                </a>
              </li>
            </ul>
          </div>
        </div>
      </div>
      <div class="media">
        <div class="media-body">Arquivado Definitivamente</div>
      </div>
    </div>
  </form>
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
        Settings(data_dir=tmp_path / "data", downloads_dir=tmp_path / "downloads"),
    )


@pytest.mark.anyio
async def test_documents_carry_the_real_label_not_a_generic_one(tmp_path: Path) -> None:
    """O seletor antigo casava o [title] vazio e todo documento virava "Documento id"."""
    async with _pagina() as page:
        await page.set_content(_TIMELINE)

        entradas, _ = await _service(tmp_path)._extract_documents(page, 50)

        titulos = {e["id"]: e["title"] for e in entradas}
        assert titulos["196750071"] == "196750071 - Petição (Outras)"
        # O anexo tem rótulo próprio e não herda o do documento que o contém.
        assert titulos["196751584"] == "196751584 - Anexo (WhatsApp Image)"


@pytest.mark.anyio
async def test_movements_are_found_although_they_are_not_direct_children(
    tmp_path: Path,
) -> None:
    """':scope > div.media' devolvia zero: as linhas são descendentes da timeline."""
    async with _pagina() as page:
        await page.set_content(_TIMELINE)

        movimentos = await _service(tmp_path)._extract_movements(page, 50)

        descricoes = [m.descricao for m in movimentos]
        assert any("Publicado Despacho" in d for d in descricoes)
        assert any("Arquivado Definitivamente" in d for d in descricoes)
        # A linha de data vira o carimbo do movimento seguinte, não um movimento.
        assert all("19/02/2025" != d for d in descricoes)
        assert movimentos[0].data == "19/02/2025"


@pytest.mark.anyio
async def test_a_nested_row_is_not_counted_as_its_own_movement(tmp_path: Path) -> None:
    """Selecionar por estrutura evita contar duas vezes a linha aninhada do anexo."""
    async with _pagina() as page:
        await page.set_content(_TIMELINE)

        movimentos = await _service(tmp_path)._extract_movements(page, 50)

        # Três linhas de topo: data, publicação, documento e arquivamento — a data
        # não vira movimento, e o anexo dentro do documento não vira linha própria.
        assert len(movimentos) == 3


@pytest.mark.anyio
async def test_the_time_of_the_act_leaves_the_description(tmp_path: Path) -> None:
    """Strings observadas no TJPE em 2026-09-08, com o horário colado no fim."""
    async with _pagina() as page:
        await page.set_content(
            """
            <!doctype html><html><body>
              <div id="divTimeLine">
                <div class="media"><div class="media-body">
                  Conclusos para despacho 07:49
                </div></div>
                <div class="media"><div class="media-body">
                  Distribuído por sorteio 15:17
                </div></div>
                <div class="media"><div class="media-body">
                  Juntada de Petição de contrarrazões
                </div></div>
              </div>
            </body></html>
            """
        )

        movimentos = await _service(tmp_path)._extract_movements(page, 50)

        por_descricao = {m.descricao: m.hora for m in movimentos}
        assert por_descricao["Conclusos para despacho"] == "07:49"
        assert por_descricao["Distribuído por sorteio"] == "15:17"
        # Movimento sem horário continua sem horário — nada é inventado.
        assert por_descricao["Juntada de Petição de contrarrazões"] is None
