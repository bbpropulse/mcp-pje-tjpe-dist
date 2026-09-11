from __future__ import annotations

import re
from urllib.parse import urlparse

import pytest
from pydantic import ValidationError

from mcp_pje_tjpe.errors import ValidacaoError
from mcp_pje_tjpe.tribunals import (
    CapacidadesTribunal,
    InstanciaPje,
    MaturidadeAdaptador,
    PoliticaAcessoTerceiros,
    TribunalCodigo,
    listar_perfis_tribunais,
    obter_instancia,
    obter_perfil_tribunal,
    validar_npu_instancia,
)


def _synthetic_npu(segmento: str) -> str:
    justica, tribunal = segmento.split(".")
    sequencial = "9999999"
    ano = "2099"
    origem = "9999"
    base = sequencial + ano + justica + tribunal + origem + "00"
    digito = 98 - (int(base) % 97)
    return f"{sequencial}-{digito:02d}.{ano}.{justica}.{tribunal}.{origem}"


def _replace_check_digit(numero: str) -> str:
    digits = re.sub(r"\D", "", numero)
    wrong = (int(digits[7:9]) % 98) + 1
    return f"{digits[:7]}-{wrong:02d}.{digits[9:13]}.{digits[13]}.{digits[14:16]}.{digits[16:]}"


def test_catalog_has_the_three_declared_tribunals_in_stable_order() -> None:
    profiles = listar_perfis_tribunais()

    assert [profile.codigo for profile in profiles] == [
        TribunalCodigo.TJPE,
        TribunalCodigo.TRT6,
        TribunalCodigo.TRF5,
    ]
    assert len({instance.codigo for profile in profiles for instance in profile.instancias}) == 7


def test_only_tjpe_advertises_operational_authenticated_capabilities() -> None:
    tjpe = obter_perfil_tribunal(TribunalCodigo.TJPE)
    assert tjpe.maturidade is MaturidadeAdaptador.OPERACIONAL
    assert tjpe.capacidades == CapacidadesTribunal(
        consulta_publica=True,
        metadados_datajud=True,
        login_assistido=True,
        acervo_autenticado=True,
        pesquisa_geral_autenticada=True,
        autos_digitais=True,
        documentos_acervo=True,
        pjedocs=True,
    )

    for codigo in (TribunalCodigo.TRT6, TribunalCodigo.TRF5):
        profile = obter_perfil_tribunal(codigo)
        assert profile.maturidade is MaturidadeAdaptador.DESCOBERTA
        # O DataJud é público e sem credenciamento: um adaptador em descoberta pode
        # anunciá-lo. O que continua vedado é qualquer capacidade autenticada.
        assert profile.capacidades == CapacidadesTribunal(metadados_datajud=True)
        assert not any(
            (
                profile.capacidades.login_assistido,
                profile.capacidades.acervo_autenticado,
                profile.capacidades.pesquisa_geral_autenticada,
                profile.capacidades.autos_digitais,
                profile.capacidades.documentos_acervo,
                profile.capacidades.pjedocs,
            )
        )


@pytest.mark.parametrize(
    (
        "tribunal",
        "instancia",
        "entrada",
        "login",
        "consulta_publica",
        "host",
        "segmento",
    ),
    [
        (
            TribunalCodigo.TJPE,
            "tjpe_1g",
            "https://pje.cloud.tjpe.jus.br/1g",
            "https://pje.cloud.tjpe.jus.br/1g/login.seam",
            "https://pje.cloud.tjpe.jus.br/1g/ConsultaPublica/listView.seam",
            "pje.cloud.tjpe.jus.br",
            "8.17",
        ),
        (
            TribunalCodigo.TJPE,
            "tjpe_2g",
            "https://pje.cloud.tjpe.jus.br/2g",
            "https://pje.cloud.tjpe.jus.br/2g/login.seam",
            "https://pje.cloud.tjpe.jus.br/2g/ConsultaPublica/listView.seam",
            "pje.cloud.tjpe.jus.br",
            "8.17",
        ),
        (
            TribunalCodigo.TRT6,
            "trt6_1g",
            "https://pje.trt6.jus.br/primeirograu",
            "https://pje.trt6.jus.br/primeirograu/login.seam",
            "https://pje.trt6.jus.br/consultaprocessual",
            "pje.trt6.jus.br",
            "5.06",
        ),
        (
            TribunalCodigo.TRT6,
            "trt6_2g",
            "https://pje.trt6.jus.br/segundograu",
            "https://pje.trt6.jus.br/segundograu/login.seam",
            "https://pje.trt6.jus.br/consultaprocessual",
            "pje.trt6.jus.br",
            "5.06",
        ),
        (
            TribunalCodigo.TRF5,
            "trf5_sjpe_1g",
            "https://pje1g.trf5.jus.br/pje",
            "https://pje1g.trf5.jus.br/pje/login.seam",
            "https://pje1g.trf5.jus.br/pjeconsulta/ConsultaPublica/listView.seam",
            "pje1g.trf5.jus.br",
            "4.05",
        ),
        (
            TribunalCodigo.TRF5,
            "trf5_2g",
            "https://pjett.trf5.jus.br/pje",
            "https://pjett.trf5.jus.br/pje/login.seam",
            "https://pjett.trf5.jus.br/pjeconsulta/ConsultaPublica/listView.seam",
            "pjett.trf5.jus.br",
            "4.05",
        ),
        (
            TribunalCodigo.TRF5,
            "trf5_turmas_recursais",
            "https://pje2g.trf5.jus.br/pje",
            "https://pje2g.trf5.jus.br/pje/login.seam",
            "https://pje2g.trf5.jus.br/pjeconsulta/ConsultaPublica/listView.seam",
            "pje2g.trf5.jus.br",
            "4.05",
        ),
    ],
)
def test_instance_catalog_binds_exact_urls_hosts_and_npu_segments(
    tribunal: TribunalCodigo,
    instancia: str,
    entrada: str,
    login: str,
    consulta_publica: str,
    host: str,
    segmento: str,
) -> None:
    selected = obter_instancia(tribunal, instancia)

    assert selected.entrada_url == entrada
    assert selected.login_url == login
    assert selected.consulta_publica_url == consulta_publica
    assert selected.hosts_permitidos == (host,)
    assert selected.segmento_npu == segmento

    for value in (selected.entrada_url, selected.login_url, selected.consulta_publica_url):
        parsed = urlparse(value)
        assert parsed.scheme == "https"
        assert parsed.hostname == host
        assert parsed.username is None
        assert parsed.password is None
        assert parsed.port in {None, 443}
        assert parsed.fragment == ""


def test_trf5_first_degree_requires_explicit_sjpe_jurisdiction_validation() -> None:
    first_degree = obter_instancia(TribunalCodigo.TRF5, "trf5_sjpe_1g")
    regional = obter_instancia(TribunalCodigo.TRF5, "trf5_2g")

    assert first_degree.exige_validacao_jurisdicao is not None
    assert "SJPE/Pernambuco" in first_degree.exige_validacao_jurisdicao
    assert regional.exige_validacao_jurisdicao is None


def test_trt6_third_party_access_policy_is_conservative_and_sourced() -> None:
    policy = obter_perfil_tribunal(TribunalCodigo.TRT6).politica_acesso_terceiros

    assert policy.limite_oficial == 1_500
    assert policy.janela_dias == 30
    assert policy.limite_local_recomendado == 1_000
    assert policy.limite_local_recomendado < policy.limite_oficial
    assert policy.fonte_oficial == (
        "https://www.trt6.jus.br/portal/noticias/2026/03/20/"
        "trt-6-bloqueara-usuariosas-do-pje-jt-que-realizem-consultas-excessivas-processos"
    )
    assert "contador local compartilhado" in policy.aviso

    for codigo in (TribunalCodigo.TJPE, TribunalCodigo.TRF5):
        other = obter_perfil_tribunal(codigo).politica_acesso_terceiros
        assert other.limite_oficial is None
        assert other.janela_dias is None
        assert other.limite_local_recomendado is None


@pytest.mark.parametrize(
    ("tribunal", "instancia"),
    [
        (TribunalCodigo.TJPE, "tjpe_1g"),
        (TribunalCodigo.TJPE, "tjpe_2g"),
        (TribunalCodigo.TRT6, "trt6_1g"),
        (TribunalCodigo.TRT6, "trt6_2g"),
        (TribunalCodigo.TRF5, "trf5_sjpe_1g"),
        (TribunalCodigo.TRF5, "trf5_2g"),
        (TribunalCodigo.TRF5, "trf5_turmas_recursais"),
    ],
)
def test_npu_validation_accepts_only_the_instance_segment_and_valid_mod97(
    tribunal: TribunalCodigo,
    instancia: str,
) -> None:
    segment = obter_instancia(tribunal, instancia).segmento_npu
    numero = _synthetic_npu(segment)

    jurisdiction = instancia == "trf5_sjpe_1g"
    assert (
        validar_npu_instancia(
            numero,
            tribunal,
            instancia,
            jurisdicao_confirmada=jurisdiction,
        )
        == numero
    )
    assert (
        validar_npu_instancia(
            re.sub(r"\D", "", numero),
            tribunal,
            instancia,
            jurisdicao_confirmada=jurisdiction,
        )
        == numero
    )


def test_trf5_first_degree_npu_requires_authenticated_jurisdiction_evidence() -> None:
    numero = _synthetic_npu("4.05")

    with pytest.raises(ValidacaoError, match="confirmação autenticada"):
        validar_npu_instancia(numero, TribunalCodigo.TRF5, "trf5_sjpe_1g")


@pytest.mark.parametrize(
    ("tribunal", "instancia", "wrong_segment"),
    [
        (TribunalCodigo.TJPE, "tjpe_1g", "5.06"),
        (TribunalCodigo.TRT6, "trt6_1g", "4.05"),
        (TribunalCodigo.TRF5, "trf5_sjpe_1g", "8.17"),
    ],
)
def test_npu_validation_rejects_a_valid_number_from_another_justice_segment(
    tribunal: TribunalCodigo,
    instancia: str,
    wrong_segment: str,
) -> None:
    with pytest.raises(ValidacaoError, match="segmento de Justiça/tribunal"):
        validar_npu_instancia(_synthetic_npu(wrong_segment), tribunal, instancia)


@pytest.mark.parametrize(
    ("tribunal", "instancia"),
    [
        (TribunalCodigo.TJPE, "tjpe_2g"),
        (TribunalCodigo.TRT6, "trt6_2g"),
        (TribunalCodigo.TRF5, "trf5_2g"),
    ],
)
def test_npu_validation_rejects_wrong_check_digit(
    tribunal: TribunalCodigo,
    instancia: str,
) -> None:
    numero = _synthetic_npu(obter_instancia(tribunal, instancia).segmento_npu)

    with pytest.raises(ValidacaoError, match="dígito verificador"):
        validar_npu_instancia(_replace_check_digit(numero), tribunal, instancia)


@pytest.mark.parametrize(
    ("numero", "message"),
    [
        ("123", "somente os algarismos"),
        ("123456789012345678901", "20 algarismos"),
        ("processo 9999999-00.2099.8.17.9999", "somente os algarismos"),
        ("9999999/00/2099/8/17/9999", "somente os algarismos"),
        ("9999999-00.2099.8.17.9999\n", "somente os algarismos"),
    ],
)
def test_npu_validation_rejects_malformed_or_decorated_input(numero: str, message: str) -> None:
    with pytest.raises(ValidacaoError, match=message):
        validar_npu_instancia(numero, TribunalCodigo.TJPE, "tjpe_1g")


def test_npu_validation_rejects_an_unknown_instance() -> None:
    with pytest.raises(ValidacaoError, match="instância desconhecida"):
        validar_npu_instancia(_synthetic_npu("8.17"), TribunalCodigo.TJPE, "tjpe_3g")


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "http://pje.example.jus.br/pje",
        "https://usuario@pje.example.jus.br/pje",
        "https://pje.example.jus.br:444/pje",
        "https://pje.example.jus.br/pje?state=segredo",
        "https://pje.example.jus.br/pje#login",
        "https://evil.example/pje",
    ],
)
def test_instance_model_rejects_unsafe_or_out_of_allowlist_urls(unsafe_url: str) -> None:
    with pytest.raises(ValidationError):
        InstanciaPje(
            codigo="teste_1g",
            nome="Instância de teste",
            grau_juridico="1º grau",
            segmento_npu="8.17",
            entrada_url=unsafe_url,
            login_url="https://pje.example.jus.br/pje/login.seam",
            consulta_publica_url="https://pje.example.jus.br/consulta",
            hosts_permitidos=("pje.example.jus.br",),
        )


def test_instance_model_rejects_duplicate_or_malformed_hosts() -> None:
    for hosts in (
        ("pje.example.jus.br", "pje.example.jus.br"),
        ("pje..example.jus.br",),
        ("https://pje.example.jus.br",),
    ):
        with pytest.raises(ValidationError):
            InstanciaPje(
                codigo="teste_1g",
                nome="Instância de teste",
                grau_juridico="1º grau",
                segmento_npu="8.17",
                entrada_url="https://pje.example.jus.br/pje",
                login_url="https://pje.example.jus.br/pje/login.seam",
                consulta_publica_url="https://pje.example.jus.br/consulta",
                hosts_permitidos=hosts,
            )


def test_access_policy_model_rejects_incomplete_or_nonconservative_limits() -> None:
    with pytest.raises(ValidationError, match="devem ser informados juntos"):
        PoliticaAcessoTerceiros(limite_oficial=1_500, aviso="teste")

    with pytest.raises(ValidationError, match="menor que o oficial"):
        PoliticaAcessoTerceiros(
            limite_oficial=1_500,
            janela_dias=30,
            limite_local_recomendado=1_500,
            aviso="teste",
        )
