from __future__ import annotations

import re
from enum import StrEnum
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mcp_pje_tjpe.errors import ValidacaoError


class TribunalCodigo(StrEnum):
    TJPE = "tjpe"
    TRT6 = "trt6"
    TRF5 = "trf5"


class MaturidadeAdaptador(StrEnum):
    OPERACIONAL = "operacional"
    DESCOBERTA = "descoberta"


class CapacidadesTribunal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    catalogo_ambientes: bool = True
    consulta_publica: bool = False
    # Metadados públicos do DataJud/CNJ: não exige login, sessão ou credenciamento,
    # por isso um adaptador em descoberta pode anunciá-la legitimamente.
    metadados_datajud: bool = False
    login_assistido: bool = False
    acervo_autenticado: bool = False
    pesquisa_geral_autenticada: bool = False
    autos_digitais: bool = False
    documentos_acervo: bool = False
    pjedocs: bool = False


class PoliticaAcessoTerceiros(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    limite_oficial: int | None = Field(default=None, ge=1)
    janela_dias: int | None = Field(default=None, ge=1)
    limite_local_recomendado: int | None = Field(default=None, ge=1)
    fonte_oficial: str | None = None
    aviso: str

    @model_validator(mode="after")
    def validate_limit_shape(self) -> PoliticaAcessoTerceiros:
        present = (self.limite_oficial is not None, self.janela_dias is not None)
        if present[0] != present[1]:
            raise ValueError("limite_oficial e janela_dias devem ser informados juntos")
        if (
            self.limite_oficial is not None
            and self.limite_local_recomendado is not None
            and self.limite_local_recomendado >= self.limite_oficial
        ):
            raise ValueError("o limite local deve ser conservador e menor que o oficial")
        return self


class InstanciaPje(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    codigo: str = Field(pattern=r"^[a-z0-9_]{2,40}$")
    nome: str = Field(min_length=3, max_length=160)
    grau_juridico: str = Field(min_length=2, max_length=80)
    segmento_npu: str = Field(pattern=r"^[1-9]\.[0-9]{2}$")
    entrada_url: str
    login_url: str
    consulta_publica_url: str
    hosts_permitidos: tuple[str, ...] = Field(min_length=1, max_length=4)
    exige_validacao_jurisdicao: str | None = Field(default=None, max_length=300)
    prioridade: int = Field(default=1, ge=1, le=9)

    @field_validator("entrada_url", "login_url", "consulta_publica_url")
    @classmethod
    def validate_https_url(cls, value: str) -> str:
        parsed = urlparse(value)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("URL possui porta inválida") from exc
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("somente URL HTTPS oficial sem credenciais ou fragmento é aceita")
        return value.rstrip("/") if parsed.path not in {"", "/"} else value

    @field_validator("hosts_permitidos")
    @classmethod
    def validate_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or any(
            re.fullmatch(r"[a-z0-9.-]{4,253}", host) is None or ".." in host for host in value
        ):
            raise ValueError("hosts_permitidos contém host inválido ou duplicado")
        return value

    @model_validator(mode="after")
    def validate_url_hosts(self) -> InstanciaPje:
        allowed = set(self.hosts_permitidos)
        for value in (self.entrada_url, self.login_url, self.consulta_publica_url):
            if urlparse(value).hostname not in allowed:
                raise ValueError("URL da instância está fora dos hosts permitidos")
        return self


class PerfilTribunal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    codigo: TribunalCodigo
    nome: str = Field(min_length=4, max_length=160)
    ramo: str = Field(min_length=4, max_length=80)
    maturidade: MaturidadeAdaptador
    sistema: str = "PJe"
    autenticacao: str = Field(min_length=3, max_length=200)
    instancias: tuple[InstanciaPje, ...] = Field(min_length=1, max_length=6)
    capacidades: CapacidadesTribunal
    politica_acesso_terceiros: PoliticaAcessoTerceiros
    fontes_oficiais: tuple[str, ...] = Field(min_length=1, max_length=12)
    avisos: tuple[str, ...] = Field(default=(), max_length=12)

    @model_validator(mode="after")
    def validate_unique_instances(self) -> PerfilTribunal:
        codes = [instance.codigo for instance in self.instancias]
        if len(set(codes)) != len(codes):
            raise ValueError("o perfil contém códigos de instância duplicados")
        if self.maturidade is MaturidadeAdaptador.DESCOBERTA and any(
            (
                self.capacidades.login_assistido,
                self.capacidades.acervo_autenticado,
                self.capacidades.pesquisa_geral_autenticada,
                self.capacidades.autos_digitais,
                self.capacidades.documentos_acervo,
                self.capacidades.pjedocs,
            )
        ):
            raise ValueError("adaptador em descoberta não pode anunciar capacidade autenticada")
        return self


_RESOLUTION_121 = (
    "A abertura de processo de terceiro pode registrar interesse conforme a Resolução CNJ 121; "
    "sigilo, permissão e ciência nunca são contornados."
)


_PROFILES: dict[TribunalCodigo, PerfilTribunal] = {
    TribunalCodigo.TJPE: PerfilTribunal(
        codigo=TribunalCodigo.TJPE,
        nome="Tribunal de Justiça de Pernambuco",
        ramo="Justiça Estadual",
        maturidade=MaturidadeAdaptador.OPERACIONAL,
        autenticacao="SSO PJe com certificado/PJeOffice e MFA assistidos pelo usuário",
        instancias=(
            InstanciaPje(
                codigo="tjpe_1g",
                nome="TJPE — 1º grau",
                grau_juridico="1º grau",
                segmento_npu="8.17",
                entrada_url="https://pje.cloud.tjpe.jus.br/1g",
                login_url="https://pje.cloud.tjpe.jus.br/1g/login.seam",
                consulta_publica_url=(
                    "https://pje.cloud.tjpe.jus.br/1g/ConsultaPublica/listView.seam"
                ),
                hosts_permitidos=("pje.cloud.tjpe.jus.br",),
            ),
            InstanciaPje(
                codigo="tjpe_2g",
                nome="TJPE — 2º grau",
                grau_juridico="2º grau",
                segmento_npu="8.17",
                entrada_url="https://pje.cloud.tjpe.jus.br/2g",
                login_url="https://pje.cloud.tjpe.jus.br/2g/login.seam",
                consulta_publica_url=(
                    "https://pje.cloud.tjpe.jus.br/2g/ConsultaPublica/listView.seam"
                ),
                hosts_permitidos=("pje.cloud.tjpe.jus.br",),
            ),
        ),
        capacidades=CapacidadesTribunal(
            consulta_publica=True,
            metadados_datajud=True,
            login_assistido=True,
            acervo_autenticado=True,
            pesquisa_geral_autenticada=True,
            autos_digitais=True,
            documentos_acervo=True,
            pjedocs=True,
        ),
        politica_acesso_terceiros=PoliticaAcessoTerceiros(aviso=_RESOLUTION_121),
        fontes_oficiais=(
            "https://portal.tjpe.jus.br/web/processo-judicial-eletronico",
            "https://docs.pje.jus.br/manuais-de-uso/Manual%20do%20advogado/",
        ),
    ),
    TribunalCodigo.TRT6: PerfilTribunal(
        codigo=TribunalCodigo.TRT6,
        nome="Tribunal Regional do Trabalho da 6ª Região",
        ramo="Justiça do Trabalho",
        maturidade=MaturidadeAdaptador.DESCOBERTA,
        autenticacao="PDPJ-Br com PJeOffice Pro, certificado e MFA assistidos pelo usuário",
        instancias=(
            InstanciaPje(
                codigo="trt6_1g",
                nome="TRT6 — 1º grau",
                grau_juridico="1º grau",
                segmento_npu="5.06",
                entrada_url="https://pje.trt6.jus.br/primeirograu/",
                login_url="https://pje.trt6.jus.br/primeirograu/login.seam",
                consulta_publica_url="https://pje.trt6.jus.br/consultaprocessual/",
                hosts_permitidos=("pje.trt6.jus.br",),
            ),
            InstanciaPje(
                codigo="trt6_2g",
                nome="TRT6 — 2º grau",
                grau_juridico="2º grau",
                segmento_npu="5.06",
                entrada_url="https://pje.trt6.jus.br/segundograu/",
                login_url="https://pje.trt6.jus.br/segundograu/login.seam",
                consulta_publica_url="https://pje.trt6.jus.br/consultaprocessual/",
                hosts_permitidos=("pje.trt6.jus.br",),
            ),
        ),
        capacidades=CapacidadesTribunal(consulta_publica=False, metadados_datajud=True),
        politica_acesso_terceiros=PoliticaAcessoTerceiros(
            limite_oficial=1_500,
            janela_dias=30,
            limite_local_recomendado=1_000,
            fonte_oficial=(
                "https://www.trt6.jus.br/portal/noticias/2026/03/20/"
                "trt-6-bloqueara-usuariosas-do-pje-jt-que-realizem-consultas-excessivas-processos"
            ),
            aviso=(
                "O TRT6 contabiliza aberturas de processos de terceiros em janela móvel; "
                "o futuro adaptador deverá usar contador local compartilhado e conservador."
            ),
        ),
        fontes_oficiais=(
            "https://www.trt6.jus.br/portal/pje",
            "https://www.trt6.jus.br/portal/sites/default/files/documents/"
            "manual_de_acesso_ao_pje_via_pdpj-br_-_usuario_externo_1.pdf",
        ),
        avisos=(
            "O painel KZ e o SSO PDPJ exigem homologação autenticada antes de liberar buscas.",
        ),
    ),
    TribunalCodigo.TRF5: PerfilTribunal(
        codigo=TribunalCodigo.TRF5,
        nome="Tribunal Regional Federal da 5ª Região / Justiça Federal em Pernambuco",
        ramo="Justiça Federal",
        maturidade=MaturidadeAdaptador.DESCOBERTA,
        autenticacao="SSO PJe/OIDC com certificado, PJeOffice Pro e MFA assistidos pelo usuário",
        instancias=(
            InstanciaPje(
                codigo="trf5_sjpe_1g",
                nome="JFPE/SJPE — Varas e JEF de 1º grau",
                grau_juridico="1º grau federal em Pernambuco",
                segmento_npu="4.05",
                entrada_url="https://pje1g.trf5.jus.br/pje",
                login_url="https://pje1g.trf5.jus.br/pje/login.seam",
                consulta_publica_url=(
                    "https://pje1g.trf5.jus.br/pjeconsulta/ConsultaPublica/listView.seam"
                ),
                hosts_permitidos=("pje1g.trf5.jus.br",),
                exige_validacao_jurisdicao=(
                    "O host atende toda a 5ª Região; exigir evidência de "
                    "SJPE/Pernambuco após login."
                ),
            ),
            InstanciaPje(
                codigo="trf5_2g",
                nome="TRF5 — 2º grau e TRU",
                grau_juridico="2º grau federal",
                segmento_npu="4.05",
                entrada_url="https://pjett.trf5.jus.br/pje",
                login_url="https://pjett.trf5.jus.br/pje/login.seam",
                consulta_publica_url=(
                    "https://pjett.trf5.jus.br/pjeconsulta/ConsultaPublica/listView.seam"
                ),
                hosts_permitidos=("pjett.trf5.jus.br",),
            ),
            InstanciaPje(
                codigo="trf5_turmas_recursais",
                nome="TRF5 — Turmas Recursais",
                grau_juridico="Turmas Recursais",
                segmento_npu="4.05",
                entrada_url="https://pje2g.trf5.jus.br/pje",
                login_url="https://pje2g.trf5.jus.br/pje/login.seam",
                consulta_publica_url=(
                    "https://pje2g.trf5.jus.br/pjeconsulta/ConsultaPublica/listView.seam"
                ),
                hosts_permitidos=("pje2g.trf5.jus.br",),
                prioridade=2,
            ),
        ),
        capacidades=CapacidadesTribunal(consulta_publica=False, metadados_datajud=True),
        politica_acesso_terceiros=PoliticaAcessoTerceiros(aviso=_RESOLUTION_121),
        fontes_oficiais=(
            "https://www.trf5.jus.br/index.php/pje?action=acesso-pje",
            "https://www.jfpe.jus.br/",
            "https://docs.pje.jus.br/servicos-negociais/servico-sso-pje-kc/",
        ),
        avisos=(
            "PJe 2.x e legado 1.x não podem compartilhar sessão ou adaptador.",
            "O hostname interno do SSO não substitui a validação do grau jurídico.",
        ),
    ),
}


def listar_perfis_tribunais() -> list[PerfilTribunal]:
    return [_PROFILES[code] for code in TribunalCodigo]


def obter_perfil_tribunal(codigo: TribunalCodigo) -> PerfilTribunal:
    return _PROFILES[codigo]


def obter_instancia(codigo: TribunalCodigo, instancia: str) -> InstanciaPje:
    profile = obter_perfil_tribunal(codigo)
    for candidate in profile.instancias:
        if candidate.codigo == instancia:
            return candidate
    raise ValidacaoError("instância desconhecida para o tribunal informado")


def validar_npu_tribunal(codigo: TribunalCodigo, numero: str) -> str:
    """Valida o NPU contra os segmentos do tribunal, sem exigir instância nem sessão.

    Usada por consultas que não navegam nem autenticam — o grau vem na resposta do
    serviço, não do chamador. Não aplica o gate de jurisdição das instâncias, que
    trata de navegação autenticada em host regional.
    """
    if re.fullmatch(r"[0-9. \-]{20,32}", numero) is None:
        raise ValidacaoError("informe somente os algarismos e separadores usuais do NPU")
    digits = re.sub(r"\D", "", numero)
    if len(digits) != 20:
        raise ValidacaoError("informe um NPU completo com 20 algarismos")
    perfil = obter_perfil_tribunal(codigo)
    aceitos = {instancia.segmento_npu.replace(".", "") for instancia in perfil.instancias}
    if digits[13:16] not in aceitos:
        esperados = ", ".join(sorted(f"{item[0]}.{item[1:]}" for item in aceitos))
        raise ValidacaoError(
            f"o segmento de Justiça/tribunal do NPU não corresponde a {perfil.nome} "
            f"(esperado {esperados})"
        )
    base = digits[:7] + digits[9:] + digits[7:9]
    if int(base) % 97 != 1:
        raise ValidacaoError("o NPU possui dígito verificador inválido")
    return f"{digits[:7]}-{digits[7:9]}.{digits[9:13]}.{digits[13]}.{digits[14:16]}.{digits[16:20]}"


def validar_npu_instancia(
    numero: str,
    codigo: TribunalCodigo,
    instancia: str,
    *,
    jurisdicao_confirmada: bool = False,
) -> str:
    if re.fullmatch(r"[0-9. \-]{20,32}", numero) is None:
        raise ValidacaoError("informe somente os algarismos e separadores usuais do NPU")
    digits = re.sub(r"\D", "", numero)
    if len(digits) != 20:
        raise ValidacaoError("informe um NPU completo com 20 algarismos")
    selected = obter_instancia(codigo, instancia)
    expected = selected.segmento_npu.replace(".", "")
    if digits[13:16] != expected:
        raise ValidacaoError("o segmento de Justiça/tribunal do NPU não corresponde à instância")
    base = digits[:7] + digits[9:] + digits[7:9]
    if int(base) % 97 != 1:
        raise ValidacaoError("o NPU possui dígito verificador inválido")
    if selected.exige_validacao_jurisdicao is not None and not jurisdicao_confirmada:
        raise ValidacaoError(
            "o NPU regional exige confirmação autenticada da jurisdição SJPE/Pernambuco"
        )
    return f"{digits[:7]}-{digits[7:9]}.{digits[9:13]}.{digits[13]}.{digits[14:16]}.{digits[16:20]}"
