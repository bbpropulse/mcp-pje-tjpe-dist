# Chat da CAP1G — estrutura observada

Colhido em 2026-09-10 (fora do horário) a partir do cliente público servido em
`https://www.tjpe.jus.br/mibew/`. Serve para escrever fixture fiel e para não
redescobrir seletor a cada sessão. Nada aqui autoriza abrir conversa sem a frase
literal do usuário.

## Onde está

- Portal: `https://portal.tjpe.jus.br/web/central-de-atendimento-processual-do-1%C2%BA-grau`
  — só aponta para o chat e publica as boas práticas (PDF) e o horário.
- Chat: `https://www.tjpe.jus.br/mibew/index.php/chat?locale=pt-br&style=default&group=1`
  (grupo `1` = "CAP", empresa "CAP TJPE").
- Horário: 8h às 19h em dias úteis. Telefone: (81) 3181-0506.

## Plataforma

**Mibew Messenger 2.x** (Apache-2.0; `Copyright 2005-2023`): cliente Backbone +
Marionette + Handlebars, comunicação pelo protocolo MibewAPI 1.0 em
`POST /mibew/index.php/thread/update` com `data=<JSON codificado>`, polling a cada
`requestsFrequency: 2` segundos. Há um plugin customizado
(`plugins/Custom/Mibew/Plugin/Dashboard`) que só injeta um item "Dashboard" no
painel do **operador**; não muda nada do lado do visitante.

A página embute a configuração inteira em um único `script`:

```js
jQuery(document).ready(function() {Mibew.Application.start({
  "server": {"url": "/mibew/index.php/thread/update", "requestsFrequency": "2"},
  "page": {"style": "default", "company": {"name": "CAP TJPE"}, "mibewHost": "https://portal.tjpe.jus.br/", ...},
  "startFrom": "leaveMessage",
  "leaveMessageOptions": {"leaveMessageForm": {"name": "Visitante", "groupId": 1, "groupName": "CAP", "showCaptcha": false, ...}}
});});
```

`startFrom` é o sinal de disponibilidade:

| `startFrom` | Significado | O que a página mostra |
|---|---|---|
| `leaveMessage` | nenhum operador do grupo em linha | `<body class="leaveMessage">`; a CAP1G trocou o formulário de recado pelo aviso "Desculpe, não há agentes disponíveis no momento…" — `form[name=leaveMessageForm]` existe, mas vazio |
| `survey` | há operador | formulário de identificação (abaixo) |
| `chat` | conversa já existente para este visitante | tela do chat direto |

`verificar_chat_cap1g` lê só esse JSON (com `json.JSONDecoder().raw_decode` a partir
de `Mibew.Application.start(`), sem carregar scripts, e nada é criado no servidor.

## Survey (`survey/form`)

Renderizado pelo cliente dentro de `#main-region > #content-wrapper`:

```html
<form name="surveyForm" method="post" action="">
  <input type="hidden" name="style"/> <input type="hidden" name="info"/>
  <input type="hidden" name="referrer"/> <input type="hidden" name="survey" value="on"/>
  <input type="hidden" name="group" value="1"/>
  <div class="errors"></div>
  <table class="form">
    <tr><td><strong>Nome:</strong></td><td><input type="text" name="name" value="Visitante" class="username"/></td></tr>
    <tr><td><strong>Email:</strong></td><td><input type="text" name="email" class="username"/></td></tr>
    <tr><td><strong>Pergunta Inicial:</strong></td><td><textarea id="message-survey" name="message"></textarea></td></tr>
  </table>
  <a href="javascript:void(0);" class="form-button" id="submit-survey">Iniciar Chat</a>
</form>
```

Aviso acima do formulário: "Bem-vindo(a) ao atendimento por chat do TJPE. Por favor,
preencha o formulário abaixo e clique no botão Iniciar Chat."

O clique em `#submit-survey` **não submete o form**: `Views.SurveyForm.submitForm` lê
os campos que a configuração habilita (`canChangeName`, `showEmail`, `showMessage`) e
chama `processSurvey` pelo MibewAPI com `{groupId, name, info, email, message,
referrer, threadId: null, token: null}`. A resposta traz `next` (`chat` ou
`leaveMessage`) e `options`; com `chat`, `Application.Chat.start(options)` troca a
tela. Validação local: nome vazio → "Name is required."; e-mail inválido → "Wrong
email address." — o texto cai em `.errors`. Como os campos podem ser desligados na
configuração, o MCP preenche só os que existem e manda a mensagem inicial como
primeira fala do chat se `#message-survey` não estiver lá.

## Tela do chat (`chat/layout`)

```html
<div id="chat-header"><div id="controls-region"></div></div>   <!-- controles: nome, som, atualizar, fechar -->
<div id="chat">
  <div id="avatar-region"></div>
  <div id="messages-region"></div>                              <!-- .message por mensagem -->
  <div id="status-region"></div>                                <!-- "Usuário remoto está digitando..." -->
</div>
<div id="message-form-region">
  <textarea id="message-input" class="message"></textarea>      <!-- só existe se user.canPost -->
  <a id="send-message" title="Enviar Mensagem">Enviar (Ctrl-Enter)</a>
</div>
```

Mensagem (`chat/message`):

```html
<span>13:01:00</span> <span class='name-agent'>Maria (CAP)</span>: <span class='message-agent'>Olá</span><br/>
<span>13:00:01</span> <span class='message-info'>Aguarde, um operador irá atendê-lo.</span><br/>
```

Classes `name-`/`message-` recebem `user`, `agent`, `info`, `connection`, `event`,
`plugin` ou `hidden`. O envio pela UI desabilita `#message-input` até a mensagem
ecoar (`multiple:add` na coleção); por isso o MCP espera `#message-input:not([disabled])`
antes de preencher.

## Objetos do cliente (o que o MCP lê)

Tudo pendurado em `window.Mibew.Objects`, só depois que o chat começa:

| Objeto | Atributos usados |
|---|---|
| `Models.thread` | `id`, `lastId`, `agentId`, `state` — **`token` existe e nunca sai da página** |
| `Models.user` | `name`, `canPost`, `isAgent`, `typing` |
| `Collections.messages` | `toJSON()` → `[{id, kind, name, message, created}]`; `created` é epoch em segundos |
| `Models.Status.typing` / `.message` | `visible`, `message` |
| `Models.Controls.close` | `closeThread()` → função `close` no servidor → `Mibew.Utils.closeChatPopup()` |

`Thread.STATE_*`: 0 fila, 1 aguardando, 2 em atendimento, 3 encerrado, 4 carregando,
5 visitante saiu, 6 convidado. `Message.KIND_*`: 1 visitante, 2 operador, 3 só para
operador, 4 info, 5 conexão, 6 evento, 7 plugin.

O cliente chama `update` (estado, `canPost`, `typing`) e `updateMessages` (novas
mensagens a partir de `lastId`) a cada 2 s enquanto a página existir; o MCP não
precisa — e não deve — fazer polling próprio ao servidor do TJPE.

## Regras operacionais não escritas

- **Até 5 processos por atendimento.** Informado em 2026-09-11 por quem usa o chat; não
  consta do PDF nem da página. O operador recusa o sexto encaminhamento na mesma
  conversa, mas nada impede encerrar e abrir outro chat para o lote seguinte. O MCP
  conta NPUs distintos citados pelo visitante (`extrair_processos`) e bloqueia antes
  de o operador precisar recusar; `PJE_TJPE_CAP1G_PROCESSOS_POR_CHAT` ajusta o número.

## Boas práticas publicadas (PDF do portal)

Respeito; identificar-se com nome e e-mail; ser claro e objetivo; aguardar com
paciência ("repetir mensagens em sequência pode atrasar o atendimento"); sem
formalidade excessiva; sem linguagem ofensiva nem caixa alta contínua — o
descumprimento pode encerrar o atendimento. O MCP transforma isso em validação:
nome e e-mail obrigatórios, recusa de mensagem repetida e de texto quase todo em
maiúsculas, e aviso quando se insiste sem resposta.
