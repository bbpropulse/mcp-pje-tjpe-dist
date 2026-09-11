from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class Grau(StrEnum):
    PRIMEIRO = "1g"
    SEGUNDO = "2g"


class EstadoLogin(StrEnum):
    NAO_INICIADO = "nao_iniciado"
    AGUARDANDO_INTERACAO = "aguardando_interacao"
    AUTENTICADO = "autenticado"
    EXPIRADO = "expirado"
    ERRO = "erro"


class EstadoAcessoPesquisaGeral(StrEnum):
    ABERTO = "aberto"
    INDETERMINADO = "indeterminado"


class EstadoSolicitacaoPjeDocs(StrEnum):
    SOLICITADO = "solicitado"
    INDETERMINADO = "indeterminado"


class EstadoDownloadPjeDocs(StrEnum):
    PROCESSANDO = "processando"
    PRONTO = "pronto"
    EXPIRADO = "expirado"


class StatusItem(BaseModel):
    nome: str
    sucesso: bool
    detalhe: str


class Diagnostico(BaseModel):
    sucesso: bool
    itens: list[StatusItem]


class StatusServidor(BaseModel):
    nome: str
    versao: str
    # Qual código está de fato em memória. O servidor stdio sobe uma vez e vive a
    # sessão inteira; sem isso, saber se uma correção entrou vira adivinhação.
    revisao: str | None = None
    tribunal: str
    transporte: str
    modo_seguro: bool
    credenciais_configuradas: bool
    recursos_ativos: list[str]


class StatusLogin(BaseModel):
    grau: Grau
    autenticado: bool
    estado: EstadoLogin
    modo_autenticacao: str = "cpf_senha_mfa"
    url_atual: str
    mensagem: str


class AmbienteTJPE(BaseModel):
    grau: Grau
    pje: str
    consulta_publica: str
    login: str


class ClasseCustas(BaseModel):
    codigo: str
    descricao: str


class ItemCustas(BaseModel):
    descricao: str
    valor: Decimal = Field(ge=0)
    fundamento_legal: str | None = None


class SimulacaoCustas(BaseModel):
    classe: ClasseCustas
    valor_causa: Decimal = Field(ge=0)
    valor_total: Decimal = Field(ge=0)
    itens: list[ItemCustas]
    fonte: str = "SICAJUD/TJPE"
    url_fonte: str
    versao_sicajud: str | None = None
    natureza: str = "estimativa pública"
    capturado_em: datetime = Field(default_factory=lambda: datetime.now(UTC))
    aviso: str


class MovimentoPublico(BaseModel):
    data: str | None = None
    descricao: str


class ProcessoPublico(BaseModel):
    numero: str
    grau: Grau
    classe: str | None = None
    assunto: str | None = None
    orgao_julgador: str | None = None
    valor_causa: str | None = None
    partes: list[str] = []
    movimentos: list[MovimentoPublico] = []
    fonte: str = "Consulta pública PJe/TJPE"
    capturado_em: datetime = Field(default_factory=lambda: datetime.now(UTC))
    url_fonte: str
    observacao: str | None = None


class ItemAcervo(BaseModel):
    numero: str
    grau: Grau
    resumo: str
    restrito: bool = False
    autos_disponiveis: bool = False


class MovimentoDatajud(BaseModel):
    data_hora: str
    codigo: int | None = None
    nome: str
    orgao_julgador: str | None = None


class MetadadosProcesso(BaseModel):
    numero: str
    tribunal: str
    grau: str | None = None
    sistema: str | None = None
    formato: str | None = None
    classe: str | None = None
    codigo_classe: int | None = None
    assuntos: list[str] = []
    orgao_julgador: str | None = None
    data_ajuizamento: str | None = None
    ultima_atualizacao: str | None = None
    nivel_sigilo: int = 0
    movimentos: list[MovimentoDatajud] = []
    total_movimentos: int = Field(default=0, ge=0)
    movimentos_truncados: bool = False
    capturado_em: datetime = Field(default_factory=lambda: datetime.now(UTC))
    fonte: str = "API pública DataJud/CNJ"
    aviso: str


class CampoFormularioPainel(BaseModel):
    nome: str
    tipo: str
    rotulo: str | None = None
    preenchido: bool = False
    opcoes: list[str] = []


class FormularioPainel(BaseModel):
    id: str | None = None
    acao: str | None = None
    campos: list[CampoFormularioPainel] = []


class EstruturaAbaPainel(BaseModel):
    grau: Grau
    aba: str
    abas_disponiveis: list[str] = []
    formularios: list[FormularioPainel] = []
    botoes: list[str] = []
    campos_visiveis: int = Field(default=0, ge=0)
    iframes: list[str] = []
    # Abas do painel são cascas: o conteúdo vive numa página .seam autônoma,
    # embutida em iframe. Saber o destino vale mais que descrever a casca.
    pagina_embutida: str | None = None
    # Quando a descida no iframe não acontece, a aba volta descrita como moldura
    # vazia. Sem saber que quadros existiam, não há como distinguir "aba sem
    # conteúdo" de "não achei o quadro".
    quadros_vistos: list[str] = []
    conteudo_embutido_lido: bool = False
    # Por que a leitura do quadro não aconteceu. Sem isto o erro do navegador é
    # engolido e a aba volta como moldura vazia, sem dizer o motivo.
    motivo_quadro: str | None = None
    paginacao: list[dict[str, object]] = []
    # Os critérios de busca do Acervo ficam no DOM mesmo recolhidos: sem a cadeia de
    # ancestrais não dá para saber qual deles está oculto nem o que o expande.
    criterios_busca: list[dict[str, object]] = []
    alternadores: list[dict[str, object]] = []
    contadores: list[str] = []
    linhas_na_lista: int = Field(default=0, ge=0)
    capturado_em: datetime = Field(default_factory=lambda: datetime.now(UTC))
    fonte: str = "Painel do Advogado autenticado do PJe/TJPE"
    aviso: str


class EstruturaAutos(BaseModel):
    numero: str
    grau: Grau
    timeline_existe: bool = False
    contagens: dict[str, int] = {}
    documentos: list[dict[str, object]] = []
    capturado_em: datetime = Field(default_factory=lambda: datetime.now(UTC))
    fonte: str = "Autos Digitais autenticados do PJe/TJPE"
    aviso: str


class JurisdicaoAcervo(BaseModel):
    nome: str
    processos: int | None = Field(default=None, ge=0)


class JurisdicoesAcervo(BaseModel):
    grau: Grau
    jurisdicoes: list[JurisdicaoAcervo]
    capturado_em: datetime = Field(default_factory=lambda: datetime.now(UTC))
    fonte: str = "Acervo autenticado do PJe/TJPE"
    aviso: str


class PaginaAcervo(BaseModel):
    grau: Grau
    processos: list[ItemAcervo]
    # Quantos processos foram lidos do painel — não o tamanho de 'processos', que
    # 'limite' pode ter cortado. O aviso diz os dois números quando divergem.
    total_carregado: int = Field(ge=0)
    # O painel pagina: numa jurisdição com milhares de processos ele renderiza 40 por vez.
    # Sem o total, 'total_carregado' passa a impressão de ser a jurisdição inteira.
    total_na_jurisdicao: int | None = Field(default=None, ge=0)
    paginas_percorridas: int = Field(default=1, ge=1)
    parcial: bool
    capturado_em: datetime = Field(default_factory=lambda: datetime.now(UTC))
    fonte: str = "Acervo autenticado do PJe/TJPE"
    aviso: str


class PreparacaoPesquisaGeral(BaseModel):
    referencia_preparo: str = Field(min_length=1)
    numero: str
    grau: Grau
    expira_em: datetime
    frase_confirmacao: str = Field(min_length=1)
    aviso: str


class AcessoPesquisaGeral(BaseModel):
    referencia_acesso: str = Field(min_length=1)
    numero: str
    grau: Grau
    estado: EstadoAcessoPesquisaGeral
    reutilizada: bool
    acessado_em: datetime
    aviso: str


class MovimentoAutos(BaseModel):
    data: str | None = None
    # O PJe cola o horário no fim da descrição, sem separador; separá-lo evita
    # descrições como "Conclusos para despacho 07:49".
    hora: str | None = None
    descricao: str


class DocumentoAutos(BaseModel):
    referencia: str
    id_exibido: str
    titulo: str
    data: str | None = None
    bloqueado_por_ciencia: bool = False
    conteudo_disponivel: bool = True


class AutosDigitais(BaseModel):
    numero: str
    grau: Grau
    origem: Literal["acervo", "pesquisa_geral"] = "acervo"
    cabecalho: dict[str, str]
    movimentos: list[MovimentoAutos]
    documentos: list[DocumentoAutos]
    documentos_parciais: bool
    capturado_em: datetime = Field(default_factory=lambda: datetime.now(UTC))
    fonte: str = "Autos Digitais autenticados do PJe/TJPE"
    aviso: str


class TextoDocumentoAutos(BaseModel):
    numero: str
    grau: Grau
    referencia_documento: str
    titulo: str
    tipo_mime: str
    paginas_lidas: int | None = None
    paginas_totais: int | None = None
    texto: str
    truncado: bool
    sha256: str
    tamanho_bytes: int = Field(ge=0)
    aviso: str


class ArquivoBaixado(BaseModel):
    numero: str
    grau: Grau
    referencia_documento: str
    titulo: str
    caminho: str
    caminho_sha256: str
    nome_arquivo: str
    tipo_mime: str
    tamanho_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    obtido_em: datetime = Field(default_factory=lambda: datetime.now(UTC))
    aviso: str


class PreparacaoPjeDocs(BaseModel):
    referencia_preparo: str = Field(min_length=1)
    numero: str
    grau: Grau
    modo: Literal["integra"] = "integra"
    expira_em: datetime
    frase_confirmacao: str = Field(min_length=1)
    inclui_expedientes: bool = False
    inclui_movimentos: bool = False
    aviso: str


class SolicitacaoPjeDocs(BaseModel):
    referencia_solicitacao: str = Field(min_length=1)
    numero: str
    grau: Grau
    estado: EstadoSolicitacaoPjeDocs
    reutilizada: bool
    solicitado_em: datetime
    aviso: str


class ItemDownloadPjeDocs(BaseModel):
    nome: str
    expira_em: datetime | None = None
    estado: EstadoDownloadPjeDocs
    referencia_resultado: str | None = None


class PaginaDownloadsPjeDocs(BaseModel):
    numero: str
    grau: Grau
    referencia_solicitacao: str = Field(min_length=1)
    downloads: list[ItemDownloadPjeDocs]
    parcial: bool
    aviso: str


class ArquivoPjeDocsBaixado(BaseModel):
    numero: str
    grau: Grau
    referencia_resultado: str = Field(min_length=1)
    nome_arquivo: str
    caminho: str
    caminho_sha256: str
    tipo_mime: str
    tamanho_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    obtido_em: datetime = Field(default_factory=lambda: datetime.now(UTC))
    aviso: str


class EstadoChatCap1g(StrEnum):
    # Espelha Thread.STATE_* do cliente Mibew; o valor numérico fica no adaptador.
    NA_FILA = "na_fila"
    AGUARDANDO_OPERADOR = "aguardando_operador"
    EM_ATENDIMENTO = "em_atendimento"
    ENCERRADO = "encerrado"
    CARREGANDO = "carregando"
    ABANDONADO = "abandonado"
    CONVIDADO = "convidado"
    INDETERMINADO = "indeterminado"


class TipoMensagemChat(StrEnum):
    # Espelha Message.KIND_* do cliente Mibew.
    VISITANTE = "visitante"
    OPERADOR = "operador"
    OCULTA = "oculta"
    INFO = "info"
    CONEXAO = "conexao"
    EVENTO = "evento"
    PLUGIN = "plugin"


class MensagemChatCap1g(BaseModel):
    id: int = Field(ge=0)
    tipo: TipoMensagemChat
    autor: str | None = None
    texto: str
    enviada_em: datetime


class DisponibilidadeChatCap1g(BaseModel):
    disponivel: bool
    # O valor de startFrom que o Mibew embute na página: survey, leaveMessage, chat…
    modo_inicial: str
    grupo: str | None = None
    horario_atendimento: str = "8h às 19h em dias úteis"
    telefone: str = "(81) 3181-0506"
    # Só olha o relógio (dia útil e faixa horária); feriados forenses ficam de fora.
    dentro_do_horario: bool
    # Cada atendimento aceita encaminhamento de até N processos; para mais, encerra-se
    # e abre-se outro chat.
    limite_processos_por_atendimento: int = Field(ge=1)
    url: str
    portal: str
    mensagem: str
    verificado_em: datetime = Field(default_factory=lambda: datetime.now(UTC))
    fonte: str = "Chat Mibew da CAP1G/TJPE"
    aviso: str


class PreparacaoChatCap1g(BaseModel):
    referencia_preparo: str = Field(min_length=1)
    nome: str
    email: str
    mensagem_inicial: str
    # NPUs distintos citados na mensagem inicial; já contam para o limite do chat.
    processos_na_mensagem: list[str] = []
    limite_processos_por_atendimento: int = Field(ge=1)
    disponivel: bool
    expira_em: datetime
    frase_confirmacao: str = Field(min_length=1)
    aviso: str


class SessaoChatCap1g(BaseModel):
    referencia_chat: str = Field(min_length=1)
    estado: EstadoChatCap1g
    pode_enviar: bool
    operador: str | None = None
    operador_digitando: bool = False
    aviso_do_chat: str | None = None
    nome_visitante: str
    mensagens: list[MensagemChatCap1g]
    total_mensagens: int = Field(ge=0)
    ultimo_id: int = Field(ge=0)
    # Quantas das mensagens devolvidas chegaram depois do marco desta chamada.
    novas_mensagens: int = Field(ge=0)
    # NPUs distintos que o visitante citou neste atendimento (pela ferramenta ou
    # digitando na janela), e quantos ainda cabem antes de precisar de outro chat.
    processos_solicitados: list[str] = []
    processos_restantes: int = Field(ge=0)
    limite_processos_por_atendimento: int = Field(ge=1)
    encerrado: bool
    iniciado_em: datetime
    atualizado_em: datetime = Field(default_factory=lambda: datetime.now(UTC))
    transcricao: str | None = None
    fonte: str = "Chat Mibew da CAP1G/TJPE"
    aviso: str


class EncerramentoChatCap1g(BaseModel):
    referencia_chat: str = Field(min_length=1)
    estado_final: EstadoChatCap1g
    encerrado_por: Literal["visitante", "operador", "indeterminado"]
    mensagens: list[MensagemChatCap1g]
    total_mensagens: int = Field(ge=0)
    processos_solicitados: list[str] = []
    transcricao: str
    caminho_sha256: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    iniciado_em: datetime
    encerrado_em: datetime = Field(default_factory=lambda: datetime.now(UTC))
    aviso: str
