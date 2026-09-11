from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import cast

import pytest
from playwright.async_api import BrowserContext, Dialog, Page

from mcp_pje_tjpe import pje_auth
from mcp_pje_tjpe.browser import BrowserManager
from mcp_pje_tjpe.config import Settings
from mcp_pje_tjpe.credentials import CredentialStore
from mcp_pje_tjpe.errors import ServicoIndisponivelError
from mcp_pje_tjpe.models import EstadoLogin, Grau
from mcp_pje_tjpe.pje_auth import PjeSessionManager


class FakeDialog:
    def __init__(self, message: str) -> None:
        self.message = message
        self.dismissed = False
        self.type = "alert"

    async def dismiss(self) -> None:
        self.dismissed = True


class FakeCertificateLocator:
    def __init__(self, page: FakePage) -> None:
        self.page = page

    async def wait_for(self, *, state: str, **options: float) -> None:
        assert state == "visible"
        assert options == {"timeout": self.page.expected_timeout}

    async def click(self) -> None:
        self.page.certificate_clicks += 1
        if self.page.after_click_url is not None:
            self.page.url = self.page.after_click_url
        if self.page.dialog is not None:
            assert self.page.dialog_handler is not None
            await self.page.dialog_handler(cast(Dialog, self.page.dialog))
        if (
            self.page.pjeoffice_delay_ticks == 0
            and self.page.pjeoffice_url is not None
            and self.page.response_handler is not None
        ):
            self.page.responder_pjeoffice()


class FakeRespostaPjeOffice:
    """Só o campo que o filtro de porta do PJeOffice inspeciona."""

    def __init__(self, url: str) -> None:
        self.url = url


class FakePage:
    def __init__(
        self,
        after_click_url: str | None,
        *,
        dialog_message: str | None = None,
        expected_timeout: int = 100,
        probe_url: str | None = None,
        probe_status: int = 200,
        probe_body: str = "<html><title>Painel do advogado</title></html>",
        probe_unavailable: bool = False,
        pjeoffice_url: str | None = "http://localhost:8800/pjeOffice/requisicao/?r=%7B%7D",
        pjeoffice_delay_ticks: int = 0,
    ) -> None:
        self.url = "about:blank"
        self.after_click_url = after_click_url
        self.expected_timeout = expected_timeout
        self.dialog = FakeDialog(dialog_message) if dialog_message else None
        self.dialog_handler: Callable[[Dialog], Awaitable[None]] | None = None
        # Um alerta do SSO significa que a ida ao PJeOffice não voltou.
        self.pjeoffice_url = None if dialog_message else pjeoffice_url
        self.response_handler: Callable[[object], None] | None = None
        self.pjeoffice_responses = 0
        self.pjeoffice_delay_ticks = pjeoffice_delay_ticks
        self.certificate_clicks = 0
        self.goto_calls = 0
        self.wait_calls: list[float] = []
        self.closed = False
        self.locator_instance = FakeCertificateLocator(self)
        self.probe_url = probe_url or after_click_url or "about:blank"
        self.probe_status = probe_status
        self.probe_body = probe_body
        self.probe_unavailable = probe_unavailable

    def set_default_timeout(self, timeout: float) -> None:
        assert timeout == self.expected_timeout

    def set_default_navigation_timeout(self, timeout: float) -> None:
        assert timeout == self.expected_timeout

    def on(
        self,
        event: str,
        handler: Callable[[Dialog], Awaitable[None]],
    ) -> None:
        if event == "response":
            self.response_handler = cast(Callable[[object], None], handler)
            return
        assert event == "dialog"
        self.dialog_handler = handler

    def remove_listener(
        self,
        event: str,
        handler: Callable[[Dialog], Awaitable[None]],
    ) -> None:
        if event == "response":
            assert handler is self.response_handler
            self.response_handler = None
            return
        assert event == "dialog"
        assert handler is self.dialog_handler
        self.dialog_handler = None

    async def goto(self, url: str, *, wait_until: str) -> None:
        assert wait_until == "domcontentloaded"
        self.goto_calls += 1
        self.url = url

    def responder_pjeoffice(self) -> None:
        assert self.pjeoffice_url is not None
        assert self.response_handler is not None
        self.pjeoffice_responses += 1
        self.response_handler(FakeRespostaPjeOffice(self.pjeoffice_url))

    async def wait_for_timeout(self, delay: float) -> None:
        self.wait_calls.append(delay)
        if self.pjeoffice_delay_ticks > 0:
            self.pjeoffice_delay_ticks -= 1
            if (
                self.pjeoffice_delay_ticks == 0
                and self.pjeoffice_url is not None
                and self.response_handler is not None
            ):
                self.responder_pjeoffice()

    def locator(self, selector: str) -> FakeCertificateLocator:
        assert selector == "#kc-pje-office"
        return self.locator_instance

    def is_closed(self) -> bool:
        return self.closed


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.closed = False
        self.request = FakeRequest(page)

    async def new_page(self) -> Page:
        return cast(Page, self.page)

    async def close(self) -> None:
        self.closed = True
        self.page.closed = True


class FakeBrowser:
    def __init__(self, page: FakePage | list[FakePage]) -> None:
        pages = page if isinstance(page, list) else [page]
        self.contexts = [FakeContext(item) for item in pages]
        self.context = self.contexts[0]
        self.context_calls = 0

    async def new_context(self) -> BrowserContext:
        context = self.contexts[self.context_calls]
        self.context_calls += 1
        return cast(BrowserContext, context)


class UnusedCredentials:
    def load(self) -> None:
        raise AssertionError("o fluxo por certificado não pode ler credenciais")


def make_manager(page: FakePage) -> tuple[PjeSessionManager, FakeBrowser]:
    browser = FakeBrowser(page)
    manager = PjeSessionManager(
        cast(BrowserManager, browser),
        Settings(headless=False, timeout_ms=page.expected_timeout),
        cast(CredentialStore, UnusedCredentials()),
    )
    return manager, browser


class FakeResponse:
    def __init__(self, page: FakePage) -> None:
        self.url = page.probe_url
        self.status = page.probe_status
        self._body = page.probe_body
        self.disposed = False

    async def text(self) -> str:
        return self._body

    async def body(self) -> bytes:
        return self._body.encode("utf-8")

    async def dispose(self) -> None:
        self.disposed = True


class FakeRequest:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.calls: list[str] = []
        self.responses: list[FakeResponse] = []

    async def get(
        self,
        url: str,
        *,
        fail_on_status_code: bool,
        timeout: float,  # noqa: ASYNC109 - espelha a assinatura do Playwright
    ) -> FakeResponse:
        assert fail_on_status_code is False
        assert timeout == self.page.expected_timeout
        assert url.endswith("/Painel/painel_usuario/advogado.seam")
        self.calls.append(url)
        if self.page.probe_unavailable:
            raise RuntimeError("falha transitória simulada")
        response = FakeResponse(self.page)
        self.responses.append(response)
        return response


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_certificate_login_success_reuses_session_without_another_click() -> None:
    page = FakePage(
        "https://pje.cloud.tjpe.jus.br/1g/Painel/painel_usuario/advogado.seam"
        "?state=segredo#fragmento"
    )
    manager, browser = make_manager(page)

    opened = await manager.abrir_login_certificado(Grau.PRIMEIRO)
    checked = await manager.verificar_login_certificado(Grau.PRIMEIRO)
    reopened = await manager.abrir_login_certificado(Grau.PRIMEIRO)

    assert opened.autenticado is True
    assert checked.autenticado is True
    assert reopened.autenticado is True
    assert opened.modo_autenticacao == "certificado_digital"
    assert opened.estado is EstadoLogin.AUTENTICADO
    assert opened.url_atual == "https://pje.cloud.tjpe.jus.br/1g/"
    assert page.certificate_clicks == 1
    assert page.goto_calls == 1
    # O PJeOffice respondeu ainda no clique: não há razão para dormir depois disso.
    assert page.wait_calls == []
    assert page.pjeoffice_responses == 1
    assert browser.context_calls == 1
    assert len(browser.context.request.calls) == 3
    assert all(response.disposed for response in browser.context.request.responses)


@pytest.mark.anyio
async def test_certificate_login_waits_for_human_and_hides_oauth_query() -> None:
    page = FakePage(
        "https://sso.cloud.pje.jus.br/auth/realms/pje/login-actions/authenticate"
        "?session_code=segredo&state=segredo#fragmento"
    )
    manager, _ = make_manager(page)

    opened = await manager.abrir_login_certificado(Grau.PRIMEIRO)
    checked = await manager.verificar_login_certificado(Grau.PRIMEIRO)

    assert opened.autenticado is False
    assert "aguardando" in opened.mensagem
    assert "PIN somente no PJeOffice" in opened.mensagem
    assert opened.estado is EstadoLogin.AGUARDANDO_INTERACAO
    assert opened.url_atual == "https://sso.cloud.pje.jus.br/"
    assert checked == opened
    assert page.certificate_clicks == 1


@pytest.mark.anyio
async def test_certificate_login_rejects_return_to_wrong_domain() -> None:
    page = FakePage("https://pje.cloud.tjpe.jus.br.evil.example/1g/Painel?state=segredo")
    manager, _ = make_manager(page)

    status = await manager.abrir_login_certificado(Grau.PRIMEIRO)

    assert status.autenticado is False
    assert "não retornou ao domínio e grau esperados" in status.mensagem
    assert status.estado is EstadoLogin.ERRO
    assert status.url_atual == ""


@pytest.mark.anyio
async def test_certificate_login_dismisses_pjeoffice_error_dialog() -> None:
    page = FakePage(None, dialog_message="PJeOffice não encontrado ou indisponível")
    manager, browser = make_manager(page)

    with pytest.raises(
        ServicoIndisponivelError,
        match=r"PJeOffice não iniciou.*não respondeu à solicitação",
    ):
        await manager.abrir_login_certificado(Grau.PRIMEIRO)

    assert page.dialog is not None
    assert page.dialog.dismissed is True
    assert page.certificate_clicks == 1
    assert browser.context.closed is True


@pytest.mark.anyio
async def test_certificate_login_reports_what_the_sso_dialog_said() -> None:
    """Sem o texto do alerta, toda falha de certificado vira a mesma frase."""
    page = FakePage(None, dialog_message="  Erro ao\n comunicar com   o PJeOffice  ")
    manager, _ = make_manager(page)

    with pytest.raises(ServicoIndisponivelError) as excecao:
        await manager.abrir_login_certificado(Grau.PRIMEIRO)

    assert 'o SSO informou: "Erro ao comunicar com o PJeOffice"' in str(excecao.value)


@pytest.mark.anyio
async def test_certificate_login_truncates_a_long_sso_dialog() -> None:
    page = FakePage(None, dialog_message="PJeOffice " + "x" * 5000)
    manager, _ = make_manager(page)

    with pytest.raises(ServicoIndisponivelError) as excecao:
        await manager.abrir_login_certificado(Grau.PRIMEIRO)

    assert len(str(excecao.value)) < 500


@pytest.mark.anyio
async def test_certificate_login_rejects_url_only_false_positive_and_disposes_probe() -> None:
    page = FakePage(
        "https://pje.cloud.tjpe.jus.br/1g/Painel/painel_usuario/advogado.seam",
        probe_url=(
            "https://sso.cloud.pje.jus.br/auth/realms/pje/protocol/openid-connect/auth"
            "?session_code=segredo&state=segredo"
        ),
        probe_body='<button id="kc-pje-office">Certificado digital</button>',
    )
    manager, browser = make_manager(page)

    status = await manager.abrir_login_certificado(Grau.PRIMEIRO)

    assert status.autenticado is False
    assert status.estado is EstadoLogin.EXPIRADO
    assert "session_code" not in status.model_dump_json()
    assert status.url_atual == "https://pje.cloud.tjpe.jus.br/1g/"
    assert browser.context.closed is True
    assert browser.context.request.responses[0].disposed is True


@pytest.mark.anyio
async def test_certificate_login_can_explicitly_restart_a_pending_attempt() -> None:
    pending_url = (
        "https://sso.cloud.pje.jus.br/auth/realms/pje/login-actions/authenticate?state=nao-retornar"
    )
    first_page = FakePage(pending_url)
    second_page = FakePage(pending_url)
    browser = FakeBrowser([first_page, second_page])
    manager = PjeSessionManager(
        cast(BrowserManager, browser),
        Settings(headless=False, timeout_ms=100),
        cast(CredentialStore, UnusedCredentials()),
    )

    first = await manager.abrir_login_certificado(Grau.PRIMEIRO)
    restarted = await manager.abrir_login_certificado(Grau.PRIMEIRO, reiniciar=True)

    assert first.estado is EstadoLogin.AGUARDANDO_INTERACAO
    assert restarted.estado is EstadoLogin.AGUARDANDO_INTERACAO
    assert first_page.certificate_clicks == 1
    assert second_page.certificate_clicks == 1
    assert browser.contexts[0].closed is True
    assert browser.context_calls == 2


@pytest.mark.anyio
async def test_certificate_login_rejects_two_pending_degrees() -> None:
    page = FakePage("https://sso.cloud.pje.jus.br/auth/realms/pje/login-actions/authenticate")
    manager, browser = make_manager(page)
    await manager.abrir_login_certificado(Grau.PRIMEIRO)

    with pytest.raises(
        ServicoIndisponivelError,
        match=r"já existe.*no 1g",
    ):
        await manager.abrir_login_certificado(Grau.SEGUNDO)

    assert browser.context_calls == 1
    assert page.certificate_clicks == 1


@pytest.mark.anyio
async def test_read_session_rejects_missing_authenticated_session() -> None:
    page = FakePage(None)
    manager, browser = make_manager(page)

    with pytest.raises(ServicoIndisponivelError, match="não há sessão autenticada"):
        async with manager.read_session(Grau.PRIMEIRO):
            pytest.fail("uma sessão ausente não pode produzir lease")

    assert browser.context_calls == 0


@pytest.mark.anyio
async def test_read_session_yields_verified_opaque_lease() -> None:
    authenticated_url = "https://pje.cloud.tjpe.jus.br/1g/Painel/painel_usuario/advogado.seam"
    page = FakePage(authenticated_url)
    manager, browser = make_manager(page)
    await manager.abrir_login_certificado(Grau.PRIMEIRO)

    async with manager.read_session(Grau.PRIMEIRO) as lease:
        assert lease.grau is Grau.PRIMEIRO
        assert lease.context is cast(BrowserContext, browser.context)
        assert lease.page is cast(Page, page)
        assert lease.generation
        assert authenticated_url not in repr(lease)
        generation = lease.generation

    assert generation
    assert browser.context.closed is False
    assert len(browser.context.request.calls) == 2


@pytest.mark.anyio
async def test_read_session_closes_expired_session_after_false_probe() -> None:
    authenticated_url = "https://pje.cloud.tjpe.jus.br/1g/Painel/painel_usuario/advogado.seam"
    page = FakePage(authenticated_url)
    manager, browser = make_manager(page)
    await manager.abrir_login_certificado(Grau.PRIMEIRO)
    page.probe_url = (
        "https://sso.cloud.pje.jus.br/auth/realms/pje/protocol/openid-connect/auth?state=segredo"
    )

    with pytest.raises(ServicoIndisponivelError, match=r"sessão autenticada.*expirou"):
        async with manager.read_session(Grau.PRIMEIRO):
            pytest.fail("um probe falso não pode produzir lease")

    assert browser.context.closed is True


@pytest.mark.anyio
async def test_read_session_generation_changes_with_new_login_context() -> None:
    authenticated_url = "https://pje.cloud.tjpe.jus.br/1g/Painel/painel_usuario/advogado.seam"
    first_page = FakePage(authenticated_url)
    second_page = FakePage(authenticated_url)
    browser = FakeBrowser([first_page, second_page])
    manager = PjeSessionManager(
        cast(BrowserManager, browser),
        Settings(headless=False, timeout_ms=100),
        cast(CredentialStore, UnusedCredentials()),
    )

    await manager.abrir_login_certificado(Grau.PRIMEIRO)
    async with manager.read_session(Grau.PRIMEIRO) as first_lease:
        first_generation = first_lease.generation

    await manager.abrir_login_certificado(Grau.PRIMEIRO, reiniciar=True)
    async with manager.read_session(Grau.PRIMEIRO) as second_lease:
        second_generation = second_lease.generation

    assert first_generation != second_generation
    assert browser.contexts[0].closed is True
    assert browser.contexts[1].closed is False


@pytest.mark.anyio
async def test_read_session_preserves_context_on_transient_probe_failure() -> None:
    authenticated_url = "https://pje.cloud.tjpe.jus.br/1g/Painel/painel_usuario/advogado.seam"
    page = FakePage(authenticated_url)
    manager, browser = make_manager(page)
    await manager.abrir_login_certificado(Grau.PRIMEIRO)
    async with manager.read_session(Grau.PRIMEIRO) as initial_lease:
        generation = initial_lease.generation
    page.probe_unavailable = True

    with pytest.raises(ServicoIndisponivelError, match="confirmar a sessão protegida"):
        async with manager.read_session(Grau.PRIMEIRO):
            pytest.fail("falha transitória não pode produzir lease")

    assert browser.context.closed is False
    page.probe_unavailable = False
    page.probe_status = 503
    with pytest.raises(ServicoIndisponivelError, match="confirmar a sessão protegida"):
        async with manager.read_session(Grau.PRIMEIRO):
            pytest.fail("HTTP transitório não pode produzir lease")

    assert browser.context.closed is False
    assert browser.context.request.responses[-1].disposed is True
    page.probe_status = 200
    page.probe_url = "https://pje.cloud.tjpe.jus.br.evil.example/1g/Painel"
    with pytest.raises(ServicoIndisponivelError, match="confirmar a sessão protegida"):
        async with manager.read_session(Grau.PRIMEIRO):
            pytest.fail("destino inesperado não pode produzir lease")

    assert browser.context.closed is False
    page.probe_url = authenticated_url
    async with manager.read_session(Grau.PRIMEIRO) as lease:
        assert lease.generation == generation


@pytest.mark.anyio
async def test_certificate_login_waits_for_the_slow_pjeoffice_round_trip() -> None:
    """No TJPE a volta levou 22s; devolver antes disso é o que parece travamento."""
    page = FakePage(
        "https://pje.cloud.tjpe.jus.br/1g/Painel/painel_usuario/advogado.seam",
        pjeoffice_delay_ticks=5,
    )
    manager, _ = make_manager(page)

    status = await manager.abrir_login_certificado(Grau.PRIMEIRO)

    assert page.pjeoffice_responses == 1
    assert len(page.wait_calls) == 5
    assert status.autenticado is True


@pytest.mark.anyio
async def test_certificate_login_gives_up_waiting_instead_of_hanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Esperar o PJeOffice não pode virar bloqueio indefinido da ferramenta."""
    monkeypatch.setattr(pje_auth, "_ESPERA_PJEOFFICE_SEGUNDOS", 0.01)
    page = FakePage(None, pjeoffice_url=None)
    manager, _ = make_manager(page)

    status = await manager.abrir_login_certificado(Grau.PRIMEIRO)

    assert page.pjeoffice_responses == 0
    assert status.autenticado is False
