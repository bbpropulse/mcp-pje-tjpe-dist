from __future__ import annotations

import asyncio
import re
import secrets
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from urllib.parse import urlparse

import pyotp
from playwright.async_api import BrowserContext, Dialog, Page, Response
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import SecretStr

from mcp_pje_tjpe.browser import BrowserManager
from mcp_pje_tjpe.config import Settings
from mcp_pje_tjpe.credentials import CredentialStore
from mcp_pje_tjpe.errors import (
    CredenciaisAusentesError,
    PjeTjpeError,
    ServicoIndisponivelError,
)
from mcp_pje_tjpe.models import EstadoLogin, Grau, StatusLogin
from mcp_pje_tjpe.pje_office import PJE_OFFICE_PORT

# O alerta do SSO é texto de UI, não conteúdo processual; corta-se só para não
# despejar uma página inteira dentro de uma mensagem de erro.
_LIMITE_TEXTO_DIALOGO = 300

# Medido no TJPE em 2026-09-08: entre o clique e a resposta do PJeOffice passaram
# 22s. Retornar antes disso deixa a janela parada num ponto que o advogado lê como
# travamento — e ele a fecha, matando o contexto. Espera-se a ida e volta.
_ESPERA_PJEOFFICE_SEGUNDOS = 60.0
_INTERVALO_ESPERA_PJEOFFICE_MS = 500


def _e_resposta_do_pjeoffice(url: str) -> bool:
    """Reconhece a ida e volta ao PJeOffice sem ler nada do que trafega nela."""
    alvo = urlparse(url)
    return alvo.hostname in {"127.0.0.1", "localhost", "::1"} and alvo.port == PJE_OFFICE_PORT


@dataclass(frozen=True, slots=True)
class ReadSessionLease:
    """Referência interna e efêmera a uma sessão autenticada de leitura."""

    grau: Grau
    context: BrowserContext = field(repr=False)
    page: Page = field(repr=False)
    generation: str = field(repr=False)


class PjeSessionManager:
    """Sessões autenticadas em memória; nunca persiste cookies em disco."""

    def __init__(
        self,
        browser: BrowserManager,
        config: Settings,
        credentials: CredentialStore,
    ) -> None:
        self.browser = browser
        self.config = config
        self.credentials = credentials
        self._contexts: dict[Grau, BrowserContext] = {}
        self._pages: dict[Grau, Page] = {}
        self._session_generations: dict[Grau, str] = {}
        self._authentication_modes: dict[Grau, str] = {}
        self._certificate_dialog_errors: dict[Grau, str] = {}
        self._certificate_dialog_handlers: dict[Grau, Callable[[Dialog], Awaitable[None]]] = {}
        self._authentication_flow_lock = asyncio.Lock()
        self._active_certificate_degree: Grau | None = None
        self._locks = {Grau.PRIMEIRO: asyncio.Lock(), Grau.SEGUNDO: asyncio.Lock()}

    async def testar_login(self, grau: Grau) -> StatusLogin:
        async with self._authentication_flow_lock, self._locks[grau]:
            existing = self._pages.get(grau)
            if existing and not existing.is_closed() and self._is_authenticated(existing.url, grau):
                if await self._probe_authenticated_session(grau):
                    return StatusLogin(
                        grau=grau,
                        autenticado=True,
                        estado=EstadoLogin.AUTENTICADO,
                        modo_autenticacao=self._authentication_modes.get(grau, "cpf_senha_mfa"),
                        url_atual=self._canonical_pje_url(grau),
                        mensagem="sessão autenticada já estava ativa",
                    )

            await self._close_degree(grau)
            credentials = self.credentials.load()
            context = await self.browser.new_context()
            page = await context.new_page()
            page.set_default_timeout(self.config.timeout_ms)
            page.set_default_navigation_timeout(self.config.timeout_ms)
            self._contexts[grau] = context
            self._pages[grau] = page
            self._session_generations[grau] = secrets.token_urlsafe(32)
            self._authentication_modes[grau] = "cpf_senha_mfa"

            try:
                base = self.config.urls.pje_base(grau.value)
                await page.goto(f"{base}/login.seam", wait_until="domcontentloaded")
                await page.locator("#username").fill(credentials.cpf)
                await page.locator("#password").fill(credentials.password.get_secret_value())
                await page.locator("#kc-login").click()
                await page.wait_for_timeout(500)

                await self._handle_totp(page, credentials.totp_seed)

                try:
                    await page.wait_for_url(
                        re.compile(rf"^https://pje\.cloud\.tjpe\.jus\.br/{grau.value}/"),
                        wait_until="domcontentloaded",
                        timeout=self.config.timeout_ms,
                    )
                except PlaywrightTimeoutError:
                    message = await self._login_error(page)
                    raise ServicoIndisponivelError(
                        message or "o SSO não confirmou a autenticação no PJe/TJPE"
                    ) from None

                authenticated = self._is_authenticated(page.url, grau)
                if authenticated:
                    authenticated = await self._probe_authenticated_session(grau)
                if not authenticated:
                    status = StatusLogin(
                        grau=grau,
                        autenticado=False,
                        estado=EstadoLogin.EXPIRADO,
                        modo_autenticacao="cpf_senha_mfa",
                        url_atual=self._canonical_pje_url(grau),
                        mensagem=(
                            "o SSO retornou ao PJe, mas uma requisição protegida não "
                            "confirmou a sessão"
                        ),
                    )
                    await self._close_degree(grau)
                    return status
                return StatusLogin(
                    grau=grau,
                    autenticado=True,
                    estado=EstadoLogin.AUTENTICADO,
                    modo_autenticacao="cpf_senha_mfa",
                    url_atual=self._canonical_pje_url(grau),
                    mensagem="login confirmado; sessão mantida somente em memória",
                )
            except PjeTjpeError:
                await self._close_degree(grau)
                raise
            except Exception:
                await self._close_degree(grau)
                raise ServicoIndisponivelError(
                    "não foi possível concluir o login no SSO do PJe/TJPE"
                ) from None

    async def abrir_login_certificado(self, grau: Grau, *, reiniciar: bool = False) -> StatusLogin:
        """Abre uma tentativa assistida sem acessar certificado ou PIN."""
        async with self._authentication_flow_lock:
            async with self._locks[grau]:
                if self.config.headless:
                    raise ServicoIndisponivelError(
                        "o login por certificado exige navegador visível; configure "
                        "PJE_TJPE_AUTH_HEADLESS=false antes de iniciar o MCP"
                    )

                active_page = (
                    self._pages.get(self._active_certificate_degree)
                    if self._active_certificate_degree is not None
                    else None
                )
                if self._active_certificate_degree is not None and (
                    active_page is None or active_page.is_closed()
                ):
                    await self._close_degree(self._active_certificate_degree)

                if (
                    self._active_certificate_degree is not None
                    and self._active_certificate_degree != grau
                ):
                    raise ServicoIndisponivelError(
                        "já existe uma autenticação por certificado aguardando interação "
                        f"no {self._active_certificate_degree.value}; conclua ou reinicie "
                        "essa tentativa antes de abrir outro grau"
                    )

                existing = self._pages.get(grau)
                if reiniciar:
                    await self._close_degree(grau)
                    existing = None
                if (
                    self._authentication_modes.get(grau) == "certificado_digital"
                    and existing
                    and not existing.is_closed()
                ):
                    return await self._verified_certificate_status(grau)

                await self._close_degree(grau)
                context = await self.browser.new_context()
                page = await context.new_page()
                page.set_default_timeout(self.config.timeout_ms)
                page.set_default_navigation_timeout(self.config.timeout_ms)
                self._contexts[grau] = context
                self._pages[grau] = page
                self._session_generations[grau] = secrets.token_urlsafe(32)
                self._authentication_modes[grau] = "certificado_digital"
                self._active_certificate_degree = grau

                async def handle_pjeoffice_dialog(dialog: Dialog) -> None:
                    original = " ".join(dialog.message.split())
                    message = original.casefold()
                    dialog_type = getattr(dialog, "type", "alert")
                    if dialog_type == "alert" and "pjeoffice" in message.replace(" ", ""):
                        detail = "o aplicativo PJeOffice não respondeu à solicitação do SSO"
                    else:
                        detail = "o SSO apresentou um diálogo inesperado durante a autenticação"
                    # O alerta do SSO diz qual etapa falhou; sem repassá-lo, todo problema
                    # de certificado vira a mesma frase e o diagnóstico morre aqui.
                    if original:
                        detail += f' (o SSO informou: "{original[:_LIMITE_TEXTO_DIALOGO]}")'
                    self._certificate_dialog_errors[grau] = detail
                    await dialog.dismiss()

                self._certificate_dialog_handlers[grau] = handle_pjeoffice_dialog
                page.on("dialog", handle_pjeoffice_dialog)

                try:
                    base = self.config.urls.pje_base(grau.value)
                    await page.goto(f"{base}/login.seam", wait_until="domcontentloaded")
                    certificate_button = page.locator("#kc-pje-office")
                    try:
                        await certificate_button.wait_for(
                            state="visible", timeout=self.config.timeout_ms
                        )
                    except PlaywrightTimeoutError:
                        message = await self._login_error(page)
                        raise ServicoIndisponivelError(
                            message or "o SSO do PJe não apresentou o botão de certificado digital"
                        ) from None

                    # O clique real delega certificado e PIN ao PJeOffice. O MCP não
                    # inspeciona nem reproduz o handler/challenge do botão.
                    respondeu = asyncio.Event()

                    def registrar_resposta(resposta: Response) -> None:
                        if _e_resposta_do_pjeoffice(resposta.url):
                            respondeu.set()

                    page.on("response", registrar_resposta)
                    try:
                        await certificate_button.click()
                        await self._aguardar_pjeoffice(grau, page, respondeu)
                    finally:
                        page.remove_listener("response", registrar_resposta)
                    return await self._verified_certificate_status(grau)
                except PjeTjpeError:
                    await self._close_degree(grau)
                    raise
                except Exception:
                    await self._close_degree(grau)
                    raise ServicoIndisponivelError(
                        "não foi possível iniciar o login por certificado no SSO do PJe/TJPE"
                    ) from None

    async def _aguardar_pjeoffice(
        self, grau: Grau, page: Page, respondeu: asyncio.Event
    ) -> None:
        """Aguarda a ida e volta ao PJeOffice; segue adiante se ela não vier."""
        limite = time.monotonic() + _ESPERA_PJEOFFICE_SEGUNDOS
        while time.monotonic() < limite:
            if respondeu.is_set() or grau in self._certificate_dialog_errors:
                return
            if page.is_closed():
                return
            await page.wait_for_timeout(_INTERVALO_ESPERA_PJEOFFICE_MS)

    async def verificar_login_certificado(self, grau: Grau) -> StatusLogin:
        """Consulta a tentativa existente, sem novo clique ou nova navegação."""
        async with self._authentication_flow_lock:
            async with self._locks[grau]:
                return await self._verified_certificate_status(grau)

    @asynccontextmanager
    async def read_session(self, grau: Grau) -> AsyncGenerator[ReadSessionLease]:
        """Entrega uma sessão protegida enquanto mantém os locks de autenticação."""
        async with self._authentication_flow_lock, self._locks[grau]:
            context = self._contexts.get(grau)
            page = self._pages.get(grau)
            generation = self._session_generations.get(grau)
            if context is None or page is None or generation is None:
                if context is not None or page is not None or generation is not None:
                    await self._close_degree(grau)
                raise ServicoIndisponivelError(
                    "não há sessão autenticada ativa para leitura no PJe/TJPE"
                )

            if page.is_closed() or not self._is_authenticated(page.url, grau):
                await self._close_degree(grau)
                raise ServicoIndisponivelError(
                    "a sessão autenticada do PJe/TJPE expirou; conclua um novo login"
                )

            # O probe converte falhas de rede/serviço em erro esperado. Nessa
            # situação o contexto permanece em memória para uma nova tentativa.
            authenticated = await self._probe_authenticated_session(grau)
            if not authenticated:
                await self._close_degree(grau)
                raise ServicoIndisponivelError(
                    "a sessão autenticada do PJe/TJPE expirou; conclua um novo login"
                )

            yield ReadSessionLease(
                grau=grau,
                context=context,
                page=page,
                generation=generation,
            )

    @asynccontextmanager
    async def cached_read_session(self, grau: Grau) -> AsyncGenerator[ReadSessionLease]:
        """Entrega a sessão local sem probe para ler um snapshot com rede bloqueada."""
        async with self._authentication_flow_lock, self._locks[grau]:
            context = self._contexts.get(grau)
            page = self._pages.get(grau)
            generation = self._session_generations.get(grau)
            if context is None or page is None or generation is None:
                if context is not None or page is not None or generation is not None:
                    await self._close_degree(grau)
                raise ServicoIndisponivelError(
                    "não há sessão local ativa correspondente ao cache dos Autos"
                )
            if page.is_closed() or not self._is_authenticated(page.url, grau):
                await self._close_degree(grau)
                raise ServicoIndisponivelError(
                    "a sessão local ligada ao cache dos Autos foi encerrada"
                )
            yield ReadSessionLease(
                grau=grau,
                context=context,
                page=page,
                generation=generation,
            )

    async def _verified_certificate_status(self, grau: Grau) -> StatusLogin:
        status = self._status_login_certificado(grau)
        if not status.autenticado:
            return status
        if await self._probe_authenticated_session(grau):
            if self._active_certificate_degree == grau:
                self._active_certificate_degree = None
            self._detach_certificate_dialog(grau)
            return status

        expired = StatusLogin(
            grau=grau,
            autenticado=False,
            estado=EstadoLogin.EXPIRADO,
            modo_autenticacao="certificado_digital",
            url_atual=self._canonical_pje_url(grau),
            mensagem=(
                "o navegador retornou ao PJe, mas uma requisição protegida não "
                "confirmou a sessão; inicie novamente o login"
            ),
        )
        await self._close_degree(grau)
        return expired

    def _status_login_certificado(self, grau: Grau) -> StatusLogin:
        error = self._certificate_dialog_errors.get(grau)
        if error:
            raise ServicoIndisponivelError(
                "o PJeOffice não iniciou a autenticação por certificado: " + error
            )

        page = self._pages.get(grau)
        if (
            self._authentication_modes.get(grau) != "certificado_digital"
            or page is None
            or page.is_closed()
        ):
            return StatusLogin(
                grau=grau,
                autenticado=False,
                estado=EstadoLogin.NAO_INICIADO,
                modo_autenticacao="certificado_digital",
                url_atual="",
                mensagem=(
                    "nenhuma tentativa de login por certificado está ativa; "
                    "abra o login assistido primeiro"
                ),
            )

        authenticated = self._is_authenticated(page.url, grau)
        if authenticated:
            return StatusLogin(
                grau=grau,
                autenticado=True,
                estado=EstadoLogin.AUTENTICADO,
                modo_autenticacao="certificado_digital",
                url_atual=self._canonical_pje_url(grau),
                mensagem=("login por certificado confirmado; sessão mantida somente em memória"),
            )

        if self._is_expected_certificate_flow(page.url, grau):
            message = (
                "aguardando aprovação humana no PJeOffice e eventual MFA; "
                "informe o PIN somente no PJeOffice. O PJeOffice costuma levar "
                "dezenas de segundos para responder: a janela parada nesse "
                "intervalo é espera, não travamento — não a feche"
            )
        else:
            message = (
                "login por certificado não confirmado: o navegador não retornou "
                "ao domínio e grau esperados do PJe/TJPE"
            )
        return StatusLogin(
            grau=grau,
            autenticado=False,
            estado=(
                EstadoLogin.AGUARDANDO_INTERACAO
                if self._is_expected_certificate_flow(page.url, grau)
                else EstadoLogin.ERRO
            ),
            modo_autenticacao="certificado_digital",
            url_atual=self._canonical_flow_url(page.url, grau),
            mensagem=message,
        )

    async def _handle_totp(self, page: Page, seed: SecretStr | None) -> None:
        selectors = (
            "#otp, input[name='otp'], input[name='totp'], input[autocomplete='one-time-code']"
        )
        otp = page.locator(selectors).first
        try:
            await otp.wait_for(state="visible", timeout=2_500)
        except PlaywrightTimeoutError:
            return

        if seed is None:
            if self.config.headless:
                raise CredenciaisAusentesError(
                    "o SSO solicitou MFA. Configure PJE_TJPE_AUTH_HEADLESS=false para "
                    "digitar o código no navegador ou opte por guardar a semente "
                    "TOTP via 'pje-tjpe setup'"
                )
            try:
                await otp.wait_for(state="hidden", timeout=120_000)
            except PlaywrightTimeoutError:
                raise ServicoIndisponivelError(
                    "o código MFA não foi concluído no navegador em até 120 segundos"
                ) from None
            return
        code = pyotp.TOTP(seed.get_secret_value()).now()
        await otp.fill(code)
        submit = page.locator("#kc-login, button[type='submit'], input[type='submit']").first
        await submit.click()

    @staticmethod
    async def _login_error(page: Page) -> str | None:
        candidates = page.locator(
            ".alert-error, .alert-danger, #input-error, .kc-feedback-text, [role='alert']"
        )
        texts = [" ".join(text.split()) for text in await candidates.all_inner_texts()]
        return (
            "o SSO apresentou uma mensagem de erro durante a autenticação" if any(texts) else None
        )

    @staticmethod
    def _is_authenticated(url: str, grau: Grau) -> bool:
        parsed = urlparse(url)
        return (
            parsed.scheme == "https"
            and parsed.netloc == "pje.cloud.tjpe.jus.br"
            and parsed.path.startswith(f"/{grau.value}/")
            and "login" not in parsed.path.lower()
        )

    @staticmethod
    def _is_expected_certificate_flow(url: str, grau: Grau) -> bool:
        parsed = urlparse(url)
        return parsed.scheme == "https" and (
            parsed.netloc == "sso.cloud.pje.jus.br"
            or (
                parsed.netloc == "pje.cloud.tjpe.jus.br"
                and parsed.path.startswith(f"/{grau.value}/")
            )
        )

    @staticmethod
    def _canonical_pje_url(grau: Grau) -> str:
        return f"https://pje.cloud.tjpe.jus.br/{grau.value}/"

    @classmethod
    def _canonical_flow_url(cls, url: str, grau: Grau) -> str:
        parsed = urlparse(url)
        if parsed.scheme == "https" and parsed.netloc == "sso.cloud.pje.jus.br":
            return "https://sso.cloud.pje.jus.br/"
        if (
            parsed.scheme == "https"
            and parsed.netloc == "pje.cloud.tjpe.jus.br"
            and parsed.path.startswith(f"/{grau.value}/")
        ):
            return cls._canonical_pje_url(grau)
        return ""

    async def _probe_authenticated_session(self, grau: Grau) -> bool:
        """Confirma o cookie com um GET protegido, sem alterar a página visível."""
        context = self._contexts.get(grau)
        page = self._pages.get(grau)
        if context is None or page is None or page.is_closed():
            return False
        if not self._is_authenticated(page.url, grau):
            return False

        response = None
        try:
            probe_url = (
                f"{self.config.urls.pje_base(grau.value)}/Painel/painel_usuario/advogado.seam"
            )
            response = await context.request.get(
                probe_url,
                fail_on_status_code=False,
                timeout=self.config.timeout_ms,
            )
            if response.status == 401:
                return False
            if not (200 <= response.status < 300):
                raise ServicoIndisponivelError(
                    "o PJe/TJPE não respondeu ao probe protegido com estado conclusivo"
                )
            if not self._is_authenticated(response.url, grau):
                parsed_response_url = urlparse(response.url)
                returned_to_login = parsed_response_url.scheme == "https" and (
                    parsed_response_url.netloc == "sso.cloud.pje.jus.br"
                    or (
                        parsed_response_url.netloc == "pje.cloud.tjpe.jus.br"
                        and parsed_response_url.path.startswith(f"/{grau.value}/")
                        and "login" in parsed_response_url.path.casefold()
                    )
                )
                if returned_to_login:
                    return False
                raise ServicoIndisponivelError(
                    "o probe protegido do PJe/TJPE retornou um destino inesperado"
                )
            body = (await response.body()).decode("utf-8", errors="replace").casefold()
            login_markers = ('id="kc-pje-office"', 'id="kc-form-login"')
            return not any(marker in body for marker in login_markers)
        except Exception:
            raise ServicoIndisponivelError(
                "não foi possível confirmar a sessão protegida do PJe/TJPE"
            ) from None
        finally:
            if response is not None:
                try:
                    await response.dispose()
                except Exception:
                    pass

    def _detach_certificate_dialog(self, grau: Grau) -> None:
        page = self._pages.get(grau)
        handler = self._certificate_dialog_handlers.pop(grau, None)
        if page is not None and handler is not None and not page.is_closed():
            page.remove_listener("dialog", handler)

    async def _close_degree(self, grau: Grau) -> None:
        context = self._contexts.pop(grau, None)
        self._pages.pop(grau, None)
        self._session_generations.pop(grau, None)
        self._authentication_modes.pop(grau, None)
        self._certificate_dialog_errors.pop(grau, None)
        self._certificate_dialog_handlers.pop(grau, None)
        if self._active_certificate_degree == grau:
            self._active_certificate_degree = None
        if context:
            await context.close()

    async def close(self) -> None:
        async with self._authentication_flow_lock:
            for grau in list(self._contexts):
                async with self._locks[grau]:
                    await self._close_degree(grau)
