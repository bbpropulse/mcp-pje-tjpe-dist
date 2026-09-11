# pyright: reportPrivateUsage=false

from typing import cast

import pytest
from playwright.async_api import BrowserContext, Page

from mcp_pje_tjpe.browser import BrowserManager
from mcp_pje_tjpe.config import Settings
from mcp_pje_tjpe.credentials import CredentialStore
from mcp_pje_tjpe.models import Grau
from mcp_pje_tjpe.pje_auth import PjeSessionManager


class _LocalPage:
    url = "https://pje.cloud.tjpe.jus.br/1g/Painel/painel_usuario/advogado.seam"

    def is_closed(self) -> bool:
        return False


def test_authenticated_url_must_return_to_expected_tjpe_degree() -> None:
    assert PjeSessionManager._is_authenticated(
        "https://pje.cloud.tjpe.jus.br/1g/Painel/painel_usuario/advogado.seam",
        Grau.PRIMEIRO,
    )
    assert not PjeSessionManager._is_authenticated(
        "https://sso.cloud.pje.jus.br/auth/realms/pje/protocol/openid-connect/auth",
        Grau.PRIMEIRO,
    )
    assert not PjeSessionManager._is_authenticated(
        "https://pje.cloud.tjpe.jus.br/2g/Painel/painel_usuario/advogado.seam",
        Grau.PRIMEIRO,
    )
    assert not PjeSessionManager._is_authenticated(
        "https://pje.cloud.tjpe.jus.br.evil.example/1g/Painel",
        Grau.PRIMEIRO,
    )
    assert not PjeSessionManager._is_authenticated(
        "https://pje.cloud.tjpe.jus.br/1g/login.seam",
        Grau.PRIMEIRO,
    )
    assert not PjeSessionManager._is_authenticated(
        "http://pje.cloud.tjpe.jus.br/1g/Painel",
        Grau.PRIMEIRO,
    )
    assert not PjeSessionManager._is_authenticated(
        "https://pje.cloud.tjpe.jus.br:443/1g/Painel",
        Grau.PRIMEIRO,
    )
    assert not PjeSessionManager._is_authenticated(
        "https://usuario@pje.cloud.tjpe.jus.br/1g/Painel",
        Grau.PRIMEIRO,
    )


@pytest.mark.anyio
async def test_cached_read_session_never_runs_remote_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = PjeSessionManager(
        cast(BrowserManager, object()),
        Settings(),
        cast(CredentialStore, object()),
    )
    context = cast(BrowserContext, object())
    page = cast(Page, _LocalPage())
    manager._contexts[Grau.PRIMEIRO] = context
    manager._pages[Grau.PRIMEIRO] = page
    manager._session_generations[Grau.PRIMEIRO] = "generation-local"

    async def forbidden_probe(_grau: Grau) -> bool:
        raise AssertionError("a leitura do cache não pode consultar o tribunal")

    monkeypatch.setattr(manager, "_probe_authenticated_session", forbidden_probe)

    async with manager.cached_read_session(Grau.PRIMEIRO) as lease:
        assert lease.context is context
        assert lease.page is page
        assert lease.generation == "generation-local"
