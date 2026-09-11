from __future__ import annotations

from mcp_pje_tjpe.browser import CHANNEL, BrowserManager
from mcp_pje_tjpe.config import Settings
from mcp_pje_tjpe.credentials import CredentialStore
from mcp_pje_tjpe.errors import PjeTjpeError
from mcp_pje_tjpe.models import Diagnostico, StatusItem


async def run_diagnostics(
    browser: BrowserManager, config: Settings, credentials: CredentialStore
) -> Diagnostico:
    items: list[StatusItem] = []

    # O navegador é pré-requisito de todo o resto; verificá-lo primeiro evita repetir
    # a mesma falha em cada endpoint e diz de imediato o que precisa ser instalado.
    try:
        await browser.start()
        items.append(
            StatusItem(
                nome=f"Navegador (canal {CHANNEL})",
                sucesso=True,
                detalhe="disponível para navegação local",
            )
        )
    except PjeTjpeError as exc:
        items.append(
            StatusItem(nome=f"Navegador (canal {CHANNEL})", sucesso=False, detalhe=str(exc))
        )
        return Diagnostico(sucesso=False, itens=items)

    targets = {
        "SICAJUD (simulação)": config.urls.sicajud_simulacao,
        "PJe TJPE 1G": f"{config.urls.pje_1g}/ConsultaPublica/listView.seam",
        "PJe TJPE 2G": f"{config.urls.pje_2g}/ConsultaPublica/listView.seam",
    }
    for name, url in targets.items():
        try:
            async with browser.page() as page:
                response = await page.goto(url, wait_until="domcontentloaded")
                ok = response is not None and response.ok
                detail = f"HTTP {response.status}" if response else "sem resposta HTTP"
                items.append(StatusItem(nome=name, sucesso=ok, detalhe=detail))
        except Exception as exc:
            items.append(StatusItem(nome=name, sucesso=False, detalhe=str(exc)))

    configured = credentials.has_credentials()
    items.append(
        StatusItem(
            nome="Credenciais locais",
            sucesso=True,
            detalhe=(
                "configuradas no Keychain"
                if configured
                else "opcionais e não configuradas; fluxos públicos estão disponíveis"
            ),
        )
    )
    return Diagnostico(sucesso=all(item.sucesso for item in items), itens=items)
