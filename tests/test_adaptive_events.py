from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import pytest

from mcp_pje_tjpe.adaptive.adapters import AdapterRegistry
from mcp_pje_tjpe.adaptive.controller import AdaptiveNavigation
from mcp_pje_tjpe.adaptive.events import ObservationStore
from mcp_pje_tjpe.adaptive.models import ControleEstrutural, ObservacaoNavegacao
from mcp_pje_tjpe.config import ModoAdaptativo
from mcp_pje_tjpe.errors import ServicoIndisponivelError
from mcp_pje_tjpe.tribunals import TribunalCodigo


def _observation(
    index: int,
    *,
    controls: tuple[ControleEstrutural, ...] = (),
) -> ObservacaoNavegacao:
    return ObservacaoNavegacao(
        referencia=f"observation-reference-{index:04d}",
        tribunal=TribunalCodigo.TJPE,
        instancia="tjpe_1g",
        fluxo="pesquisa_geral",
        etapa="search_submit",
        modo=ModoAdaptativo.OBSERVE,
        origem="https://pje.cloud.tjpe.jus.br",
        formato_caminho="/:dynamic/processo/consultaprocesso/listview.seam",
        assinatura_pagina=f"{index:064x}",
        codigo_falha="unique_control_not_found",
        controles=controls,
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_store_round_trip_is_newest_first_and_applies_retention(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path, max_events=3)

    results = await asyncio.gather(
        *(store.append(store.seal(_observation(index))) for index in range(6))
    )

    assert results == [True] * 6
    assert await store.count() == 3
    observations = await store.list(limit=3)
    assert [item.referencia for item in observations] == [
        "observation-reference-0005",
        "observation-reference-0004",
        "observation-reference-0003",
    ]
    assert len(store.path.read_text(encoding="utf-8").splitlines()) == 3


@pytest.mark.anyio
async def test_store_uses_private_directory_file_lock_and_key_permissions(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path)

    signature = store.sign(b"estrutura conhecida")
    assert await store.append(store.seal(_observation(1))) is True

    assert len(signature) == 64
    for path in (store.path, store.lock_path, store.key_path, store.retention_path):
        assert path.is_file()
    if os.name != "nt":
        assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
        for path in (store.path, store.lock_path, store.key_path, store.retention_path):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_store_signature_is_stable_per_installation_and_does_not_expose_input(
    tmp_path: Path,
) -> None:
    first = ObservationStore(tmp_path)
    same_installation = ObservationStore(tmp_path)

    signature = first.sign(b"JSESSIONID=segredo")

    assert signature == same_installation.sign(b"JSESSIONID=segredo")
    assert signature != first.sign(b"JSESSIONID=outro")
    assert "segredo" not in signature
    assert "JSESSIONID" not in signature


def test_signing_key_creation_handles_short_binary_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_pje_tjpe.adaptive.events as events

    original_write = events.os.write

    def short_write(fd: int, payload: bytes | memoryview) -> int:
        return original_write(fd, payload[:3])

    monkeypatch.setattr(events.os, "write", short_write)
    store = ObservationStore(tmp_path)
    signature = store.sign(b"estrutura")

    assert len(store.key_path.read_bytes()) == 32
    assert ObservationStore(tmp_path).sign(b"estrutura") == signature


@pytest.mark.anyio
async def test_sanitized_control_persists_only_vocab_and_identifier_signature(
    tmp_path: Path,
) -> None:
    store = ObservationStore(tmp_path)
    navigation = AdaptiveNavigation(AdapterRegistry(), store, ModoAdaptativo.OBSERVE)
    raw_secret = "JSESSIONID=sessao-super-secreta"
    raw_npu = "9999999-99.2099.8.17.9999"
    controls = navigation.sanitize_controls(
        [
            {
                "tag": "button",
                "role": "button",
                "type": "submit",
                "visible": True,
                "enabled": True,
                "editable": None,
                "nativeSubmit": True,
                "ownsForm": True,
                "semanticText": f"Pesquisar Alice {raw_npu} password ViewState",
                "identifier": raw_secret,
            }
        ]
    )

    assert controls[0].tokens_semanticos == ("pesquisar",)
    assert controls[0].assinatura_identificador is not None
    assert raw_secret not in controls[0].model_dump_json()
    assert raw_npu not in controls[0].model_dump_json()
    assert await store.append(store.seal(_observation(1, controls=controls))) is True

    persisted = store.path.read_text(encoding="utf-8")
    for forbidden in (raw_secret, raw_npu, "Alice", "password", "ViewState"):
        assert forbidden not in persisted


@pytest.mark.anyio
async def test_malformed_jsonl_fails_closed_without_leaking_contents(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path)
    store.root.mkdir(parents=True, mode=0o700)
    store.path.write_text('{"cookie":"secret-value"}\n', encoding="utf-8")
    os.chmod(store.path, 0o600)

    assert await store.list(limit=1) == []
    assert store.last_error == "não foi possível ler o JSONL sanitizado"
    assert "secret-value" not in (store.last_error or "")

    navigation = AdaptiveNavigation(AdapterRegistry(), store, ModoAdaptativo.OBSERVE)
    with pytest.raises(ServicoIndisponivelError, match="não pôde ser lido"):
        await navigation.list_observations(limit=1)


@pytest.mark.anyio
async def test_store_refuses_symlinked_adaptive_directory(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "adaptive").symlink_to(external, target_is_directory=True)
    store = ObservationStore(data_dir)

    assert await store.append(_observation(1)) is False
    assert store.last_error == "não foi possível atualizar o JSONL sanitizado"
    assert list(external.iterdir()) == []


@pytest.mark.anyio
async def test_store_refuses_dangling_symlink_instead_of_reporting_empty(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "adaptive").symlink_to(tmp_path / "missing", target_is_directory=True)
    store = ObservationStore(data_dir)

    assert await store.count() == 0
    assert store.last_error == "não foi possível contar o JSONL sanitizado"


@pytest.mark.anyio
async def test_shared_store_rejects_retention_mismatch_without_truncating(
    tmp_path: Path,
) -> None:
    first = ObservationStore(tmp_path, max_events=3)
    assert await first.append(first.seal(_observation(1))) is True

    conflicting = ObservationStore(tmp_path, max_events=1)
    assert await conflicting.append(conflicting.seal(_observation(2))) is False
    assert conflicting.last_error == "não foi possível atualizar o JSONL sanitizado"
    assert await first.count() == 1
    assert len(first.path.read_text(encoding="utf-8").splitlines()) == 1


@pytest.mark.anyio
async def test_append_revalidates_model_copy_before_persisting(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path)
    sealed = store.seal(_observation(1))
    poisoned = sealed.model_copy(update={"codigo_falha": "JSESSIONID=segredo"})

    assert await store.append(poisoned) is False
    assert not store.path.exists()


@pytest.mark.anyio
async def test_store_rejects_invalid_limits_and_preserves_existing_events(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="entre 1"):
        ObservationStore(tmp_path, max_events=0)

    store = ObservationStore(tmp_path, max_events=2)
    assert await store.append(store.seal(_observation(1))) is True

    with pytest.raises(ValueError, match="intervalo"):
        await store.list(limit=0)
    with pytest.raises(ValueError, match="intervalo"):
        await store.list(limit=3)

    assert await store.count() == 1


def test_store_refuses_invalid_persisted_signing_key(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path)
    store.root.mkdir(parents=True, mode=0o700)
    store.key_path.write_bytes(b"short")
    os.chmod(store.key_path, 0o600)

    with pytest.raises(ValueError, match="tamanho inválido"):
        store.sign(b"estrutura")
