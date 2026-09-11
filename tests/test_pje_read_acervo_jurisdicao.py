from __future__ import annotations

# pyright: reportPrivateUsage=false
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import pytest
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from mcp_pje_tjpe.config import Settings
from mcp_pje_tjpe.errors import InterfacePjeAlteradaError, ValidacaoError
from mcp_pje_tjpe.models import Grau
from mcp_pje_tjpe.pje_auth import PjeSessionManager, ReadSessionLease
from mcp_pje_tjpe.pje_read import (
    PjeReadService,
    _AcervoBinding,
    _click_visible_exact,
    _fold_text,
    _split_jurisdicao,
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


def _jurisdicao_link(index: int, label: str, *, hidden: bool = False) -> str:
    style = ' style="display:none"' if hidden else ""
    return f'<a id="formAbaAcervo:trAc:{index}:linkJur"{style} href="#">{label}</a>'


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@asynccontextmanager
async def _page() -> AsyncGenerator[Page]:
    async with async_playwright() as playwright:
        browser: Browser = await playwright.chromium.launch(headless=True)
        try:
            yield await browser.new_page()
        finally:
            await browser.close()


def _service(
    tmp_path: Path,
    sessions: object | None = None,
    *,
    timeout_ms: int = 30_000,
) -> PjeReadService:
    return PjeReadService(
        cast(PjeSessionManager, sessions if sessions is not None else object()),
        Settings(
            data_dir=tmp_path / "data",
            downloads_dir=tmp_path / "downloads",
            timeout_ms=timeout_ms,
        ),
    )


class _FakeReadSessions:
    def __init__(self, lease: ReadSessionLease) -> None:
        self.lease = lease

    @asynccontextmanager
    async def read_session(self, grau: Grau) -> AsyncGenerator[ReadSessionLease]:
        assert grau is self.lease.grau
        yield self.lease


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Recife", "  recife  "),
        ("São Lourenço da Mata", "sao lourenco da mata"),
        ("JABOATÃO   DOS\nGUARARAPES", "Jaboatão dos Guararapes"),
    ],
)
def test_fold_text_normalizes_accents_case_and_spacing(left: str, right: str) -> None:
    assert _fold_text(left) == _fold_text(right)


def test_fold_text_keeps_distinct_labels_distinct() -> None:
    assert _fold_text("Recife") != _fold_text("Recife - Fórum Rodolfo Aureliano")


@pytest.mark.anyio
async def test_click_visible_exact_reaches_richfaces_tab_cell_rendered_late() -> None:
    async with _page() as page:
        await page.set_content(
            """
            <!doctype html><html><body>
              <div id="painel"></div>
              <script>
                window.cliques = [];
                setTimeout(() => {
                  const cell = document.createElement('td');
                  cell.className = 'rich-tab-header';
                  cell.textContent = 'Acervo';
                  cell.onclick = () => window.cliques.push('Acervo');
                  document.getElementById('painel').appendChild(cell);
                }, 400);
              </script>
            </body></html>
            """
        )

        # Sem espera o controle ainda não existe: a aba RichFaces é renderizada depois.
        assert await _click_visible_exact(page, "Acervo") is False
        assert await _click_visible_exact(page, "Acervo", timeout_ms=5_000) is True
        assert await page.evaluate("window.cliques") == ["Acervo"]
        # A comparação continua exata: 'Acerv' não pode casar com 'Acervo'.
        assert await _click_visible_exact(page, "Acerv", timeout_ms=300) is False


@pytest.mark.anyio
async def test_acervo_jurisdicoes_lists_visible_labels_deduplicated_and_sorted() -> None:
    async with _page() as page:
        await page.set_content(
            f"""
            <!doctype html><html><body>
              {_jurisdicao_link(0, "Recife")}
              {_jurisdicao_link(1, "Jaboatão dos Guararapes")}
              {_jurisdicao_link(2, "Recife")}
              {_jurisdicao_link(3, "Olinda", hidden=True)}
              <a id="outro-link" href="#">Caruaru</a>
            </body></html>
            """
        )

        assert [
            (item.nome, item.processos)
            for item in await PjeReadService._acervo_jurisdicoes(page)
        ] == [("Jaboatão dos Guararapes", None), ("Recife", None)]


@pytest.mark.anyio
async def test_select_acervo_jurisdicao_rejects_blank_ambiguous_and_unknown(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    async with _page() as page:
        await page.set_content(
            f"""
            <!doctype html><html><body>
              {_jurisdicao_link(0, "Recife - Varas")}
              {_jurisdicao_link(1, "Recife - Juizados")}
              {_jurisdicao_link(2, "Olinda")}
            </body></html>
            """
        )

        with pytest.raises(ValidacaoError, match="informe a jurisdição"):
            await service._select_acervo_jurisdicao(page, "   ")

        # "Recife" não nomeia nenhuma delas; é só prefixo de duas.
        with pytest.raises(ValidacaoError, match="ambígua"):
            await service._select_acervo_jurisdicao(page, "Recife")

        with pytest.raises(ValidacaoError, match="não aparece no seu Acervo") as unknown:
            await service._select_acervo_jurisdicao(page, "Caruaru")
        assert "Olinda" in str(unknown.value)
        assert "Recife" in str(unknown.value)


@pytest.mark.anyio
async def test_select_acervo_jurisdicao_clicks_match_ignoring_accents_and_waits(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    numero = _synthetic_npu()
    async with _page() as page:
        await page.set_content(
            f"""
            <!doctype html><html><body>
              {_jurisdicao_link(0, "Jaboatão dos Guararapes")}
              {_jurisdicao_link(1, "Olinda")}
              <div id="divListaAcervo"></div>
              <script>
                window.escolhida = null;
                for (const link of document.querySelectorAll('a[id^="formAbaAcervo:trAc:"]')) {{
                  link.onclick = () => {{
                    window.escolhida = link.innerText;
                    setTimeout(() => {{
                      document.getElementById('divListaAcervo').innerHTML =
                        '<a href="/1g/Processo/ConsultaProcesso/Detalhe/' +
                        'listProcessoCompletoAdvogado.seam?id=20">{numero}</a>';
                    }}, 300);
                  }};
                }}
              </script>
            </body></html>
            """
        )

        await service._select_acervo_jurisdicao(page, "jaboatao dos guararapes")

        assert await page.evaluate("window.escolhida") == "Jaboatão dos Guararapes"
        assert await page.locator("#divListaAcervo a").count() == 1


@pytest.mark.anyio
async def test_extract_acervo_entries_falls_back_to_lista_container_without_aria() -> None:
    numero = _synthetic_npu()
    async with _page() as page:
        await page.set_content(
            f"""
            <!doctype html><html><body>
              <table class="rich-tabpanel"><tr>
                <td class="rich-tab-header">Acervo</td>
                <td class="rich-tab-header">Expedientes</td>
              </tr></table>
              <div id="divListaAcervo">
                <table><tbody><tr>
                  <td>{numero}</td>
                  <td><a id="autos" href="/1g/Processo/ConsultaProcesso/Detalhe/
                    listProcessoCompletoAdvogado.seam?id=20">Autos</a></td>
                </tr></tbody></table>
              </div>
              <aside id="fora-da-lista">
                <a id="atalho" href="/1g/Processo/ConsultaProcesso/Detalhe/
                  listProcessoCompletoAdvogado.seam?id=21">{numero}</a>
              </aside>
            </body></html>
            """
        )

        entries = await PjeReadService._extract_acervo_entries(page)

        # O painel RichFaces não expõe ARIA: a raiz vem do container de resultados,
        # e o que está fora dele continua ignorado.
        assert len(entries) == 1
        assert entries[0]["text"] == "Autos"
        assert numero in entries[0]["context"]
        assert entries[0]["href"].endswith("listProcessoCompletoAdvogado.seam?id=20")


@pytest.mark.anyio
async def test_extract_acervo_entries_still_requires_a_known_root(tmp_path: Path) -> None:
    _ = tmp_path
    async with _page() as page:
        await page.set_content(
            """
            <!doctype html><html><body>
              <table class="rich-tabpanel"><tr>
                <td class="rich-tab-header">Acervo</td>
              </tr></table>
              <div id="painel-desconhecido"></div>
            </body></html>
            """
        )

        with pytest.raises(InterfacePjeAlteradaError, match="raiz estrutural"):
            await PjeReadService._extract_acervo_entries(page)


@pytest.mark.anyio
async def test_listar_acervo_without_jurisdicao_reports_available_ones(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _page() as page:
        await page.set_content(
            f"""
            <!doctype html><html><body>
              {_jurisdicao_link(0, "Recife")}
              {_jurisdicao_link(1, "Olinda")}
            </body></html>
            """
        )
        lease = ReadSessionLease(
            grau=Grau.PRIMEIRO,
            context=cast(BrowserContext, object()),
            page=page,
            generation="generation-a",
        )
        service = _service(tmp_path, _FakeReadSessions(lease))
        carregado: list[str | None] = []

        async def fake_load(
            actual_page: Page,
            grau: Grau,
            jurisdicao: str | None = None,
        ) -> None:
            assert actual_page is page
            assert grau is Grau.PRIMEIRO
            carregado.append(jurisdicao)

        monkeypatch.setattr(service, "_load_acervo", fake_load)

        with pytest.raises(ValidacaoError, match="organizado por jurisdição") as reported:
            await service.listar_acervo(Grau.PRIMEIRO)

        assert carregado == [None]
        assert "Olinda" in str(reported.value)
        assert "Recife" in str(reported.value)


@pytest.mark.anyio
async def test_listar_acervo_forwards_jurisdicao_and_validates_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _page() as page:
        await page.set_content("<!doctype html><html><body></body></html>")
        lease = ReadSessionLease(
            grau=Grau.PRIMEIRO,
            context=cast(BrowserContext, object()),
            page=page,
            generation="generation-a",
        )
        service = _service(tmp_path, _FakeReadSessions(lease))
        carregado: list[str | None] = []

        async def fake_load(
            actual_page: Page,
            grau: Grau,
            jurisdicao: str | None = None,
        ) -> None:
            _ = actual_page, grau
            carregado.append(jurisdicao)

        async def fake_entries(actual_page: Page) -> list[dict[str, str]]:
            _ = actual_page
            return []

        monkeypatch.setattr(service, "_load_acervo", fake_load)
        monkeypatch.setattr(service, "_extract_acervo_entries", fake_entries)

        # O limite é validado antes de qualquer navegação.
        with pytest.raises(ValidacaoError, match="limite do Acervo"):
            await service.listar_acervo(Grau.PRIMEIRO, limite=0, jurisdicao="Recife")
        assert carregado == []

        pagina = await service.listar_acervo(Grau.PRIMEIRO, jurisdicao="Recife")

        assert carregado == ["Recife"]
        assert pagina.processos == []


@pytest.mark.anyio
async def test_empty_jurisdiction_is_a_normal_state_not_a_changed_interface(
    tmp_path: Path,
) -> None:
    # O container de resultados aparece vazio: a jurisdição existe e simplesmente não
    # tem processos. Antes isso estourava InterfacePjeAlteradaError depois de 15 s.
    service = _service(tmp_path, timeout_ms=1_500)
    async with _page() as page:
        await page.set_content(
            f"""
            <!doctype html><html><body>
              {_jurisdicao_link(0, "Olinda")}
              <div id="divListaAcervo"></div>
            </body></html>
            """
        )

        await service._select_acervo_jurisdicao(page, "Olinda")

        assert await page.locator("#divListaAcervo a").count() == 0


@pytest.mark.anyio
async def test_missing_results_container_still_reports_a_changed_interface(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, timeout_ms=1_000)
    async with _page() as page:
        await page.set_content(
            f"""
            <!doctype html><html><body>
              {_jurisdicao_link(0, "Olinda")}
            </body></html>
            """
        )

        with pytest.raises(InterfacePjeAlteradaError, match="container de resultados"):
            await service._select_acervo_jurisdicao(page, "Olinda")


@pytest.mark.anyio
async def test_listar_jurisdicoes_acervo_reports_labels_without_selecting_any(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _page() as page:
        await page.set_content(
            f"""
            <!doctype html><html><body>
              {_jurisdicao_link(0, "Recife")}
              {_jurisdicao_link(1, "Olinda")}
              <div id="divListaAcervo"></div>
            </body></html>
            """
        )
        lease = ReadSessionLease(
            grau=Grau.PRIMEIRO,
            context=cast(BrowserContext, object()),
            page=page,
            generation="generation-a",
        )
        service = _service(tmp_path, _FakeReadSessions(lease))
        carregado: list[str | None] = []

        async def fake_load(
            actual_page: Page,
            grau: Grau,
            jurisdicao: str | None = None,
        ) -> None:
            _ = actual_page, grau
            carregado.append(jurisdicao)

        monkeypatch.setattr(service, "_load_acervo", fake_load)

        resultado = await service.listar_jurisdicoes_acervo(Grau.PRIMEIRO)

        # Nenhuma jurisdição é selecionada: a descoberta não navega.
        assert carregado == [None]
        assert [item.nome for item in resultado.jurisdicoes] == ["Olinda", "Recife"]
        assert resultado.grau is Grau.PRIMEIRO


# Rótulos reais colhidos do Acervo do TJPE em 04/09/2026: o painel cola a contagem
# de processos no texto do link, e ela muda quando o acervo muda.
@pytest.mark.parametrize(
    ("rotulo", "nome", "processos"),
    [
        ("Abreu e Lima - Varas 2", "Abreu e Lima - Varas", 2),
        ("Recife - Varas 1650", "Recife - Varas", 1650),
        ("Jaboatão dos Guararapes - Varas 127", "Jaboatão dos Guararapes - Varas", 127),
        ("Núcleos de Justiça 4.0 3", "Núcleos de Justiça 4.0", 3),
        ("Olinda - Juizados 1", "Olinda - Juizados", 1),
        ("Sem contagem", "Sem contagem", None),
    ],
)
def test_the_panel_count_is_split_out_of_the_jurisdiction_name(
    rotulo: str, nome: str, processos: int | None
) -> None:
    assert _split_jurisdicao(rotulo) == (nome, processos)


@pytest.mark.anyio
async def test_jurisdictions_report_a_stable_name_and_the_panel_count() -> None:
    async with _page() as page:
        await page.set_content(
            f"""
            <!doctype html><html><body>
              {_jurisdicao_link(0, "Recife - Varas 1650")}
              {_jurisdicao_link(1, "Olinda - Juizados 1")}
              {_jurisdicao_link(2, "Núcleos de Justiça 4.0 3")}
            </body></html>
            """
        )

        itens = await PjeReadService._acervo_jurisdicoes(page)

        # O nome não carrega a contagem: ele precisa continuar válido amanhã.
        assert [(item.nome, item.processos) for item in itens] == [
            ("Núcleos de Justiça 4.0", 3),
            ("Olinda - Juizados", 1),
            ("Recife - Varas", 1650),
        ]


@pytest.mark.anyio
async def test_selecting_by_the_stable_name_ignores_the_changing_count(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, timeout_ms=2_000)
    async with _page() as page:
        await page.set_content(
            f"""
            <!doctype html><html><body>
              {_jurisdicao_link(0, "Olinda - Varas 83")}
              {_jurisdicao_link(1, "Olinda - Juizados 1")}
              <div id="divListaAcervo"></div>
              <script>
                window.escolhida = null;
                for (const link of document.querySelectorAll('a[id^="formAbaAcervo:trAc:"]')) {{
                  link.onclick = () => {{ window.escolhida = link.innerText; }};
                }}
              </script>
            </body></html>
            """
        )

        await service._select_acervo_jurisdicao(page, "Olinda - Varas")

        assert await page.evaluate("window.escolhida") == "Olinda - Varas 83"


@pytest.mark.anyio
async def test_an_exact_name_wins_over_a_longer_one_that_starts_the_same(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, timeout_ms=2_000)
    async with _page() as page:
        await page.set_content(
            f"""
            <!doctype html><html><body>
              {_jurisdicao_link(0, "Recife - Varas 1650")}
              {_jurisdicao_link(1, "Recife - Varas Empresariais 12")}
              <div id="divListaAcervo"></div>
              <script>
                window.escolhida = null;
                for (const link of document.querySelectorAll('a[id^="formAbaAcervo:trAc:"]')) {{
                  link.onclick = () => {{ window.escolhida = link.innerText; }};
                }}
              </script>
            </body></html>
            """
        )

        await service._select_acervo_jurisdicao(page, "Recife - Varas")
        assert await page.evaluate("window.escolhida") == "Recife - Varas 1650"

        # Um prefixo que não é nome de ninguém continua ambíguo, com as candidatas.
        with pytest.raises(ValidacaoError, match="ambígua") as reportado:
            await service._select_acervo_jurisdicao(page, "Recife")
        assert "Recife - Varas Empresariais" in str(reportado.value)


@pytest.mark.anyio
async def test_reopening_the_autos_reloads_the_binding_jurisdiction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recarregar o Acervo sem jurisdição cai no seletor vazio e perde o processo.

    Observado contra o PJe real: listar_acervo registrava o vínculo, mas
    consultar_autos recarregava a aba sem escolher jurisdição nenhuma e então não
    reencontrava o link assinado — quebrado para todo processo.
    """
    numero = _synthetic_npu()
    async with _page() as page:
        await page.set_content("<!doctype html><html><body></body></html>")
        lease = ReadSessionLease(
            grau=Grau.PRIMEIRO,
            context=cast(BrowserContext, object()),
            page=page,
            generation="generation-a",
        )
        service = _service(tmp_path, _FakeReadSessions(lease))
        service._acervo[(lease.generation, Grau.PRIMEIRO, numero)] = _AcervoBinding(
            generation=lease.generation,
            grau=Grau.PRIMEIRO,
            numero=numero,
            processo_id="20",
            autos_url=(
                "https://pje.cloud.tjpe.jus.br/1g/Processo/ConsultaProcesso/Detalhe/"
                "listProcessoCompletoAdvogado.seam?id=20"
            ),
            jurisdicao="Abreu e Lima - Varas",
        )
        recarregado: list[str | None] = []

        async def fake_load(
            actual_page: Page, grau: Grau, jurisdicao: str | None = None
        ) -> None:
            _ = actual_page, grau
            recarregado.append(jurisdicao)

        async def fake_refresh(actual_lease: ReadSessionLease, actual_numero: str) -> None:
            _ = actual_lease, actual_numero

        async def fake_open(
            actual_lease: ReadSessionLease, actual_numero: str
        ) -> tuple[Page, BrowserContext, str]:
            _ = actual_lease, actual_numero
            return page, cast(BrowserContext, object()), "20"

        async def fake_model(*_args: object, **_kwargs: object) -> object:
            return object()

        monkeypatch.setattr(service, "_load_acervo", fake_load)
        monkeypatch.setattr(service, "_refresh_acervo_binding", fake_refresh)
        monkeypatch.setattr(service, "_open_autos_from_acervo", fake_open)
        monkeypatch.setattr(service, "_build_autos_model", fake_model)

        await service.consultar_autos(numero, Grau.PRIMEIRO)

        assert recarregado == ["Abreu e Lima - Varas"]


@pytest.mark.anyio
async def test_switching_jurisdictions_waits_for_the_previous_list_to_go(
    tmp_path: Path,
) -> None:
    """Nada limpa a lista antes do clique.

    Com uma jurisdição já carregada, os links dela seguem no DOM e uma espera por
    "existe link" é satisfeita na hora — pela lista errada. Medido: a seleção
    devolvia o controle com os processos da jurisdição anterior ainda na tela.
    """
    service = _service(tmp_path)
    async with _page() as page:
        await page.set_content(
            f"""
            <!doctype html><html><body>
              {_jurisdicao_link(0, "Jaboatão dos Guararapes")}
              {_jurisdicao_link(1, "Olinda")}
              <div id="divListaAcervo">
                <a href="/1g/Processo/ConsultaProcesso/Detalhe/
                   listProcessoCompletoAdvogado.seam?id=1">anterior</a>
              </div>
              <script>
                for (const link of document.querySelectorAll(
                    'a[id^="formAbaAcervo:trAc:"]')) {{
                  link.onclick = () => {{
                    setTimeout(() => {{
                      document.getElementById('divListaAcervo').innerHTML =
                        '<a href="/1g/Processo/ConsultaProcesso/Detalhe/' +
                        'listProcessoCompletoAdvogado.seam?id=2">nova</a>';
                    }}, 400);
                  }};
                }}
              </script>
            </body></html>
            """
        )

        await service._select_acervo_jurisdicao(page, "olinda")

        assert await page.locator("#divListaAcervo").inner_text() == "nova"


@pytest.mark.anyio
async def test_the_first_selection_of_a_session_has_no_previous_list(
    tmp_path: Path,
) -> None:
    """Sem lista anterior não há troca a esperar: vale o primeiro link aparecer."""
    service = _service(tmp_path)
    async with _page() as page:
        await page.set_content(
            f"""
            <!doctype html><html><body>
              {_jurisdicao_link(0, "Olinda")}
              <div id="divListaAcervo"></div>
              <script>
                document.querySelector('a[id^="formAbaAcervo:trAc:"]').onclick = () => {{
                  setTimeout(() => {{
                    document.getElementById('divListaAcervo').innerHTML =
                      '<a href="/1g/Processo/ConsultaProcesso/Detalhe/' +
                      'listProcessoCompletoAdvogado.seam?id=2">primeira</a>';
                  }}, 300);
                }};
              </script>
            </body></html>
            """
        )

        await service._select_acervo_jurisdicao(page, "olinda")

        assert await page.locator("#divListaAcervo").inner_text() == "primeira"
