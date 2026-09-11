from __future__ import annotations

# pyright: reportPrivateUsage=false
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from playwright.async_api import BrowserContext, Page

import mcp_pje_tjpe.pje_read as pje_read
from mcp_pje_tjpe.config import Settings
from mcp_pje_tjpe.errors import ValidacaoError
from mcp_pje_tjpe.models import Grau
from mcp_pje_tjpe.pje_auth import PjeSessionManager, ReadSessionLease
from mcp_pje_tjpe.pje_read import (
    PjeReadService,
    _AcervoBinding,
    _DocumentBinding,
    _FetchedDocument,
)

NUMERO = "9999902-02.2099.8.17.9999"
# _REFERENCE exige 20-80 caracteres de token.
REF_UM = "referencia-de-documento-um"
REF_DOIS = "referencia-de-documento-dois"
DOC = "196750071"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _FakeSessions:
    def __init__(self, lease: ReadSessionLease) -> None:
        self.lease = lease

    @asynccontextmanager
    async def read_session(self, grau: Grau) -> AsyncGenerator[ReadSessionLease]:
        assert grau is self.lease.grau
        yield self.lease


def _monta(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[PjeReadService, list[str]]:
    """Serviço com a navegação dublada e um contador de revalidações completas."""
    lease = ReadSessionLease(
        grau=Grau.PRIMEIRO,
        context=cast(BrowserContext, object()),
        page=cast(Page, object()),
        generation="generation-a",
    )
    service = PjeReadService(
        cast(PjeSessionManager, _FakeSessions(lease)),
        Settings(data_dir=tmp_path / "data", downloads_dir=tmp_path / "downloads"),
    )
    service._acervo[(lease.generation, Grau.PRIMEIRO, NUMERO)] = _AcervoBinding(
        generation=lease.generation,
        grau=Grau.PRIMEIRO,
        numero=NUMERO,
        processo_id="20",
        autos_url="https://pje.cloud.tjpe.jus.br/1g/Autos?id=20",
        jurisdicao="Abreu e Lima - Varas",
    )
    for referencia in (REF_UM, REF_DOIS):
        service._documents[referencia] = _DocumentBinding(
            generation=lease.generation,
            grau=Grau.PRIMEIRO,
            numero=NUMERO,
            processo_id="20",
            documento_id=DOC,
            titulo="Documento juntado",
            bloqueado_por_ciencia=False,
        )

    navegacoes: list[str] = []

    async def fake_load(_page: Page, _grau: Grau, jurisdicao: str | None = None) -> None:
        navegacoes.append(jurisdicao or "")

    async def fake_refresh(_lease: ReadSessionLease, _numero: str) -> None:
        return None

    async def fake_open(
        _lease: ReadSessionLease, _numero: str
    ) -> tuple[Page, BrowserContext, str]:
        class _Pagina:
            async def evaluate(self, _script: str) -> str:
                return "Mozilla/5.0 teste"

        class _Contexto:
            async def close(self) -> None:
                return None

        return cast(Page, _Pagina()), cast(BrowserContext, _Contexto()), "20"

    async def fake_entries(_page: Page, _limit: int) -> tuple[list[dict[str, Any]], bool]:
        return [{"id": DOC, "context": "Documento juntado"}], False

    async def fake_bytes(*_args: object, **_kwargs: object) -> _FetchedDocument:
        return cast(_FetchedDocument, object())

    monkeypatch.setattr(service, "_load_acervo", fake_load)
    monkeypatch.setattr(service, "_refresh_acervo_binding", fake_refresh)
    monkeypatch.setattr(service, "_open_autos_from_acervo", fake_open)
    monkeypatch.setattr(service, "_extract_documents", fake_entries)
    monkeypatch.setattr(service, "_download_document_bytes", fake_bytes)
    return service, navegacoes


@pytest.mark.anyio
async def test_documents_of_one_process_share_a_single_revalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, navegacoes = _monta(tmp_path, monkeypatch)
    async with service.sessions.read_session(Grau.PRIMEIRO) as lease:
        for _ in range(5):
            await service._fetch_registered_document(lease, NUMERO, Grau.PRIMEIRO, REF_UM)

    # Antes, cada peça repetia painel -> Acervo -> jurisdição -> Autos.
    assert navegacoes == ["Abreu e Lima - Varas"]


@pytest.mark.anyio
async def test_the_window_expires_and_the_full_proof_runs_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, navegacoes = _monta(tmp_path, monkeypatch)
    relogio = [1_000.0]
    monkeypatch.setattr(pje_read.time, "monotonic", lambda: relogio[0])

    async with service.sessions.read_session(Grau.PRIMEIRO) as lease:
        await service._fetch_registered_document(lease, NUMERO, Grau.PRIMEIRO, REF_UM)
        relogio[0] += pje_read._REVALIDACAO_AUTOS_SEGUNDOS + 1
        await service._fetch_registered_document(lease, NUMERO, Grau.PRIMEIRO, REF_DOIS)

    assert len(navegacoes) == 2


@pytest.mark.anyio
async def test_a_document_that_became_blocked_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _ = _monta(tmp_path, monkeypatch)

    async def bloqueado(_page: Page, _limit: int) -> tuple[list[dict[str, Any]], bool]:
        return [{"id": DOC, "context": "Aguardando tomar ciência"}], False

    monkeypatch.setattr(service, "_extract_documents", bloqueado)
    async with service.sessions.read_session(Grau.PRIMEIRO) as lease:
        with pytest.raises(ValidacaoError, match="pendente de ciência"):
            await service._fetch_registered_document(lease, NUMERO, Grau.PRIMEIRO, REF_UM)


@pytest.mark.anyio
async def test_invalidating_the_window_forces_the_full_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, navegacoes = _monta(tmp_path, monkeypatch)
    async with service.sessions.read_session(Grau.PRIMEIRO) as lease:
        await service._fetch_registered_document(lease, NUMERO, Grau.PRIMEIRO, REF_UM)
        service.invalidar_revalidacao_autos(lease, NUMERO)
        await service._fetch_registered_document(lease, NUMERO, Grau.PRIMEIRO, REF_DOIS)

    assert len(navegacoes) == 2
