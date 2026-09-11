from __future__ import annotations

# pyright: reportPrivateUsage=false
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import pytest
from playwright.async_api import Browser, Page, async_playwright

from mcp_pje_tjpe.browser import BrowserManager
from mcp_pje_tjpe.config import Settings
from mcp_pje_tjpe.errors import ServicoIndisponivelError
from mcp_pje_tjpe.models import Grau
from mcp_pje_tjpe.pje_public import PjePublicClient, _npu_digits

NPU = "9999903-84.2099.8.17.9999"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _client(tmp_path: Path) -> PjePublicClient:
    config = Settings(data_dir=tmp_path / "data", downloads_dir=tmp_path / "downloads")
    return PjePublicClient(cast(BrowserManager, object()), config)


def _pagina_com_mascara(*, atraso_ms: int) -> str:
    """Reproduz o campo mascarado do TJPE, com a máscara montada por script.

    Até o script assumir, o campo aceita qualquer coisa; depois dele, a digitação é
    reformatada. O atraso simula o intervalo entre o campo ficar visível e a máscara
    entrar em vigor — a corrida que fazia a consulta falhar de forma intermitente.
    """
    return f"""
    <!doctype html><html><body>
      <label for="proc">Processo</label>
      <input id="proc" name="proc" value="" />
      <script>
        const campo = document.getElementById('proc');
        // Antes da máscara: engole os dígitos, como um campo ainda não inicializado.
        campo.addEventListener('input', function pre() {{ campo.value = ''; }});
        setTimeout(() => {{
          const novo = campo.cloneNode(true);
          campo.replaceWith(novo);
          novo.addEventListener('input', () => {{
            const d = novo.value.replace(/\\D/g, '').slice(0, 20);
            novo.value = d.length < 8 ? d
              : d.slice(0,7) + '-' + d.slice(7,9) +
                (d.length > 9 ? '.' + d.slice(9,13) : '') +
                (d.length > 13 ? '.' + d.slice(13,14) : '') +
                (d.length > 14 ? '.' + d.slice(14,16) : '') +
                (d.length > 16 ? '.' + d.slice(16,20) : '');
          }});
        }}, {atraso_ms});
      </script>
    </body></html>
    """


@pytest.mark.anyio
async def test_second_attempt_recovers_the_mask_race(tmp_path: Path) -> None:
    client = _client(tmp_path)
    async with async_playwright() as playwright:
        browser: Browser = await playwright.chromium.launch(headless=True)
        try:
            page: Page = await browser.new_page()
            await page.set_content(_pagina_com_mascara(atraso_ms=250))
            await page.get_by_label("Processo", exact=True).wait_for(state="visible")

            # A primeira digitação cai antes da máscara e é engolida; a segunda acerta.
            await client._type_exact_npu(page, _npu_digits(NPU))

            valor = await page.get_by_label("Processo", exact=True).input_value()
            assert _npu_digits(valor) == _npu_digits(NPU)
            assert valor == NPU
        finally:
            await browser.close()


@pytest.mark.anyio
async def test_a_field_that_never_accepts_the_npu_still_fails_closed(tmp_path: Path) -> None:
    client = _client(tmp_path)
    async with async_playwright() as playwright:
        browser: Browser = await playwright.chromium.launch(headless=True)
        try:
            page: Page = await browser.new_page()
            # Campo que trunca sempre: a garantia de "exatamente o NPU" não pode ceder.
            await page.set_content(
                """
                <!doctype html><html><body>
                  <label for="proc">Processo</label>
                  <input id="proc" name="proc" value="" />
                  <script>
                    const campo = document.getElementById('proc');
                    campo.addEventListener('input', () => {
                      campo.value = campo.value.replace(/\\D/g, '').slice(0, 12);
                    });
                  </script>
                </body></html>
                """
            )
            await page.get_by_label("Processo", exact=True).wait_for(state="visible")

            with pytest.raises(ServicoIndisponivelError, match="máscara de processo"):
                await client._type_exact_npu(page, _npu_digits(NPU))
        finally:
            await browser.close()


@pytest.mark.anyio
async def test_rate_limited_202_is_named_instead_of_reported_as_a_raw_status(
    tmp_path: Path,
) -> None:
    """202 com corpo vazio é o limitador do TJPE, não indisponibilidade do tribunal."""
    config = Settings(
        data_dir=tmp_path / "data",
        downloads_dir=tmp_path / "downloads",
        headless=True,
    )
    manager = BrowserManager(config)
    client = PjePublicClient(manager, config)
    try:
        context = await manager.new_context()
        await context.route(
            "**/ConsultaPublica/listView.seam*",
            lambda route: route.fulfill(status=202, content_type="text/html", body=""),
        )

        async def apenas_este_contexto() -> AsyncGenerator[Page]:
            page = await context.new_page()
            try:
                yield page
            finally:
                await page.close()

        client.browser = cast(  # pyright: ignore[reportAttributeAccessIssue]
            BrowserManager,
            type("_Fixo", (), {"page": staticmethod(asynccontextmanager(apenas_este_contexto))})(),
        )

        with pytest.raises(ServicoIndisponivelError) as reportado:
            await client.consultar(NPU, Grau.PRIMEIRO)

        mensagem = str(reportado.value)
        assert "limitou o acesso" in mensagem
        assert "aguarde" in mensagem
        await context.close()
    finally:
        await manager.close()
