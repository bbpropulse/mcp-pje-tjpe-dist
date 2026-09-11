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
from mcp_pje_tjpe.pje_read import PjeReadService, _AcervoBinding

# Estrutura colhida do Acervo real do TJPE em 08/09/2026. Dois detalhes que importam:
# a mesma tela traz um "Peticionar" por linha de processo ao lado do "Pesquisar", e os
# critérios ficam num dropdown sem visibilidade até o alternador ser acionado — havia
# também um segundo ícone de lupa, de busca por número, que não pode ser confundido.
_ACERVO = """
<!doctype html><html><head><style>
  .dropdown-menu { visibility: hidden; }
  li.dropdown.aberto .dropdown-menu { visibility: visible; }
</style></head><body>
  <form id="formAcervo">
   <div id="formAcervo:filtros">
    <ul class="nav navbar-nav menu-icones-topo">
     <li class="dropdown drop-menu">
      <a class="btn-flat" id="btnPesquisarContexto"
         title="Pesquisar">Ícone de lupa</a>
      <a class="btn-menu-abas dropdown-toggle"
         title="Pesquisar nesta caixa">Ícone de pesquisa</a>
      <div id="formAcervo:divCampos" class="dropdown-menu">
       <input name="formAcervo:itDestPend" type="text" title="Informe o nome da parte." />
       <input name="formAcervo:itIMF" type="text" title="Insira o CPF, CNPJ..." />
       <input name="formAcervo:itOAB" type="text" title="Insira o número da OAB..." />
       <input name="formAcervo:itCL" type="text" title="Insira o código da classe..." />
       <input name="formAcervo:itAS" type="text" title="Insira o código do assunto..." />
       <input id="formAcervo:btPesqAc" type="button" value="Pesquisar" />
      </div>
     </li>
    </ul>
   </div>
    <div id="divListaAcervo"><a href="/1g/p?id=1">processo</a></div>
    <input id="formAcervo:tbProcessos:1:pet" type="button" value="Peticionar" />
    <input id="formAcervo:tbProcessos:2:pet" type="button" value="Peticionar" />
  </form>
  <script>
    window.acoes = [];
    for (const a of document.querySelectorAll('a')) {
      a.addEventListener('click', () => {
        window.acoes.push(a.getAttribute('title'));
        if (a.classList.contains('dropdown-toggle')) {
          a.closest('li.dropdown').classList.add('aberto');
        }
      });
    }
    for (const b of document.querySelectorAll('input[type=button]')) {
      b.addEventListener('click', () => {
        window.acoes.push(b.getAttribute('id'));
        if (b.getAttribute('id') !== 'formAcervo:btPesqAc') return;
        document.getElementById('divListaAcervo').innerHTML =
          '<a href="/1g/p?id=9">achado</a><a href="/1g/p?id=10">achado</a>';
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


class _FakeSessions:
    def __init__(self, lease: ReadSessionLease) -> None:
        self.lease = lease

    @asynccontextmanager
    async def read_session(self, grau: Grau) -> AsyncGenerator[ReadSessionLease]:
        assert grau is self.lease.grau
        yield self.lease


def _service(tmp_path: Path, page: Page | None = None) -> PjeReadService:
    lease = ReadSessionLease(
        grau=Grau.PRIMEIRO,
        context=cast(BrowserContext, object()),
        page=cast(Page, page if page is not None else object()),
        generation="generation-a",
    )
    return PjeReadService(
        cast(PjeSessionManager, _FakeSessions(lease)),
        Settings(
            data_dir=tmp_path / "data",
            downloads_dir=tmp_path / "downloads",
            timeout_ms=3_000,
        ),
    )


@pytest.mark.anyio
async def test_only_the_search_button_is_ever_clicked(tmp_path: Path) -> None:
    """A tela tem um Peticionar por linha; escolher botão por rótulo seria temerário."""
    async with _pagina() as page:
        await page.set_content(_ACERVO)

        await _service(tmp_path)._pesquisar_no_acervo(page, {"parte": "Bradesco"})

        # Só dois toques: abrir a área de busca e disparar a pesquisa. Nenhum dos
        # "Peticionar" que dividem a tela com ela é tocado.
        assert await page.evaluate("window.acoes") == [
            "Pesquisar nesta caixa",
            "formAcervo:btPesqAc",
        ]


@pytest.mark.anyio
async def test_each_criterion_lands_in_its_own_field(tmp_path: Path) -> None:
    async with _pagina() as page:
        await page.set_content(_ACERVO)

        await _service(tmp_path)._pesquisar_no_acervo(
            page, {"parte": "Maria", "documento": "12345678000195", "classe": "Cível"}
        )

        valores = await page.evaluate(
            """() => Object.fromEntries(
                Array.from(document.querySelectorAll('#formAcervo input[type=text]'))
                  .map((el) => [el.getAttribute('name'), el.value])
            )"""
        )
        assert valores["formAcervo:itDestPend"] == "Maria"
        assert valores["formAcervo:itIMF"] == "12345678000195"
        assert valores["formAcervo:itCL"] == "Cível"
        # O que não foi pedido continua vazio: nada é preenchido por conta própria.
        assert valores["formAcervo:itOAB"] == ""
        assert valores["formAcervo:itAS"] == ""


@pytest.mark.anyio
async def test_a_renamed_search_button_stops_the_search(tmp_path: Path) -> None:
    """Se o id fixado passar a apontar para outra ação, é melhor falhar do que clicar."""
    async with _pagina() as page:
        await page.set_content(_ACERVO.replace('value="Pesquisar"', 'value="Peticionar"'))

        with pytest.raises(InterfacePjeAlteradaError, match="deixou de ser Pesquisar"):
            await _service(tmp_path)._pesquisar_no_acervo(page, {"parte": "x"})

        # Abrir a área de busca é inevitável para alcançar os campos; o que não pode
        # acontecer é o clique seguir para uma ação processual.
        assert await page.evaluate("window.acoes") == ["Pesquisar nesta caixa"]


@pytest.mark.anyio
async def test_a_missing_form_or_field_fails_closed(tmp_path: Path) -> None:
    async with _pagina() as page:
        await page.set_content("<!doctype html><html><body></body></html>")
        with pytest.raises(InterfacePjeAlteradaError, match="formulário de busca"):
            await _service(tmp_path)._pesquisar_no_acervo(page, {"parte": "x"})

    async with _pagina() as page:
        await page.set_content(
            _ACERVO.replace('name="formAcervo:itOAB"', 'name="formAcervo:outroNome"')
        )
        with pytest.raises(InterfacePjeAlteradaError, match="'oab' não existe mais"):
            await _service(tmp_path)._pesquisar_no_acervo(page, {"oab": "12345"})


@pytest.mark.anyio
async def test_searching_requires_a_jurisdiction(tmp_path: Path) -> None:
    """A busca roda dentro de uma jurisdição; sem ela não há lista onde pesquisar."""
    with pytest.raises(ValidacaoError, match="informe 'jurisdicao'"):
        await _service(tmp_path).listar_acervo(Grau.PRIMEIRO, parte="Bradesco")


@pytest.mark.anyio
async def test_blank_criteria_are_ignored_rather_than_submitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Critério em branco não é critério: submetê-lo pediria o acervo inteiro de volta."""
    async with _pagina() as page:
        await page.set_content(_ACERVO)
        service = _service(tmp_path, page)

        async def sem_navegar(*_a: object, **_k: object) -> None:
            return None

        monkeypatch.setattr(service, "_load_acervo", sem_navegar)
        async def sem_jurisdicoes(_page: Page) -> list[object]:
            return []

        monkeypatch.setattr(service, "_acervo_jurisdicoes", sem_jurisdicoes)

        # Só espaços: se contassem como critério, o erro seria o da busca sem
        # jurisdição. Sendo ignorados, cai no erro normal de jurisdição ausente.
        with pytest.raises(ValidacaoError, match="organizado por jurisdição"):
            await service.listar_acervo(Grau.PRIMEIRO, parte="   ", oab="")


def _vinculo(criterios: tuple[tuple[str, str], ...]) -> _AcervoBinding:
    return _AcervoBinding(
        generation="generation-a",
        grau=Grau.PRIMEIRO,
        # NPU sintético: sequência e ano impossíveis, como no resto da suíte.
        numero="9999999-02.2099.8.17.9999",
        processo_id="1",
        autos_url="https://pje.cloud.tjpe.jus.br/1g/autos",
        jurisdicao="Recife - TJPE",
        criterios=criterios,
    )


@pytest.mark.anyio
async def test_reloading_a_binding_replays_the_search_that_found_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Observado no TJPE: um processo achado por busca não abria.

    Revalidar recarregava só a jurisdição, que renderiza os primeiros processos dela.
    O processo achado pela pesquisa não estava entre eles e era dado por sumido — a
    busca encontrava processos que nunca poderiam ser abertos.
    """
    service = _service(tmp_path)
    chamadas: list[tuple[str, object]] = []

    async def fake_load(
        _page: Page, _grau: Grau, jurisdicao: str | None = None
    ) -> int | None:
        chamadas.append(("carregou", jurisdicao))
        return None

    async def fake_search(_page: Page, criterios: dict[str, str]) -> None:
        chamadas.append(("pesquisou", dict(criterios)))

    monkeypatch.setattr(service, "_load_acervo", fake_load)
    monkeypatch.setattr(service, "_pesquisar_no_acervo", fake_search)

    await service._repor_tela_do_vinculo(
        cast(Page, object()), Grau.PRIMEIRO, _vinculo((("parte", "SAUDE EXEMPLO"),))
    )

    assert chamadas == [
        ("carregou", "Recife - TJPE"),
        ("pesquisou", {"parte": "SAUDE EXEMPLO"}),
    ]


@pytest.mark.anyio
async def test_a_binding_without_a_search_reloads_without_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repetir busca que nunca houve seria inventar um filtro na revalidação."""
    service = _service(tmp_path)
    chamadas: list[str] = []

    async def fake_load(
        _page: Page, _grau: Grau, _jurisdicao: str | None = None
    ) -> int | None:
        chamadas.append("carregou")
        return None

    async def fake_search(_page: Page, _criterios: dict[str, str]) -> None:
        chamadas.append("pesquisou")

    monkeypatch.setattr(service, "_load_acervo", fake_load)
    monkeypatch.setattr(service, "_pesquisar_no_acervo", fake_search)

    await service._repor_tela_do_vinculo(cast(Page, object()), Grau.PRIMEIRO, _vinculo(()))

    assert chamadas == ["carregou"]
