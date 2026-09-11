from __future__ import annotations

import asyncio
import hashlib
import http.client
import os
import re
import secrets
import shutil
import tempfile
import threading
import unicodedata
from collections import OrderedDict
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from functools import wraps
from pathlib import Path
from typing import Any, ParamSpec, TypeVar, cast
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from zoneinfo import ZoneInfo

from playwright.async_api import BrowserContext, Dialog, Locator, Page, Route, WebSocketRoute

from mcp_pje_tjpe.config import Settings
from mcp_pje_tjpe.errors import (
    CredenciaisAusentesError,
    InterfacePjeAlteradaError,
    PjeTjpeError,
    ServicoIndisponivelError,
    ValidacaoError,
)
from mcp_pje_tjpe.models import (
    ArquivoPjeDocsBaixado,
    EstadoDownloadPjeDocs,
    EstadoSolicitacaoPjeDocs,
    Grau,
    ItemDownloadPjeDocs,
    PaginaDownloadsPjeDocs,
    PreparacaoPjeDocs,
    SolicitacaoPjeDocs,
)
from mcp_pje_tjpe.pje_auth import PjeSessionManager, ReadSessionLease
from mcp_pje_tjpe.pje_public import normalize_npu_tjpe
from mcp_pje_tjpe.pje_read import (
    PjeReadService,
    PjeReadTarget,
    atomic_publish,
    browser_cookie_header,
    secure_subdirectory,
)

CONFIRMATION_PHRASE = "CONFIRMO DOWNLOAD INTEGRAL NO PJEDOCS"

_REFERENCE = re.compile(r"^[A-Za-z0-9_-]{20,80}$")
_COOKIE_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_NPU_TOKEN = re.compile(r"(?<!\d)(?:\d{7}-\d{2}\.\d{4}\.8\.17\.\d{4}|\d{20})(?!\d)")
_PAGINATION = re.compile(r"\b(?P<atual>\d+)\s+de\s+(?P<total>\d+)\b", re.IGNORECASE)
_WARNING = re.compile(
    r"(?:tomar\s+ci[eê]ncia|registrar\s+ci[eê]ncia|pendente\s+de\s+ci[eê]ncia|"
    r"visualizar\s+expediente|acesso\s+ser[aá]\s+registrado|responsabiliza[cç][aã]o|"
    r"acesso\s+negado|n[aã]o\s+possui\s+permiss[aã]o|acesso\s+restrito|"
    r"documento\s+sigiloso)",
    re.IGNORECASE,
)
_SUCCESS = (
    "Prezado usuário, o documento solicitado será gerado e, quando finalizado, "
    "será disponibilizado no menu principal em: Download > Área de download."
)
_SUCCESS_COMPACT = " ".join(_SUCCESS.split()).casefold()
_PLAN_TTL = timedelta(minutes=10)
_MAX_REGISTRY_DEFAULT = 500
_DOWNLOAD_SLOT_WAIT_SECONDS = 1.0
_DOWNLOAD_PROCESS_SLOT = threading.BoundedSemaphore(value=1)
_DISK_RESERVE_BYTES = 64 * 1024 * 1024
_DOWNLOAD_CHUNK_BYTES = 256 * 1024
_LOGIN_MARKERS = (
    b'id="kc-pje-office"',
    b'id="kc-form-login"',
    b'name="username"',
)
_UNSAFE_MAGIC = (
    b"MZ",
    b"\x7fELF",
    b"\xcf\xfa\xed\xfe",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xfe\xed\xfa\xce",
)

_P = ParamSpec("_P")
_T = TypeVar("_T")


def _compact(value: str) -> str:
    return " ".join(value.split())


def _consume_background_exception(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        _ = task.exception()


def _sanitized_failure(
    message: str,
) -> Callable[[Callable[_P, Awaitable[_T]]], Callable[_P, Awaitable[_T]]]:
    """Impede que erros de navegador/rede exponham URLs efêmeras ou cookies."""

    def decorate(function: Callable[_P, Awaitable[_T]]) -> Callable[_P, Awaitable[_T]]:
        @wraps(function)
        async def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _T:
            try:
                return await function(*args, **kwargs)
            except asyncio.CancelledError:
                raise
            except PjeTjpeError:
                raise
            except Exception:
                raise ServicoIndisponivelError(message) from None

        return wrapped

    return decorate


class _PlanStatus(StrEnum):
    PREPARADO = "preparado"
    SUBMETENDO = "submetendo"
    SOLICITADO = "solicitado"
    INDETERMINADO = "indeterminado"


@dataclass(slots=True)
class _Plan:
    reference: str
    generation: str = field(repr=False)
    grau: Grau
    numero: str
    processo_id: str = field(repr=False)
    fingerprint: str = field(repr=False)
    baseline: frozenset[str] = field(repr=False)
    created_at: datetime
    expires_at: datetime
    status: _PlanStatus = _PlanStatus.PREPARADO
    request_reference: str | None = None


@dataclass(frozen=True, slots=True)
class _RequestBinding:
    reference: str
    plan_reference: str
    generation: str = field(repr=False)
    grau: Grau
    numero: str
    processo_id: str = field(repr=False)
    baseline: frozenset[str] = field(repr=False)
    state: EstadoSolicitacaoPjeDocs
    requested_at: datetime


@dataclass(frozen=True, slots=True)
class _ResultBinding:
    reference: str
    request_reference: str
    generation: str = field(repr=False)
    grau: Grau
    numero: str
    processo_id: str = field(repr=False)
    row_key: str = field(repr=False)
    row_fingerprint: str = field(repr=False)
    remote_name: str
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class _AreaRow:
    key: str
    name: str = field(repr=False)
    expiration_text: str
    context: str = field(repr=False)
    expires_at: datetime | None
    link_count: int
    literal_href: str = field(repr=False)
    absolute_href: str = field(repr=False)
    onclick: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class _AreaSnapshot:
    rows: tuple[_AreaRow, ...]
    partial: bool

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(row.key for row in self.rows)


@dataclass(frozen=True, slots=True)
class _DownloadedArtifact:
    path: Path
    sidecar: Path
    mime_type: str
    size: int
    sha256: str


@dataclass(slots=True)
class _NetworkGate:
    grau: Grau
    dialog_epoch: int = 0
    allowed_post_target: str | None = field(default=None, repr=False)
    allowed_dialog_epoch: int | None = field(default=None, repr=False)
    required_post_marker: str | None = field(default=None, repr=False)
    required_post_fields: dict[str, str] = field(
        default_factory=lambda: dict[str, str](), repr=False
    )
    strict_post_fields: bool = False
    remaining_posts: int = 0
    post_count: int = 0
    blocked_mutation: bool = False

    def permit_single_post(
        self,
        target: str,
        marker: str,
        fields: dict[str, str] | None = None,
    ) -> None:
        validated_target = _post_target(target, self.grau)
        validated_marker = _validated_post_marker(marker)
        audited_fields = dict(fields or {})
        if validated_marker in audited_fields:
            raise InterfacePjeAlteradaError(
                "o marcador do submit colide com um campo ordinário do formulário"
            )
        self.allowed_post_target = validated_target
        self.allowed_dialog_epoch = self.dialog_epoch
        self.required_post_marker = validated_marker
        self.required_post_fields = audited_fields
        self.strict_post_fields = fields is not None
        self.remaining_posts = 1

    def close_post_window(self) -> None:
        self.allowed_post_target = None
        self.allowed_dialog_epoch = None
        self.required_post_marker = None
        self.required_post_fields.clear()
        self.strict_post_fields = False
        self.remaining_posts = 0

    async def handle(self, route: Route) -> None:
        request = route.request
        same_degree = _is_same_degree_url(request.url, self.grau)
        method = request.method.upper()
        if same_degree and method in {"GET", "HEAD"}:
            await route.continue_()
            return
        if (
            same_degree
            and method == "POST"
            and self.allowed_post_target is not None
            and self.allowed_dialog_epoch == self.dialog_epoch
            and self.required_post_marker is not None
            and self.remaining_posts > 0
            and _same_post_target(request.url, self.allowed_post_target)
            and _post_matches_contract(
                request.post_data,
                self.required_post_marker,
                self.required_post_fields,
                strict=self.strict_post_fields,
            )
        ):
            self.post_count += 1
            self.remaining_posts -= 1
            await route.continue_()
            return
        if method not in {"GET", "HEAD"}:
            self.blocked_mutation = True
        await route.abort("blockedbyclient")


@dataclass(slots=True)
class _UiSession:
    context: BrowserContext = field(repr=False)
    page: Page = field(repr=False)
    gate: _NetworkGate = field(repr=False)
    dialogs: list[str] = field(default_factory=lambda: list[str](), repr=False)


class _SubmissionUncertain(Exception):
    def __init__(self, baseline: frozenset[str]) -> None:
        super().__init__("submission outcome uncertain")
        self.baseline = baseline


class PjeDocsService:
    """Adaptador conservador da UI PJeDocs, sem API ou rotas presumidas."""

    def __init__(
        self,
        sessions: PjeSessionManager,
        read_service: PjeReadService,
        config: Settings,
        *,
        registry_limit: int = _MAX_REGISTRY_DEFAULT,
    ) -> None:
        if registry_limit < 1:
            raise ValueError("registry_limit deve ser positivo")
        if config.max_pjedocs_bytes < 1:
            raise ValueError("PJE_TJPE_MAX_PJEDOCS_BYTES deve ser positivo")
        self.sessions = sessions
        self.read = read_service
        self.config = config
        self.registry_limit = registry_limit
        self._plans: OrderedDict[str, _Plan] = OrderedDict()
        self._requests: OrderedDict[str, _RequestBinding] = OrderedDict()
        self._results: OrderedDict[str, _ResultBinding] = OrderedDict()
        self._fingerprints: dict[str, str] = {}
        self._result_by_row: dict[tuple[str, str], str] = {}
        self._expired_requests: set[str] = set()
        self._submission_tasks: dict[str, asyncio.Task[SolicitacaoPjeDocs]] = {}
        self._download_tasks: dict[str, asyncio.Task[ArquivoPjeDocsBaixado]] = {}
        self._state_lock = asyncio.Lock()
        self._download_slot = asyncio.Semaphore(1)

    @_sanitized_failure("não foi possível inspecionar com segurança o PJeDocs")
    async def preparar(self, numero: str, grau: Grau) -> PreparacaoPjeDocs:
        formatted = normalize_npu_tjpe(numero)
        await self._assert_autos_safe(formatted, grau)

        async with self.sessions.read_session(grau) as lease:
            target = await self.read.preparar_alvo_pjedocs(lease, formatted)
            async with self._interactive_session(lease, grau) as ui:
                await _goto_audited(ui.page, target.autos_url, grau, formatted)
                await _unique_control(ui.page, "Download autos do processo")
                if ui.dialogs or ui.gate.blocked_mutation:
                    raise InterfacePjeAlteradaError(
                        "o PJe apresentou uma interação inesperada durante a preparação"
                    )
                snapshot = await _open_area_download(ui, self.config, grau)
                if snapshot.partial:
                    raise InterfacePjeAlteradaError(
                        "a Área de download está paginada; a preparação foi bloqueada"
                    )
                if ui.dialogs or ui.gate.blocked_mutation:
                    raise InterfacePjeAlteradaError(
                        "o PJe apresentou uma interação inesperada na Área de download"
                    )

        fingerprint = _plan_fingerprint(target)
        now = datetime.now(UTC)
        async with self._state_lock:
            previous_ref = self._fingerprints.get(fingerprint)
            previous = self._plans.get(previous_ref) if previous_ref is not None else None
            if previous is not None:
                expired_result_seen = (
                    previous.request_reference is not None
                    and previous.request_reference in self._expired_requests
                )
                if (previous.status == _PlanStatus.PREPARADO and previous.expires_at > now) or (
                    previous.status != _PlanStatus.PREPARADO and not expired_result_seen
                ):
                    self._plans.move_to_end(previous.reference)
                    return _preparation_model(previous)

            reference = secrets.token_urlsafe(24)
            plan = _Plan(
                reference=reference,
                generation=target.generation,
                grau=grau,
                numero=formatted,
                processo_id=target.processo_id,
                fingerprint=fingerprint,
                baseline=snapshot.keys,
                created_at=now,
                expires_at=now + _PLAN_TTL,
            )
            self._plans[reference] = plan
            self._fingerprints[fingerprint] = reference
            self._trim_registries()
            return _preparation_model(plan)

    @_sanitized_failure("não foi possível concluir a solicitação segura no PJeDocs")
    async def solicitar(
        self,
        numero: str,
        grau: Grau,
        referencia_preparo: str,
        confirmacao: str,
    ) -> SolicitacaoPjeDocs:
        formatted = normalize_npu_tjpe(numero)
        if confirmacao != CONFIRMATION_PHRASE:
            raise ValidacaoError(
                "confirmação incorreta; copie literalmente a frase devolvida por "
                "preparar_download_pjedocs"
            )

        created = False
        async with self._state_lock:
            plan = self._resolve_plan(referencia_preparo, formatted, grau)
            if plan.request_reference is not None:
                binding = self._requests.get(plan.request_reference)
                if binding is None:
                    raise ValidacaoError("a referência da solicitação expirou localmente")
                self._requests.move_to_end(binding.reference)
                return _request_model(binding, reused=True)
            if plan.expires_at <= datetime.now(UTC):
                raise ValidacaoError(
                    "a preparação expirou; inspecione novamente o processo antes de confirmar"
                )
            task = self._submission_tasks.get(plan.reference)
            if task is None:
                if plan.status == _PlanStatus.SUBMETENDO:
                    binding = self._record_request_locked(
                        plan,
                        plan.baseline,
                        EstadoSolicitacaoPjeDocs.INDETERMINADO,
                    )
                    return _request_model(binding, reused=True)
                plan.status = _PlanStatus.SUBMETENDO
                task = asyncio.create_task(self._run_submission(plan.reference))
                task.add_done_callback(_consume_background_exception)
                self._submission_tasks[plan.reference] = task
                created = True

        result = await asyncio.shield(task)
        return result if created else result.model_copy(update={"reutilizada": True})

    @_sanitized_failure("não foi possível consultar com segurança a Área de download")
    async def listar(
        self,
        numero: str,
        grau: Grau,
        referencia_solicitacao: str,
    ) -> PaginaDownloadsPjeDocs:
        formatted = normalize_npu_tjpe(numero)
        async with self._state_lock:
            request = self._resolve_request(referencia_solicitacao, formatted, grau)

        async with self.sessions.read_session(grau) as lease:
            target = await self.read.preparar_alvo_pjedocs(lease, formatted)
            _assert_target_binding(target, request)
            async with self._interactive_session(lease, grau) as ui:
                snapshot = await _open_area_download(ui, self.config, grau)
                if ui.dialogs or ui.gate.blocked_mutation:
                    raise InterfacePjeAlteradaError(
                        "o PJe apresentou uma interação inesperada na Área de download"
                    )

        if snapshot.partial:
            return PaginaDownloadsPjeDocs(
                numero=formatted,
                grau=grau,
                referencia_solicitacao=request.reference,
                downloads=[],
                parcial=True,
                aviso=(
                    "A tabela possui outras páginas; nenhum resultado foi correlacionado "
                    "nem recebeu referência local."
                ),
            )

        candidates = _correlated_rows(snapshot, request)
        if not candidates:
            return PaginaDownloadsPjeDocs(
                numero=formatted,
                grau=grau,
                referencia_solicitacao=request.reference,
                downloads=[
                    ItemDownloadPjeDocs(
                        nome=f"Íntegra do processo {formatted}",
                        estado=EstadoDownloadPjeDocs.PROCESSANDO,
                    )
                ],
                parcial=False,
                aviso=(
                    "O PJeDocs ainda não apresentou uma nova linha inequivocamente ligada "
                    "a esta solicitação; não foi emitida referência de resultado."
                ),
            )
        if len(candidates) != 1:
            raise InterfacePjeAlteradaError(
                "mais de um resultado novo corresponde ao processo; a correlação foi bloqueada"
            )

        row = candidates[0]
        expired = row.expires_at is not None and row.expires_at <= datetime.now(UTC)
        state = EstadoDownloadPjeDocs.EXPIRADO if expired else EstadoDownloadPjeDocs.PROCESSANDO
        result_reference: str | None = None
        if not expired and row.link_count == 1:
            _validated_download_url(row, grau)
            result_reference = await self._register_result(request, row)
            state = EstadoDownloadPjeDocs.PRONTO
        elif row.link_count > 1:
            raise InterfacePjeAlteradaError(
                "o resultado apresentou mais de um controle Download; nenhum link foi usado"
            )
        elif row.literal_href or row.onclick:
            raise InterfacePjeAlteradaError(
                "o controle do resultado não é um único link literal reconhecido"
            )

        if expired:
            async with self._state_lock:
                self._expired_requests.add(request.reference)

        return PaginaDownloadsPjeDocs(
            numero=formatted,
            grau=grau,
            referencia_solicitacao=request.reference,
            downloads=[
                ItemDownloadPjeDocs(
                    nome=f"Íntegra do processo {request.numero}",
                    expira_em=row.expires_at,
                    estado=state,
                    referencia_resultado=result_reference,
                )
            ],
            parcial=False,
            aviso=(
                "Estado reconciliado somente com a linha nova deste NPU. A URL renovável "
                "do PJeDocs não é persistida nem devolvida."
            ),
        )

    @_sanitized_failure("não foi possível baixar com segurança o resultado do PJeDocs")
    async def baixar(
        self,
        numero: str,
        grau: Grau,
        referencia_resultado: str,
    ) -> ArquivoPjeDocsBaixado:
        formatted = normalize_npu_tjpe(numero)
        async with self._state_lock:
            result = self._resolve_result(referencia_resultado, formatted, grau)
            existing_task = self._download_tasks.get(result.reference)
            if existing_task is not None:
                task = existing_task
            else:
                task = asyncio.create_task(self._download_registered_result(result))
                task.add_done_callback(_consume_background_exception)
                self._download_tasks[result.reference] = task
        return await asyncio.shield(task)

    async def _assert_autos_safe(self, numero: str, grau: Grau) -> None:
        autos = await self.read.consultar_autos(
            numero,
            grau,
            limite_documentos=500,
            limite_movimentos=0,
        )
        if autos.documentos_parciais:
            raise ValidacaoError(
                "a árvore de documentos está parcial; a íntegra não será solicitada "
                "sem verificar todos os avisos de ciência"
            )
        if any(document.bloqueado_por_ciencia for document in autos.documentos):
            raise ValidacaoError(
                "os Autos contêm documento pendente de ciência; a íntegra foi bloqueada"
            )

    async def _run_submission(self, plan_reference: str) -> SolicitacaoPjeDocs:
        try:
            async with self._state_lock:
                plan = self._plans.get(plan_reference)
                if plan is None:
                    raise ValidacaoError("a preparação expirou localmente")
            try:
                baseline = await self._submit_once(plan)
                state = EstadoSolicitacaoPjeDocs.SOLICITADO
            except _SubmissionUncertain as uncertain:
                baseline = uncertain.baseline
                state = EstadoSolicitacaoPjeDocs.INDETERMINADO
            except BaseException:
                async with self._state_lock:
                    current = self._plans.get(plan_reference)
                    if current is not None and current.request_reference is None:
                        current.status = _PlanStatus.PREPARADO
                raise

            finalizer = asyncio.create_task(
                self._finalize_submission(plan_reference, baseline, state)
            )
            finalizer.add_done_callback(_consume_background_exception)
            try:
                return await asyncio.shield(finalizer)
            except asyncio.CancelledError:
                # O chamador pode ser cancelado, mas o tombstone pós-clique precisa
                # chegar ao registro antes que outra chamada possa avaliar reenvio.
                await asyncio.shield(finalizer)
                raise
        finally:
            async with self._state_lock:
                current_task = asyncio.current_task()
                if self._submission_tasks.get(plan_reference) is current_task:
                    self._submission_tasks.pop(plan_reference, None)

    async def _finalize_submission(
        self,
        plan_reference: str,
        baseline: frozenset[str],
        state: EstadoSolicitacaoPjeDocs,
    ) -> SolicitacaoPjeDocs:
        async with self._state_lock:
            current = self._plans.get(plan_reference)
            if current is None:
                raise ValidacaoError("a preparação expirou localmente")
            if current.request_reference is not None:
                existing = self._requests.get(current.request_reference)
                if existing is None:
                    raise ValidacaoError("a solicitação expirou localmente")
                return _request_model(existing, reused=True)
            binding = self._record_request_locked(current, baseline, state)
            return _request_model(binding, reused=False)

    def _record_request_locked(
        self,
        plan: _Plan,
        baseline: frozenset[str],
        state: EstadoSolicitacaoPjeDocs,
    ) -> _RequestBinding:
        reference = secrets.token_urlsafe(24)
        binding = _RequestBinding(
            reference=reference,
            plan_reference=plan.reference,
            generation=plan.generation,
            grau=plan.grau,
            numero=plan.numero,
            processo_id=plan.processo_id,
            baseline=baseline,
            state=state,
            requested_at=datetime.now(UTC),
        )
        self._requests[reference] = binding
        plan.request_reference = reference
        plan.status = (
            _PlanStatus.SOLICITADO
            if state == EstadoSolicitacaoPjeDocs.SOLICITADO
            else _PlanStatus.INDETERMINADO
        )
        self._trim_registries()
        return binding

    async def _submit_once(self, plan: _Plan) -> frozenset[str]:
        baseline = plan.baseline
        maybe_clicked = False
        try:
            await self._assert_autos_safe(plan.numero, plan.grau)
            async with self.sessions.read_session(plan.grau) as lease:
                target = await self.read.preparar_alvo_pjedocs(lease, plan.numero)
                _assert_target_binding(target, plan)
                async with self._interactive_session(lease, plan.grau) as ui:
                    current = await _open_area_download(ui, self.config, plan.grau)
                    if current.partial:
                        raise InterfacePjeAlteradaError(
                            "a Área de download está paginada antes da solicitação"
                        )
                    if ui.dialogs or ui.gate.blocked_mutation:
                        raise InterfacePjeAlteradaError(
                            "o PJe apresentou uma interação inesperada antes da solicitação"
                        )
                    baseline = current.keys
                    await _goto_audited(ui.page, target.autos_url, plan.grau, plan.numero)
                    await _assert_no_warning(ui.page)
                    if ui.dialogs or ui.gate.blocked_mutation:
                        raise InterfacePjeAlteradaError(
                            "o PJe apresentou aviso ou mutação antes de abrir o PJeDocs"
                        )
                    open_control = await _unique_control(ui.page, "Download autos do processo")
                    _assert_plan_fresh(plan)
                    open_permission = await _control_post_permission(
                        open_control,
                        ui.page,
                        plan.grau,
                    )
                    if ui.dialogs or ui.gate.blocked_mutation:
                        raise InterfacePjeAlteradaError(
                            "o PJe apresentou aviso durante a abertura do PJeDocs"
                        )
                    _assert_plan_fresh(plan)
                    if open_permission is not None:
                        ui.gate.permit_single_post(*open_permission)
                    maybe_clicked = True
                    try:
                        action_page = await _click_and_resolve_page(
                            ui.context,
                            ui.page,
                            open_control,
                            plan.grau,
                            wait_ms=350,
                        )
                    finally:
                        ui.gate.close_post_window()
                    form = await _integral_form(action_page)
                    await _assert_no_warning(action_page)
                    await _configure_integral_form(form)
                    submit = await _unique_control(form, "DOWNLOAD")
                    await _unique_control(form, "CANCELAR")
                    if ui.dialogs or ui.gate.blocked_mutation:
                        raise InterfacePjeAlteradaError(
                            "o PJe apresentou diálogo ou mutação inesperada antes do envio"
                        )
                    if await _success_in_body(action_page):
                        raise InterfacePjeAlteradaError(
                            "a confirmação do PJeDocs já estava presente antes do envio"
                        )
                    dialog_baseline = len(ui.dialogs)
                    submit_target = await _form_post_target(form, action_page, plan.grau)
                    submit_marker = await _control_post_marker(submit)
                    audited_fields = await _validate_integral_form(form, require_no=True)
                    if ui.dialogs or ui.gate.blocked_mutation:
                        raise InterfacePjeAlteradaError(
                            "o PJe apresentou aviso imediatamente antes do envio"
                        )
                    _assert_plan_fresh(plan)
                    post_baseline = ui.gate.post_count
                    ui.gate.permit_single_post(
                        submit_target,
                        submit_marker,
                        audited_fields,
                    )
                    try:
                        await submit.click()
                        await action_page.wait_for_timeout(900)
                    finally:
                        ui.gate.close_post_window()
                    if ui.gate.blocked_mutation:
                        raise InterfacePjeAlteradaError(
                            "o PJe tentou uma mutação fora da janela confirmada"
                        )
                    if ui.gate.post_count != post_baseline + 1:
                        raise InterfacePjeAlteradaError(
                            "o clique DOWNLOAD não produziu exatamente um POST auditado"
                        )
                    if not await _submission_succeeded(
                        action_page,
                        ui.dialogs[dialog_baseline:],
                    ):
                        raise InterfacePjeAlteradaError(
                            "o PJe não apresentou a confirmação oficial da solicitação"
                        )
                    return baseline
        except asyncio.CancelledError:
            if maybe_clicked:
                raise _SubmissionUncertain(baseline) from None
            raise
        except (
            CredenciaisAusentesError,
            InterfacePjeAlteradaError,
            ServicoIndisponivelError,
            ValidacaoError,
        ):
            if maybe_clicked:
                raise _SubmissionUncertain(baseline) from None
            raise
        except Exception:
            if maybe_clicked:
                raise _SubmissionUncertain(baseline) from None
            raise ServicoIndisponivelError(
                "não foi possível preparar o formulário oficial do PJeDocs"
            ) from None

    async def _register_result(self, request: _RequestBinding, row: _AreaRow) -> str:
        key = (request.reference, row.key)
        async with self._state_lock:
            previous_ref = self._result_by_row.get(key)
            if previous_ref is not None and previous_ref in self._results:
                self._results.move_to_end(previous_ref)
                return previous_ref
            reference = secrets.token_urlsafe(24)
            self._results[reference] = _ResultBinding(
                reference=reference,
                request_reference=request.reference,
                generation=request.generation,
                grau=request.grau,
                numero=request.numero,
                processo_id=request.processo_id,
                row_key=row.key,
                row_fingerprint=_row_fingerprint(row),
                remote_name=f"Íntegra do processo {request.numero}",
                expires_at=row.expires_at,
            )
            self._result_by_row[key] = reference
            self._trim_registries()
            return reference

    async def _download_registered_result(
        self,
        result: _ResultBinding,
    ) -> ArquivoPjeDocsBaixado:
        try:
            if result.expires_at is not None and result.expires_at <= datetime.now(UTC):
                async with self._state_lock:
                    self._expired_requests.add(result.request_reference)
                raise ValidacaoError(
                    "o resultado do PJeDocs expirou; execute uma nova preparação e "
                    "confirme novamente para solicitar outra geração"
                )
            async with self.sessions.read_session(result.grau) as lease:
                target = await self.read.preparar_alvo_pjedocs(lease, result.numero)
                _assert_target_binding(target, result)
                async with self._interactive_session(lease, result.grau) as ui:
                    snapshot = await _open_area_download(ui, self.config, result.grau)
                    if snapshot.partial or ui.dialogs or ui.gate.blocked_mutation:
                        raise InterfacePjeAlteradaError(
                            "a Área de download não pôde ser revalidada integralmente"
                        )
                    matches = [row for row in snapshot.rows if row.key == result.row_key]
                    if len(matches) != 1:
                        raise ValidacaoError(
                            "o resultado não aparece mais de forma inequívoca na Área de download"
                        )
                    row = matches[0]
                    if (
                        not _row_matches_npu(row, result.numero)
                        or _row_fingerprint(row) != result.row_fingerprint
                    ):
                        raise ValidacaoError(
                            "a linha do resultado não corresponde mais ao processo vinculado"
                        )
                    url = _validated_download_url(row, result.grau)
                    cookie_header = await browser_cookie_header(lease.context, url)
                    user_agent = cast(str, await lease.page.evaluate("navigator.userAgent"))

            try:
                await asyncio.wait_for(
                    self._download_slot.acquire(), timeout=_DOWNLOAD_SLOT_WAIT_SECONDS
                )
            except TimeoutError:
                raise ServicoIndisponivelError(
                    "o downloader seguro do PJeDocs está ocupado; tente novamente em instantes"
                ) from None
            try:
                worker = asyncio.create_task(
                    asyncio.to_thread(
                        self._download_to_bundle,
                        result,
                        url,
                        cookie_header,
                        user_agent,
                    )
                )
            except BaseException:
                self._download_slot.release()
                raise

            def release_slot(done: asyncio.Task[_DownloadedArtifact]) -> None:
                self._download_slot.release()
                if not done.cancelled():
                    _ = done.exception()

            worker.add_done_callback(release_slot)
            artifact = await asyncio.shield(worker)
            return ArquivoPjeDocsBaixado(
                numero=result.numero,
                grau=result.grau,
                referencia_resultado=result.reference,
                nome_arquivo=artifact.path.name,
                caminho=str(artifact.path),
                caminho_sha256=str(artifact.sidecar),
                tipo_mime=artifact.mime_type,
                tamanho_bytes=artifact.size,
                sha256=artifact.sha256,
                aviso=(
                    "Íntegra e sidecar SHA-256 gravados com permissão restrita. O hash "
                    "identifica os bytes recebidos, mas não substitui assinatura digital."
                ),
            )
        finally:
            async with self._state_lock:
                current_task = asyncio.current_task()
                if self._download_tasks.get(result.reference) is current_task:
                    self._download_tasks.pop(result.reference, None)

    def _download_to_bundle(
        self,
        result: _ResultBinding,
        url: str,
        cookie_header: str,
        user_agent: str,
    ) -> _DownloadedArtifact:
        if not _DOWNLOAD_PROCESS_SLOT.acquire(timeout=_DOWNLOAD_SLOT_WAIT_SECONDS):
            raise ServicoIndisponivelError(
                "o downloader seguro do PJeDocs está ocupado; tente novamente em instantes"
            )
        try:
            root = self.config.downloads_dir.expanduser().resolve()
            process_dir = secure_subdirectory(
                root,
                "TJPE",
                result.grau.value,
                result.numero,
                "pjedocs",
            )
            return _stream_pjedocs_https(
                url,
                cookie_header=cookie_header,
                user_agent=user_agent,
                max_bytes=self.config.max_pjedocs_bytes,
                timeout_seconds=max(self.config.timeout_ms / 1_000, 1.0),
                destination=process_dir,
                numero=result.numero,
            )
        finally:
            _DOWNLOAD_PROCESS_SLOT.release()

    @asynccontextmanager
    async def _interactive_session(
        self,
        lease: ReadSessionLease,
        grau: Grau,
    ) -> AsyncGenerator[_UiSession]:
        browser = lease.context.browser
        if browser is None:
            raise ServicoIndisponivelError(
                "o navegador autenticado não permite criar o adaptador isolado do PJeDocs"
            )
        base = self.config.urls.pje_base(grau.value) + "/"
        cookies = await _scoped_cookies(lease.context, base)
        context = await browser.new_context(
            accept_downloads=False,
            locale="pt-BR",
            java_script_enabled=True,
            service_workers="block",
            viewport={"width": 1440, "height": 1000},
        )
        gate = _NetworkGate(grau)
        dialogs: list[str] = []

        async def dismiss_dialog(dialog: Dialog) -> None:
            gate.dialog_epoch += 1
            dialogs.append(_compact(dialog.message)[:2_000])
            await dialog.dismiss()

        def attach_dialog_handler(page: Page) -> None:
            page.on("dialog", dismiss_dialog)

        async def block_websocket(route: WebSocketRoute) -> None:
            await route.close()

        async def handle_route(route: Route) -> None:
            # Playwright annotates callbacks; a slots-bound method cannot receive
            # the private attribute it uses for that bookkeeping.
            await gate.handle(route)

        try:
            await context.add_cookies(cast(Any, cookies))
            await context.route("**/*", handle_route)
            await context.route_web_socket("**/*", block_websocket)
            context.on("page", attach_dialog_handler)
            page = await context.new_page()
            page.set_default_timeout(self.config.timeout_ms)
            page.set_default_navigation_timeout(self.config.timeout_ms)
            yield _UiSession(context=context, page=page, gate=gate, dialogs=dialogs)
        finally:
            await context.close()

    def _resolve_plan(self, reference: str, numero: str, grau: Grau) -> _Plan:
        _validate_reference(reference, "preparação")
        plan = self._plans.get(reference)
        if plan is None:
            raise ValidacaoError("referência de preparação desconhecida ou expirada")
        if plan.numero != numero or plan.grau != grau:
            raise ValidacaoError("a preparação pertence a outro grau ou processo")
        self._plans.move_to_end(reference)
        return plan

    def _resolve_request(self, reference: str, numero: str, grau: Grau) -> _RequestBinding:
        _validate_reference(reference, "solicitação")
        request = self._requests.get(reference)
        if request is None:
            raise ValidacaoError("referência de solicitação desconhecida ou expirada")
        if request.numero != numero or request.grau != grau:
            raise ValidacaoError("a solicitação pertence a outro grau ou processo")
        self._requests.move_to_end(reference)
        return request

    def _resolve_result(self, reference: str, numero: str, grau: Grau) -> _ResultBinding:
        _validate_reference(reference, "resultado")
        result = self._results.get(reference)
        if result is None:
            raise ValidacaoError("referência de resultado desconhecida ou expirada")
        if result.numero != numero or result.grau != grau:
            raise ValidacaoError("o resultado pertence a outro grau ou processo")
        self._results.move_to_end(reference)
        return result

    def _trim_registries(self) -> None:
        while len(self._plans) > self.registry_limit:
            newest_plan = next(reversed(self._plans))
            removable = next(
                (
                    (reference, plan)
                    for reference, plan in self._plans.items()
                    if plan.status == _PlanStatus.PREPARADO
                    and reference not in self._submission_tasks
                    and reference != newest_plan
                ),
                None,
            )
            if removable is None:
                break
            reference, plan = removable
            del self._plans[reference]
            if self._fingerprints.get(plan.fingerprint) == reference:
                self._fingerprints.pop(plan.fingerprint, None)
        while len(self._results) > self.registry_limit:
            removable_result = next(
                (
                    (reference, result)
                    for reference, result in self._results.items()
                    if reference not in self._download_tasks
                ),
                None,
            )
            if removable_result is None:
                break
            reference, result = removable_result
            del self._results[reference]
            key = (result.request_reference, result.row_key)
            if self._result_by_row.get(key) == reference:
                self._result_by_row.pop(key, None)


def _preparation_model(plan: _Plan) -> PreparacaoPjeDocs:
    return PreparacaoPjeDocs(
        referencia_preparo=plan.reference,
        numero=plan.numero,
        grau=plan.grau,
        expira_em=plan.expires_at,
        frase_confirmacao=CONFIRMATION_PHRASE,
        aviso=(
            "A interface e a Área de download foram apenas inspecionadas; nenhuma geração "
            "foi solicitada. A confirmação pedirá a íntegra, sem expedientes e movimentos."
        ),
    )


def _assert_plan_fresh(plan: _Plan) -> None:
    if plan.expires_at <= datetime.now(UTC):
        raise ValidacaoError(
            "a preparação expirou antes da janela mutável; nenhum novo clique será tentado"
        )


def _request_model(binding: _RequestBinding, *, reused: bool) -> SolicitacaoPjeDocs:
    warning = (
        "O clique ocorreu, mas a confirmação oficial não pôde ser provada. "
        "O MCP não reenviará automaticamente; use listar_downloads_pjedocs para reconciliar."
        if binding.state == EstadoSolicitacaoPjeDocs.INDETERMINADO
        else "Solicitação confirmada pela mensagem oficial do PJeDocs."
    )
    return SolicitacaoPjeDocs(
        referencia_solicitacao=binding.reference,
        numero=binding.numero,
        grau=binding.grau,
        estado=binding.state,
        reutilizada=reused,
        solicitado_em=binding.requested_at,
        aviso=warning,
    )


def _plan_fingerprint(target: PjeReadTarget) -> str:
    raw = "\0".join(
        (target.generation, target.grau.value, target.numero, target.processo_id, "integra")
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _validate_reference(reference: str, kind: str) -> None:
    if _REFERENCE.fullmatch(reference) is None:
        raise ValidacaoError(f"referência de {kind} inválida")


def _assert_target_binding(
    target: PjeReadTarget, binding: _Plan | _RequestBinding | _ResultBinding
) -> None:
    if (
        target.generation != binding.generation
        or target.grau != binding.grau
        or target.numero != binding.numero
        or target.processo_id != binding.processo_id
    ):
        raise ValidacaoError("a referência pertence a outra sessão, grau ou processo")


async def _goto_audited(page: Page, url: str, grau: Grau, numero: str | None = None) -> None:
    if not _is_same_degree_url(url, grau):
        raise InterfacePjeAlteradaError("a navegação autenticada não pertence ao grau esperado")
    response = await page.goto(url, wait_until="domcontentloaded")
    if response is not None:
        if response.status >= 400:
            raise ServicoIndisponivelError(
                f"a interface autenticada do TJPE respondeu HTTP {response.status}"
            )
        if response.request.redirected_from is not None:
            raise CredenciaisAusentesError(
                "a sessão foi redirecionada durante a navegação autenticada"
            )
    if not _is_same_degree_url(page.url, grau):
        raise CredenciaisAusentesError("a sessão saiu do domínio e grau autenticados")
    await page.wait_for_timeout(250)
    body = await page.locator("body").inner_text()
    if numero is not None and numero not in body:
        raise InterfacePjeAlteradaError("a tela autenticada não confirmou o NPU selecionado")
    if await page.locator("#kc-form-login, #kc-pje-office, input[name='username']").count():
        raise CredenciaisAusentesError("a sessão autenticada expirou")


def _is_same_degree_url(url: str, grau: Grau) -> bool:
    if not url.isascii() or any(character in url for character in "\r\n\\"):
        return False
    parsed = urlparse(url)
    decoded = unquote(parsed.path)
    decoded_twice = unquote(decoded)
    unsafe_path = any(
        ".." in candidate
        or "\\" in candidate
        or "//" in candidate
        or any(character.isspace() or ord(character) < 32 for character in candidate)
        for candidate in (decoded, decoded_twice)
    )
    return (
        parsed.scheme == "https"
        and parsed.netloc == "pje.cloud.tjpe.jus.br"
        and decoded.startswith(f"/{grau.value}/")
        and not unsafe_path
        and "login" not in decoded.casefold()
    )


def _post_target(url: str, grau: Grau) -> str:
    if not _is_same_degree_url(url, grau):
        raise InterfacePjeAlteradaError("o destino POST não pertence ao grau autenticado")
    parsed = urlparse(url)
    if parsed.username is not None or parsed.password is not None:
        raise InterfacePjeAlteradaError("o destino POST possui credenciais embutidas")
    try:
        port = parsed.port
    except ValueError:
        port = -1
    if port not in {None, 443}:
        raise InterfacePjeAlteradaError("o destino POST usa uma porta não permitida")
    return parsed._replace(fragment="").geturl()


def _same_post_target(candidate: str, expected: str) -> bool:
    left = urlparse(candidate)._replace(fragment="")
    right = urlparse(expected)._replace(fragment="")
    return left == right


def _validated_post_marker(marker: str) -> str:
    compact = marker.strip()
    if (
        not 1 <= len(compact) <= 200
        or not compact.isascii()
        or re.fullmatch(r"[A-Za-z0-9_.:-]+", compact) is None
    ):
        raise InterfacePjeAlteradaError("o controle POST não possui identificador auditável")
    return compact


def _post_matches_contract(
    data: str | None,
    marker: str,
    required_fields: dict[str, str],
    *,
    strict: bool,
) -> bool:
    if data is None or len(data) > 1024 * 1024:
        return False
    try:
        fields = parse_qs(data, keep_blank_values=True, strict_parsing=False)
    except ValueError:
        return False
    direct_marker = fields.get(marker)
    source_marker = fields.get("javax.faces.source")
    marker_present = (direct_marker is not None and len(direct_marker) == 1) or source_marker == [
        marker
    ]
    if not marker_present:
        return False
    if not all(fields.get(name) == [value] for name, value in required_fields.items()):
        return False
    if not strict:
        return True
    expected = set(required_fields)
    if direct_marker is not None:
        expected.add(marker)
    if source_marker is not None:
        expected.add("javax.faces.source")
    return set(fields) == expected


async def _unique_control(root: Page | Locator, label: str) -> Locator:
    token = secrets.token_urlsafe(12)
    selector = (
        "button, a, input[type='button'], input[type='submit'], [role='button'], [role='link']"
    )
    payload = await root.locator(selector).evaluate_all(
        r"""
        (elements, args) => {
          const compact = (value) => (value || '')
            .replace(/\s+/g, ' ')
            .trim()
            .toLocaleLowerCase('pt-BR');
          const expected = compact(args.label);
          const visible = elements.filter((element) => {
            const style = window.getComputedStyle(element);
            if (
              element.getClientRects().length === 0 ||
              style.visibility === 'hidden' ||
              style.display === 'none'
            ) return false;
            if (element.disabled || element.getAttribute('aria-disabled') === 'true') return false;
            const values = [
              element.innerText,
              element.textContent,
              element.getAttribute('aria-label'),
              element.getAttribute('title'),
              element.value,
            ];
            return values.some((value) => compact(value) === expected);
          });
          for (const element of elements) element.removeAttribute('data-mcp-pjedocs-control');
          if (visible.length === 1) visible[0].setAttribute('data-mcp-pjedocs-control', args.token);
          return {count: visible.length};
        }
        """,
        {"label": label, "token": token},
    )
    raw = cast(dict[str, object], payload)
    if raw.get("count") != 1:
        raise InterfacePjeAlteradaError(
            f"a interface não apresentou um único controle ativo com o rótulo {label!r}"
        )
    locator = root.locator(f'[data-mcp-pjedocs-control="{token}"]')
    if await locator.count() != 1:
        raise InterfacePjeAlteradaError("o controle reconhecido mudou antes da interação")
    return locator


async def _click_and_resolve_page(
    context: BrowserContext,
    current: Page,
    control: Locator,
    grau: Grau,
    *,
    wait_ms: int,
) -> Page:
    pages_before = {id(page) for page in context.pages if not page.is_closed()}
    await control.click()
    await current.wait_for_timeout(wait_ms)
    candidates = [
        page for page in context.pages if not page.is_closed() and id(page) not in pages_before
    ]
    if len(candidates) > 1:
        raise InterfacePjeAlteradaError("o PJe abriu mais de uma janela inesperadamente")
    selected = candidates[0] if candidates else current
    if selected.url != "about:blank" and not _is_same_degree_url(selected.url, grau):
        raise InterfacePjeAlteradaError("o PJe abriu uma janela fora do grau autenticado")
    return selected


async def _open_area_download(
    ui: _UiSession,
    config: Settings,
    grau: Grau,
) -> _AreaSnapshot:
    panel = f"{config.urls.pje_base(grau.value)}/Painel/painel_usuario/advogado.seam"
    await _goto_audited(ui.page, panel, grau)
    menu = await _unique_control(ui.page, "Download")
    page = await _click_and_resolve_page(
        ui.context,
        ui.page,
        menu,
        grau,
        wait_ms=250,
    )
    area = await _unique_control(page, "Área de download")
    page = await _click_and_resolve_page(
        ui.context,
        page,
        area,
        grau,
        wait_ms=500,
    )
    ui.page = page
    if not _is_same_degree_url(page.url, grau):
        raise InterfacePjeAlteradaError("a Área de download saiu do grau autenticado")
    body = _compact(await page.locator("body").inner_text())
    if "área de download" not in body.casefold():
        raise InterfacePjeAlteradaError("o PJe não apresentou a Área de download esperada")
    return await _extract_area_snapshot(page)


async def _extract_area_snapshot(page: Page) -> _AreaSnapshot:
    raw = await page.locator("table").evaluate_all(
        r"""
        (tables) => {
          const fold = (value) => (value || '')
            .normalize('NFD')
            .replace(/[\u0300-\u036f]/g, '')
            .replace(/\s+/g, ' ')
            .trim()
            .toLocaleLowerCase('pt-BR');
          const visible = (element) => {
            const style = window.getComputedStyle(element);
            return (
              element.getClientRects().length > 0 &&
              style.visibility !== 'hidden' &&
              style.display !== 'none'
            );
          };
          const matches = [];
          for (const table of tables) {
            if (!visible(table)) continue;
            const headerCells = [...table.querySelectorAll('thead th')];
            const headers = headerCells.map((cell) => fold(cell.innerText || cell.textContent));
            const nameIndex = headers.indexOf('nome do arquivo');
            const expirationIndex = headers.indexOf('expiracao');
            if (nameIndex < 0 || expirationIndex < 0) continue;
            const rows = [];
            for (const tr of table.querySelectorAll('tbody tr')) {
              if (!visible(tr)) continue;
              const cells = [...tr.querySelectorAll(':scope > td')];
              if (!cells.length) continue;
              const controls = [...tr.querySelectorAll(
                'a, button, [role="link"], [role="button"]'
              )].filter((element) => {
                if (
                  !visible(element) ||
                  element.disabled ||
                  element.getAttribute('aria-disabled') === 'true'
                ) return false;
                const label = fold(
                  element.innerText ||
                  element.textContent ||
                  element.getAttribute('aria-label') ||
                  element.getAttribute('title')
                );
                return label === 'download';
              });
              const link = controls.length === 1 ? controls[0] : null;
              rows.push({
                name: (
                  cells[nameIndex]?.innerText ||
                  cells[nameIndex]?.textContent || ''
                ).trim().slice(0, 1000),
                expiration: (
                  cells[expirationIndex]?.innerText ||
                  cells[expirationIndex]?.textContent || ''
                ).trim().slice(0, 200),
                context: (tr.innerText || tr.textContent || '').trim().slice(0, 3000),
                linkCount: controls.length,
                href: link?.getAttribute('href') || '',
                absoluteHref: link && 'href' in link ? link.href || '' : '',
                onclick: link?.getAttribute('onclick') || '',
              });
            }
            matches.push({rows});
          }
          return matches;
        }
        """
    )
    tables = cast(list[dict[str, object]], raw)
    if len(tables) != 1:
        raise InterfacePjeAlteradaError(
            "a Área de download não apresentou uma única tabela com os cabeçalhos oficiais"
        )
    raw_rows = tables[0].get("rows")
    if not isinstance(raw_rows, list):
        raise InterfacePjeAlteradaError("a tabela da Área de download possui estrutura inválida")
    rows: list[_AreaRow] = []
    keys: set[str] = set()
    for item in cast(list[dict[str, object]], raw_rows):
        name = _compact(str(item.get("name", "")))
        expiration = _compact(str(item.get("expiration", "")))
        if not name:
            raise InterfacePjeAlteradaError("a Área de download contém uma linha sem nome")
        key = hashlib.sha256(f"{name}\0{expiration}".encode()).hexdigest()
        if key in keys:
            raise InterfacePjeAlteradaError("a Área de download contém linhas indistinguíveis")
        keys.add(key)
        link_count = item.get("linkCount")
        if not isinstance(link_count, int) or link_count < 0:
            raise InterfacePjeAlteradaError("a linha do PJeDocs possui controles inválidos")
        expires_at = _parse_expiration(expiration)
        if expiration and expires_at is None:
            raise InterfacePjeAlteradaError(
                "a Área de download apresentou uma expiração em formato desconhecido"
            )
        rows.append(
            _AreaRow(
                key=key,
                name=name,
                expiration_text=expiration,
                context=_compact(str(item.get("context", ""))),
                expires_at=expires_at,
                link_count=link_count,
                literal_href=str(item.get("href", "")),
                absolute_href=str(item.get("absoluteHref", "")),
                onclick=str(item.get("onclick", "")),
            )
        )
    text = await page.locator("body").inner_text()
    partial = any(
        int(match.group("atual")) < int(match.group("total"))
        for match in _PAGINATION.finditer(text)
    )
    structural_pagination = cast(
        dict[str, int],
        await page.locator(
            "[rel='next'], .ui-paginator-next, .p-paginator-next, "
            "[aria-label='Próxima'], [aria-label='Próximo']"
        ).evaluate_all(
            r"""
            (elements) => {
              const enabled = elements.filter((element) => {
                const style = window.getComputedStyle(element);
                const classes = (element.className || '').toString();
                return (
                  element.getClientRects().length > 0 &&
                  style.visibility !== 'hidden' &&
                  style.display !== 'none' &&
                  !element.disabled &&
                  element.getAttribute('aria-disabled') !== 'true' &&
                  !/(?:^|\s)(?:ui-state-disabled|p-disabled|disabled)(?:\s|$)/.test(classes)
                );
              });
              const pageLinks = [...document.querySelectorAll(
                '.ui-paginator-pages a, .p-paginator-pages button'
              )].filter((element) => {
                const style = window.getComputedStyle(element);
                return (
                  element.getClientRects().length > 0 &&
                  style.visibility !== 'hidden' &&
                  style.display !== 'none'
                );
              });
              return {enabledNext: enabled.length, pageLinks: pageLinks.length};
            }
            """
        ),
    )
    partial = (
        partial
        or structural_pagination.get("enabledNext", 0) > 0
        or structural_pagination.get("pageLinks", 0) > 1
    )
    return _AreaSnapshot(rows=tuple(rows), partial=partial)


def _parse_expiration(value: str) -> datetime | None:
    raw = _compact(value)
    timezone = ZoneInfo("America/Recife")
    for pattern in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(raw, pattern).replace(tzinfo=timezone).astimezone(UTC)
        except ValueError:
            pass
    try:
        parsed_date = datetime.strptime(raw, "%d/%m/%Y").date()
    except ValueError:
        return None
    return datetime.combine(parsed_date, time.max, timezone).astimezone(UTC)


async def _integral_form(page: Page) -> Locator:
    markers = (
        "Tipo de documento",
        "ID a partir de",
        "Cronologia",
        "Incluir expediente",
        "Incluir movimentos",
    )
    token = secrets.token_urlsafe(12)
    for selector in ("form", "[role='dialog']", ".ui-dialog", ".modal"):
        payload = await page.locator(selector).evaluate_all(
            r"""
            (elements, args) => {
              const fold = (value) => (value || '')
                .normalize('NFD')
                .replace(/[\u0300-\u036f]/g, '')
                .replace(/\s+/g, ' ')
                .trim()
                .toLocaleLowerCase('pt-BR');
              const expected = args.markers.map(fold);
              const visible = elements.filter((element) => {
                const style = window.getComputedStyle(element);
                if (
                  element.getClientRects().length === 0 ||
                  style.visibility === 'hidden' ||
                  style.display === 'none'
                ) return false;
                const text = fold(element.innerText || element.textContent);
                return expected.every((marker) => text.includes(marker));
              });
              for (const element of elements) element.removeAttribute('data-mcp-pjedocs-form');
              if (visible.length === 1) {
                visible[0].setAttribute('data-mcp-pjedocs-form', args.token);
              }
              return {count: visible.length};
            }
            """,
            {"markers": list(markers), "token": token},
        )
        result = cast(dict[str, object], payload)
        if result.get("count") == 1:
            return page.locator(f'[data-mcp-pjedocs-form="{token}"]')
        if isinstance(result.get("count"), int) and cast(int, result["count"]) > 1:
            raise InterfacePjeAlteradaError(
                "o PJe apresentou mais de um formulário compatível com o PJeDocs"
            )
    raise InterfacePjeAlteradaError("o formulário integral do PJeDocs não foi reconhecido")


async def _assert_no_warning(page: Page) -> None:
    visible_texts = await page.locator(
        "[role='alert'], [role='dialog'], .ui-messages-error, .ui-messages-warn, "
        ".alert-warning, .alert-danger, .modal"
    ).evaluate_all(
        r"""
        (elements) => elements.filter((element) => {
          const style = window.getComputedStyle(element);
          return (
            element.getClientRects().length > 0 &&
            style.visibility !== 'hidden' &&
            style.display !== 'none'
          );
        }).map((element) => (element.innerText || element.textContent || '').trim().slice(0, 3000))
        """
    )
    body_text = await page.locator("body").inner_text()
    if _WARNING.search(body_text) or any(
        _WARNING.search(str(text)) for text in cast(list[object], visible_texts)
    ):
        raise ValidacaoError(
            "o PJe apresentou aviso de ciência, permissão ou acesso restrito; a ação foi bloqueada"
        )


async def _configure_integral_form(form: Locator) -> None:
    await _validate_integral_form(form, require_no=False)
    await _select_no(form, "Incluir expediente")
    await _select_no(form, "Incluir movimentos")
    await _validate_integral_form(form, require_no=True)


async def _validate_integral_form(form: Locator, *, require_no: bool) -> dict[str, str]:
    payload = cast(
        dict[str, object],
        await form.evaluate(
            r"""
            (form, requireNo) => {
              const fold = (value) => (value || '')
                .normalize('NFD')
                .replace(/[\u0300-\u036f]/g, '')
                .replace(/\s+/g, ' ')
                .trim()
                .replace(/:\s*$/, '')
                .toLocaleLowerCase('pt-BR');
              const labelText = (label) => {
                const clone = label.cloneNode(true);
                for (const child of clone.querySelectorAll('input, select, textarea')) {
                  child.remove();
                }
                return clone.textContent || '';
              };
              const controls = [...form.elements]
                .filter((element) => ['input', 'select', 'textarea'].includes(
                  element.tagName.toLowerCase()
                ))
                .map(
                (element) => {
                  const labels = [...(element.labels || [])].map(labelText);
                  const aria = element.getAttribute('aria-label') || '';
                  if (aria) labels.push(aria);
                  const labelledBy = (element.getAttribute('aria-labelledby') || '')
                    .split(/\s+/).filter(Boolean);
                  for (const id of labelledBy) {
                    labels.push(document.getElementById(id)?.textContent || '');
                  }
                  return {
                    element,
                    labels: labels.map(fold),
                    tag: element.tagName.toLowerCase(),
                    type: (element.type || '').toLowerCase(),
                    name: element.getAttribute('name') || '',
                    value: (element.value || '').trim(),
                    selected: fold(element.selectedOptions?.[0]?.text || ''),
                    disabled: Boolean(element.disabled),
                    checked: Boolean(element.checked),
                    multiple: Boolean(element.multiple),
                  };
                }
              );
              const named = (name) => controls.filter((item) => item.labels.includes(name));
              const expected = {
                'tipo de documento': 1,
                'id a partir de': 1,
                'ate': 2,
                'periodo de': 1,
                'cronologia': 1,
                'incluir expediente': 1,
                'incluir movimentos': 1,
              };
              const countsOk = Object.entries(expected).every(
                ([name, count]) => named(name).length === count
              );
              const documentType = named('tipo de documento')[0];
              const chronology = named('cronologia')[0];
              const filters = [
                ...named('id a partir de'),
                ...named('ate'),
                ...named('periodo de'),
              ];
              const expedition = named('incluir expediente')[0];
              const movements = named('incluir movimentos')[0];
              const audited = [
                documentType,
                ...filters,
                chronology,
                expedition,
                movements,
              ].filter(Boolean);
              const successful = controls.filter((item) => {
                if (!item.name || item.disabled) return false;
                if (item.tag === 'input') {
                  if (['button', 'submit', 'reset', 'image', 'file'].includes(item.type)) {
                    return false;
                  }
                  if (['checkbox', 'radio'].includes(item.type) && !item.checked) return false;
                }
                return true;
              });
              const hidden = successful.filter(
                (item) => item.tag === 'input' && item.type === 'hidden'
              );
              const ordinary = successful.filter(
                (item) => !(item.tag === 'input' && item.type === 'hidden')
              );
              const submitted = [...audited, ...hidden];
              const fieldNames = submitted.map((item) => item.name);
              return {
                tagOk: form.tagName.toLowerCase() === 'form',
                countsOk,
                documentOk: (
                  documentType?.tag === 'select' &&
                  documentType?.selected === 'selecione'
                ),
                chronologyOk: chronology?.tag === 'select' && !chronology?.multiple,
                filtersOk: filters.length === 4 && filters.every(
                  (item) => item.tag === 'input' && item.value === ''
                ),
                noOk: !requireNo || (
                  expedition?.tag === 'select' && !expedition?.multiple &&
                  expedition?.selected === 'nao' && movements?.tag === 'select' &&
                  !movements?.multiple && movements?.selected === 'nao'
                ),
                fieldsOk: (
                  audited.length === 8 &&
                  audited.every((item) => successful.includes(item)) &&
                  ordinary.length === 8 &&
                  ordinary.every((item) => audited.includes(item)) &&
                  submitted.length <= 128 &&
                  fieldNames.every((name) => /^[A-Za-z0-9_.:-]{1,200}$/.test(name)) &&
                  new Set(fieldNames).size === fieldNames.length &&
                  submitted.every((item) => item.value.length <= 65536)
                ),
                fields: submitted.map((item) => ({name: item.name, value: item.value})),
              };
            }
            """,
            require_no,
        ),
    )
    if not all(
        payload.get(key) is True
        for key in (
            "tagOk",
            "countsOk",
            "documentOk",
            "chronologyOk",
            "filtersOk",
            "noOk",
            "fieldsOk",
        )
    ):
        raise InterfacePjeAlteradaError(
            "o formulário integral não preservou todos os campos e valores auditados"
        )
    raw_fields = payload.get("fields")
    if not isinstance(raw_fields, list):
        raise InterfacePjeAlteradaError("o formulário integral não expôs seus campos auditados")
    result: dict[str, str] = {}
    for item in cast(list[dict[str, object]], raw_fields):
        name = item.get("name")
        value = item.get("value")
        if not isinstance(name, str) or not isinstance(value, str) or name in result:
            raise InterfacePjeAlteradaError("os campos auditados do PJeDocs são ambíguos")
        result[name] = value
    if not 8 <= len(result) <= 128:
        raise InterfacePjeAlteradaError(
            "o PJeDocs não preservou seus oito campos e o estado oculto auditado"
        )
    return result


async def _form_post_target(form: Locator, page: Page, grau: Grau) -> str:
    payload = cast(
        dict[str, str],
        await form.evaluate(
            """
            element => ({
              tag: element.tagName.toLowerCase(),
              method: element.method?.toLowerCase() || '',
              enctype: element.enctype?.toLowerCase() || '',
              literal: element.getAttribute('action') || '',
              absolute: element.action || ''
            })
            """
        ),
    )
    if (
        payload.get("tag") != "form"
        or payload.get("method") != "post"
        or payload.get("enctype") != "application/x-www-form-urlencoded"
        or not payload.get("literal")
    ):
        raise InterfacePjeAlteradaError(
            "o formulário PJeDocs não possui POST urlencoded e action literal auditáveis"
        )
    action = payload.get("absolute", "")
    if not action:
        action = urljoin(page.url, payload["literal"])
    return _post_target(action, grau)


async def _control_post_marker(control: Locator) -> str:
    payload = cast(
        dict[str, object],
        await control.evaluate(
            """
            element => {
              const tag = element.tagName.toLowerCase();
              const type = (element.type || '').toLowerCase();
              const form = element.form;
              return {
                valid: (
                  (tag === 'button' || tag === 'input') &&
                  type === 'submit' &&
                  form instanceof HTMLFormElement &&
                  form.hasAttribute('data-mcp-pjedocs-form')
                ),
                marker: element.getAttribute('name') || element.id || ''
              };
            }
            """
        ),
    )
    if payload.get("valid") is not True or not isinstance(payload.get("marker"), str):
        raise InterfacePjeAlteradaError(
            "DOWNLOAD não é um submit nativo associado ao formulário auditado"
        )
    return _validated_post_marker(cast(str, payload["marker"]))


async def _control_post_permission(
    control: Locator,
    page: Page,
    grau: Grau,
) -> tuple[str, str, dict[str, str]] | None:
    payload = cast(
        dict[str, object],
        await control.evaluate(
            """
            element => {
              const form = element.form || element.closest('form');
              return {
                tag: element.tagName.toLowerCase(),
                href: element.getAttribute('href') || '',
                absoluteHref: element.href || '',
                onclick: element.getAttribute('onclick') || '',
                marker: element.getAttribute('name') || element.id || '',
                formMethod: form?.method?.toLowerCase() || '',
                formEnctype: form?.enctype?.toLowerCase() || '',
                formLiteral: form?.getAttribute('action') || '',
                formAbsolute: form?.action || '',
                fields: form ? [...new FormData(form).entries()] : []
              };
            }
            """
        ),
    )
    href = str(payload.get("href", "")).strip()
    if payload.get("tag") == "a" and href and not href.startswith(("#", "javascript:")):
        if str(payload.get("onclick", "")).strip():
            raise InterfacePjeAlteradaError(
                "o controle do PJeDocs mistura link literal e mutação JavaScript"
            )
        if not _is_same_degree_url(str(payload.get("absoluteHref", "")), grau):
            raise InterfacePjeAlteradaError("o controle do PJeDocs aponta para outro domínio")
        return None
    if (
        payload.get("formMethod") != "post"
        or payload.get("formEnctype") != "application/x-www-form-urlencoded"
        or not payload.get("formLiteral")
        or not payload.get("formAbsolute")
    ):
        raise InterfacePjeAlteradaError(
            "o controle do PJeDocs não pertence a um formulário POST auditável"
        )
    form_absolute = str(payload["formAbsolute"])
    form_literal = str(payload["formLiteral"])
    target = _post_target(form_absolute, grau)
    if not _same_post_target(target, urljoin(page.url, form_literal)):
        raise InterfacePjeAlteradaError("a action do controle PJeDocs mudou durante a inspeção")
    raw_fields = payload.get("fields")
    if not isinstance(raw_fields, list):
        raise InterfacePjeAlteradaError("o formulário de abertura não é auditável")
    raw_field_items = cast(list[object], raw_fields)
    if len(raw_field_items) > 128:
        raise InterfacePjeAlteradaError("o formulário de abertura não é auditável")
    fields: dict[str, str] = {}
    for raw_item in raw_field_items:
        if not isinstance(raw_item, list):
            raise InterfacePjeAlteradaError("o formulário de abertura possui campos inválidos")
        item = cast(list[object], raw_item)
        if len(item) != 2 or not all(isinstance(value, str) for value in item):
            raise InterfacePjeAlteradaError("o formulário de abertura possui campos inválidos")
        name, value = cast(tuple[str, str], tuple(item))
        if _validated_post_marker(name) != name or len(value) > 65536 or name in fields:
            raise InterfacePjeAlteradaError("o formulário de abertura possui campos ambíguos")
        fields[name] = value
    return target, _validated_post_marker(str(payload.get("marker", ""))), fields


async def _unique_labeled(form: Locator, label: str, *, required: bool) -> Locator | None:
    token = secrets.token_urlsafe(12)
    payload = cast(
        dict[str, object],
        await form.locator("input, select, textarea").evaluate_all(
            r"""
            (elements, args) => {
              const fold = (value) => (value || '')
                .normalize('NFD')
                .replace(/[\u0300-\u036f]/g, '')
                .replace(/\s+/g, ' ')
                .trim()
                .toLocaleLowerCase('pt-BR');
              const labelText = (label) => {
                const clone = label.cloneNode(true);
                for (const control of clone.querySelectorAll('input, select, textarea')) {
                  control.remove();
                }
                return clone.textContent || '';
              };
              const names = (element) => {
                const result = [element.getAttribute('aria-label') || ''];
                for (const owner of element.labels || []) result.push(labelText(owner));
                const labelledBy = (element.getAttribute('aria-labelledby') || '')
                  .split(/\s+/)
                  .filter(Boolean);
                for (const id of labelledBy) {
                  result.push(document.getElementById(id)?.textContent || '');
                }
                return result;
              };
              const expected = fold(args.label);
              const candidates = elements.filter((element) => (
                !element.disabled &&
                element.getAttribute('aria-disabled') !== 'true' &&
                names(element).some((name) => fold(name) === expected)
              ));
              for (const element of elements) {
                element.removeAttribute('data-mcp-pjedocs-field');
              }
              if (candidates.length === 1) {
                candidates[0].setAttribute('data-mcp-pjedocs-field', args.token);
              }
              return {count: candidates.length};
            }
            """,
            {"label": label, "token": token},
        ),
    )
    count = payload.get("count")
    if count == 0 and not required:
        return None
    if count != 1:
        raise InterfacePjeAlteradaError(
            f"o formulário não apresentou um único campo rotulado {label!r}"
        )
    locator = form.locator(f'[data-mcp-pjedocs-field="{token}"]')
    if await locator.count() != 1:
        raise InterfacePjeAlteradaError("o campo reconhecido mudou antes da interação")
    return locator


async def _select_no(form: Locator, label: str) -> None:
    control = await _unique_labeled(form, label, required=True)
    assert control is not None
    if await control.evaluate("element => element.tagName.toLowerCase()") != "select":
        raise InterfacePjeAlteradaError(f"o campo {label!r} não é um seletor nativo auditável")
    options = cast(
        list[dict[str, str]],
        await control.evaluate(
            """
            element => [...element.options].map(option => ({
              value: option.value,
              text: option.text
            }))
            """
        ),
    )
    matches = [option for option in options if _fold(option.get("text", "")) == "nao"]
    if len(matches) != 1:
        raise InterfacePjeAlteradaError(f"o campo {label!r} não possui uma única opção Não")
    await control.select_option(value=matches[0]["value"])
    selected = cast(
        str,
        await control.evaluate("element => element.selectedOptions[0]?.text || ''"),
    )
    if _fold(selected) != "nao":
        raise InterfacePjeAlteradaError(f"o campo {label!r} não permaneceu configurado como Não")


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return _compact(normalized).casefold()


async def _submission_succeeded(page: Page, dialogs: list[str]) -> bool:
    compact_dialogs = [_compact(message).casefold() for message in dialogs]
    if any(message != _SUCCESS_COMPACT for message in compact_dialogs):
        return False
    if _SUCCESS_COMPACT in compact_dialogs:
        return True
    return await _success_in_body(page)


async def _success_in_body(page: Page) -> bool:
    body = _compact(await page.locator("body").inner_text()).casefold()
    return _SUCCESS_COMPACT in body


def _correlated_rows(snapshot: _AreaSnapshot, request: _RequestBinding) -> list[_AreaRow]:
    candidates: list[_AreaRow] = []
    for row in snapshot.rows:
        if row.key in request.baseline:
            continue
        if _row_matches_npu(row, request.numero):
            candidates.append(row)
    return candidates


def _row_matches_npu(row: _AreaRow, numero: str) -> bool:
    tokens = _NPU_TOKEN.findall(f"{row.name} {row.context}")
    if not tokens:
        return False
    normalized: set[str] = set()
    for token in tokens:
        try:
            normalized.add(normalize_npu_tjpe(token))
        except ValidacaoError:
            return False
    return normalized == {numero}


def _row_fingerprint(row: _AreaRow) -> str:
    normalized = "\0".join(
        (
            _compact(row.name),
            _compact(row.expiration_text),
            _compact(row.context),
            str(row.link_count),
        )
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _validated_download_url(row: _AreaRow, grau: Grau) -> str:
    if row.link_count != 1:
        raise InterfacePjeAlteradaError("o resultado não possui um único link Download")
    literal = row.literal_href.strip()
    if (
        not literal
        or literal.startswith(("#", "javascript:", "data:", "blob:"))
        or row.onclick.strip()
    ):
        raise InterfacePjeAlteradaError("o resultado não expôs um link HTTPS literal")
    url = row.absolute_href.strip() or urljoin(
        f"https://pje.cloud.tjpe.jus.br/{grau.value}/", literal
    )
    if (
        len(url) > 8_192
        or not url.isascii()
        or any(character.isspace() or ord(character) < 32 for character in url)
    ):
        raise InterfacePjeAlteradaError("o link renovável do PJeDocs possui formato inseguro")
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError:
        port = -1
    decoded = unquote(parsed.path)
    decoded_twice = unquote(decoded)
    segments = decoded.split("/")
    segments_twice = decoded_twice.split("/")
    unsafe_decoded = any(
        character.isspace() or ord(character) < 32
        for candidate in (decoded, decoded_twice)
        for character in candidate
    )
    if (
        parsed.scheme != "https"
        or parsed.hostname != "pje.cloud.tjpe.jus.br"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or not decoded.startswith(f"/{grau.value}/")
        or ".." in segments
        or ".." in segments_twice
        or "\\" in decoded
        or "\\" in decoded_twice
        or "//" in decoded
        or "//" in decoded_twice
        or unsafe_decoded
        or parsed.params
        or parsed.fragment
    ):
        raise InterfacePjeAlteradaError(
            "o link renovável do PJeDocs não pertence ao host e grau permitidos"
        )
    return url


async def _scoped_cookies(context: BrowserContext, url: str) -> list[dict[str, object]]:
    raw = await context.cookies([url])
    result: list[dict[str, object]] = []
    for cookie in raw:
        name = cookie.get("name")
        value = cookie.get("value")
        if (
            not isinstance(name, str)
            or not isinstance(value, str)
            or _COOKIE_NAME.fullmatch(name) is None
            or not value.isascii()
            or any(character in value for character in "\r\n;")
        ):
            raise ServicoIndisponivelError(
                "a sessão contém um cookie que não pode ser isolado com segurança"
            )
        item: dict[str, object] = {
            "name": name,
            "value": value,
            "url": url,
            "httpOnly": bool(cookie.get("httpOnly", False)),
            "secure": True,
        }
        same_site = cookie.get("sameSite")
        if same_site in {"Strict", "Lax", "None"}:
            item["sameSite"] = same_site
        expires = cookie.get("expires")
        if isinstance(expires, int | float) and expires > 0:
            item["expires"] = expires
        result.append(item)
    if not result:
        raise CredenciaisAusentesError("a sessão autenticada não forneceu cookies ao PJeDocs")
    return result


def _stream_pjedocs_https(
    url: str,
    *,
    cookie_header: str,
    user_agent: str,
    max_bytes: int,
    timeout_seconds: float,
    destination: Path,
    numero: str,
) -> _DownloadedArtifact:
    if max_bytes < 1:
        raise ValidacaoError("o limite local do PJeDocs deve ser positivo")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc not in {
        "pje.cloud.tjpe.jus.br",
        "pje.cloud.tjpe.jus.br:443",
    }:
        raise InterfacePjeAlteradaError("o download do PJeDocs não pertence ao TJPE")
    if shutil.disk_usage(destination).free <= _DISK_RESERVE_BYTES:
        raise ServicoIndisponivelError("não há espaço livre seguro para o download")
    safe_user_agent = user_agent
    if (
        not safe_user_agent.isascii()
        or any(character in safe_user_agent for character in "\r\n")
        or len(safe_user_agent) > 512
    ):
        safe_user_agent = "Mozilla/5.0 mcp-pje-tjpe/0.6"
    target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    connection = http.client.HTTPSConnection("pje.cloud.tjpe.jus.br", 443, timeout=timeout_seconds)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".pjedocs-part-", dir=destination)
    temporary = Path(temporary_name)
    hasher = hashlib.sha256()
    total = 0
    prefix = bytearray()
    try:
        os.chmod(temporary, 0o600)
        connection.request(
            "GET",
            target,
            headers={
                "Accept-Encoding": "identity",
                "Cookie": cookie_header,
                "User-Agent": safe_user_agent,
            },
        )
        response = connection.getresponse()
        if response.status in {301, 302, 303, 307, 308}:
            raise ServicoIndisponivelError(
                "o TJPE tentou redirecionar o resultado; o redirecionamento não foi seguido"
            )
        if response.status == 401:
            raise CredenciaisAusentesError("a sessão expirou durante o download do PJeDocs")
        if response.status == 403:
            raise ValidacaoError("o TJPE recusou o resultado por permissão ou expiração")
        if response.status != 200:
            raise ServicoIndisponivelError(
                f"o download autenticado do PJeDocs respondeu HTTP {response.status}"
            )
        encoding = _compact(response.getheader("content-encoding", "") or "").casefold()
        if encoding not in {"", "identity"}:
            raise ServicoIndisponivelError(
                "o PJeDocs devolveu codificação de transporte não permitida"
            )
        length_header = response.getheader("content-length")
        declared: int | None = None
        if length_header is not None:
            try:
                declared = int(length_header)
            except ValueError:
                raise ServicoIndisponivelError(
                    "o PJeDocs devolveu Content-Length inválido"
                ) from None
            if declared < 0:
                raise ServicoIndisponivelError("o PJeDocs devolveu tamanho negativo")
            if declared > max_bytes:
                raise ValidacaoError(
                    "a íntegra excede PJE_TJPE_MAX_PJEDOCS_BYTES; nenhum fluxo "
                    "alternativo foi iniciado"
                )
            if shutil.disk_usage(destination).free <= declared + _DISK_RESERVE_BYTES:
                raise ServicoIndisponivelError("não há espaço livre seguro para a íntegra")

        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            while True:
                chunk = response.read(_DOWNLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValidacaoError(
                        "a íntegra excede PJE_TJPE_MAX_PJEDOCS_BYTES; o arquivo "
                        "parcial foi removido"
                    )
                if len(prefix) < 8_192:
                    prefix.extend(chunk[: 8_192 - len(prefix)])
                stream.write(chunk)
                hasher.update(chunk)
                if shutil.disk_usage(destination).free <= _DISK_RESERVE_BYTES:
                    raise ServicoIndisponivelError(
                        "o espaço livre seguro terminou durante o download da íntegra"
                    )
            stream.flush()
            os.fsync(stream.fileno())
        if declared is not None and total != declared:
            raise ServicoIndisponivelError(
                "o PJeDocs encerrou o download com tamanho diferente do declarado"
            )
        mime_type, extension = _validated_pjedocs_payload(bytes(prefix), total)
        digest = hasher.hexdigest()
        digits = re.sub(r"\D", "", numero)
        filename = f"integra-{digits}-{digest[:16]}{extension}"
        final = destination / filename
        _publish_streamed_file(temporary, final, digest)
        sidecar = final.with_suffix(final.suffix + ".sha256")
        atomic_publish(sidecar, f"{digest}  {final.name}\n".encode("ascii"))
        return _DownloadedArtifact(
            path=final,
            sidecar=sidecar,
            mime_type=mime_type,
            size=total,
            sha256=digest,
        )
    finally:
        connection.close()
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _validated_pjedocs_payload(prefix: bytes, total: int) -> tuple[str, str]:
    if total == 0:
        raise ServicoIndisponivelError("o PJeDocs devolveu um arquivo vazio")
    lowered = prefix.lower()
    if any(prefix.startswith(magic) for magic in _UNSAFE_MAGIC):
        raise ServicoIndisponivelError("o PJeDocs devolveu um formato executável")
    if any(marker in lowered for marker in _LOGIN_MARKERS) or b"<html" in lowered:
        raise CredenciaisAusentesError("a sessão expirou durante o download do PJeDocs")
    if prefix.startswith(b"%PDF-"):
        return "application/pdf", ".pdf"
    if prefix.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return "application/zip", ".zip"
    raise ServicoIndisponivelError(
        "o resultado do PJeDocs não possui assinatura reconhecida de PDF ou ZIP"
    )


def _publish_streamed_file(temporary: Path, target: Path, digest: str) -> None:
    if target.parent.is_symlink() or target.is_symlink():
        raise ServicoIndisponivelError("o destino do PJeDocs contém link simbólico inseguro")
    if target.exists():
        if (
            not target.is_file()
            or target.stat().st_size != temporary.stat().st_size
            or _sha256_file(target) != digest
        ):
            raise ServicoIndisponivelError(
                "já existe um arquivo diferente no destino calculado para a íntegra"
            )
        target.chmod(0o600)
        return
    try:
        os.link(temporary, target)
    except FileExistsError:
        if (
            target.is_symlink()
            or not target.is_file()
            or target.stat().st_size != temporary.stat().st_size
            or _sha256_file(target) != digest
        ):
            raise ServicoIndisponivelError(
                "o destino da íntegra foi ocupado por outro arquivo"
            ) from None
    target.chmod(0o600)


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()
