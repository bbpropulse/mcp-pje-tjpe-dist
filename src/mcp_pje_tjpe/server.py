from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations

from mcp_pje_tjpe import __version__
from mcp_pje_tjpe.adaptive import AdapterRegistry, AdaptiveNavigation, ObservationStore
from mcp_pje_tjpe.adaptive.models import (
    ObservacaoNavegacao,
    StatusNavegacaoAdaptativa,
    ValidacaoAdaptadoresOffline,
)
from mcp_pje_tjpe.browser import BrowserManager
from mcp_pje_tjpe.build import revisao_carregada
from mcp_pje_tjpe.cap1g_chat import Cap1gChatService
from mcp_pje_tjpe.config import Settings, settings
from mcp_pje_tjpe.credentials import CredentialStore, credential_store
from mcp_pje_tjpe.datajud import DatajudClient
from mcp_pje_tjpe.diagnostics import run_diagnostics
from mcp_pje_tjpe.errors import ServicoIndisponivelError, ValidacaoError
from mcp_pje_tjpe.models import (
    AcessoPesquisaGeral,
    AmbienteTJPE,
    ArquivoBaixado,
    ArquivoPjeDocsBaixado,
    AutosDigitais,
    ClasseCustas,
    Diagnostico,
    DisponibilidadeChatCap1g,
    EncerramentoChatCap1g,
    EstruturaAbaPainel,
    EstruturaAutos,
    Grau,
    JurisdicoesAcervo,
    MetadadosProcesso,
    PaginaAcervo,
    PaginaDownloadsPjeDocs,
    PreparacaoChatCap1g,
    PreparacaoPesquisaGeral,
    PreparacaoPjeDocs,
    ProcessoPublico,
    SessaoChatCap1g,
    SimulacaoCustas,
    SolicitacaoPjeDocs,
    StatusLogin,
    StatusServidor,
    TextoDocumentoAutos,
)
from mcp_pje_tjpe.pje_auth import PjeSessionManager
from mcp_pje_tjpe.pje_docs import PjeDocsService
from mcp_pje_tjpe.pje_office import (
    PjeOfficeDiagnostic,
    check_local_pje_office_port,
    diagnose_pje_office,
)
from mcp_pje_tjpe.pje_public import PjePublicClient
from mcp_pje_tjpe.pje_read import PjeReadService
from mcp_pje_tjpe.pje_search import PjeSearchService
from mcp_pje_tjpe.sicajud import SicajudClient
from mcp_pje_tjpe.tribunals import (
    PerfilTribunal,
    TribunalCodigo,
    listar_perfis_tribunais,
)


@dataclass(slots=True)
class Runtime:
    config: Settings
    browser: BrowserManager
    auth_browser: BrowserManager
    sicajud: SicajudClient
    pje_publico: PjePublicClient
    datajud: DatajudClient
    pje_sessions: PjeSessionManager
    pje_leitura: PjeReadService
    pje_pesquisa: PjeSearchService
    pje_docs: PjeDocsService
    adaptive: AdaptiveNavigation
    adapters: AdapterRegistry
    credentials: CredentialStore
    chat_browser: BrowserManager
    cap1g: Cap1gChatService


@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncGenerator[Runtime]:
    browser = BrowserManager(settings)
    auth_config = replace(settings, headless=settings.auth_headless)
    auth_browser = BrowserManager(auth_config, accept_downloads=False)
    pje_sessions = PjeSessionManager(auth_browser, auth_config, credential_store)
    pje_leitura = PjeReadService(pje_sessions, auth_config)
    adapters = AdapterRegistry()
    adaptive_store = ObservationStore(
        settings.data_dir,
        max_events=settings.adaptive_max_events,
    )
    adaptive = AdaptiveNavigation(adapters, adaptive_store, settings.adaptive_mode)
    pje_pesquisa = PjeSearchService(
        pje_sessions,
        pje_leitura,
        auth_config,
        adaptive=adaptive,
    )
    pje_docs = PjeDocsService(pje_sessions, pje_leitura, auth_config)
    chat_browser = BrowserManager(
        replace(settings, headless=settings.chat_headless), accept_downloads=False
    )
    cap1g = Cap1gChatService(chat_browser, browser, settings)
    runtime = Runtime(
        config=settings,
        browser=browser,
        auth_browser=auth_browser,
        sicajud=SicajudClient(browser, settings),
        pje_publico=PjePublicClient(browser, settings),
        datajud=DatajudClient(settings),
        pje_sessions=pje_sessions,
        pje_leitura=pje_leitura,
        pje_pesquisa=pje_pesquisa,
        pje_docs=pje_docs,
        adaptive=adaptive,
        adapters=adapters,
        credentials=credential_store,
        chat_browser=chat_browser,
        cap1g=cap1g,
    )
    try:
        yield runtime
    finally:
        await cap1g.close()
        await chat_browser.close()
        await pje_sessions.close()
        await auth_browser.close()
        await browser.close()


mcp = MCPServer(
    "pje-tjpe",
    version=__version__,
    instructions=(
        "Ferramentas locais e seguras para serviços judiciais de Pernambuco. "
        "TJPE está operacional; TRT6 e TRF5 estão catalogados em modo de descoberta e "
        "não autorizam ainda navegação autenticada. A aprendizagem adaptativa usa somente "
        "estrutura sanitizada em JSONL local; YAML nunca altera políticas de rede, sigilo, "
        "permissão ou confirmação. "
        "O login por certificado é assistido: certificado e PIN ficam no PJeOffice. "
        "Metadados públicos por NPU vêm do DataJud do CNJ em consultar_metadados_processo: "
        "sem navegador, sem sessão e sem registrar acesso, valendo para TJPE, TRT6 e TRF5; "
        "não traz partes, documentos nem intimações, e não substitui a leitura autenticada. "
        "O Acervo do TJPE é particionado por jurisdição: use listar_jurisdicoes_acervo "
        "e depois listar_acervo com o rótulo escolhido; sem jurisdição não há lista. "
        "A leitura autenticada usa processos do Acervo da mesma sessão ou um NPU exato "
        "da Pesquisa Geral aberto após confirmação explícita. A abertura de processo de "
        "terceiro pode ser registrada pelo PJe nos termos da Resolução CNJ 121. Tanto "
        "essa abertura quanto a íntegra assíncrona do PJeDocs exigem uma nova mensagem "
        "literal do usuário. Nunca reutilize automaticamente a frase devolvida por uma "
        "preparação. A consulta posterior dos Autos pesquisados usa somente o conteúdo "
        "em cache; documentos e PJeDocs permanecem restritos ao Acervo. O servidor não "
        "gera guias, não registra ciência e não protocola petições. "
        "O chat da CAP1G (Central de Atendimento Processual do 1º Grau) é uma conversa "
        "com um servidor do tribunal, das 8h às 19h em dias úteis: verificar_chat_cap1g é "
        "passivo; preparar_chat_cap1g valida nome, e-mail e mensagem inicial e devolve "
        "uma frase que o usuário precisa enviar em nova mensagem para iniciar_chat_cap1g. "
        "Depois, uma mensagem por vez com enviar_mensagem_chat_cap1g, esperando a resposta "
        "com aguardar_resposta_chat_cap1g em vez de repetir; encerrar_chat_cap1g grava a "
        "transcrição com SHA-256. O que o operador informa no chat não substitui os Autos."
    ),
    lifespan=lifespan,
)


def _runtime(ctx: Context[Runtime]) -> Runtime:
    return ctx.request_context.lifespan_context


@mcp.tool()
async def status_servidor(ctx: Context[Runtime]) -> StatusServidor:
    """Informa versão, segurança e recursos disponíveis sem abrir o navegador."""
    runtime = _runtime(ctx)
    return StatusServidor(
        nome="mcp-pje-tjpe",
        versao=__version__,
        revisao=revisao_carregada(),
        tribunal="TJPE",
        transporte="stdio",
        modo_seguro=True,
        credenciais_configuradas=runtime.credentials.has_credentials(),
        recursos_ativos=[
            "simulação pública de custas no SICAJUD",
            "pesquisa de classes processuais para custas",
            "consulta processual pública do PJe/TJPE",
            "metadados públicos por NPU na API DataJud do CNJ (TJPE, TRT6 e TRF5)",
            "diagnóstico dos ambientes públicos",
            "teste de login individual em memória",
            "login assistido por certificado via PJeOffice",
            "leitura autenticada do Acervo e dos Autos Digitais",
            "abertura confirmada por NPU exato na Pesquisa Geral autenticada",
            "leitura e download individual com SHA-256",
            "solicitação confirmada de íntegra e download via PJeDocs com SHA-256",
            "catálogo oficial inicial de ambientes TRT6 e TRF5/JFPE",
            "observação estrutural sanitizada em JSONL e replay offline de adaptadores YAML",
            "atendimento pelo chat da CAP1G com confirmação literal e transcrição SHA-256",
        ],
    )


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
def listar_tribunais_suportados() -> list[PerfilTribunal]:
    """Lista ambientes oficiais e informa claramente quais adaptadores ainda estão em descoberta."""
    return listar_perfis_tribunais()


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
async def status_navegacao_adaptativa(
    ctx: Context[Runtime],
) -> StatusNavegacaoAdaptativa:
    """Informa modo, retenção e adaptadores locais sem abrir navegador ou acessar tribunal."""
    return await _runtime(ctx).adaptive.status()


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
async def listar_falhas_navegacao(
    ctx: Context[Runtime],
    limite: int = 20,
) -> list[ObservacaoNavegacao]:
    """Lê eventos estruturais sanitizados; nunca retorna HTML, NPU, credencial ou texto livre."""
    runtime = _runtime(ctx)
    if not 1 <= limite <= runtime.config.adaptive_max_events:
        raise ValidacaoError("limite deve respeitar a retenção adaptativa configurada")
    return await runtime.adaptive.list_observations(limit=limite)


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
async def validar_adaptadores_offline(
    ctx: Context[Runtime],
    limite: int = 100,
) -> ValidacaoAdaptadoresOffline:
    """Reavalia o JSONL com os YAML empacotados sem rede, clique ou preenchimento."""
    runtime = _runtime(ctx)
    if not 1 <= limite <= runtime.config.adaptive_max_events:
        raise ValidacaoError("limite deve respeitar a retenção adaptativa configurada")
    return await runtime.adaptive.validate_offline(limit=limite)


@mcp.tool()
def listar_ambientes() -> list[AmbienteTJPE]:
    """Lista URLs oficiais configuradas para o PJe/TJPE de 1º e 2º graus."""
    return [
        AmbienteTJPE(
            grau=grau,
            pje=settings.urls.pje_base(grau.value),
            consulta_publica=(
                f"{settings.urls.pje_base(grau.value)}/ConsultaPublica/listView.seam"
            ),
            login=f"{settings.urls.pje_base(grau.value)}/login.seam",
        )
        for grau in Grau
    ]


@mcp.tool()
async def diagnosticar_ambiente(ctx: Context[Runtime]) -> Diagnostico:
    """Testa Chromium, SICAJUD e consultas públicas do TJPE sem fazer login."""
    runtime = _runtime(ctx)
    return await run_diagnostics(runtime.browser, runtime.config, runtime.credentials)


@mcp.tool()
async def diagnosticar_pjeoffice(ctx: Context[Runtime]) -> PjeOfficeDiagnostic:
    """Verifica passivamente porta local e botões SSO, sem clicar ou enviar dados."""
    runtime = _runtime(ctx)
    return await diagnose_pje_office(runtime.browser, runtime.config)


@mcp.tool()
async def pesquisar_classes_custas(termo: str, ctx: Context[Runtime]) -> list[ClasseCustas]:
    """Pesquisa códigos e descrições de classes na lista oficial do SICAJUD."""
    return await _runtime(ctx).sicajud.pesquisar_classes(termo)


@mcp.tool()
async def simular_custas(
    classe: str,
    valor_causa: str,
    ctx: Context[Runtime],
    codigo_classe: str | None = None,
) -> SimulacaoCustas:
    """Simula taxa e custas no SICAJUD; não cria, vincula ou paga uma guia."""
    return await _runtime(ctx).sicajud.simular(classe, valor_causa, codigo_classe=codigo_classe)


@mcp.tool()
async def consultar_processo_publico(
    numero: str, grau: Grau, ctx: Context[Runtime]
) -> ProcessoPublico:
    """Consulta os dados públicos de um NPU do TJPE e mascara CPF/CNPJ no retorno."""
    return await _runtime(ctx).pje_publico.consultar(numero, grau)


@mcp.tool()
async def testar_login(grau: Grau, ctx: Context[Runtime]) -> StatusLogin:
    """Testa o login pessoal salvo no Keychain, sem abrir processo ou expediente."""
    return await _runtime(ctx).pje_sessions.testar_login(grau)


@mcp.tool()
async def abrir_login_certificado(
    grau: Grau, ctx: Context[Runtime], reiniciar: bool = False
) -> StatusLogin:
    """Abre o SSO visível e delega certificado/PIN ao PJeOffice instalado localmente."""
    runtime = _runtime(ctx)
    port_status = await check_local_pje_office_port()
    if not port_status.conexao_aceita:
        raise ServicoIndisponivelError(
            "o PJeOffice não está respondendo em localhost:8800 (IPv4 ou IPv6); "
            "abra o aplicativo oficial antes de iniciar o login"
        )
    return await runtime.pje_sessions.abrir_login_certificado(grau, reiniciar=reiniciar)


@mcp.tool()
async def verificar_login_certificado(grau: Grau, ctx: Context[Runtime]) -> StatusLogin:
    """Verifica a tentativa existente sem clicar novamente ou solicitar PIN."""
    return await _runtime(ctx).pje_sessions.verificar_login_certificado(grau)


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    )
)
async def consultar_metadados_processo(
    numero: str,
    ctx: Context[Runtime],
    tribunal: TribunalCodigo = TribunalCodigo.TJPE,
    limite_movimentos: int = 100,
) -> MetadadosProcesso:
    """Metadados públicos de um NPU na API DataJud do CNJ, sem navegador nem sessão."""
    return await _runtime(ctx).datajud.consultar_metadados(
        numero,
        tribunal,
        limite_movimentos=limite_movimentos,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    )
)
async def inspecionar_aba_painel(
    grau: Grau, aba: str, ctx: Context[Runtime], jurisdicao: str | None = None
) -> EstruturaAbaPainel:
    """Descreve a estrutura de uma aba consultiva do painel, sem preencher ou submeter."""
    return await _runtime(ctx).pje_leitura.inspecionar_aba_painel(grau, aba, jurisdicao)


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    )
)
async def inspecionar_estrutura_autos(
    numero: str, grau: Grau, ctx: Context[Runtime]
) -> EstruturaAutos:
    """Descreve a estrutura da timeline dos Autos, sem abrir ou ler peça alguma."""
    return await _runtime(ctx).pje_leitura.inspecionar_estrutura_autos(numero, grau)


@mcp.tool()
async def listar_jurisdicoes_acervo(grau: Grau, ctx: Context[Runtime]) -> JurisdicoesAcervo:
    """Lista as jurisdições do Acervo deste grau, para escolher uma em listar_acervo."""
    return await _runtime(ctx).pje_leitura.listar_jurisdicoes_acervo(grau)


@mcp.tool()
async def listar_acervo(
    grau: Grau,
    ctx: Context[Runtime],
    pesquisa: str | None = None,
    limite: int = 50,
    jurisdicao: str | None = None,
    filtro: str | None = None,
    paginas: int = 1,
    parte: str | None = None,
    documento: str | None = None,
    oab: str | None = None,
    classe: str | None = None,
    assunto: str | None = None,
) -> PaginaAcervo:
    """Lista ou pesquisa o Acervo de uma jurisdição; a busca roda no próprio PJe."""
    return await _runtime(ctx).pje_leitura.listar_acervo(
        grau,
        pesquisa=pesquisa,
        limite=limite,
        jurisdicao=jurisdicao,
        filtro=filtro,
        paginas=paginas,
        parte=parte,
        documento=documento,
        oab=oab,
        classe=classe,
        assunto=assunto,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    )
)
async def preparar_acesso_pesquisa_geral(
    numero: str,
    grau: Grau,
    ctx: Context[Runtime],
) -> PreparacaoPesquisaGeral:
    """Pesquisa um NPU exato e prepara sua abertura sem registrar o acesso ao processo."""
    return await _runtime(ctx).pje_pesquisa.preparar(numero, grau)


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=True,
        open_world_hint=True,
    )
)
async def abrir_autos_pesquisa_geral(
    numero: str,
    grau: Grau,
    referencia_preparo: str,
    confirmacao: str,
    ctx: Context[Runtime],
) -> AcessoPesquisaGeral:
    """Abre o resultado somente após nova mensagem literal; o PJe pode registrar o acesso."""
    return await _runtime(ctx).pje_pesquisa.abrir(
        numero,
        grau,
        referencia_preparo,
        confirmacao,
    )


@mcp.tool()
async def consultar_autos(
    numero: str,
    grau: Grau,
    ctx: Context[Runtime],
    limite_documentos: int = 200,
    limite_movimentos: int = 100,
) -> AutosDigitais:
    """Lê Autos do Acervo ou o cache de uma abertura confirmada na Pesquisa Geral."""
    return await _runtime(ctx).pje_leitura.consultar_autos(
        numero,
        grau,
        limite_documentos=limite_documentos,
        limite_movimentos=limite_movimentos,
    )


@mcp.tool()
async def ler_documento_autos(
    numero: str,
    grau: Grau,
    referencia_documento: str,
    ctx: Context[Runtime],
    max_paginas: int = 30,
    max_caracteres: int = 100_000,
) -> TextoDocumentoAutos:
    """Extrai texto de documento já enumerado nos Autos e informa seu SHA-256."""
    return await _runtime(ctx).pje_leitura.ler_documento(
        numero,
        grau,
        referencia_documento,
        max_paginas=max_paginas,
        max_caracteres=max_caracteres,
    )


@mcp.tool()
async def baixar_documento_autos(
    numero: str,
    grau: Grau,
    referencia_documento: str,
    ctx: Context[Runtime],
) -> ArquivoBaixado:
    """Baixa um documento enumerado nos Autos e grava arquivo e sidecar SHA-256."""
    return await _runtime(ctx).pje_leitura.baixar_documento(
        numero,
        grau,
        referencia_documento,
    )


@mcp.tool()
async def preparar_download_pjedocs(
    numero: str,
    grau: Grau,
    ctx: Context[Runtime],
) -> PreparacaoPjeDocs:
    """Prepara a solicitação da íntegra no PJeDocs sem disparar a geração."""
    return await _runtime(ctx).pje_docs.preparar(numero, grau)


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=True,
        open_world_hint=True,
    )
)
async def solicitar_download_pjedocs(
    numero: str,
    grau: Grau,
    referencia_preparo: str,
    confirmacao: str,
    ctx: Context[Runtime],
) -> SolicitacaoPjeDocs:
    """Solicita a íntegra preparada somente após nova mensagem do usuário com a frase literal."""
    return await _runtime(ctx).pje_docs.solicitar(
        numero,
        grau,
        referencia_preparo,
        confirmacao,
    )


@mcp.tool()
async def listar_downloads_pjedocs(
    numero: str,
    grau: Grau,
    referencia_solicitacao: str,
    ctx: Context[Runtime],
) -> PaginaDownloadsPjeDocs:
    """Consulta o estado do resultado solicitado no PJeDocs sem abrir outros processos."""
    return await _runtime(ctx).pje_docs.listar(numero, grau, referencia_solicitacao)


@mcp.tool()
async def baixar_resultado_pjedocs(
    numero: str,
    grau: Grau,
    referencia_resultado: str,
    ctx: Context[Runtime],
) -> ArquivoPjeDocsBaixado:
    """Baixa um resultado pronto e vinculado, gravando o arquivo e seu SHA-256."""
    return await _runtime(ctx).pje_docs.baixar(numero, grau, referencia_resultado)


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    )
)
async def verificar_chat_cap1g(ctx: Context[Runtime]) -> DisponibilidadeChatCap1g:
    """Verifica passivamente se a CAP1G tem operador no chat, sem abrir conversa."""
    return await _runtime(ctx).cap1g.verificar()


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    )
)
async def preparar_chat_cap1g(
    nome: str,
    email: str,
    mensagem_inicial: str,
    ctx: Context[Runtime],
) -> PreparacaoChatCap1g:
    """Valida identificação e mensagem inicial e prepara o chat sem iniciá-lo."""
    return await _runtime(ctx).cap1g.preparar(nome, email, mensagem_inicial)


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=True,
        open_world_hint=True,
    )
)
async def iniciar_chat_cap1g(
    referencia_preparo: str,
    confirmacao: str,
    ctx: Context[Runtime],
) -> SessaoChatCap1g:
    """Abre o chat preparado, em janela visível, somente após a frase literal do usuário."""
    return await _runtime(ctx).cap1g.iniciar(referencia_preparo, confirmacao)


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    )
)
async def ler_chat_cap1g(
    referencia_chat: str,
    ctx: Context[Runtime],
    desde_id: int = 0,
) -> SessaoChatCap1g:
    """Lê estado e mensagens do chat em andamento sem esperar nem enviar nada."""
    return await _runtime(ctx).cap1g.ler(referencia_chat, desde_id=desde_id)


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    )
)
async def aguardar_resposta_chat_cap1g(
    referencia_chat: str,
    ctx: Context[Runtime],
    timeout_segundos: int = 60,
    desde_id: int | None = None,
) -> SessaoChatCap1g:
    """Espera mensagem nova desde a última entregue, ou mudança de estado, até o tempo dado."""
    return await _runtime(ctx).cap1g.aguardar(
        referencia_chat,
        timeout_segundos=timeout_segundos,
        desde_id=desde_id,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=True,
    )
)
async def enviar_mensagem_chat_cap1g(
    referencia_chat: str,
    mensagem: str,
    ctx: Context[Runtime],
) -> SessaoChatCap1g:
    """Envia uma mensagem ao operador e confirma o eco no chat; recusa repetição."""
    return await _runtime(ctx).cap1g.enviar(referencia_chat, mensagem)


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=True,
        open_world_hint=True,
    )
)
async def encerrar_chat_cap1g(
    referencia_chat: str,
    ctx: Context[Runtime],
) -> EncerramentoChatCap1g:
    """Encerra a conversa, fecha a janela e grava a transcrição com sidecar SHA-256."""
    return await _runtime(ctx).cap1g.encerrar(referencia_chat)


def serve_stdio() -> None:
    mcp.run()


if __name__ == "__main__":
    serve_stdio()
