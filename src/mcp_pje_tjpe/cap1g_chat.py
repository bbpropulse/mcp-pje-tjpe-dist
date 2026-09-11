"""Atendimento pelo chat da Central de Atendimento Processual do 1º Grau (CAP1G).

O portal do TJPE só aponta para um Mibew Messenger 2.x. Levantado em 2026-09-10 a
partir do cliente servido em https://www.tjpe.jus.br/mibew/:

- a página do chat embute ``Mibew.Application.start({...})`` com ``startFrom``:
  ``survey`` quando há operador no grupo, ``leaveMessage`` quando não há — e a
  CAP1G desativou o formulário de recado, então fora do horário não há nada a fazer;
- o survey é ``form[name=surveyForm]`` com ``name``, ``email`` e ``#message-survey``;
  ``#submit-survey`` chama ``processSurvey`` e, com operador, troca a tela pelo chat;
- no chat, ``Mibew.Objects.Models.thread``/``user`` e ``Collections.messages`` são
  modelos Backbone; ``#message-input`` + ``#send-message`` postam; o próprio
  cliente faz ``update`` a cada 2s, mantendo a conversa viva enquanto a página existir.

Este módulo dirige essa página com Playwright em janela visível: quem conversa com
o servidor do tribunal é o advogado, e ele precisa ver — e poder intervir. As boas
práticas publicadas pela CAP1G (identificar-se, ser objetivo, não repetir mensagens,
evitar caixa alta) viram validação aqui, não conselho.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import re
import secrets
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any, Literal, ParamSpec, TypeVar, cast
from zoneinfo import ZoneInfo

from playwright.async_api import BrowserContext, Page, Route
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from mcp_pje_tjpe.browser import BrowserManager
from mcp_pje_tjpe.config import Settings
from mcp_pje_tjpe.errors import PjeTjpeError, ServicoIndisponivelError, ValidacaoError
from mcp_pje_tjpe.models import (
    DisponibilidadeChatCap1g,
    EncerramentoChatCap1g,
    EstadoChatCap1g,
    MensagemChatCap1g,
    PreparacaoChatCap1g,
    SessaoChatCap1g,
    TipoMensagemChat,
)
from mcp_pje_tjpe.pje_read import atomic_publish, secure_subdirectory

_P = ParamSpec("_P")
_T = TypeVar("_T")

_REFERENCE = re.compile(r"^[A-Za-z0-9_-]{20,80}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_APPLICATION_START = re.compile(r"Mibew\.Application\.start\(")
_HTML_TAG = re.compile(r"</?[A-Za-z][^<>]*>")
# Numeração única do CNJ, com ou sem os separadores: é o que identifica um
# "encaminhamento" para a CAP1G, que aceita poucos por atendimento.
_NPU_CNJ = re.compile(r"(?<!\d)(\d{7})-?(\d{2})\.?(\d{4})\.?(\d)\.?(\d{2})\.?(\d{4})(?!\d)")
_PLAN_TTL = timedelta(minutes=10)
_MAX_NOME = 100
_MAX_MENSAGEM = 2000
_POLL_SECONDS = 2.0
_ECHO_TIMEOUT_SECONDS = 15.0
_CLOSE_TIMEOUT_SECONDS = 10.0
_MAX_WAIT_SECONDS = 300
_DEFAULT_WAIT_SECONDS = 60
_HORARIO_INICIO = 8
_HORARIO_FIM = 19
_FUSO_TRIBUNAL = ZoneInfo("America/Recife")
_HORARIO_TEXTO = "8h às 19h em dias úteis"

# Thread.STATE_* e Message.KIND_* do cliente Mibew (js/compiled/default_app.js).
_ESTADOS = {
    0: EstadoChatCap1g.NA_FILA,
    1: EstadoChatCap1g.AGUARDANDO_OPERADOR,
    2: EstadoChatCap1g.EM_ATENDIMENTO,
    3: EstadoChatCap1g.ENCERRADO,
    4: EstadoChatCap1g.CARREGANDO,
    5: EstadoChatCap1g.ABANDONADO,
    6: EstadoChatCap1g.CONVIDADO,
}
_ESTADO_ENCERRADO = 3
_TIPOS = {
    1: TipoMensagemChat.VISITANTE,
    2: TipoMensagemChat.OPERADOR,
    3: TipoMensagemChat.OCULTA,
    4: TipoMensagemChat.INFO,
    5: TipoMensagemChat.CONEXAO,
    6: TipoMensagemChat.EVENTO,
    7: TipoMensagemChat.PLUGIN,
}

# Nunca devolve thread.token: é a credencial da conversa e só a página precisa dela.
_SNAPSHOT_JS = """
() => {
  const mibew = window.Mibew;
  const models = mibew && mibew.Objects && mibew.Objects.Models;
  if (!models || !models.thread || !models.user) {
    const survey = document.querySelector("form[name='surveyForm']");
    const leave = document.querySelector("form[name='leaveMessageForm']")
      || document.body.classList.contains("leaveMessage");
    const errors = document.querySelector(".errors");
    return {
      stage: survey ? "survey" : (leave ? "leaveMessage" : "unknown"),
      errors: errors ? errors.textContent.trim() : "",
    };
  }
  const thread = models.thread.toJSON();
  const user = models.user.toJSON();
  const collection = mibew.Objects.Collections && mibew.Objects.Collections.messages;
  const messages = collection ? collection.toJSON() : [];
  const status = models.Status || {};
  const typing = !!(status.typing && status.typing.get && status.typing.get("visible"));
  const notice = (status.message && status.message.get && status.message.get("visible"))
    ? String(status.message.get("message") || "")
    : "";
  return {
    stage: "chat",
    thread: {
      id: thread.id,
      state: thread.state,
      agentId: thread.agentId,
      lastId: thread.lastId,
    },
    user: { name: String(user.name || ""), canPost: !!user.canPost },
    typing: typing,
    notice: notice,
    messages: messages.map((m) => ({
      id: m.id,
      kind: m.kind,
      name: String(m.name || ""),
      message: String(m.message || ""),
      created: m.created || 0,
    })),
  };
}
"""

# Primeira tela do Mibew: survey (há operador), leaveMessage (não há) ou o chat
# direto (conversa já existente). Só devolve algo quando a página decidiu.
_STAGE_JS = """
() => {
  if (document.querySelector("#messages-region")) { return "chat"; }
  if (document.querySelector("form[name='surveyForm']")) { return "survey"; }
  if (document.querySelector("form[name='leaveMessageForm']")
      || document.body.classList.contains("leaveMessage")) { return "leave"; }
  return null;
}
"""

# Desfecho do clique em "Iniciar Chat": só devolve algo quando a página decidiu.
_SURVEY_OUTCOME_JS = """
() => {
  if (document.querySelector("#messages-region")) { return { outcome: "chat" }; }
  if (document.querySelector("form[name='leaveMessageForm']")) { return { outcome: "leave" }; }
  const errors = document.querySelector(".errors");
  const detail = errors ? errors.textContent.trim() : "";
  if (detail) { return { outcome: "error", detail: detail }; }
  return null;
}
"""

_CLOSE_JS = """
() => {
  const mibew = window.Mibew;
  const controls = mibew && mibew.Objects && mibew.Objects.Models
    && mibew.Objects.Models.Controls;
  if (controls && controls.close && controls.close.closeThread) {
    controls.close.closeThread();
    return true;
  }
  return false;
}
"""


def frase_confirmacao_chat(nome: str, email: str) -> str:
    return (
        "CONFIRMO INICIAR O CHAT DA CAP1G DO TJPE COMO "
        f"{normalizar_nome(nome).upper()} ({normalizar_email(email)})"
    )


def normalizar_nome(nome: str) -> str:
    clean = " ".join(nome.split())
    if len(clean) < 2 or len(clean) > _MAX_NOME or not any(c.isalpha() for c in clean):
        raise ValidacaoError("nome deve ter entre 2 e 100 caracteres e conter letras")
    return clean


def normalizar_email(email: str) -> str:
    clean = email.strip().lower()
    if _EMAIL.fullmatch(clean) is None or len(clean) > 254:
        raise ValidacaoError("e-mail inválido; a CAP1G pede nome e e-mail para direcionar o pedido")
    return clean


def normalizar_mensagem(mensagem: str) -> str:
    clean = "\n".join(" ".join(line.split()) for line in mensagem.strip().splitlines()).strip()
    if not clean:
        raise ValidacaoError("a mensagem não pode ser vazia")
    if len(clean) > _MAX_MENSAGEM:
        raise ValidacaoError(f"a mensagem deve ter no máximo {_MAX_MENSAGEM} caracteres")
    letras = [c for c in clean if c.isalpha()]
    if len(letras) >= 12 and sum(c.isupper() for c in letras) / len(letras) >= 0.7:
        raise ValidacaoError(
            "mensagem quase toda em caixa alta; as boas práticas da CAP1G pedem "
            "escrita normal, sem maiúsculas contínuas"
        )
    return clean


def extrair_processos(texto: str) -> list[str]:
    """NPUs distintos citados no texto, no formato oficial e na ordem em que aparecem."""
    vistos: dict[str, None] = {}
    for match in _NPU_CNJ.finditer(texto):
        seq, dv, ano, justica, tribunal, origem = match.groups()
        vistos.setdefault(f"{seq}-{dv}.{ano}.{justica}.{tribunal}.{origem}", None)
    return list(vistos)


def dentro_do_horario(momento: datetime | None = None) -> bool:
    local = (momento or datetime.now(UTC)).astimezone(_FUSO_TRIBUNAL)
    return local.weekday() < 5 and _HORARIO_INICIO <= local.hour < _HORARIO_FIM


def extrair_opcoes_mibew(documento: str) -> dict[str, Any]:
    """Lê o JSON de ``Mibew.Application.start({...})`` embutido na página do chat."""
    match = _APPLICATION_START.search(documento)
    if match is None:
        raise ServicoIndisponivelError(
            "a página do chat da CAP1G não é mais um Mibew reconhecível; "
            "verifique o portal antes de tentar de novo"
        )
    inicio = match.end()
    while inicio < len(documento) and documento[inicio].isspace():
        inicio += 1
    try:
        options, _ = json.JSONDecoder().raw_decode(documento, inicio)
    except json.JSONDecodeError:
        raise ServicoIndisponivelError(
            "a configuração embutida do chat da CAP1G não pôde ser lida"
        ) from None
    if not isinstance(options, dict):
        raise ServicoIndisponivelError("a configuração embutida do chat da CAP1G mudou de formato")
    return cast(dict[str, Any], options)


def _sanitized_failure(
    message: str,
) -> Callable[[Callable[_P, Awaitable[_T]]], Callable[_P, Awaitable[_T]]]:
    """Impede que erros de navegador/rede exponham URLs, tokens ou texto da conversa."""

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


@dataclass(slots=True)
class _Plan:
    reference: str
    nome: str
    email: str
    mensagem_inicial: str
    disponivel: bool
    created_at: datetime
    expires_at: datetime
    session_reference: str | None = None


@dataclass(slots=True)
class _Session:
    reference: str
    plan_reference: str
    nome: str
    email: str
    context: BrowserContext | None = field(repr=False)
    page: Page | None = field(repr=False)
    started_at: datetime
    thread_id: int | None = None
    state: int | None = None
    can_post: bool = False
    typing: bool = False
    notice: str = ""
    visitor_name: str = ""
    messages: list[MensagemChatCap1g] = field(default_factory=list[MensagemChatCap1g])
    seen_ids: set[int] = field(default_factory=set[int])
    last_id: int = 0
    # Incrementa a cada mudança que interessa a quem espera: mensagem nova, estado,
    # permissão de envio ou encerramento. Digitação não conta — pisca demais.
    version: int = 0
    # O que o chamador já recebeu. O monitor lê a página o tempo todo, então "novo"
    # tem de ser medido contra a última resposta entregue, não contra o que o
    # servidor já viu — senão a resposta que chegou entre duas chamadas some.
    delivered_id: int = 0
    delivered_version: int = 0
    ended: bool = False
    ended_reason: str | None = None
    closed_by_visitor: bool = False
    monitor: asyncio.Task[None] | None = field(default=None, repr=False)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition, repr=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    partial_path: Path | None = None
    final_path: Path | None = None
    sha256: str | None = None
    last_visitor_text: str | None = None
    # Campos de identificação que o survey da CAP1G não ofereceu desta vez.
    missing_fields: list[str] = field(default_factory=list[str])
    # O que deu errado depois de a conversa existir — não derruba o chat, avisa.
    opening_warning: str | None = None
    # NPUs distintos citados pelo visitante, na ordem: a CAP1G limita quantos
    # encaminhamentos um atendimento leva, e o que foi digitado à mão também conta.
    processos: dict[str, None] = field(default_factory=dict[str, None])


class Cap1gChatService:
    """Uma conversa por vez com a CAP1G, dirigida numa janela visível do Chrome."""

    def __init__(
        self,
        chat_browser: BrowserManager,
        public_browser: BrowserManager,
        config: Settings,
        *,
        poll_seconds: float = _POLL_SECONDS,
        echo_seconds: float = _ECHO_TIMEOUT_SECONDS,
    ) -> None:
        self.chat_browser = chat_browser
        self.public_browser = public_browser
        self.config = config
        self.poll_seconds = poll_seconds
        self.echo_seconds = echo_seconds
        self._plans: OrderedDict[str, _Plan] = OrderedDict()
        self._sessions: OrderedDict[str, _Session] = OrderedDict()
        self._active: str | None = None
        self._state_lock = asyncio.Lock()

    # -- disponibilidade -----------------------------------------------------------

    @_sanitized_failure("não foi possível verificar o chat da CAP1G")
    async def verificar(self) -> DisponibilidadeChatCap1g:
        options = await self._read_chat_options()
        return self._availability(options)

    async def _read_chat_options(self) -> dict[str, Any]:
        # Só o documento interessa: scripts, estilos e imagens do Mibew ficam de fora,
        # e um GET da página não cria conversa alguma.
        async with self.public_browser.page() as page:
            await page.route("**/*", _only_documents)
            await page.goto(self.config.urls.cap1g_chat, wait_until="domcontentloaded")
            html = await page.content()
        return extrair_opcoes_mibew(html)

    def _availability(self, options: dict[str, Any]) -> DisponibilidadeChatCap1g:
        modo = str(options.get("startFrom") or "desconhecido")
        disponivel = modo in {"survey", "chat"}
        grupo = _group_name(options)
        no_horario = dentro_do_horario()
        if disponivel:
            mensagem = "há operador disponível; o chat abre pelo formulário de identificação"
        elif no_horario:
            mensagem = (
                "sem operador disponível agora, embora dentro da faixa de atendimento; "
                "pode ser feriado forense, pausa ou fila cheia — tente mais tarde ou ligue"
            )
        else:
            mensagem = f"fora do horário de atendimento ({_HORARIO_TEXTO}); não há como iniciar"
        return DisponibilidadeChatCap1g(
            disponivel=disponivel,
            modo_inicial=modo,
            grupo=grupo,
            dentro_do_horario=no_horario,
            limite_processos_por_atendimento=self.config.cap1g_processos_por_chat,
            url=self.config.urls.cap1g_chat,
            portal=self.config.urls.cap1g_portal,
            mensagem=mensagem,
            aviso=(
                "Verificação passiva: nenhuma conversa foi aberta. O chat é atendido por "
                "servidores da CAP1G; iniciar exige nome, e-mail e confirmação literal."
            ),
        )

    # -- preparação e início --------------------------------------------------------

    @_sanitized_failure("não foi possível preparar o chat da CAP1G")
    async def preparar(self, nome: str, email: str, mensagem_inicial: str) -> PreparacaoChatCap1g:
        clean_nome = normalizar_nome(nome)
        clean_email = normalizar_email(email)
        clean_mensagem = normalizar_mensagem(mensagem_inicial)
        processos = extrair_processos(clean_mensagem)
        limite = self.config.cap1g_processos_por_chat
        if len(processos) > limite:
            raise ValidacaoError(
                f"a mensagem inicial cita {len(processos)} processos e a CAP1G aceita "
                f"encaminhamento de no máximo {limite} por atendimento; divida em lotes "
                "e abra um chat por lote"
            )
        availability = self._availability(await self._read_chat_options())
        if not availability.disponivel:
            raise ServicoIndisponivelError(
                f"a CAP1G está sem operador no chat agora ({availability.mensagem}); "
                f"atendimento {_HORARIO_TEXTO} ou pelo telefone {availability.telefone}"
            )
        now = datetime.now(UTC)
        async with self._state_lock:
            reference = secrets.token_urlsafe(24)
            plan = _Plan(
                reference=reference,
                nome=clean_nome,
                email=clean_email,
                mensagem_inicial=clean_mensagem,
                disponivel=True,
                created_at=now,
                expires_at=now + _PLAN_TTL,
            )
            self._plans[reference] = plan
            self._trim_plans()
        return PreparacaoChatCap1g(
            referencia_preparo=reference,
            nome=clean_nome,
            email=clean_email,
            mensagem_inicial=clean_mensagem,
            processos_na_mensagem=processos,
            limite_processos_por_atendimento=limite,
            disponivel=True,
            expira_em=plan.expires_at,
            frase_confirmacao=frase_confirmacao_chat(clean_nome, clean_email),
            aviso=(
                "Nada foi enviado. Iniciar abre uma conversa real com um servidor da "
                "CAP1G, em seu nome, com a mensagem inicial acima. Confirme em uma nova "
                "mensagem com a frase literal; a preparação expira em 10 minutos."
            ),
        )

    @_sanitized_failure("não foi possível iniciar o chat da CAP1G com segurança")
    async def iniciar(self, referencia_preparo: str, confirmacao: str) -> SessaoChatCap1g:
        async with self._state_lock:
            plan = self._resolve_plan(referencia_preparo)
            if confirmacao != frase_confirmacao_chat(plan.nome, plan.email):
                raise ValidacaoError(
                    "confirmação incorreta; envie em uma nova mensagem a frase literal "
                    "devolvida pela preparação"
                )
            if plan.session_reference is not None:
                existing = self._sessions.get(plan.session_reference)
                if existing is not None:
                    return self._snapshot(existing, baseline=0, reused=True)
            if plan.expires_at <= datetime.now(UTC):
                raise ValidacaoError("a preparação expirou; prepare o chat novamente")
            active = self._sessions.get(self._active) if self._active else None
            if active is not None and not active.ended:
                raise ValidacaoError(
                    "já existe um atendimento em andamento nesta sessão; continue nele "
                    "ou encerre-o antes de iniciar outro"
                )
            session = _Session(
                reference=secrets.token_urlsafe(24),
                plan_reference=plan.reference,
                nome=plan.nome,
                email=plan.email,
                context=None,
                page=None,
                started_at=datetime.now(UTC),
            )
            plan.session_reference = session.reference
            self._sessions[session.reference] = session
            self._active = session.reference
            self._trim_sessions()

        try:
            await self._open_chat(session, plan.mensagem_inicial)
        except BaseException:
            await self._stop_monitor(session)
            await self._discard_browser(session)
            if session.partial_path is not None:
                session.partial_path.unlink(missing_ok=True)
            async with self._state_lock:
                plan.session_reference = None
                self._sessions.pop(session.reference, None)
                if self._active == session.reference:
                    self._active = None
            raise
        return self._snapshot(session, baseline=0, reused=False)

    async def _open_chat(self, session: _Session, mensagem_inicial: str) -> None:
        context = await self.chat_browser.new_context()
        session.context = context
        page = await context.new_page()
        session.page = page
        page.set_default_timeout(self.config.timeout_ms)
        page.set_default_navigation_timeout(self.config.timeout_ms)
        await page.goto(self.config.urls.cap1g_chat, wait_until="domcontentloaded")

        try:
            stage = await (await page.wait_for_function(_STAGE_JS)).json_value()
        except PlaywrightTimeoutError:
            raise ServicoIndisponivelError(
                "a página do chat da CAP1G não mostrou formulário nem aviso"
            ) from None
        if stage == "leave":
            raise ServicoIndisponivelError(
                "a CAP1G ficou sem operador antes de abrir o chat; nenhuma conversa foi criada"
            )
        message_in_survey = False
        if stage == "survey":
            message_in_survey = await self._submit_survey(session, page, mensagem_inicial)

        snapshot = await self._evaluate_snapshot(session)
        if snapshot is None or snapshot.get("stage") != "chat":
            raise ServicoIndisponivelError("o chat abriu, mas o cliente Mibew não expôs a conversa")
        self._apply_snapshot(session, snapshot)
        session.partial_path = self._partial_transcript_path(session)
        self._write_partial(session)
        session.monitor = asyncio.create_task(self._monitor(session))

        # Daqui em diante a conversa existe e um servidor pode já estar em linha:
        # falhar a primeira mensagem não pode fechar a janela na cara dele.
        if not message_in_survey:
            try:
                await self._post(session, mensagem_inicial)
            except PjeTjpeError as exc:
                session.opening_warning = f"A mensagem inicial não foi confirmada: {exc}."
            return
        # A pergunta inicial já foi com o survey; ela só ecoa no primeiro ciclo de
        # atualização do cliente. Esperar por ela deixa a resposta de iniciar completa.
        session.last_visitor_text = mensagem_inicial
        self._register_processes(session, mensagem_inicial)
        try:
            await self._wait_until(
                session,
                lambda s: any(m.tipo is TipoMensagemChat.VISITANTE for m in s.messages),
                limite_segundos=self.echo_seconds,
            )
        except TimeoutError:
            session.opening_warning = (
                "A pergunta inicial foi enviada com o formulário, mas ainda não apareceu "
                "na conversa; confira com ler_chat_cap1g antes de repeti-la."
            )

    async def _submit_survey(self, session: _Session, page: Page, mensagem_inicial: str) -> bool:
        """Preenche e envia o survey; devolve se a pergunta inicial foi junto."""
        survey = page.locator("form[name='surveyForm']")
        # A CAP1G pode desligar campos do survey; só se preenche o que existe, e a
        # mensagem inicial vai como primeira fala do chat se o campo não estiver lá.
        for rotulo, seletor, valor in (
            ("nome", "input[name='name']", session.nome),
            ("e-mail", "input[name='email']", session.email),
        ):
            field_locator = survey.locator(seletor)
            if await field_locator.count() > 0:
                await field_locator.fill(valor)
            else:
                session.missing_fields.append(rotulo)
        message_field = survey.locator("#message-survey")
        message_in_survey = await message_field.count() > 0
        if message_in_survey:
            await message_field.fill(mensagem_inicial)
        await survey.locator("#submit-survey").click()

        try:
            handle = await page.wait_for_function(_SURVEY_OUTCOME_JS)
            outcome = cast(dict[str, Any], await handle.json_value())
        except PlaywrightTimeoutError:
            raise ServicoIndisponivelError(
                "o chat da CAP1G não respondeu ao formulário de identificação"
            ) from None
        if outcome.get("outcome") == "error":
            detail = str(outcome.get("detail") or "")[:200]
            raise ValidacaoError(f"o chat recusou o formulário: {detail}")
        if outcome.get("outcome") != "chat":
            raise ServicoIndisponivelError(
                "a CAP1G ficou sem operador ao enviar o formulário; nenhuma conversa foi criada"
            )
        return message_in_survey

    # -- leitura e espera -------------------------------------------------------------

    @_sanitized_failure("não foi possível ler o chat da CAP1G")
    async def ler(self, referencia_chat: str, desde_id: int = 0) -> SessaoChatCap1g:
        if desde_id < 0:
            raise ValidacaoError("desde_id deve ser zero ou positivo")
        session = self._resolve_session(referencia_chat)
        await self._refresh(session)
        return self._snapshot(session, baseline=desde_id, reused=False)

    @_sanitized_failure("não foi possível aguardar o chat da CAP1G")
    async def aguardar(
        self,
        referencia_chat: str,
        timeout_segundos: int = _DEFAULT_WAIT_SECONDS,
        desde_id: int | None = None,
    ) -> SessaoChatCap1g:
        if not 1 <= timeout_segundos <= _MAX_WAIT_SECONDS:
            raise ValidacaoError(f"timeout_segundos deve ficar entre 1 e {_MAX_WAIT_SECONDS}")
        if desde_id is not None and desde_id < 0:
            raise ValidacaoError("desde_id deve ser zero ou positivo")
        session = self._resolve_session(referencia_chat)
        baseline = session.delivered_id if desde_id is None else desde_id
        seen_version = session.delivered_version
        await self._refresh(session)

        def novidade(current: _Session) -> bool:
            return current.ended or current.version != seen_version or current.last_id > baseline

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_segundos
        async with session.condition:
            while not novidade(session):
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(session.condition.wait(), remaining)
                except TimeoutError:
                    break
        return self._snapshot(session, baseline=baseline, reused=False)

    # -- envio e encerramento ---------------------------------------------------------

    @_sanitized_failure("não foi possível enviar a mensagem ao chat da CAP1G")
    async def enviar(self, referencia_chat: str, mensagem: str) -> SessaoChatCap1g:
        text = normalizar_mensagem(mensagem)
        session = self._resolve_session(referencia_chat)
        async with session.lock:
            await self._refresh(session)
            if session.ended or session.state == _ESTADO_ENCERRADO:
                raise ValidacaoError(
                    f"o atendimento já foi encerrado ({session.ended_reason or 'pelo operador'})"
                )
            if not session.can_post:
                raise ValidacaoError(
                    "o chat não aceita mensagens agora: o operador encerrou ou a conexão caiu"
                )
            if session.last_visitor_text == text:
                raise ValidacaoError(
                    "mensagem idêntica à última enviada; repetir mensagens em sequência "
                    "atrasa o atendimento segundo as boas práticas da CAP1G"
                )
            novos = [n for n in extrair_processos(text) if n not in session.processos]
            limite = self.config.cap1g_processos_por_chat
            if len(session.processos) + len(novos) > limite:
                raise ValidacaoError(
                    f"este atendimento já tem {len(session.processos)} processo(s) e a "
                    f"CAP1G aceita encaminhamento de no máximo {limite} por chat; a "
                    f"mensagem citaria mais {len(novos)}. Encerre este chat com "
                    "encerrar_chat_cap1g e inicie outro para os demais processos"
                )
            baseline = session.last_id
            consecutive = bool(session.messages) and (
                session.messages[-1].tipo is TipoMensagemChat.VISITANTE
            )
            await self._post(session, text)
        snapshot = self._snapshot(session, baseline=baseline, reused=False)
        if consecutive:
            snapshot.aviso = (
                "Mensagem enviada logo após outra sua, sem resposta do operador entre "
                "elas. A CAP1G pede paciência: aguarde antes de insistir. " + snapshot.aviso
            )
        return snapshot

    async def _post(self, session: _Session, text: str) -> None:
        page = self._page(session)
        field_locator = page.locator("#message-input")
        await field_locator.wait_for(state="visible")
        # O cliente desabilita o campo enquanto um envio anterior não ecoa; fill()
        # numa textarea desabilitada estoura sem explicar.
        await page.locator("#message-input:not([disabled])").wait_for(state="visible")
        baseline = session.last_id
        await field_locator.fill(text)
        await page.locator("#send-message").click()
        # Clicado, o texto está a caminho do tribunal mesmo que o eco demore. Marcar
        # só depois do eco deixaria uma repetição passar pela checagem de duplicata —
        # e repetir mensagem é justamente o que a CAP1G pede para não fazer.
        session.last_visitor_text = text
        self._register_processes(session, text)
        try:
            await self._wait_until(
                session,
                lambda s: any(
                    m.id > baseline and m.tipo is TipoMensagemChat.VISITANTE for m in s.messages
                ),
                limite_segundos=self.echo_seconds,
            )
        except TimeoutError:
            if session.ended:
                raise ValidacaoError(
                    f"o atendimento acabou antes de confirmar o envio ({session.ended_reason})"
                ) from None
            raise ServicoIndisponivelError(
                "a mensagem foi enviada, mas o chat ainda não a mostrou; confira com "
                "ler_chat_cap1g — não a repita"
            ) from None

    @_sanitized_failure("não foi possível encerrar o chat da CAP1G")
    async def encerrar(self, referencia_chat: str) -> EncerramentoChatCap1g:
        session = self._resolve_session(referencia_chat)
        async with session.lock:
            if session.final_path is not None and session.sha256 is not None:
                return self._closure(session, reused=True)
            await self._refresh(session)
            page = session.page
            if not session.ended and page is not None and not page.is_closed():
                # Marcado antes do clique: o Mibew fecha a janela ao confirmar, e o
                # monitor leria isso como "janela fechada" em vez de encerramento.
                session.closed_by_visitor = True
                try:
                    requested = await page.evaluate(_CLOSE_JS)
                except PlaywrightError:
                    requested = False
                if requested:
                    try:
                        await self._wait_until(
                            session,
                            lambda s: s.ended or s.state == _ESTADO_ENCERRADO,
                            limite_segundos=_CLOSE_TIMEOUT_SECONDS,
                        )
                    except TimeoutError:
                        pass
                    await self._end(session, "encerrado pelo visitante")
                else:
                    await self._end(session, "encerrado pelo visitante sem confirmação do chat")
            elif not session.ended:
                await self._end(session, "janela do chat indisponível")
            if page is not None and not page.is_closed():
                # Um ciclo de atualização do cliente Mibew antes da leitura final: o
                # aviso de encerramento pode vir depois do estado, e a transcrição
                # precisa dele.
                await asyncio.sleep(self.poll_seconds + 0.5)
                try:
                    await self._refresh(session)
                except PlaywrightError:
                    pass
            await self._stop_monitor(session)
            await self._discard_browser(session)
            self._publish_final(session)
            async with self._state_lock:
                if self._active == session.reference:
                    self._active = None
            return self._closure(session, reused=False)

    async def close(self) -> None:
        """Encerra o que ainda estiver aberto ao desligar o servidor."""
        for session in list(self._sessions.values()):
            await self._stop_monitor(session)
            page = session.page
            if not session.ended and page is not None and not page.is_closed():
                # Melhor esforço: avisa o Mibew que o visitante saiu, sem esperar.
                try:
                    await page.evaluate(_CLOSE_JS)
                except PlaywrightError:
                    pass
            if not session.ended:
                session.ended = True
                session.ended_reason = "servidor desligado"
            await self._discard_browser(session)
            if session.final_path is None and session.messages:
                try:
                    self._publish_final(session)
                except Exception:
                    pass
            elif session.partial_path is not None:
                session.partial_path.unlink(missing_ok=True)

    # -- monitor ----------------------------------------------------------------------

    async def _monitor(self, session: _Session) -> None:
        # Segue lendo mesmo depois do encerramento: o aviso final do Mibew pode
        # chegar num ciclo posterior ao estado, e a página só some em encerrar().
        try:
            while session.page is not None and not session.page.is_closed():
                await asyncio.sleep(self.poll_seconds)
                try:
                    await self._refresh(session)
                except PlaywrightError:
                    # Contexto destruído no meio de um evaluate é transitório; só a
                    # janela fechada encerra, e _refresh já cuida desse caso.
                    continue
        except asyncio.CancelledError:
            raise
        except Exception:
            if not session.ended:
                await self._end(session, "monitor interrompido")

    async def _refresh(self, session: _Session) -> None:
        page = session.page
        if page is None or page.is_closed():
            if not session.ended:
                await self._end(session, self._window_gone_reason(session))
            return
        try:
            snapshot = await self._evaluate_snapshot(session)
        except PlaywrightError:
            if page.is_closed():
                if not session.ended:
                    await self._end(session, self._window_gone_reason(session))
                return
            raise
        if snapshot is None or snapshot.get("stage") != "chat":
            if not session.ended:
                await self._end(session, "a página do chat saiu da conversa")
            return
        changed = self._apply_snapshot(session, snapshot)
        if session.state == _ESTADO_ENCERRADO and not session.ended:
            await self._end(session, self._closed_reason(session))
            return
        if changed:
            self._write_partial(session)
            await self._notify(session)

    @staticmethod
    def _window_gone_reason(session: _Session) -> str:
        return "encerrado pelo visitante" if session.closed_by_visitor else "janela do chat fechada"

    @staticmethod
    def _closed_reason(session: _Session) -> str:
        if session.closed_by_visitor:
            return "encerrado pelo visitante"
        return "encerrado pelo operador ou pelo servidor do chat"

    async def _evaluate_snapshot(self, session: _Session) -> dict[str, Any] | None:
        page = self._page(session)
        result = await page.evaluate(_SNAPSHOT_JS)
        if result is None:
            return None
        return cast(dict[str, Any], result)

    def _apply_snapshot(self, session: _Session, snapshot: dict[str, Any]) -> bool:
        changed = False
        thread = cast(dict[str, Any], snapshot.get("thread") or {})
        user = cast(dict[str, Any], snapshot.get("user") or {})
        thread_id = _as_int(thread.get("id"))
        if thread_id and session.thread_id != thread_id:
            session.thread_id = thread_id
        state = _as_int(thread.get("state"), default=None)
        if state != session.state:
            session.state = state
            changed = True
        can_post = bool(user.get("canPost"))
        if can_post != session.can_post:
            session.can_post = can_post
            changed = True
        session.visitor_name = str(user.get("name") or session.nome)
        session.typing = bool(snapshot.get("typing"))
        session.notice = str(snapshot.get("notice") or "")
        for raw in cast(list[dict[str, Any]], snapshot.get("messages") or []):
            message = _message_from(raw)
            if message is None or message.id in session.seen_ids:
                continue
            session.seen_ids.add(message.id)
            session.messages.append(message)
            session.last_id = max(session.last_id, message.id)
            if message.tipo is TipoMensagemChat.VISITANTE:
                self._register_processes(session, message.texto)
            changed = True
        session.messages.sort(key=lambda m: m.id)
        if changed:
            session.version += 1
        return changed

    @staticmethod
    def _register_processes(session: _Session, texto: str) -> None:
        for numero in extrair_processos(texto):
            session.processos.setdefault(numero, None)

    async def _notify(self, session: _Session) -> None:
        async with session.condition:
            session.condition.notify_all()

    async def _end(self, session: _Session, reason: str) -> None:
        if session.ended:
            return
        session.ended = True
        session.ended_reason = reason
        session.can_post = False
        session.version += 1
        self._write_partial(session)
        await self._notify(session)

    async def _wait_until(
        self,
        session: _Session,
        predicate: Callable[[_Session], bool],
        *,
        limite_segundos: float,
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + limite_segundos
        while True:
            try:
                await self._refresh(session)
            except PlaywrightError:
                # Evaluate interrompido no meio de uma troca de contexto: tenta de
                # novo até o prazo; a janela fechada _refresh já trata sozinho.
                pass
            if predicate(session):
                return
            remaining = deadline - loop.time()
            if remaining <= 0 or session.ended:
                if predicate(session):
                    return
                raise TimeoutError
            await asyncio.sleep(min(0.5, remaining))

    async def _stop_monitor(self, session: _Session) -> None:
        task = session.monitor
        session.monitor = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def _discard_browser(self, session: _Session) -> None:
        context = session.context
        session.context = None
        session.page = None
        if context is not None:
            try:
                await context.close()
            except PlaywrightError:
                pass

    # -- transcrição --------------------------------------------------------------------

    def _transcript_dir(self) -> Path:
        root = self.config.downloads_dir.expanduser().resolve()
        return secure_subdirectory(root, "TJPE", "CAP1G")

    def _partial_transcript_path(self, session: _Session) -> Path:
        stamp = session.started_at.astimezone(_FUSO_TRIBUNAL).strftime("%Y%m%d-%H%M%S")
        return self._transcript_dir() / f"{stamp}-chat-cap1g.parcial.md"

    def _write_partial(self, session: _Session) -> None:
        path = session.partial_path
        if path is None:
            return
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(self._transcript(session))
        except OSError:
            # O parcial é rede de segurança, não o registro final; falhar aqui não
            # pode derrubar a conversa.
            pass

    def _publish_final(self, session: _Session) -> None:
        if session.final_path is not None and session.sha256 is not None:
            return
        body = self._transcript(session).encode("utf-8")
        digest = hashlib.sha256(body).hexdigest()
        stamp = session.started_at.astimezone(_FUSO_TRIBUNAL).strftime("%Y%m%d-%H%M%S")
        target = self._transcript_dir() / f"{stamp}-chat-cap1g-{digest[:12]}.md"
        atomic_publish(target, body)
        atomic_publish(
            target.with_suffix(target.suffix + ".sha256"),
            f"{digest}  {target.name}\n".encode("ascii"),
        )
        session.final_path = target
        session.sha256 = digest
        partial = session.partial_path
        if partial is not None:
            partial.unlink(missing_ok=True)
            session.partial_path = None

    def _transcript(self, session: _Session) -> str:
        started = session.started_at.astimezone(_FUSO_TRIBUNAL)
        processos = ", ".join(session.processos) if session.processos else "nenhum"
        lines = [
            "# Atendimento no chat da CAP1G/TJPE",
            "",
            f"- Iniciado em: {started:%Y-%m-%d %H:%M:%S} ({_FUSO_TRIBUNAL.key})",
            f"- Visitante: {session.nome} <{session.email}>",
            f"- Conversa Mibew: {session.thread_id if session.thread_id else 'não informada'}",
            f"- Processos citados: {processos}",
            f"- Estado: {_estado(session).value}",
        ]
        if session.ended:
            lines.append(f"- Encerramento: {session.ended_reason or 'sem motivo registrado'}")
        lines.extend(["", "## Mensagens", ""])
        for message in session.messages:
            when = message.enviada_em.astimezone(_FUSO_TRIBUNAL).strftime("%H:%M:%S")
            texto = message.texto.replace("\n", "\n  ")
            if message.tipo in {TipoMensagemChat.VISITANTE, TipoMensagemChat.OPERADOR}:
                author = message.autor or message.tipo.value
                lines.append(f"- [{when}] **{author}**: {texto}")
            else:
                lines.append(f"- [{when}] ({message.tipo.value}) {texto}")
        lines.append("")
        return "\n".join(lines)

    # -- modelos ------------------------------------------------------------------------

    def _snapshot(self, session: _Session, *, baseline: int, reused: bool) -> SessaoChatCap1g:
        new_messages = [m for m in session.messages if m.id > baseline]
        session.delivered_id = session.last_id
        session.delivered_version = session.version
        operador = next(
            (m.autor for m in reversed(session.messages) if m.tipo is TipoMensagemChat.OPERADOR),
            None,
        )
        estado = _estado(session)
        if session.ended:
            aviso = (
                f"Atendimento encerrado: {session.ended_reason}. Use encerrar_chat_cap1g "
                "para gravar a transcrição final com SHA-256."
            )
        elif estado is EstadoChatCap1g.EM_ATENDIMENTO:
            aviso = (
                "Operador em linha. Envie uma mensagem por vez, objetiva, e aguarde a "
                "resposta antes de insistir; a conversa segue visível na janela do Chrome."
            )
        else:
            aviso = (
                "Aguardando operador. A janela do Chrome mantém a conversa viva; use "
                "aguardar_resposta_chat_cap1g em vez de reenviar a mensagem."
            )
        if session.missing_fields and not session.ended:
            aviso = (
                f"O formulário do chat não tinha campo de {' e '.join(session.missing_fields)}; "
                "identifique-se na conversa, como pedem as boas práticas da CAP1G. " + aviso
            )
        if session.opening_warning:
            aviso = session.opening_warning + " " + aviso
        limite = self.config.cap1g_processos_por_chat
        restantes = max(0, limite - len(session.processos))
        if restantes == 0 and not session.ended:
            aviso = (
                f"Limite de {limite} processos deste atendimento atingido: para outros "
                "processos, encerre este chat e inicie um novo. " + aviso
            )
        if reused:
            aviso = (
                "Conversa já iniciada por esta preparação; nenhum novo chat foi aberto. " + aviso
            )
        encerrado = session.ended or session.state == _ESTADO_ENCERRADO
        return SessaoChatCap1g(
            referencia_chat=session.reference,
            estado=estado,
            pode_enviar=session.can_post and not encerrado,
            operador=operador,
            operador_digitando=session.typing and not session.ended,
            aviso_do_chat=session.notice or None,
            nome_visitante=session.visitor_name or session.nome,
            mensagens=new_messages,
            total_mensagens=len(session.messages),
            ultimo_id=session.last_id,
            novas_mensagens=len(new_messages),
            processos_solicitados=list(session.processos),
            processos_restantes=restantes,
            limite_processos_por_atendimento=limite,
            encerrado=encerrado,
            iniciado_em=session.started_at,
            transcricao=str(session.final_path or session.partial_path or "") or None,
            aviso=aviso,
        )

    def _closure(self, session: _Session, *, reused: bool) -> EncerramentoChatCap1g:
        assert session.final_path is not None and session.sha256 is not None
        encerrado_por: Literal["visitante", "operador", "indeterminado"]
        if session.closed_by_visitor:
            encerrado_por = "visitante"
        elif session.state == _ESTADO_ENCERRADO:
            encerrado_por = "operador"
        else:
            encerrado_por = "indeterminado"
        aviso = (
            "Transcrição gravada localmente com permissão restrita e sidecar SHA-256. "
            "O que o operador prometeu no chat não é decisão judicial: confira nos Autos."
        )
        if reused:
            aviso = (
                "O atendimento já estava encerrado; transcrição anterior reaproveitada. " + aviso
            )
        final_path = session.final_path
        return EncerramentoChatCap1g(
            referencia_chat=session.reference,
            estado_final=_estado(session),
            encerrado_por=encerrado_por,
            mensagens=list(session.messages),
            total_mensagens=len(session.messages),
            processos_solicitados=list(session.processos),
            transcricao=str(final_path),
            caminho_sha256=str(final_path.with_suffix(final_path.suffix + ".sha256")),
            sha256=session.sha256,
            iniciado_em=session.started_at,
            aviso=aviso,
        )

    # -- registros ----------------------------------------------------------------------

    def _resolve_plan(self, reference: str) -> _Plan:
        if _REFERENCE.fullmatch(reference) is None:
            raise ValidacaoError("referência de preparação inválida")
        plan = self._plans.get(reference)
        if plan is None:
            raise ValidacaoError("referência de preparação desconhecida ou expirada")
        self._plans.move_to_end(reference)
        return plan

    def _resolve_session(self, reference: str) -> _Session:
        if _REFERENCE.fullmatch(reference) is None:
            raise ValidacaoError("referência de chat inválida")
        session = self._sessions.get(reference)
        if session is None:
            raise ValidacaoError("referência de chat desconhecida; inicie um novo atendimento")
        return session

    def _page(self, session: _Session) -> Page:
        page = session.page
        if page is None or page.is_closed():
            raise ServicoIndisponivelError("a janela do chat foi fechada; o atendimento acabou")
        return page

    def _trim_plans(self, limit: int = 50) -> None:
        while len(self._plans) > limit:
            victim = next(
                (ref for ref, plan in self._plans.items() if plan.session_reference is None),
                None,
            )
            if victim is None:
                break
            del self._plans[victim]

    def _trim_sessions(self, limit: int = 20) -> None:
        for reference in list(self._sessions):
            if len(self._sessions) <= limit:
                break
            session = self._sessions[reference]
            if session.ended and reference != self._active:
                del self._sessions[reference]


async def _only_documents(route: Route) -> None:
    if route.request.resource_type == "document":
        await route.fallback()
    else:
        await route.abort("blockedbyclient")


def _group_name(options: dict[str, Any]) -> str | None:
    for key in ("surveyOptions", "leaveMessageOptions"):
        section = options.get(key)
        if isinstance(section, dict):
            section_dict = cast(dict[str, Any], section)
            for form_key in ("surveyForm", "leaveMessageForm"):
                form = section_dict.get(form_key)
                if isinstance(form, dict):
                    name = cast(dict[str, Any], form).get("groupName")
                    if isinstance(name, str) and name:
                        return name
    return None


def _as_int(value: object, *, default: int | None = 0) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return default


def _message_from(raw: dict[str, Any]) -> MensagemChatCap1g | None:
    identifier = _as_int(raw.get("id"), default=None)
    if identifier is None or identifier < 0:
        return None
    kind = _as_int(raw.get("kind"), default=None)
    tipo = _TIPOS.get(kind if kind is not None else -1, TipoMensagemChat.PLUGIN)
    created = _as_int(raw.get("created"), default=0) or 0
    when = datetime.fromtimestamp(created, UTC) if created > 0 else datetime.now(UTC)
    autor = _plain_text(str(raw.get("name") or "")) or None
    return MensagemChatCap1g(
        id=identifier,
        tipo=tipo,
        autor=autor if tipo in {TipoMensagemChat.VISITANTE, TipoMensagemChat.OPERADOR} else None,
        texto=_plain_text(str(raw.get("message") or "")),
        enviada_em=when,
    )


def _plain_text(value: str) -> str:
    # O Mibew guarda texto puro, mas entrega com <br/> e entidades; a transcrição
    # e o modelo devolvem o que o operador escreveu, não o que o navegador pintou.
    without_breaks = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    return html.unescape(_HTML_TAG.sub("", without_breaks)).strip()


def _estado(session: _Session) -> EstadoChatCap1g:
    estado = (
        EstadoChatCap1g.INDETERMINADO
        if session.state is None
        else _ESTADOS.get(session.state, EstadoChatCap1g.INDETERMINADO)
    )
    if not session.ended or estado is EstadoChatCap1g.ENCERRADO:
        return estado
    # Acabou sem o chat ter sido fechado: o Mibew vai marcar o visitante como
    # "saiu", e é isso que a leitura deve dizer — não "em atendimento".
    return EstadoChatCap1g.ENCERRADO if session.closed_by_visitor else EstadoChatCap1g.ABANDONADO
