# Changelog

Formato inspirado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/);
versões seguem [SemVer](https://semver.org/lang/pt-BR/). Cada versão publicada aqui
corresponde a um snapshot no repositório público de distribuição.

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
