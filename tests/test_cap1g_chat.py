from __future__ import annotations

# pyright: reportPrivateUsage=false
import json
import stat
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import pytest
from playwright.async_api import Browser, BrowserContext, Page, Route, async_playwright

from mcp_pje_tjpe.browser import BrowserManager
from mcp_pje_tjpe.cap1g_chat import (
    Cap1gChatService,
    dentro_do_horario,
    extrair_opcoes_mibew,
    extrair_processos,
    frase_confirmacao_chat,
    normalizar_email,
    normalizar_mensagem,
    normalizar_nome,
)
from mcp_pje_tjpe.config import Settings
from mcp_pje_tjpe.errors import ServicoIndisponivelError, ValidacaoError
from mcp_pje_tjpe.models import EstadoChatCap1g, TipoMensagemChat

CHAT_URL = "https://www.tjpe.jus.br/mibew/index.php/chat?locale=pt-br&style=default&group=1"

# Configuração que o Mibew do TJPE embute na página, colhida em 2026-09-10 fora do
# horário. A versão online só troca startFrom e a seção de opções.
_PAGE_OPTIONS = {
    "server": {"url": "/mibew/index.php/thread/update", "requestsFrequency": "2"},
    "page": {
        "style": "default",
        "mibewBasePath": "/mibew",
        "mibewBaseUrl": "/mibew/index.php",
        "stylePath": "/mibew/styles/chats/default",
        "company": {"name": "CAP TJPE", "chatLogoURL": ""},
        "mibewHost": "https://portal.tjpe.jus.br/",
        "title": "CAP TJPE",
    },
}
_OFFLINE_OPTIONS = {
    **_PAGE_OPTIONS,
    "startFrom": "leaveMessage",
    "leaveMessageOptions": {
        "leaveMessageForm": {
            "name": "Visitante",
            "email": None,
            "groupId": 1,
            "groupName": "CAP",
            "info": None,
            "referrer": None,
            "showCaptcha": False,
            "groups": False,
        },
        "page": {"title": "CAP: Não há operadores disponíveis no momento"},
    },
}
_ONLINE_OPTIONS = {
    **_PAGE_OPTIONS,
    "startFrom": "survey",
    "surveyOptions": {
        "surveyForm": {
            "name": "Visitante",
            "email": "",
            "groupId": 1,
            "groupName": "CAP",
            "info": "",
            "referrer": "",
            "showEmail": True,
            "showMessage": True,
            "canChangeName": True,
            "groups": False,
        }
    },
}

# Marcação idêntica aos templates Handlebars servidos pelo TJPE (survey/form,
# chat/layout, chat/message_form, chat/message), renderizados em 2026-09-10.
_SURVEY_HTML = """
<div id="headers"><div class="info-message">Bem-vindo(a) ao atendimento por chat do TJPE.
Por favor, preencha o formulário abaixo e clique no botão Iniciar Chat.</div></div>
<div id="content-wrapper">
<form name="surveyForm" method="post" action="">
    <input type="hidden" name="style" value=""/>
    <input type="hidden" name="info" value=""/>
    <input type="hidden" name="referrer" value=""/>
    <input type="hidden" name="survey" value="on"/>
    <input type="hidden" name="group" value="1"/>
    <div class="errors"></div>
    <table class="form">
        __IDENTITY_ROWS__
        __MESSAGE_ROW__
    </table>
    <br/>
    <a href="javascript:void(0);" class="form-button" id="submit-survey">Iniciar Chat</a>
    <div class="clear">&nbsp;</div>
</form>
<div id="ajax-loader"><img src="" alt="Loading..." /></div>
</div>
"""
_IDENTITY_ROWS = """
        <tr><td><strong>Nome:</strong></td>
            <td><input type="text" name="name" size="50" value="Visitante"
                       class="username" /></td></tr>
        <tr><td><strong>Email:</strong></td>
            <td><input type="text" name="email" size="50" value="" class="username"/></td></tr>
"""
_MESSAGE_ROW = """
        <tr><td><strong>Pergunta Inicial:</strong></td>
            <td valign="top"><textarea id="message-survey" name="message" tabindex="0"
                                       cols="45" rows="2"></textarea></td></tr>
"""
_CHAT_HTML = """
<div id="chat-header"><div id="controls-region"></div></div>
<div id="chat">
    <div id="avatar-region"></div>
    <div id="messages-region"></div>
    <div id="status-region"></div>
</div>
<div id="message-form-region">
<div id="message">
    <textarea id="message-input" class="message" tabindex="0" rows="4" cols="10"></textarea>
</div>
<div id="send"><div id="post-message">
    <a href="javascript:void(0)" id="send-message" title="Enviar Mensagem">Enviar (Ctrl-Enter)</a>
</div></div>
</div>
"""
_LEAVE_HTML = """
<div id="headers"><div id="description-region"><div class="info-message">Desculpe, não há
agentes disponíveis no momento.</div></div></div>
<div id="content-wrapper"><form name="leaveMessageForm" method="post" action="">
<div class="errors"></div><table class="form"></table></form></div>
"""

_FAKE_APP_JS = """
(function () {
  function Model(attrs) { this.attributes = attrs; }
  Model.prototype.get = function (key) { return this.attributes[key]; };
  Model.prototype.set = function (values) {
    for (var key in values) { this.attributes[key] = values[key]; }
  };
  Model.prototype.toJSON = function () { return JSON.parse(JSON.stringify(this.attributes)); };
  function Collection() { this.items = []; }
  Collection.prototype.toJSON = function () { return JSON.parse(JSON.stringify(this.items)); };
  Collection.prototype.add = function (item) { this.items.push(item); render(); };

  var thread, user, messages, typing, notice, lastId = 0;
  var fake = window.__mibewFake = {
    posted: [], closeRequested: false, survey: null, forceOffline: false, rejectEmail: null,
    swallowPosts: false
  };

  function render() {
    var region = document.getElementById("messages-region");
    if (!region) { return; }
    region.innerHTML = messages.items.map(function (m) {
      var kind = {1: "user", 2: "agent", 4: "info", 5: "connection", 6: "event"}[m.kind] || "";
      var name = (m.kind === 1 || m.kind === 2)
        ? "<span class='name-" + kind + "'>" + m.name + "</span>: " : "";
      return "<div class='message'><span>" + m.created + "</span> " + name
        + "<span class='message-" + kind + "'>" + m.message + "</span><br/></div>";
    }).join("");
  }

  function addMessage(kind, name, text) {
    lastId += 1;
    thread.set({lastId: lastId});
    messages.add({id: lastId, kind: kind, name: name, message: text,
                  created: Math.floor(Date.now() / 1000)});
    return lastId;
  }

  fake.operator = function (text, name) {
    thread.set({state: 2, agentId: 7});
    return addMessage(2, name || "Maria (CAP)", text);
  };
  fake.info = function (text) { return addMessage(4, "", text); };
  fake.visitorTypes = function (text) { return addMessage(1, user.get("name"), text); };
  fake.typing = function (value) { typing.set({visible: !!value}); };
  fake.operatorCloses = function () {
    thread.set({state: 3});
    user.set({canPost: false});
    // Como no Mibew real, o aviso chega num ciclo de atualização posterior ao estado.
    setTimeout(function () { addMessage(4, "", "Conversa encerrada pelo operador"); }, 30);
  };

  function startChat(name, email, message) {
    document.body.className = "chat";
    document.getElementById("main-region").innerHTML = __CHAT_HTML__;
    thread = new Model({id: 4242, token: "secret-thread-token", lastId: 0,
                        userId: "visitor-1", agentId: null, state: 1});
    user = new Model({isAgent: false, name: name, canPost: true, typing: false,
                      canChangeName: true, defaultName: false});
    messages = new Collection();
    typing = new Model({visible: false});
    notice = new Model({visible: false, message: ""});
    window.Mibew.Objects = {
      Models: {
        thread: thread, user: user,
        Status: {typing: typing, message: notice},
        Controls: {close: {closeThread: function () {
          fake.closeRequested = true;
          setTimeout(function () {
            thread.set({state: 3});
            user.set({canPost: false});
            addMessage(4, "", "Conversa encerrada pelo visitante");
          }, 30);
        }}}
      },
      Collections: {messages: messages}
    };
    addMessage(4, "", "Aguarde, um operador ir&aacute; atend&ecirc;-lo.<br/>Obrigado.");
    if (message) { setTimeout(function () { addMessage(1, name, message); }, 20); }
    var input = document.getElementById("message-input");
    document.getElementById("send-message").addEventListener("click", function () {
      var text = input.value;
      if (!text || input.disabled) { return; }
      input.disabled = true;
      fake.posted.push(text);
      if (fake.swallowPosts) { return; }
      setTimeout(function () {
        addMessage(1, user.get("name"), text);
        input.value = "";
        input.disabled = false;
      }, 30);
    });
  }

  function renderSurvey() {
    document.body.className = "survey";
    document.getElementById("main-region").innerHTML = __SURVEY_HTML__;
    document.getElementById("submit-survey").addEventListener("click", function () {
      var nameField = document.querySelector("input[name='name']");
      var emailField = document.querySelector("input[name='email']");
      var name = nameField ? nameField.value.trim() : "Visitante";
      var email = emailField ? emailField.value.trim() : "";
      var field = document.getElementById("message-survey");
      var message = field ? field.value : "";
      var errors = document.querySelector(".errors");
      if (!name) { errors.textContent = "Name is required."; return; }
      if (fake.rejectEmail && email === fake.rejectEmail) {
        errors.textContent = "Wrong email address."; return;
      }
      if (fake.forceOffline) {
        document.getElementById("main-region").innerHTML = __LEAVE_HTML__; return;
      }
      fake.survey = {name: name, email: email, message: message};
      startChat(name, email, message);
    });
  }

  window.Mibew.Application = {start: function (options) {
    if (options.startFrom === "survey") { renderSurvey(); }
    else if (options.startFrom === "chat") { startChat("Visitante", "", ""); }
    else { document.body.className = "leaveMessage";
           document.getElementById("main-region").innerHTML = __LEAVE_HTML__; }
  }};
})();
"""


def _fake_page(
    options: Mapping[str, object],
    *,
    message_field: bool = True,
    identity_fields: bool = True,
) -> str:
    survey = _SURVEY_HTML.replace(
        "__IDENTITY_ROWS__", _IDENTITY_ROWS if identity_fields else ""
    ).replace("__MESSAGE_ROW__", _MESSAGE_ROW if message_field else "")
    app = (
        _FAKE_APP_JS.replace("__CHAT_HTML__", json.dumps(_CHAT_HTML))
        .replace("__SURVEY_HTML__", json.dumps(survey))
        .replace("__LEAVE_HTML__", json.dumps(_LEAVE_HTML))
    )
    start = json.dumps(options, ensure_ascii=False)
    body_class = "leaveMessage" if options.get("startFrom") == "leaveMessage" else "survey"
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Mibew Messenger</title>
<script>var Mibew = Mibew || {{}}; Mibew.PluginOptions = [];</script>
<script>{app}</script>
</head>
<body class="{body_class}">
<div id="main-region"></div>
<script>Mibew.Application.start({start});</script>
</body></html>"""


class _FakeBrowserManager:
    """Serve o Mibew falso em contextos reais do Chromium, como faria o BrowserManager."""

    def __init__(self, browser: Browser, html: Callable[[], str]) -> None:
        self.browser = browser
        self.html = html
        self.contexts: list[BrowserContext] = []

    async def new_context(self) -> BrowserContext:
        context = await self.browser.new_context()

        async def fulfill(route: Route) -> None:
            await route.fulfill(
                status=200, content_type="text/html; charset=utf-8", body=self.html()
            )

        await context.route(lambda url: url.startswith("https://www.tjpe.jus.br/mibew/"), fulfill)
        self.contexts.append(context)
        return context

    @asynccontextmanager
    async def page(self) -> AsyncGenerator[Page]:
        context = await self.new_context()
        page = await context.new_page()
        try:
            yield page
        finally:
            await context.close()

    async def close(self) -> None:
        for context in self.contexts:
            await context.close()

    def chat_page(self) -> Page:
        return next(page for context in reversed(self.contexts) for page in context.pages)


def _texto(caminho: str) -> str:
    return Path(caminho).read_text(encoding="utf-8")


def _existe(caminho: str) -> bool:
    return Path(caminho).is_file()


def _modo(caminho: str) -> int:
    return stat.S_IMODE(Path(caminho).stat().st_mode)


def _parciais(caminho: str) -> list[Path]:
    return list(Path(caminho).parent.glob("*.parcial.md"))


def _service(
    browser: Browser,
    tmp_path: Path,
    html: Callable[[], str],
    *,
    echo_seconds: float = 15.0,
    limite_processos: int = 5,
) -> tuple[Cap1gChatService, _FakeBrowserManager]:
    manager = _FakeBrowserManager(browser, html)
    settings = Settings(
        timeout_ms=5_000,
        downloads_dir=tmp_path / "downloads",
        cap1g_processos_por_chat=limite_processos,
    )
    service = Cap1gChatService(
        cast(BrowserManager, manager),
        cast(BrowserManager, manager),
        settings,
        poll_seconds=0.1,
        echo_seconds=echo_seconds,
    )
    return service, manager


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def browser() -> AsyncIterator[Browser]:
    async with async_playwright() as playwright:
        instance = await playwright.chromium.launch(headless=True)
        try:
            yield instance
        finally:
            await instance.close()


# -- parsing e validação ----------------------------------------------------------------


def test_extracts_mibew_options_from_real_offline_markup() -> None:
    html = (
        "<script>jQuery(document).ready(function() {Mibew.Application.start("
        + json.dumps(_OFFLINE_OPTIONS)
        + ");});</script>"
    )
    options = extrair_opcoes_mibew(html)
    assert options["startFrom"] == "leaveMessage"
    assert options["leaveMessageOptions"]["leaveMessageForm"]["groupName"] == "CAP"


def test_embedded_options_tolerate_whitespace_before_the_json() -> None:
    html = "Mibew.Application.start(\n  " + json.dumps(_ONLINE_OPTIONS) + ");"
    assert extrair_opcoes_mibew(html)["startFrom"] == "survey"


def test_plain_text_keeps_comparisons_and_drops_markup() -> None:
    from mcp_pje_tjpe.cap1g_chat import _plain_text

    assert _plain_text("valor &lt; 10 e &gt; 5") == "valor < 10 e > 5"
    assert _plain_text("valor < 10 e > 5") == "valor < 10 e > 5"
    assert _plain_text('veja <a href="https://x">o link</a><br/>ok') == "veja o link\nok"


def test_unrecognized_page_is_reported_as_unavailable_service() -> None:
    with pytest.raises(ServicoIndisponivelError, match="não é mais um Mibew"):
        extrair_opcoes_mibew("<html><body>Manutenção</body></html>")
    with pytest.raises(ServicoIndisponivelError, match="não pôde ser lida"):
        extrair_opcoes_mibew("Mibew.Application.start({broken")


def test_identification_and_message_rules_follow_cap1g_good_practices() -> None:
    assert normalizar_nome("  Ana   Exemplo ") == "Ana Exemplo"
    with pytest.raises(ValidacaoError, match="nome"):
        normalizar_nome("1")
    assert normalizar_email(" Advogado@Exemplo.COM ") == "advogado@exemplo.com"
    with pytest.raises(ValidacaoError, match="e-mail"):
        normalizar_email("sem-arroba")
    assert normalizar_mensagem("  Bom dia,\n\n  peço  andamento. ") == "Bom dia,\n\npeço andamento."
    with pytest.raises(ValidacaoError, match="vazia"):
        normalizar_mensagem("   ")
    with pytest.raises(ValidacaoError, match="no máximo"):
        normalizar_mensagem("x" * 2001)
    with pytest.raises(ValidacaoError, match="caixa alta"):
        normalizar_mensagem("PEÇO O ANDAMENTO DO PROCESSO IMEDIATAMENTE")
    # Siglas e número de processo não contam como gritar.
    assert normalizar_mensagem("OAB/PE 12345, processo 0000001-02.2026.8.17.0001, peço andamento")


def test_confirmation_phrase_is_bound_to_normalized_identification() -> None:
    phrase = frase_confirmacao_chat(" Ana  Exemplo ", "Ana@Exemplo.com")
    assert phrase == "CONFIRMO INICIAR O CHAT DA CAP1G DO TJPE COMO ANA EXEMPLO (ana@exemplo.com)"


def test_process_numbers_are_extracted_once_each_in_official_format() -> None:
    texto = (
        "Peço andamento dos processos 9999901-17.2099.8.17.9999 e 99999020220998179999; "
        "o primeiro, 9999901-17.2099.8.17.9999, é urgente. OAB/PE 12345, CPF 111.111.111-11."
    )
    assert extrair_processos(texto) == [
        "9999901-17.2099.8.17.9999",
        "9999902-02.2099.8.17.9999",
    ]
    assert extrair_processos("sem processo; protocolo 12345678901234567890123") == []


def test_business_hours_follow_recife_clock() -> None:
    recife = ZoneInfo("America/Recife")
    assert dentro_do_horario(datetime(2026, 9, 10, 8, 0, tzinfo=recife))
    assert dentro_do_horario(datetime(2026, 9, 10, 18, 59, tzinfo=recife))
    assert not dentro_do_horario(datetime(2026, 9, 10, 19, 0, tzinfo=recife))
    assert not dentro_do_horario(datetime(2026, 9, 12, 10, 0, tzinfo=recife))
    # 21h UTC de quinta ainda são 18h em Recife.
    assert dentro_do_horario(datetime(2026, 9, 10, 21, 30, tzinfo=UTC))


# -- disponibilidade e preparação ---------------------------------------------------------


@pytest.mark.anyio
async def test_verificar_reads_offline_and_online_without_opening_a_chat(
    browser: Browser, tmp_path: Path
) -> None:
    online = False
    service, manager = _service(
        browser, tmp_path, lambda: _fake_page(_ONLINE_OPTIONS if online else _OFFLINE_OPTIONS)
    )
    try:
        offline_status = await service.verificar()
        assert offline_status.disponivel is False
        assert offline_status.modo_inicial == "leaveMessage"
        assert offline_status.grupo == "CAP"
        assert offline_status.url == CHAT_URL

        online = True
        online_status = await service.verificar()
        assert online_status.disponivel is True
        assert online_status.modo_inicial == "survey"
        assert service._sessions == {}
        # A verificação usa contextos descartáveis; nenhuma página do chat sobrevive.
        assert all(not context.pages for context in manager.contexts)
    finally:
        await manager.close()


@pytest.mark.anyio
async def test_preparar_refuses_when_no_operator_is_online(
    browser: Browser, tmp_path: Path
) -> None:
    service, manager = _service(browser, tmp_path, lambda: _fake_page(_OFFLINE_OPTIONS))
    try:
        with pytest.raises(ServicoIndisponivelError, match="sem operador"):
            await service.preparar("Ana Exemplo", "ana@exemplo.com", "Peço andamento.")
        assert service._plans == {}
    finally:
        await manager.close()


# -- conversa completa --------------------------------------------------------------------


@pytest.mark.anyio
async def test_full_conversation_is_confirmed_monitored_and_transcribed(
    browser: Browser, tmp_path: Path
) -> None:
    service, manager = _service(browser, tmp_path, lambda: _fake_page(_ONLINE_OPTIONS))
    try:
        preparation = await service.preparar(
            "Ana Exemplo",
            "ana@exemplo.com",
            "Bom dia. OAB/PE 12345. Peço informar o andamento do processo.",
        )
        assert preparation.disponivel is True
        assert "nova mensagem" in preparation.aviso

        with pytest.raises(ValidacaoError, match="confirmação incorreta"):
            await service.iniciar(preparation.referencia_preparo, "confirmo")
        assert service._sessions == {}

        session = await service.iniciar(
            preparation.referencia_preparo, preparation.frase_confirmacao
        )
        assert session.estado is EstadoChatCap1g.AGUARDANDO_OPERADOR
        assert session.pode_enviar is True
        assert session.nome_visitante == "Ana Exemplo"
        assert session.operador is None
        assert [m.tipo for m in session.mensagens] == [
            TipoMensagemChat.INFO,
            TipoMensagemChat.VISITANTE,
        ]
        # Entidades e <br/> do Mibew não vazam para o modelo nem para a transcrição.
        assert session.mensagens[0].texto == "Aguarde, um operador irá atendê-lo.\nObrigado."
        assert session.mensagens[1].autor == "Ana Exemplo"
        assert session.transcricao is not None and session.transcricao.endswith(".parcial.md")

        page = manager.chat_page()
        survey = await page.evaluate("window.__mibewFake.survey")
        assert survey == {
            "name": "Ana Exemplo",
            "email": "ana@exemplo.com",
            "message": "Bom dia. OAB/PE 12345. Peço informar o andamento do processo.",
        }
        # A mesma preparação não abre um segundo chat.
        again = await service.iniciar(preparation.referencia_preparo, preparation.frase_confirmacao)
        assert again.referencia_chat == session.referencia_chat
        assert "nenhum novo chat foi aberto" in again.aviso

        other = await service.preparar("Outra Pessoa", "outra@exemplo.com", "Olá")
        with pytest.raises(ValidacaoError, match="já existe um atendimento"):
            await service.iniciar(other.referencia_preparo, other.frase_confirmacao)

        # Sem novidade, a espera devolve zero mensagens novas e o estado atual.
        idle = await service.aguardar(session.referencia_chat, timeout_segundos=1)
        assert idle.novas_mensagens == 0
        assert idle.estado is EstadoChatCap1g.AGUARDANDO_OPERADOR

        await page.evaluate("window.__mibewFake.typing(true)")
        await page.evaluate("window.__mibewFake.operator('Bom dia, em que posso ajudar?')")
        answered = await service.aguardar(session.referencia_chat, timeout_segundos=5)
        assert answered.estado is EstadoChatCap1g.EM_ATENDIMENTO
        assert answered.operador == "Maria (CAP)"
        assert [m.texto for m in answered.mensagens] == ["Bom dia, em que posso ajudar?"]
        assert answered.mensagens[0].tipo is TipoMensagemChat.OPERADOR

        sent = await service.enviar(
            session.referencia_chat, "Peço a expedição da certidão de trânsito em julgado."
        )
        assert [m.tipo for m in sent.mensagens] == [TipoMensagemChat.VISITANTE]
        assert sent.mensagens[0].texto == "Peço a expedição da certidão de trânsito em julgado."
        assert "logo após outra sua" not in sent.aviso
        assert await page.evaluate("window.__mibewFake.posted") == [
            "Peço a expedição da certidão de trânsito em julgado."
        ]

        with pytest.raises(ValidacaoError, match="idêntica"):
            await service.enviar(
                session.referencia_chat, "Peço a expedição da certidão de trânsito em julgado."
            )
        insistent = await service.enviar(session.referencia_chat, "Consegue verificar hoje?")
        assert "logo após outra sua" in insistent.aviso

        everything = await service.ler(session.referencia_chat)
        assert everything.total_mensagens == 5
        assert everything.novas_mensagens == 5
        since = await service.ler(session.referencia_chat, desde_id=everything.ultimo_id - 1)
        assert [m.texto for m in since.mensagens] == ["Consegue verificar hoje?"]

        closure = await service.encerrar(session.referencia_chat)
        assert closure.encerrado_por == "visitante"
        assert closure.estado_final is EstadoChatCap1g.ENCERRADO
        assert closure.total_mensagens == 6
        # O encerramento passou pelo controle do Mibew, não por fechar a janela.
        assert closure.mensagens[-1].texto == "Conversa encerrada pelo visitante"
        assert page.is_closed()

        assert _existe(closure.transcricao) and _existe(closure.caminho_sha256)
        expected_dir = str((tmp_path / "downloads" / "TJPE" / "CAP1G").resolve())
        assert closure.transcricao.startswith(expected_dir + "/")
        assert closure.caminho_sha256 == closure.transcricao + ".sha256"
        assert _modo(closure.transcricao) == 0o600
        transcript_name = closure.transcricao.rsplit("/", 1)[1]
        assert _texto(closure.caminho_sha256) == f"{closure.sha256}  {transcript_name}\n"
        body = _texto(closure.transcricao)
        assert "**Ana Exemplo**: Peço a expedição da certidão" in body
        assert "**Maria (CAP)**: Bom dia, em que posso ajudar?" in body
        assert "(info) Aguarde, um operador irá atendê-lo." in body
        assert "Encerramento: encerrado pelo visitante" in body
        assert _parciais(closure.transcricao) == []

        # Encerrar de novo não reabre nada e devolve a mesma transcrição.
        repeated = await service.encerrar(session.referencia_chat)
        assert repeated.transcricao == closure.transcricao
        assert "já estava encerrado" in repeated.aviso

        # O token da conversa é credencial da página e nunca aparece nos modelos.
        for model in (session, answered, sent, everything, closure):
            assert "secret-thread-token" not in model.model_dump_json()
    finally:
        await service.close()
        await manager.close()


@pytest.mark.anyio
async def test_operator_closing_ends_the_session_and_blocks_sending(
    browser: Browser, tmp_path: Path
) -> None:
    service, manager = _service(browser, tmp_path, lambda: _fake_page(_ONLINE_OPTIONS))
    try:
        preparation = await service.preparar("Ana Exemplo", "ana@exemplo.com", "Olá, bom dia")
        session = await service.iniciar(
            preparation.referencia_preparo, preparation.frase_confirmacao
        )
        page = manager.chat_page()
        await page.evaluate("window.__mibewFake.operator('Um momento')")
        await page.evaluate("window.__mibewFake.operatorCloses()")

        ended = await service.aguardar(session.referencia_chat, timeout_segundos=5)
        assert ended.encerrado is True
        assert ended.pode_enviar is False
        assert ended.estado is EstadoChatCap1g.ENCERRADO
        assert "encerrado pelo operador" in ended.aviso

        with pytest.raises(ValidacaoError, match="encerrado"):
            await service.enviar(session.referencia_chat, "Ainda está aí?")

        closure = await service.encerrar(session.referencia_chat)
        assert closure.encerrado_por == "operador"
        assert [m.texto for m in closure.mensagens][-2:] == [
            "Um momento",
            "Conversa encerrada pelo operador",
        ]
        assert "Encerramento: encerrado pelo operador" in _texto(closure.transcricao)
    finally:
        await service.close()
        await manager.close()


@pytest.mark.anyio
async def test_initial_message_goes_to_chat_when_survey_has_no_message_field(
    browser: Browser, tmp_path: Path
) -> None:
    service, manager = _service(
        browser, tmp_path, lambda: _fake_page(_ONLINE_OPTIONS, message_field=False)
    )
    try:
        preparation = await service.preparar("Ana Exemplo", "ana@exemplo.com", "Peço andamento.")
        session = await service.iniciar(
            preparation.referencia_preparo, preparation.frase_confirmacao
        )
        page = manager.chat_page()
        assert await page.evaluate("window.__mibewFake.survey.message") == ""
        assert await page.evaluate("window.__mibewFake.posted") == ["Peço andamento."]
        assert [m.texto for m in session.mensagens if m.tipo is TipoMensagemChat.VISITANTE] == [
            "Peço andamento."
        ]
        with pytest.raises(ValidacaoError, match="idêntica"):
            await service.enviar(session.referencia_chat, "Peço andamento.")
    finally:
        await service.close()
        await manager.close()


@pytest.mark.anyio
async def test_survey_without_identity_fields_still_opens_and_warns(
    browser: Browser, tmp_path: Path
) -> None:
    service, manager = _service(
        browser, tmp_path, lambda: _fake_page(_ONLINE_OPTIONS, identity_fields=False)
    )
    try:
        preparation = await service.preparar("Ana Exemplo", "ana@exemplo.com", "Olá")
        session = await service.iniciar(
            preparation.referencia_preparo, preparation.frase_confirmacao
        )
        assert session.estado is EstadoChatCap1g.AGUARDANDO_OPERADOR
        assert "não tinha campo de nome e e-mail" in session.aviso
        page = manager.chat_page()
        assert await page.evaluate("window.__mibewFake.survey") == {
            "name": "Visitante",
            "email": "",
            "message": "Olá",
        }
    finally:
        await service.close()
        await manager.close()


@pytest.mark.anyio
async def test_rejected_form_and_operators_leaving_do_not_leave_a_session_behind(
    browser: Browser, tmp_path: Path
) -> None:
    mode = "reject"

    def html() -> str:
        page = _fake_page(_ONLINE_OPTIONS)
        if mode == "reject":
            return page.replace("rejectEmail: null", "rejectEmail: 'recusado@exemplo.com'")
        return page.replace("forceOffline: false", "forceOffline: true")

    service, manager = _service(browser, tmp_path, html)
    try:
        preparation = await service.preparar("Ana Exemplo", "recusado@exemplo.com", "Olá")
        with pytest.raises(ValidacaoError, match="recusou o formulário: Wrong email"):
            await service.iniciar(preparation.referencia_preparo, preparation.frase_confirmacao)
        assert service._sessions == {}
        assert service._active is None

        mode = "offline"
        preparation = await service.preparar("Ana Exemplo", "ana@exemplo.com", "Olá")
        with pytest.raises(ServicoIndisponivelError, match="ficou sem operador"):
            await service.iniciar(preparation.referencia_preparo, preparation.frase_confirmacao)
        assert service._sessions == {}
        # Contextos das tentativas falhas foram fechados com elas.
        assert all(not context.pages for context in manager.contexts)
    finally:
        await service.close()
        await manager.close()


@pytest.mark.anyio
async def test_closing_the_window_by_hand_ends_the_session_with_a_reason(
    browser: Browser, tmp_path: Path
) -> None:
    service, manager = _service(browser, tmp_path, lambda: _fake_page(_ONLINE_OPTIONS))
    try:
        preparation = await service.preparar("Ana Exemplo", "ana@exemplo.com", "Olá")
        session = await service.iniciar(
            preparation.referencia_preparo, preparation.frase_confirmacao
        )
        await manager.chat_page().close()

        snapshot = await service.ler(session.referencia_chat)
        assert snapshot.encerrado is True
        assert snapshot.estado is EstadoChatCap1g.ABANDONADO
        assert "janela do chat fechada" in snapshot.aviso
        with pytest.raises(ValidacaoError, match="janela do chat fechada"):
            await service.enviar(session.referencia_chat, "Ainda aí?")
        closure = await service.encerrar(session.referencia_chat)
        assert closure.encerrado_por == "indeterminado"
        assert _existe(closure.transcricao)
    finally:
        await service.close()
        await manager.close()


@pytest.mark.anyio
async def test_page_that_opens_straight_into_the_chat_skips_the_survey(
    browser: Browser, tmp_path: Path
) -> None:
    mode = "survey"

    def html() -> str:
        return _fake_page({**_ONLINE_OPTIONS, "startFrom": mode})

    service, manager = _service(browser, tmp_path, html)
    try:
        preparation = await service.preparar(
            "Ana Exemplo", "ana@exemplo.com", "Bom dia, peço andamento"
        )
        mode = "chat"
        session = await service.iniciar(
            preparation.referencia_preparo, preparation.frase_confirmacao
        )
        page = manager.chat_page()
        assert await page.evaluate("window.__mibewFake.survey") is None
        assert await page.evaluate("window.__mibewFake.posted") == ["Bom dia, peço andamento"]
        assert [m.texto for m in session.mensagens if m.tipo is TipoMensagemChat.VISITANTE] == [
            "Bom dia, peço andamento"
        ]
        assert session.pode_enviar is True
    finally:
        await service.close()
        await manager.close()


@pytest.mark.anyio
async def test_unconfirmed_send_counts_as_sent_and_is_not_repeated(
    browser: Browser, tmp_path: Path
) -> None:
    service, manager = _service(
        browser, tmp_path, lambda: _fake_page(_ONLINE_OPTIONS), echo_seconds=1.0
    )
    try:
        preparation = await service.preparar("Ana Exemplo", "ana@exemplo.com", "Olá")
        session = await service.iniciar(
            preparation.referencia_preparo, preparation.frase_confirmacao
        )
        page = manager.chat_page()
        await page.evaluate("window.__mibewFake.operator('Pois não?')")
        await page.evaluate("window.__mibewFake.swallowPosts = true")

        with pytest.raises(ServicoIndisponivelError, match="não a repita"):
            await service.enviar(session.referencia_chat, "Peço a certidão de objeto e pé.")
        assert await page.evaluate("window.__mibewFake.posted") == [
            "Peço a certidão de objeto e pé."
        ]
        # O texto saiu para o tribunal: repetir é o que a CAP1G pede para não fazer.
        with pytest.raises(ValidacaoError, match="idêntica"):
            await service.enviar(session.referencia_chat, "Peço a certidão de objeto e pé.")
        assert await page.evaluate("window.__mibewFake.posted") == [
            "Peço a certidão de objeto e pé."
        ]
    finally:
        await service.close()
        await manager.close()


@pytest.mark.anyio
async def test_failed_opening_message_keeps_the_conversation_and_warns(
    browser: Browser, tmp_path: Path
) -> None:
    service, manager = _service(
        browser,
        tmp_path,
        lambda: _fake_page(_ONLINE_OPTIONS, message_field=False).replace(
            "swallowPosts: false", "swallowPosts: true"
        ),
        echo_seconds=1.0,
    )
    try:
        preparation = await service.preparar("Ana Exemplo", "ana@exemplo.com", "Olá, bom dia")
        session = await service.iniciar(
            preparation.referencia_preparo, preparation.frase_confirmacao
        )
        # A conversa existe e fica de pé; só a mensagem inicial fica sem confirmação.
        assert session.encerrado is False
        assert "mensagem inicial não foi confirmada" in session.aviso
        assert not manager.chat_page().is_closed()
        assert service._active == session.referencia_chat
        with pytest.raises(ValidacaoError, match="idêntica"):
            await service.enviar(session.referencia_chat, "Olá, bom dia")
        closure = await service.encerrar(session.referencia_chat)
        assert closure.encerrado_por == "visitante"
    finally:
        await service.close()
        await manager.close()


@pytest.mark.anyio
async def test_closing_without_the_mibew_control_is_reported_as_unconfirmed(
    browser: Browser, tmp_path: Path
) -> None:
    service, manager = _service(browser, tmp_path, lambda: _fake_page(_ONLINE_OPTIONS))
    try:
        preparation = await service.preparar("Ana Exemplo", "ana@exemplo.com", "Olá")
        session = await service.iniciar(
            preparation.referencia_preparo, preparation.frase_confirmacao
        )
        await manager.chat_page().evaluate("delete window.Mibew.Objects.Models.Controls")
        closure = await service.encerrar(session.referencia_chat)
        assert closure.encerrado_por == "visitante"
        assert "sem confirmação do chat" in _texto(closure.transcricao)
    finally:
        await service.close()
        await manager.close()


@pytest.mark.anyio
async def test_each_chat_forwards_at_most_the_allowed_processes(
    browser: Browser, tmp_path: Path
) -> None:
    def npu(i: int) -> str:
        return f"99999{i:02d}-00.2099.8.17.9999"

    service, manager = _service(
        browser, tmp_path, lambda: _fake_page(_ONLINE_OPTIONS), limite_processos=3
    )
    try:
        status = await service.verificar()
        assert status.limite_processos_por_atendimento == 3

        with pytest.raises(ValidacaoError, match="no máximo 3 por atendimento"):
            await service.preparar(
                "Ana Exemplo",
                "ana@exemplo.com",
                f"Peço andamento de {npu(1)}, {npu(2)}, {npu(3)} e {npu(4)}.",
            )

        preparation = await service.preparar(
            "Ana Exemplo", "ana@exemplo.com", f"Peço andamento de {npu(1)}."
        )
        assert preparation.processos_na_mensagem == [npu(1)]
        session = await service.iniciar(
            preparation.referencia_preparo, preparation.frase_confirmacao
        )
        assert session.processos_solicitados == [npu(1)]
        assert session.processos_restantes == 2
        page = manager.chat_page()

        # O que o advogado digita na própria janela também conta.
        await page.evaluate(f"window.__mibewFake.visitorTypes('E também o {npu(2)}, por favor')")
        typed = await service.aguardar(session.referencia_chat, timeout_segundos=5)
        assert typed.processos_solicitados == [npu(1), npu(2)]
        assert typed.processos_restantes == 1

        # Repetir um processo já citado não consome vaga.
        again = await service.enviar(session.referencia_chat, f"Sobre o {npu(1)}: há alvará?")
        assert again.processos_restantes == 1

        last = await service.enviar(session.referencia_chat, f"Por fim, o {npu(3)}.")
        assert last.processos_restantes == 0
        assert "Limite de 3 processos deste atendimento atingido" in last.aviso

        with pytest.raises(ValidacaoError, match="no máximo 3 por chat"):
            await service.enviar(session.referencia_chat, f"Ah, e o {npu(4)} também.")
        # Sem processo novo, a conversa segue normalmente.
        await service.enviar(session.referencia_chat, "Obrigada pela atenção.")

        closure = await service.encerrar(session.referencia_chat)
        assert closure.processos_solicitados == [npu(1), npu(2), npu(3)]
        assert f"Processos citados: {npu(1)}, {npu(2)}, {npu(3)}" in _texto(closure.transcricao)

        # Outro lote é outro chat: a sessão seguinte começa do zero.
        next_preparation = await service.preparar(
            "Ana Exemplo", "ana@exemplo.com", f"Peço andamento de {npu(4)}."
        )
        next_session = await service.iniciar(
            next_preparation.referencia_preparo, next_preparation.frase_confirmacao
        )
        assert next_session.processos_solicitados == [npu(4)]
        assert next_session.processos_restantes == 2
    finally:
        await service.close()
        await manager.close()


@pytest.mark.anyio
async def test_references_and_timeouts_are_validated_before_touching_the_browser(
    browser: Browser, tmp_path: Path
) -> None:
    service, manager = _service(browser, tmp_path, lambda: _fake_page(_ONLINE_OPTIONS))
    try:
        with pytest.raises(ValidacaoError, match="referência de preparação inválida"):
            await service.iniciar("curta", "frase")
        with pytest.raises(ValidacaoError, match="desconhecida"):
            await service.iniciar("A" * 24, "frase")
        with pytest.raises(ValidacaoError, match="referência de chat inválida"):
            await service.ler("x")
        with pytest.raises(ValidacaoError, match="desconhecida"):
            await service.aguardar("B" * 24, timeout_segundos=1)
        with pytest.raises(ValidacaoError, match="timeout_segundos"):
            await service.aguardar("B" * 24, timeout_segundos=0)
        with pytest.raises(ValidacaoError, match="desde_id"):
            await service.ler("B" * 24, desde_id=-1)
        assert manager.contexts == []
    finally:
        await manager.close()
