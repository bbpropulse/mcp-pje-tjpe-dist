from __future__ import annotations

# pyright: reportPrivateUsage=false
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import pytest
from playwright.async_api import BrowserContext, Page

from mcp_pje_tjpe.config import Settings
from mcp_pje_tjpe.errors import ValidacaoError
from mcp_pje_tjpe.models import Grau, ItemAcervo
from mcp_pje_tjpe.pje_auth import PjeSessionManager, ReadSessionLease
from mcp_pje_tjpe.pje_read import PjeReadService, _filtrar_acervo

# Resumos com a forma que o Acervo do TJPE entrega. Nomes e números são
# inventados: um NPU real devolve as partes verdadeiras na consulta pública,
# o que anularia a troca dos nomes.
RESUMOS = [
    "PETICIONAR ProceComCiv 9999901-17.2099.8.17.9999 Contratos Bancários "
    "MARIA DA CONCEIÇÃO X BANCO BRADESCO S/A /3ª Vara Cível da Comarca de Abreu e Lima "
    "Distribuído em 09/01/2025 Último movimento: 27/08/2026 08:45 - Proferido despacho",
    "PETICIONAR ProceComCiv 9999902-02.2099.8.17.9999 Contratos Bancários "
    "JOÃO INÁCIO X BANCO BMG /2ª Vara Cível da Comarca de Abreu e Lima "
    "Distribuído em 05/08/2024 Último movimento: 27/02/2025 10:55 - Arquivado",
    "PETICIONAR ExeTitExtr 0000111-11.2023.8.17.0001 Alimentos "
    "ANA SOUZA X JOSÉ LIMA /1ª Vara de Família do Recife "
    "Distribuído em 01/03/2023 Último movimento: 10/10/2025 09:00 - Concluso",
]


def _itens() -> list[ItemAcervo]:
    numeros = [
        "9999901-17.2099.8.17.9999",
        "9999902-02.2099.8.17.9999",
        "0000111-11.2023.8.17.0001",
    ]
    return [
        ItemAcervo(numero=n, grau=Grau.PRIMEIRO, resumo=r, autos_disponiveis=True)
        for n, r in zip(numeros, RESUMOS, strict=True)
    ]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.parametrize(
    ("filtro", "esperados"),
    [
        ("bmg", ["9999902-02.2099.8.17.9999"]),
        ("bradesco", ["9999901-17.2099.8.17.9999"]),
        # Sem acento e sem caixa: "conceicao" acha "CONCEIÇÃO".
        ("conceicao", ["9999901-17.2099.8.17.9999"]),
        ("alimentos", ["0000111-11.2023.8.17.0001"]),
        # Vários termos combinam por E, não por OU.
        ("contratos abreu", ["9999901-17.2099.8.17.9999", "9999902-02.2099.8.17.9999"]),
        ("contratos familia", []),
        # O número também entra no que é filtrado.
        ("9999902", ["9999902-02.2099.8.17.9999"]),
    ],
)
def test_the_summary_covers_what_a_lawyer_searches_for(
    filtro: str, esperados: list[str]
) -> None:
    assert [item.numero for item in _filtrar_acervo(_itens(), filtro)] == esperados


def test_an_empty_filter_is_refused_instead_of_matching_everything() -> None:
    for vazio in ("", "   ", "\t"):
        with pytest.raises(ValidacaoError, match="ao menos um termo"):
            _filtrar_acervo(_itens(), vazio)


class _FakeSessions:
    def __init__(self, lease: ReadSessionLease) -> None:
        self.lease = lease

    @asynccontextmanager
    async def read_session(self, grau: Grau) -> AsyncGenerator[ReadSessionLease]:
        assert grau is self.lease.grau
        yield self.lease


class _Corpo:
    async def inner_text(self) -> str:
        return "acervo"


class _PaginaFalsa:
    def locator(self, _seletor: str) -> _Corpo:
        return _Corpo()


@pytest.mark.anyio
async def test_the_filter_runs_before_the_limit_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Filtrar depois do corte devolveria quase nada numa jurisdição grande."""
    lease = ReadSessionLease(
        grau=Grau.PRIMEIRO,
        context=cast(BrowserContext, object()),
        page=cast(Page, _PaginaFalsa()),
        generation="generation-a",
    )
    service = PjeReadService(
        cast(PjeSessionManager, _FakeSessions(lease)),
        Settings(data_dir=tmp_path / "data", downloads_dir=tmp_path / "downloads"),
    )

    async def fake_load(*_args: object, **_kwargs: object) -> None:
        return None

    async def fake_entries(_page: Page) -> list[dict[str, str]]:
        return []

    monkeypatch.setattr(service, "_load_acervo", fake_load)
    monkeypatch.setattr(service, "_extract_acervo_entries", fake_entries)
    monkeypatch.setattr(
        "mcp_pje_tjpe.pje_read.parse_acervo_entries", lambda _raw, _grau: _itens()
    )

    pagina = await service.listar_acervo(
        Grau.PRIMEIRO, jurisdicao="Abreu e Lima - Varas", limite=1, filtro="bmg"
    )

    # Um resultado devolvido, mas o aviso diz sobre quantos o filtro passou.
    assert [p.numero for p in pagina.processos] == ["9999902-02.2099.8.17.9999"]
    assert "3 processos" in pagina.aviso
    assert "nada foi pesquisado ou gravado" in pagina.aviso


@pytest.mark.anyio
async def test_without_a_filter_the_wording_stays_out_of_the_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = ReadSessionLease(
        grau=Grau.PRIMEIRO,
        context=cast(BrowserContext, object()),
        page=cast(Page, _PaginaFalsa()),
        generation="generation-a",
    )
    service = PjeReadService(
        cast(PjeSessionManager, _FakeSessions(lease)),
        Settings(data_dir=tmp_path / "data", downloads_dir=tmp_path / "downloads"),
    )

    async def fake_load(*_args: object, **_kwargs: object) -> None:
        return None

    async def fake_entries(_page: Page) -> list[dict[str, str]]:
        return []

    monkeypatch.setattr(service, "_load_acervo", fake_load)
    monkeypatch.setattr(service, "_extract_acervo_entries", fake_entries)
    monkeypatch.setattr(
        "mcp_pje_tjpe.pje_read.parse_acervo_entries", lambda _raw, _grau: _itens()
    )

    pagina = await service.listar_acervo(Grau.PRIMEIRO, jurisdicao="Abreu e Lima - Varas")

    assert len(pagina.processos) == 3
    assert "filtro" not in pagina.aviso


@pytest.mark.anyio
async def test_the_gap_between_rendered_and_total_is_stated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recife - Varas tem 1650 processos e o painel renderiza 40 por vez.

    Sem o total, `total_carregado` passa a impressão de ser a jurisdição inteira, e
    o filtro local pareceria cobrir tudo quando cobre 2,7%.
    """
    lease = ReadSessionLease(
        grau=Grau.PRIMEIRO,
        context=cast(BrowserContext, object()),
        page=cast(Page, _PaginaFalsa()),
        generation="generation-a",
    )
    service = PjeReadService(
        cast(PjeSessionManager, _FakeSessions(lease)),
        Settings(data_dir=tmp_path / "data", downloads_dir=tmp_path / "downloads"),
    )

    async def fake_load(
        _page: Page, _grau: Grau, _jurisdicao: str | None = None
    ) -> int | None:
        return 1650

    async def fake_entries(_page: Page) -> list[dict[str, str]]:
        return []

    monkeypatch.setattr(service, "_load_acervo", fake_load)
    monkeypatch.setattr(service, "_extract_acervo_entries", fake_entries)
    monkeypatch.setattr(
        "mcp_pje_tjpe.pje_read.parse_acervo_entries", lambda _raw, _grau: _itens()
    )

    pagina = await service.listar_acervo(Grau.PRIMEIRO, jurisdicao="Recife - Varas")

    assert pagina.total_na_jurisdicao == 1650
    assert pagina.total_carregado == 3
    assert "1650 processos" in pagina.aviso
    # O aviso precisa dizer quanto ficou de fora E como buscar o resto.
    assert "trouxe 3 em 1 página(s)" in pagina.aviso
    assert "paginas" in pagina.aviso


@pytest.mark.anyio
async def test_no_gap_no_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lease = ReadSessionLease(
        grau=Grau.PRIMEIRO,
        context=cast(BrowserContext, object()),
        page=cast(Page, _PaginaFalsa()),
        generation="generation-a",
    )
    service = PjeReadService(
        cast(PjeSessionManager, _FakeSessions(lease)),
        Settings(data_dir=tmp_path / "data", downloads_dir=tmp_path / "downloads"),
    )

    async def fake_load(
        _page: Page, _grau: Grau, _jurisdicao: str | None = None
    ) -> int | None:
        return 3

    async def fake_entries(_page: Page) -> list[dict[str, str]]:
        return []

    monkeypatch.setattr(service, "_load_acervo", fake_load)
    monkeypatch.setattr(service, "_extract_acervo_entries", fake_entries)
    monkeypatch.setattr(
        "mcp_pje_tjpe.pje_read.parse_acervo_entries", lambda _raw, _grau: _itens()
    )

    pagina = await service.listar_acervo(Grau.PRIMEIRO, jurisdicao="Abreu e Lima - Varas")

    assert pagina.total_na_jurisdicao == 3
    assert "paginação" not in pagina.aviso


@pytest.mark.anyio
async def test_a_search_does_not_claim_the_rest_is_still_out_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Observado no TJPE: parte='SAUDE EXEMPLO' devolveu 1 de 1650 e o aviso ainda dizia
    que 'o restante exige aumentar paginas'. Não havia restante: parcial era false."""
    lease = ReadSessionLease(
        grau=Grau.PRIMEIRO,
        context=cast(BrowserContext, object()),
        page=cast(Page, _PaginaFalsa()),
        generation="generation-a",
    )
    service = PjeReadService(
        cast(PjeSessionManager, _FakeSessions(lease)),
        Settings(data_dir=tmp_path / "data", downloads_dir=tmp_path / "downloads"),
    )

    async def fake_load(
        _page: Page, _grau: Grau, _jurisdicao: str | None = None
    ) -> int | None:
        return 1650

    async def fake_entries(_page: Page) -> list[dict[str, str]]:
        return []

    async def fake_search(_page: Page, _criterios: dict[str, str]) -> None:
        return None

    monkeypatch.setattr(service, "_load_acervo", fake_load)
    monkeypatch.setattr(service, "_extract_acervo_entries", fake_entries)
    monkeypatch.setattr(service, "_pesquisar_no_acervo", fake_search)
    monkeypatch.setattr(
        "mcp_pje_tjpe.pje_read.parse_acervo_entries", lambda _raw, _grau: _itens()[:1]
    )

    pagina = await service.listar_acervo(
        Grau.PRIMEIRO, jurisdicao="Recife - Varas", parte="SAUDE EXEMPLO"
    )

    assert pagina.total_carregado == 1
    assert "a busca no servidor devolveu 1" in pagina.aviso
    assert "o restante exige" not in pagina.aviso


@pytest.mark.anyio
async def test_a_local_filter_does_not_claim_the_rest_is_still_out_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Observado no TJPE: procurar um processo pelo número dentro da jurisdição
    devolveu 1 de 58 e o aviso ainda mandava 'aumentar paginas ou restringir a
    busca' — a busca já tinha sido restringida."""
    lease = ReadSessionLease(
        grau=Grau.PRIMEIRO,
        context=cast(BrowserContext, object()),
        page=cast(Page, _PaginaFalsa()),
        generation="generation-a",
    )
    service = PjeReadService(
        cast(PjeSessionManager, _FakeSessions(lease)),
        Settings(data_dir=tmp_path / "data", downloads_dir=tmp_path / "downloads"),
    )

    async def fake_load(
        _page: Page, _grau: Grau, _jurisdicao: str | None = None
    ) -> int | None:
        return 58

    async def fake_entries(_page: Page) -> list[dict[str, str]]:
        return []

    monkeypatch.setattr(service, "_load_acervo", fake_load)
    monkeypatch.setattr(service, "_extract_acervo_entries", fake_entries)
    monkeypatch.setattr(
        "mcp_pje_tjpe.pje_read.parse_acervo_entries", lambda _raw, _grau: _itens()
    )
    alvo = _itens()[0].numero

    pagina = await service.listar_acervo(
        Grau.PRIMEIRO, jurisdicao="Recife - Juizados", pesquisa=alvo
    )

    assert pagina.total_carregado == 1
    assert "o filtro local deixou 1" in pagina.aviso
    assert "o restante exige" not in pagina.aviso
