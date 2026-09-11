# Telas do Painel do Advogado — estrutura observada

Colhido do TJPE em 2026-09-08 com `inspecionar_aba_painel`, em sessão autenticada por
certificado. Serve para escrever fixture fiel e para não redescobrir seletor a cada
sessão — cada releitura destas telas custa um PIN.

Nada aqui autoriza operar as telas. Abas de ação (Peticionar, Expedientes, Habilitação
nos autos, Novo processo, Minhas petições) seguem fora da allowlist de inspeção.

## Abas por grau

| 1º grau | 2º grau |
|---|---|
| Expedientes, Novo processo, Consulta processos, Peticionar, Habilitação nos autos, Push, Acervo, Minhas petições, Principal, Períodos de inativação, Filtros da caixa | Expedientes, Novo processo, Consulta processos, Peticionar, Habilitação nos autos, Push, Acervo, **Intimações de pauta**, Minhas petições |

`Intimações de pauta` só existe no 2º grau. `Períodos de inativação` e `Filtros da caixa`
aparecem no 1º grau como sub-abas da área de caixas do Acervo.

## Acervo — renderizado no próprio painel

É a exceção: não usa iframe. Particionado por jurisdição, com semântica diferente por grau.

- 1º grau: comarcas — `Recife - Varas`, `Jaboatão dos Guararapes - Varas`…
- 2º grau: órgãos — `Recife - TJPE`, `Recife - Turmas Recursais`, `Caruaru - Câmara Regional`

Cada rótulo vem seguido da contagem de processos entre parênteses; os números variam
por escritório e por dia, e o painel pagina a lista em blocos de cerca de 40.

Árvore de jurisdições: `a[id^="formAbaAcervo:trAc:"]` — o número é id de nó, não posição.

### Busca (form `formAcervo`)

Os critérios ficam num dropdown do Bootstrap, `div#formAcervo:divCampos.dropdown-menu`,
dentro de `li.dropdown` na barra de ícones. Estão sempre no DOM, **sem visibilidade** até
o alternador ser acionado — `fill()` espera por visibilidade e estoura.

| Critério | Campo |
|---|---|
| parte | `formAcervo:itDestPend` |
| documento | `formAcervo:itIMF` |
| OAB | `formAcervo:itOAB` |
| classe | `formAcervo:itCL` |
| assunto | `formAcervo:itAS` |

- Alternador: `a.dropdown-toggle[title="Pesquisar nesta caixa"]`
- Disparo: `input[id="formAcervo:btPesqAc"]`
- **Cuidado**: a mesma barra tem `a#btnPesquisarContexto` com `title="Pesquisar"`, que é
  busca por número de processo. Fixar pelo título evita trocar um pelo outro.

A busca roda no servidor e alcança partes que o resumo da linha não exibe — um processo
aparece pelo nome de uma parte que a linha esconde atrás de "e outros (1)".

### Paginação

Datascroller `formAcervo:tbProcessos:scPendentes`; botões disparam
`Event.fire(this, 'rich:datascroller:onscroll', {'page': 'next'})` e recebem
`rich-datascr-button-dsbld` na última página.

**A aba tem mais de um `div.rich-datascr`**: `formLogHistMov:tbHistoricoMov:logTable`
(histórico de movimentação) vem antes no DOM e nunca tem próxima habilitada. Selecionar
por posição pagina o scroller errado, calado.

## Abas-casca: Push e Consulta processos

Não têm formulário próprio. O conteúdo vive numa página `.seam` dentro de iframe, e o
`evaluate` no quadro pode falhar enquanto o a4j ainda navega — daí a repetição até prazo.

### Push — `/{grau}/Push/listView.seam`

Cadastro de assinatura, não superfície de leitura: registra processo + e-mail e o PJe passa
a notificar movimentações.

| Campo | Papel |
|---|---|
| `j_id172:inputNumeroProcesso-...:inputNumeroProcesso` | processo a monitorar |
| `j_id172:emailInputDecoration:emailInput` | e-mail dos avisos |
| `j_id172:inputObservacaoDecoration:inputObservacao` | observação |
| `j_id172:btnIncluirAcompanhamento` | botão Incluir |

Incluir um Push cria regra permanente que dispara e-mail — classe de ação que este
servidor não pratica.

### Consulta processos — `/{grau}/Processo/ConsultaProcesso/listView.seam`

Form `fPP`. Alcança processos de terceiros, o que traz a Resolução CNJ 121 para dentro.

| Grupo | Campos |
|---|---|
| Partes | `fPP:j_id169:nomeParte`, `fPP:j_id178:outrosNomesAlcunha`, `fPP:dpDec:documentoParte` (radio `tipoMascaraDocumento`) |
| Advogado | `fPP:j_id187:nomeAdvogado`, `fPP:decorationDados:numeroOAB` + `letraOAB` + `ufOABCombo` |
| Processo | `fPP:numeroProcesso:{numeroSequencial,numeroDigitoVerificador,Ano,ramoJustica,respectivoTribunal,NumeroOrgaoJustica}` |
| Classificação | `fPP:j_id255:assunto`, `fPP:j_id264:classeJudicial`, `fPP:j_id278:numeroDocumento` |
| Órgão | `fPP:jurisdicaoComboDecoration:jurisdicaoCombo`, `fPP:orgaoJulgadorComboDecoration:orgaoJulgadorCombo`, `fPP:orgaoJulgadorColegiadoComboDecoration:orgaoJulgadorColegiadoCombo` |
| Faixas | `fPP:dataAutuacaoDecoration:dataAutuacao{Inicio,Fim}InputDate`, `fPP:valorDaCausaDecoration:valorCausa{Inicial,Final}` |
| Movimento | `fPP:j_id419:movimentacaoProcessualSuggest` |
| Criminais | painel recolhido `fPP:j_id436:j_id441`: `orgaoOrigemCriminal`, `numeroProcedCriminal` + `anoProcedCriminal`, `numeroProtocoloPolicia` |

- Disparo: `fPP:searchProcessos` · Limpar: `fPP:clearButtonProcessos`
- Resultado: `fPP:processosTable`, datascroller `fPP:processosTable:scTabela`
- O painel criminal recolhe por `SimpleTogglePanelManager.toggleOnClient` — **mecanismo
  distinto** do dropdown Bootstrap do Acervo. Não são intercambiáveis.
