from __future__ import annotations

import asyncio
import re
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from mcp_pje_tjpe.browser import BrowserManager
from mcp_pje_tjpe.config import Settings
from mcp_pje_tjpe.models import Grau

PJE_OFFICE_HOST = "127.0.0.1"
PJE_OFFICE_HOSTS = (PJE_OFFICE_HOST, "::1")
PJE_OFFICE_PORT = 8800

_PORT_IDENTITY_NOTICE = (
    "A porta aberta indica somente que uma conexão TCP foi aceita por um serviço local "
    "possivelmente compatível; isso não autentica a identidade do processo nem confirma que ele "
    "seja o PJeOffice."
)
_NO_LOCAL_DATA_NOTICE = (
    "Nenhum challenge, cookie, CPF, senha, PIN ou outro dado de aplicação foi enviado ao "
    "serviço local."
)
_CERTIFICATE_LABEL = re.compile(r"^\s*certificado\s+digital\s*$", re.IGNORECASE)


class PjeOfficePortStatus(BaseModel):
    """Resultado de um teste TCP passivo na interface de loopback."""

    host: str | None = None
    hosts_testados: list[str] = Field(default_factory=lambda: list(PJE_OFFICE_HOSTS))
    porta: int = PJE_OFFICE_PORT
    conexao_aceita: bool
    identidade_processo_verificada: bool = False
    dados_aplicacao_enviados: bool = False
    detalhe: str


class PjeOfficeSsoStatus(BaseModel):
    """Presença da opção de certificado em uma tela SSO, sem acioná-la."""

    grau: Grau
    url_solicitada: str
    url_final: str | None = None
    http_status: int | None = None
    seletor_kc_pje_office_presente: bool = False
    rotulo_certificado_digital_presente: bool = False
    botao_certificado_presente: bool = False
    clique_realizado: bool = False
    detalhe: str


class PjeOfficeDiagnostic(BaseModel):
    """Diagnóstico passivo de prontidão aparente para login com certificado."""

    porta_local: PjeOfficePortStatus
    sso: list[PjeOfficeSsoStatus] = Field(min_length=2, max_length=2)
    pronto_para_login: bool
    mensagem: str
    aviso_seguranca: str = _NO_LOCAL_DATA_NOTICE


async def check_local_pje_office_port(*, timeout_seconds: float = 1.0) -> PjeOfficePortStatus:
    """Testa somente o handshake TCP de ``localhost:8800`` em IPv4 e IPv6.

    A função não escreve no socket. Uma conexão aceita não identifica o processo que
    escuta na porta e não constitui autenticação do PJeOffice ou do usuário.
    """

    for host in PJE_OFFICE_HOSTS:
        writer: asyncio.StreamWriter | None = None
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, PJE_OFFICE_PORT),
                timeout=timeout_seconds,
            )
        except (OSError, TimeoutError):
            continue
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass

        return PjeOfficePortStatus(
            host=host,
            conexao_aceita=True,
            detalhe=f"{_PORT_IDENTITY_NOTICE} {_NO_LOCAL_DATA_NOTICE}",
        )

    return PjeOfficePortStatus(
        conexao_aceita=False,
        detalhe=(
            f"A porta local {PJE_OFFICE_PORT} não aceitou conexão TCP em IPv4 ou IPv6. "
            f"{_NO_LOCAL_DATA_NOTICE}"
        ),
    )


async def inspect_pje_office_sso(
    browser: BrowserManager,
    config: Settings,
    grau: Grau,
) -> PjeOfficeSsoStatus:
    """Abre o login do grau indicado e apenas inspeciona a opção de certificado."""

    url = f"{config.urls.pje_base(grau.value)}/login.seam"
    try:
        async with browser.page() as page:
            response = await page.goto(url, wait_until="domcontentloaded")
            selector_present = await page.locator("#kc-pje-office").count() > 0
            button_by_label = page.get_by_role("button", name=_CERTIFICATE_LABEL)
            link_by_label = page.get_by_role("link", name=_CERTIFICATE_LABEL)
            label_present = await button_by_label.count() > 0 or await link_by_label.count() > 0
            button_present = selector_present or label_present
            return PjeOfficeSsoStatus(
                grau=grau,
                url_solicitada=url,
                url_final=_safe_final_url(page.url, grau),
                http_status=response.status if response is not None else None,
                seletor_kc_pje_office_presente=selector_present,
                rotulo_certificado_digital_presente=label_present,
                botao_certificado_presente=button_present,
                detalhe=(
                    "A tela SSO expõe a opção Certificado Digital; nenhum clique foi realizado."
                    if button_present
                    else "A tela SSO não expôs a opção Certificado Digital; nenhum "
                    "clique foi realizado."
                ),
            )
    except Exception:
        return PjeOfficeSsoStatus(
            grau=grau,
            url_solicitada=url,
            detalhe=("Não foi possível inspecionar a tela SSO; nenhum clique foi realizado."),
        )


async def diagnose_pje_office(
    browser: BrowserManager,
    config: Settings,
) -> PjeOfficeDiagnostic:
    """Combina a porta local e as duas telas SSO em um diagnóstico passivo."""

    port_status, first_degree, second_degree = await asyncio.gather(
        check_local_pje_office_port(),
        inspect_pje_office_sso(browser, config, Grau.PRIMEIRO),
        inspect_pje_office_sso(browser, config, Grau.SEGUNDO),
    )
    sso = [first_degree, second_degree]
    degrees_with_button = {status.grau for status in sso if status.botao_certificado_presente}
    ready = port_status.conexao_aceita and degrees_with_button == {
        Grau.PRIMEIRO,
        Grau.SEGUNDO,
    }
    if ready:
        message = (
            "A porta local aceita conexão e o SSO de 1G e 2G mostra Certificado Digital. "
            "O ambiente aparenta estar pronto para iniciar um login assistido, mas a porta "
            "aberta não autentica a identidade do processo nem confirma o login do usuário."
        )
    else:
        message = (
            "O ambiente ainda não atende a todos os sinais passivos de prontidão: a porta "
            "local deve aceitar conexão e o SSO de 1G e 2G deve mostrar Certificado Digital. "
            "Uma porta aberta não autentica a identidade do processo."
        )
    return PjeOfficeDiagnostic(
        porta_local=port_status,
        sso=sso,
        pronto_para_login=ready,
        mensagem=message,
    )


def _safe_final_url(url: str, grau: Grau) -> str | None:
    parsed = urlsplit(url)
    if parsed.scheme == "https" and parsed.netloc == "sso.cloud.pje.jus.br":
        return "https://sso.cloud.pje.jus.br/"
    if (
        parsed.scheme == "https"
        and parsed.netloc == "pje.cloud.tjpe.jus.br"
        and parsed.path.startswith(f"/{grau.value}/")
    ):
        return f"https://pje.cloud.tjpe.jus.br/{grau.value}/"
    return None
