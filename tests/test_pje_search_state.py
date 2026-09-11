from __future__ import annotations

# pyright: reportPrivateUsage=false
import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import BrowserContext, Locator, Page

import mcp_pje_tjpe.pje_search as pje_search
from mcp_pje_tjpe.config import Settings
from mcp_pje_tjpe.errors import ServicoIndisponivelError, ValidacaoError
from mcp_pje_tjpe.models import EstadoAcessoPesquisaGeral, Grau
from mcp_pje_tjpe.pje_auth import PjeSessionManager, ReadSessionLease
from mcp_pje_tjpe.pje_read import PjeReadService
from mcp_pje_tjpe.pje_search import (
    PjeSearchService,
    _AccessBinding,
    _DialogState,
    _Plan,
    _plan_fingerprint,
    _PlanStatus,
    _SearchGate,
    _SearchResult,
    _UiSession,
    confirmation_phrase,
)

PREPARATION_REFERENCE = "preparation_reference_01"


def _synthetic_npu(
    sequence: str = "9999999",
    *,
    year: str = "2099",
    origin: str = "9999",
) -> str:
    base = sequence + year + "8" + "17" + origin + "00"
    check_digits = 98 - (int(base) % 97)
    return f"{sequence}-{check_digits:02d}.{year}.8.17.{origin}"


def _search_result(
    *,
    numero: str | None = None,
    grau: Grau = Grau.PRIMEIRO,
    processo_id: str = "123456",
    capability: str = "SEARCH_CAPABILITY",
) -> _SearchResult:
    selected = numero or _synthetic_npu()
    return _SearchResult(
        numero=selected,
        processo_id=processo_id,
        autos_url=(
            f"https://pje.cloud.tjpe.jus.br/{grau.value}/Processo/ConsultaProcesso/"
            f"Detalhe/listProcessoCompletoAdvogado.seam?id={processo_id}&ca={capability}"
        ),
        fingerprint=f"result-fingerprint-{processo_id}",
        anchor=cast(Locator, object()),
    )


def _plan(
    *,
    numero: str | None = None,
    grau: Grau = Grau.PRIMEIRO,
    generation: str = "generation-a",
    processo_id: str = "123456",
    reference: str = PREPARATION_REFERENCE,
    expires_at: datetime | None = None,
) -> _Plan:
    selected = numero or _synthetic_npu()
    result = _search_result(numero=selected, grau=grau, processo_id=processo_id)
    now = datetime.now(UTC)
    return _Plan(
        reference=reference,
        generation=generation,
        grau=grau,
        numero=selected,
        processo_id=processo_id,
        fingerprint=_plan_fingerprint(generation, grau, selected, result),
        created_at=now,
        expires_at=expires_at or now + timedelta(minutes=5),
    )


class _FakeSessions:
    def __init__(self, *, generation: str = "generation-a") -> None:
        self.calls: list[Grau] = []
        self.lease = ReadSessionLease(
            grau=Grau.PRIMEIRO,
            context=cast(BrowserContext, object()),
            page=cast(Page, object()),
            generation=generation,
        )

    @asynccontextmanager
    async def read_session(self, grau: Grau) -> AsyncGenerator[ReadSessionLease]:
        self.calls.append(grau)
        yield ReadSessionLease(
            grau=grau,
            context=self.lease.context,
            page=self.lease.page,
            generation=self.lease.generation,
        )


class _FakeRead:
    def __init__(self) -> None:
        self.registrations: list[tuple[str, str, str]] = []

    def registrar_autos_pesquisa_geral(
        self,
        lease: ReadSessionLease,
        numero: str,
        processo_id: str,
        _autos_url: str,
        _autos_html: str,
    ) -> None:
        self.registrations.append((lease.generation, numero, processo_id))

    def possui_vinculo_acervo(self, _lease: ReadSessionLease, _numero: str) -> bool:
        return False


def _service(
    tmp_path: Path,
    *,
    generation: str = "generation-a",
    registry_limit: int = 500,
) -> tuple[PjeSearchService, _FakeSessions, _FakeRead]:
    sessions = _FakeSessions(generation=generation)
    read = _FakeRead()
    service = PjeSearchService(
        cast(PjeSessionManager, sessions),
        cast(PjeReadService, read),
        Settings(
            timeout_ms=100,
            data_dir=tmp_path / "data",
            downloads_dir=tmp_path / "downloads",
        ),
        registry_limit=registry_limit,
    )
    return service, sessions, read


def _seed_plan(service: PjeSearchService, plan: _Plan | None = None) -> _Plan:
    selected = plan or _plan()
    service._plans[selected.reference] = selected
    service._fingerprints[selected.fingerprint] = selected.reference
    return selected


def _ui(numero: str, grau: Grau) -> _UiSession:
    return _UiSession(
        context=cast(BrowserContext, object()),
        page=cast(Page, object()),
        gate=_SearchGate(grau),
        dialogs=_DialogState(numero),
    )


@pytest.mark.anyio
async def test_invalid_npu_and_nonliteral_confirmation_never_start_opening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, sessions, _ = _service(tmp_path)
    plan = _seed_plan(service)
    runner = AsyncMock()
    monkeypatch.setattr(service, "_run_opening", runner)

    with pytest.raises(ValidacaoError, match="NPU válido"):
        await service.preparar("123", Grau.PRIMEIRO)
    assert sessions.calls == []

    expected = confirmation_phrase(plan.numero, plan.grau)
    for changed in (expected.casefold(), f"{expected} ", expected.replace("CONFIRMO", "Confirmo")):
        with pytest.raises(ValidacaoError, match="confirmação incorreta"):
            await service.abrir(plan.numero, plan.grau, plan.reference, changed)

    runner.assert_not_awaited()
    assert service._opening_tasks == {}
    assert plan.status is _PlanStatus.PREPARADO


@pytest.mark.anyio
async def test_prepare_normalizes_npu_reuses_fingerprint_until_ttl_then_rotates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _ = _service(tmp_path)
    numero = _synthetic_npu()
    result = _search_result(numero=numero)
    ui = _ui(numero, Grau.PRIMEIRO)

    @asynccontextmanager
    async def interactive_session(
        _lease: ReadSessionLease,
        _numero: str,
    ) -> AsyncGenerator[_UiSession]:
        yield ui

    search = AsyncMock(return_value=result)
    monkeypatch.setattr(service, "_interactive_session", interactive_session)
    monkeypatch.setattr(service, "_search_exact", search)

    digits = "".join(character for character in numero if character.isdigit())
    first = await service.preparar(digits, Grau.PRIMEIRO)
    repeated = await service.preparar(numero, Grau.PRIMEIRO)

    assert first.numero == numero
    assert first.frase_confirmacao == confirmation_phrase(numero, Grau.PRIMEIRO)
    assert repeated.referencia_preparo == first.referencia_preparo
    assert search.await_count == 2

    service._plans[first.referencia_preparo].expires_at = datetime.now(UTC) - timedelta(seconds=1)
    renewed = await service.preparar(numero, Grau.PRIMEIRO)

    assert renewed.referencia_preparo != first.referencia_preparo
    assert renewed.frase_confirmacao == first.frase_confirmacao
    assert (
        service._fingerprints[
            result_fingerprint := _plan_fingerprint("generation-a", Grau.PRIMEIRO, numero, result)
        ]
        == renewed.referencia_preparo
    )
    assert result_fingerprint == service._plans[renewed.referencia_preparo].fingerprint


@pytest.mark.anyio
async def test_prepare_uses_existing_acervo_binding_without_general_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, sessions, read = _service(tmp_path)
    numero = _synthetic_npu()

    def has_acervo(_lease: ReadSessionLease, _numero: str) -> bool:
        return True

    monkeypatch.setattr(read, "possui_vinculo_acervo", has_acervo)

    def unexpected_interactive_session(*_args: object) -> None:
        raise AssertionError("a Pesquisa Geral não deve abrir quando o NPU já está no Acervo")

    monkeypatch.setattr(service, "_interactive_session", unexpected_interactive_session)

    with pytest.raises(ValidacaoError, match="vínculo auditado no Acervo"):
        await service.preparar(numero, Grau.PRIMEIRO)

    assert sessions.calls == [Grau.PRIMEIRO]
    assert service._plans == {}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("reference", "numero", "grau", "message"),
    [
        ("short", _synthetic_npu(), Grau.PRIMEIRO, "referência de preparação inválida"),
        (
            "unknown_preparation_ref_01",
            _synthetic_npu(),
            Grau.PRIMEIRO,
            "desconhecida ou expirada",
        ),
        (
            PREPARATION_REFERENCE,
            _synthetic_npu("9999998"),
            Grau.PRIMEIRO,
            "outro grau ou processo",
        ),
        (PREPARATION_REFERENCE, _synthetic_npu(), Grau.SEGUNDO, "outro grau ou processo"),
    ],
)
async def test_preparation_reference_is_bound_to_exact_npu_and_degree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reference: str,
    numero: str,
    grau: Grau,
    message: str,
) -> None:
    service, sessions, _ = _service(tmp_path)
    _seed_plan(service)
    runner = AsyncMock()
    monkeypatch.setattr(service, "_run_opening", runner)

    with pytest.raises(ValidacaoError, match=message):
        await service.abrir(numero, grau, reference, confirmation_phrase(numero, grau))

    runner.assert_not_awaited()
    assert sessions.calls == []
    assert service._opening_tasks == {}


@pytest.mark.anyio
async def test_plan_from_previous_authenticated_generation_is_rejected_before_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, sessions, _ = _service(tmp_path, generation="generation-b")
    plan = _seed_plan(service, _plan(generation="generation-a"))
    interactive = AsyncMock()
    monkeypatch.setattr(service, "_interactive_session", interactive)

    with pytest.raises(ValidacaoError, match="sessão autenticada anterior"):
        await service.abrir(
            plan.numero,
            plan.grau,
            plan.reference,
            confirmation_phrase(plan.numero, plan.grau),
        )

    assert sessions.calls == [Grau.PRIMEIRO]
    interactive.assert_not_awaited()
    assert plan.status is _PlanStatus.PREPARADO
    assert plan.access_reference is None
    assert service._opening_tasks == {}


@pytest.mark.anyio
async def test_concurrent_openers_share_exactly_one_remote_open_and_access_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _ = _service(tmp_path)
    plan = _seed_plan(service)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def open_once(_plan: _Plan) -> None:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()

    monkeypatch.setattr(service, "_open_once", open_once)
    phrase = confirmation_phrase(plan.numero, plan.grau)
    first = asyncio.ensure_future(service.abrir(plan.numero, plan.grau, plan.reference, phrase))
    await started.wait()
    second = asyncio.ensure_future(service.abrir(plan.numero, plan.grau, plan.reference, phrase))
    await asyncio.sleep(0)
    release.set()
    one, two = await asyncio.gather(first, second)

    assert calls == 1
    assert one.referencia_acesso == two.referencia_acesso
    assert {one.reutilizada, two.reutilizada} == {False, True}
    assert one.estado is EstadoAcessoPesquisaGeral.ABERTO

    repeated = await service.abrir(plan.numero, plan.grau, plan.reference, phrase)
    assert repeated.referencia_acesso == one.referencia_acesso
    assert repeated.reutilizada is True
    assert calls == 1


@pytest.mark.anyio
async def test_cancelling_one_waiter_does_not_cancel_shared_opening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _ = _service(tmp_path)
    plan = _seed_plan(service)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def open_once(_plan: _Plan) -> None:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()

    monkeypatch.setattr(service, "_open_once", open_once)
    phrase = confirmation_phrase(plan.numero, plan.grau)
    cancelled_waiter = asyncio.ensure_future(
        service.abrir(plan.numero, plan.grau, plan.reference, phrase)
    )
    await started.wait()
    surviving_waiter = asyncio.ensure_future(
        service.abrir(plan.numero, plan.grau, plan.reference, phrase)
    )
    await asyncio.sleep(0)

    cancelled_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter
    release.set()
    result = await surviving_waiter

    assert calls == 1
    assert result.estado is EstadoAcessoPesquisaGeral.ABERTO
    assert result.reutilizada is True
    assert plan.access_reference == result.referencia_acesso


@pytest.mark.anyio
async def test_internal_cancellation_before_click_resets_plan_and_allows_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _ = _service(tmp_path)
    plan = _seed_plan(service)
    ui = _ui(plan.numero, plan.grau)
    searching = asyncio.Event()
    hold_search = asyncio.Event()

    @asynccontextmanager
    async def interactive_session(
        _lease: ReadSessionLease,
        _numero: str,
    ) -> AsyncGenerator[_UiSession]:
        yield ui

    async def search_exact(_ui: _UiSession, _numero: str, _grau: Grau) -> _SearchResult:
        searching.set()
        await hold_search.wait()
        return _search_result(numero=plan.numero, grau=plan.grau)

    monkeypatch.setattr(service, "_interactive_session", interactive_session)
    monkeypatch.setattr(service, "_search_exact", search_exact)
    plan.status = _PlanStatus.ABRINDO
    task = asyncio.create_task(service._run_opening(plan.reference))
    service._opening_tasks[plan.reference] = task
    await searching.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert plan.status is _PlanStatus.PREPARADO
    assert plan.access_reference is None
    assert service._opening_tasks == {}

    retry = AsyncMock()
    monkeypatch.setattr(service, "_open_once", retry)
    result = await service.abrir(
        plan.numero,
        plan.grau,
        plan.reference,
        confirmation_phrase(plan.numero, plan.grau),
    )
    assert result.estado is EstadoAcessoPesquisaGeral.ABERTO
    retry.assert_awaited_once_with(plan)


@pytest.mark.anyio
async def test_internal_cancellation_after_click_becomes_tombstone_and_never_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, read = _service(tmp_path)
    plan = _seed_plan(service)
    result = _search_result(numero=plan.numero, grau=plan.grau, processo_id=plan.processo_id)
    ui = _ui(plan.numero, plan.grau)
    click_started = asyncio.Event()
    hold_click = asyncio.Event()
    clicks = 0

    @asynccontextmanager
    async def interactive_session(
        _lease: ReadSessionLease,
        _numero: str,
    ) -> AsyncGenerator[_UiSession]:
        yield ui

    async def click_exact_result(
        _ui: _UiSession,
        _result: _SearchResult,
        *,
        timeout_ms: int,
    ) -> Page:
        nonlocal clicks
        assert timeout_ms == 100
        clicks += 1
        click_started.set()
        await hold_click.wait()
        raise AssertionError("o clique sintético deveria ser cancelado")

    monkeypatch.setattr(service, "_interactive_session", interactive_session)
    monkeypatch.setattr(service, "_search_exact", AsyncMock(return_value=result))
    monkeypatch.setattr(pje_search, "_click_exact_result", click_exact_result)
    plan.status = _PlanStatus.ABRINDO
    task = asyncio.create_task(service._run_opening(plan.reference))
    service._opening_tasks[plan.reference] = task
    await click_started.wait()

    task.cancel()
    first = await task

    assert first.estado is EstadoAcessoPesquisaGeral.INDETERMINADO
    assert first.reutilizada is False
    assert plan.status is _PlanStatus.INDETERMINADO
    assert plan.access_reference == first.referencia_acesso
    assert service._opening_tasks == {}
    assert read.registrations == []
    assert ui.gate.opening_target is None
    assert ui.gate.remaining_autos_gets == 0

    repeated = await service.abrir(
        plan.numero,
        plan.grau,
        plan.reference,
        confirmation_phrase(plan.numero, plan.grau),
    )
    assert repeated.estado is EstadoAcessoPesquisaGeral.INDETERMINADO
    assert repeated.referencia_acesso == first.referencia_acesso
    assert repeated.reutilizada is True
    assert "não tentará novamente" in repeated.aviso
    assert clicks == 1


@pytest.mark.anyio
async def test_autos_gate_remains_armed_through_capture_and_cache_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, read = _service(tmp_path)
    plan = _plan()
    result = _search_result(
        numero=plan.numero,
        grau=plan.grau,
        processo_id=plan.processo_id,
    )
    ui = _ui(plan.numero, plan.grau)
    autos_page = cast(Page, object())

    @asynccontextmanager
    async def interactive_session(
        _lease: ReadSessionLease,
        _numero: str,
    ) -> AsyncGenerator[_UiSession]:
        yield ui

    async def click_exact_result(
        _ui: _UiSession,
        _result: _SearchResult,
        *,
        timeout_ms: int,
    ) -> Page:
        assert timeout_ms == 100
        ui.gate.autos_get_count = 1
        ui.gate.remaining_autos_gets = 0
        return autos_page

    async def validated_autos_html(
        page: Page,
        _numero: str,
        _grau: Grau,
        _processo_id: str,
    ) -> str:
        assert page is autos_page
        assert ui.gate.opening_target == result.autos_url
        return "<!doctype html><html><body>Autos</body></html>"

    original_register = read.registrar_autos_pesquisa_geral

    def register(
        lease: ReadSessionLease,
        numero: str,
        processo_id: str,
        autos_url: str,
        autos_html: str,
    ) -> None:
        assert ui.gate.opening_target == result.autos_url
        original_register(lease, numero, processo_id, autos_url, autos_html)

    monkeypatch.setattr(service, "_interactive_session", interactive_session)
    monkeypatch.setattr(service, "_search_exact", AsyncMock(return_value=result))
    monkeypatch.setattr(pje_search, "_click_exact_result", click_exact_result)
    monkeypatch.setattr(pje_search, "_validated_autos_html", validated_autos_html)
    monkeypatch.setattr(read, "registrar_autos_pesquisa_geral", register)

    await service._open_once(plan)

    assert read.registrations == [(plan.generation, plan.numero, plan.processo_id)]
    assert ui.gate.opening_target is None
    assert ui.gate.remaining_autos_gets == 0


@pytest.mark.anyio
async def test_orphan_opening_state_becomes_indeterminate_without_remote_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, sessions, _ = _service(tmp_path)
    plan = _seed_plan(service)
    plan.status = _PlanStatus.ABRINDO
    runner = AsyncMock()
    monkeypatch.setattr(service, "_run_opening", runner)

    first = await service.abrir(
        plan.numero,
        plan.grau,
        plan.reference,
        confirmation_phrase(plan.numero, plan.grau),
    )
    repeated = await service.abrir(
        plan.numero,
        plan.grau,
        plan.reference,
        confirmation_phrase(plan.numero, plan.grau),
    )

    assert first.estado is EstadoAcessoPesquisaGeral.INDETERMINADO
    assert first.reutilizada is True
    assert repeated.referencia_acesso == first.referencia_acesso
    assert repeated.estado is EstadoAcessoPesquisaGeral.INDETERMINADO
    assert repeated.reutilizada is True
    runner.assert_not_awaited()
    assert sessions.calls == []


@pytest.mark.anyio
async def test_ttl_blocks_first_open_but_not_idempotent_read_of_completed_tombstone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _ = _service(tmp_path)
    expired = _seed_plan(
        service,
        _plan(expires_at=datetime.now(UTC) - timedelta(seconds=1)),
    )
    opener = AsyncMock()
    monkeypatch.setattr(service, "_open_once", opener)
    phrase = confirmation_phrase(expired.numero, expired.grau)

    with pytest.raises(ValidacaoError, match="preparação expirou"):
        await service.abrir(expired.numero, expired.grau, expired.reference, phrase)
    opener.assert_not_awaited()

    expired.expires_at = datetime.now(UTC) + timedelta(minutes=1)
    first = await service.abrir(expired.numero, expired.grau, expired.reference, phrase)
    expired.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    repeated = await service.abrir(expired.numero, expired.grau, expired.reference, phrase)

    assert repeated.referencia_acesso == first.referencia_acesso
    assert repeated.reutilizada is True
    opener.assert_awaited_once_with(expired)


def test_registry_limit_must_be_positive(tmp_path: Path) -> None:
    sessions = cast(PjeSessionManager, _FakeSessions())
    read = cast(PjeReadService, _FakeRead())

    with pytest.raises(ValueError, match="registry_limit deve ser positivo"):
        PjeSearchService(sessions, read, Settings(data_dir=tmp_path), registry_limit=0)


def test_registry_pressure_preserves_access_tombstones_and_evicts_old_prepared(
    tmp_path: Path,
) -> None:
    service, _, _ = _service(tmp_path, registry_limit=2)
    tombstone = _seed_plan(service, _plan(reference="opened_preparation_ref_01"))
    binding = service._record_access_locked(tombstone, EstadoAcessoPesquisaGeral.INDETERMINADO)
    removable = _seed_plan(service, _plan(reference="removable_preparation_01"))
    newest = _seed_plan(service, _plan(reference="newest_preparation_ref_01"))

    service._trim_registries()

    assert tombstone.reference in service._plans
    assert binding.reference in service._accesses
    assert removable.reference not in service._plans
    assert newest.reference in service._plans
    assert service._fingerprints.get(removable.fingerprint) != removable.reference


@pytest.mark.anyio
async def test_unexpected_opening_failure_does_not_expose_url_capability_or_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _ = _service(tmp_path)
    plan = _seed_plan(service)
    leaked = (
        "https://pje.cloud.tjpe.jus.br/1g/Processo/ConsultaProcesso/Detalhe/"
        "listProcessoCompletoAdvogado.seam?id=123456&ca=SECRET_CAPABILITY"
    )
    monkeypatch.setattr(
        service,
        "_open_once",
        AsyncMock(side_effect=RuntimeError(f"{leaked} cookie=SECRET generation-a")),
    )

    with pytest.raises(ServicoIndisponivelError) as captured:
        await service.abrir(
            plan.numero,
            plan.grau,
            plan.reference,
            confirmation_phrase(plan.numero, plan.grau),
        )

    message = str(captured.value)
    assert message == "não foi possível concluir a abertura autenticada com segurança"
    assert "https://" not in message
    assert "SECRET_CAPABILITY" not in message
    assert "cookie" not in message
    assert "generation-a" not in message
    assert plan.status is _PlanStatus.PREPARADO
    assert plan.access_reference is None


def test_internal_repr_and_public_models_do_not_expose_remote_url_or_internal_ids(
    tmp_path: Path,
) -> None:
    service, _, _ = _service(tmp_path)
    result = _search_result(capability="SECRET_CAPABILITY")
    plan = _seed_plan(service, _plan(processo_id=result.processo_id))
    binding = service._record_access_locked(plan, EstadoAcessoPesquisaGeral.ABERTO)

    result_repr = repr(result)
    plan_repr = repr(plan)
    binding_repr = repr(binding)
    preparation_json = pje_search._preparation_model(plan).model_dump_json()
    access_json = pje_search._access_model(binding, reused=False).model_dump_json()

    for rendered in (result_repr, plan_repr, binding_repr, preparation_json, access_json):
        assert "SECRET_CAPABILITY" not in rendered
        assert "listProcessoCompletoAdvogado" not in rendered
        assert "https://" not in rendered
    assert "generation-a" not in plan_repr
    assert "generation-a" not in binding_repr
    assert result.processo_id not in result_repr
    assert plan.processo_id not in plan_repr
    assert binding.processo_id not in binding_repr
    assert isinstance(binding, _AccessBinding)
