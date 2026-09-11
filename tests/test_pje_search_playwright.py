from __future__ import annotations

# pyright: reportPrivateUsage=false
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest
from playwright.async_api import Browser, Page, Route, async_playwright

from mcp_pje_tjpe.adaptive import AdapterRegistry, AdaptiveNavigation, ObservationStore
from mcp_pje_tjpe.config import ModoAdaptativo, Settings
from mcp_pje_tjpe.errors import InterfacePjeAlteradaError, ValidacaoError
from mcp_pje_tjpe.models import Grau
from mcp_pje_tjpe.pje_auth import PjeSessionManager, ReadSessionLease
from mcp_pje_tjpe.pje_read import PjeReadService
from mcp_pje_tjpe.pje_search import (
    PjeSearchService,
    _click_exact_result,
    _configure_exact_npu_form,
    _DialogState,
    _extract_exact_result,
    _SearchGate,
    _SearchResult,
    _UiSession,
    _validated_autos_html,
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


def _search_form(numero_fields: str, *, extra: str = "") -> str:
    action = "https://pje.cloud.tjpe.jus.br/1g/Processo/ConsultaProcesso/listView.seam"
    return f"""
        <!doctype html>
        <html><body>
          <form id="consulta" name="consulta" method="post" action="{action}">
            <input type="hidden" name="consulta" value="consulta">
            <input type="hidden" name="javax.faces.ViewState" value="view-state-safe">
            {numero_fields}
            {extra}
            <button type="submit" name="consulta:pesquisar">Pesquisar</button>
          </form>
        </body></html>
    """


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def browser() -> AsyncIterator[Browser]:
    async with async_playwright() as playwright:
        instance = await playwright.chromium.launch(headless=True)
        try:
            yield instance
        finally:
            await instance.close()


@pytest.mark.anyio
async def test_interactive_session_registers_gate_with_real_playwright(browser: Browser) -> None:
    numero = _synthetic_npu()
    authenticated = await browser.new_context()
    page = await authenticated.new_page()
    await authenticated.add_cookies(
        [
            {
                "name": "JSESSIONID",
                "value": "test-session",
                "url": "https://pje.cloud.tjpe.jus.br/1g/",
            }
        ]
    )
    lease = ReadSessionLease(
        grau=Grau.PRIMEIRO,
        context=authenticated,
        page=page,
        generation="generation-a",
    )
    service = PjeSearchService(
        cast(PjeSessionManager, object()),
        cast(PjeReadService, object()),
        Settings(timeout_ms=1_000),
    )
    try:
        async with service._interactive_session(lease, numero) as ui:
            assert not ui.page.is_closed()
            assert ui.gate.grau is Grau.PRIMEIRO
    finally:
        await authenticated.close()


@pytest.mark.anyio
async def test_interactive_session_rejects_access_dialog_from_foreign_popup(
    browser: Browser,
) -> None:
    numero = _synthetic_npu()
    authenticated = await browser.new_context()
    page = await authenticated.new_page()
    await authenticated.add_cookies(
        [
            {
                "name": "JSESSIONID",
                "value": "test-session",
                "url": "https://pje.cloud.tjpe.jus.br/1g/",
            }
        ]
    )
    lease = ReadSessionLease(
        grau=Grau.PRIMEIRO,
        context=authenticated,
        page=page,
        generation="generation-a",
    )
    service = PjeSearchService(
        cast(PjeSessionManager, object()),
        cast(PjeReadService, object()),
        Settings(timeout_ms=1_000),
    )
    try:
        async with service._interactive_session(lease, numero) as ui:
            ui.dialogs.allow_access_notice = True
            await ui.page.set_content(
                """
                <button id="open" onclick="
                  const foreign = window.open('about:blank', 'foreign-popup');
                  const accepted = foreign.confirm(
                    'Resolução CNJ 121: o acesso será registrado como pedido de acesso.'
                  );
                  foreign.name = accepted ? 'continued' : 'dismissed';
                ">Abrir</button>
                """
            )
            await ui.page.locator("#open").click()
            await ui.page.wait_for_timeout(100)

            foreign = next(page for page in ui.context.pages if page is not ui.page)
            assert ui.dialogs.accepted_access_notices == 0
            # Chromium may dismiss a popup dialog before Playwright delivers the
            # new-page callback. In both schedules the foreign popup remains an
            # unexpected page and the access notice is never accepted.
            assert foreign in ui.observed_pages
            assert await foreign.evaluate("window.name") == "dismissed"
    finally:
        await authenticated.close()


@pytest.mark.anyio
async def test_exact_npu_form_accepts_only_single_or_six_audited_fields(
    browser: Browser,
) -> None:
    numero = _synthetic_npu()
    context = await browser.new_context()
    page = await context.new_page()
    try:
        await page.set_content(
            _search_form(
                '<label for="numeroProcesso">Processo</label>'
                '<input id="numeroProcesso" name="consulta:numeroProcesso" '
                'type="text" maxlength="25">'
            )
        )
        single = await _configure_exact_npu_form(page, numero, Grau.PRIMEIRO)
        assert single.fields["consulta:numeroProcesso"] == numero
        assert single.marker == "consulta:pesquisar"

        digits = "".join(character for character in numero if character.isdigit())
        parts = (digits[:7], digits[7:9], digits[9:13], "8", "17", digits[16:20])
        fields = "".join(
            (
                f'<input id="numeroProcesso{index}" '
                f'name="consulta:numeroProcesso{index}" type="text" '
                f'maxlength="{len(value)}" value="{value if index in {3, 4} else ""}" '
                f"{'readonly' if index in {3, 4} else ''}>"
            )
            for index, value in enumerate(parts)
        )
        await page.set_content(_search_form(fields))
        segmented = await _configure_exact_npu_form(page, numero, Grau.PRIMEIRO)
        for index in (0, 1, 2, 5):
            assert segmented.fields[f"consulta:numeroProcesso{index}"] == parts[index]
    finally:
        await context.close()


@pytest.mark.anyio
async def test_active_adapter_accepts_only_reviewed_semantic_form_variants(
    browser: Browser,
    tmp_path: Path,
) -> None:
    numero = _synthetic_npu()
    context = await browser.new_context()
    page = await context.new_page()
    html = _search_form(
        '<label for="numeroUnico">Numeração única</label>'
        '<input id="numeroUnico" name="consulta:numeroUnico" type="text" maxlength="25">'
    ).replace(
        '<button type="submit" name="consulta:pesquisar">Pesquisar</button>',
        '<button type="submit" name="consulta:consultar" value="Consultar">Consultar</button>',
    )
    try:
        await page.set_content(html)
        with pytest.raises(InterfacePjeAlteradaError, match="submit nativo Pesquisar"):
            await _configure_exact_npu_form(page, numero, Grau.PRIMEIRO)

        adaptive = AdaptiveNavigation(
            AdapterRegistry(),
            ObservationStore(tmp_path),
            ModoAdaptativo.ACTIVE,
        )
        contract = await _configure_exact_npu_form(
            page,
            numero,
            Grau.PRIMEIRO,
            adaptive=adaptive,
        )

        assert contract.marker == "consulta:consultar"
        assert contract.marker_value == "Consultar"
        assert contract.fields["consulta:numeroUnico"] == numero
    finally:
        await context.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("numero_field", "extra", "message"),
    [
        (
            '<input id="numeroProcesso" name="consulta:numeroProcesso" type="text">',
            '<input id="nomeParte" name="consulta:nomeParte" type="text" value="Alice">',
            "outro critério textual",
        ),
        (
            '<input id="numeroProcesso" name="consulta:numeroProcesso" type="text">',
            '<input type="hidden" name="consulta:protocolar" value="false">',
            "ação processual",
        ),
        (
            '<input id="numeroProcesso" name="consulta:numeroProcesso" type="text">',
            '<input type="hidden" name="consulta:componenteDesconhecido" value="acionar">',
            "fora da lista consultiva",
        ),
        (
            '<input id="numeroProcesso" name="consulta:numeroProcesso" type="text">',
            '<input type="hidden" name="consulta:componenteDesconhecido" value="">',
            "fora da lista consultiva",
        ),
        (
            '<input id="numeroProcesso" type="text">',
            "",
            "nome auditável",
        ),
    ],
)
async def test_exact_npu_form_rejects_other_criteria_and_action_fields(
    browser: Browser,
    numero_field: str,
    extra: str,
    message: str,
) -> None:
    context = await browser.new_context()
    page = await context.new_page()
    try:
        await page.set_content(_search_form(numero_field, extra=extra))
        with pytest.raises(InterfacePjeAlteradaError, match=message):
            await _configure_exact_npu_form(page, _synthetic_npu(), Grau.PRIMEIRO)
    finally:
        await context.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ('name="consulta:pesquisar" value="Excluir"', "valor do controle"),
        ('name="consulta:searchDelete"', "ação não permitida"),
        ('name="action"', "action da pesquisa|semanticamente uma pesquisa"),
    ],
)
async def test_exact_npu_form_requires_semantic_submit_name_and_search_value(
    browser: Browser,
    replacement: str,
    message: str,
) -> None:
    context = await browser.new_context()
    page = await context.new_page()
    try:
        html = _search_form(
            '<input id="numeroProcesso" name="consulta:numeroProcesso" type="text">'
        ).replace('name="consulta:pesquisar"', replacement)
        await page.set_content(html)
        with pytest.raises(InterfacePjeAlteradaError, match=message):
            await _configure_exact_npu_form(page, _synthetic_npu(), Grau.PRIMEIRO)
    finally:
        await context.close()


def _result_html(numero: str, *, context: str = "", links: int = 1) -> str:
    href = (
        "https://pje.cloud.tjpe.jus.br/1g/Processo/ConsultaProcesso/"
        "Detalhe/listProcessoCompletoAdvogado.seam?id=20&amp;ca=segredo"
    )
    anchors = "".join(f'<a href="{href}">{numero}</a>' for _ in range(links))
    return (
        "<!doctype html><html><body><table><tr><td>"
        f"{anchors} {context}</td></tr></table></body></html>"
    )


@pytest.mark.anyio
async def test_exact_result_is_unique_unrestricted_and_bound_to_one_npu(
    browser: Browser,
) -> None:
    numero = _synthetic_npu()
    context = await browser.new_context()
    page = await context.new_page()
    try:
        await page.set_content(_result_html(numero))
        result = await _extract_exact_result(page, numero, Grau.PRIMEIRO)
        assert result.numero == numero
        assert result.processo_id == "20"
        assert "segredo" not in repr(result)
        assert "autos_url" not in repr(result)

        for html, error, message in (
            (_result_html(numero, links=2), InterfacePjeAlteradaError, "único link"),
            (
                _result_html(numero, context=_synthetic_npu("9999998")),
                InterfacePjeAlteradaError,
                "conflitante",
            ),
            (
                _result_html(numero, context="Segredo de justiça: Sim"),
                ValidacaoError,
                "sigilo",
            ),
            (
                _result_html(numero, context="Processo em segredo de justiça"),
                ValidacaoError,
                "sigilo",
            ),
            (
                _result_html(numero, context="Acesso negado"),
                ValidacaoError,
                "sigilo",
            ),
            (
                _result_html(numero, context="Página 1 de 2"),
                InterfacePjeAlteradaError,
                "paginação",
            ),
        ):
            await page.set_content(html)
            with pytest.raises(error, match=message):
                await _extract_exact_result(page, numero, Grau.PRIMEIRO)
    finally:
        await context.close()


def _autos_html(numero: str, *, notice: str = "", body_extra: str = "") -> str:
    return f"""
        <!doctype html><html><body>
          <h1>{numero}</h1>
          <div>Segredo de justiça: Não</div>
          {notice}
          <form id="divTimeLine">
            <div class="media tipo-M">Protocolo juntado como movimento histórico</div>
            <div class="media tipo-D">
              <a onclick="abrirLinkDocumento('321')">Petição inicial</a>
            </div>
          </form>
          {body_extra}
        </body></html>
    """


@pytest.mark.anyio
async def test_autos_validation_blocks_notices_but_not_historical_movement(
    browser: Browser,
) -> None:
    numero = _synthetic_npu()
    target = (
        "https://pje.cloud.tjpe.jus.br/1g/Processo/ConsultaProcesso/"
        "Detalhe/listProcessoCompletoAdvogado.seam?id=20"
    )
    context = await browser.new_context()
    page = await context.new_page()

    async def fulfill(route: Route) -> None:
        await route.fulfill(status=200, content_type="text/html", body=_autos_html(numero))

    await context.route(target, fulfill)
    try:
        await page.goto(target)
        html = await _validated_autos_html(page, numero, Grau.PRIMEIRO, "20")
        assert numero in html

        await page.set_content(
            _autos_html(
                numero,
                notice='<div class="mensagem-aviso">Tomar ciência da intimação pendente</div>',
            )
        )
        with pytest.raises(ValidacaoError, match="ciência"):
            await _validated_autos_html(page, numero, Grau.PRIMEIRO, "20")

        await page.set_content(_autos_html(numero, body_extra="Segredo de justiça: Sim"))
        with pytest.raises(ValidacaoError, match="sigilo"):
            await _validated_autos_html(page, numero, Grau.PRIMEIRO, "20")

        await page.set_content(_autos_html(numero, body_extra="Acesso negado"))
        with pytest.raises(ValidacaoError, match="negativa de acesso"):
            await _validated_autos_html(page, numero, Grau.PRIMEIRO, "20")
    finally:
        await context.close()


@pytest.mark.anyio
async def test_exact_click_rejects_any_additional_popup(browser: Browser) -> None:
    numero = _synthetic_npu()
    target = (
        "https://pje.cloud.tjpe.jus.br/1g/Processo/ConsultaProcesso/"
        "Detalhe/listProcessoCompletoAdvogado.seam?id=20"
    )
    context = await browser.new_context()
    observed_pages: list[Page] = []

    def observe_page(observed: Page) -> None:
        observed_pages.append(observed)

    context.on("page", observe_page)
    page = await context.new_page()

    async def fulfill(route: Route) -> None:
        await route.fulfill(status=200, content_type="text/html", body=_autos_html(numero))

    await context.route(target, fulfill)
    try:
        await page.set_content(
            f"""
            <!doctype html><html><body>
              <a id="resultado" href="{target}"
                 onclick="window.open('about:blank'); window.location.href=this.href; return false">
                {numero}
              </a>
            </body></html>
            """
        )
        ui = _UiSession(
            context=context,
            page=page,
            gate=_SearchGate(Grau.PRIMEIRO),
            dialogs=_DialogState(numero),
            observed_pages=observed_pages,
        )
        result = _SearchResult(
            numero=numero,
            processo_id="20",
            autos_url=target,
            fingerprint="fingerprint",
            anchor=page.locator("#resultado"),
        )

        with pytest.raises(InterfacePjeAlteradaError, match="página adicional"):
            await _click_exact_result(ui, result, timeout_ms=2_000)
        assert len(observed_pages) >= 2
    finally:
        await context.close()
