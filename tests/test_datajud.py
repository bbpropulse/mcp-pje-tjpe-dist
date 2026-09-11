from __future__ import annotations

# pyright: reportPrivateUsage=false
import json
from typing import Any

import pytest

import mcp_pje_tjpe.datajud as datajud
from mcp_pje_tjpe.config import Settings
from mcp_pje_tjpe.datajud import (
    HOST,
    DatajudClient,
    _index_for,
    _normalize_datetime,
    _parse_movimentos,
    _post_search,
    _to_model,
)
from mcp_pje_tjpe.errors import ServicoIndisponivelError, ValidacaoError
from mcp_pje_tjpe.tribunals import TribunalCodigo, validar_npu_tribunal

CHAVE = "chave-publica-de-teste"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _npu(segmento: str, sequence: str = "9999999", year: str = "2099") -> str:
    """NPU sintético válido para um segmento J.TR (ex.: '817', '506', '405')."""
    origem = "9999"
    base = sequence + year + segmento + origem + "00"
    digito = 98 - (int(base) % 97)
    return f"{sequence}-{digito:02d}.{year}.{segmento[0]}.{segmento[1:]}.{origem}"


def _source(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "numeroProcesso": "99999990220998179999",
        "tribunal": "TJPE",
        "grau": "G1",
        "nivelSigilo": 0,
        "sistema": {"codigo": 1, "nome": "PJe"},
        "formato": {"codigo": 1, "nome": "Eletrônico"},
        "classe": {"codigo": 156, "nome": "Cumprimento de sentença"},
        "assuntos": [{"codigo": 10439, "nome": "Indenização por Dano Material"}],
        "orgaoJulgador": {"codigo": 18022, "nome": "9ª VARA CÍVEL", "codigoMunicipioIBGE": 2611606},
        "dataAjuizamento": "20170424000000",
        "dataHoraUltimaAtualizacao": "2026-08-13T05:50:44.064000Z",
        "movimentos": [],
    }
    base.update(overrides)
    return base


def _payload(**overrides: object) -> dict[str, object]:
    return {"hits": {"total": {"value": 1}, "hits": [{"_source": _source(**overrides)}]}}


# --------------------------------------------------------------------------- NPU


def test_npu_is_validated_against_the_tribunal_segment_not_only_tjpe() -> None:
    assert validar_npu_tribunal(TribunalCodigo.TJPE, _npu("817")).endswith(".8.17.9999")
    assert validar_npu_tribunal(TribunalCodigo.TRT6, _npu("506")).endswith(".5.06.9999")
    assert validar_npu_tribunal(TribunalCodigo.TRF5, _npu("405")).endswith(".4.05.9999")


def test_npu_from_another_tribunal_is_refused_with_the_expected_segment() -> None:
    with pytest.raises(ValidacaoError, match=r"esperado 5\.06") as reportado:
        validar_npu_tribunal(TribunalCodigo.TRT6, _npu("817"))
    assert "Trabalho" in str(reportado.value)

    with pytest.raises(ValidacaoError, match=r"esperado 8\.17"):
        validar_npu_tribunal(TribunalCodigo.TJPE, _npu("405"))


def test_npu_with_wrong_check_digit_or_shape_is_refused() -> None:
    valido = _npu("817")
    outro_digito = f"{(int(valido[8:10]) + 1) % 100:02d}"
    quebrado = valido[:8] + outro_digito + valido[10:]
    with pytest.raises(ValidacaoError, match="dígito verificador"):
        validar_npu_tribunal(TribunalCodigo.TJPE, quebrado)
    # Comprimento errado dentro do formato aceito: 21 algarismos.
    with pytest.raises(ValidacaoError, match="20 algarismos"):
        validar_npu_tribunal(TribunalCodigo.TJPE, "999999902209981799991")
    # Curto demais ou com caractere estranho para antes, no guarda de formato.
    for entrada in ("123456789012345678", "0000001-11.2020.8.17.0001; DROP"):
        with pytest.raises(ValidacaoError, match="algarismos e separadores"):
            validar_npu_tribunal(TribunalCodigo.TJPE, entrada)


# ------------------------------------------------------------------------ datas


@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        ("20170424000000", "2017-04-24T00:00:00Z"),
        ("2026-08-13T05:50:44.064000Z", "2026-08-13T05:50:44.064000Z"),
        ("20179999000000", "20179999000000"),
        ("", None),
        (None, None),
        (12345, None),
    ],
)
def test_the_two_date_shapes_datajud_uses_are_unified(
    entrada: object, esperado: str | None
) -> None:
    assert _normalize_datetime(entrada) == esperado


# ------------------------------------------------------------------- movimentos


def test_movements_come_back_newest_first_and_truncate_with_the_real_total() -> None:
    bruto = [
        {"codigo": 51, "dataHora": "2026-06-16T10:03:34.000Z", "nome": "Conclusão"},
        {"codigo": 92, "dataHora": "2026-07-09T08:00:00.000Z", "nome": "Publicação"},
        {"codigo": 1051, "dataHora": "2026-07-19T09:00:00.000Z", "nome": "Decurso de Prazo"},
        {"dataHora": "2026-01-01T00:00:00.000Z", "nome": "   "},
        "linha inválida",
    ]

    movimentos, total, truncados = _parse_movimentos(bruto, 2)

    # A API devolve fora de ordem cronológica; o mais recente precisa vir primeiro.
    assert [m.nome for m in movimentos] == ["Decurso de Prazo", "Publicação"]
    assert total == 3
    assert truncados is True
    assert _parse_movimentos(bruto, 10)[2] is False
    assert _parse_movimentos("nada disso", 10) == ([], 0, False)


# ---------------------------------------------------------------------- modelo


def test_public_metadata_is_mapped_without_any_personal_field() -> None:
    modelo = _to_model(_payload(), _npu("817"), TribunalCodigo.TJPE, 100)

    assert modelo.tribunal == "TJPE"
    assert modelo.grau == "G1"
    assert modelo.codigo_classe == 156
    assert modelo.orgao_julgador == "9ª VARA CÍVEL"
    assert modelo.data_ajuizamento == "2017-04-24T00:00:00Z"
    assert modelo.nivel_sigilo == 0
    # O DataJud não publica parte, advogado nem documento — o modelo não inventa campo.
    campos = set(modelo.model_dump())
    assert campos.isdisjoint({"partes", "advogados", "cpf", "cnpj", "documentos", "valor_causa"})


def test_a_process_absent_from_datajud_says_so_instead_of_returning_empty() -> None:
    with pytest.raises(ServicoIndisponivelError, match="não possui registro público"):
        _to_model({"hits": {"hits": []}}, _npu("817"), TribunalCodigo.TJPE, 100)


def test_a_non_public_secrecy_level_is_refused_rather_than_returned() -> None:
    # O DataJud publica só nível 0; qualquer outro significa mudança de política.
    with pytest.raises(ServicoIndisponivelError, match="nível de sigilo 4"):
        _to_model(_payload(nivelSigilo=4), _npu("817"), TribunalCodigo.TJPE, 100)


def test_a_mismatched_process_in_the_answer_is_refused() -> None:
    with pytest.raises(ServicoIndisponivelError, match="processo diferente"):
        _to_model(
            _payload(numeroProcesso="00000000000000000000"),
            _npu("817"),
            TribunalCodigo.TJPE,
            100,
        )


@pytest.mark.parametrize(
    "payload",
    [{}, {"hits": "texto"}, {"hits": {"hits": [{"sem_source": 1}]}}, {"hits": {"hits": ["x"]}}],
)
def test_unexpected_payload_shapes_fail_closed(payload: dict[str, object]) -> None:
    with pytest.raises(ServicoIndisponivelError):
        _to_model(payload, _npu("817"), TribunalCodigo.TJPE, 100)


# ------------------------------------------------------------------------- HTTP


class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self, amount: int | None = None) -> bytes:
        return self._body[:amount] if amount is not None else self._body


class _FakeConnection:
    ultima: _FakeConnection | None = None

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.method: str | None = None
        self.path: str | None = None
        self.body: bytes | None = None
        self.headers: dict[str, str] = {}
        self.closed = False
        self.status = 200
        self.payload = json.dumps(_payload()).encode("utf-8")
        _FakeConnection.ultima = self

    def request(
        self, method: str, path: str, body: bytes | None = None, headers: Any = None
    ) -> None:
        self.method, self.path, self.body = method, path, body
        self.headers = dict(headers or {})

    def getresponse(self) -> _FakeResponse:
        return _FakeResponse(self.status, self.payload)

    def close(self) -> None:
        self.closed = True


def _install(monkeypatch: pytest.MonkeyPatch, *, status: int = 200, body: bytes | None = None):
    criadas: list[_FakeConnection] = []

    def factory(host: str, port: int, timeout: float) -> _FakeConnection:
        conexao = _FakeConnection(host, port, timeout)
        conexao.status = status
        if body is not None:
            conexao.payload = body
        criadas.append(conexao)
        return conexao

    monkeypatch.setattr(datajud.http.client, "HTTPSConnection", factory)
    return criadas


def test_the_request_is_pinned_to_the_cnj_host_and_index_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    criadas = _install(monkeypatch)

    _post_search("api_publica_tjpe", '{"size":1}', api_key=CHAVE, timeout_seconds=5)

    conexao = criadas[0]
    assert (conexao.host, conexao.port) == (HOST, 443)
    assert (conexao.method, conexao.path) == ("POST", "/api_publica_tjpe/_search")
    assert conexao.headers["Authorization"] == f"APIKey {CHAVE}"
    assert conexao.closed is True


@pytest.mark.parametrize("indice", ["../etc/passwd", "api_publica_tjpe/_all", "outra_coisa", ""])
def test_only_a_catalog_shaped_index_is_accepted(
    indice: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    criadas = _install(monkeypatch)
    with pytest.raises(ValidacaoError, match="índice DataJud inválido"):
        _post_search(indice, "{}", api_key=CHAVE, timeout_seconds=5)
    assert criadas == []


def test_the_index_always_comes_from_the_catalog_code() -> None:
    for codigo in TribunalCodigo:
        assert _index_for(codigo) == f"api_publica_{codigo.value}"


@pytest.mark.parametrize("chave", ["", "chave com \r\n quebra", "chave-não-ascii-ç"])
def test_a_malformed_api_key_never_leaves_the_process(
    chave: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    criadas = _install(monkeypatch)
    with pytest.raises(ValidacaoError, match="chave da API DataJud"):
        _post_search("api_publica_tjpe", "{}", api_key=chave, timeout_seconds=5)
    assert criadas == []


@pytest.mark.parametrize(
    ("status", "trecho"),
    [
        (401, "recusou a chave pública"),
        (403, "recusou a chave pública"),
        (429, "limitou a taxa"),
        (302, "redirecionar"),
        (500, "HTTP 500"),
    ],
)
def test_each_failure_status_gets_its_own_explanation(
    status: int, trecho: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, status=status)
    with pytest.raises(ServicoIndisponivelError, match=trecho):
        _post_search("api_publica_tjpe", "{}", api_key=CHAVE, timeout_seconds=5)


def test_a_non_json_answer_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, body=b"<html>manutencao</html>")
    with pytest.raises(ServicoIndisponivelError, match="não é JSON"):
        _post_search("api_publica_tjpe", "{}", api_key=CHAVE, timeout_seconds=5)

    _install(monkeypatch, body=b'["lista"]')
    with pytest.raises(ServicoIndisponivelError, match="não é um objeto JSON"):
        _post_search("api_publica_tjpe", "{}", api_key=CHAVE, timeout_seconds=5)


def test_an_oversized_answer_is_cut_off_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, body=b"x" * (datajud._MAX_RESPONSE_BYTES + 10))
    with pytest.raises(ServicoIndisponivelError, match="excedeu o limite local"):
        _post_search("api_publica_tjpe", "{}", api_key=CHAVE, timeout_seconds=5)


def test_network_failures_are_reported_as_service_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(host: str, port: int, timeout: float) -> _FakeConnection:
        raise TimeoutError("sem rota")

    monkeypatch.setattr(datajud.http.client, "HTTPSConnection", explode)
    with pytest.raises(ServicoIndisponivelError, match="não foi possível falar com a API DataJud"):
        _post_search("api_publica_tjpe", "{}", api_key=CHAVE, timeout_seconds=5)


# ----------------------------------------------------------------------- cliente


@pytest.mark.anyio
async def test_the_client_builds_the_query_itself_from_the_npu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    criadas = _install(monkeypatch)
    numero = _npu("817")
    client = DatajudClient(Settings(datajud_api_key=CHAVE))

    modelo = await client.consultar_metadados(numero, TribunalCodigo.TJPE)

    corpo = json.loads((criadas[0].body or b"").decode("utf-8"))
    # O chamador informa um NPU; a query do Elasticsearch é montada aqui dentro.
    assert corpo == {"size": 1, "query": {"match": {"numeroProcesso": "99999990220998179999"}}}
    assert modelo.numero == numero


@pytest.mark.anyio
async def test_the_client_rejects_an_out_of_range_movement_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    criadas = _install(monkeypatch)
    client = DatajudClient(Settings(datajud_api_key=CHAVE))

    for limite in (0, -1, 501):
        with pytest.raises(ValidacaoError, match="limite_movimentos"):
            await client.consultar_metadados(
                _npu("817"), TribunalCodigo.TJPE, limite_movimentos=limite
            )
    assert criadas == []
