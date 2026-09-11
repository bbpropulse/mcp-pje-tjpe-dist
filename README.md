# MCP PJe Pernambuco — versão 0.7

Servidor MCP `stdio`, local e independente, para usar serviços judiciais de
Pernambuco no Codex ou no Claude Code. O **TJPE permanece operacional** com
SICAJUD, consulta pública, login assistido, Acervo, Autos, Pesquisa Geral,
PJeDocs e, a partir da versão 0.7, o **chat da CAP1G** (Central de Atendimento
Processual do 1º Grau). A versão 0.6 iniciou adaptadores separados para **TRT6**
e **TRF5/JFPE** em modo de descoberta, além de uma camada de aprendizagem
estrutural local sem banco de dados.

Este repositório é a **distribuição pública**: recebe um snapshot a cada versão
(veja o [CHANGELOG](CHANGELOG.md)); o histórico de desenvolvimento fica em
repositório privado. Licença [MIT](LICENSE). Sugestões e problemas: abra uma
issue aqui.

TRT6 e TRF5 ainda não executam login, pesquisa ou leitura autenticada nesta
versão. O catálogo informa seus ambientes oficiais e suas políticas sem atribuir
a eles as permissões já auditadas para o TJPE.

O projeto não depende do Processa AI nem do Railway. O Chromium executa na
máquina do usuário, credenciais opcionais ficam no cofre do sistema operacional
e o protocolo MCP nunca recebe CPF, senha ou semente MFA como argumento.

## Ferramentas desta versão

| Ferramenta | O que faz | Altera o tribunal? |
| --- | --- | --- |
| `status_servidor` | Mostra versão, segurança e recursos | Não |
| `listar_tribunais_suportados` | Lista instâncias, maturidade, capacidades e avisos de TJPE, TRT6 e TRF5 | Não |
| `status_navegacao_adaptativa` | Mostra modo, retenção e adaptadores locais | Não; nem abre navegador |
| `listar_falhas_navegacao` | Lê observações estruturais sanitizadas do JSONL | Não; somente arquivo local |
| `validar_adaptadores_offline` | Reavalia as observações contra os YAML empacotados | Não; sem rede ou clique |
| `listar_ambientes` | Lista PJe 1G/2G e consulta pública | Não |
| `diagnosticar_ambiente` | Testa Chromium e endpoints oficiais | Não |
| `diagnosticar_pjeoffice` | Verifica passivamente a porta local e o botão de certificado no SSO 1G/2G | Não; não clica nem envia dados ao aplicativo |
| `consultar_processo_publico` | Consulta um NPU público em 1G ou 2G | Não |
| `pesquisar_classes_custas` | Pesquisa classes CNJ no SICAJUD | Não |
| `simular_custas` | Calcula uma estimativa pública de custas | Não gera guia |
| `testar_login` | Testa CPF/senha/MFA guardados localmente | Somente autenticação |
| `abrir_login_certificado` | Abre o SSO visível e inicia o fluxo no PJeOffice | Somente autenticação assistida |
| `verificar_login_certificado` | Confirma a sessão com uma requisição protegida sem clicar novamente | Não |
| `consultar_metadados_processo` | Metadados públicos de um NPU na API DataJud do CNJ (TJPE, TRT6, TRF5), sem navegador nem sessão | Não |
| `listar_jurisdicoes_acervo` | Lista as jurisdições do Acervo deste grau, sem selecionar nenhuma | Não |
| `listar_acervo` | Lista ou **pesquisa** o Acervo de uma jurisdição — busca no próprio PJe por parte, documento, OAB, classe ou assunto —, percorre até 20 páginas, filtra localmente por termos e registra os NPUs elegíveis nesta sessão/grau | Não |
| `preparar_acesso_pesquisa_geral` | Pesquisa somente um NPU exato e prepara sua abertura sem clicar no resultado | Não abre o processo |
| `abrir_autos_pesquisa_geral` | Abre uma vez o resultado preparado após confirmação literal | Sim; o PJe pode registrar o acesso nos termos da Resolução CNJ 121 |
| `consultar_autos` | Lê cabeçalho, movimentos e documentos do Acervo ou o cache de uma abertura confirmada | Não faz novo acesso para resultado da Pesquisa Geral |
| `ler_documento_autos` | Extrai texto de documento retornado por processo do Acervo e calcula SHA-256 | Não |
| `baixar_documento_autos` | Grava documento de processo do Acervo e seu sidecar `.sha256` no diretório local configurado | Não no tribunal; grava arquivos locais |
| `preparar_download_pjedocs` | Valida processo, sessão, grau e interface e prepara uma referência efêmera para a íntegra | Não; não clica em `DOWNLOAD` |
| `solicitar_download_pjedocs` | Solicita uma vez a geração da íntegra após confirmação literal | Sim; cria um trabalho assíncrono no PJeDocs |
| `listar_downloads_pjedocs` | Consulta a Área de download e devolve estado e referência opaca do resultado | Não |
| `baixar_resultado_pjedocs` | Baixa o resultado pronto com teto independente, SHA-256 e sidecar local | Não no tribunal; grava arquivos locais |
| `verificar_chat_cap1g` | Lê a página do chat Mibew da CAP1G e diz se há operador, sem abrir conversa | Não |
| `preparar_chat_cap1g` | Valida nome, e-mail e mensagem inicial pelas boas práticas da CAP1G e devolve a frase de confirmação | Não; nada é enviado |
| `iniciar_chat_cap1g` | Abre a conversa em janela visível do Chrome após a frase literal | Sim; cria um atendimento real com um servidor da CAP1G |
| `ler_chat_cap1g` | Devolve estado, operador e mensagens da conversa em andamento | Não |
| `aguardar_resposta_chat_cap1g` | Bloqueia até chegar mensagem nova ou mudar o estado, com teto de tempo | Não |
| `enviar_mensagem_chat_cap1g` | Envia uma mensagem e confirma o eco no chat; recusa repetição e caixa alta | Sim; fala em nome do usuário |
| `encerrar_chat_cap1g` | Encerra a conversa, fecha a janela e grava a transcrição com sidecar SHA-256 | Sim; fecha o atendimento |

### Estágio por tribunal

| Adaptador | Instâncias iniciais | Estado da versão 0.6 |
| --- | --- | --- |
| TJPE | 1º e 2º graus | Operacional; comportamento anterior preservado |
| TRT6 | 1º grau, 2º grau e consulta pública unificada | Descoberta; URLs e política de acesso catalogadas, sem navegação autenticada |
| TRF5/JFPE | SJPE 1º grau, TRF5 2º grau/TRU e Turmas Recursais | Descoberta; instâncias separadas, sem navegação autenticada |

No TRT6, o Tribunal informa bloqueio automático quando um usuário ultrapassa
**1.500 acessos a processos de terceiros em 30 dias**; reincidência pode levar a
bloqueio definitivo. O perfil da v0.6 registra esse teto e reserva um limite
local conservador de 1.000 para a futura implementação, mas ainda não abre esses
processos. A consulta pública não entra na contagem segundo o
[comunicado oficial do TRT6](https://www.trt6.jus.br/portal/noticias/2026/03/20/trt-6-bloqueara-usuariosas-do-pje-jt-que-realizem-consultas-excessivas-processos).

No TRF5, `pje1g.trf5.jus.br` atende toda a 5ª Região. Um futuro acesso da JFPE
deverá comprovar a jurisdição Pernambuco dentro da sessão; o hostname sozinho
não basta. O ambiente `pje2g` representa as Turmas Recursais, enquanto o 2º
grau do Tribunal usa `pjett`; os identificadores internos do SSO não são usados
como grau jurídico.

### Aprendizagem e correção sem banco de dados

A aprendizagem desta versão é um mecanismo determinístico de feedback, não um
modelo que se retreina sozinho. Quando a Pesquisa Geral do TJPE deixa de
encontrar o botão ou o campo de NPU no formato esperado, o MCP pode registrar um
snapshot estrutural sanitizado em:

```text
<PJE_TJPE_DATA_DIR>/adaptive/events-v1.jsonl
```

Em sistemas POSIX, o diretório recebe permissão `0700`; eventos, lock e chave
HMAC recebem `0600`. No Windows, esses bits POSIX não são simulados: o diretório
de dados herda a ACL do perfil do usuário e deve permanecer inacessível a outras
contas. O arquivo é limitado e rotacionado, usa lock entre processos e pode ser
inspecionado pelo Codex ou Claude para propor uma alteração revisada no YAML.
Nenhuma estratégia observada é promovida automaticamente. Processos MCP que
compartilham o mesmo diretório também devem usar a mesma retenção; o primeiro
writer persiste essa política e configurações divergentes falham sem truncar o
histórico.

Os modos são:

- `observe` (padrão): mantém a descoberta auditada e registra falhas sanitizadas;
- `shadow`: também avalia, apenas sobre a falha capturada, quais estratégias YAML
  teriam encontrado um candidato, sem alterar a navegação;
- `active`: aceita somente sinônimos semânticos presentes em YAML empacotado e
  aprovado no Git. Correspondência única, submit nativo, formulário POST,
  allowlists de rede, NPU exato, avisos e a fronteira de efeito continuam
  obrigatórios em Python.

A v0.6 aprova para o TJPE apenas alternativas estreitas, como `Consultar` para
o submit e `Numeração única` para o campo. Os YAML de TRT6 e TRF5 estão
deliberadamente com `approved_for_active: false`.

O JSONL nunca guarda NPU, CPF/CNPJ, nomes, HTML, screenshots, texto livre,
cookies, `ViewState`, query strings, tokens SSO, PIN ou OTP. Identificadores de
controles viram HMAC local; palavras são reduzidas a uma lista semântica fechada;
segmentos desconhecidos do caminho são descartados. A captura é recusada depois
de qualquer possível clique que possa abrir Autos.

Para revisar uma falha: consulte `listar_falhas_navegacao`, altere o YAML em um
commit, rode `validar_adaptadores_offline` e execute a suíte. A ativação é uma
decisão de código revisável, não uma mutação autônoma feita pelo MCP.

A consulta processual valida o dígito do NPU, preserva a sessão JSF necessária
para abrir o detalhe e mascara CPF/CNPJ antes de devolver dados ao cliente MCP.
Se o portal exigir CAPTCHA, a ferramenta solicita interação humana e nunca tenta
contorná-lo.

O simulador usa diretamente a tela pública de simulação do SICAJUD. Ele verifica
a classe efetivamente selecionada, o valor interpretado pelo formulário, cada
item de preparo e a soma total. O resultado é uma estimativa oficial, não uma
guia, cobrança ou prova de valor definitivo.

### Metadados públicos pelo DataJud/CNJ

`consultar_metadados_processo` consulta a
[API pública DataJud](https://datajud-wiki.cnj.jus.br/api-publica/acesso) do CNJ
por HTTPS direto: **sem navegador, sem sessão, sem certificado e sem MFA**. Vale
para os três tribunais do catálogo — TJPE, TRT6 e TRF5 — e nenhum acesso é
registrado no PJe por essa via.

O que ela entrega: classe, assuntos, órgão julgador, grau, sistema, formato,
data de ajuizamento, última atualização e a linha de movimentações, da mais
recente para a mais antiga. O que ela **não** entrega: partes, advogados,
CPF/CNPJ, documentos, intimações e prazos — o DataJud simplesmente não publica
esses campos, e por isso não há o que mascarar na resposta. É complementar à
leitura autenticada, nunca substituta: para os Autos e o Acervo continua valendo
o fluxo com sessão do advogado.

Barreiras da ferramenta: o NPU é validado contra o segmento de Justiça/tribunal
do catálogo e o dígito verificador antes de qualquer requisição; a consulta ao
Elasticsearch é montada aqui a partir do NPU, e nunca aceita uma query vinda do
chamador; o host e a rota do índice são fixos; nenhum redirecionamento é
seguido; e a resposta é recusada se o número devolvido divergir do consultado ou
se o `nivelSigilo` não for o nível público.

A chave do CNJ é pública e fixa, mas o próprio CNJ avisa que pode rotacioná-la.
Ela vem embutida como padrão e é sobrescrevível por `PJE_TJPE_DATAJUD_API_KEY`;
se o CNJ trocá-la, a ferramenta devolve um erro dizendo exatamente isso e onde
obter a vigente.

### Leitura autenticada segura

A leitura autenticada usa sempre a mesma sessão que concluiu o login. Para um
processo do Acervo, o fluxo é:

1. Concluir e verificar o login no grau desejado.
2. Executar `listar_jurisdicoes_acervo` nesse grau. O Acervo do TJPE é
   particionado por jurisdição e a lista de processos só popula depois que uma
   delas é escolhida. (`listar_acervo` sem `jurisdicao` também informa as
   disponíveis, mas como erro de validação; a ferramenta de descoberta existe
   para não exigir uma chamada com falha.)
3. Repetir `listar_acervo` no mesmo grau informando `jurisdicao` (o rótulo é
   comparado sem acento e sem diferenciar maiúsculas; um prefixo ambíguo é
   recusado pedindo o rótulo completo). A ferramenta abre somente a aba
   **Acervo** e registra em memória os processos efetivamente apresentados pelo
   PJe ao usuário. `autos_disponiveis=true` indica que o link GET dos Autos foi
   reconhecido e validado sem ser acionado.
4. Executar `consultar_autos` para um desses NPUs, no mesmo grau e na mesma
   sessão.
5. Usar a `referencia_documento` retornada pelos Autos em
   `ler_documento_autos` ou `baixar_documento_autos`. A referência também fica
   vinculada à mesma sessão, grau e processo.

Se a sessão for renovada ou encerrada, o Acervo deve ser listado novamente. A
automação falha de forma fechada se detectar uma interface desconhecida, uma
referência expirada ou qualquer sinal de expediente pendente de ciência. Ela
não abre as áreas **Expedientes**, **Intimações** ou **Agrupadores**, não aceita
diálogo de ciência e não clica em controles para mover processos, criar caixas,
favoritar ou incluir lembretes.

Ao consultar Autos provenientes do Acervo, o MCP não clica novamente no NPU. Ele
faz um único GET HTTPS da rota clássica previamente auditada, não segue
redirecionamentos e processa o HTML em um contexto descartável com JavaScript e
Service Workers desativados, sem cookies e com toda a rede bloqueada. Se um item
do Acervo não oferecer essa rota reconhecida — inclusive quando usar somente a
navegação Angular — ele continua visível com `autos_disponiveis=false`, mas não
pode ser aberto por esta versão. Para origem `pesquisa_geral`, `consultar_autos`
não repete esse GET: entrega apenas o snapshot guardado pela abertura confirmada.

### Pesquisa Geral autenticada por NPU exato

A Pesquisa Geral é uma alternativa controlada somente para processo que não
tenha aparecido no Acervo da sessão e do grau atuais. Ela não aceita nome,
CPF/CNPJ, fragmento do número, curingas, listas ou paginação automática. O fluxo
é deliberadamente dividido:

O [Manual do Advogado](https://docs.pje.jus.br/manuais-de-uso/Manual%20do%20advogado/#pesquisar-processos)
documenta a pesquisa e a abertura dos Autos. As regras oficiais
[RN452/RN469](https://docs.pje.jus.br/configura%C3%A7%C3%B5es-do-pje/Regras%20negociais/#rn452)
tratam o acesso de terceiro e seu registro; a
[RN397](https://docs.pje.jus.br/configura%C3%A7%C3%B5es-do-pje/Regras%20negociais/#rn397)
trata separadamente a ciência em intimação pendente. Por isso, esta versão
autoriza apenas a primeira operação confirmada e continua bloqueando a segunda.

1. `preparar_acesso_pesquisa_geral` pesquisa o NPU completo, exige um único
   resultado inequívoco e devolve uma referência efêmera com a frase literal de
   confirmação. Não abre o link dos Autos.
2. O cliente deve aguardar uma **nova mensagem do usuário** contendo exatamente
   a frase devolvida. Não pode copiar a frase automaticamente nem interpretar
   uma aprovação genérica.
3. `abrir_autos_pesquisa_geral` revalida sessão, grau, NPU e resultado antes de
   abrir uma única vez. A ferramenta é marcada como mutável/destrutiva porque,
   conforme a [Resolução CNJ 121](https://atos.cnj.jus.br/atos/detalhar/atos-normativos?documento=92),
   o PJe pode registrar o acesso de advogado não vinculado. Se o desfecho ficar
   incerto depois da possível abertura, o estado é `indeterminado` e o MCP não
   tenta novamente.
4. `consultar_autos` devolve somente o conteúdo guardado em memória durante essa
   abertura, sem emitir outra requisição de acesso. O campo `origem` vale
   `pesquisa_geral`, e documentos sem bytes capturados aparecem com
   `conteudo_disponivel=false`.

O cache e as referências valem apenas durante a vida do processo MCP e da mesma
geração de sessão. Reiniciar o servidor ou refazer o login exige nova preparação
e nova confirmação. `ler_documento_autos`, `baixar_documento_autos` e todo o
fluxo PJeDocs permanecem restritos a processos provenientes do Acervo: a
confirmação da Pesquisa Geral não autoriza novo acesso, download de documento ou
geração de íntegra.

O MCP não aceita automaticamente aviso de sigilo, permissão, ciência ou
responsabilização diferente do contrato reconhecido para a abertura preparada.
Processo sigiloso de terceiro, resultado ambíguo, interface alterada ou qualquer
tentativa de abrir Expedientes faz a operação falhar de forma fechada.

`ler_documento_autos` devolve o texto extraído, tamanho e SHA-256 dos bytes
recebidos. `baixar_documento_autos` publica o arquivo com permissão local
restrita e cria, ao lado, um arquivo `<nome>.sha256` no formato aceito por
utilitários de verificação. O hash identifica exatamente os bytes baixados, mas
não substitui a assinatura digital nem constitui, isoladamente, prova de
autenticidade jurídica.

Para PDFs, a extração textual acontece em um subprocesso descartável. O MCP
limita memória residente, CPU, tempo de relógio, número de páginas e quantidade
de caracteres; se qualquer teto for alcançado, encerra o parser sem derrubar o
servidor e orienta o uso do arquivo baixado. Apenas um parser pode executar por
vez; chamadas concorrentes recebem backpressure curto, e o limite continua
ocupado até o subprocesso terminar mesmo se o cliente cancelar a solicitação.

O download direto tem teto local padrão de **3 MiB (3.145.728 bytes)**,
controlado por `PJE_TJPE_MAX_DOCUMENT_BYTES`. O fluxo assíncrono PJeDocs, a
íntegra do processo e arquivos maiores usam outro caminho e outro teto. O
transporte direto não segue redirecionamentos e lê os bytes de forma incremental,
interrompendo a conexão assim que o teto for ultrapassado.

### Íntegra assíncrona pelo PJeDocs

O PJeDocs opera somente sobre processo que já apareceu no Acervo da sessão
autenticada atual e do mesmo grau. Nesta versão, ele solicita exclusivamente a
**íntegra**: o formulário fica sem filtros de tipo, ID ou período, e as opções
**Incluir expediente** e **Incluir movimentos** são sempre verificadas como
**Não**. O fluxo é deliberadamente dividido em quatro chamadas:

1. `preparar_download_pjedocs` revalida NPU, grau, sessão, Autos e a interface
   conhecida do PJeDocs, registra uma linha de base da Área de download e devolve
   uma referência efêmera e a frase literal exigida para confirmação. Esta etapa
   não aciona a geração.
2. Depois da preparação, o cliente deve aguardar uma **nova mensagem do usuário**
   contendo a frase literal antes de invocar `solicitar_download_pjedocs`. A
   ferramenta é marcada no protocolo como mutável/destrutiva para que o host
   possa pedir aprovação. Ela revalida todos os vínculos e clica uma única vez em
   `DOWNLOAD`. Essa é uma mutação técnica: cria um trabalho assíncrono de geração,
   sem protocolar, peticionar ou alterar os Autos. Se o resultado do clique ficar
   incerto, o MCP marca a solicitação como indeterminada e não a repete
   automaticamente. O servidor `stdio` valida a transição em duas etapas, mas não
   consegue provar sozinho a autoria humana da frase; essa garantia depende da
   interface e da política de aprovação do cliente MCP.
   A proteção contra clique duplicado vale durante a vida do processo MCP; depois
   de reiniciar o servidor, reconcilie a Área de download antes de preparar outra
   solicitação.
3. `listar_downloads_pjedocs` consulta a Área de download e tenta reconciliar
   somente uma nova linha inequívoca com a solicitação. Enquanto o arquivo não
   estiver pronto, devolve o estado de processamento; não mantém polling contínuo.
   Se a linha for inequivocamente reconhecida como expirada, uma chamada posterior
   a `preparar_download_pjedocs` cria uma nova referência e exige outra confirmação.
   Antes dessa comprovação, preparar e solicitar continuam idempotentes e nunca
   repetem automaticamente o clique anterior.
4. `baixar_resultado_pjedocs` resolve o link novamente no momento do uso e baixa
   o arquivo pronto de forma incremental. A URL temporária nunca é armazenada nem
   exposta pelo protocolo MCP.

Segundo o manual oficial do TJPE, o arquivo gerado permanece disponível por
**24 horas**, e o link mostrado na Área de download é renovado a cada
**2 minutos**. Por isso, o MCP usa uma referência opaca e sempre obtém um link
novo; ele não oferece uma URL para copiar, reutilizar ou compartilhar.
O nome remoto da linha também não é devolvido: o modelo usa somente o nome
sintético `Íntegra do processo <NPU>` para impedir que texto inesperado da
interface transporte uma URL ou capability temporária.

O download do resultado tem teto local independente, com padrão de
**512 MiB (536.870.912 bytes)**, configurado por
`PJE_TJPE_MAX_PJEDOCS_BYTES`. O arquivo recebe nome derivado do NPU e do hash,
permissão `0600` e publicação atômica em diretórios `0700`; ao lado, o MCP cria
um sidecar `.sha256` também restrito. O conteúdo não é extraído nem executado.
O hash identifica os bytes recebidos, mas não substitui a validação oficial por
QR Code ou número único descrita no manual.

Os valores **3 MB** e **3 MiB** não são equivalentes nem configuram o mesmo
controle. O manual usa **3 MB** para distinguir o download individual do fluxo
PJeDocs. Já **3 MiB (3.145.728 bytes)** é o teto local desta aplicação para
`ler_documento_autos` e `baixar_documento_autos`; ele não altera o PJe nem o
limite do PJeDocs. O teto de 512 MiB protege separadamente o arquivo final da
íntegra.

A automação falha de forma fechada diante de sessão, grau ou processo divergente,
referência vencida, aviso de ciência ou acesso, formulário desconhecido, controles
ambíguos, resultado que não possa ser reconciliado com segurança, link fora do
host/grau esperado, redirecionamento, resposta HTML ou arquivo acima do teto.
Ela não tenta contornar essas condições.

### Atendimento pelo chat da CAP1G

A [Central de Atendimento Processual do 1º Grau](https://portal.tjpe.jus.br/web/central-de-atendimento-processual-do-1%C2%BA-grau)
atende advogados e partes por chat, das **8h às 19h em dias úteis**, ou pelo
telefone (81) 3181-0506. O chat é um **Mibew Messenger 2.x** hospedado em
`www.tjpe.jus.br/mibew`; a estrutura observada está em
[`docs/chat-cap1g.md`](docs/chat-cap1g.md). Quem responde é um servidor do
tribunal, então o MCP trata a conversa como ação externa em nome do usuário:

1. `verificar_chat_cap1g` baixa somente o documento HTML do chat (sem scripts,
   estilos ou imagens) e lê o `startFrom` que o Mibew embute: `survey` significa
   operador disponível; `leaveMessage` significa ninguém em linha — e a CAP1G
   desativou o recado fora do horário, então não há nada a deixar. Um GET da
   página não cria conversa.
2. `preparar_chat_cap1g` exige nome e e-mail (as boas práticas da CAP1G pedem
   identificação para direcionar o pedido) e uma mensagem inicial objetiva. Ele
   recusa mensagem vazia, acima de 2.000 caracteres ou quase toda em caixa alta,
   confirma que há operador e devolve `referencia_preparo`, a mensagem
   normalizada que será enviada e a frase literal de confirmação. A preparação
   expira em 10 minutos; nada é enviado.
3. `iniciar_chat_cap1g` só aceita a frase literal em **nova mensagem do usuário**.
   Ele abre o Chrome **visível** (`PJE_TJPE_CHAT_HEADLESS=false` por padrão),
   preenche o formulário de identificação, clica em *Iniciar Chat* e mantém a
   janela aberta: é o próprio cliente Mibew que faz o polling a cada 2 segundos
   e conserva a conversa viva. A resposta só volta depois que a pergunta inicial
   ecoa na conversa (ou, se o formulário não tiver esse campo, depois de enviá-la
   como primeira mensagem); se a página já abrir direto no chat, o formulário é
   pulado. Uma vez aberta a conversa, uma falha na primeira mensagem vira aviso
   na resposta, não fechamento da janela. O advogado acompanha tudo na janela e
   pode digitar nela por conta própria. Há uma conversa por vez; a mesma
   preparação nunca abre um segundo chat.
4. `ler_chat_cap1g` e `aguardar_resposta_chat_cap1g` leem os modelos do cliente
   Mibew (`thread`, `user`, `messages`), não o HTML pintado: cada mensagem volta
   com `id`, tipo (`visitante`, `operador`, `info`…), autor e horário. A espera
   devolve assim que chega mensagem nova **desde a última entregue** ou o estado
   muda (operador entrou, encerrou), e respeita `timeout_segundos` (1 a 300;
   padrão 60) — o teto do cliente MCP também vale. Um monitor interno lê a
   página a cada 2 segundos e grava uma transcrição parcial, para nada se perder
   entre chamadas.
5. `enviar_mensagem_chat_cap1g` digita no campo do chat, clica em *Enviar* e só
   retorna quando a mensagem ecoa na conversa. Se o eco não vier no prazo, a
   mensagem é tratada como **enviada** mesmo assim: o erro pede para conferir com
   `ler_chat_cap1g`, e o texto idêntico passa a ser recusado. Também recusa texto
   igual ao último enviado e avisa quando a mensagem segue outra sua sem resposta
   do operador — a CAP1G pede que não se repita mensagem em sequência.
6. `encerrar_chat_cap1g` aciona o controle *Fechar chat* do Mibew, fecha a
   janela e publica a transcrição em Markdown, com permissão `0600` e sidecar
   `.sha256`, em `Downloads/PJe-TJPE/TJPE/CAP1G/`. Se o operador encerrar antes,
   a conversa volta como `encerrado` e o envio é recusado; se a janela for
   fechada à mão, o atendimento acaba com esse motivo registrado.

O token da conversa do Mibew nunca sai da página: os modelos devolvidos ao
protocolo MCP não o contêm. O que um servidor informa no chat é orientação de
atendimento, não decisão judicial; confira nos Autos antes de agir.

## Fora do escopo seguro

Esta versão não:

- pesquisa por nome, CPF/CNPJ, NPU parcial, curingas ou listas na Pesquisa Geral;
- percorre mais de 20 páginas do Acervo numa chamada: o painel entrega cerca de 40
  processos por página, e cada avanço é um round-trip a4j;
- reutiliza a abertura da Pesquisa Geral para novo acesso, documento individual
  ou PJeDocs;
- contorna as permissões, restrições de sigilo ou avisos de acesso do PJe;
- gera ou paga guia;
- registra ciência ou abre expediente pendente;
- cria caixas, favoritos ou qualquer outro estado no PJe;
- solicita habilitação, junta, assina ou protocola documentos;
- lê ou armazena certificado, chave privada ou PIN, nem controla token criptográfico;
- escolhe o certificado ou aprova a autenticação no lugar do usuário;
- expõe um cliente MNI universal;
- inicia conversa na CAP1G sem a frase literal do usuário, mantém mais de uma
  conversa por vez, envia anexos pelo chat ou deixa recado fora do horário.

Ciência, habilitação, assinatura, peticionamento e protocolo continuam fora
desta versão e exigiriam uma fase separada, com confirmação explícita e trilha
de auditoria própria. A solicitação de íntegra no PJeDocs não autoriza nenhuma
dessas ações.

## Requisitos e instalação

- macOS, Linux ou Windows;
- Python 3.11 a 3.14 (Python 3.13 recomendado);
- [`uv`](https://docs.astral.sh/uv/);
- **Google Chrome instalado** — o navegador é aberto pelo canal `chrome` do
  Playwright, e não pelo Chromium empacotado. O PJe do TJPE serve conteúdo
  diferente ao Chromium puro, e o PJeOffice espera um Chrome real;
- Codex ou Claude Code com suporte a MCP local.

Para login por certificado, também são necessários o
[PJeOffice/PJeOffice Pro oficial](https://docs.pje.jus.br/servicos-negociais/pjeoffice-pro/)
instalado e em execução, além de um certificado digital válido e reconhecido
pelo aplicativo. Esses itens não são necessários para as ferramentas públicas.

```bash
git clone https://github.com/bbpropulse/mcp-pje-tjpe-dist.git mcp-pje-tjpe
cd mcp-pje-tjpe
uv sync
uv run playwright install chrome
uv run pje-tjpe doctor
```

`playwright install chrome` só confirma (ou instala) o Google Chrome do sistema;
o Chromium empacotado do Playwright não é usado pelo servidor — apenas pela suíte
de testes (`uv run playwright install chromium`, opcional).

O diagnóstico deve confirmar SICAJUD, PJe 1G e PJe 2G. Credenciais não são
necessárias para consulta pública ou simulação de custas.

Sem clonar, o `uvx` resolve o pacote direto do Git a cada execução (o Chrome
continua sendo requisito da máquina):

```bash
uvx --from git+https://github.com/bbpropulse/mcp-pje-tjpe-dist pje-tjpe doctor
```

Para atualizar um clone, `git pull` seguido de `uv sync`; cada versão chega como
um único commit com a tag correspondente (`v0.7.0`, …).

## Codex

Substitua o caminho abaixo pelo caminho absoluto do clone:

```bash
codex mcp add --env PJE_TJPE_AUTH_HEADLESS=false pje-tjpe -- \
  "/CAMINHO/ABSOLUTO/mcp-pje-tjpe/.venv/bin/pje-tjpe" serve
codex mcp list
```

O navegador de autenticação já é visível por padrão; a opção foi escrita no
comando para tornar essa exigência do fluxo por certificado explícita.

Depois de abrir o PJeOffice/PJeOffice Pro, peça ao Codex, nesta ordem:

```text
Use o pje-tjpe para diagnosticar o PJeOffice.
Abra o login assistido por certificado no primeiro grau. Não peça meu PIN.
Verifique se o login por certificado no primeiro grau foi concluído, sem clicar novamente.
```

Para remover:

```bash
codex mcp remove pje-tjpe
```

## Claude Code

```bash
claude mcp add --transport stdio --scope user pje-tjpe \
  --env PJE_TJPE_AUTH_HEADLESS=false -- \
  "/CAMINHO/ABSOLUTO/mcp-pje-tjpe/.venv/bin/pje-tjpe" serve
claude mcp list
```

Ou, sem clone, deixando o `uvx` buscar a versão publicada:

```bash
claude mcp add --transport stdio --scope user pje-tjpe -- \
  uvx --from git+https://github.com/bbpropulse/mcp-pje-tjpe-dist pje-tjpe serve
```

`PJE_TJPE_AUTH_HEADLESS=false` também já é o padrão; ele aparece no comando
para documentar que o fluxo assistido precisa do navegador visível.

No Claude Code, use o mesmo fluxo em três pedidos:

```text
Use o servidor pje-tjpe para executar diagnosticar_pjeoffice.
Execute abrir_login_certificado com grau 1g. Eu concluirei a aprovação no PJeOffice.
Execute verificar_login_certificado com grau 1g, sem iniciar outra tentativa.
```

Para remover:

```bash
claude mcp remove --scope user pje-tjpe
```

## Login individual e MFA

Desde novembro de 2025, o TJPE exige autenticação multifator para usuários
externos. O fluxo atual redireciona 1G/2G ao SSO nacional do PJe e depois retorna
ao tribunal.

Salve CPF e senha somente pelo terminal local:

```bash
uv run pje-tjpe setup
```

Por padrão, não é necessário guardar a semente TOTP. Para digitar o código MFA
no navegador, mantenha `PJE_TJPE_AUTH_HEADLESS=false`, que já é o padrão da
aplicação. `PJE_TJPE_HEADLESS` controla apenas o navegador das operações
públicas e não substitui essa configuração.

O `setup` também oferece, de forma opt-in, guardar a semente TOTP no Keychain.
Isso automatiza o código, mas reduz a separação entre os fatores porque senha e
semente ficam no mesmo cofre. O fluxo por certificado não usa essas credenciais
e não exige executar `setup`.

### Certificado digital

O fluxo de certificado, introduzido na versão 0.2, é assistido:

1. Instale e abra o PJeOffice/PJeOffice Pro oficial e conecte o token, se houver.
2. Execute `diagnosticar_pjeoffice`. O teste apenas tenta abrir uma conexão TCP
   em `localhost:8800` (IPv4 e IPv6) e procura **Certificado Digital** nas telas SSO de
   1G e 2G. Ele não clica no botão nem envia challenge, cookie, CPF, senha ou PIN
   ao serviço local. Porta aberta não prova que o processo seja o PJeOffice.
3. Execute `abrir_login_certificado` com `grau` igual a `1g` ou `2g`. O MCP abre
   o SSO em um Chromium visível, aciona **Certificado Digital** uma vez e mantém
   esse contexto somente em memória.
4. Escolha o certificado e digite o PIN exclusivamente na interface nativa do
   PJeOffice/PJeOffice Pro. Conclua também eventual MFA solicitado pelo SSO.
5. Execute `verificar_login_certificado` para consultar a mesma tentativa. Essa
   ferramenta não altera a aba visível nem clica novamente: ela confirma os cookies
   com uma requisição de leitura a uma página protegida. O retorno expõe somente a
   URL canônica do ambiente, nunca query, token SSO ou caminho de processo.

Se você cancelar a janela nativa ou quiser começar de novo no mesmo grau, execute
`abrir_login_certificado` com `reiniciar=true`. O MCP encerra o contexto anterior
antes de fazer um único novo clique. Ele também impede tentativas simultâneas de
1G e 2G enquanto uma delas aguarda interação humana.

> **Nunca forneça o PIN ao Codex, ao Claude, ao chat, a uma ferramenta MCP, ao
> terminal ou a uma variável de ambiente. Digite-o somente na janela oficial do
> PJeOffice/PJeOffice Pro.**

O MCP não seleciona o certificado, não lê a chave privada, não captura o PIN e
não aprova a operação. Fechar o servidor encerra a sessão mantida em memória.

Os testes automatizados validam socket, navegador, domínios e estados com mocks.
Um teste ponta a ponta real não pode ser concluído de forma autônoma: ele exige o
PJeOffice/PJeOffice Pro instalado e aberto, certificado válido, acesso ao SSO e
interação humana para selecionar o certificado e informar PIN e eventual MFA.
Por isso, a suíte padrão não afirma que um login real por certificado foi validado.

Remova os segredos locais com:

```bash
uv run pje-tjpe clear-credentials
```

## Exemplos de pedidos ao MCP

```text
Consulte o processo público 0000000-00.2026.8.17.0000 no primeiro grau.

Pesquise classes de custas contendo "procedimento comum".

Simule no SICAJUD as custas do código 7, Procedimento Comum Cível,
com valor da causa de R$ 50.000,00.

Diagnostique o acesso aos serviços públicos do TJPE.

Diagnostique passivamente o PJeOffice e os botões de certificado de 1G e 2G.

Abra o login assistido por certificado no segundo grau. Eu selecionarei o
certificado e digitarei o PIN somente no PJeOffice.

Verifique a tentativa de login por certificado do segundo grau sem clicar novamente.

Liste meu Acervo do primeiro grau. Não abra Expedientes nem registre ciência.

Consulte os Autos do processo que acabou de ser retornado pelo Acervo.

Prepare a Pesquisa Geral autenticada pelo NPU exato no primeiro grau. Ainda não
abra o resultado.

Abra o resultado preparado usando exatamente a referencia_preparo e a frase de
confirmação que enviei nesta nova mensagem.

Consulte os Autos em cache desse processo, sem realizar um novo acesso.

Leia o documento usando a referencia_documento retornada pelos Autos.

Baixe esse documento direto, gere o sidecar SHA-256 e informe os dois caminhos.

Prepare o download da íntegra pelo PJeDocs para o processo retornado pelo meu
Acervo do primeiro grau. Ainda não solicite a geração.

Solicite a geração usando a referencia_preparo e exatamente a frase de
confirmação retornadas pela preparação.

Consulte a Área de download usando a referencia_solicitacao. Não repita a
solicitação se o arquivo ainda estiver sendo processado.

Baixe o resultado pronto usando a referencia_resultado, valide o teto local e
informe os caminhos do arquivo e do sidecar SHA-256. Não exponha a URL temporária.

Verifique se a CAP1G está com operador no chat. Não abra conversa.

Prepare o chat da CAP1G em meu nome, com meu e-mail, pedindo que informem se o
alvará do processo tal já foi expedido. Ainda não inicie.

Inicie o chat usando a referencia_preparo e exatamente a frase de confirmação
que envio nesta mensagem.

Aguarde a resposta do operador por até dois minutos e me diga o que ele escreveu.

Responda ao operador informando o número da OAB e aguarde de novo.

Encerre o chat e me informe o caminho da transcrição e o SHA-256.
```

Use sempre um NPU real que você esteja autorizado a consultar. O NPU do exemplo
é apenas ilustrativo e não passa na validação.

## Configuração

| Variável | Padrão | Finalidade |
| --- | --- | --- |
| `PJE_TJPE_HEADLESS` | `true` | Exibe ou oculta o Chromium usado em operações públicas |
| `PJE_TJPE_AUTH_HEADLESS` | `false` | Mantém visível o Chromium de autenticação; deve ser `false` para certificado |
| `PJE_TJPE_CHAT_HEADLESS` | `false` | Mantém visível a janela do chat da CAP1G para o advogado acompanhar e intervir |
| `PJE_TJPE_TIMEOUT_MS` | `30000` | Timeout do portal em milissegundos |
| `PJE_TJPE_MAX_DOCUMENT_BYTES` | `3145728` (3 MiB) | Limite local para leitura e download direto de um documento |
| `PJE_TJPE_MAX_PJEDOCS_BYTES` | `536870912` (512 MiB) | Teto local independente para baixar a íntegra pronta do PJeDocs |
| `PJE_TJPE_ADAPTIVE_MODE` | `observe` | Modo `observe`, `shadow` ou `active`; valor inválido impede a inicialização |
| `PJE_TJPE_ADAPTIVE_MAX_EVENTS` | `200` | Retenção do JSONL local, de 1 a 10.000; deve coincidir entre processos |
| `PJE_TJPE_DATAJUD_API_KEY` | chave pública vigente do CNJ | Sobrescreve a chave da API DataJud se o CNJ a rotacionar |
| `PJE_TJPE_DATA_DIR` | diretório de dados do SO | Reserva dados locais do MCP |
| `PJE_TJPE_DOWNLOAD_DIR` | `Downloads/PJe-TJPE` | Destino dos documentos, íntegras, transcrições do chat e sidecars SHA-256 |

## Testes

```bash
uv run pytest
uv run ruff check .
uv run pyright
```

O teste ao vivo do SICAJUD apenas simula valores e nunca gera guia:

```bash
RUN_TJPE_LIVE_TESTS=1 uv run pytest -m live
```

Para também testar uma consulta pública real, use somente um processo autorizado:

```bash
RUN_TJPE_LIVE_TESTS=1 \
TJPE_TEST_NPU="NNNNNNN-DD.AAAA.8.17.OOOO" \
TJPE_TEST_GRAU="1g" \
uv run pytest -m live
```

Esse marcador cobre serviços públicos; ele não executa login ponta a ponta por
certificado e não substitui a validação humana descrita acima.

## Distribuição pública

Cada versão chega a este repositório como um único commit, gerado no repositório
de desenvolvimento por `scripts/publicar_distribuicao.py`: o script exige árvore
limpa, roda `ruff` e a suíte, exporta só os arquivos rastreados (`git archive`),
varre o snapshot procurando NPU ou CPF com dígitos válidos, e-mail real ou
caminho pessoal — qualquer achado interrompe a publicação — e então commita e
etiqueta (`vX.Y.Z`) na pasta de distribuição. O histórico de desenvolvimento não
é publicado.

## Referências oficiais

- [PJe/TJPE](https://portal.tjpe.jus.br/web/processo-judicial-eletronico)
- [PJe-JT/TRT6](https://www.trt6.jus.br/portal/pje)
- [Histórico e autenticação do PJe-JT/TRT6](https://www.trt6.jus.br/portal/pje/historico)
- [Acessos PJe/TRF5](https://www.trf5.jus.br/index.php/pje?action=acesso-pje)
- [Justiça Federal em Pernambuco](https://www.jfpe.jus.br/)
- [Manual do Advogado](https://docs.pje.jus.br/manuais-de-uso/Manual%20do%20advogado/)
- [Resolução CNJ 121](https://atos.cnj.jus.br/atos/detalhar/atos-normativos?documento=92)
- [Serviço Autos Digitais do PJe](https://docs.pje.jus.br/servicos-negociais/servico-autos-digitais/)
- [Manual PJeDocs do TJPE](https://portal.tjpe.jus.br/documents/101861/102095/Manual%2BPJeDocs.pdf/a218e088-0fd2-90ed-3440-1c2dd0f54499)
- [MFA para usuários externos do TJPE](https://portal.tjpe.jus.br/-/pje-usu%C3%A1rios-as-externos-as-t%C3%AAm-nova-forma-de-acesso-a-partir-desta-segunda-feira-3/11-)
- [Serviço SSO do PJe](https://docs.pje.jus.br/servicos-negociais/servico-sso-pje-kc/)
- [PJeOffice Pro](https://docs.pje.jus.br/servicos-negociais/pjeoffice-pro/)
- [Simulação pública do SICAJUD](https://www.tjpe.jus.br/custasjudiciais/xhtml/simulacao/simularCustas.xhtml)

O próprio Manual do Advogado alerta que parte do conteúdo foi migrada da antiga
Wiki e pode estar desatualizada. Por isso, o código de autenticação foi conferido
contra o SSO atual e a comunicação mais recente do TJPE sobre MFA.
