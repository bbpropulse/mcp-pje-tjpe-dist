from __future__ import annotations

import asyncio
import hashlib
import http.client
import multiprocessing
import os
import re
import secrets
import tempfile
import threading
import time
import unicodedata
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass, field
from html.parser import HTMLParser
from io import BytesIO
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, ClassVar, Protocol, cast
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import psutil
from playwright.async_api import BrowserContext, Dialog, Locator, Page, Route
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pypdf import PdfReader

from mcp_pje_tjpe.config import Settings
from mcp_pje_tjpe.errors import (
    CredenciaisAusentesError,
    InterfacePjeAlteradaError,
    PjeTjpeError,
    ServicoIndisponivelError,
    ValidacaoError,
)
from mcp_pje_tjpe.models import (
    ArquivoBaixado,
    AutosDigitais,
    DocumentoAutos,
    EstruturaAbaPainel,
    EstruturaAutos,
    FormularioPainel,
    Grau,
    ItemAcervo,
    JurisdicaoAcervo,
    JurisdicoesAcervo,
    MovimentoAutos,
    PaginaAcervo,
    TextoDocumentoAutos,
)
from mcp_pje_tjpe.pje_auth import PjeSessionManager, ReadSessionLease
from mcp_pje_tjpe.pje_public import mask_cpf_cnpj, normalize_npu_tjpe

_NPU = re.compile(r"\b\d{7}-\d{2}\.\d{4}\.8\.17\.\d{4}\b")
_DOCUMENT_ID = re.compile(r"^[0-9]{1,20}$")
_AUTH_CODE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_COOKIE_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_REFERENCE = re.compile(r"^[A-Za-z0-9_-]{20,80}$")
_DATE = re.compile(r"\b\d{2}/\d{2}/\d{4}(?:\s+\d{2}:\d{2}(?::\d{2})?)?\b")
# O conteúdo da aba-casca chega por a4j depois do clique; o quadro pode ainda não
# ter navegado quando a estrutura é lida.
_ESPERA_QUADRO_SEGUNDOS = 5.0

# Intervalo entre checagens de que o a4j já trocou a lista do Acervo.
_INTERVALO_TROCA_LISTA_MS = 200


def _caminho_sem_sessao(url_ou_caminho: str) -> str:
    """Caminho sem o ';jsessionid=' que o Seam anexa — ele muda a cada sessão."""
    caminho = urlparse(url_ou_caminho).path or url_ou_caminho
    return caminho.split(";", 1)[0]


# Horário do ato no fim da linha do movimento, sem separador antes dele.
_HORA_FINAL = re.compile(r"\s(\d{1,2}:\d{2}(?::\d{2})?)\s*$")
_PAGINATION = re.compile(r"\b(?P<atual>\d+)\s+de\s+(?P<total>\d+)\b", re.IGNORECASE)
_BLOCKED_ACERVO = re.compile(
    r"(?:tomar\s+ci[eê]ncia|visualizar\s+expediente|responder\s+expediente)",
    re.IGNORECASE,
)
_BLOCKED_DOCUMENT = re.compile(
    r"(?:tomar\s+ci[eê]ncia|pendente\s+de\s+ci[eê]ncia|"
    r"visualizar\s+documento\s+pendente|registrar\s+ci[eê]ncia)",
    re.IGNORECASE,
)
_RESTRICTED_CACHED_AUTOS = re.compile(
    r"(?:processo\s+(?:sigiloso|(?:em\s+)?segredo\s+de\s+justi[cç]a)|"
    r"tramita(?:ndo)?\s+(?:em\s+)?segredo\s+de\s+justi[cç]a|acesso\s+restrito|"
    r"documento\s+sigiloso|acesso\s+negado|n[aã]o\s+possui\s+permiss[aã]o|"
    r"sem\s+permiss[aã]o|acesso\s+n[aã]o\s+autorizado|usu[aá]rio\s+n[aã]o\s+autorizado|"
    r"segredo\s+de\s+justi[cç]a(?!(?:\s|:)*(?:n[aã]o|false|no)\b))",
    re.IGNORECASE,
)
_LOGIN_MARKERS = (
    b'id="kc-pje-office"',
    b'id="kc-form-login"',
    b'name="username"',
)
_ALLOWED_CLASSIC_AUTOS = {
    "listAutosDigitais.seam": "idProcesso",
    "listProcessoCompletoAdvogado.seam": "id",
    "listProcessoCompleto.seam": "id",
}
_HEADER_LABELS = (
    "Classe judicial",
    "Assunto",
    "Autuação",
    "Última distribuição",
    "Valor da causa",
    "Segredo de justiça",
    "Prioridade",
    "Órgão colegiado",
    "Órgão julgador",
    "Relator",
    "Polo ativo",
    "Polo passivo",
    "Outros interessados",
)
_MIME_EXTENSIONS = {
    "application/pdf": ".pdf",
    "text/html": ".html",
    "application/xhtml+xml": ".html",
    "text/plain": ".txt",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/tiff": ".tiff",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "video/mp4": ".mp4",
    "video/ogg": ".ogv",
    "video/quicktime": ".mov",
}
# Cada download revalidava do zero: recarregar o Acervo, reabrir os Autos e varrer a
# timeline inteira — ~11 s por peça, 20 navegações completas para baixar 20
# documentos. A revalidação existe para reprovar autorização (o processo continua no
# Acervo) e bloqueio por ciência, e não para renovar a URL, que vem do vínculo do
# documento. Reaproveitá-la por uma janela curta mantém a checagem e corta a
# repetição; passada a janela, o caminho completo volta a ser percorrido.
_REVALIDACAO_AUTOS_SEGUNDOS = 60.0
_MAX_AUTOS_HTML_BYTES = 10 * 1024 * 1024
_PDF_MEMORY_LIMIT_BYTES = 512 * 1024 * 1024
_PDF_CPU_LIMIT_SECONDS = 8
_PDF_WALL_TIMEOUT_SECONDS = 15.0
_PDF_SLOT_WAIT_SECONDS = 1.0
_PDF_PROCESS_SLOT = threading.BoundedSemaphore(value=1)
_UNSAFE_MAGIC = (
    b"MZ",
    b"\x7fELF",
    b"\xcf\xfa\xed\xfe",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xfe\xed\xfa\xce",
)
_QUOTED_JS_VALUE = re.compile(r"['\"](?P<value>[^'\"\r\n]{1,2048})['\"]")
_BINARY_MAGIC: dict[str, tuple[bytes, ...]] = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/tiff": (b"II*\x00", b"MM\x00*"),
    "audio/mpeg": (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"),
    "audio/ogg": (b"OggS",),
    "video/ogg": (b"OggS",),
}


def _compact(value: str) -> str:
    return " ".join(value.split())


@dataclass(frozen=True, slots=True)
class _DocumentBinding:
    generation: str
    grau: Grau
    numero: str
    processo_id: str
    documento_id: str
    titulo: str
    bloqueado_por_ciencia: bool


@dataclass(frozen=True, slots=True)
class _AcervoBinding:
    generation: str
    grau: Grau
    numero: str
    processo_id: str
    autos_url: str = field(repr=False)
    # O Acervo é particionado: sem a jurisdição de origem, recarregá-lo cai no
    # seletor vazio e o link assinado do processo não é reencontrado.
    jurisdicao: str = ""
    # E, se o processo veio de uma busca, recarregar sem repeti-la mostra os
    # primeiros da jurisdição — não ele. Guardado como par ordenado para o vínculo
    # continuar hashável.
    criterios: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class _PesquisaGeralBinding:
    generation: str = field(repr=False)
    grau: Grau
    numero: str
    processo_id: str = field(repr=False)
    autos_html: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class PjeReadTarget:
    """Vínculo interno renovado para consumidores seguros dos Autos."""

    generation: str = field(repr=False)
    grau: Grau
    numero: str
    processo_id: str = field(repr=False)
    autos_url: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class _FetchedDocument:
    binding: _DocumentBinding
    body: bytes
    mime_type: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()


class _HtmlTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"script", "style", "noscript"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and _compact(data):
            self.parts.append(_compact(data))


class _WorkerProcess(Protocol):
    @property
    def pid(self) -> int | None: ...

    def is_alive(self) -> bool: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def join(self, timeout: float | None = None) -> None: ...


class PjeReadService:
    """Leitura autenticada do Acervo/Autos sem ações processuais."""

    def __init__(
        self,
        sessions: PjeSessionManager,
        config: Settings,
        *,
        registry_limit: int = 5_000,
    ) -> None:
        if registry_limit < 1:
            raise ValueError("registry_limit deve ser positivo")
        self.sessions = sessions
        self.config = config
        self.registry_limit = registry_limit
        self._acervo: OrderedDict[tuple[str, Grau, str], _AcervoBinding] = OrderedDict()
        self._autos_revalidados: OrderedDict[
            tuple[str, Grau, str], tuple[float, dict[str, str], str]
        ] = OrderedDict()
        self._pesquisa_geral: OrderedDict[tuple[str, Grau, str], _PesquisaGeralBinding] = (
            OrderedDict()
        )
        self._documents: OrderedDict[str, _DocumentBinding] = OrderedDict()
        self._pdf_slot = asyncio.Semaphore(1)

    async def inspecionar_aba_painel(
        self, grau: Grau, aba: str, jurisdicao: str | None = None
    ) -> EstruturaAbaPainel:
        """Descreve a estrutura de uma aba consultiva do painel, sem submeter nada.

        Serve para conhecer telas que o servidor ainda não opera — hoje a Consulta
        Processos. Devolve nomes de campos, tipos e rótulos de botões; não lê
        resultado de processo, não preenche critério e não clica em ação.
        """
        alvo = _fold_text(aba)
        permitidas = {_fold_text(nome): nome for nome in self._ABAS_INSPECIONAVEIS}
        if alvo not in permitidas:
            disponiveis = ", ".join(self._ABAS_INSPECIONAVEIS)
            raise ValidacaoError(
                f"a aba {aba!r} não está na lista consultiva inspecionável. "
                f"Disponíveis: {disponiveis}"
            )
        rotulo = permitidas[alvo]

        if jurisdicao is not None and rotulo != "Acervo":
            raise ValidacaoError("jurisdição só se aplica à aba Acervo")

        async with self.sessions.read_session(grau) as lease:
            await self._load_acervo(lease.page, grau, jurisdicao)
            antes = self._assinatura_de_formularios(
                cast(dict[str, Any], await lease.page.evaluate(self._MAPA_ABA))
            )
            if rotulo != "Acervo" and not await _click_visible_exact(
                lease.page, rotulo, timeout_ms=self.config.timeout_ms
            ):
                raise InterfacePjeAlteradaError(
                    f"o painel autenticado não apresentou a aba {rotulo!r}"
                )
            bruto = await self._aguardar_troca_de_aba(lease.page, antes)
            visto = _rotulo_de_aba(
                [str(item) for item in cast(list[Any], bruto.get("abas", []))], rotulo
            )

        iframes = [str(item) for item in cast(list[Any], bruto.get("iframes", []))]
        embutida = _pagina_embutida(iframes)
        # Abas como Push e Consulta processos são cascas: a moldura não tem
        # formulário nenhum e o conteúdo vive numa página .seam dentro do iframe.
        # Descrever a casca seria descrever nada.
        interno, quadros, motivo_quadro = await self._estrutura_embutida(
            lease.page, embutida
        )
        if interno is not None:
            for chave in (
                "formularios",
                "botoes",
                "campos_visiveis",
                "paginacao",
                "criterios",
                "alternadores",
                "contador",
                "linhas_lista",
            ):
                bruto[chave] = interno.get(chave, bruto.get(chave))

        return EstruturaAbaPainel(
            grau=grau,
            aba=visto or rotulo,
            abas_disponiveis=[str(item) for item in cast(list[Any], bruto.get("abas", []))],
            formularios=[
                FormularioPainel.model_validate(item)
                for item in cast(list[Any], bruto.get("formularios", []))
            ],
            botoes=[str(item) for item in cast(list[Any], bruto.get("botoes", []))],
            campos_visiveis=int(cast(int, bruto.get("campos_visiveis", 0))),
            iframes=iframes,
            pagina_embutida=embutida,
            quadros_vistos=quadros,
            conteudo_embutido_lido=interno is not None,
            motivo_quadro=motivo_quadro,
            paginacao=[
                _mascarar_amostras(cast(dict[str, object], item))
                for item in cast(list[Any], bruto.get("paginacao", []))
            ],
            criterios_busca=[
                _mascarar_amostras(cast(dict[str, object], item))
                for item in cast(list[Any], bruto.get("criterios", []))
            ],
            alternadores=[
                _mascarar_amostras(cast(dict[str, object], item))
                for item in cast(list[Any], bruto.get("alternadores", []))
            ],
            contadores=[str(item) for item in cast(list[Any], bruto.get("contador", []))],
            linhas_na_lista=int(cast(int, bruto.get("linhas_lista", 0))),
            aviso=(
                "Descrição estrutural da aba, para diagnóstico. Nenhum critério foi "
                "preenchido, nenhum formulário foi submetido e nenhum resultado de "
                "processo foi lido. Conhecer a tela não autoriza operá-la."
            ),
        )

    @staticmethod
    def _assinatura_de_formularios(bruto: dict[str, Any]) -> frozenset[str]:
        return frozenset(
            str(cast(dict[str, Any], f).get("id") or "")
            for f in cast(list[Any], bruto.get("formularios", []))
        )

    async def _aguardar_troca_de_aba(
        self, page: Page, antes: frozenset[str]
    ) -> dict[str, Any]:
        """Espera o a4j trocar o conteúdo, comparando quais formulários existem.

        Duas versões erraram aqui. A primeira cronometrava 1,2 s fixos e fotografava
        antes da resposta chegar. A segunda esperava "algum campo visível", condição
        que a aba anterior já satisfazia — devolvendo o conteúdo dela como se fosse o
        da aba pedida. O que muda de fato quando a aba troca é o conjunto de
        formulários da página.
        """
        limite = time.monotonic() + self.config.timeout_ms / 1_000
        bruto = cast(dict[str, Any], await page.evaluate(self._MAPA_ABA))
        while (
            self._assinatura_de_formularios(bruto) == antes
            and time.monotonic() < limite
        ):
            await page.wait_for_timeout(400)
            bruto = cast(dict[str, Any], await page.evaluate(self._MAPA_ABA))
        return bruto

    async def inspecionar_estrutura_autos(self, numero: str, grau: Grau) -> EstruturaAutos:
        """Descreve a estrutura da timeline dos Autos, sem ler conteúdo de peça.

        O extrator de documentos e o de movimentos foram escritos contra uma estrutura
        presumida: no PJe real toda peça sai como "Documento <id>" e a lista de
        movimentos volta vazia. Isto mostra o que a página tem, para que o conserto
        venha de evidência e não de tentativa.
        """
        formatado = normalize_npu_tjpe(numero)
        async with self.sessions.read_session(grau) as lease:
            vinculo = self._require_acervo_binding(lease, formatado)
            await self._repor_tela_do_vinculo(lease.page, grau, vinculo)
            await self._refresh_acervo_binding(lease, formatado)
            pagina, contexto, _ = await self._open_autos_from_acervo(lease, formatado)
            try:
                bruto = cast(dict[str, Any], await pagina.evaluate(self._MAPA_AUTOS))
            finally:
                await contexto.close()

        raiz = cast(dict[str, Any], bruto.get("raiz", {}))
        contagens = {
            str(chave): int(cast(int, valor))
            for chave, valor in cast(dict[str, Any], bruto.get("contagens", {})).items()
        }
        documentos = [
            _mascarar_amostras(cast(dict[str, object], item))
            for item in cast(list[Any], bruto.get("documentos", []))
        ]
        return EstruturaAutos(
            numero=formatado,
            grau=grau,
            timeline_existe=bool(raiz.get("timeline_existe")),
            contagens=contagens,
            documentos=documentos,
            aviso=(
                "Descrição estrutural da timeline, para diagnóstico. Amostras de texto "
                "são curtas e têm CPF/CNPJ mascarados; nenhum documento foi aberto, "
                "lido ou baixado."
            ),
        )

    async def _avancar_pagina_acervo(self, page: Page) -> bool:
        """Avança uma página no datascroller do Acervo. False quando não há próxima.

        O controle não responde a clique comum: os botões disparam
        `Event.fire(this, 'rich:datascroller:onscroll', {'page': 'next'})`, e o de
        avançar recebe a classe `-dsbld` quando a página atual é a última.
        """
        # Jurisdição que cabe numa página não tem scroller nenhum; esperar por ele
        # apareceria como travamento até o prazo estourar.
        if await page.locator(self._SCROLLER).count() == 0:
            return False
        antes = await page.locator(self._LISTA_ACERVO_LINK).count()
        avancou = await page.locator(self._SCROLLER).first.evaluate(
            """
            (scroller) => {
              const alvo = Array.from(
                scroller.querySelectorAll('td.rich-datascr-button')
              ).find((td) => {
                const acao = td.getAttribute('onclick') || '';
                const desabilitado = td.classList.contains('rich-datascr-button-dsbld');
                return !desabilitado && acao.includes("'page': 'next'");
              });
              if (!alvo) return false;
              alvo.click();
              return true;
            }
            """
        )
        if not bool(avancou):
            return False
        # A troca chega por a4j: espera a lista deixar de ser a mesma.
        limite = time.monotonic() + self.config.timeout_ms / 1_000
        while time.monotonic() < limite:
            await page.wait_for_timeout(300)
            if await page.locator(self._LISTA_ACERVO_LINK).count() != antes:
                return True
        return True

    async def _abrir_busca_acervo(self, page: Page, criterios: dict[str, str]) -> None:
        """Abre o dropdown de critérios, se ainda estiver fechado. Falha fechando."""
        primeiro = self._CRITERIOS_ACERVO[next(iter(criterios))]
        campo = page.locator(f'#formAcervo [name="{primeiro}"]')
        if await campo.count() != 1:
            # Campo ausente é problema do critério, não do alternador; quem sabe
            # dizer isso com precisão é o laço de preenchimento.
            return
        if await campo.is_visible():
            return

        alternador = page.locator(self._ABRIR_BUSCA_ACERVO)
        if await alternador.count() != 1:
            raise InterfacePjeAlteradaError(
                "o Acervo não apresentou o controle que abre a área de busca "
                "('Pesquisar nesta caixa')"
            )
        await alternador.click()
        try:
            await campo.wait_for(state="visible", timeout=self.config.timeout_ms)
        except PlaywrightTimeoutError:
            raise InterfacePjeAlteradaError(
                "a área de busca do Acervo não abriu depois de acionar "
                "'Pesquisar nesta caixa'"
            ) from None

    async def _pesquisar_no_acervo(self, page: Page, criterios: dict[str, str]) -> None:
        """Usa a busca do próprio Acervo, que filtra no servidor sem gravar nada.

        É diferente do filtrosFormAcervo, cujo botão é "Gravar" e persiste a
        configuração na caixa do usuário. Aqui o botão é "Pesquisar": nada fica.
        """
        if await page.locator("form#formAcervo").count() != 1:
            raise InterfacePjeAlteradaError(
                "o Acervo não apresentou o formulário de busca esperado"
            )

        await self._abrir_busca_acervo(page, criterios)

        for chave, valor in criterios.items():
            campo = page.locator(f'#formAcervo [name="{self._CRITERIOS_ACERVO[chave]}"]')
            if await campo.count() != 1:
                raise InterfacePjeAlteradaError(
                    f"o critério {chave!r} não existe mais no formulário do Acervo"
                )
            # O campo existe no DOM mas pode estar recolhido. fill() espera por
            # visibilidade e estoura em TimeoutError, que sobe mascarado como erro
            # sem texto — sem isto, a falha não diz nada a quem for corrigi-la.
            if not await campo.is_visible():
                raise InterfacePjeAlteradaError(
                    f"o campo do critério {chave!r} está no formulário do Acervo mas "
                    "não visível; a área de busca provavelmente está recolhida"
                )
            try:
                await campo.fill(valor)
            except PlaywrightTimeoutError as erro:
                raise InterfacePjeAlteradaError(
                    f"o campo do critério {chave!r} não aceitou preenchimento: {erro}"
                ) from None
            if _fold_text(await campo.input_value()) != _fold_text(valor):
                raise InterfacePjeAlteradaError(
                    f"o campo do critério {chave!r} não aceitou o valor informado"
                )

        botao = page.locator(f"#formAcervo {self._BOTAO_PESQUISAR}")
        if await botao.count() != 1:
            raise InterfacePjeAlteradaError("o Acervo não apresentou o botão Pesquisar")
        rotulo = _fold_text(await botao.get_attribute("value") or "")
        if rotulo != _fold_text("Pesquisar"):
            raise InterfacePjeAlteradaError(
                f"o botão fixado por id deixou de ser Pesquisar; agora diz {rotulo!r}"
            )

        antes = await page.locator(self._LISTA_ACERVO_LINK).count()
        try:
            await botao.click()
        except PlaywrightTimeoutError as erro:
            raise InterfacePjeAlteradaError(
                f"o botão Pesquisar do Acervo não aceitou o clique: {erro}"
            ) from None
        # A busca volta por a4j; espera a lista deixar de ser a de antes.
        limite = time.monotonic() + self.config.timeout_ms / 1_000
        while time.monotonic() < limite:
            await page.wait_for_timeout(300)
            if await page.locator(self._LISTA_ACERVO_LINK).count() != antes:
                return

    async def listar_jurisdicoes_acervo(self, grau: Grau) -> JurisdicoesAcervo:
        """Jurisdições do Acervo do usuário, sem selecionar nenhuma nem listar processos."""
        async with self.sessions.read_session(grau) as lease:
            await self._load_acervo(lease.page, grau)
            jurisdicoes = await self._acervo_jurisdicoes(lease.page)
        return JurisdicoesAcervo(
            grau=grau,
            jurisdicoes=jurisdicoes,
            aviso=(
                "O Acervo do TJPE é particionado por jurisdição e nenhuma foi selecionada "
                "aqui. Use o campo 'nome' em listar_acervo(jurisdicao=...); 'processos' é "
                "a contagem que o painel exibe e não faz parte do nome. A lista cobre "
                "apenas o que o PJe apresenta a este usuário neste grau."
            ),
        )

    async def listar_acervo(
        self,
        grau: Grau,
        *,
        pesquisa: str | None = None,
        limite: int = 50,
        jurisdicao: str | None = None,
        filtro: str | None = None,
        paginas: int = 1,
        parte: str | None = None,
        documento: str | None = None,
        oab: str | None = None,
        classe: str | None = None,
        assunto: str | None = None,
    ) -> PaginaAcervo:
        if not 1 <= limite <= 100:
            raise ValidacaoError("limite do Acervo deve estar entre 1 e 100")
        # Cada página é um round-trip a4j de alguns segundos; o teto evita que um
        # pedido distraído percorra as 38 páginas de uma jurisdição grande.
        if not 1 <= paginas <= 20:
            raise ValidacaoError("paginas deve estar entre 1 e 20")
        criterios = {
            chave: valor.strip()
            for chave, valor in (
                ("parte", parte), ("documento", documento), ("oab", oab),
                ("classe", classe), ("assunto", assunto),
            )
            if valor is not None and valor.strip()
        }
        if criterios and jurisdicao is None:
            raise ValidacaoError(
                "a busca do Acervo roda dentro de uma jurisdição; informe 'jurisdicao'"
            )
        numero_pesquisado = normalize_npu_tjpe(pesquisa) if pesquisa else None

        async with self.sessions.read_session(grau) as lease:
            if jurisdicao is None:
                # O Acervo do TJPE é por jurisdição; sem escolher uma, o container
                # de resultados fica vazio. Devolve as opções em vez de lista vazia.
                await self._load_acervo(lease.page, grau)
                disponiveis = await self._acervo_jurisdicoes(lease.page)
                rotulos = ", ".join(
                    f"{item.nome} ({item.processos})" if item.processos is not None
                    else item.nome
                    for item in disponiveis
                )
                raise ValidacaoError(
                    "o Acervo do TJPE é organizado por jurisdição; informe uma em "
                    "'jurisdicao'. Disponíveis no seu Acervo, com a contagem de "
                    "processos: " + (rotulos or "nenhuma")
                )
            na_jurisdicao = await self._load_acervo(lease.page, grau, jurisdicao)
            if criterios:
                try:
                    await self._pesquisar_no_acervo(lease.page, criterios)
                except PjeTjpeError:
                    raise
                except Exception as erro:
                    # Só PjeTjpeError chega ao usuário com texto; qualquer outra vira
                    # "Error executing tool listar_acervo", sem uma pista sequer.
                    raise InterfacePjeAlteradaError(
                        "a busca no Acervo falhou de forma não prevista: "
                        f"{type(erro).__name__}: {erro}"
                    ) from None
            raw_entries = await self._extract_acervo_entries(lease.page)
            percorridas = 1
            while percorridas < paginas:
                if not await self._avancar_pagina_acervo(lease.page):
                    break
                percorridas += 1
                raw_entries.extend(await self._extract_acervo_entries(lease.page))
            items = parse_acervo_entries(raw_entries, grau)
            audited: dict[str, tuple[str, str]] = {}
            for entry in raw_entries:
                target = _audited_autos_target(entry, grau)
                if target is None:
                    continue
                parsed_items = parse_acervo_entries([entry], grau)
                if not parsed_items:
                    continue
                audited.setdefault(parsed_items[0].numero, target)

            marked_items: list[ItemAcervo] = []
            for item in items:
                target = audited.get(item.numero)
                if target is None:
                    marked_items.append(item)
                    continue
                autos_url, processo_id = target
                key = (lease.generation, grau, item.numero)
                self._acervo[key] = _AcervoBinding(
                    generation=lease.generation,
                    grau=grau,
                    numero=item.numero,
                    processo_id=processo_id,
                    autos_url=autos_url,
                    jurisdicao=jurisdicao,
                    criterios=tuple(sorted(criterios.items())),
                )
                self._acervo.move_to_end(key)
                marked_items.append(item.model_copy(update={"autos_disponiveis": True}))
            while len(self._acervo) > self.registry_limit:
                self._acervo.popitem(last=False)
            items = marked_items

            if numero_pesquisado is not None:
                items = [item for item in items if item.numero == numero_pesquisado]
            # O filtro entra antes do corte: numa jurisdição grande, filtrar só os
            # primeiros itens devolveria quase nada do que o usuário procura.
            total_antes_do_filtro = len(items)
            if filtro is not None:
                items = _filtrar_acervo(items, filtro)

            page_text = await lease.page.locator("body").inner_text()
            partial = _page_is_partial(page_text) or len(items) > limite
            selected = items[:limite]
            return PaginaAcervo(
                grau=grau,
                processos=selected,
                total_carregado=len(items),
                total_na_jurisdicao=na_jurisdicao,
                paginas_percorridas=percorridas,
                parcial=partial,
                aviso=(
                    "Listagem estritamente da aba Acervo. Não foram abertas as áreas de "
                    "Expedientes, Intimações ou Agrupadores, e nenhum processo foi movido. "
                    "Autos só podem ser abertos quando autos_disponiveis=true, isto é, "
                    "quando o link GET do processo foi validado antes de qualquer navegação. "
                    "Se parcial=true, a interface ainda possui páginas ou itens não renderizados."
                    + (
                        (
                            # Com busca, o total da jurisdição não é o que ficou de
                            # fora: dizer "o restante exige paginar" seria falso.
                            f" Esta jurisdição tem {na_jurisdicao} processos e a busca"
                            f" no servidor devolveu {len(items)} em"
                            f" {percorridas} página(s)."
                            if criterios
                            else (
                                # Filtrar por NPU ou por texto também estreita o que
                                # sobra: dizer que "o restante exige paginar" depois de
                                # já ter restringido é falso — foi o que aconteceu ao
                                # buscar um processo pelo número dentro da jurisdição.
                                f" Esta jurisdição tem {na_jurisdicao} processos, esta"
                                f" leitura percorreu {percorridas} página(s) e o filtro"
                                f" local deixou {len(items)}."
                                if numero_pesquisado is not None or filtro is not None
                                else (
                                    f" Esta jurisdição tem {na_jurisdicao} processos e"
                                    f" esta leitura trouxe {len(items)} em"
                                    f" {percorridas} página(s): o restante exige"
                                    " aumentar 'paginas' ou restringir a busca."
                                )
                            )
                        )
                        if na_jurisdicao is not None and na_jurisdicao > len(items)
                        else ""
                    )
                    + (
                        # 'total_carregado' conta o que foi lido, não o que vai na
                        # resposta; sem dizer isso os dois números se contradizem.
                        f" De {len(items)} processos lidos, 'limite' devolveu"
                        f" {len(selected)}."
                        if len(selected) < len(items)
                        else ""
                    )
                    + (
                        " A busca foi feita pelo próprio Acervo, no servidor, com os"
                        f" critérios {sorted(criterios)}; nada foi gravado no PJe."
                        if criterios
                        else ""
                    )
                    + (
                        f" O filtro {filtro!r} foi aplicado localmente sobre o resumo dos"
                        f" {total_antes_do_filtro} processos que a jurisdição apresentou;"
                        " nada foi pesquisado ou gravado no PJe."
                        if filtro is not None
                        else ""
                    )
                ),
            )

    async def consultar_autos(
        self,
        numero: str,
        grau: Grau,
        *,
        limite_documentos: int = 200,
        limite_movimentos: int = 100,
    ) -> AutosDigitais:
        formatted = normalize_npu_tjpe(numero)
        if not 1 <= limite_documentos <= 500:
            raise ValidacaoError("limite_documentos deve estar entre 1 e 500")
        if not 0 <= limite_movimentos <= 500:
            raise ValidacaoError("limite_movimentos deve estar entre 0 e 500")

        has_search_snapshot = any(
            cached_grau == grau and cached_numero == formatted
            for _generation, cached_grau, cached_numero in self._pesquisa_geral
        )
        if has_search_snapshot:
            async with self.sessions.cached_read_session(grau) as lease:
                key = (lease.generation, grau, formatted)
                binding = self._pesquisa_geral.get(key)
                if binding is not None:
                    self._pesquisa_geral.move_to_end(key)
                    autos_page, autos_context, processo_id = await self._open_cached_pesquisa_geral(
                        lease, binding
                    )
                    return await self._build_autos_model(
                        lease,
                        formatted,
                        autos_page,
                        autos_context,
                        processo_id,
                        limite_documentos=limite_documentos,
                        limite_movimentos=limite_movimentos,
                        from_acervo=False,
                    )

        async with self.sessions.read_session(grau) as lease:
            # A jurisdição vem do vínculo: recarregar sem ela deixaria a lista vazia.
            vinculo = self._require_acervo_binding(lease, formatted)
            await self._repor_tela_do_vinculo(lease.page, grau, vinculo)
            await self._refresh_acervo_binding(lease, formatted)
            autos_page, autos_context, processo_id = await self._open_autos_from_acervo(
                lease, formatted
            )
            return await self._build_autos_model(
                lease,
                formatted,
                autos_page,
                autos_context,
                processo_id,
                limite_documentos=limite_documentos,
                limite_movimentos=limite_movimentos,
                from_acervo=True,
            )

    async def _build_autos_model(
        self,
        lease: ReadSessionLease,
        numero: str,
        autos_page: Page,
        autos_context: BrowserContext,
        processo_id: str,
        *,
        limite_documentos: int,
        limite_movimentos: int,
        from_acervo: bool,
    ) -> AutosDigitais:
        try:
            header = await self._extract_header(autos_page, numero)
            raw_documents, partial = await self._extract_documents(autos_page, limite_documentos)
            documents = self._register_documents(
                raw_documents,
                lease=lease,
                numero=numero,
                processo_id=processo_id,
                conteudo_disponivel=from_acervo,
            )
            movements = await self._extract_movements(autos_page, limite_movimentos)
            return AutosDigitais(
                numero=numero,
                grau=lease.grau,
                cabecalho=header,
                movimentos=movements,
                documentos=documents,
                documentos_parciais=partial,
                origem="acervo" if from_acervo else "pesquisa_geral",
                aviso=(
                    (
                        "Autos abertos somente a partir do Acervo desta sessão. A aba "
                        "Expedientes não foi aberta; documentos sinalizados como pendentes "
                        "de ciência não podem ser lidos nem baixados."
                    )
                    if from_acervo
                    else (
                        "Leitura feita exclusivamente do HTML capturado no acesso geral "
                        "confirmado. Nenhuma requisição ao tribunal foi emitida; conteúdo e "
                        "download de documentos e PJeDocs continuam restritos ao Acervo."
                    )
                ),
            )
        finally:
            await autos_context.close()

    def registrar_autos_pesquisa_geral(
        self,
        lease: ReadSessionLease,
        numero: str,
        processo_id: str,
        autos_url: str,
        autos_html: str,
    ) -> None:
        """Registra somente o HTML já obtido após uma abertura geral confirmada."""
        formatted = normalize_npu_tjpe(numero)
        if (
            not _DOCUMENT_ID.fullmatch(processo_id)
            or _autos_process_id(autos_url, lease.grau) != processo_id
        ):
            raise InterfacePjeAlteradaError(
                "a abertura geral não corresponde ao processo e grau confirmados"
            )
        encoded = autos_html.encode("utf-8")
        if len(encoded) > _MAX_AUTOS_HTML_BYTES:
            raise ValidacaoError("os Autos excederam o teto local de HTML autenticado")
        prefix = encoded[:8_192].lower()
        if b"<html" not in prefix and b"<!doctype html" not in prefix:
            raise InterfacePjeAlteradaError(
                "o conteúdo capturado dos Autos não é um documento HTML reconhecido"
            )
        lowered = encoded.lower()
        if any(marker in lowered for marker in _LOGIN_MARKERS):
            raise CredenciaisAusentesError("a sessão expirou durante a abertura geral")
        if re.search(r"<meta\b[^>]*http-equiv\s*=\s*['\"]?refresh", autos_html, re.I):
            raise InterfacePjeAlteradaError("os Autos tentaram renovar a navegação automaticamente")
        text_extractor = _HtmlTextExtractor()
        text_extractor.feed(autos_html)
        captured_text = " ".join(text_extractor.parts)
        if formatted not in captured_text:
            raise InterfacePjeAlteradaError("o HTML aberto não confirmou o NPU pesquisado")
        if _RESTRICTED_CACHED_AUTOS.search(captured_text):
            raise ValidacaoError("os Autos indicaram sigilo, restrição ou negativa de acesso")
        sanitized_html = re.sub(
            r"(?i)([?&](?:amp;)?ca=)[A-Za-z0-9_-]{1,256}",
            r"\1REDACTED",
            autos_html,
        )
        key = (lease.generation, lease.grau, formatted)
        self._pesquisa_geral[key] = _PesquisaGeralBinding(
            generation=lease.generation,
            grau=lease.grau,
            numero=formatted,
            processo_id=processo_id,
            autos_html=sanitized_html,
        )
        self._pesquisa_geral.move_to_end(key)
        while len(self._pesquisa_geral) > self.registry_limit:
            self._pesquisa_geral.popitem(last=False)

    async def preparar_alvo_pjedocs(
        self,
        lease: ReadSessionLease,
        numero: str,
    ) -> PjeReadTarget:
        """Renova um alvo do Acervo sem pesquisar, clicar no processo ou expor a rota."""
        formatted = normalize_npu_tjpe(numero)
        vinculo = self._require_acervo_binding(lease, formatted)
        await self._repor_tela_do_vinculo(lease.page, lease.grau, vinculo)
        binding = await self._refresh_acervo_binding(lease, formatted)
        if _autos_process_id(binding.autos_url, lease.grau) != binding.processo_id:
            raise InterfacePjeAlteradaError(
                "a rota renovada dos Autos não corresponde ao processo auditado"
            )
        return PjeReadTarget(
            generation=lease.generation,
            grau=lease.grau,
            numero=formatted,
            processo_id=binding.processo_id,
            autos_url=binding.autos_url,
        )

    async def ler_documento(
        self,
        numero: str,
        grau: Grau,
        referencia_documento: str,
        *,
        max_paginas: int = 30,
        max_caracteres: int = 100_000,
    ) -> TextoDocumentoAutos:
        if not 1 <= max_paginas <= 100:
            raise ValidacaoError("max_paginas deve estar entre 1 e 100")
        if not 1_000 <= max_caracteres <= 500_000:
            raise ValidacaoError("max_caracteres deve estar entre 1.000 e 500.000")
        formatted = normalize_npu_tjpe(numero)

        async with self.sessions.read_session(grau) as lease:
            fetched = await self._fetch_registered_document(
                lease, formatted, grau, referencia_documento
            )

        if fetched.mime_type == "application/pdf":
            text, pages_read, pages_total, characters_truncated = await self._extract_pdf_text(
                fetched.body,
                max_pages=max_paginas,
                max_characters=max_caracteres,
            )
        else:
            text, pages_read, pages_total = await asyncio.to_thread(
                _extract_document_text,
                fetched.body,
                fetched.mime_type,
                max_pages=max_paginas,
            )
            characters_truncated = len(text) > max_caracteres
            if characters_truncated:
                text = text[:max_caracteres]
        pages_truncated = (
            pages_read is not None and pages_total is not None and (pages_read < pages_total)
        )
        return TextoDocumentoAutos(
            numero=formatted,
            grau=grau,
            referencia_documento=referencia_documento,
            titulo=fetched.binding.titulo,
            tipo_mime=fetched.mime_type,
            paginas_lidas=pages_read,
            paginas_totais=pages_total,
            texto=text,
            truncado=pages_truncated or characters_truncated,
            sha256=fetched.sha256,
            tamanho_bytes=len(fetched.body),
            aviso=(
                "Conteúdo lido da sessão autenticada. O SHA-256 identifica os bytes "
                "recebidos, mas não substitui a assinatura digital nem prova autenticidade "
                "jurídica isoladamente. Se truncado=true, use o arquivo baixado para "
                "analisar o teor integral."
            ),
        )

    async def _extract_pdf_text(
        self,
        body: bytes,
        *,
        max_pages: int,
        max_characters: int,
    ) -> tuple[str, int, int, bool]:
        """Aplica backpressure sem liberar a vaga se o chamador for cancelado."""
        try:
            await asyncio.wait_for(
                self._pdf_slot.acquire(),
                timeout=_PDF_SLOT_WAIT_SECONDS,
            )
        except TimeoutError:
            raise ServicoIndisponivelError(
                "o extrator seguro de PDF está ocupado; tente novamente em instantes"
            ) from None

        try:
            worker = asyncio.create_task(
                asyncio.to_thread(
                    _extract_pdf_sandboxed,
                    body,
                    max_pages=max_pages,
                    max_characters=max_characters,
                )
            )
        except BaseException:
            self._pdf_slot.release()
            raise

        def release_slot(done: asyncio.Task[tuple[str, int, int, bool]]) -> None:
            self._pdf_slot.release()
            if not done.cancelled():
                _ = done.exception()

        worker.add_done_callback(release_slot)
        return await asyncio.shield(worker)

    async def baixar_documento(
        self,
        numero: str,
        grau: Grau,
        referencia_documento: str,
    ) -> ArquivoBaixado:
        formatted = normalize_npu_tjpe(numero)
        async with self.sessions.read_session(grau) as lease:
            fetched = await self._fetch_registered_document(
                lease, formatted, grau, referencia_documento
            )

        path, sidecar = await asyncio.to_thread(
            self._publish_download_bundle,
            fetched,
        )
        return ArquivoBaixado(
            numero=formatted,
            grau=grau,
            referencia_documento=referencia_documento,
            titulo=fetched.binding.titulo,
            caminho=str(path),
            caminho_sha256=str(sidecar),
            nome_arquivo=path.name,
            tipo_mime=fetched.mime_type,
            tamanho_bytes=len(fetched.body),
            sha256=fetched.sha256,
            aviso=(
                "Arquivo e sidecar SHA-256 gravados localmente com permissão restrita. "
                "O hash identifica os bytes baixados e não substitui a assinatura digital."
            ),
        )

    async def _load_acervo(
        self, page: Page, grau: Grau, jurisdicao: str | None = None
    ) -> int | None:
        dialogs: list[bool] = []

        async def dismiss_dialog(dialog: Dialog) -> None:
            dialogs.append(True)
            await dialog.dismiss()

        page.on("dialog", dismiss_dialog)
        try:
            panel_url = (
                f"{self.config.urls.pje_base(grau.value)}/Painel/painel_usuario/advogado.seam"
            )
            response = await page.goto(panel_url, wait_until="domcontentloaded")
            if response is not None and response.status >= 400:
                raise ServicoIndisponivelError(
                    f"o painel autenticado do TJPE respondeu HTTP {response.status}"
                )
            if not _is_same_degree_url(page.url, grau):
                raise CredenciaisAusentesError(
                    "a sessão autenticada expirou antes da leitura do Acervo"
                )

            clicked_acervo = await _click_visible_exact(
                page, "Acervo", timeout_ms=self.config.timeout_ms
            )
            if clicked_acervo:
                await page.wait_for_timeout(350)
            clicked_general = await _click_visible_exact(page, "Acervo geral", timeout_ms=1500)
            if clicked_general:
                await page.wait_for_timeout(500)

            if not clicked_acervo and not clicked_general:
                raise InterfacePjeAlteradaError(
                    "o painel não apresentou um controle reconhecido da área Acervo"
                )

            body_text = await page.locator("body").inner_text()
            if "acervo" not in body_text.casefold():
                raise InterfacePjeAlteradaError(
                    "o painel autenticado não apresentou a área Acervo esperada"
                )

            # No TJPE o Acervo é particionado: abrir a aba mostra as jurisdições, e a
            # lista de processos só popula depois que uma delas é escolhida. Sem essa
            # etapa o container de resultados fica vazio.
            quantidade: int | None = None
            if jurisdicao is not None:
                quantidade = await self._select_acervo_jurisdicao(page, jurisdicao)

            if dialogs:
                raise InterfacePjeAlteradaError(
                    "o PJe apresentou um diálogo inesperado ao abrir o Acervo; "
                    "a operação foi cancelada"
                )
            return quantidade
        finally:
            page.remove_listener("dialog", dismiss_dialog)

    # Diagnóstico de estrutura, não capacidade: só abas consultivas entram, e nada é
    # submetido. Abrir "Peticionar", "Habilitação nos Autos" ou "Expedientes" ficaria
    # a um clique de ação processual, que é fronteira que este servidor não cruza.
    _ABAS_INSPECIONAVEIS = ("Consulta Processos", "Acervo", "Push")

    _MAPA_ABA = r"""
    () => {
      const amostra = (t) => (t || '').replace(/\s+/g, ' ').trim().slice(0, 120);
      const renderizado = (el) => {
        if (el.getClientRects().length === 0) return false;
        const estilo = window.getComputedStyle(el);
        return estilo.visibility !== 'hidden' && estilo.opacity !== '0';
      };
      const visivel = (el) => el.type !== 'hidden' && renderizado(el);
      const abas = Array.from(document.querySelectorAll('td.rich-tab-header'))
        .map((td) => amostra(td.textContent)).filter(Boolean);
      const formularios = Array.from(document.querySelectorAll('form')).map((f) => ({
        id: f.getAttribute('id'),
        acao: amostra((f.getAttribute('action') || '').split(';')[0]),
        campos: Array.from(f.elements).slice(0, 120).map((el) => ({
          nome: el.getAttribute('name') || '',
          tipo: (el.type || el.tagName || '').toLowerCase(),
          rotulo: amostra(el.getAttribute('title') || el.getAttribute('placeholder') || ''),
          preenchido: Boolean(el.value),
          opcoes: el.tagName === 'SELECT'
            ? Array.from(el.options).slice(0, 12).map((o) => amostra(o.textContent))
            : [],
        })).filter((c) => c.nome),
      }));
      const botoes = Array.from(document.querySelectorAll(
        'input[type=button], input[type=submit], button, a.btn'
      )).filter(visivel).slice(0, 40)
        .map((b) => amostra(b.value || b.textContent)).filter(Boolean);
      // Conteúdo de aba a4j chega depois do clique; contar o que já é visível diz
      // se a fotografia foi tirada cedo demais.
      const campos_visiveis = Array.from(
        document.querySelectorAll('input, select, textarea')
      ).filter(visivel).length;
      const iframes = Array.from(document.querySelectorAll('iframe'))
        .map((f) => amostra((f.getAttribute('src') || '').split('?')[0]));
      // O Acervo pagina no servidor: 40 linhas de cada vez. Sem saber qual controle
      // avança a página, paginar seria adivinhar o seletor — que é como nasceram os
      // defeitos de título e movimento.
      const paginacao = Array.from(document.querySelectorAll(
        '.rich-datascr, .rich-datascr-button, .rich-inslider, [class*="datascr" i],' +
        ' [id*="scroller" i], [class*="paginacao" i], [id*="paginacao" i]'
      )).slice(0, 20).map((el) => ({
        tag: el.tagName.toLowerCase(),
        classes: (el.className || '').toString().split(/\s+/).filter(Boolean).slice(0, 5),
        id: (el.getAttribute('id') || '').replace(/\d{3,}/g, '#'),
        texto: amostra(el.textContent),
        onclick: amostra(el.getAttribute('onclick')),
      }));
      // "1 a 40 de 1650": o total vem depois do intervalo, e um padrão que pare no
      // intervalo perde justamente o número que diz o tamanho da jurisdição.
      const contador = (document.body.innerText || '')
        .match(/\b\d+(?:\s+a\s+\d+)?\s+de\s+\d+\b/g) || [];
      const linhas_lista = document.querySelectorAll(
        '#divListaAcervo tr, table[id$=":tbProcessos"] tr'
      ).length;
      // Os critérios do Acervo existem no DOM mesmo recolhidos. Sem saber QUAL
      // ancestral está oculto e QUEM o alterna, expandir a área seria adivinhar
      // um seletor — que foi como nasceram os defeitos de título e de movimento.
      const criterios = Array.from(
        document.querySelectorAll('#formAcervo [name^="formAcervo:it"]')
      ).slice(0, 8).map((el) => {
        const cadeia = [];
        let no = el.parentElement;
        while (no && no !== document.body && cadeia.length < 8) {
          cadeia.push({
            tag: no.tagName.toLowerCase(),
            id: no.getAttribute('id') || '',
            classes: (no.className || '').toString()
              .split(/\s+/).filter(Boolean).slice(0, 4),
            oculto: !renderizado(no),
          });
          no = no.parentElement;
        }
        return {campo: el.getAttribute('name'), visivel: visivel(el), cadeia};
      });
      const alternadores = Array.from(
        document.querySelectorAll('[onclick], [alt], [title]')
      ).filter((el) => {
        if (!visivel(el)) return false;
        const pista = ((el.getAttribute('alt') || '') + ' ' +
                       (el.getAttribute('title') || '') + ' ' +
                       (el.getAttribute('onclick') || '')).toLowerCase();
        return pista.includes('lupa') || pista.includes('pesquis') ||
               pista.includes('toggle') || pista.includes('expand');
      }).slice(0, 12).map((el) => ({
        tag: el.tagName.toLowerCase(),
        id: el.getAttribute('id') || '',
        classes: (el.className || '').toString()
          .split(/\s+/).filter(Boolean).slice(0, 4),
        alt: amostra(el.getAttribute('alt')),
        title: amostra(el.getAttribute('title')),
        texto: amostra(el.textContent).slice(0, 40),
        onclick: amostra(el.getAttribute('onclick')),
      }));
      return {
        abas, formularios, botoes, campos_visiveis, iframes,
        paginacao, contador: contador.slice(0, 6), linhas_lista,
        criterios, alternadores,
      };
    }
    """

    _MAPA_AUTOS = r"""
() => {
  const amostra = (texto) => (texto || '').replace(/\s+/g, ' ').trim().slice(0, 120);
  const descrever = (el) => ({
    tag: el.tagName.toLowerCase(),
    classes: (el.className || '').toString().split(/\s+/).filter(Boolean).slice(0, 6),
    atributos: Array.from(el.attributes).map((a) => a.name).slice(0, 12),
    texto: amostra(el.innerText),
  });

  const timeline = document.querySelector('#divTimeLine');
  const raiz = {
    timeline_existe: Boolean(timeline),
    filhos_diretos: timeline
      ? Array.from(timeline.children).slice(0, 6).map(descrever)
      : [],
  };

  // Contagem por seletor: mostra de imediato qual premissa do extrator quebrou.
  const contagens = {};
  for (const sel of [
    '#divTimeLine .media.tipo-D',
    '#divTimeLine .media',
    '#divTimeLine .media.tipo-D [onclick*="abrirLinkDocumento"]',
    '[onclick*="abrirLinkDocumento"]',
    '#divTimeLine > div.media',
    '.timeline-item', '.documento', '[id^="divTimeLine"]',
  ]) {
    contagens[sel] = document.querySelectorAll(sel).length;
  }

  // Para os primeiros documentos, o que cada seletor de título devolveria.
  const alvos = Array.from(
    document.querySelectorAll('[onclick*="abrirLinkDocumento"]')
  ).slice(0, 4);
  const documentos = alvos.map((el) => {
    const container =
      el.closest('.media.tipo-D') || el.closest('.media') || el.parentElement;
    const candidatos = {};
    for (const sel of [
      'span.title', '[title]', 'strong', 'a span', 'a', '.titulo',
      '.media-heading', '.nomeArquivo', 'span',
    ]) {
      const achado = container ? container.querySelector(sel) : null;
      candidatos[sel] = achado ? amostra(achado.textContent) : null;
    }
    return {
      onclick: amostra(el.getAttribute('onclick')),
      texto_do_proprio_elemento: amostra(el.textContent),
      titulo_do_atributo: el.getAttribute('title'),
      container: container ? descrever(container) : null,
      candidatos_de_titulo: candidatos,
      ancestrais: (() => {
        const cadeia = [];
        let no = el.parentElement;
        for (let i = 0; i < 4 && no; i += 1) {
          cadeia.push({
            tag: no.tagName.toLowerCase(),
            classes: (no.className || '').toString().split(/\s+/).filter(Boolean).slice(0, 5),
          });
          no = no.parentElement;
        }
        return cadeia;
      })(),
    };
  });

  return {raiz, contagens, documentos};
}
    """

    # Campos de critério do formulário de busca do Acervo, verificados na tela em
    # 05/09/2026. A lista é nominal e fechada: preencher só o que está aqui impede
    # que um nome novo do PJe vire critério sem alguém ter olhado para ele.
    _CRITERIOS_ACERVO: ClassVar[dict[str, str]] = {
        "parte": "formAcervo:itDestPend",
        "documento": "formAcervo:itIMF",
        "oab": "formAcervo:itOAB",
        "classe": "formAcervo:itCL",
        "assunto": "formAcervo:itAS",
    }
    # A mesma tela traz um "Peticionar" por linha de processo. O disparo da busca é
    # fixado por id e conferido pelo rótulo — jamais escolhido por texto de botão.
    _BOTAO_PESQUISAR = 'input[id="formAcervo:btPesqAc"]'
    # Os critérios moram num dropdown do Bootstrap (div#formAcervo:divCampos
    # .dropdown-menu). Ficam no DOM o tempo todo, mas sem visibilidade até o
    # alternador ser acionado — e fill() espera por visibilidade. Há outro ícone de
    # lupa na tela (#btnPesquisarContexto, "Pesquise por número de processo"); este
    # é fixado pelo título para não trocar um pelo outro.
    _ABRIR_BUSCA_ACERVO = 'a.dropdown-toggle[title="Pesquisar nesta caixa"]'

    _JURISDICAO_LINK = 'a[id^="formAbaAcervo:trAc:"]'
    _LISTA_ACERVO = '#divListaAcervo, table[id$=":tbProcessos"]'
    # Datascroller do RichFaces observado no Acervo: os botões disparam um evento
    # próprio, não um clique comum, e o de avançar fica desabilitado na última.
    # A aba traz mais de um datascroller: o do histórico de movimentação
    # (formLogHistMov:tbHistoricoMov:logTable) vem antes no DOM e nunca tem próxima
    # página habilitada. Pegar o primeiro 'div.rich-datascr' fazia a paginação parar
    # na página 1 sem erro — observado no TJPE em 2026-09-08. Amarra-se à tabela de
    # processos, não à posição.
    _SCROLLER = 'div.rich-datascr[id*=":tbProcessos:"]'
    _SCROLLER_PROXIMA = (
        'div.rich-datascr[id*=":tbProcessos:"] '
        "td.rich-datascr-button:not(.rich-datascr-button-dsbld)"
    )
    _LISTA_ACERVO_LINK = '#divListaAcervo a, table[id$=":tbProcessos"] a'

    @classmethod
    async def _acervo_jurisdicoes(cls, page: Page) -> list[JurisdicaoAcervo]:
        """Jurisdições visíveis, sem clicar em nenhuma, separando nome e contagem."""
        raw = await page.locator(cls._JURISDICAO_LINK).evaluate_all(
            """
            (links) => links
              .filter((a) => a.getClientRects().length > 0)
              .map((a) => (a.innerText || '').replace(/\\s+/g, ' ').trim())
              .filter((text) => text.length > 0 && text.length <= 80)
            """
        )
        rotulos = cast(list[str], raw)
        vistos: dict[str, JurisdicaoAcervo] = {}
        for rotulo in rotulos:
            nome, processos = _split_jurisdicao(rotulo)
            vistos.setdefault(nome, JurisdicaoAcervo(nome=nome, processos=processos))
        return sorted(vistos.values(), key=lambda item: item.nome)

    async def _select_acervo_jurisdicao(self, page: Page, jurisdicao: str) -> int | None:
        alvo = _fold_text(jurisdicao)
        if not alvo:
            raise ValidacaoError("informe a jurisdição do Acervo a consultar")

        candidatos = page.locator(self._JURISDICAO_LINK)
        exatos: list[tuple[Locator, int | None]] = []
        prefixos: list[tuple[str, Locator, int | None]] = []
        for index in range(await candidatos.count()):
            candidato = candidatos.nth(index)
            if not await candidato.is_visible():
                continue
            nome, quantidade = _split_jurisdicao(await candidato.inner_text())
            comparavel = _fold_text(nome)
            if not comparavel:
                continue
            # O nome exato vence o prefixo: "Olinda - Varas" não pode ficar ambíguo
            # só porque existe uma jurisdição cujo nome começa igual.
            if comparavel == alvo:
                exatos.append((candidato, quantidade))
            elif comparavel.startswith(alvo):
                prefixos.append((nome, candidato, quantidade))

        if len(exatos) == 1:
            escolhido, quantidade_escolhida = exatos[0]
        elif not exatos and len(prefixos) == 1:
            escolhido, quantidade_escolhida = prefixos[0][1], prefixos[0][2]
        elif exatos or prefixos:
            opcoes = ", ".join(sorted(nome for nome, _, _q in prefixos)) or jurisdicao
            raise ValidacaoError(
                f"a jurisdição {jurisdicao!r} é ambígua no Acervo; "
                f"informe o nome completo. Candidatas: {opcoes}"
            )
        else:
            disponiveis = ", ".join(
                item.nome for item in await self._acervo_jurisdicoes(page)
            ) or "nenhuma"
            raise ValidacaoError(
                f"a jurisdição {jurisdicao!r} não aparece no seu Acervo. "
                f"Disponíveis: {disponiveis}"
            )

        # Nada limpa a lista antes do clique: se o painel já tinha outra jurisdição
        # carregada, os links dela seguem no DOM e uma espera por "existe link"
        # é satisfeita na hora, pela lista errada. Guarda-se o estado anterior para
        # esperar a troca de fato — como a paginação e a busca já fazem.
        assinatura_antes = await self._assinatura_lista_acervo(page)
        await escolhido.click()
        # A lista carrega por a4j. O container aparecer é o que prova que a interface
        # continua a esperada; ele vir sem links é uma jurisdição legitimamente sem
        # processos — estado normal do Acervo, não interface alterada.
        try:
            await page.locator(self._LISTA_ACERVO).first.wait_for(
                state="attached", timeout=self.config.timeout_ms
            )
        except PlaywrightTimeoutError:
            raise InterfacePjeAlteradaError(
                f"a jurisdição {jurisdicao!r} foi selecionada, mas o Acervo não "
                "apresentou o container de resultados esperado"
            ) from None
        if assinatura_antes:
            limite = time.monotonic() + self.config.timeout_ms / 1_000
            while time.monotonic() < limite:
                if await self._assinatura_lista_acervo(page) != assinatura_antes:
                    break
                await page.wait_for_timeout(_INTERVALO_TROCA_LISTA_MS)
        else:
            with suppress(PlaywrightTimeoutError):
                await page.locator(self._LISTA_ACERVO_LINK).first.wait_for(
                    state="attached", timeout=self.config.timeout_ms
                )
        return quantidade_escolhida

    async def _assinatura_lista_acervo(self, page: Page) -> str:
        """Identidade da lista renderizada, para saber quando ela trocou de fato."""
        destinos = await page.locator(self._LISTA_ACERVO_LINK).evaluate_all(
            "(links) => links.slice(0, 20).map((a) => a.getAttribute('href') || '')"
        )
        return "|".join(cast(list[str], destinos))

    @staticmethod
    async def _extract_acervo_entries(page: Page) -> list[dict[str, str]]:
        raw = await page.locator("a").evaluate_all(
            r"""
            (anchors) => {
              const selectedRoots = [];
              for (const tab of document.querySelectorAll(
                '[role="tab"][aria-selected="true"]'
              )) {
                if (!/acervo/i.test(tab.textContent || '')) continue;
                const controls = (tab.getAttribute('aria-controls') || '').trim();
                if (controls) {
                  const root = document.getElementById(controls);
                  if (root) selectedRoots.push(root);
                }
              }
              // O painel do TJPE é RichFaces e não expõe ARIA nenhum: sem
              // [role="tab"][aria-selected] nem aria-controls. Nesse caso a raiz é o
              // container de resultados, pelo id real da página. Alternativa aditiva:
              // interfaces com ARIA seguem resolvendo pelo caminho acima.
              if (selectedRoots.length === 0) {
                for (const selector of [
                  '#divListaAcervo',
                  'table[id$=":tbProcessos"]'
                ]) {
                  const root = document.querySelector(selector);
                  if (root) { selectedRoots.push(root); break; }
                }
              }
              const blockedSelector = [
                '[id*="expediente" i]', '[class*="expediente" i]',
                '[id*="intimacao" i]', '[class*="intimacao" i]',
                '[id*="agrupador" i]', '[class*="agrupador" i]'
              ].join(',');
              const npuPattern = /\b\d{7}-\d{2}\.\d{4}\.8\.17\.\d{4}\b/;
              if (selectedRoots.length !== 1) {
                return {rootCount: selectedRoots.length, entries: []};
              }
              const entries = anchors.filter((a) => {
                const style = window.getComputedStyle(a);
                if (
                  a.getClientRects().length === 0 ||
                  style.visibility === 'hidden' || style.display === 'none' ||
                  a.closest(blockedSelector)
                ) return false;
                return selectedRoots[0].contains(a);
              })
              .map((a) => {
                const container = a.closest(
                  'tr, li, .media, .rich-table-row, [class*="processo"]'
                );
                if (!container) return null;
                const context = (container.innerText || '').trim().slice(0, 2000);
                if (!npuPattern.test(context)) return null;
                return {
                  text: (a.innerText || a.textContent || '').trim(),
                  context,
                  href: a.getAttribute('href') || '',
                  absoluteHref: a.href || '',
                  onclick: a.getAttribute('onclick') || '',
                };
              })
              .filter(Boolean);
              return {rootCount: 1, entries};
            }
            """
        )
        payload = cast(dict[str, object], raw)
        if payload.get("rootCount") != 1 or not isinstance(payload.get("entries"), list):
            raise InterfacePjeAlteradaError(
                "o PJe não apresentou uma única raiz estrutural para a aba Acervo"
            )
        return cast(list[dict[str, str]], payload["entries"])

    async def _estrutura_embutida(
        self, page: Page, embutida: str | None
    ) -> tuple[dict[str, Any] | None, list[str], str | None]:
        """Lê a página que a aba embute, quando ela é só uma casca.

        Devolve também os caminhos dos quadros vistos: quando a descida não acontece,
        sem eles a aba volta descrita como moldura vazia e nada diz o porquê.

        Só desce em quadro do mesmo host do PJe: um iframe apontando para fora não é
        conteúdo do painel e não deve ser descrito como se fosse.
        """
        if not embutida:
            return None, [], None
        alvo = urlparse(urljoin(page.url, embutida))
        if alvo.hostname != urlparse(page.url).hostname:
            return None, [], "o quadro embutido aponta para fora do host do PJe"

        limite = time.monotonic() + _ESPERA_QUADRO_SEGUNDOS
        vistos: list[str] = []
        while True:
            vistos = []
            for quadro in page.frames:
                if quadro == page.main_frame:
                    continue
                caminho = _caminho_sem_sessao(quadro.url)
                vistos.append(caminho)
                if caminho != _caminho_sem_sessao(alvo.path):
                    continue
                try:
                    bruto = await quadro.evaluate(self._MAPA_ABA)
                except PlaywrightError as erro:
                    # Engolir isto foi o que deixou a aba Push voltar como moldura
                    # vazia sem uma linha dizendo por quê.
                    motivo = _compact(str(erro))[:200]
                    if time.monotonic() >= limite:
                        return None, vistos, motivo
                    await page.wait_for_timeout(300)
                    break
                return cast(dict[str, Any], bruto), vistos, None
            else:
                # O a4j troca o conteúdo da aba depois do clique: o quadro pode ainda
                # não ter navegado quando a fotografia é tirada.
                if time.monotonic() >= limite:
                    return None, vistos, "nenhum quadro correspondeu à página embutida"
                await page.wait_for_timeout(300)

    async def _repor_tela_do_vinculo(
        self, page: Page, grau: Grau, vinculo: _AcervoBinding
    ) -> None:
        """Repõe a tela que produziu o vínculo, com a mesma busca.

        Recarregar só a jurisdição renderiza os primeiros processos dela. Um processo
        achado pela pesquisa não está entre eles, e a revalidação o dá por sumido: a
        busca encontraria processos que nunca poderiam ser abertos.
        """
        await self._load_acervo(page, grau, vinculo.jurisdicao or None)
        if vinculo.criterios:
            await self._pesquisar_no_acervo(page, dict(vinculo.criterios))

    async def _refresh_acervo_binding(self, lease: ReadSessionLease, numero: str) -> _AcervoBinding:
        """Renova internamente o link assinado sem clicar no processo."""
        anterior = self._require_acervo_binding(lease, numero)
        entries = await self._extract_acervo_entries(lease.page)
        for entry in entries:
            parsed_items = parse_acervo_entries([entry], lease.grau)
            if not parsed_items or parsed_items[0].numero != numero:
                continue
            target = _audited_autos_target(entry, lease.grau)
            if target is None:
                continue
            autos_url, processo_id = target
            key = (lease.generation, lease.grau, numero)
            binding = _AcervoBinding(
                generation=lease.generation,
                grau=lease.grau,
                numero=numero,
                processo_id=processo_id,
                autos_url=autos_url,
                jurisdicao=anterior.jurisdicao,
            )
            self._acervo[key] = binding
            self._acervo.move_to_end(key)
            return binding
        raise InterfacePjeAlteradaError(
            "o processo não apresentou mais um link GET auditado no Acervo; "
            "nenhuma navegação foi realizada"
        )

    def _require_acervo_binding(self, lease: ReadSessionLease, numero: str) -> _AcervoBinding:
        key = (lease.generation, lease.grau, numero)
        binding = self._acervo.get(key)
        if binding is None:
            raise ValidacaoError(
                "o processo não possui um link de Autos auditado no Acervo desta sessão. "
                "Execute listar_acervo primeiro e confirme autos_disponiveis=true; o MCP "
                "não trata um vínculo apenas da Pesquisa Geral como autorização do Acervo"
            )
        self._acervo.move_to_end(key)
        return binding

    def possui_vinculo_acervo(self, lease: ReadSessionLease, numero: str) -> bool:
        """Informa apenas se o NPU já foi auditado no Acervo desta sessão e grau."""
        formatted = normalize_npu_tjpe(numero)
        return (lease.generation, lease.grau, formatted) in self._acervo

    def _require_pesquisa_geral_binding(
        self,
        lease: ReadSessionLease,
        numero: str,
    ) -> _PesquisaGeralBinding:
        key = (lease.generation, lease.grau, numero)
        binding = self._pesquisa_geral.get(key)
        if binding is None:
            raise ValidacaoError(
                "o processo não foi visto no Acervo nem aberto pela pesquisa geral "
                "confirmada nesta sessão e grau"
            )
        self._pesquisa_geral.move_to_end(key)
        return binding

    async def _open_cached_pesquisa_geral(
        self,
        lease: ReadSessionLease,
        binding: _PesquisaGeralBinding,
    ) -> tuple[Page, BrowserContext, str]:
        if (
            binding.generation != lease.generation
            or binding.grau != lease.grau
            or binding.numero != normalize_npu_tjpe(binding.numero)
        ):
            raise ValidacaoError("o acesso geral pertence a outra sessão, grau ou processo")
        browser = lease.context.browser
        if browser is None:
            raise ServicoIndisponivelError(
                "o navegador autenticado não permite criar o leitor isolado dos Autos"
            )
        offline_context = await browser.new_context(
            accept_downloads=False,
            java_script_enabled=False,
            service_workers="block",
        )

        async def block_all_network(route: Route) -> None:
            await route.abort("blockedbyclient")

        await offline_context.route("**/*", block_all_network)
        page = await offline_context.new_page()
        page.set_default_timeout(self.config.timeout_ms)
        try:
            await page.set_content(
                binding.autos_html,
                wait_until="domcontentloaded",
                timeout=self.config.timeout_ms,
            )
            body_text = await page.locator("body").inner_text()
            if binding.numero not in body_text:
                raise InterfacePjeAlteradaError(
                    "o cache autenticado dos Autos não confirmou o NPU esperado"
                )
            return page, offline_context, binding.processo_id
        except Exception:
            await offline_context.close()
            raise

    async def _open_autos_from_acervo(
        self,
        lease: ReadSessionLease,
        numero: str,
    ) -> tuple[Page, BrowserContext, str]:
        binding = self._require_acervo_binding(lease, numero)
        if _autos_process_id(binding.autos_url, lease.grau) != binding.processo_id:
            raise InterfacePjeAlteradaError(
                "a rota dos Autos deixou de corresponder ao processo auditado"
            )
        cookie_header = await _browser_cookie_header(lease.context, binding.autos_url)
        user_agent = cast(str, await lease.page.evaluate("navigator.userAgent"))
        try:
            body, content_type = await asyncio.to_thread(
                _stream_document_https,
                binding.autos_url,
                cookie_header=cookie_header,
                user_agent=user_agent,
                max_bytes=_MAX_AUTOS_HTML_BYTES,
                timeout_seconds=max(self.config.timeout_ms / 1_000, 1.0),
            )
            lowered = body[:8_192].lower()
            mime = content_type.split(";", maxsplit=1)[0].strip().casefold()
            if any(marker in lowered for marker in _LOGIN_MARKERS):
                raise CredenciaisAusentesError("a sessão expirou durante a leitura dos Autos")
            if mime not in {"text/html", "application/xhtml+xml", ""} or not (
                b"<html" in lowered or b"<!doctype html" in lowered
            ):
                raise InterfacePjeAlteradaError(
                    "a rota auditada dos Autos não devolveu HTML reconhecido"
                )

            browser = lease.context.browser
            if browser is None:
                raise ServicoIndisponivelError(
                    "o navegador autenticado não permite criar o leitor isolado dos Autos"
                )
            offline_context = await browser.new_context(
                accept_downloads=False,
                java_script_enabled=False,
                service_workers="block",
            )

            async def block_all_network(route: Route) -> None:
                await route.abort("blockedbyclient")

            await offline_context.route("**/*", block_all_network)
            autos_page = await offline_context.new_page()
            autos_page.set_default_timeout(self.config.timeout_ms)
            try:
                await autos_page.set_content(
                    body.decode("utf-8", errors="replace"),
                    wait_until="domcontentloaded",
                    timeout=self.config.timeout_ms,
                )
                body_text = await autos_page.locator("body").inner_text()
                if numero not in body_text:
                    raise InterfacePjeAlteradaError(
                        "os Autos obtidos não confirmaram o NPU selecionado no Acervo"
                    )
                return autos_page, offline_context, binding.processo_id
            except Exception:
                await offline_context.close()
                raise
        except (
            CredenciaisAusentesError,
            InterfacePjeAlteradaError,
            ServicoIndisponivelError,
            ValidacaoError,
        ):
            raise
        except Exception:
            raise ServicoIndisponivelError(
                "não foi possível obter os Autos Digitais auditados"
            ) from None

    async def _extract_header(self, page: Page, numero: str) -> dict[str, str]:
        text = mask_cpf_cnpj(await page.locator("body").inner_text())
        if numero not in text:
            raise InterfacePjeAlteradaError("o cabeçalho dos Autos não contém o NPU selecionado")
        return parse_autos_header(text)

    async def _extract_documents(
        self,
        page: Page,
        limit: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        selector = "#divTimeLine .media.tipo-D [onclick*='abrirLinkDocumento']"
        previous = -1
        stable = 0
        exhausted = False
        for _ in range(40):
            count = await page.locator(selector).count()
            if count >= limit:
                break
            if count == previous:
                stable += 1
                if stable >= 2:
                    exhausted = True
                    break
            else:
                stable = 0
            previous = count
            await page.evaluate(
                """
                () => {
                  const docs = document.querySelectorAll(
                    '#divTimeLine .media.tipo-D [onclick*="abrirLinkDocumento"]'
                  );
                  if (docs.length) docs[docs.length - 1].scrollIntoView({block: 'end'});
                  for (const selector of [
                    '#tabPanelDocs', '.scroll-y', '.documentos-panel',
                    '#documentos', '#divTimeLine', 'aside'
                  ]) {
                    const element = document.querySelector(selector);
                    if (element) element.scrollTop = element.scrollHeight;
                  }
                }
                """
            )
            await page.wait_for_timeout(250)

        raw = await page.locator(selector).evaluate_all(
            r"""
            (elements, limit) => {
              const result = [];
              const seen = new Set();
              for (const element of elements) {
                if (result.length >= limit) break;
                const onclick = element.getAttribute('onclick') || '';
                const match = onclick.match(
                  /abrirLinkDocumento\(['"]?(\d+)['"]?\)/
                );
                if (!match || seen.has(match[1])) continue;
                seen.add(match[1]);
                const container = element.closest('.media.tipo-D');
                if (!container) continue;
                const context = (container.innerText || '').trim().slice(0, 2000);
                // O rótulo real é o <a> cujo texto começa pelo id ("196750071 -
                // Petição (Outras)"). A lista de seletores antiga casava primeiro o
                // elemento com [title]="Abrir documento...", cujo texto é vazio, e
                // todo documento caía no rótulo genérico. O escopo é a linha do
                // próprio anexo quando ela existe, para não herdar o título do pai.
                const escopo = element.closest('li') || container;
                const rotulo = Array.from(escopo.querySelectorAll('a'))
                  .map((a) => (a.innerText || a.textContent || '').trim())
                  .find((texto) => texto.startsWith(match[1]));
                const title = (rotulo || element.textContent || '').trim();
                result.push({id: match[1], title, context});
              }
              return result;
            }
            """,
            limit,
        )
        entries = cast(list[dict[str, Any]], raw)
        if not entries:
            raise InterfacePjeAlteradaError(
                "os Autos não apresentaram a árvore auditada de documentos"
            )
        return entries, not exhausted or len(entries) >= limit

    async def _extract_movements(self, page: Page, limit: int) -> list[MovimentoAutos]:
        if limit == 0:
            return []
        timeline = page.locator("form#divTimeLine, #divTimeLine").first
        if await timeline.count() == 0:
            raise InterfacePjeAlteradaError("os Autos não apresentaram a linha do tempo auditada")
        raw = await timeline.evaluate(
            """
            (timeline, limit) => {
              const result = [];
              let currentDate = '';
              // As linhas não são filhas diretas da timeline: ':scope > div.media'
              // devolvia zero e a lista de movimentos vinha sempre vazia. Selecionar
              // por estrutura — as .media que não estão dentro de outra .media — não
              // depende de adivinhar a taxonomia de classes do PJe.
              const todas = Array.from(timeline.querySelectorAll('.media'));
              const entries = todas.filter(
                (el) => !todas.some((outra) => outra !== el && outra.contains(el))
              );
              for (const element of entries) {
                if (element.classList.contains('data')) {
                  currentDate = (
                    element.querySelector('span.data-interna, time')?.textContent ||
                    element.textContent || ''
                  ).trim().slice(0, 100);
                  continue;
                }
                const body = element.querySelector('.media-body') || element;
                const text = (body.innerText || body.textContent || '').trim();
                if (!text) continue;
                result.push({text: text.slice(0, 3000), date: currentDate});
                if (result.length >= limit) break;
              }
              return result;
            }
            """,
            limit,
        )
        movements: list[MovimentoAutos] = []
        for item in cast(list[dict[str, str]], raw):
            description = mask_cpf_cnpj(_compact(item.get("text", "")))
            if not description:
                continue
            raw_date = _compact(item.get("date", ""))
            date = _DATE.search(raw_date or description)
            # O PJe encerra a linha com o horário do ato, sem separador. Deixá-lo na
            # descrição produz "Conclusos para despacho 07:49", que não é o nome de
            # movimento nenhum e estraga qualquer agrupamento por descrição.
            hora = _HORA_FINAL.search(description)
            if hora:
                description = description[: hora.start()].rstrip()
            movements.append(
                MovimentoAutos(
                    data=date.group(0) if date else raw_date or None,
                    hora=hora.group(1) if hora else None,
                    descricao=description,
                )
            )
        return movements

    def _register_documents(
        self,
        entries: list[dict[str, Any]],
        *,
        lease: ReadSessionLease,
        numero: str,
        processo_id: str,
        conteudo_disponivel: bool = True,
    ) -> list[DocumentoAutos]:
        documents: list[DocumentoAutos] = []
        seen: set[str] = set()
        for entry in entries:
            document_id = str(entry.get("id", ""))
            if not _DOCUMENT_ID.fullmatch(document_id) or document_id in seen:
                continue
            seen.add(document_id)
            context = _compact(str(entry.get("context", "")))
            title = _compact(str(entry.get("title", "")))
            if not title:
                title = f"Documento {document_id}"
            title = re.sub(rf"^{re.escape(document_id)}\s*[-{chr(8211)}:]?\s*", "", title)
            title = mask_cpf_cnpj(title)[:300] or f"Documento {document_id}"
            blocked = _BLOCKED_DOCUMENT.search(f"{title} {context}") is not None
            date_match = _DATE.search(context)
            reference = secrets.token_urlsafe(24)
            self._documents[reference] = _DocumentBinding(
                generation=lease.generation,
                grau=lease.grau,
                numero=numero,
                processo_id=processo_id,
                documento_id=document_id,
                titulo=title,
                bloqueado_por_ciencia=blocked,
            )
            documents.append(
                DocumentoAutos(
                    referencia=reference,
                    id_exibido=document_id,
                    titulo=title,
                    data=date_match.group(0) if date_match else None,
                    bloqueado_por_ciencia=blocked,
                    conteudo_disponivel=conteudo_disponivel and not blocked,
                )
            )
        while len(self._documents) > self.registry_limit:
            self._documents.popitem(last=False)
        if not documents:
            raise InterfacePjeAlteradaError(
                "a árvore dos Autos não apresentou identificadores de documento válidos"
            )
        return documents

    async def _fetch_registered_document(
        self,
        lease: ReadSessionLease,
        numero: str,
        grau: Grau,
        reference: str,
    ) -> _FetchedDocument:
        binding = self._resolve_document(lease, numero, grau, reference)
        current, user_agent = await self._revalidated_autos(lease, numero, grau, binding)
        if binding.documento_id not in current:
            raise ValidacaoError("o documento não aparece mais nos Autos desta sessão")
        if _BLOCKED_DOCUMENT.search(current[binding.documento_id]):
            raise ValidacaoError(
                "o documento está associado a uma ação pendente de ciência; "
                "a leitura foi bloqueada"
            )
        return await self._download_document_bytes(
            lease.context,
            binding,
            user_agent=user_agent,
        )

    async def _revalidated_autos(
        self,
        lease: ReadSessionLease,
        numero: str,
        grau: Grau,
        binding: _DocumentBinding,
    ) -> tuple[dict[str, str], str]:
        """Estado dos documentos, revalidado de fato ou reaproveitado por pouco tempo.

        Baixar uma peça exige provar de novo que o processo segue no Acervo e que o
        documento não passou a estar pendente de ciência. Refazer essa prova para cada
        peça de um mesmo processo, em segundos, repete a navegação inteira sem trazer
        informação nova — ver `_REVALIDACAO_AUTOS_SEGUNDOS`.
        """
        chave = (lease.generation, grau, numero)
        agora = time.monotonic()
        recente = self._autos_revalidados.get(chave)
        if recente is not None and agora - recente[0] <= _REVALIDACAO_AUTOS_SEGUNDOS:
            self._autos_revalidados.move_to_end(chave)
            return recente[1], recente[2]

        vinculo = self._require_acervo_binding(lease, numero)
        await self._repor_tela_do_vinculo(lease.page, grau, vinculo)
        await self._refresh_acervo_binding(lease, numero)
        autos_page, autos_context, process_id = await self._open_autos_from_acervo(lease, numero)
        try:
            if process_id != binding.processo_id:
                raise ValidacaoError("a referência não corresponde mais ao processo desta sessão")
            entries, _ = await self._extract_documents(autos_page, 500)
            current = {
                str(entry.get("id")): _compact(str(entry.get("context", ""))) for entry in entries
            }
            user_agent = cast(str, await autos_page.evaluate("navigator.userAgent"))
        finally:
            await autos_context.close()

        self._autos_revalidados[chave] = (agora, current, user_agent)
        self._autos_revalidados.move_to_end(chave)
        while len(self._autos_revalidados) > self.registry_limit:
            self._autos_revalidados.popitem(last=False)
        return current, user_agent

    def invalidar_revalidacao_autos(self, lease: ReadSessionLease, numero: str) -> None:
        """Descarta a janela de revalidação para forçar a prova completa na próxima peça."""
        self._autos_revalidados.pop((lease.generation, lease.grau, numero), None)

    def _resolve_document(
        self,
        lease: ReadSessionLease,
        numero: str,
        grau: Grau,
        reference: str,
    ) -> _DocumentBinding:
        if not _REFERENCE.fullmatch(reference):
            raise ValidacaoError("referência de documento inválida")
        binding = self._documents.get(reference)
        if binding is None:
            raise ValidacaoError(
                "referência de documento desconhecida ou expirada; consulte os Autos novamente"
            )
        if (
            binding.generation != lease.generation
            or binding.grau != grau
            or binding.numero != numero
        ):
            raise ValidacaoError("a referência pertence a outra sessão, grau ou processo")
        if binding.bloqueado_por_ciencia:
            raise ValidacaoError(
                "o documento foi sinalizado como pendente de ciência e não pode ser aberto"
            )
        key = (lease.generation, grau, numero)
        if key in self._pesquisa_geral and key not in self._acervo:
            raise ValidacaoError(
                "o conteúdo de documentos obtidos pela pesquisa geral não está liberado; "
                "liste o processo no Acervo para ler ou baixar arquivos nesta versão"
            )
        self._require_acervo_binding(lease, numero)
        return binding

    async def _download_document_bytes(
        self,
        context: BrowserContext,
        binding: _DocumentBinding,
        *,
        user_agent: str,
    ) -> _FetchedDocument:
        url = (
            f"{self.config.urls.pje_base(binding.grau.value)}"
            "/seam/resource/rest/pje-legacy/documento/download/"
            f"TJPE/{binding.grau.value}/{binding.processo_id}/{binding.documento_id}"
        )
        try:
            if not _is_exact_document_url(url, binding):
                raise InterfacePjeAlteradaError(
                    "a rota local calculada para o documento não passou na validação"
                )
            cookie_header = await _browser_cookie_header(context, url)
            body, content_type = await asyncio.to_thread(
                _stream_document_https,
                url,
                cookie_header=cookie_header,
                user_agent=user_agent,
                max_bytes=self.config.max_document_bytes,
                timeout_seconds=max(self.config.timeout_ms / 1_000, 1.0),
            )
            mime_type = _validated_mime(content_type, body)
            return _FetchedDocument(binding=binding, body=body, mime_type=mime_type)
        except (
            CredenciaisAusentesError,
            InterfacePjeAlteradaError,
            ServicoIndisponivelError,
            ValidacaoError,
        ):
            raise
        except Exception:
            raise ServicoIndisponivelError(
                "não foi possível baixar o documento autenticado do TJPE"
            ) from None

    def _publish_download(self, fetched: _FetchedDocument) -> Path:
        root = self.config.downloads_dir.expanduser().resolve()
        process_dir = _secure_subdirectory(
            root,
            "TJPE",
            fetched.binding.grau.value,
            fetched.binding.numero,
            "documentos",
        )

        slug = _safe_slug(fetched.binding.titulo)
        extension = _MIME_EXTENSIONS[fetched.mime_type]
        filename = f"{fetched.binding.documento_id}-{slug}-{fetched.sha256[:12]}{extension}"
        target = process_dir / filename
        _atomic_publish(target, fetched.body)
        return target

    def _publish_download_bundle(self, fetched: _FetchedDocument) -> tuple[Path, Path]:
        path = self._publish_download(fetched)
        sidecar = path.with_suffix(path.suffix + ".sha256")
        _atomic_publish(
            sidecar,
            f"{fetched.sha256}  {path.name}\n".encode("ascii"),
        )
        return path, sidecar


def parse_acervo_entries(entries: list[dict[str, str]], grau: Grau) -> list[ItemAcervo]:
    items: list[ItemAcervo] = []
    seen: set[str] = set()
    for entry in entries:
        text = _compact(entry.get("text", ""))
        context = _compact(entry.get("context", ""))
        match = _NPU.search(text) or _NPU.search(context)
        if match is None or _BLOCKED_ACERVO.search(context):
            continue
        numero = match.group(0)
        if numero in seen:
            continue
        try:
            numero = normalize_npu_tjpe(numero)
        except ValidacaoError:
            continue
        seen.add(numero)
        summary = mask_cpf_cnpj(context or text)[:1_000]
        items.append(
            ItemAcervo(
                numero=numero,
                grau=grau,
                resumo=summary,
                restrito=bool(re.search(r"segredo\s+de\s+justi[cç]a|sigiloso", summary, re.I)),
            )
        )
    return items


def parse_autos_header(text: str) -> dict[str, str]:
    lines = [_compact(line) for line in text.splitlines() if _compact(line)]
    result: dict[str, str] = {}
    for label in _HEADER_LABELS:
        folded = label.casefold()
        for index, line in enumerate(lines):
            current = line.casefold()
            if current == folded and index + 1 < len(lines):
                result[label] = lines[index + 1][:1_000]
                break
            if current.startswith(folded + ":"):
                result[label] = line.split(":", maxsplit=1)[1].strip()[:1_000]
                break
    return result


def _page_is_partial(text: str) -> bool:
    match = _PAGINATION.search(text)
    return bool(match and int(match.group("atual")) < int(match.group("total")))


# O rótulo da jurisdição no painel do TJPE traz a contagem de processos colada pelo
# innerText ("Abreu e Lima - Varas 2"). O número muda quando o acervo muda, então
# devolvê-lo dentro do nome ensinaria o chamador a usar um identificador instável.
_JURISDICAO_CONTAGEM = re.compile(r"^(?P<nome>.+?)\s+(?P<processos>\d{1,7})$")


def _mascarar_amostras(no: dict[str, object]) -> dict[str, object]:
    """Mascara CPF/CNPJ em qualquer texto do mapa antes de ele sair do servidor."""
    limpo: dict[str, object] = {}
    for chave, valor in no.items():
        if isinstance(valor, str):
            limpo[chave] = mask_cpf_cnpj(valor)
        elif isinstance(valor, dict):
            limpo[chave] = _mascarar_amostras(cast(dict[str, object], valor))
        elif isinstance(valor, list):
            limpo[chave] = [
                _mascarar_amostras(cast(dict[str, object], item))
                if isinstance(item, dict)
                else (mask_cpf_cnpj(item) if isinstance(item, str) else item)
                for item in cast(list[Any], valor)
            ]
        else:
            limpo[chave] = valor
    return limpo


def _filtrar_acervo(itens: list[ItemAcervo], filtro: str) -> list[ItemAcervo]:
    """Filtra pelo resumo, exigindo todos os termos, sem acento e sem caixa.

    O resumo que o PJe monta para cada linha já traz classe, assunto, partes, órgão
    julgador e datas, então filtrar por ele cobre o que um advogado procura sem
    tocar no formulário de filtros do tribunal — que grava estado na caixa.
    """
    termos = [_fold_text(termo) for termo in filtro.split() if termo.strip()]
    if not termos:
        raise ValidacaoError("informe ao menos um termo em 'filtro'")
    return [
        item
        for item in itens
        if all(termo in _fold_text(f"{item.numero} {item.resumo}") for termo in termos)
    ]


_IFRAME_DECORATIVO = re.compile(r"(?:spacer|blank)\.(?:gif|png|html?)$", re.I)


def _pagina_embutida(iframes: list[str]) -> str | None:
    """Destino real da aba, quando ela é uma casca em torno de uma página .seam.

    O RichFaces espalha iframes decorativos (spacer.gif) pela página; o que
    interessa é o único que aponta para uma tela do PJe.
    """
    candidatos = [
        src
        for src in iframes
        if src and not _IFRAME_DECORATIVO.search(src) and ".seam" in src
    ]
    unicos = sorted(dict.fromkeys(candidatos))
    return unicos[0] if len(unicos) == 1 else None


def _rotulo_de_aba(abas: list[str], procurado: str) -> str | None:
    """Devolve o rótulo como a tela o escreve.

    O painel do TJPE usa "Consulta processos", não "Consulta Processos"; reportar a
    grafia da allowlist enganaria quem for usar o nome depois.
    """
    alvo = _fold_text(procurado)
    for aba in abas:
        if _fold_text(aba) == alvo:
            return aba
    return None


def _split_jurisdicao(rotulo: str) -> tuple[str, int | None]:
    match = _JURISDICAO_CONTAGEM.fullmatch(rotulo.strip())
    if match is None:
        return rotulo.strip(), None
    return match.group("nome").strip(), int(match.group("processos"))


def _fold_text(value: str) -> str:
    """Normaliza para comparar rótulos: sem acento, sem caixa, espaços colapsados."""
    normalized = unicodedata.normalize("NFD", " ".join(value.split()))
    return "".join(
        character for character in normalized if not unicodedata.combining(character)
    ).casefold()


async def _click_visible_exact(page: Page, label: str, timeout_ms: int = 0) -> bool:
    """Clica o controle exato, aguardando até o painel renderizá-lo."""
    pattern = re.compile(rf"^\s*{re.escape(label)}\s*$", re.IGNORECASE)
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        locators = (
            page.get_by_role("tab", name=pattern),
            page.get_by_role("link", name=pattern),
            page.get_by_role("button", name=pattern),
            # As abas do painel do TJPE são células RichFaces (<td>), sem papel
            # de acessibilidade; o texto continua exato e sensível a maiúsculas.
            page.locator("td.rich-tab-header").filter(has_text=pattern),
        )
        for locator in locators:
            for index in range(await locator.count()):
                candidate = locator.nth(index)
                if await candidate.is_visible() and await candidate.is_enabled():
                    await candidate.click()
                    return True
        if time.monotonic() >= deadline:
            return False
        await page.wait_for_timeout(250)


def _is_same_degree_url(url: str, grau: Grau) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.netloc == "pje.cloud.tjpe.jus.br"
        and parsed.path.startswith(f"/{grau.value}/")
        and "login" not in parsed.path.casefold()
    )


def _audited_autos_target(entry: dict[str, str], grau: Grau) -> tuple[str, str] | None:
    """Extrai somente uma rota GET de Autos literal e reconhecida no link do Acervo."""
    candidates = [entry.get("absoluteHref", ""), entry.get("href", "")]
    candidates.extend(
        match.group("value") for match in _QUOTED_JS_VALUE.finditer(entry.get("onclick", ""))
    )
    base = f"https://pje.cloud.tjpe.jus.br/{grau.value}/"
    for candidate in candidates:
        value = candidate.strip().replace("&amp;", "&")
        if not value or value.startswith(("#", "javascript:", "data:")):
            continue
        value = urljoin(base, value)
        process_id = _autos_process_id(value, grau)
        parsed = urlparse(value)
        classic_prefix = f"/{grau.value}/Processo/ConsultaProcesso/Detalhe/"
        if process_id is not None and parsed.path.startswith(classic_prefix):
            return value, process_id
    return None


def _autos_process_id(url: str, grau: Grau) -> str | None:
    parsed = urlparse(url)
    decoded_path = unquote(parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "pje.cloud.tjpe.jus.br"
        or ".." in decoded_path
        or "\\" in decoded_path
        or "//" in decoded_path
    ):
        return None

    prefix = f"/{grau.value}/Processo/ConsultaProcesso/Detalhe/"
    if decoded_path.startswith(prefix):
        if parsed.params or parsed.fragment:
            return None
        page_name = decoded_path.removeprefix(prefix)
        parameter = _ALLOWED_CLASSIC_AUTOS.get(page_name)
        if parameter is None:
            return None
        try:
            query = parse_qs(
                parsed.query,
                keep_blank_values=True,
                strict_parsing=True,
            )
        except ValueError:
            return None
        if not set(query).issubset({parameter, "ca"}):
            return None
        values = query.get(parameter, [])
        if len(values) == 1 and _DOCUMENT_ID.fullmatch(values[0]):
            auth_codes = query.get("ca", [])
            if auth_codes and (len(auth_codes) != 1 or _AUTH_CODE.fullmatch(auth_codes[0]) is None):
                return None
            return values[0]
        return None

    if decoded_path == f"/{grau.value}/ng2/dev.seam":
        if parsed.params or parsed.query:
            return None
        match = re.fullmatch(r"/?autos-digitais/(?P<id>[0-9]{1,20})/?", parsed.fragment)
        return match.group("id") if match else None
    return None


def audited_autos_target(entry: dict[str, str], grau: Grau) -> tuple[str, str] | None:
    """API interna compartilhada: valida uma rota literal reconhecida de Autos."""
    return _audited_autos_target(entry, grau)


def autos_process_id(url: str, grau: Grau) -> str | None:
    """API interna compartilhada: extrai o ID apenas de rotas de Autos permitidas."""
    return _autos_process_id(url, grau)


def is_same_degree_url(url: str, grau: Grau) -> bool:
    """API interna compartilhada: restringe navegação ao host e grau autenticados."""
    return _is_same_degree_url(url, grau)


def _is_exact_document_url(url: str, binding: _DocumentBinding) -> bool:
    parsed = urlparse(url)
    expected = (
        f"/{binding.grau.value}/seam/resource/rest/pje-legacy/documento/download/"
        f"TJPE/{binding.grau.value}/{binding.processo_id}/{binding.documento_id}"
    )
    return (
        parsed.scheme == "https"
        and parsed.netloc == "pje.cloud.tjpe.jus.br"
        and unquote(parsed.path) == expected
        and not parsed.params
        and not parsed.query
        and not parsed.fragment
    )


async def _browser_cookie_header(context: BrowserContext, url: str) -> str:
    cookies = await context.cookies([url])
    cookie_parts: list[str] = []
    for cookie in cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        if (
            not isinstance(name, str)
            or not isinstance(value, str)
            or _COOKIE_NAME.fullmatch(name) is None
            or not value.isascii()
            or any(character in value for character in "\r\n;")
        ):
            raise ServicoIndisponivelError(
                "a sessão contém um cookie que não pode ser enviado com segurança"
            )
        cookie_parts.append(f"{name}={value}")
    if not cookie_parts:
        raise CredenciaisAusentesError("a sessão autenticada não forneceu cookies para a leitura")
    return "; ".join(cookie_parts)


def _stream_document_https(
    url: str,
    *,
    cookie_header: str,
    user_agent: str,
    max_bytes: int,
    timeout_seconds: float,
) -> tuple[bytes, str]:
    """Baixa por HTTPS sem redirects e interrompe a leitura ao atingir o teto."""
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "pje.cloud.tjpe.jus.br"
        or parsed.params
        or parsed.fragment
    ):
        raise InterfacePjeAlteradaError("a rota de download não pertence ao TJPE")
    if max_bytes < 1:
        raise ValidacaoError("o limite local de documento deve ser positivo")

    safe_user_agent = user_agent
    if (
        not safe_user_agent.isascii()
        or any(character in safe_user_agent for character in "\r\n")
        or len(safe_user_agent) > 512
    ):
        safe_user_agent = "Mozilla/5.0 mcp-pje-tjpe/0.6"
    target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    connection = http.client.HTTPSConnection(
        "pje.cloud.tjpe.jus.br",
        443,
        timeout=timeout_seconds,
    )
    try:
        connection.request(
            "GET",
            target,
            headers={
                "Accept-Encoding": "identity",
                "Cookie": cookie_header,
                "User-Agent": safe_user_agent,
            },
        )
        response = connection.getresponse()
        if response.status in {301, 302, 303, 307, 308}:
            location = response.getheader("location", "") or ""
            destination = urlparse(urljoin(url, location))
            if (
                destination.netloc == "sso.cloud.pje.jus.br"
                or "login" in destination.path.casefold()
            ):
                raise CredenciaisAusentesError("a sessão expirou durante o download do documento")
            raise ServicoIndisponivelError(
                "o TJPE tentou redirecionar o download; nenhum redirecionamento foi seguido"
            )
        if response.status == 401:
            raise CredenciaisAusentesError("a sessão expirou durante o download do documento")
        if response.status != 200:
            raise ServicoIndisponivelError(
                f"o download autenticado respondeu HTTP {response.status}"
            )

        length_header = response.getheader("content-length")
        try:
            declared_length = int(length_header) if length_header is not None else None
        except ValueError:
            declared_length = None
        if declared_length is not None and declared_length > max_bytes:
            raise ValidacaoError(
                "o conteúdo excede o limite local desta operação; nenhum redirecionamento "
                "ou fluxo alternativo foi iniciado"
            )

        body = bytearray()
        while True:
            chunk = response.read(min(64 * 1024, max_bytes - len(body) + 1))
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > max_bytes:
                raise ValidacaoError(
                    "o conteúdo excede o limite local desta operação; nenhum "
                    "redirecionamento ou fluxo alternativo foi iniciado"
                )
        return bytes(body), response.getheader("content-type", "") or ""
    finally:
        connection.close()


def _validated_mime(content_type: str, body: bytes) -> str:
    if not body:
        raise ServicoIndisponivelError("o TJPE devolveu um documento vazio")
    lowered = body[:8_192].lower()
    if any(body.startswith(magic) for magic in _UNSAFE_MAGIC):
        raise ServicoIndisponivelError("o TJPE devolveu um formato executável não permitido")
    if any(marker in lowered for marker in _LOGIN_MARKERS):
        raise CredenciaisAusentesError("a sessão expirou durante o download do documento")

    mime = content_type.split(";", maxsplit=1)[0].strip().casefold()
    if body.startswith(b"%PDF-"):
        mime = "application/pdf"
    elif b"<html" in lowered or b"<!doctype html" in lowered:
        mime = "text/html"
    if mime == "application/octet-stream":
        raise ServicoIndisponivelError(
            "o TJPE não identificou o formato do documento de modo seguro"
        )
    if mime not in _MIME_EXTENSIONS:
        raise ServicoIndisponivelError(
            f"o tipo de documento {mime or 'desconhecido'} não é permitido nesta versão"
        )
    if mime == "application/pdf" and not body.startswith(b"%PDF-"):
        raise ServicoIndisponivelError("o conteúdo recebido não possui assinatura de PDF")
    expected_magic = _BINARY_MAGIC.get(mime)
    if expected_magic is not None and not body.startswith(expected_magic):
        raise ServicoIndisponivelError(
            "o conteúdo recebido não corresponde ao tipo de documento declarado"
        )
    if mime in {"video/mp4", "video/quicktime"} and not (len(body) >= 12 and body[4:8] == b"ftyp"):
        raise ServicoIndisponivelError(
            "o conteúdo recebido não corresponde ao tipo de documento declarado"
        )
    return mime


def _set_pdf_worker_limits() -> None:
    """Aplica limites adicionais quando o sistema oferece ``resource``."""
    try:
        import resource
    except ImportError:
        return
    try:
        resource.setrlimit(
            resource.RLIMIT_CPU,
            (_PDF_CPU_LIMIT_SECONDS, _PDF_CPU_LIMIT_SECONDS),
        )
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    except (AttributeError, OSError, ValueError):
        # O pai também monitora CPU, memória e relógio em todos os sistemas.
        pass
    try:
        resource.setrlimit(
            resource.RLIMIT_AS,
            (_PDF_MEMORY_LIMIT_BYTES, _PDF_MEMORY_LIMIT_BYTES),
        )
    except (AttributeError, OSError, ValueError):
        # O macOS reserva um espaço virtual enorme no processo Python e não permite
        # reduzir RLIMIT_AS abaixo dele. O pai impõe o mesmo teto pelo RSS nesse caso.
        pass


def _pdf_worker(
    body: bytes,
    max_pages: int,
    max_characters: int,
    sender: Connection,
) -> None:
    """Extrai PDF em processo descartável e devolve somente dados limitados."""
    try:
        _set_pdf_worker_limits()
        reader = PdfReader(BytesIO(body), strict=False)
        total_pages = len(reader.pages)
        pages_read = 0
        characters_truncated = False
        parts: list[str] = []
        character_count = 0

        for page in reader.pages[:max_pages]:
            page_text = (page.extract_text() or "").strip()
            pages_read += 1
            separator = "\n\n" if parts and page_text else ""
            remaining = max_characters - character_count
            fragment = separator + page_text
            if len(fragment) > remaining:
                if remaining:
                    parts.append(fragment[:remaining])
                character_count = max_characters
                characters_truncated = True
                break
            if fragment:
                parts.append(fragment)
                character_count += len(fragment)

        sender.send(
            (
                "ok",
                "".join(parts).strip(),
                pages_read,
                total_pages,
                characters_truncated,
            )
        )
    except BaseException:
        try:
            sender.send(("error",))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        sender.close()


def _stop_pdf_worker(process: _WorkerProcess) -> None:
    if not process.is_alive():
        process.join(timeout=0.2)
        return
    process.terminate()
    process.join(timeout=1.0)
    if process.is_alive():
        process.kill()
        process.join(timeout=1.0)


def _extract_pdf_sandboxed(
    body: bytes,
    *,
    max_pages: int,
    max_characters: int,
    wall_timeout_seconds: float = _PDF_WALL_TIMEOUT_SECONDS,
) -> tuple[str, int, int, bool]:
    """Limita globalmente a um subprocesso e aplica backpressure curto."""
    if max_pages < 1 or max_characters < 1 or wall_timeout_seconds <= 0:
        raise ValidacaoError("os limites de extração de PDF devem ser positivos")
    if not _PDF_PROCESS_SLOT.acquire(timeout=_PDF_SLOT_WAIT_SECONDS):
        raise ServicoIndisponivelError(
            "o extrator seguro de PDF está ocupado; tente novamente em instantes"
        )
    try:
        return _extract_pdf_in_worker(
            body,
            max_pages=max_pages,
            max_characters=max_characters,
            wall_timeout_seconds=wall_timeout_seconds,
        )
    finally:
        _PDF_PROCESS_SLOT.release()


def _extract_pdf_in_worker(
    body: bytes,
    *,
    max_pages: int,
    max_characters: int,
    wall_timeout_seconds: float,
) -> tuple[str, int, int, bool]:
    """Executa o parser em subprocesso com teto de recursos, tempo e saída."""

    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_pdf_worker,
        args=(body, max_pages, max_characters, sender),
        daemon=True,
    )
    started = False
    try:
        try:
            process.start()
            started = True
        except (OSError, RuntimeError):
            raise ServicoIndisponivelError(
                "não foi possível iniciar a extração segura do PDF"
            ) from None
        finally:
            sender.close()

        pid = process.pid
        if pid is None:
            raise ServicoIndisponivelError("não foi possível monitorar a extração segura do PDF")
        try:
            worker_monitor = psutil.Process(pid)
        except psutil.Error:
            raise ServicoIndisponivelError(
                "não foi possível monitorar a extração segura do PDF"
            ) from None

        deadline = time.monotonic() + wall_timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ServicoIndisponivelError(
                    "a extração do PDF excedeu o limite seguro de tempo; baixe o arquivo"
                )
            if receiver.poll(min(0.05, remaining)):
                break
            try:
                resident_bytes = worker_monitor.memory_info().rss
                cpu_times = worker_monitor.cpu_times()
            except psutil.NoSuchProcess:
                if receiver.poll(0):
                    break
                raise ServicoIndisponivelError(
                    "o PDF excedeu um limite seguro ou não pôde ser extraído; baixe o arquivo"
                ) from None
            except psutil.Error:
                raise ServicoIndisponivelError(
                    "não foi possível monitorar a extração segura do PDF"
                ) from None
            if resident_bytes > _PDF_MEMORY_LIMIT_BYTES:
                raise ServicoIndisponivelError(
                    "a extração do PDF excedeu o limite seguro de memória; baixe o arquivo"
                )
            if cpu_times.user + cpu_times.system > _PDF_CPU_LIMIT_SECONDS:
                raise ServicoIndisponivelError(
                    "a extração do PDF excedeu o limite seguro de CPU; baixe o arquivo"
                )
        try:
            result = receiver.recv()
        except (EOFError, OSError):
            raise ServicoIndisponivelError(
                "o PDF excedeu um limite seguro ou não pôde ser extraído; baixe o arquivo"
            ) from None
    finally:
        receiver.close()
        if started:
            _stop_pdf_worker(process)

    raw_result: object = result
    if not isinstance(raw_result, tuple):
        raise ServicoIndisponivelError(
            "o PDF excedeu um limite seguro ou não pôde ser extraído; baixe o arquivo"
        )
    candidate = cast(tuple[object, ...], raw_result)
    if len(candidate) != 5:
        raise ServicoIndisponivelError(
            "o PDF excedeu um limite seguro ou não pôde ser extraído; baixe o arquivo"
        )
    result = candidate
    if (
        result[0] != "ok"
        or not isinstance(result[1], str)
        or not isinstance(result[2], int)
        or not isinstance(result[3], int)
        or not isinstance(result[4], bool)
        or len(result[1]) > max_characters
        or not 0 <= result[2] <= max_pages
        or result[3] < result[2]
    ):
        raise ServicoIndisponivelError(
            "o PDF excedeu um limite seguro ou não pôde ser extraído; baixe o arquivo"
        )
    return result[1], result[2], result[3], result[4]


def _extract_document_text(
    body: bytes,
    mime_type: str,
    *,
    max_pages: int,
) -> tuple[str, int | None, int | None]:
    try:
        if mime_type == "application/pdf":
            reader = PdfReader(BytesIO(body), strict=False)
            total_pages = len(reader.pages)
            pages = reader.pages[:max_pages]
            text = "\n\n".join((page.extract_text() or "").strip() for page in pages)
            return text.strip(), len(pages), total_pages
        if mime_type in {"text/html", "application/xhtml+xml"}:
            parser = _HtmlTextExtractor()
            parser.feed(body.decode("utf-8", errors="replace"))
            return "\n".join(parser.parts), None, None
        if mime_type == "text/plain":
            return body.decode("utf-8", errors="replace"), None, None
    except Exception:
        raise ServicoIndisponivelError(
            "o documento foi baixado, mas seu texto não pôde ser extraído"
        ) from None
    raise ValidacaoError(
        "este formato pode ser baixado, mas não possui extração textual nesta versão"
    )


def _safe_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", normalized).strip("-._")[:80]
    return slug or "documento"


def _secure_subdirectory(root: Path, *parts: str) -> Path:
    """Cria componentes privados sem atravessar links simbólicos internos."""
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not root.is_dir():
        raise ServicoIndisponivelError("o destino configurado para downloads não é um diretório")

    current = root
    for part in parts:
        candidate = current / part
        if candidate.is_symlink():
            raise ServicoIndisponivelError(
                "o destino do download contém um link simbólico inseguro"
            )
        try:
            candidate.mkdir(mode=0o700)
        except FileExistsError:
            if candidate.is_symlink() or not candidate.is_dir():
                raise ServicoIndisponivelError(
                    "o destino do download contém um componente inseguro"
                ) from None
        candidate.chmod(0o700)
        current = candidate
    return current


def _atomic_publish(target: Path, data: bytes) -> None:
    if target.parent.is_symlink():
        raise ServicoIndisponivelError(
            "o diretório de destino do download é um link simbólico inseguro"
        )
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.is_symlink():
        raise ServicoIndisponivelError("o destino do download é um link simbólico inseguro")
    if target.exists():
        if (
            target.is_file()
            and hashlib.sha256(target.read_bytes()).digest() == hashlib.sha256(data).digest()
        ):
            target.chmod(0o600)
            return
        raise ServicoIndisponivelError(
            "já existe um arquivo diferente no destino calculado para o download"
        )

    descriptor, temporary_name = tempfile.mkstemp(prefix=".pje-part-", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            if (
                target.is_symlink()
                or not target.is_file()
                or hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(data).digest()
            ):
                raise ServicoIndisponivelError(
                    "o destino do download foi ocupado por outro arquivo"
                ) from None
    finally:
        temporary.unlink(missing_ok=True)


async def browser_cookie_header(context: BrowserContext, url: str) -> str:
    """Serializa somente cookies válidos para um URL HTTPS auditado."""
    return await _browser_cookie_header(context, url)


def secure_subdirectory(root: Path, *parts: str) -> Path:
    """Cria uma subárvore privada sem seguir links simbólicos."""
    return _secure_subdirectory(root, *parts)


def atomic_publish(target: Path, data: bytes) -> None:
    """Publica bytes de forma atômica em um destino privado."""
    _atomic_publish(target, data)
