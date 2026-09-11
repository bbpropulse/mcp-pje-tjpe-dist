# Changelog

Formato inspirado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/);
versões seguem [SemVer](https://semver.org/lang/pt-BR/). Cada versão publicada aqui
corresponde a um snapshot no repositório público de distribuição.

## 0.7.2 — 2026-09-11

### Adicionado

- Limite de encaminhamentos por atendimento da CAP1G (5, ajustável por
  `PJE_TJPE_CAP1G_PROCESSOS_POR_CHAT`): o MCP conta os NPUs distintos citados pelo
  visitante — mensagem inicial, `enviar_mensagem_chat_cap1g` e o que for digitado na
  janela —, devolve `processos_solicitados`/`processos_restantes`, recusa a mensagem
  que passaria do limite e orienta a encerrar e abrir outro chat para o próximo lote.
  A transcrição lista os processos citados.

## 0.7.1 — 2026-09-11

### Corrigido

- `enviar_mensagem_chat_cap1g`: uma mensagem clicada mas sem eco no prazo agora conta
  como enviada — o erro pede para conferir a conversa e o texto idêntico passa a ser
  recusado, em vez de convidar a repetir o pedido ao tribunal.
- Erros transitórios do navegador durante uma espera (eco, encerramento) são
  tolerados até o prazo, em vez de deixar o encerramento pela metade.
- `iniciar_chat_cap1g`: falha na primeira mensagem depois de a conversa existir vira
  aviso, não fechamento da janela na frente do operador; a página que abre direto
  no chat (visitante já conhecido) é reconhecida em vez de esperar um formulário.
- Janela fechada à mão passa a ler `abandonado`; encerrar sem alcançar o controle
  do Mibew registra que o chat não confirmou; ao desligar o servidor o visitante
  se despede da conversa antes de a janela sumir.
- Texto com `<` e `>` em prosa deixa de ser tratado como marcação; espaço antes do
  JSON embutido na página não impede a leitura.

## 0.7.0 — 2026-09-11

### Adicionado

- Atendimento pelo chat da **CAP1G** (Central de Atendimento Processual do 1º Grau),
  um Mibew Messenger 2.x: `verificar_chat_cap1g`, `preparar_chat_cap1g`,
  `iniciar_chat_cap1g`, `ler_chat_cap1g`, `aguardar_resposta_chat_cap1g`,
  `enviar_mensagem_chat_cap1g` e `encerrar_chat_cap1g`. Iniciar exige frase literal
  em nova mensagem; a janela do Chrome fica visível; a transcrição é gravada com
  sidecar SHA-256; repetição de mensagem e caixa alta são recusadas conforme as
  boas práticas publicadas pela CAP1G.
- `docs/chat-cap1g.md` com a estrutura do cliente Mibew observada no TJPE.
- Variável `PJE_TJPE_CHAT_HEADLESS` (padrão `false`).
- Distribuição pública em `bbpropulse/mcp-pje-tjpe-dist`, com `LICENSE` (MIT) e
  `scripts/publicar_distribuicao.py` para gerar cada snapshot.

### Alterado

- `docs/telas-do-painel.md` deixa de trazer contagens do acervo de um escritório.

## 0.6.0

- Catálogo de tribunais com TRT6 e TRF5/JFPE em modo de descoberta.
- Aprendizagem estrutural local (JSONL sanitizado) e adaptadores YAML com replay
  offline: `status_navegacao_adaptativa`, `listar_falhas_navegacao`,
  `validar_adaptadores_offline`.
- Metadados públicos por NPU na API DataJud do CNJ.

Versões anteriores: SICAJUD, consulta pública, login assistido (CPF/senha/MFA e
certificado via PJeOffice), Acervo por jurisdição, Autos Digitais, Pesquisa Geral
com abertura confirmada e íntegra assíncrona pelo PJeDocs — descritas no README.
