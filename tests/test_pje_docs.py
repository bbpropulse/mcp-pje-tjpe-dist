from __future__ import annotations

# pyright: reportPrivateUsage=false
import asyncio
import hashlib
import os
import stat
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from playwright.async_api import BrowserContext, Locator, Page, Route, async_playwright

import mcp_pje_tjpe.pje_docs as pje_docs
from mcp_pje_tjpe.config import Settings
from mcp_pje_tjpe.errors import (
    CredenciaisAusentesError,
    InterfacePjeAlteradaError,
    ServicoIndisponivelError,
    ValidacaoError,
)
from mcp_pje_tjpe.models import (
    ArquivoPjeDocsBaixado,
    EstadoDownloadPjeDocs,
    EstadoSolicitacaoPjeDocs,
    Grau,
)
from mcp_pje_tjpe.pje_auth import PjeSessionManager, ReadSessionLease
from mcp_pje_tjpe.pje_docs import (
    CONFIRMATION_PHRASE,
    PjeDocsService,
    _AreaRow,
    _AreaSnapshot,
    _assert_no_warning,
    _assert_target_binding,
    _configure_integral_form,
    _control_post_marker,
    _correlated_rows,
    _extract_area_snapshot,
    _form_post_target,
    _integral_form,
    _is_same_degree_url,
    _NetworkGate,
    _Plan,
    _PlanStatus,
    _publish_streamed_file,
    _RequestBinding,
    _ResultBinding,
    _row_fingerprint,
    _stream_pjedocs_https,
    _SubmissionUncertain,
    _UiSession,
    _unique_control,
    _validate_integral_form,
    _validated_download_url,
    _validated_pjedocs_payload,
)
from mcp_pje_tjpe.pje_read import PjeReadService, PjeReadTarget

PREPARATION_REFERENCE = "preparation_reference_01"
REQUEST_REFERENCE = "request_reference_000001"
RESULT_REFERENCE = "result_reference_0000001"


def _synthetic_npu(
    sequence: str = "9999999",
    *,
    year: str = "2099",
    origin: str = "9999",
) -> str:
    base = sequence + year + "8" + "17" + origin + "00"
    check_digits = 98 - (int(base) % 97)
    return f"{sequence}-{check_digits:02d}.{year}.8.17.{origin}"


def _target(
    *,
    numero: str | None = None,
    grau: Grau = Grau.PRIMEIRO,
    generation: str = "generation-a",
    processo_id: str = "123456",
) -> PjeReadTarget:
    return PjeReadTarget(
        generation=generation,
        grau=grau,
        numero=numero or _synthetic_npu(),
        processo_id=processo_id,
        autos_url=(
            f"https://pje.cloud.tjpe.jus.br/{grau.value}/Processo/ConsultaProcesso/"
            f"Detalhe/listProcessoCompletoAdvogado.seam?id={processo_id}"
        ),
    )


def _plan(
    *,
    numero: str | None = None,
    grau: Grau = Grau.PRIMEIRO,
    generation: str = "generation-a",
    processo_id: str = "123456",
    reference: str = PREPARATION_REFERENCE,
) -> _Plan:
    now = datetime.now(UTC)
    return _Plan(
        reference=reference,
        generation=generation,
        grau=grau,
        numero=numero or _synthetic_npu(),
        processo_id=processo_id,
        fingerprint=f"fingerprint-{reference}",
        baseline=frozenset({"existing-row"}),
        created_at=now,
        expires_at=now + timedelta(minutes=5),
    )


def _request(
    *,
    numero: str | None = None,
    grau: Grau = Grau.PRIMEIRO,
    generation: str = "generation-a",
    processo_id: str = "123456",
    reference: str = REQUEST_REFERENCE,
    baseline: frozenset[str] = frozenset(),
) -> _RequestBinding:
    return _RequestBinding(
        reference=reference,
        plan_reference=PREPARATION_REFERENCE,
        generation=generation,
        grau=grau,
        numero=numero or _synthetic_npu(),
        processo_id=processo_id,
        baseline=baseline,
        state=EstadoSolicitacaoPjeDocs.SOLICITADO,
        requested_at=datetime.now(UTC),
    )


def _row(
    *,
    name: str | None = None,
    context: str | None = None,
    literal_href: str = "/1g/Download/resultado.seam?token=renovavel",
    absolute_href: str = (
        "https://pje.cloud.tjpe.jus.br/1g/Download/resultado.seam?token=renovavel"
    ),
    onclick: str = "",
    link_count: int = 1,
) -> _AreaRow:
    numero = _synthetic_npu()
    return _AreaRow(
        key="row-key-new",
        name=name or f"Integra do processo {numero}.pdf",
        expiration_text="02/09/2099 12:00",
        context=context or f"Integra do processo {numero}",
        expires_at=datetime(2099, 9, 2, 15, tzinfo=UTC),
        link_count=link_count,
        literal_href=literal_href,
        absolute_href=absolute_href,
        onclick=onclick,
    )


class _FakeSessions:
    def __init__(self, target: PjeReadTarget) -> None:
        self.target = target
        self.calls: list[Grau] = []
        self.lease = ReadSessionLease(
            grau=target.grau,
            context=cast(BrowserContext, object()),
            page=cast(Page, object()),
            generation=target.generation,
        )

    @asynccontextmanager
    async def read_session(self, grau: Grau) -> AsyncGenerator[ReadSessionLease]:
        self.calls.append(grau)
        yield self.lease


class _FakeRead:
    def __init__(self, target: PjeReadTarget) -> None:
        self.target = target
        self.calls: list[tuple[str, str]] = []

    async def preparar_alvo_pjedocs(
        self,
        lease: ReadSessionLease,
        numero: str,
    ) -> PjeReadTarget:
        self.calls.append((lease.generation, numero))
        return self.target


def _service(
    tmp_path: Path,
    *,
    target: PjeReadTarget | None = None,
    registry_limit: int = 500,
) -> tuple[PjeDocsService, _FakeSessions, _FakeRead]:
    bound_target = target or _target()
    sessions = _FakeSessions(bound_target)
    read = _FakeRead(bound_target)
    service = PjeDocsService(
        cast(PjeSessionManager, sessions),
        cast(PjeReadService, read),
        Settings(
            data_dir=tmp_path / "data",
            downloads_dir=tmp_path / "downloads",
            max_pjedocs_bytes=16 * 1024 * 1024,
        ),
        registry_limit=registry_limit,
    )
    return service, sessions, read


def _seed_plan(service: PjeDocsService, plan: _Plan | None = None) -> _Plan:
    selected = plan or _plan()
    service._plans[selected.reference] = selected
    service._fingerprints[selected.fingerprint] = selected.reference
    return selected


def _seed_request(
    service: PjeDocsService,
    request: _RequestBinding | None = None,
) -> _RequestBinding:
    selected = request or _request()
    service._requests[selected.reference] = selected
    return selected


@pytest.mark.anyio
async def test_preparar_only_inspects_and_returns_literal_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _ = _service(tmp_path)
    control = AsyncMock()
    ui = _UiSession(
        context=cast(BrowserContext, object()),
        page=cast(Page, object()),
        gate=_NetworkGate(Grau.PRIMEIRO),
    )

    @asynccontextmanager
    async def interactive_session(
        _lease: ReadSessionLease,
        _grau: Grau,
    ) -> AsyncGenerator[_UiSession]:
        yield ui

    monkeypatch.setattr(service, "_assert_autos_safe", AsyncMock())
    monkeypatch.setattr(service, "_interactive_session", interactive_session)
    monkeypatch.setattr(pje_docs, "_goto_audited", AsyncMock())
    monkeypatch.setattr(pje_docs, "_unique_control", AsyncMock(return_value=control))
    monkeypatch.setattr(
        pje_docs,
        "_open_area_download",
        AsyncMock(return_value=_AreaSnapshot(rows=(), partial=False)),
    )

    prepared = await service.preparar(_synthetic_npu(), Grau.PRIMEIRO)

    assert prepared.frase_confirmacao == CONFIRMATION_PHRASE
    assert prepared.modo == "integra"
    assert prepared.inclui_expedientes is False
    assert prepared.inclui_movimentos is False
    assert prepared.referencia_preparo in service._plans
    control.click.assert_not_awaited()
    assert ui.gate.allowed_post_target is None
    assert ui.gate.remaining_posts == 0


@pytest.mark.anyio
async def test_new_preparation_requires_observed_expiry_and_preserves_old_tombstone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _ = _service(tmp_path)
    numero = _synthetic_npu()
    ui = _UiSession(
        context=cast(BrowserContext, object()),
        page=cast(Page, object()),
        gate=_NetworkGate(Grau.PRIMEIRO),
    )
    expired_row = replace(
        _row(literal_href="", absolute_href="", link_count=0),
        expiration_text="31/08/2026 12:00",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    snapshots = iter(
        (
            _AreaSnapshot(rows=(), partial=False),
            _AreaSnapshot(rows=(expired_row,), partial=False),
            _AreaSnapshot(rows=(expired_row,), partial=False),
            _AreaSnapshot(rows=(expired_row,), partial=False),
        )
    )

    @asynccontextmanager
    async def interactive_session(
        _lease: ReadSessionLease,
        _grau: Grau,
    ) -> AsyncGenerator[_UiSession]:
        yield ui

    async def open_area_download(
        _ui: _UiSession,
        _config: Settings,
        _grau: Grau,
    ) -> _AreaSnapshot:
        return next(snapshots)

    submit_once = AsyncMock(return_value=frozenset())
    monkeypatch.setattr(service, "_assert_autos_safe", AsyncMock())
    monkeypatch.setattr(service, "_interactive_session", interactive_session)
    monkeypatch.setattr(service, "_submit_once", submit_once)
    monkeypatch.setattr(pje_docs, "_goto_audited", AsyncMock())
    monkeypatch.setattr(pje_docs, "_unique_control", AsyncMock())
    monkeypatch.setattr(pje_docs, "_open_area_download", open_area_download)

    original = await service.preparar(numero, Grau.PRIMEIRO)
    old_request = await service.solicitar(
        numero,
        Grau.PRIMEIRO,
        original.referencia_preparo,
        CONFIRMATION_PHRASE,
    )

    before_observation = await service.preparar(numero, Grau.PRIMEIRO)
    assert before_observation.referencia_preparo == original.referencia_preparo

    expired = await service.listar(
        numero,
        Grau.PRIMEIRO,
        old_request.referencia_solicitacao,
    )
    assert len(expired.downloads) == 1
    assert expired.downloads[0].estado is EstadoDownloadPjeDocs.EXPIRADO
    assert expired.downloads[0].referencia_resultado is None

    renewed = await service.preparar(numero, Grau.PRIMEIRO)
    assert renewed.referencia_preparo != original.referencia_preparo
    assert renewed.frase_confirmacao == CONFIRMATION_PHRASE

    with pytest.raises(ValidacaoError, match="confirmação incorreta"):
        await service.solicitar(
            numero,
            Grau.PRIMEIRO,
            renewed.referencia_preparo,
            "confirmo novamente",
        )
    submit_once.assert_awaited_once()

    repeated_old = await service.solicitar(
        numero,
        Grau.PRIMEIRO,
        original.referencia_preparo,
        CONFIRMATION_PHRASE,
    )
    assert repeated_old.referencia_solicitacao == old_request.referencia_solicitacao
    assert repeated_old.reutilizada is True
    submit_once.assert_awaited_once()

    new_request = await service.solicitar(
        numero,
        Grau.PRIMEIRO,
        renewed.referencia_preparo,
        CONFIRMATION_PHRASE,
    )
    assert new_request.referencia_solicitacao != old_request.referencia_solicitacao
    assert new_request.reutilizada is False
    assert submit_once.await_count == 2
    assert original.referencia_preparo in service._plans
    assert old_request.referencia_solicitacao in service._requests
    assert old_request.referencia_solicitacao in service._expired_requests


@pytest.mark.anyio
async def test_incorrect_confirmation_never_starts_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, sessions, _ = _service(tmp_path)
    plan = _seed_plan(service)
    submit = AsyncMock()
    monkeypatch.setattr(service, "_run_submission", submit)

    with pytest.raises(ValidacaoError, match="confirmação incorreta"):
        await service.solicitar(plan.numero, plan.grau, plan.reference, "confirmo")

    submit.assert_not_awaited()
    assert sessions.calls == []
    assert service._submission_tasks == {}
    assert plan.request_reference is None


@pytest.mark.anyio
async def test_public_decorator_never_exposes_capability_url_from_unexpected_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _ = _service(tmp_path)
    leaked = "https://pje.cloud.tjpe.jus.br/1g/download?ca=SECRET_CAPABILITY"
    monkeypatch.setattr(
        service,
        "_assert_autos_safe",
        AsyncMock(side_effect=RuntimeError(f"falha interna em {leaked}")),
    )

    with pytest.raises(ServicoIndisponivelError) as captured:
        await service.preparar(_synthetic_npu(), Grau.PRIMEIRO)

    message = str(captured.value)
    assert message == "não foi possível inspecionar com segurança o PJeDocs"
    assert "SECRET_CAPABILITY" not in message
    assert "https://" not in message


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("numero", "grau"),
    [
        (_synthetic_npu("9999998"), Grau.PRIMEIRO),
        (_synthetic_npu(), Grau.SEGUNDO),
    ],
)
async def test_preparation_reference_is_bound_to_npu_and_degree(
    tmp_path: Path,
    numero: str,
    grau: Grau,
) -> None:
    service, sessions, _ = _service(tmp_path)
    plan = _seed_plan(service)

    with pytest.raises(ValidacaoError, match="outro grau ou processo"):
        await service.solicitar(numero, grau, plan.reference, CONFIRMATION_PHRASE)

    assert sessions.calls == []
    assert service._submission_tasks == {}


@pytest.mark.parametrize(
    ("changed", "expected"),
    [
        ({"generation": "generation-b"}, "outra sessão"),
        ({"grau": Grau.SEGUNDO}, "outra sessão"),
        ({"numero": _synthetic_npu("9999998")}, "outra sessão"),
        ({"processo_id": "654321"}, "outra sessão"),
    ],
)
def test_internal_binding_rejects_every_cross_session_dimension(
    changed: dict[str, object],
    expected: str,
) -> None:
    plan = _plan()
    values: dict[str, object] = {
        "numero": plan.numero,
        "grau": plan.grau,
        "generation": plan.generation,
        "processo_id": plan.processo_id,
    }
    values.update(changed)
    target = _target(
        numero=cast(str, values["numero"]),
        grau=cast(Grau, values["grau"]),
        generation=cast(str, values["generation"]),
        processo_id=cast(str, values["processo_id"]),
    )

    with pytest.raises(ValidacaoError, match=expected):
        _assert_target_binding(target, plan)


@pytest.mark.anyio
async def test_concurrent_submissions_share_one_remote_attempt_and_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _ = _service(tmp_path)
    plan = _seed_plan(service)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def submit_once(_plan: _Plan) -> frozenset[str]:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return frozenset({"baseline"})

    monkeypatch.setattr(service, "_submit_once", submit_once)
    first = asyncio.ensure_future(
        service.solicitar(plan.numero, plan.grau, plan.reference, CONFIRMATION_PHRASE)
    )
    await started.wait()
    second = asyncio.ensure_future(
        service.solicitar(plan.numero, plan.grau, plan.reference, CONFIRMATION_PHRASE)
    )
    await asyncio.sleep(0)
    release.set()
    one, two = await asyncio.gather(first, second)

    assert calls == 1
    assert one.referencia_solicitacao == two.referencia_solicitacao
    assert {one.reutilizada, two.reutilizada} == {False, True}

    repeated = await service.solicitar(
        plan.numero,
        plan.grau,
        plan.reference,
        CONFIRMATION_PHRASE,
    )
    assert repeated.referencia_solicitacao == one.referencia_solicitacao
    assert repeated.reutilizada is True
    assert calls == 1


@pytest.mark.anyio
async def test_cancelling_one_waiter_does_not_cancel_shared_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _ = _service(tmp_path)
    plan = _seed_plan(service)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def submit_once(_plan: _Plan) -> frozenset[str]:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return frozenset()

    monkeypatch.setattr(service, "_submit_once", submit_once)
    cancelled_waiter = asyncio.ensure_future(
        service.solicitar(plan.numero, plan.grau, plan.reference, CONFIRMATION_PHRASE)
    )
    await started.wait()
    surviving_waiter = asyncio.ensure_future(
        service.solicitar(plan.numero, plan.grau, plan.reference, CONFIRMATION_PHRASE)
    )
    await asyncio.sleep(0)

    cancelled_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter
    release.set()
    result = await surviving_waiter

    assert calls == 1
    assert result.estado is EstadoSolicitacaoPjeDocs.SOLICITADO
    assert result.reutilizada is True
    assert plan.request_reference == result.referencia_solicitacao


@pytest.mark.anyio
async def test_uncertain_submission_is_recorded_and_never_retried_automatically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _ = _service(tmp_path)
    plan = _seed_plan(service)
    submit = AsyncMock(side_effect=_SubmissionUncertain(frozenset({"baseline-at-click"})))
    monkeypatch.setattr(service, "_submit_once", submit)

    first = await service.solicitar(
        plan.numero,
        plan.grau,
        plan.reference,
        CONFIRMATION_PHRASE,
    )
    repeated = await service.solicitar(
        plan.numero,
        plan.grau,
        plan.reference,
        CONFIRMATION_PHRASE,
    )

    assert first.estado is EstadoSolicitacaoPjeDocs.INDETERMINADO
    assert first.reutilizada is False
    assert repeated.estado is EstadoSolicitacaoPjeDocs.INDETERMINADO
    assert repeated.reutilizada is True
    assert repeated.referencia_solicitacao == first.referencia_solicitacao
    assert "não reenviará" in first.aviso
    submit.assert_awaited_once()


@pytest.mark.anyio
async def test_orphan_submitting_plan_becomes_indeterminate_without_resubmission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, sessions, _ = _service(tmp_path)
    plan = _seed_plan(service)
    plan.status = _PlanStatus.SUBMETENDO
    runner = AsyncMock()
    monkeypatch.setattr(service, "_run_submission", runner)

    first = await service.solicitar(
        plan.numero,
        plan.grau,
        plan.reference,
        CONFIRMATION_PHRASE,
    )
    repeated = await service.solicitar(
        plan.numero,
        plan.grau,
        plan.reference,
        CONFIRMATION_PHRASE,
    )

    assert first.estado is EstadoSolicitacaoPjeDocs.INDETERMINADO
    assert first.reutilizada is True
    assert repeated.referencia_solicitacao == first.referencia_solicitacao
    assert repeated.estado is EstadoSolicitacaoPjeDocs.INDETERMINADO
    assert repeated.reutilizada is True
    runner.assert_not_awaited()
    assert sessions.calls == []


@pytest.mark.anyio
async def test_direct_internal_cancellation_after_submit_keeps_request_tombstone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _ = _service(tmp_path)
    plan = _seed_plan(service)
    plan.status = _PlanStatus.SUBMETENDO
    started = asyncio.Event()
    release_submission = asyncio.Event()

    async def submit_once(_plan: _Plan) -> frozenset[str]:
        started.set()
        await release_submission.wait()
        return frozenset({"baseline-after-click"})

    monkeypatch.setattr(service, "_submit_once", submit_once)
    task = asyncio.create_task(service._run_submission(plan.reference))
    service._submission_tasks[plan.reference] = task
    await started.wait()
    await service._state_lock.acquire()
    try:
        release_submission.set()
        await asyncio.sleep(0)
        task.cancel()
    finally:
        service._state_lock.release()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert plan.request_reference is not None
    request = service._requests[plan.request_reference]
    assert request.state is EstadoSolicitacaoPjeDocs.SOLICITADO
    assert request.baseline == frozenset({"baseline-after-click"})
    assert plan.status is _PlanStatus.SOLICITADO
    assert service._submission_tasks == {}


@pytest.mark.anyio
async def test_registry_trim_preserves_mutating_requested_and_downloading_entries(
    tmp_path: Path,
) -> None:
    service, _, _ = _service(tmp_path, registry_limit=1)
    protected = [
        _plan(reference="submitting_reference_01"),
        _plan(reference="requested_reference_001"),
        _plan(reference="indeterminate_reference_1"),
    ]
    protected[0].status = _PlanStatus.SUBMETENDO
    protected[1].status = _PlanStatus.SOLICITADO
    protected[2].status = _PlanStatus.INDETERMINADO
    removable = _plan(reference="removable_prepared_001")
    newest = _plan(reference="newest_prepared_000001")
    for plan in (*protected, removable, newest):
        service._plans[plan.reference] = plan
        service._fingerprints[plan.fingerprint] = plan.reference

    service._trim_registries()

    assert removable.reference not in service._plans
    assert newest.reference in service._plans
    assert all(plan.reference in service._plans for plan in protected)

    original = _row()

    def result(reference: str, row_key: str) -> _ResultBinding:
        return _ResultBinding(
            reference=reference,
            request_reference=REQUEST_REFERENCE,
            generation="generation-a",
            grau=Grau.PRIMEIRO,
            numero=_synthetic_npu(),
            processo_id="123456",
            row_key=row_key,
            row_fingerprint=_row_fingerprint(original),
            remote_name=original.name,
            expires_at=original.expires_at,
        )

    active = result("active_result_reference_01", "active-row")
    disposable = result("disposable_result_ref_01", "disposable-row")
    service._results[active.reference] = active
    service._results[disposable.reference] = disposable
    release_download = asyncio.Event()

    async def hold_download() -> ArquivoPjeDocsBaixado:
        await release_download.wait()
        raise AssertionError("download sintético não deveria concluir")

    download_task = asyncio.create_task(hold_download())
    service._download_tasks[active.reference] = download_task
    service._trim_registries()

    assert active.reference in service._results
    assert disposable.reference not in service._results
    download_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await download_task


class _FakePage:
    url = "https://pje.cloud.tjpe.jus.br/1g/Processo/ConsultaProcesso/Detalhe/autos.seam"

    async def wait_for_timeout(self, _milliseconds: float) -> None:
        return None


class _ClickControl:
    def __init__(
        self,
        error: BaseException | None = None,
        *,
        gate: _NetworkGate | None = None,
        post_delta: int = 0,
    ) -> None:
        self.error = error
        self.gate = gate
        self.post_delta = post_delta
        self.clicks = 0

    async def click(self) -> None:
        self.clicks += 1
        if self.gate is not None:
            self.gate.post_count += self.post_delta
        if self.error is not None:
            raise self.error


def _audited_fields() -> dict[str, str]:
    return {
        "tipo": "",
        "inicio": "",
        "fim": "",
        "fim-periodo": "",
        "periodo": "",
        "cronologia": "asc",
        "expediente": "nao",
        "movimentos": "nao",
    }


def _install_submission_fakes(
    service: PjeDocsService,
    plan: _Plan,
    monkeypatch: pytest.MonkeyPatch,
    *,
    open_error: BaseException | None = None,
    submit_error: BaseException | None = None,
    post_delta: int = 1,
    stale_success: bool = False,
    dialog_after_goto: bool = False,
    expire_stage: str | None = None,
) -> tuple[_UiSession, _ClickControl, _ClickControl, AsyncMock]:
    page = cast(Page, _FakePage())
    ui = _UiSession(
        context=cast(BrowserContext, object()),
        page=page,
        gate=_NetworkGate(plan.grau),
    )
    open_control = _ClickControl(open_error)
    submit_control = _ClickControl(
        submit_error,
        gate=ui.gate,
        post_delta=post_delta,
    )
    cancel_control = _ClickControl()

    @asynccontextmanager
    async def interactive_session(
        _lease: ReadSessionLease,
        _grau: Grau,
    ) -> AsyncGenerator[_UiSession]:
        yield ui

    async def unique_control(_root: Page | Locator, label: str) -> object:
        if label == "Download autos do processo":
            return open_control
        if label == "DOWNLOAD":
            return submit_control
        if label == "CANCELAR":
            return cancel_control
        raise AssertionError(label)

    async def click_and_resolve_page(
        _context: BrowserContext,
        _current: Page,
        control: object,
        _grau: Grau,
        *,
        wait_ms: int,
    ) -> Page:
        assert wait_ms == 350
        assert control is open_control
        await open_control.click()
        return page

    async def goto_audited(*_args: object, **_kwargs: object) -> None:
        if dialog_after_goto:
            ui.dialogs.append("Acesso será registrado")

    warning_calls = 0

    async def assert_no_warning(_page: Page) -> None:
        nonlocal warning_calls
        warning_calls += 1
        if expire_stage == "before_opener" and warning_calls == 1:
            plan.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    async def success_in_body(_page: Page) -> bool:
        if expire_stage == "before_submit":
            plan.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        return stale_success

    resolver = AsyncMock(side_effect=click_and_resolve_page)
    monkeypatch.setattr(service, "_assert_autos_safe", AsyncMock())
    monkeypatch.setattr(service, "_interactive_session", interactive_session)
    monkeypatch.setattr(
        pje_docs,
        "_open_area_download",
        AsyncMock(return_value=_AreaSnapshot(rows=(), partial=False)),
    )
    monkeypatch.setattr(pje_docs, "_goto_audited", goto_audited)
    monkeypatch.setattr(pje_docs, "_assert_no_warning", assert_no_warning)
    monkeypatch.setattr(pje_docs, "_control_post_permission", AsyncMock(return_value=None))
    monkeypatch.setattr(pje_docs, "_click_and_resolve_page", resolver)
    monkeypatch.setattr(pje_docs, "_integral_form", AsyncMock(return_value=cast(Locator, object())))
    monkeypatch.setattr(pje_docs, "_configure_integral_form", AsyncMock())
    monkeypatch.setattr(
        pje_docs,
        "_validate_integral_form",
        AsyncMock(return_value=_audited_fields()),
    )
    monkeypatch.setattr(pje_docs, "_success_in_body", success_in_body)
    monkeypatch.setattr(
        pje_docs,
        "_form_post_target",
        AsyncMock(return_value="https://pje.cloud.tjpe.jus.br/1g/form"),
    )
    monkeypatch.setattr(
        pje_docs,
        "_control_post_marker",
        AsyncMock(return_value="downloadButton"),
    )
    monkeypatch.setattr(pje_docs, "_unique_control", unique_control)
    monkeypatch.setattr(pje_docs, "_submission_succeeded", AsyncMock(return_value=True))
    return ui, open_control, submit_control, resolver


@pytest.mark.anyio
async def test_opener_failure_after_click_is_tombstoned_and_never_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _ = _service(tmp_path)
    plan = _seed_plan(service)
    _, opener, submit, _ = _install_submission_fakes(
        service,
        plan,
        monkeypatch,
        open_error=InterfacePjeAlteradaError("opener falhou depois do clique"),
    )

    first = await service.solicitar(
        plan.numero,
        plan.grau,
        plan.reference,
        CONFIRMATION_PHRASE,
    )
    repeated = await service.solicitar(
        plan.numero,
        plan.grau,
        plan.reference,
        CONFIRMATION_PHRASE,
    )

    assert first.estado is EstadoSolicitacaoPjeDocs.INDETERMINADO
    assert repeated.referencia_solicitacao == first.referencia_solicitacao
    assert repeated.reutilizada is True
    assert opener.clicks == 1
    assert submit.clicks == 0


@pytest.mark.anyio
async def test_dialog_raised_during_autos_navigation_blocks_before_opener(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _ = _service(tmp_path)
    plan = _plan()
    ui, opener, submit, resolver = _install_submission_fakes(
        service,
        plan,
        monkeypatch,
        dialog_after_goto=True,
    )

    with pytest.raises(InterfacePjeAlteradaError, match="antes de abrir"):
        await service._submit_once(plan)

    assert ui.dialogs == ["Acesso será registrado"]
    assert opener.clicks == 0
    assert submit.clicks == 0
    resolver.assert_not_awaited()


@pytest.mark.anyio
async def test_stale_success_message_blocks_final_submit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _ = _service(tmp_path)
    plan = _plan()
    _, opener, submit, _ = _install_submission_fakes(
        service,
        plan,
        monkeypatch,
        stale_success=True,
    )

    with pytest.raises(_SubmissionUncertain):
        await service._submit_once(plan)

    assert opener.clicks == 1
    assert submit.clicks == 0


@pytest.mark.anyio
@pytest.mark.parametrize("post_delta", [0, 2])
async def test_submit_without_exactly_one_audited_post_is_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    post_delta: int,
) -> None:
    service, _, _ = _service(tmp_path)
    plan = _plan()
    ui, _, submit, _ = _install_submission_fakes(
        service,
        plan,
        monkeypatch,
        post_delta=post_delta,
    )

    with pytest.raises(_SubmissionUncertain):
        await service._submit_once(plan)

    assert submit.clicks == 1
    assert ui.gate.post_count == post_delta


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("expire_stage", "error"),
    [
        ("before_opener", ValidacaoError),
        ("before_submit", _SubmissionUncertain),
    ],
)
async def test_plan_ttl_is_revalidated_immediately_before_mutating_clicks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    expire_stage: str,
    error: type[BaseException],
) -> None:
    service, _, _ = _service(tmp_path)
    plan = _plan()
    _, opener, submit, _ = _install_submission_fakes(
        service,
        plan,
        monkeypatch,
        expire_stage=expire_stage,
    )

    with pytest.raises(error):
        await service._submit_once(plan)

    assert submit.clicks == 0
    assert opener.clicks == (0 if expire_stage == "before_opener" else 1)


@pytest.mark.anyio
async def test_plan_expiring_inside_open_permission_never_clicks_opener(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _ = _service(tmp_path)
    plan = _plan()
    _, opener, submit, resolver = _install_submission_fakes(service, plan, monkeypatch)

    async def expire_inside_permission(
        _control: Locator,
        _page: Page,
        _grau: Grau,
    ) -> None:
        plan.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    monkeypatch.setattr(pje_docs, "_control_post_permission", expire_inside_permission)

    with pytest.raises(ValidacaoError, match="expirou antes da janela mutável"):
        await service._submit_once(plan)

    assert opener.clicks == 0
    assert submit.clicks == 0
    resolver.assert_not_awaited()


@pytest.mark.anyio
async def test_plan_expiring_inside_final_form_validation_never_clicks_submit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _ = _service(tmp_path)
    plan = _plan()
    ui, opener, submit, _ = _install_submission_fakes(service, plan, monkeypatch)

    async def expire_inside_validation(
        _form: Locator,
        *,
        require_no: bool,
    ) -> dict[str, str]:
        assert require_no is True
        plan.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        return _audited_fields()

    monkeypatch.setattr(pje_docs, "_validate_integral_form", expire_inside_validation)

    with pytest.raises(_SubmissionUncertain):
        await service._submit_once(plan)

    assert opener.clicks == 1
    assert submit.clicks == 0
    assert ui.gate.post_count == 0
    assert ui.gate.allowed_post_target is None


@pytest.mark.anyio
async def test_failure_after_submit_click_becomes_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _ = _service(tmp_path)
    plan = _plan()
    page = cast(Page, _FakePage())
    ui = _UiSession(
        context=cast(BrowserContext, object()),
        page=page,
        gate=_NetworkGate(plan.grau),
    )
    open_control = _ClickControl()
    submit_control = _ClickControl(
        InterfacePjeAlteradaError("falha apó clique"),
        gate=ui.gate,
        post_delta=1,
    )
    cancel_control = _ClickControl()

    @asynccontextmanager
    async def interactive_session(
        _lease: ReadSessionLease,
        _grau: Grau,
    ) -> AsyncGenerator[_UiSession]:
        yield ui

    async def unique_control(_root: Page | Locator, label: str) -> object:
        if label == "Download autos do processo":
            return open_control
        if label == "DOWNLOAD":
            return submit_control
        if label == "CANCELAR":
            return cancel_control
        raise AssertionError(label)

    async def click_and_resolve_page(
        _context: BrowserContext,
        _current: Page,
        control: object,
        _grau: Grau,
        *,
        wait_ms: int,
    ) -> Page:
        assert wait_ms == 350
        assert control is open_control
        await open_control.click()
        return page

    monkeypatch.setattr(service, "_assert_autos_safe", AsyncMock())
    monkeypatch.setattr(service, "_interactive_session", interactive_session)
    monkeypatch.setattr(
        pje_docs,
        "_open_area_download",
        AsyncMock(return_value=_AreaSnapshot(rows=(), partial=False)),
    )
    monkeypatch.setattr(pje_docs, "_goto_audited", AsyncMock())
    monkeypatch.setattr(pje_docs, "_assert_no_warning", AsyncMock())
    monkeypatch.setattr(pje_docs, "_control_post_permission", AsyncMock(return_value=None))
    monkeypatch.setattr(pje_docs, "_click_and_resolve_page", click_and_resolve_page)
    monkeypatch.setattr(pje_docs, "_integral_form", AsyncMock(return_value=cast(Locator, object())))
    monkeypatch.setattr(pje_docs, "_configure_integral_form", AsyncMock())
    monkeypatch.setattr(
        pje_docs,
        "_validate_integral_form",
        AsyncMock(return_value=_audited_fields()),
        raising=False,
    )
    monkeypatch.setattr(
        pje_docs,
        "_success_in_body",
        AsyncMock(return_value=False),
        raising=False,
    )
    monkeypatch.setattr(
        pje_docs,
        "_form_post_target",
        AsyncMock(return_value="https://pje.cloud.tjpe.jus.br/1g/form"),
    )
    monkeypatch.setattr(
        pje_docs,
        "_control_post_marker",
        AsyncMock(return_value="downloadButton"),
    )
    monkeypatch.setattr(pje_docs, "_unique_control", unique_control)

    with pytest.raises(_SubmissionUncertain):
        await service._submit_once(plan)

    assert open_control.clicks == 1
    assert submit_control.clicks == 1
    assert cancel_control.clicks == 0


@pytest.mark.anyio
async def test_unexpected_dialog_blocks_before_any_submission_click(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _ = _service(tmp_path)
    plan = _plan()
    ui = _UiSession(
        context=cast(BrowserContext, object()),
        page=cast(Page, _FakePage()),
        gate=_NetworkGate(plan.grau),
        dialogs=["Acesso será registrado"],
    )
    controls = AsyncMock()

    @asynccontextmanager
    async def interactive_session(
        _lease: ReadSessionLease,
        _grau: Grau,
    ) -> AsyncGenerator[_UiSession]:
        yield ui

    monkeypatch.setattr(service, "_assert_autos_safe", AsyncMock())
    monkeypatch.setattr(service, "_interactive_session", interactive_session)
    monkeypatch.setattr(
        pje_docs,
        "_open_area_download",
        AsyncMock(return_value=_AreaSnapshot(rows=(), partial=False)),
    )
    monkeypatch.setattr(pje_docs, "_unique_control", controls)

    with pytest.raises(InterfacePjeAlteradaError, match="antes da solicitação"):
        await service._submit_once(plan)

    controls.assert_not_awaited()
    assert ui.gate.post_count == 0
    assert ui.gate.allowed_post_target is None


class _FakeRouteRequest:
    def __init__(self, method: str, url: str, post_data: str | None) -> None:
        self.method = method
        self.url = url
        self.post_data = post_data


class _FakeRoute:
    def __init__(self, method: str, url: str, post_data: str | None = None) -> None:
        self.request = _FakeRouteRequest(method, url, post_data)
        self.continued = 0
        self.aborted: list[str] = []

    async def continue_(self) -> None:
        self.continued += 1

    async def abort(self, error_code: str) -> None:
        self.aborted.append(error_code)


@pytest.mark.anyio
async def test_network_gate_allows_one_post_only_to_exact_form_action() -> None:
    action = "https://pje.cloud.tjpe.jus.br/1g/form.seam?conversation=abc"
    gate = _NetworkGate(Grau.PRIMEIRO)
    gate.permit_single_post(
        action,
        "downloadButton",
        {
            "javax.faces.ViewState": "abc",
            "inicio": "",
            "expediente": "nao",
            "movimentos": "nao",
        },
    )
    exact = _FakeRoute(
        "POST",
        action,
        "javax.faces.ViewState=abc&downloadButton=x&inicio=&expediente=nao&movimentos=nao",
    )

    await gate.handle(cast(Route, exact))

    assert exact.continued == 1
    assert exact.aborted == []
    assert gate.post_count == 1
    assert gate.remaining_posts == 0

    replay = _FakeRoute("POST", action, "downloadButton=x")
    await gate.handle(cast(Route, replay))
    assert replay.continued == 0
    assert replay.aborted == ["blockedbyclient"]
    assert gate.blocked_mutation is True

    other_gate = _NetworkGate(Grau.PRIMEIRO)
    other_gate.permit_single_post(action, "downloadButton")
    near_match = _FakeRoute(
        "POST",
        "https://pje.cloud.tjpe.jus.br/1g/form.seam?conversation=other",
        "downloadButton=x",
    )
    await other_gate.handle(cast(Route, near_match))
    assert near_match.continued == 0
    assert near_match.aborted == ["blockedbyclient"]
    assert other_gate.remaining_posts == 1
    assert other_gate.blocked_mutation is True

    marker_gate = _NetworkGate(Grau.PRIMEIRO)
    marker_gate.permit_single_post(action, "downloadButton")
    missing_marker = _FakeRoute("POST", action, "javax.faces.ViewState=abc&other=x")
    await marker_gate.handle(cast(Route, missing_marker))
    assert missing_marker.continued == 0
    assert missing_marker.aborted == ["blockedbyclient"]
    assert marker_gate.remaining_posts == 1
    assert marker_gate.blocked_mutation is True

    value_only_gate = _NetworkGate(Grau.PRIMEIRO)
    value_only_gate.permit_single_post(action, "downloadButton")
    value_only = _FakeRoute("POST", action, "other=downloadButton")
    await value_only_gate.handle(cast(Route, value_only))
    assert value_only.continued == 0
    assert value_only.aborted == ["blockedbyclient"]
    assert value_only_gate.blocked_mutation is True

    field_gate = _NetworkGate(Grau.PRIMEIRO)
    field_gate.permit_single_post(action, "downloadButton", {"expediente": "nao"})
    changed_field = _FakeRoute("POST", action, "downloadButton=x&expediente=sim")
    await field_gate.handle(cast(Route, changed_field))
    assert changed_field.continued == 0
    assert changed_field.aborted == ["blockedbyclient"]
    assert field_gate.remaining_posts == 1
    assert field_gate.blocked_mutation is True

    duplicate_gate = _NetworkGate(Grau.PRIMEIRO)
    duplicate_gate.permit_single_post(action, "downloadButton", {"expediente": "nao"})
    duplicate_field = _FakeRoute(
        "POST",
        action,
        "downloadButton=x&expediente=nao&expediente=nao",
    )
    await duplicate_gate.handle(cast(Route, duplicate_field))
    assert duplicate_field.continued == 0
    assert duplicate_field.aborted == ["blockedbyclient"]

    empty_gate = _NetworkGate(Grau.PRIMEIRO)
    empty_gate.permit_single_post(action, "downloadButton", {"inicio": ""})
    missing_empty_field = _FakeRoute("POST", action, "downloadButton=x")
    await empty_gate.handle(cast(Route, missing_empty_field))
    assert missing_empty_field.continued == 0
    assert missing_empty_field.aborted == ["blockedbyclient"]

    dialog_gate = _NetworkGate(Grau.PRIMEIRO)
    dialog_gate.permit_single_post(action, "downloadButton")
    dialog_gate.dialog_epoch += 1
    after_dialog = _FakeRoute("POST", action, "downloadButton=x")
    await dialog_gate.handle(cast(Route, after_dialog))
    assert after_dialog.continued == 0
    assert after_dialog.aborted == ["blockedbyclient"]

    collision_gate = _NetworkGate(Grau.PRIMEIRO)
    with pytest.raises(InterfacePjeAlteradaError, match="colide"):
        collision_gate.permit_single_post(
            action,
            "expediente",
            {"expediente": "nao"},
        )


@pytest.mark.anyio
async def test_network_gate_strict_contract_rejects_unexpected_extra_field() -> None:
    action = "https://pje.cloud.tjpe.jus.br/1g/form.seam?conversation=abc"
    expected = {
        "tipo": "",
        "inicio": "",
        "fim": "",
        "fim-periodo": "",
        "periodo": "",
        "cronologia": "asc",
        "expediente": "nao",
        "movimentos": "nao",
    }
    gate = _NetworkGate(Grau.PRIMEIRO)
    gate.permit_single_post(action, "downloadButton", expected)
    route = _FakeRoute(
        "POST",
        action,
        (
            "downloadButton=x&tipo=&inicio=&fim=&fim-periodo=&periodo=&cronologia=asc&"
            "expediente=nao&movimentos=nao&acaoExtra=gerar"
        ),
    )

    await gate.handle(cast(Route, route))

    assert route.continued == 0
    assert route.aborted == ["blockedbyclient"]
    assert gate.post_count == 0
    assert gate.remaining_posts == 1
    assert gate.blocked_mutation is True


def test_row_correlation_never_concatenates_unrelated_digit_fragments() -> None:
    request = _request()
    digits = "".join(character for character in request.numero if character.isdigit())
    fragmented = replace(
        _row(),
        key="fragmented-row",
        name=f"arquivo {digits[:10]}",
        context=f"lote {digits[10:]}",
    )
    valid = replace(_row(), key="valid-row")

    assert _correlated_rows(_AreaSnapshot(rows=(fragmented,), partial=False), request) == []
    assert _correlated_rows(_AreaSnapshot(rows=(valid,), partial=False), request) == [valid]


def test_row_correlation_rejects_expected_npu_mixed_with_second_valid_npu() -> None:
    request = _request()
    other = _synthetic_npu("9999998")
    mixed = replace(
        _row(),
        name=f"Íntegra do processo {request.numero}.pdf",
        context=f"Resultado relacionado também ao processo {other}",
    )

    assert _correlated_rows(_AreaSnapshot(rows=(mixed,), partial=False), request) == []


@pytest.mark.anyio
@pytest.mark.parametrize("mutation", ["missing_npu", "changed_fingerprint"])
async def test_download_revalidates_row_npu_and_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    service, _, _ = _service(tmp_path)
    original = _row()
    result = _ResultBinding(
        reference=RESULT_REFERENCE,
        request_reference=REQUEST_REFERENCE,
        generation="generation-a",
        grau=Grau.PRIMEIRO,
        numero=_synthetic_npu(),
        processo_id="123456",
        row_key=original.key,
        row_fingerprint=_row_fingerprint(original),
        remote_name=original.name,
        expires_at=original.expires_at,
    )
    service._results[result.reference] = result
    if mutation == "missing_npu":
        changed = replace(original, name="integra.pdf", context="linha sem processo")
    else:
        changed = replace(original, context=f"{original.context} conteúdo alterado")
    ui = _UiSession(
        context=cast(BrowserContext, object()),
        page=cast(Page, _FakePage()),
        gate=_NetworkGate(Grau.PRIMEIRO),
    )

    @asynccontextmanager
    async def interactive_session(
        _lease: ReadSessionLease,
        _grau: Grau,
    ) -> AsyncGenerator[_UiSession]:
        yield ui

    download = Mock()
    monkeypatch.setattr(service, "_interactive_session", interactive_session)
    monkeypatch.setattr(service, "_download_to_bundle", download)
    monkeypatch.setattr(
        pje_docs,
        "_open_area_download",
        AsyncMock(return_value=_AreaSnapshot(rows=(changed,), partial=False)),
    )

    with pytest.raises(ValidacaoError, match="não corresponde mais"):
        await service.baixar(result.numero, result.grau, result.reference)

    download.assert_not_called()
    assert service._download_tasks == {}


@pytest.mark.anyio
async def test_ready_result_is_reconciled_once_without_exposing_renewable_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _ = _service(tmp_path)
    request = _seed_request(service)
    renewable_url = (
        "https://pje.cloud.tjpe.jus.br/1g/Download/resultado.seam?token=segredo-renovavel"
    )
    row = _row(
        name=f"Integra {_synthetic_npu()} CPF 111.111.111-11.pdf",
        literal_href="/1g/Download/resultado.seam?token=segredo-renovavel",
        absolute_href=renewable_url,
    )
    ui = _UiSession(
        context=cast(BrowserContext, object()),
        page=cast(Page, object()),
        gate=_NetworkGate(Grau.PRIMEIRO),
    )

    @asynccontextmanager
    async def interactive_session(
        _lease: ReadSessionLease,
        _grau: Grau,
    ) -> AsyncGenerator[_UiSession]:
        yield ui

    monkeypatch.setattr(service, "_interactive_session", interactive_session)
    monkeypatch.setattr(
        pje_docs,
        "_open_area_download",
        AsyncMock(return_value=_AreaSnapshot(rows=(row,), partial=False)),
    )

    first = await service.listar(request.numero, request.grau, request.reference)
    second = await service.listar(request.numero, request.grau, request.reference)

    assert first.parcial is False
    assert len(first.downloads) == 1
    item = first.downloads[0]
    assert item.estado is EstadoDownloadPjeDocs.PRONTO
    assert item.referencia_resultado is not None
    assert item.referencia_resultado == second.downloads[0].referencia_resultado
    assert "111.111.111-11" not in item.nome
    assert "segredo-renovavel" not in first.model_dump_json()
    assert renewable_url not in first.model_dump_json()
    assert len(service._results) == 1
    binding = service._results[item.referencia_resultado]
    assert not hasattr(binding, "literal_href")
    assert not hasattr(binding, "absolute_href")


@pytest.mark.anyio
async def test_remote_name_capability_is_replaced_by_synthetic_name_in_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _ = _service(tmp_path)
    request = _seed_request(service)
    secret = "SECRET_CAPABILITY"
    capability_url = "https://pje.cloud.tjpe.jus.br/1g/Download/resultado.seam?ca=" + secret
    row = _row(
        name=f"Íntegra {request.numero} {capability_url}.pdf",
        context=f"Resultado da íntegra do processo {request.numero}",
    )
    ui = _UiSession(
        context=cast(BrowserContext, object()),
        page=cast(Page, object()),
        gate=_NetworkGate(Grau.PRIMEIRO),
    )

    @asynccontextmanager
    async def interactive_session(
        _lease: ReadSessionLease,
        _grau: Grau,
    ) -> AsyncGenerator[_UiSession]:
        yield ui

    monkeypatch.setattr(service, "_interactive_session", interactive_session)
    monkeypatch.setattr(
        pje_docs,
        "_open_area_download",
        AsyncMock(return_value=_AreaSnapshot(rows=(row,), partial=False)),
    )

    page = await service.listar(request.numero, request.grau, request.reference)

    assert len(page.downloads) == 1
    item = page.downloads[0]
    assert item.nome == f"Íntegra do processo {request.numero}"
    assert item.referencia_resultado is not None
    serialized = page.model_dump_json()
    assert secret not in serialized
    assert capability_url not in serialized
    binding = service._results[item.referencia_resultado]
    assert binding.remote_name == f"Íntegra do processo {request.numero}"
    assert secret not in binding.remote_name


@pytest.mark.anyio
async def test_request_reference_is_checked_before_area_navigation(tmp_path: Path) -> None:
    service, sessions, _ = _service(tmp_path)
    request = _seed_request(service)

    with pytest.raises(ValidacaoError, match="outro grau ou processo"):
        await service.listar(_synthetic_npu("9999998"), request.grau, request.reference)

    assert sessions.calls == []


@pytest.mark.anyio
async def test_interactive_session_registers_gate_with_real_playwright(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            authenticated = await browser.new_context()
            page = await authenticated.new_page()
            await authenticated.add_cookies(
                [
                    {
                        "name": "JSESSIONID",
                        "value": "test-session",
                        "url": "https://pje.cloud.tjpe.jus.br/1g/",
                    }
                ]
            )
            lease = ReadSessionLease(
                grau=Grau.PRIMEIRO,
                context=authenticated,
                page=page,
                generation="generation-a",
            )
            try:
                async with service._interactive_session(lease, Grau.PRIMEIRO) as ui:
                    assert not ui.page.is_closed()
                    assert ui.gate.grau is Grau.PRIMEIRO
            finally:
                await authenticated.close()
        finally:
            await browser.close()


@pytest.mark.anyio
async def test_area_table_parser_and_exact_controls_run_locally_without_network() -> None:
    numero = _synthetic_npu()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(
                f"""
                <!doctype html>
                <html><head>
                  <base href="https://pje.cloud.tjpe.jus.br/1g/">
                </head><body>
                  <button id="exact">Abrir menu</button>
                  <button id="longer">Abrir menu de downloads</button>
                  <button disabled>Abrir menu</button>
                  <table>
                    <thead><tr>
                      <th>Nome do arquivo</th><th>Expiração</th><th>Ação</th>
                    </tr></thead>
                    <tbody><tr>
                      <td>Íntegra {numero}.pdf</td>
                      <td>01/09/2026 12:30</td>
                      <td><a href="/1g/Download/arquivo.seam?id=abc">Download</a></td>
                    </tr></tbody>
                  </table>
                  <p>Página 1 de 2</p>
                </body></html>
                """
            )

            control = await _unique_control(page, "Abrir menu")
            snapshot = await _extract_area_snapshot(page)

            assert await control.get_attribute("id") == "exact"
            assert snapshot.partial is True
            assert len(snapshot.rows) == 1
            row = snapshot.rows[0]
            assert row.name == f"Íntegra {numero}.pdf"
            assert row.expires_at == datetime(2026, 9, 1, 15, 30, tzinfo=UTC)
            assert row.link_count == 1
            assert row.literal_href == "/1g/Download/arquivo.seam?id=abc"
            assert row.absolute_href.startswith("https://pje.cloud.tjpe.jus.br/1g/")

            await page.locator("body").evaluate(
                "element => element.insertAdjacentHTML('afterbegin', '<button>Abrir menu</button>')"
            )
            with pytest.raises(InterfacePjeAlteradaError, match="um único controle"):
                await _unique_control(page, "Abrir menu")
        finally:
            await browser.close()


@pytest.mark.anyio
async def test_integral_form_selector_requires_exact_shape_and_forces_both_no_values() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(
                """
                <!doctype html>
                <html><body>
                  <form id="pjedocs" method="post"
                        enctype="application/x-www-form-urlencoded"
                        action="https://pje.cloud.tjpe.jus.br/1g/form.seam">
                    <label for="tipo">Tipo de documento</label>
                    <select id="tipo" name="tipo"><option value="">Selecione</option></select>
                    <label for="inicio">ID a partir de</label>
                    <input id="inicio" name="inicio" value="">
                    <label for="fim">Até</label><input id="fim" name="fim" value="">
                    <label for="fim-periodo">Até</label>
                    <input id="fim-periodo" name="fim-periodo" value="">
                    <label for="periodo">Período de</label>
                    <input id="periodo" name="periodo" value="">
                    <label for="cronologia">Cronologia</label>
                    <select id="cronologia" name="cronologia">
                      <option value="asc">Crescente</option>
                    </select>
                    <label for="expediente">Incluir expediente</label>
                    <select id="expediente" name="expediente">
                      <option value="sim">Sim</option><option value="nao">Não</option>
                    </select>
                    <label for="movimentos">Incluir movimentos</label>
                    <select id="movimentos" name="movimentos">
                      <option value="sim">Sim</option><option value="nao">Não</option>
                    </select>
                    <button type="submit" name="downloadButton">DOWNLOAD</button>
                    <button type="button">CANCELAR</button>
                  </form>
                  <a id="download-anchor" role="button">DOWNLOAD alternativo</a>
                  <div id="download-role" role="button">DOWNLOAD alternativo por role</div>
                </body></html>
                """
            )

            form = await _integral_form(page)
            await _configure_integral_form(form)
            audited = await _validate_integral_form(form, require_no=True)
            submit = await _unique_control(form, "DOWNLOAD")

            assert await page.locator("#tipo").input_value() == ""
            assert await page.locator("#expediente").input_value() == "nao"
            assert await page.locator("#movimentos").input_value() == "nao"
            assert audited == _audited_fields()
            assert await submit.is_enabled()
            assert await _form_post_target(form, page, Grau.PRIMEIRO) == (
                "https://pje.cloud.tjpe.jus.br/1g/form.seam"
            )
            assert await _control_post_marker(submit) == "downloadButton"

            await form.evaluate(
                """
                element => element.insertAdjacentHTML(
                  'beforeend',
                  '<label for="acao-extra">Ação extra</label>' +
                  '<input id="acao-extra" name="acaoExtra" value="gerar">'
                )
                """
            )
            with pytest.raises(InterfacePjeAlteradaError):
                await _validate_integral_form(form, require_no=True)
            await page.locator("#acao-extra").evaluate("element => element.remove()")
            await page.locator("label[for='acao-extra']").evaluate("element => element.remove()")

            await form.evaluate("element => element.setAttribute('method', 'get')")
            with pytest.raises(InterfacePjeAlteradaError, match="POST urlencoded"):
                await _form_post_target(form, page, Grau.PRIMEIRO)
            await form.evaluate("element => element.setAttribute('method', 'post')")

            for selector in ("#download-anchor", "#download-role"):
                with pytest.raises(InterfacePjeAlteradaError, match="submit nativo"):
                    await _control_post_marker(page.locator(selector))

            await page.locator("body").evaluate(
                "element => element.appendChild(document.querySelector('#pjedocs').cloneNode(true))"
            )
            with pytest.raises(InterfacePjeAlteradaError, match="mais de um formulário"):
                await _integral_form(page)
        finally:
            await browser.close()


@pytest.mark.anyio
async def test_warning_scan_rejects_visible_body_text_outside_alert_selectors() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(
                "<!doctype html><html><body><p>Acesso será registrado</p></body></html>"
            )

            with pytest.raises(ValidacaoError, match="a ação foi bloqueada"):
                await _assert_no_warning(page)
        finally:
            await browser.close()


def test_validated_download_url_accepts_only_literal_https_link_of_same_degree() -> None:
    row = _row(
        literal_href="/1g/Download/resultado.seam?id=abc",
        absolute_href="https://pje.cloud.tjpe.jus.br/1g/Download/resultado.seam?id=abc",
    )

    assert _validated_download_url(row, Grau.PRIMEIRO) == row.absolute_href


@pytest.mark.parametrize(
    ("literal", "absolute", "onclick"),
    [
        ("javascript:download()", "javascript:download()", ""),
        ("#resultado", "https://pje.cloud.tjpe.jus.br/1g/#resultado", ""),
        ("/1g/Download/x", "http://pje.cloud.tjpe.jus.br/1g/Download/x", ""),
        ("/1g/Download/x", "https://evil.example/1g/Download/x", ""),
        ("/1g/Download/x", "https://user@pje.cloud.tjpe.jus.br/1g/Download/x", ""),
        ("/1g/Download/x", "https://pje.cloud.tjpe.jus.br:444/1g/Download/x", ""),
        ("/2g/Download/x", "https://pje.cloud.tjpe.jus.br/2g/Download/x", ""),
        ("/1g/%2e%2e/2g/x", "https://pje.cloud.tjpe.jus.br/1g/%2e%2e/2g/x", ""),
        ("/1g/Download/x", "https://pje.cloud.tjpe.jus.br/1g/Download/x#fragment", ""),
        ("/1g/Download/x", "https://pje.cloud.tjpe.jus.br/1g/Download/x", "baixar()"),
    ],
)
def test_validated_download_url_fails_closed(
    literal: str,
    absolute: str,
    onclick: str,
) -> None:
    with pytest.raises(InterfacePjeAlteradaError):
        _validated_download_url(
            _row(literal_href=literal, absolute_href=absolute, onclick=onclick),
            Grau.PRIMEIRO,
        )


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://pje.cloud.tjpe.jus.br/1g/Painel/inicio.seam", True),
        ("https://pje.cloud.tjpe.jus.br/2g/Painel/inicio.seam", False),
        ("https://pje.cloud.tjpe.jus.br/1g/login.seam", False),
        ("https://pje.cloud.tjpe.jus.br/1g/%2e%2e/2g/x", False),
        ("https://pje.cloud.tjpe.jus.br/1g//Download/x", False),
        ("https://evil.example/1g/Painel/inicio.seam", False),
    ],
)
def test_authenticated_navigation_url_is_fail_closed(url: str, allowed: bool) -> None:
    assert _is_same_degree_url(url, Grau.PRIMEIRO) is allowed


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b"%PDF-1.7\nbody", ("application/pdf", ".pdf")),
        (b"PK\x03\x04zip body", ("application/zip", ".zip")),
        (b"PK\x05\x06empty zip", ("application/zip", ".zip")),
    ],
)
def test_payload_accepts_only_recognized_pdf_or_zip(
    payload: bytes,
    expected: tuple[str, str],
) -> None:
    assert _validated_pjedocs_payload(payload, len(payload)) == expected


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (b'<html><form id="kc-form-login"></form></html>', CredenciaisAusentesError),
        (b"MZ\x90\x00executable", ServicoIndisponivelError),
        (b"\x7fELF executable", ServicoIndisponivelError),
        (b"plain unknown payload", ServicoIndisponivelError),
    ],
)
def test_payload_rejects_html_executable_and_unknown_formats(
    payload: bytes,
    error: type[Exception],
) -> None:
    with pytest.raises(error):
        _validated_pjedocs_payload(payload, len(payload))


class _FakeHTTPResponse:
    def __init__(
        self,
        body: bytes = b"",
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.offset = 0
        self.read_sizes: list[int] = []
        self.headers = (
            {
                "content-length": str(len(body)),
                "content-encoding": "identity",
            }
            if headers is None
            else {name.casefold(): value for name, value in headers.items()}
        )

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name.casefold(), default)

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        result = self.body[self.offset : self.offset + size]
        self.offset += len(result)
        return result


class _FakeHTTPSConnection:
    def __init__(self, response: _FakeHTTPResponse) -> None:
        self.response = response
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.closed = False

    def request(self, method: str, target: str, *, headers: dict[str, str]) -> None:
        self.requests.append((method, target, headers))

    def getresponse(self) -> _FakeHTTPResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


def _connection_factory(
    _host: str,
    _port: int,
    *,
    timeout: float,
    connection: _FakeHTTPSConnection,
) -> _FakeHTTPSConnection:
    assert timeout == 10
    return connection


def _mock_pjedocs_connection(
    monkeypatch: pytest.MonkeyPatch,
    response: _FakeHTTPResponse,
) -> _FakeHTTPSConnection:
    connection = _FakeHTTPSConnection(response)

    def connection_factory(host: str, port: int, *, timeout: float) -> _FakeHTTPSConnection:
        return _connection_factory(
            host,
            port,
            timeout=timeout,
            connection=connection,
        )

    monkeypatch.setattr(pje_docs.http.client, "HTTPSConnection", connection_factory)
    return connection


def test_streamed_download_publishes_hash_sidecar_and_restricted_file_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b"%PDF-1.7\nsynthetic pjedocs result"
    response = _FakeHTTPResponse(body)
    connection = _mock_pjedocs_connection(monkeypatch, response)
    destination = tmp_path / "bundle"
    destination.mkdir()

    artifact = _stream_pjedocs_https(
        "https://pje.cloud.tjpe.jus.br/1g/Download/resultado.seam?id=abc",
        cookie_header="JSESSIONID=abc123",
        user_agent="Mozilla/5.0",
        max_bytes=1024,
        timeout_seconds=10,
        destination=destination,
        numero=_synthetic_npu(),
    )

    digest = hashlib.sha256(body).hexdigest()
    assert artifact.path.read_bytes() == body
    assert artifact.sidecar.read_text(encoding="ascii") == f"{digest}  {artifact.path.name}\n"
    assert artifact.sha256 == digest
    assert artifact.mime_type == "application/pdf"
    assert artifact.size == len(body)
    assert stat.S_IMODE(artifact.path.stat().st_mode) == 0o600
    assert not list(destination.glob(".pjedocs-part-*"))
    assert connection.requests == [
        (
            "GET",
            "/1g/Download/resultado.seam?id=abc",
            {
                "Accept-Encoding": "identity",
                "Cookie": "JSESSIONID=abc123",
                "User-Agent": "Mozilla/5.0",
            },
        )
    ]
    assert connection.closed is True


@pytest.mark.parametrize(
    ("status", "headers", "body", "max_bytes", "error", "message"),
    [
        (302, None, b"", 1024, ServicoIndisponivelError, "redirecionar"),
        (401, None, b"", 1024, CredenciaisAusentesError, "sessão expirou"),
        (403, None, b"", 1024, ValidacaoError, "permissão ou expiração"),
        (
            200,
            {"content-encoding": "gzip", "content-length": "10"},
            b"compressed",
            1024,
            ServicoIndisponivelError,
            "codificação de transporte",
        ),
        (
            200,
            {"content-encoding": "identity", "content-length": "inválido"},
            b"%PDF-body",
            1024,
            ServicoIndisponivelError,
            "Content-Length inválido",
        ),
        (
            200,
            {"content-encoding": "identity", "content-length": "2048"},
            b"%PDF-body",
            1024,
            ValidacaoError,
            "excede PJE_TJPE_MAX_PJEDOCS_BYTES",
        ),
        (
            200,
            {"content-encoding": "identity", "content-length": "99"},
            b"%PDF-body",
            1024,
            ServicoIndisponivelError,
            "tamanho diferente",
        ),
        (
            200,
            {"content-encoding": "identity"},
            b"%PDF-" + (b"x" * 20),
            8,
            ValidacaoError,
            "arquivo parcial foi removido",
        ),
    ],
)
def test_streamed_download_fails_closed_and_removes_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    headers: dict[str, str] | None,
    body: bytes,
    max_bytes: int,
    error: type[Exception],
    message: str,
) -> None:
    response = _FakeHTTPResponse(body, status=status, headers=headers)
    connection = _mock_pjedocs_connection(monkeypatch, response)
    destination = tmp_path / "failed-bundle"
    destination.mkdir()

    with pytest.raises(error, match=message):
        _stream_pjedocs_https(
            "https://pje.cloud.tjpe.jus.br/1g/Download/resultado.seam?id=abc",
            cookie_header="JSESSIONID=abc123",
            user_agent="Mozilla/5.0",
            max_bytes=max_bytes,
            timeout_seconds=10,
            destination=destination,
            numero=_synthetic_npu(),
        )

    assert connection.closed is True
    assert list(destination.iterdir()) == []


class _DiskUsage:
    def __init__(self, free: int) -> None:
        self.free = free


def test_streamed_download_rechecks_disk_reserve_after_each_chunk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _FakeHTTPResponse(
        b"%PDF-1.7\nbody",
        headers={"content-encoding": "identity"},
    )
    connection = _mock_pjedocs_connection(monkeypatch, response)
    destination = tmp_path / "disk-bundle"
    destination.mkdir()
    disk_checks = 0

    def disk_usage(_path: Path) -> _DiskUsage:
        nonlocal disk_checks
        disk_checks += 1
        if disk_checks == 1:
            return _DiskUsage(pje_docs._DISK_RESERVE_BYTES + 1024)
        return _DiskUsage(pje_docs._DISK_RESERVE_BYTES)

    monkeypatch.setattr(pje_docs.shutil, "disk_usage", disk_usage)

    with pytest.raises(ServicoIndisponivelError, match="terminou durante"):
        _stream_pjedocs_https(
            "https://pje.cloud.tjpe.jus.br/1g/Download/resultado.seam?id=abc",
            cookie_header="JSESSIONID=abc123",
            user_agent="Mozilla/5.0",
            max_bytes=1024,
            timeout_seconds=10,
            destination=destination,
            numero=_synthetic_npu(),
        )

    assert disk_checks == 2
    assert connection.closed is True
    assert list(destination.iterdir()) == []


def test_atomic_stream_publication_never_overwrites_or_follows_symlink(tmp_path: Path) -> None:
    source = tmp_path / ".part"
    source.write_bytes(b"safe bytes")
    digest = hashlib.sha256(b"safe bytes").hexdigest()
    target = tmp_path / "integra.pdf"

    _publish_streamed_file(source, target, digest)
    assert target.read_bytes() == b"safe bytes"
    assert source.stat().st_ino == target.stat().st_ino

    collision = tmp_path / "collision.pdf"
    collision.write_bytes(b"different")
    with pytest.raises(ServicoIndisponivelError, match="arquivo diferente"):
        _publish_streamed_file(source, collision, digest)
    assert collision.read_bytes() == b"different"

    victim = tmp_path / "victim.pdf"
    victim.write_bytes(b"do not replace")
    symlink = tmp_path / "symlink.pdf"
    symlink.symlink_to(victim)
    with pytest.raises(ServicoIndisponivelError, match="link simbólico"):
        _publish_streamed_file(source, symlink, digest)
    assert victim.read_bytes() == b"do not replace"
    assert os.path.islink(symlink)
