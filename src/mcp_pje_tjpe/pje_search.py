from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import unicodedata
from collections import OrderedDict
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import wraps
from typing import Any, ParamSpec, TypeVar, cast
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from playwright.async_api import (
    BrowserContext,
    Dialog,
    Locator,
    Page,
    Route,
    WebSocketRoute,
)

from mcp_pje_tjpe.adaptive.adapters import fold_semantic
from mcp_pje_tjpe.adaptive.controller import AdaptiveNavigation
from mcp_pje_tjpe.config import Settings
from mcp_pje_tjpe.errors import (
    CredenciaisAusentesError,
    InterfacePjeAlteradaError,
    PjeTjpeError,
    ServicoIndisponivelError,
    ValidacaoError,
)
from mcp_pje_tjpe.models import (
    AcessoPesquisaGeral,
    EstadoAcessoPesquisaGeral,
    Grau,
    PreparacaoPesquisaGeral,
)
from mcp_pje_tjpe.pje_auth import PjeSessionManager, ReadSessionLease
from mcp_pje_tjpe.pje_public import normalize_npu_tjpe
from mcp_pje_tjpe.pje_read import (
    PjeReadService,
    audited_autos_target,
    autos_process_id,
    is_same_degree_url,
)
from mcp_pje_tjpe.tribunals import TribunalCodigo

_REFERENCE = re.compile(r"^[A-Za-z0-9_-]{20,80}$")
_COOKIE_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_FIELD_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
_NPU_TOKEN = re.compile(r"(?<!\d)(?:\d{7}-\d{2}\.\d{4}\.8\.17\.\d{4}|\d{20})(?!\d)")
_PAGINATION = re.compile(r"\b(?P<atual>\d+)\s+de\s+(?P<total>\d+)\b", re.I)
_PLAN_TTL = timedelta(minutes=10)
_MAX_AUTOS_HTML_BYTES = 10 * 1024 * 1024
_MAX_REGISTRY_DEFAULT = 500
_SEARCH_WAIT_MS = 900
_OPEN_WAIT_MS = 5_000

_BLOCKED_ACTION = re.compile(
    r"(?:tomar\s+ci[eê]ncia|registrar\s+ci[eê]ncia|visualizar\s+expediente|"
    r"responder\s+expediente|intima[cç][aã]o\s+pendente|habilita[cç][aã]o|"
    r"protocol(?:ar|o)|assin(?:ar|atura)|acesso\s+negado|n[aã]o\s+possui\s+permiss[aã]o)",
    re.I,
)
_RESTRICTED_RESULT = re.compile(
    r"(?:processo\s+(?:sigiloso|(?:em\s+)?segredo\s+de\s+justi[cç]a)|"
    r"tramita(?:ndo)?\s+(?:em\s+)?segredo\s+de\s+justi[cç]a|acesso\s+restrito|"
    r"documento\s+sigiloso|segredo\s+de\s+justi[cç]a"
    r"(?!(?:\s|:)*(?:n[aã]o|false|no)\b))",
    re.I,
)
_ACCESS_DENIED = re.compile(
    r"(?:acesso\s+negado|n[aã]o\s+possui\s+permiss[aã]o|sem\s+permiss[aã]o|"
    r"acesso\s+n[aã]o\s+autorizado|usu[aá]rio\s+n[aã]o\s+autorizado)",
    re.I,
)
_FORBIDDEN_FORM_TERM = re.compile(
    r"(?:ciencia|expedient|intimac|habilit|peticion|protocol|assin|download|"
    r"gerar.?guia|emitir.?guia|custas|delete|excluir|remover|apagar|cancelar|"
    r"alterar|salvar|incluir|editar|enviar)",
    re.I,
)
_RESOLUTION_121 = re.compile(r"\bresolucao\b.{0,48}\b121\b", re.I)
_SAFE_SEARCH_MARKER = re.compile(
    r"^(?:(?:btn|botao|button|cmd|command)[_-]?)?"
    r"(?:pesquisar|pesquisa|consultar|buscar|search)"
    r"(?:[_-]?(?:processo|processos|btn|botao|button|command))?$",
    re.I,
)
# Sentinela do Seam para combo sem seleção. É textualmente não vazio, mas significa
# exatamente "nenhum critério escolhido"; tratá-lo como vazio preserva a invariante
# de "somente o NPU preenchido" em vez de enfraquecê-la.
_SEAM_NO_SELECTION_PREFIX = "org.jboss.seam.ui.NoSelectionConverter."

# Estado de widget RichFaces/Seam que a página envia junto sem ser critério de
# busca. Lista explícita e auditada, observada no formulário fPP do TJPE.
# Só estes podem vir COM valor, porque o valor é estado de UI e não critério:
#   tipomascaradocumento / habilitarmascara* -> máscara de digitação;
#   *inputcurrentdate -> mês exibido no popup do calendário; o filtro de data em
#                        si é *inputdate, que continua tratado como critério.
_WIDGET_STATE_FIELD = re.compile(
    r"^(?:tipomascaradocumento|habilitarmascara[a-z0-9]*|[a-z0-9_]*inputcurrentdate)$",
    re.I,
)
# Estes são tolerados APENAS vazios: com valor seriam critério de busca de fato
# (suggestion box selecionada) ou componente anônimo não auditável.
_WIDGET_STATE_FIELD_IF_EMPTY = re.compile(
    r"^(?:[a-z0-9_]*_selection|autoscroll|j_id\d+)$",
    re.I,
)

# Colisões de substring conhecidas e auditadas contra _FORBIDDEN_FORM_TERM. Nenhuma
# é ação processual; a lista é exaustiva e explícita, nunca um padrão genérico:
#   habilitarmascara*      -> máscara de digitação, não "habilitação nos autos";
#   numeroprotocolopolicia -> critério "nº do protocolo/BO da polícia", não
#                             "protocolar" peça.
_AUDITED_TERM_COLLISION = re.compile(
    r"^(?:habilitarmascara[a-z0-9]*|numeroprotocolopolicia)$",
    re.I,
)


def _is_seam_no_selection(value: str) -> bool:
    return value.strip().startswith(_SEAM_NO_SELECTION_PREFIX)


def _sanitized_link_shape(entry: dict[str, str]) -> str:
    """Descreve href/onclick sem devolver NPU, id de processo ou texto livre."""

    def shape(value: str) -> str:
        return re.sub(r"\d{3,}", "#", value)[:110] if value else "vazio"

    return (
        f"href={shape(entry.get('href', ''))!r} "
        f"onclick={shape(entry.get('onclick', ''))!r}"
    )


_SAFE_STATE_FIELD = re.compile(
    r"^(?:(?:javax|jakarta)\.faces\.(?:ViewState|ClientWindow)|"
    r"org\.jboss\.seam\.cid|cid|_?csrf(?:Token)?|csrf[-_]token|"
    r"conversation(?:Id|Propagation))$",
    re.I,
)

_P = ParamSpec("_P")
_T = TypeVar("_T")


def _compact(value: str) -> str:
    return " ".join(value.split())


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFD", value)
    return "".join(
        character for character in normalized if not unicodedata.combining(character)
    ).casefold()


def confirmation_phrase(numero: str, grau: Grau) -> str:
    formatted = normalize_npu_tjpe(numero)
    return (
        "CONFIRMO INTERESSE E POSSÍVEL REGISTRO DE ACESSO AO PROCESSO "
        f"{formatted} NO PJE-TJPE {grau.value.upper()}"
    )


def _sanitized_failure(
    message: str,
) -> Callable[[Callable[_P, Awaitable[_T]]], Callable[_P, Awaitable[_T]]]:
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


def _consume_background_exception(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        _ = task.exception()


class _PlanStatus(StrEnum):
    PREPARADO = "preparado"
    ABRINDO = "abrindo"
    ABERTO = "aberto"
    INDETERMINADO = "indeterminado"


@dataclass(slots=True)
class _Plan:
    reference: str
    generation: str = field(repr=False)
    grau: Grau
    numero: str
    processo_id: str = field(repr=False)
    fingerprint: str = field(repr=False)
    created_at: datetime
    expires_at: datetime
    status: _PlanStatus = _PlanStatus.PREPARADO
    access_reference: str | None = None


@dataclass(frozen=True, slots=True)
class _AccessBinding:
    reference: str
    plan_reference: str
    generation: str = field(repr=False)
    grau: Grau
    numero: str
    processo_id: str = field(repr=False)
    state: EstadoAcessoPesquisaGeral
    accessed_at: datetime


@dataclass(frozen=True, slots=True)
class _SearchResult:
    numero: str
    processo_id: str = field(repr=False)
    autos_url: str = field(repr=False)
    fingerprint: str = field(repr=False)
    anchor: Locator = field(repr=False)


@dataclass(frozen=True, slots=True)
class _FormContract:
    submit: Locator = field(repr=False)
    target: str = field(repr=False)
    marker: str = field(repr=False)
    marker_value: str = field(repr=False)
    fields: dict[str, str] = field(repr=False)


@dataclass(slots=True)
class _DialogState:
    numero: str
    allow_access_notice: bool = False
    accepted_access_notices: int = 0
    rejected: bool = False
    messages: list[str] = field(default_factory=list[str], repr=False)


@dataclass(slots=True)
class _SearchGate:
    grau: Grau
    allowed_post_target: str | None = field(default=None, repr=False)
    required_marker: str | None = field(default=None, repr=False)
    required_marker_value: str | None = field(default=None, repr=False)
    required_fields: dict[str, str] = field(default_factory=dict[str, str], repr=False)
    remaining_posts: int = 0
    search_post_count: int = 0
    opening_target: str | None = field(default=None, repr=False)
    remaining_autos_gets: int = 0
    autos_get_count: int = 0
    blocked_mutation: bool = False
    search_page_posts_open: bool = False
    search_page_post_count: int = 0
    blocked_detail: str | None = field(default=None, repr=False)

    # Registra a primeira requisição barrada em forma sanitizada, para que o erro
    # diga o que aconteceu em vez de só "fora da rota auditada". Sem query e com
    # sequências longas de dígitos mascaradas: nunca devolve NPU nem id de processo.
    def _record_block(self, method: str, resource_type: str, url: str) -> None:
        if self.blocked_detail is not None:
            return
        path = re.sub(r"\d{4,}", "#", unquote(urlparse(url).path))[:120]
        self.blocked_detail = f"{method} {resource_type} {path}"

    def permit_search_post(self, contract: _FormContract) -> None:
        if contract.marker in contract.fields:
            raise InterfacePjeAlteradaError("o marcador Pesquisar colide com um campo ordinário")
        self.allowed_post_target = _search_target(contract.target, self.grau)
        self.required_marker = _validated_marker(contract.marker)
        self.required_marker_value = _validated_marker_value(contract.marker_value)
        self.required_fields = dict(contract.fields)
        self.remaining_posts = 1

    def close_search_post(self) -> None:
        self.allowed_post_target = None
        self.required_marker = None
        self.required_marker_value = None
        self.required_fields.clear()
        self.remaining_posts = 0

    # RichFaces 3 (a4j) não carrega identidade da ação no corpo: não há
    # javax.faces.source, e o id do componente de origem é autogerado e instável
    # (fPP:j_id479). Além disso, a máscara do campo de NPU faz round-trip a4j
    # ao ser preenchida, então a página produz vários POSTs por design.
    # Nessa via a identidade vem do ELEMENTO auditado que clicamos (texto exato,
    # único, dentro do <form>, componente aprovado por _SAFE_SEARCH_MARKER) e não
    # do corpo. Continuam valendo: URL fixada da consulta, bloqueio de GET de
    # Autos, allowlist de rede e a exigência (validada em Python antes do clique)
    # de que somente o NPU esteja preenchido.
    # O alvo é fixado por ROTA, não por URL literal: o Seam acrescenta ?cid=NNN
    # (id de conversação) à página, e os POSTs a4j não repetem esse parâmetro.
    # _is_search_page_url valida esquema, host, porta e caminho e só admite cid.
    def permit_search_page_posts(self) -> None:
        self.search_page_posts_open = True

    def close_search_page_posts(self) -> None:
        self.search_page_posts_open = False

    def permit_autos_get(self, url: str) -> None:
        if autos_process_id(url, self.grau) is None:
            raise InterfacePjeAlteradaError("a abertura não apontou para Autos reconhecidos")
        self.opening_target = url
        self.remaining_autos_gets = 1

    def close_autos_get(self) -> None:
        self.opening_target = None
        self.remaining_autos_gets = 0

    async def handle(self, route: Route) -> None:
        request = route.request
        method = request.method.upper()
        if method in {"GET", "HEAD"}:
            process_id = autos_process_id(request.url, self.grau)
            if process_id is not None:
                if (
                    method == "GET"
                    and request.resource_type == "document"
                    and self.opening_target is not None
                    and self.remaining_autos_gets > 0
                    and _same_url(request.url, self.opening_target)
                ):
                    self.autos_get_count += 1
                    self.remaining_autos_gets = 0
                    await route.continue_()
                    return
                self.blocked_mutation = True
                self._record_block(method, request.resource_type, request.url)
                await route.abort("blockedbyclient")
                return
            if self.opening_target is not None:
                await route.abort("blockedbyclient")
                return
            if _allowed_search_get(request.url, request.resource_type, self.grau):
                await route.continue_()
                return
            await route.abort("blockedbyclient")
            return
        if (
            method == "POST"
            and self.allowed_post_target is not None
            and self.required_marker is not None
            and self.required_marker_value is not None
            and self.remaining_posts > 0
            and _same_url(request.url, self.allowed_post_target)
            and _post_matches_contract(
                request.post_data,
                self.required_marker,
                self.required_fields,
                marker_value=self.required_marker_value,
            )
        ):
            self.search_post_count += 1
            self.remaining_posts = 0
            await route.continue_()
            return
        # Via a4j: identidade vem do elemento clicado, não do corpo. Ver o
        # comentário em permit_search_page_posts.
        if (
            method == "POST"
            and self.search_page_posts_open
            and _is_search_page_url(request.url, self.grau)
        ):
            self.search_page_post_count += 1
            await route.continue_()
            return
        self.blocked_mutation = True
        self._record_block(method, request.resource_type, request.url)
        await route.abort("blockedbyclient")


@dataclass(slots=True)
class _UiSession:
    context: BrowserContext = field(repr=False)
    page: Page = field(repr=False)
    gate: _SearchGate
    dialogs: _DialogState
    observed_pages: list[Page] = field(default_factory=list[Page], repr=False)


class _OpeningUncertain(Exception):
    pass


class PjeSearchService:
    """Pesquisa geral por NPU exato e abertura confirmada com cache em memória."""

    def __init__(
        self,
        sessions: PjeSessionManager,
        read_service: PjeReadService,
        config: Settings,
        *,
        adaptive: AdaptiveNavigation | None = None,
        registry_limit: int = _MAX_REGISTRY_DEFAULT,
    ) -> None:
        if registry_limit < 1:
            raise ValueError("registry_limit deve ser positivo")
        self.sessions = sessions
        self.read = read_service
        self.config = config
        self.adaptive = adaptive
        self.registry_limit = registry_limit
        self._plans: OrderedDict[str, _Plan] = OrderedDict()
        self._fingerprints: dict[str, str] = {}
        self._accesses: OrderedDict[str, _AccessBinding] = OrderedDict()
        self._opening_tasks: dict[str, asyncio.Task[AcessoPesquisaGeral]] = {}
        self._state_lock = asyncio.Lock()

    @_sanitized_failure("não foi possível pesquisar o processo autenticado com segurança")
    async def preparar(self, numero: str, grau: Grau) -> PreparacaoPesquisaGeral:
        formatted = normalize_npu_tjpe(numero)
        async with self.sessions.read_session(grau) as lease:
            if self.read.possui_vinculo_acervo(lease, formatted):
                raise ValidacaoError(
                    "o processo já possui vínculo auditado no Acervo desta sessão e grau; "
                    "use consultar_autos para evitar uma abertura geral desnecessária"
                )
            async with self._interactive_session(lease, formatted) as ui:
                result = await self._search_exact(ui, formatted, grau)
            generation = lease.generation

        now = datetime.now(UTC)
        fingerprint = _plan_fingerprint(generation, grau, formatted, result)
        async with self._state_lock:
            previous_ref = self._fingerprints.get(fingerprint)
            previous = self._plans.get(previous_ref) if previous_ref else None
            if previous is not None and (
                previous.status != _PlanStatus.PREPARADO or previous.expires_at > now
            ):
                self._plans.move_to_end(previous.reference)
                return _preparation_model(previous)
            reference = secrets.token_urlsafe(24)
            plan = _Plan(
                reference=reference,
                generation=generation,
                grau=grau,
                numero=formatted,
                processo_id=result.processo_id,
                fingerprint=fingerprint,
                created_at=now,
                expires_at=now + _PLAN_TTL,
            )
            self._plans[reference] = plan
            self._fingerprints[fingerprint] = reference
            self._trim_registries()
            return _preparation_model(plan)

    @_sanitized_failure("não foi possível concluir a abertura autenticada com segurança")
    async def abrir(
        self,
        numero: str,
        grau: Grau,
        referencia_preparo: str,
        confirmacao: str,
    ) -> AcessoPesquisaGeral:
        formatted = normalize_npu_tjpe(numero)
        if confirmacao != confirmation_phrase(formatted, grau):
            raise ValidacaoError(
                "confirmação incorreta; envie em uma nova mensagem a frase literal "
                "devolvida pela preparação"
            )
        created = False
        async with self._state_lock:
            plan = self._resolve_plan(referencia_preparo, formatted, grau)
            if plan.access_reference is not None:
                binding = self._accesses.get(plan.access_reference)
                if binding is None:
                    raise ValidacaoError("a referência do acesso expirou localmente")
                self._accesses.move_to_end(binding.reference)
                return _access_model(binding, reused=True)
            _assert_plan_fresh(plan)
            task = self._opening_tasks.get(plan.reference)
            if task is None:
                if plan.status == _PlanStatus.ABRINDO:
                    binding = self._record_access_locked(
                        plan, EstadoAcessoPesquisaGeral.INDETERMINADO
                    )
                    return _access_model(binding, reused=True)
                plan.status = _PlanStatus.ABRINDO
                task = asyncio.create_task(self._run_opening(plan.reference))
                task.add_done_callback(_consume_background_exception)
                self._opening_tasks[plan.reference] = task
                created = True
        result = await asyncio.shield(task)
        return result if created else result.model_copy(update={"reutilizada": True})

    async def _run_opening(self, plan_reference: str) -> AcessoPesquisaGeral:
        try:
            async with self._state_lock:
                plan = self._plans.get(plan_reference)
                if plan is None:
                    raise ValidacaoError("a preparação expirou localmente")
            try:
                await self._open_once(plan)
                state = EstadoAcessoPesquisaGeral.ABERTO
            except _OpeningUncertain:
                state = EstadoAcessoPesquisaGeral.INDETERMINADO
            except BaseException:
                async with self._state_lock:
                    current = self._plans.get(plan_reference)
                    if current is not None and current.access_reference is None:
                        current.status = _PlanStatus.PREPARADO
                raise
            finalizer = asyncio.create_task(self._finalize_opening(plan_reference, state))
            finalizer.add_done_callback(_consume_background_exception)
            try:
                return await asyncio.shield(finalizer)
            except asyncio.CancelledError:
                await asyncio.shield(finalizer)
                raise
        finally:
            async with self._state_lock:
                current_task = asyncio.current_task()
                if self._opening_tasks.get(plan_reference) is current_task:
                    self._opening_tasks.pop(plan_reference, None)

    async def _finalize_opening(
        self,
        plan_reference: str,
        state: EstadoAcessoPesquisaGeral,
    ) -> AcessoPesquisaGeral:
        async with self._state_lock:
            plan = self._plans.get(plan_reference)
            if plan is None:
                raise ValidacaoError("a preparação expirou localmente")
            if plan.access_reference is not None:
                existing = self._accesses.get(plan.access_reference)
                if existing is None:
                    raise ValidacaoError("o acesso expirou localmente")
                return _access_model(existing, reused=True)
            binding = self._record_access_locked(plan, state)
            return _access_model(binding, reused=False)

    async def _open_once(self, plan: _Plan) -> None:
        maybe_clicked = False
        try:
            async with self.sessions.read_session(plan.grau) as lease:
                if lease.generation != plan.generation:
                    raise ValidacaoError("a preparação pertence a uma sessão autenticada anterior")
                async with self._interactive_session(lease, plan.numero) as ui:
                    result = await self._search_exact(ui, plan.numero, plan.grau)
                    if (
                        result.processo_id != plan.processo_id
                        or _plan_fingerprint(
                            lease.generation,
                            plan.grau,
                            plan.numero,
                            result,
                        )
                        != plan.fingerprint
                    ):
                        raise InterfacePjeAlteradaError(
                            "o resultado mudou desde a preparação; nenhuma abertura foi feita"
                        )
                    _assert_plan_fresh(plan)
                    ui.dialogs.allow_access_notice = True
                    ui.gate.permit_autos_get(result.autos_url)
                    try:
                        maybe_clicked = True
                        autos_page = await _click_exact_result(
                            ui,
                            result,
                            timeout_ms=min(self.config.timeout_ms, _OPEN_WAIT_MS),
                        )
                        ui.dialogs.allow_access_notice = False
                        if ui.dialogs.rejected or ui.gate.blocked_mutation:
                            raise InterfacePjeAlteradaError(
                                "o PJe apresentou interação diferente do acesso registrado esperado"
                            )
                        if ui.gate.autos_get_count != 1:
                            raise InterfacePjeAlteradaError(
                                "a confirmação não produziu exatamente um GET auditado dos Autos"
                            )
                        autos_html = await _validated_autos_html(
                            autos_page,
                            plan.numero,
                            plan.grau,
                            plan.processo_id,
                        )
                        if (
                            ui.dialogs.rejected
                            or ui.gate.blocked_mutation
                            or _has_unexpected_pages(ui, (ui.page, autos_page))
                        ):
                            raise InterfacePjeAlteradaError(
                                "o PJe apresentou interação tardia diferente da abertura confirmada"
                            )
                        self.read.registrar_autos_pesquisa_geral(
                            lease,
                            plan.numero,
                            plan.processo_id,
                            result.autos_url,
                            autos_html,
                        )
                    finally:
                        ui.dialogs.allow_access_notice = False
                        ui.gate.close_autos_get()
        except asyncio.CancelledError:
            if maybe_clicked:
                raise _OpeningUncertain from None
            raise
        except (
            CredenciaisAusentesError,
            InterfacePjeAlteradaError,
            ServicoIndisponivelError,
            ValidacaoError,
        ):
            if maybe_clicked:
                raise _OpeningUncertain from None
            raise
        except Exception:
            if maybe_clicked:
                raise _OpeningUncertain from None
            raise ServicoIndisponivelError(
                "não foi possível abrir os Autos pesquisados com segurança"
            ) from None

    async def _search_exact(self, ui: _UiSession, numero: str, grau: Grau) -> _SearchResult:
        search_url = _canonical_search_url(self.config, grau)
        # A janela abre antes do goto: a própria carga da consulta dispara POST a4j
        # (inicialização de combos) e, depois, a máscara do campo de NPU dispara
        # outro no preenchimento. Nenhum é mutação, e a janela só admite POST para
        # a rota auditada da consulta deste grau.
        ui.gate.permit_search_page_posts()
        try:
            return await self._search_exact_in_window(ui, numero, grau, search_url)
        finally:
            ui.gate.close_search_page_posts()

    async def _search_exact_in_window(
        self, ui: _UiSession, numero: str, grau: Grau, search_url: str
    ) -> _SearchResult:
        response = await ui.page.goto(search_url, wait_until="domcontentloaded")
        if response is not None:
            if response.status >= 400:
                raise ServicoIndisponivelError(
                    f"a pesquisa autenticada respondeu HTTP {response.status}"
                )
            if response.request.redirected_from is not None:
                raise CredenciaisAusentesError(
                    "a sessão foi redirecionada ao abrir a pesquisa autenticada"
                )
        if not _is_search_page_url(ui.page.url, grau):
            raise CredenciaisAusentesError("a sessão saiu da pesquisa autenticada do grau esperado")
        await _assert_no_blocking_notice(ui.page)
        if ui.dialogs.rejected or ui.dialogs.messages:
            raise InterfacePjeAlteradaError("o PJe apresentou diálogo antes da pesquisa por NPU")
        contract = await _configure_exact_npu_form(
            ui.page,
            numero,
            grau,
            adaptive=self.adaptive,
        )
        ui.gate.permit_search_post(contract)
        try:
            await contract.submit.click()
            await ui.page.wait_for_timeout(_SEARCH_WAIT_MS)
        finally:
            ui.gate.close_search_post()
        if ui.gate.blocked_mutation:
            raise InterfacePjeAlteradaError(
                "a pesquisa tentou uma requisição fora da rota consultiva auditada: "
                f"{ui.gate.blocked_detail or 'origem não registrada'}"
            )
        if (ui.gate.search_post_count + ui.gate.search_page_post_count) < 1:
            raise InterfacePjeAlteradaError(
                "Pesquisar não produziu nenhum POST consultivo à rota auditada da consulta"
            )
        if not _is_search_page_url(ui.page.url, grau):
            raise InterfacePjeAlteradaError(
                "a pesquisa navegou para uma rota diferente da consulta processual"
            )
        await _assert_no_blocking_notice(ui.page)
        if ui.dialogs.rejected or ui.dialogs.messages:
            raise InterfacePjeAlteradaError(
                "o PJe apresentou diálogo durante a pesquisa consultiva por NPU"
            )
        result = await _extract_exact_result(ui.page, numero, grau)
        if (
            ui.dialogs.rejected
            or ui.dialogs.messages
            or ui.gate.blocked_mutation
            or _has_unexpected_pages(ui, (ui.page,))
        ):
            raise InterfacePjeAlteradaError(
                "o PJe apresentou interação tardia durante a pesquisa consultiva"
            )
        return result

    @asynccontextmanager
    async def _interactive_session(
        self,
        lease: ReadSessionLease,
        numero: str,
    ) -> AsyncGenerator[_UiSession]:
        browser = lease.context.browser
        if browser is None:
            raise ServicoIndisponivelError(
                "o navegador autenticado não permite isolar a pesquisa geral"
            )
        base = self.config.urls.pje_base(lease.grau.value) + "/"
        cookies = await _scoped_cookies(lease.context, base)
        context = await browser.new_context(
            accept_downloads=False,
            locale="pt-BR",
            java_script_enabled=True,
            service_workers="block",
            viewport={"width": 1440, "height": 1000},
        )
        gate = _SearchGate(lease.grau)
        dialog_state = _DialogState(numero)
        observed_pages: list[Page] = []
        primary_page: Page | None = None

        async def handle_dialog(dialog: Dialog) -> None:
            message = _compact(dialog.message)[:2_000]
            dialog_state.messages.append(message)
            source_page = dialog.page
            authorized_source = source_page is not None and (
                source_page is primary_page
                or (
                    gate.opening_target is not None
                    and _same_url(source_page.url, gate.opening_target)
                )
            )
            if (
                dialog_state.allow_access_notice
                and dialog_state.accepted_access_notices == 0
                and authorized_source
                and _is_official_access_dialog(message, numero)
            ):
                dialog_state.accepted_access_notices += 1
                await dialog.accept()
                return
            dialog_state.rejected = True
            await dialog.dismiss()

        def attach_dialog_handler(page: Page) -> None:
            observed_pages.append(page)
            page.on("dialog", handle_dialog)

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
            primary_page = page
            page.set_default_timeout(self.config.timeout_ms)
            page.set_default_navigation_timeout(self.config.timeout_ms)
            yield _UiSession(
                context=context,
                page=page,
                gate=gate,
                dialogs=dialog_state,
                observed_pages=observed_pages,
            )
        finally:
            await context.close()

    def _resolve_plan(self, reference: str, numero: str, grau: Grau) -> _Plan:
        if _REFERENCE.fullmatch(reference) is None:
            raise ValidacaoError("referência de preparação inválida")
        plan = self._plans.get(reference)
        if plan is None:
            raise ValidacaoError("referência de preparação desconhecida ou expirada")
        if plan.numero != numero or plan.grau != grau:
            raise ValidacaoError("a preparação pertence a outro grau ou processo")
        self._plans.move_to_end(reference)
        return plan

    def _record_access_locked(
        self,
        plan: _Plan,
        state: EstadoAcessoPesquisaGeral,
    ) -> _AccessBinding:
        reference = secrets.token_urlsafe(24)
        binding = _AccessBinding(
            reference=reference,
            plan_reference=plan.reference,
            generation=plan.generation,
            grau=plan.grau,
            numero=plan.numero,
            processo_id=plan.processo_id,
            state=state,
            accessed_at=datetime.now(UTC),
        )
        self._accesses[reference] = binding
        plan.access_reference = reference
        plan.status = (
            _PlanStatus.ABERTO
            if state is EstadoAcessoPesquisaGeral.ABERTO
            else _PlanStatus.INDETERMINADO
        )
        self._trim_registries()
        return binding

    def _trim_registries(self) -> None:
        while len(self._plans) > self.registry_limit:
            newest = next(reversed(self._plans))
            candidates = [
                (reference, plan)
                for reference, plan in self._plans.items()
                if reference not in self._opening_tasks and reference != newest
            ]
            removable = next(
                (
                    (reference, plan)
                    for reference, plan in candidates
                    if plan.status == _PlanStatus.PREPARADO
                ),
                None,
            )
            if removable is None:
                removable = next(iter(candidates), None)
            if removable is None:
                break
            reference, plan = removable
            del self._plans[reference]
            if self._fingerprints.get(plan.fingerprint) == reference:
                self._fingerprints.pop(plan.fingerprint, None)
            if plan.access_reference is not None:
                self._accesses.pop(plan.access_reference, None)


def _preparation_model(plan: _Plan) -> PreparacaoPesquisaGeral:
    return PreparacaoPesquisaGeral(
        referencia_preparo=plan.reference,
        numero=plan.numero,
        grau=plan.grau,
        expira_em=plan.expires_at,
        frase_confirmacao=confirmation_phrase(plan.numero, plan.grau),
        aviso=(
            "A pesquisa autenticada encontrou um único resultado, mas não abriu os Autos. "
            "A abertura pode registrar seu interesse conforme a Resolução CNJ 121/2010."
        ),
    )


def _access_model(binding: _AccessBinding, *, reused: bool) -> AcessoPesquisaGeral:
    warning = (
        "A abertura pode ter sido registrada, mas o HTML dos Autos não pôde ser provado. "
        "O MCP não tentará novamente automaticamente."
        if binding.state is EstadoAcessoPesquisaGeral.INDETERMINADO
        else (
            "Acesso confirmado e HTML mantido apenas em memória. Use consultar_autos; "
            "essa leitura não fará nova requisição ao tribunal."
        )
    )
    return AcessoPesquisaGeral(
        referencia_acesso=binding.reference,
        numero=binding.numero,
        grau=binding.grau,
        estado=binding.state,
        reutilizada=reused,
        acessado_em=binding.accessed_at,
        aviso=warning,
    )


def _assert_plan_fresh(plan: _Plan) -> None:
    if plan.expires_at <= datetime.now(UTC):
        raise ValidacaoError(
            "a preparação expirou antes da abertura; pesquise novamente antes de confirmar"
        )


def _plan_fingerprint(
    generation: str,
    grau: Grau,
    numero: str,
    result: _SearchResult,
) -> str:
    parsed = urlparse(result.autos_url)
    shape = f"{parsed.path}\0{result.processo_id}\0{result.fingerprint}"
    raw = "\0".join((generation, grau.value, numero, shape))
    return hashlib.sha256(raw.encode()).hexdigest()


def _canonical_search_url(config: Settings, grau: Grau) -> str:
    return f"{config.urls.pje_base(grau.value)}/Processo/ConsultaProcesso/listView.seam"


def _search_target(url: str, grau: Grau) -> str:
    parsed = urlparse(url)
    decoded = unquote(parsed.path)
    expected = f"/{grau.value}/Processo/ConsultaProcesso/listView.seam"
    try:
        port = parsed.port
    except ValueError:
        port = -1
    if (
        parsed.scheme != "https"
        or parsed.hostname != "pje.cloud.tjpe.jus.br"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or decoded != expected
        or parsed.params
        or parsed.fragment
        or ".." in decoded.split("/")
        or "\\" in decoded
        or "//" in decoded
    ):
        raise InterfacePjeAlteradaError("a action da pesquisa não pertence à rota permitida")
    try:
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise InterfacePjeAlteradaError("a action da pesquisa possui query inválida") from None
    if not set(query).issubset({"cid"}) or any(
        len(values) != 1 or re.fullmatch(r"[0-9]{1,12}", values[0]) is None
        for values in query.values()
    ):
        raise InterfacePjeAlteradaError("a action da pesquisa possui parâmetros inesperados")
    return parsed._replace(fragment="").geturl()


def _is_search_page_url(url: str, grau: Grau) -> bool:
    try:
        _search_target(url, grau)
    except InterfacePjeAlteradaError:
        return False
    return True


def _same_url(candidate: str, expected: str) -> bool:
    return urlparse(candidate)._replace(fragment="") == urlparse(expected)._replace(fragment="")


def _allowed_search_get(url: str, resource_type: str, grau: Grau) -> bool:
    if not is_same_degree_url(url, grau):
        return False
    if resource_type == "document":
        return _is_search_page_url(url, grau)
    path = unquote(urlparse(url).path).casefold()
    # O RichFaces 3 busca recursos empacotados por XHR em GET. Só os caminhos de
    # recurso são liberados: XHR para a própria rota da consulta permanece barrado,
    # conforme test_allowed_search_get_rejects_other_navigation_and_resource_types.
    if resource_type in {"xhr", "fetch"}:
        return "/rfres/" in path or "/a4j/" in path
    if resource_type not in {"script", "stylesheet", "image", "font", "media"}:
        return False
    return (
        "/javax.faces.resource/" in path
        or "/resources/" in path
        or "/static/" in path
        or "/rfres/" in path
        or "/a4j/" in path
        or path.endswith(
            (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".woff", ".woff2")
        )
    )


def _validated_marker(value: str) -> str:
    marker = value.strip()
    if _FIELD_NAME.fullmatch(marker) is None:
        raise InterfacePjeAlteradaError("o controle Pesquisar não possui marcador auditável")
    folded = _fold(marker)
    if _FORBIDDEN_FORM_TERM.search(folded):
        raise InterfacePjeAlteradaError("o controle Pesquisar contém uma ação não permitida")
    component = re.split(r"[:.]", folded)[-1]
    if _SAFE_SEARCH_MARKER.fullmatch(component) is None:
        raise InterfacePjeAlteradaError(
            "o marcador do controle não identifica semanticamente uma pesquisa"
        )
    return marker


def _validated_marker_value(value: str) -> str:
    folded = _fold(_compact(value))
    if (
        len(value) > 256
        or any(character in value for character in "\r\n")
        or folded not in {"", "pesquisar", "consultar"}
    ):
        raise InterfacePjeAlteradaError("o valor do controle Pesquisar não é permitido")
    return value


def _post_matches_contract(
    data: str | None,
    marker: str,
    required: dict[str, str],
    *,
    marker_value: str | None = None,
) -> bool:
    if data is None or len(data) > 1024 * 1024:
        return False
    try:
        fields = parse_qs(data, keep_blank_values=True, strict_parsing=False)
    except ValueError:
        return False
    direct = fields.get(marker)
    source = fields.get("javax.faces.source")
    if direct is not None and (
        len(direct) != 1 or (marker_value is not None and direct != [marker_value])
    ):
        return False
    if source is not None and source != [marker]:
        return False
    if not (direct is not None or source == [marker]):
        return False
    if not all(fields.get(name) == [value] for name, value in required.items()):
        return False
    expected = set(required)
    if direct is not None:
        expected.add(marker)
    if source is not None:
        expected.add("javax.faces.source")
    return set(fields) == expected


async def _configure_exact_npu_form(
    page: Page,
    numero: str,
    grau: Grau,
    *,
    adaptive: AdaptiveNavigation | None = None,
) -> _FormContract:
    submit_names = ("Pesquisar",)
    npu_names = ("Número do processo", "Processo")
    if adaptive is not None:
        submit_names = adaptive.semantic_phrases(
            TribunalCodigo.TJPE,
            f"tjpe_{grau.value}",
            "pesquisa_geral",
            "search_submit",
            submit_names,
        )
        npu_names = adaptive.semantic_phrases(
            TribunalCodigo.TJPE,
            f"tjpe_{grau.value}",
            "pesquisa_geral",
            "npu_field",
            npu_names,
        )
    token = secrets.token_urlsafe(12)
    payload = cast(
        dict[str, object],
        # DESVIO LOCAL AUTORIZADO PELO USUÁRIO (não está no projeto original):
        # o "Pesquisar" do TJPE é <input type="button"> que dispara AJAX, e não um
        # submit nativo. Aceitar type="button" enfraquece a garantia de que o clique
        # vai a um submit de formulário verificável. As outras barreiras seguem
        # intactas: correspondência única, visível, habilitado, dentro de <form>,
        # NPU exato, allowlist de rede e fronteira de efeito.
        await page.locator(
            "button, input[type='submit'], input[type='button']"
        ).evaluate_all(
            r"""
            (controls, args) => {
              const compact = (value) => (value || '').replace(/\s+/g, ' ').trim()
                .toLocaleLowerCase('pt-BR');
              const visible = controls.filter((element) => {
                const style = window.getComputedStyle(element);
                const text = element.tagName.toLowerCase() === 'input'
                  ? element.value : (element.innerText || element.textContent || '');
                return args.names.includes(compact(text)) &&
                  ['submit', 'button'].includes(element.type) &&
                  !element.disabled && element.getClientRects().length > 0 &&
                  style.visibility !== 'hidden' && style.display !== 'none' &&
                  element.form instanceof HTMLFormElement;
              });
              for (const element of controls) {
                element.removeAttribute('data-mcp-search-submit');
                element.form?.removeAttribute('data-mcp-search-form');
              }
              if (visible.length === 1) {
                visible[0].setAttribute('data-mcp-search-submit', args.token);
                visible[0].form.setAttribute('data-mcp-search-form', args.token);
              }
              return {count: visible.length};
            }
            """,
            {
                "token": token,
                "names": [" ".join(name.split()).casefold() for name in submit_names],
            },
        ),
    )
    if payload.get("count") != 1:
        if adaptive is not None:
            await adaptive.record_discovery_failure(
                page,
                tribunal=TribunalCodigo.TJPE,
                instancia=f"tjpe_{grau.value}",
                flow="pesquisa_geral",
                step="search_submit",
                failure_code="unique_control_not_found",
            )
        raise InterfacePjeAlteradaError(
            "a consulta não apresentou um único submit nativo Pesquisar"
        )
    form = page.locator(f'[data-mcp-search-form="{token}"]')
    submit = page.locator(f'[data-mcp-search-submit="{token}"]')
    field_token = secrets.token_urlsafe(12)
    shape = cast(
        dict[str, object],
        await form.evaluate(
            r"""
            (form, args) => {
              const fold = (value) => (value || '').normalize('NFD')
                .replace(/[\u0300-\u036f]/g, '').replace(/\s+/g, ' ').trim()
                .toLocaleLowerCase('pt-BR');
              const labelText = (element) => [...(element.labels || [])]
                .map((label) => fold(label.textContent || '')).join(' ');
              const fields = [...form.elements].filter((element) => {
                const tag = element.tagName.toLowerCase();
                const type = (element.type || '').toLowerCase();
                if (tag !== 'input' || !['text', 'search', 'tel'].includes(type)) return false;
                const identity = fold([
                  element.id, element.name, element.getAttribute('aria-label'),
                  labelText(element)
                ].join(' '));
                // These structural aliases are part of the audited TJPE contract.  The
                // adapter may add approved semantic phrases, but it must never weaken
                // the native one-or-six-field invariant below.
                if (identity.includes('numeroprocesso') ||
                    identity.includes('numero do processo') || identity === 'processo') {
                  return true;
                }
                const semanticLabel = fold([
                  element.getAttribute('aria-label'), labelText(element)
                ].join(' '));
                return args.names.some((name) => {
                  return semanticLabel === name;
                });
              });
              const otherCriteria = [...form.elements].filter((element) => {
                const tag = element.tagName.toLowerCase();
                const type = (element.type || '').toLowerCase();
                const typedInput = tag === 'input' && [
                  'text', 'search', 'tel', 'email', 'number', 'date'
                ].includes(type);
                return !element.disabled && !fields.includes(element) &&
                  (typedInput || tag === 'textarea' || tag === 'select');
              }).map((element) => ({
                name: element.getAttribute('name') || '',
                value: element.value || ''
              }));
              for (const element of form.querySelectorAll('[data-mcp-search-npu]')) {
                element.removeAttribute('data-mcp-search-npu');
              }
              fields.forEach((element, index) => {
                element.setAttribute('data-mcp-search-npu', `${args.token}-${index}`);
              });
              return {
                count: fields.length,
                metadata: fields.map((element) => ({
                  disabled: Boolean(element.disabled),
                  readOnly: Boolean(element.readOnly),
                  maxLength: Number(element.maxLength),
                  name: element.getAttribute('name') || '',
                  value: element.value || ''
                })),
                otherCriteria
              };
            }
            """,
            {
                "token": field_token,
                "names": [fold_semantic(name) for name in npu_names],
            },
        ),
    )
    count = shape.get("count")
    raw_metadata = shape.get("metadata")
    raw_other_criteria = shape.get("otherCriteria")
    if (
        not isinstance(count, int)
        or count not in {1, 6}
        or not isinstance(raw_metadata, list)
        or not isinstance(raw_other_criteria, list)
    ):
        if adaptive is not None:
            await adaptive.record_discovery_failure(
                page,
                tribunal=TribunalCodigo.TJPE,
                instancia=f"tjpe_{grau.value}",
                flow="pesquisa_geral",
                step="npu_field",
                failure_code="npu_field_shape_changed",
            )
        raise InterfacePjeAlteradaError(
            "o formulário não apresentou um campo único ou seis segmentos auditáveis do NPU"
        )
    empty_criteria_names: set[str] = set()
    for raw_criterion in cast(list[object], raw_other_criteria):
        if not isinstance(raw_criterion, dict):
            raise InterfacePjeAlteradaError("o formulário contém critério não auditável")
        criterion = cast(dict[str, object], raw_criterion)
        name = criterion.get("name")
        value = criterion.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            raise InterfacePjeAlteradaError("o formulário contém critério inválido")
        if value.strip() and not _is_seam_no_selection(value):
            raise InterfacePjeAlteradaError(
                "o formulário contém outro critério textual ou seletor preenchido além do NPU"
            )
        if name:
            if _FIELD_NAME.fullmatch(name) is None:
                raise InterfacePjeAlteradaError(
                    "um critério vazio do formulário não possui nome auditável"
                )
            empty_criteria_names.add(name)
    metadata = cast(list[dict[str, object]], raw_metadata)
    if len(metadata) != count:
        raise InterfacePjeAlteradaError("os campos do NPU possuem estrutura ambígua")
    parts = _npu_parts(numero)
    submitted_npu_fields: list[tuple[str, str]] = []
    if count == 1:
        item = metadata[0]
        if item.get("disabled") is True or item.get("readOnly") is True:
            raise InterfacePjeAlteradaError("o campo Processo não pode receber o NPU")
        maximum = item.get("maxLength")
        if isinstance(maximum, int) and maximum not in {-1, 0} and maximum < 20:
            raise InterfacePjeAlteradaError("o campo Processo não comporta um NPU completo")
        name = item.get("name")
        if not isinstance(name, str) or _FIELD_NAME.fullmatch(name) is None:
            raise InterfacePjeAlteradaError("o campo Processo não possui nome auditável")
        submitted_npu_fields.append((name, numero))
        await form.locator(f'[data-mcp-search-npu="{field_token}-0"]').fill(numero)
    else:
        expected_lengths = (7, 2, 4, 1, 2, 4)
        for index, (item, expected_length) in enumerate(
            zip(metadata, expected_lengths, strict=True)
        ):
            maximum = item.get("maxLength")
            if isinstance(maximum, int) and maximum not in {-1, 0, expected_length}:
                raise InterfacePjeAlteradaError("os segmentos do NPU mudaram de formato")
            locator = form.locator(f'[data-mcp-search-npu="{field_token}-{index}"]')
            if index in {3, 4}:
                current = re.sub(r"\D", "", str(item.get("value", "")))
                if current != parts[index]:
                    raise InterfacePjeAlteradaError(
                        "o formulário não está fixado na Justiça e no tribunal TJPE"
                    )
                if item.get("disabled") is not True:
                    name = item.get("name")
                    if not isinstance(name, str) or _FIELD_NAME.fullmatch(name) is None:
                        raise InterfacePjeAlteradaError(
                            "um segmento fixo do NPU não possui nome auditável"
                        )
                    submitted_npu_fields.append((name, parts[index]))
                continue
            if item.get("disabled") is True or item.get("readOnly") is True:
                raise InterfacePjeAlteradaError("um segmento variável do NPU não é editável")
            name = item.get("name")
            if not isinstance(name, str) or _FIELD_NAME.fullmatch(name) is None:
                raise InterfacePjeAlteradaError("um segmento do NPU não possui nome auditável")
            submitted_npu_fields.append((name, parts[index]))
            await locator.fill(parts[index])
    verified = cast(
        list[str],
        await form.locator("[data-mcp-search-npu]").evaluate_all(
            "elements => elements.map((element) => element.value || '')"
        ),
    )
    if count == 1:
        if re.sub(r"\D", "", verified[0]) != re.sub(r"\D", "", numero):
            raise InterfacePjeAlteradaError("o campo Processo não preservou o NPU exato")
    elif [re.sub(r"\D", "", value) for value in verified] != list(parts):
        raise InterfacePjeAlteradaError("os segmentos não preservaram o NPU exato")

    form_payload = cast(
        dict[str, object],
        await form.evaluate(
            r"""
            (form) => ({
              tag: form.tagName.toLowerCase(),
              id: form.id || '',
              name: form.getAttribute('name') || '',
              method: (form.method || '').toLowerCase(),
              enctype: (form.enctype || '').toLowerCase(),
              literal: form.getAttribute('action') || '',
              absolute: form.action || '',
              fields: [...new FormData(form).entries()]
            })
            """
        ),
    )
    if (
        form_payload.get("tag") != "form"
        or not isinstance(form_payload.get("id"), str)
        or not isinstance(form_payload.get("name"), str)
        or form_payload.get("method") != "post"
        or form_payload.get("enctype") != "application/x-www-form-urlencoded"
        or not isinstance(form_payload.get("literal"), str)
        or not isinstance(form_payload.get("absolute"), str)
    ):
        raise InterfacePjeAlteradaError("Pesquisar não pertence a um formulário POST urlencoded")
    target = _search_target(cast(str, form_payload["absolute"]), grau)
    literal_target = _search_target(urljoin(page.url, cast(str, form_payload["literal"])), grau)
    if not _same_url(target, literal_target):
        raise InterfacePjeAlteradaError("a action da pesquisa mudou durante a inspeção")
    raw_fields = form_payload.get("fields")
    if not isinstance(raw_fields, list):
        raise InterfacePjeAlteradaError("o formulário não expôs seus campos auditáveis")
    fields: dict[str, str] = {}
    for raw_item in cast(list[object], raw_fields):
        if not isinstance(raw_item, list):
            raise InterfacePjeAlteradaError("o formulário possui campos não textuais")
        item = cast(list[object], raw_item)
        if len(item) != 2 or not all(isinstance(value, str) for value in item):
            raise InterfacePjeAlteradaError("o formulário possui campos inválidos")
        name, value = cast(tuple[str, str], tuple(item))
        if _FIELD_NAME.fullmatch(name) is None or name in fields or len(value) > 65_536:
            raise InterfacePjeAlteradaError("o formulário possui campos ambíguos")
        if _FORBIDDEN_FORM_TERM.search(_fold(name)) and not _AUDITED_TERM_COLLISION.fullmatch(
            re.split(r"[:.]", _fold(name))[-1]
        ):
            raise InterfacePjeAlteradaError(
                "o formulário contém campo associado a ação processual não permitida"
            )
        fields[name] = value
    if not 1 <= len(fields) <= 256:
        raise InterfacePjeAlteradaError("o formulário possui quantidade inesperada de campos")
    for name, expected_value in submitted_npu_fields:
        actual_value = fields.get(name)
        if actual_value is None or re.sub(r"\D", "", actual_value) != re.sub(
            r"\D", "", expected_value
        ):
            raise InterfacePjeAlteradaError(
                "o POST consultivo não contém exatamente os campos esperados do NPU"
            )
    marker_payload = cast(
        dict[str, object],
        await submit.evaluate(
            """
            element => ({
              valid: ['submit', 'button'].includes(element.type) &&
                element.form instanceof HTMLFormElement &&
                element.form.hasAttribute('data-mcp-search-form'),
              marker: element.getAttribute('name') || element.id || '',
              value: element.value || ''
            })
            """
        ),
    )
    if (
        marker_payload.get("valid") is not True
        or not isinstance(marker_payload.get("marker"), str)
        or not isinstance(marker_payload.get("value"), str)
    ):
        raise InterfacePjeAlteradaError("Pesquisar deixou de ser um submit nativo")
    marker = _validated_marker(cast(str, marker_payload["marker"]))
    marker_value = _validated_marker_value(cast(str, marker_payload["value"]))
    _validate_safe_form_fields(
        fields,
        npu_names={name for name, _value in submitted_npu_fields},
        form_identifiers={
            value
            for value in (
                cast(str, form_payload["id"]),
                cast(str, form_payload["name"]),
            )
            if value
        },
        empty_criteria_names=empty_criteria_names,
        marker=marker,
    )
    return _FormContract(
        submit=submit,
        target=target,
        marker=marker,
        marker_value=marker_value,
        fields=fields,
    )


def _validate_safe_form_fields(
    fields: dict[str, str],
    *,
    npu_names: set[str],
    form_identifiers: set[str],
    empty_criteria_names: set[str],
    marker: str,
) -> None:
    for name, value in fields.items():
        if name in npu_names:
            continue
        if name == "javax.faces.source":
            if value != marker:
                raise InterfacePjeAlteradaError(
                    "o evento JSF do formulário não corresponde ao botão Pesquisar"
                )
            continue
        if _SAFE_STATE_FIELD.fullmatch(name):
            continue
        if name in form_identifiers and value in form_identifiers:
            continue
        if name in empty_criteria_names and (value == "" or _is_seam_no_selection(value)):
            continue
        component = re.split(r"[:.]", name)[-1]
        if _WIDGET_STATE_FIELD.fullmatch(component):
            continue
        if _WIDGET_STATE_FIELD_IF_EMPTY.fullmatch(component) and value == "":
            continue
        raise InterfacePjeAlteradaError(
            "o formulário contém campo fora da lista consultiva permitida"
        )


def _npu_parts(numero: str) -> tuple[str, str, str, str, str, str]:
    digits = re.sub(r"\D", "", normalize_npu_tjpe(numero))
    return digits[:7], digits[7:9], digits[9:13], digits[13], digits[14:16], digits[16:20]


async def _assert_no_blocking_notice(page: Page) -> None:
    texts = cast(
        list[str],
        await page.locator(
            "[role='alert'], [role='dialog'], .ui-messages-error, .ui-messages-warn, "
            ".ui-messages, .rich-messages, .alert-warning, .alert-danger, .modal, "
            "[class*='mensagem' i], [class*='message' i], [class*='aviso' i], "
            "[class*='toast' i]"
        ).evaluate_all(
            r"""
            (elements) => elements.filter((element) => {
              const style = window.getComputedStyle(element);
              return element.getClientRects().length > 0 && style.visibility !== 'hidden' &&
                style.display !== 'none';
            }).map((element) => (
              element.innerText || element.textContent || ''
            ).trim().slice(0, 3000))
            """
        ),
    )
    if any(
        _BLOCKED_ACTION.search(text)
        or _RESTRICTED_RESULT.search(text)
        or _ACCESS_DENIED.search(text)
        for text in texts
    ):
        raise ValidacaoError(
            "o PJe apresentou aviso de ciência, permissão ou sigilo; a operação foi bloqueada"
        )


async def _extract_exact_result(page: Page, numero: str, grau: Grau) -> _SearchResult:
    body_text = await page.locator("body").inner_text()
    if _page_is_partial(body_text):
        raise InterfacePjeAlteradaError(
            "a pesquisa retornou paginação; nenhum resultado foi autorizado"
        )
    token = secrets.token_urlsafe(12)
    raw = cast(
        dict[str, object],
        await page.locator("a").evaluate_all(
            r"""
            (anchors, args) => {
              const compact = (value) => (value || '').replace(/\s+/g, ' ').trim();
              const expectedDigits = args.numero.replace(/\D/g, '');
              const candidates = anchors.filter((anchor) => {
                const style = window.getComputedStyle(anchor);
                const text = compact(anchor.innerText || anchor.textContent || '');
                return text.replace(/\D/g, '') === expectedDigits &&
                  anchor.getClientRects().length > 0 && style.visibility !== 'hidden' &&
                  style.display !== 'none';
              });
              for (const anchor of anchors) anchor.removeAttribute('data-mcp-search-result');
              if (candidates.length === 1) {
                candidates[0].setAttribute('data-mcp-search-result', args.token);
              }
              return {
                count: candidates.length,
                entry: candidates.length === 1 ? (() => {
                  const anchor = candidates[0];
                  const container = anchor.closest(
                    'tr, li, .media, [class*="processo" i]'
                  ) || anchor;
                  return {
                    text: compact(anchor.innerText || anchor.textContent || ''),
                    context: compact(
                      container.innerText || container.textContent || ''
                    ).slice(0, 4000),
                    href: anchor.getAttribute('href') || '',
                    absoluteHref: anchor.href || '',
                    onclick: anchor.getAttribute('onclick') || ''
                  };
                })() : null
              };
            }
            """,
            {"numero": numero, "token": token},
        ),
    )
    if raw.get("count") == 0:
        raise ValidacaoError("o NPU exato não foi encontrado na pesquisa autenticada")
    if raw.get("count") != 1 or not isinstance(raw.get("entry"), dict):
        raise InterfacePjeAlteradaError(
            "a pesquisa não apresentou um único link com o NPU integral"
        )
    entry = cast(dict[str, str], raw["entry"])
    if _RESTRICTED_RESULT.search(entry.get("context", "")) or _ACCESS_DENIED.search(
        entry.get("context", "")
    ):
        raise ValidacaoError(
            "o resultado indica sigilo ou acesso restrito; os Autos não serão abertos"
        )
    tokens = _normalized_npu_tokens(f"{entry.get('text', '')} {entry.get('context', '')}")
    if tokens != {numero}:
        raise InterfacePjeAlteradaError(
            "a linha do resultado contém processo ausente ou conflitante"
        )
    target = audited_autos_target(entry, grau)
    if target is None:
        raise InterfacePjeAlteradaError(
            "o resultado não apresentou uma única rota GET reconhecida dos Autos; "
            f"link encontrado: {_sanitized_link_shape(entry)}"
        )
    autos_url, processo_id = target
    fingerprint = hashlib.sha256(
        "\0".join(
            (
                numero,
                processo_id,
                urlparse(autos_url).path,
            )
        ).encode()
    ).hexdigest()
    return _SearchResult(
        numero=numero,
        processo_id=processo_id,
        autos_url=autos_url,
        fingerprint=fingerprint,
        anchor=page.locator(f'[data-mcp-search-result="{token}"]'),
    )


def _normalized_npu_tokens(text: str) -> set[str]:
    normalized: set[str] = set()
    for token in _NPU_TOKEN.findall(text):
        try:
            normalized.add(normalize_npu_tjpe(token))
        except ValidacaoError:
            return set()
    return normalized


def _page_is_partial(text: str) -> bool:
    if any(
        int(match.group("atual")) < int(match.group("total"))
        for match in _PAGINATION.finditer(text)
    ):
        return True
    folded = _fold(text)
    return "proxima pagina" in folded or "carregar mais resultados" in folded


def _is_official_access_dialog(message: str, numero: str) -> bool:
    folded = _fold(_compact(message))
    raw_mentions = _NPU_TOKEN.findall(message)
    mentioned_npus = _normalized_npu_tokens(message)
    return (
        _RESOLUTION_121.search(folded) is not None
        and "cnj" in folded
        and "acesso" in folded
        and ("registr" in folded or "pedido" in folded)
        and (not raw_mentions or mentioned_npus == {numero})
        and _BLOCKED_ACTION.search(message) is None
        and _RESTRICTED_RESULT.search(message) is None
        and _ACCESS_DENIED.search(message) is None
    )


def _has_unexpected_pages(ui: _UiSession, allowed: tuple[Page, ...]) -> bool:
    return any(
        all(observed is not accepted for accepted in allowed) for observed in ui.observed_pages
    )


async def _click_exact_result(
    ui: _UiSession,
    result: _SearchResult,
    *,
    timeout_ms: int,
) -> Page:
    before = set(ui.context.pages)
    await result.anchor.click()
    deadline = asyncio.get_running_loop().time() + max(timeout_ms, 250) / 1_000
    while asyncio.get_running_loop().time() < deadline:
        candidates = [
            page
            for page in ui.context.pages
            if autos_process_id(page.url, ui.gate.grau) == result.processo_id
            and _same_url(page.url, result.autos_url)
        ]
        if len(candidates) == 1:
            page = candidates[0]
            await page.wait_for_load_state("domcontentloaded", timeout=max(timeout_ms, 250))
            await asyncio.sleep(0.1)
            if _has_unexpected_pages(ui, (ui.page, page)):
                raise InterfacePjeAlteradaError(
                    "a abertura criou uma página adicional não autorizada"
                )
            return page
        if len(candidates) > 1:
            raise InterfacePjeAlteradaError("a abertura criou mais de uma página de Autos")
        await asyncio.sleep(0.05)
    new_pages = [page for page in ui.context.pages if page not in before]
    if new_pages:
        raise InterfacePjeAlteradaError("a nova página não corresponde aos Autos confirmados")
    raise ServicoIndisponivelError("o PJe não concluiu a abertura dos Autos confirmados")


async def _validated_autos_html(
    page: Page,
    numero: str,
    grau: Grau,
    processo_id: str,
) -> str:
    if autos_process_id(page.url, grau) != processo_id:
        raise InterfacePjeAlteradaError("a página aberta não corresponde ao resultado pesquisado")
    body_text = await page.locator("body").inner_text()
    if numero not in body_text:
        raise InterfacePjeAlteradaError("os Autos abertos não confirmaram o NPU")
    await _assert_no_blocking_notice(page)
    if _RESTRICTED_RESULT.search(body_text) or _ACCESS_DENIED.search(body_text):
        raise ValidacaoError("os Autos indicaram sigilo, restrição ou negativa de acesso")
    structural = page.locator("#divTimeLine, form#divTimeLine, [onclick*='abrirLinkDocumento']")
    if await structural.count() == 0:
        raise InterfacePjeAlteradaError("a página não apresentou a estrutura reconhecida dos Autos")
    html = await page.content()
    if autos_process_id(page.url, grau) != processo_id:
        raise InterfacePjeAlteradaError(
            "a página saiu dos Autos confirmados durante a captura protegida"
        )
    if len(html.encode("utf-8")) > _MAX_AUTOS_HTML_BYTES:
        raise ValidacaoError("os Autos excederam o teto local de HTML autenticado")
    return html


async def _scoped_cookies(
    context: BrowserContext,
    url: str,
) -> list[dict[str, object]]:
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
        raise CredenciaisAusentesError("a sessão autenticada não forneceu cookies à pesquisa geral")
    return result
