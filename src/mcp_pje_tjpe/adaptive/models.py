from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from mcp_pje_tjpe.config import ModoAdaptativo
from mcp_pje_tjpe.tribunals import TribunalCodigo

_STATIC_PATH_COMPONENT = (
    r"(?:consulta-terceiros|consultaprocesso|consultapublica|detalhe|"
    r"listautosdigitais\.seam|listview\.seam|login\.seam|pje|pjeconsulta|pjekz|"
    r"primeirograu|processo|segundograu)"
)
_PATH_COMPONENT = rf"(?:{_STATIC_PATH_COMPONENT}|:(?:dynamic|segment))"
_PATH_SHAPE = rf"^/(?:{_PATH_COMPONENT}(?:/{_PATH_COMPONENT}){{0,31}})?$"
TokenSemantico = Annotated[str, Field(pattern=r"^[a-z]{3,30}$")]
CodigoFalha = Literal["unique_control_not_found", "npu_field_shape_changed"]


class ControleEstrutural(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["input", "button", "select", "textarea", "a", "div", "span"]
    role: Literal["button", "textbox", "searchbox", "combobox", "link"] | None = None
    tipo: Literal[
        "text",
        "search",
        "tel",
        "email",
        "number",
        "date",
        "submit",
        "button",
        "select-one",
        "textarea",
        "unknown",
    ] = "unknown"
    visivel: bool
    habilitado: bool
    editavel: bool | None = None
    submit_nativo: bool = False
    possui_formulario: bool = False
    tokens_semanticos: tuple[TokenSemantico, ...] = Field(default=(), max_length=16)
    tokens_desconhecidos: int = Field(default=0, ge=0, le=100)
    assinatura_identificador: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class AvaliacaoEstrategia(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    estrategia_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,79}$")
    correspondencias: int = Field(ge=0, le=100)
    quantidade_aceita: bool
    aprovada_para_active: bool


class ObservacaoNavegacao(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    referencia: str = Field(pattern=r"^[A-Za-z0-9_-]{20,80}$")
    ocorrida_em: datetime = Field(default_factory=lambda: datetime.now(UTC))
    tribunal: TribunalCodigo
    instancia: str = Field(pattern=r"^[a-z0-9_]{2,40}$")
    sistema: Literal["pje"] = "pje"
    fluxo: str = Field(pattern=r"^[a-z0-9_]{3,80}$")
    etapa: str = Field(pattern=r"^[a-z0-9_]{3,80}$")
    modo: ModoAdaptativo
    origem: str = Field(pattern=r"^https://[a-z0-9.-]{4,253}$")
    formato_caminho: str = Field(min_length=1, max_length=300, pattern=_PATH_SHAPE)
    assinatura_pagina: str = Field(pattern=r"^[0-9a-f]{64}$")
    codigo_falha: CodigoFalha
    estrategias: tuple[AvaliacaoEstrategia, ...] = Field(default=(), max_length=32)
    controles: tuple[ControleEstrutural, ...] = Field(default=(), max_length=100)
    limite_efeito_ultrapassado: Literal[False] = False
    dados_sensiveis_persistidos: Literal[False] = False


class StatusNavegacaoAdaptativa(BaseModel):
    modo: ModoAdaptativo
    persistencia: Literal["jsonl_local"] = "jsonl_local"
    schema_version: Literal[1] = 1
    observacoes: int = Field(ge=0)
    limite_observacoes: int = Field(ge=1)
    adaptadores_carregados: int = Field(ge=0)
    adaptadores_active: int = Field(ge=0)
    ultimo_erro_local: str | None = None
    aviso: str


class ResultadoReplayAdaptador(BaseModel):
    referencia_observacao: str
    adapter_id: str
    etapa: str
    estrategias: tuple[AvaliacaoEstrategia, ...]
    candidato_unico: bool
    executou_navegacao: Literal[False] = False


class ValidacaoAdaptadoresOffline(BaseModel):
    observacoes_avaliadas: int = Field(ge=0)
    resultados: list[ResultadoReplayAdaptador]
    rede_utilizada: Literal[False] = False
    aviso: str
