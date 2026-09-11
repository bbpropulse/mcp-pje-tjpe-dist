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
from mcp_pje_tjpe.pje_read import PjeReadService


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


class _FakeSessions:
    def __init__(self, lease: ReadSessionLease) -> None:
        self.lease = lease

    @asynccontextmanager
    async def read_session(self, grau: Grau) -> AsyncGenerator[ReadSessionLease]:
        assert grau is self.lease.grau
        yield self.lease


def _service(tmp_path: Path, page: Page, *, timeout_ms: int = 2_000) -> PjeReadService:
    lease = ReadSessionLease(
        grau=Grau.PRIMEIRO,
        context=cast(BrowserContext, object()),
        page=page,
        generation="generation-a",
    )
    return PjeReadService(
        cast(PjeSessionManager, _FakeSessions(lease)),
        Settings(
            data_dir=tmp_path / "data",
            downloads_dir=tmp_path / "downloads",
            timeout_ms=timeout_ms,
        ),
    )


_PAINEL = """
<!doctype html><html><body>
  <table class="rich-tabpanel"><tr>
    <td class="rich-tab-header">Expedientes</td>
    <td class="rich-tab-header">Consulta Processos</td>
    <td class="rich-tab-header">Peticionar</td>
    <td class="rich-tab-header">Acervo</td>
  </tr></table>
  <div id="alvo"></div>
  <script>
    window.submetidos = [];
    for (const td of document.querySelectorAll('td.rich-tab-header')) {
      td.onclick = () => {
        if (td.textContent.trim() !== 'Consulta Processos') return;
        document.getElementById('alvo').innerHTML = `
          <form id="fPP" action="/1g/Painel/painel_usuario/advogado.seam;jsessionid=X">
            <input name="fPP:numProcesso" type="text" title="Número do processo" />
            <input name="fPP:nomeParte" type="text" title="Nome da parte" />
            <select name="fPP:classe"><option>Todas</option><option>Cível</option></select>
            <input name="fPP:pesquisar" type="button" value="Pesquisar" />
          </form>`;
      };
    }
    document.addEventListener('submit', (e) => { window.submetidos.push(1); e.preventDefault(); });
  </script>
</body></html>
"""


async def _sem_navegar(_page: Page, _grau: Grau, _jurisdicao: str | None = None) -> None:
    return None


@pytest.mark.parametrize(
    "aba", ["Peticionar", "Expedientes", "Habilitação nos Autos", "Novo Processo", "qualquer"]
)
@pytest.mark.anyio
async def test_action_tabs_are_refused_by_the_allowlist(
    aba: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Abrir uma aba de ação deixaria o servidor a um clique de ato processual."""
    async with _page() as page:
        await page.set_content(_PAINEL)
        service = _service(tmp_path, page)
        monkeypatch.setattr(service, "_load_acervo", _sem_navegar)

        with pytest.raises(ValidacaoError, match="lista consultiva inspecionável"):
            await service.inspecionar_aba_painel(Grau.PRIMEIRO, aba)


@pytest.mark.anyio
async def test_the_consultation_tab_is_described_without_submitting_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _page() as page:
        await page.set_content(_PAINEL)
        service = _service(tmp_path, page)
        monkeypatch.setattr(service, "_load_acervo", _sem_navegar)

        estrutura = await service.inspecionar_aba_painel(Grau.PRIMEIRO, "consulta processos")

        assert estrutura.aba == "Consulta Processos"
        assert "Peticionar" in estrutura.abas_disponiveis
        campos = {c.nome: c for c in estrutura.formularios[0].campos}
        assert set(campos) == {
            "fPP:numProcesso", "fPP:nomeParte", "fPP:classe", "fPP:pesquisar"
        }
        assert campos["fPP:nomeParte"].rotulo == "Nome da parte"
        assert campos["fPP:classe"].opcoes == ["Todas", "Cível"]
        assert "Pesquisar" in estrutura.botoes
        # A sessão não é levada a lugar nenhum: nenhum formulário foi submetido.
        assert await page.evaluate("window.submetidos") == []
        # Os critérios de busca continuam vazios: descrever a tela não é usá-la.
        assert not campos["fPP:numProcesso"].preenchido
        assert not campos["fPP:nomeParte"].preenchido


@pytest.mark.anyio
async def test_a_missing_tab_is_reported_as_a_changed_interface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _page() as page:
        await page.set_content("<!doctype html><html><body></body></html>")
        service = _service(tmp_path, page)
        monkeypatch.setattr(service, "_load_acervo", _sem_navegar)

        with pytest.raises(InterfacePjeAlteradaError, match="não apresentou a aba"):
            await service.inspecionar_aba_painel(Grau.PRIMEIRO, "Consulta Processos")


# Rótulos reais do painel do TJPE, colhidos em 04/09/2026: o "p" é minúsculo.
_PAINEL_A4J = """
<!doctype html><html><body>
  <table class="rich-tabpanel"><tr>
    <td class="rich-tab-header">Expedientes</td>
    <td class="rich-tab-header">Consulta processos</td>
    <td class="rich-tab-header">Minhas petições</td>
  </tr></table>
  <form id="tabPanel:_form">
    <input name="javax.faces.ViewState" type="hidden" value="v" />
  </form>
  <!-- A aba anterior já deixou campo visível na tela: esperar por "algum campo
       visível" devolveria este conteúdo como se fosse o da aba pedida. -->
  <form id="formAbaAnterior">
    <input name="formAbaAnterior:algo" type="text" />
  </form>
  <div id="alvo"></div>
  <script>
    for (const td of document.querySelectorAll('td.rich-tab-header')) {
      td.onclick = () => {
        if (!td.textContent.includes('Consulta')) return;
        // O conteúdo da aba chega por a4j, bem depois do clique.
        setTimeout(() => {
          document.getElementById('alvo').innerHTML = `
            <form id="fPP">
              <input name="fPP:numProcesso" type="text" title="Número do processo" />
              <input name="fPP:pesquisar" type="button" value="Pesquisar" />
            </form>`;
        }, 1800);
      };
    }
  </script>
</body></html>
"""


@pytest.mark.anyio
async def test_the_a4j_tab_content_is_waited_for_instead_of_timed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fotografar cedo capturava só ViewState e autoScroll, sugerindo aba sem formulário."""
    async with _page() as page:
        await page.set_content(_PAINEL_A4J)
        # O conteúdo chega em 1,8 s — depois do 1,2 s que a versão cronometrada dava.
        service = _service(tmp_path, page, timeout_ms=8_000)
        monkeypatch.setattr(service, "_load_acervo", _sem_navegar)

        estrutura = await service.inspecionar_aba_painel(Grau.PRIMEIRO, "Consulta Processos")

        assert estrutura.campos_visiveis > 0
        nomes = {c.nome for f in estrutura.formularios for c in f.campos}
        assert "fPP:numProcesso" in nomes
        assert "Pesquisar" in estrutura.botoes


@pytest.mark.anyio
async def test_the_label_is_reported_as_the_screen_writes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _page() as page:
        await page.set_content(_PAINEL_A4J)
        service = _service(tmp_path, page, timeout_ms=8_000)
        monkeypatch.setattr(service, "_load_acervo", _sem_navegar)

        estrutura = await service.inspecionar_aba_painel(Grau.PRIMEIRO, "Consulta Processos")

        # A allowlist escreve com P maiúsculo; a tela escreve minúsculo. Vale a tela.
        assert estrutura.aba == "Consulta processos"
        assert "Minhas petições" in estrutura.abas_disponiveis


@pytest.mark.anyio
async def test_the_autos_structure_map_masks_identifiers_before_leaving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O mapa carrega amostras de texto da tela; identificador pessoal não sai daqui."""
    from mcp_pje_tjpe.pje_read import _AcervoBinding, _mascarar_amostras

    sujo = {
        "onclick": "abrirLinkDocumento('1') CPF: 123.456.789-00",
        "container": {"texto": "Parte CNPJ: 12.345.678/0001-95"},
        "ancestrais": [{"tag": "div"}, "CPF: 111.222.333-44"],
    }

    limpo = _mascarar_amostras(sujo)

    assert "123.456.789-00" not in str(limpo)
    assert "12.345.678/0001-95" not in str(limpo)
    assert "111.222.333-44" not in str(limpo)
    assert "abrirLinkDocumento" in str(limpo)
    _ = tmp_path, monkeypatch, _AcervoBinding


# Destinos reais colhidos do painel do TJPE em 05/09/2026, ao lado dos iframes
# decorativos que o RichFaces espalha pela página.
@pytest.mark.parametrize(
    ("iframes", "esperado"),
    [
        (
            ["../../Processo/ConsultaProcesso/listView.seam"],
            "../../Processo/ConsultaProcesso/listView.seam",
        ),
        (["../../Push/listView.seam"], "../../Push/listView.seam"),
        (
            [
                "/1g/a4j/g/3_3_3.Finalorg/richfaces/renderkit/html/images/spacer.gif",
                "/1g/a4j/g/3_3_3.Finalorg/richfaces/renderkit/html/images/spacer.gif",
                "../../Push/listView.seam",
            ],
            "../../Push/listView.seam",
        ),
        # Só decorativos: a aba não é casca de página nenhuma.
        (["/1g/a4j/g/3_3_3.Final/spacer.gif"], None),
        ([], None),
        # Ambíguo: dois destinos distintos não permitem afirmar qual é o da aba.
        (["../../Push/listView.seam", "../../Outra/listView.seam"], None),
    ],
)
def test_the_tab_destination_is_named_apart_from_decorative_iframes(
    iframes: list[str], esperado: str | None
) -> None:
    from mcp_pje_tjpe.pje_read import _pagina_embutida

    assert _pagina_embutida(iframes) == esperado


_ACERVO_COM_PAGINACAO = """
<!doctype html><html><body>
  <table class="rich-tabpanel"><tr><td class="rich-tab-header">Acervo</td></tr></table>
  <div id="divListaAcervo">
    <table><tr><td>linha 1</td></tr><tr><td>linha 2</td></tr></table>
  </div>
  <div class="rich-datascr" id="formAbaAcervo:scrollerProcessos1234">
    <span class="rich-datascr-button" onclick="A4J.AJAX.Submit('x')">»</span>
  </div>
  <p>Exibindo 1 a 40 de 1650 registros</p>
</body></html>
"""


@pytest.mark.anyio
async def test_the_pagination_control_is_mapped_for_a_big_jurisdiction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sem saber qual controle avança a página, paginar seria adivinhar o seletor."""
    async with _page() as page:
        await page.set_content(_ACERVO_COM_PAGINACAO)
        service = _service(tmp_path, page, timeout_ms=2_000)

        async def load(_p: Page, _g: Grau, jurisdicao: str | None = None) -> None:
            assert jurisdicao == "Recife - Varas"

        monkeypatch.setattr(service, "_load_acervo", load)

        estrutura = await service.inspecionar_aba_painel(
            Grau.PRIMEIRO, "Acervo", "Recife - Varas"
        )

        assert estrutura.linhas_na_lista == 2
        assert "1 a 40 de 1650" in " ".join(estrutura.contadores)
        assert estrutura.paginacao
        # O id autogerado é mascarado: ele muda entre versões do PJe.
        assert "1234" not in str(estrutura.paginacao)
        assert any("datascr" in str(item.get("classes")) for item in estrutura.paginacao)


@pytest.mark.anyio
async def test_a_jurisdiction_only_makes_sense_for_the_acervo_tab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _page() as page:
        await page.set_content(_ACERVO_COM_PAGINACAO)
        service = _service(tmp_path, page, timeout_ms=2_000)
        monkeypatch.setattr(service, "_load_acervo", _sem_navegar)

        with pytest.raises(ValidacaoError, match="só se aplica à aba Acervo"):
            await service.inspecionar_aba_painel(Grau.PRIMEIRO, "Push", "Recife - Varas")


_CASCA = """
<!doctype html><html><body>
  <iframe src="../../Push/listView.seam"></iframe>
</body></html>
"""

_EMBUTIDA = """
<!doctype html><html><body>
  <form id="formPush">
    <input name="formPush:numero" type="text" title="Número do processo" />
    <input id="formPush:btIncluir" type="button" value="Incluir" />
  </form>
</body></html>
"""


@pytest.mark.anyio
async def test_a_shell_tab_is_described_by_the_page_it_embeds(tmp_path: Path) -> None:
    """Push e Consulta processos não têm formulário próprio: só o iframe.

    Observado no TJPE em 2026-09-08: a aba Push devolvia formularios=[], botoes=[] e
    linhas_na_lista=0 porque o inspetor descrevia a moldura, não a página embutida.
    """
    async with _page() as page:

        async def servir(rota: object) -> None:
            url = rota.request.url  # type: ignore[attr-defined]
            corpo = _EMBUTIDA if "listView.seam" in url else _CASCA
            await rota.fulfill(  # type: ignore[attr-defined]
                status=200, content_type="text/html; charset=utf-8", body=corpo
            )

        await page.route("https://pje.cloud.tjpe.jus.br/**", servir)
        await page.goto(
            "https://pje.cloud.tjpe.jus.br/1g/Painel/painel_usuario/advogado.seam"
        )

        interno, quadros, _motivo = await _service(tmp_path, page)._estrutura_embutida(
            page, "../../Push/listView.seam"
        )

        assert interno is not None
        assert [f["id"] for f in interno["formularios"]] == ["formPush"]
        assert "Incluir" in interno["botoes"]
        assert quadros == ["/1g/Push/listView.seam"]


@pytest.mark.anyio
async def test_a_frame_on_another_host_is_not_described(tmp_path: Path) -> None:
    """Um iframe apontando para fora não é conteúdo do painel."""
    async with _page() as page:
        await page.goto("data:text/html,<html><body></body></html>")

        interno, quadros, _motivo = await _service(tmp_path, page)._estrutura_embutida(
            page, "https://exemplo.invalido/listView.seam"
        )

        assert interno is None
        assert quadros == []


@pytest.mark.anyio
async def test_a_jsessionid_in_the_frame_path_still_matches(tmp_path: Path) -> None:
    """O JBoss Seam anexa ';jsessionid=' ao caminho do quadro.

    Comparar caminhos crus fazia a descida falhar calada no TJPE, e a aba Push
    voltava descrita como moldura vazia.
    """
    async with _page() as page:

        async def servir(rota: object) -> None:
            url = rota.request.url  # type: ignore[attr-defined]
            corpo = _EMBUTIDA if "listView.seam" in url else _CASCA
            await rota.fulfill(  # type: ignore[attr-defined]
                status=200, content_type="text/html; charset=utf-8", body=corpo
            )

        await page.route("https://pje.cloud.tjpe.jus.br/**", servir)
        await page.goto(
            "https://pje.cloud.tjpe.jus.br/1g/Painel/painel_usuario/advogado.seam"
        )
        await page.evaluate(
            "() => { document.querySelector('iframe').src ="
            " '../../Push/listView.seam;jsessionid=ABC123'; }"
        )

        interno, quadros, _motivo = await _service(tmp_path, page)._estrutura_embutida(
            page, "../../Push/listView.seam"
        )

        assert interno is not None
        assert [f["id"] for f in interno["formularios"]] == ["formPush"]
        assert quadros == ["/1g/Push/listView.seam"]
