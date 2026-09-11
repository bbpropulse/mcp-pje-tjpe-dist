from __future__ import annotations

# pyright: reportPrivateUsage=false
from dataclasses import dataclass
from pathlib import Path

import pytest

import mcp_pje_tjpe.pje_docs as pje_docs
from mcp_pje_tjpe.errors import (
    CredenciaisAusentesError,
    ServicoIndisponivelError,
    ValidacaoError,
)
from mcp_pje_tjpe.pje_docs import _stream_pjedocs_https

_CAPABILITY = "capability-ultrassecreta-nao-vazar"
_DOWNLOAD_URL = (
    f"https://pje.cloud.tjpe.jus.br/1g/Download/resultado.seam?id=resultado-1&ca={_CAPABILITY}"
)
_REQUEST_TARGET = f"/1g/Download/resultado.seam?id=resultado-1&ca={_CAPABILITY}"
_NPU = "9999999-02.2099.8.17.9999"


class _FakeHTTPResponse:
    def __init__(
        self,
        body: bytes = b"",
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.headers = {name.casefold(): value for name, value in (headers or {}).items()}
        self.offset = 0
        self.read_sizes: list[int] = []
        self.header_requests: list[str] = []

    def getheader(self, name: str, default: str | None = None) -> str | None:
        self.header_requests.append(name.casefold())
        return self.headers.get(name.casefold(), default)

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        chunk = self.body[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


class _FakeHTTPSConnection:
    def __init__(self, response: _FakeHTTPResponse) -> None:
        self.response = response
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.getresponse_calls = 0
        self.closed = False

    def request(self, method: str, target: str, *, headers: dict[str, str]) -> None:
        self.requests.append((method, target, headers))

    def getresponse(self) -> _FakeHTTPResponse:
        self.getresponse_calls += 1
        return self.response

    def close(self) -> None:
        self.closed = True


class _FakeConnectionFactory:
    def __init__(self, connection: _FakeHTTPSConnection) -> None:
        self.connection = connection
        self.calls: list[tuple[str, int, float]] = []

    def __call__(self, host: str, port: int, *, timeout: float) -> _FakeHTTPSConnection:
        self.calls.append((host, port, timeout))
        return self.connection


@dataclass(frozen=True, slots=True)
class _DiskUsage:
    free: int


def _ample_disk_space(_path: Path) -> _DiskUsage:
    return _DiskUsage(free=1 << 50)


def _assert_stream_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: _FakeHTTPResponse,
    *,
    error_type: type[Exception],
    match: str,
    max_bytes: int = 1_024,
) -> tuple[_FakeHTTPSConnection, _FakeConnectionFactory]:
    connection = _FakeHTTPSConnection(response)
    factory = _FakeConnectionFactory(connection)
    monkeypatch.setattr(pje_docs.http.client, "HTTPSConnection", factory)
    monkeypatch.setattr(pje_docs.shutil, "disk_usage", _ample_disk_space)

    destination = tmp_path / "resultado"
    destination.mkdir()
    with pytest.raises(error_type, match=match) as captured:
        _stream_pjedocs_https(
            _DOWNLOAD_URL,
            cookie_header="JSESSIONID=valor-opaco",
            user_agent="Mozilla/5.0 teste",
            max_bytes=max_bytes,
            timeout_seconds=10,
            destination=destination,
            numero=_NPU,
        )

    message = str(captured.value)
    assert _CAPABILITY not in message
    assert _DOWNLOAD_URL not in message
    assert factory.calls == [("pje.cloud.tjpe.jus.br", 443, 10)]
    assert connection.requests == [
        (
            "GET",
            _REQUEST_TARGET,
            {
                "Accept-Encoding": "identity",
                "Cookie": "JSESSIONID=valor-opaco",
                "User-Agent": "Mozilla/5.0 teste",
            },
        )
    ]
    assert connection.getresponse_calls == 1
    assert connection.closed is True
    assert list(destination.iterdir()) == []
    return connection, factory


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_is_never_followed_and_temporary_file_is_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    response = _FakeHTTPResponse(
        status=status,
        headers={"location": f"https://evil.example/roubo?ca={_CAPABILITY}"},
    )

    connection, factory = _assert_stream_failure(
        tmp_path,
        monkeypatch,
        response,
        error_type=ServicoIndisponivelError,
        match="redirecionamento não foi seguido",
    )

    assert len(factory.calls) == 1
    assert all("evil.example" not in target for _, target, _ in connection.requests)
    assert response.read_sizes == []


@pytest.mark.parametrize(
    ("status", "error_type", "match"),
    [
        (401, CredenciaisAusentesError, "sessão expirou"),
        (403, ValidacaoError, "permissão ou expiração"),
    ],
)
def test_authentication_and_authorization_failures_use_expected_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    error_type: type[Exception],
    match: str,
) -> None:
    response = _FakeHTTPResponse(status=status)

    _assert_stream_failure(
        tmp_path,
        monkeypatch,
        response,
        error_type=error_type,
        match=match,
    )

    assert response.read_sizes == []


@pytest.mark.parametrize(
    ("declared", "body", "max_bytes", "error_type", "match", "body_is_read"),
    [
        (
            "não-número",
            b"%PDF-1.7\nbody",
            1_024,
            ServicoIndisponivelError,
            "Content-Length inválido",
            False,
        ),
        (
            "-1",
            b"%PDF-1.7\nbody",
            1_024,
            ServicoIndisponivelError,
            "tamanho negativo",
            False,
        ),
        (
            "1025",
            b"%PDF-1.7\nbody",
            1_024,
            ValidacaoError,
            "excede PJE_TJPE_MAX_PJEDOCS_BYTES",
            False,
        ),
        (
            "99",
            b"%PDF-1.7\nbody",
            1_024,
            ServicoIndisponivelError,
            "tamanho diferente do declarado",
            True,
        ),
        (
            "1",
            b"%PDF-1.7\nbody",
            1_024,
            ServicoIndisponivelError,
            "tamanho diferente do declarado",
            True,
        ),
    ],
)
def test_content_length_is_validated_against_limits_and_received_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    declared: str,
    body: bytes,
    max_bytes: int,
    error_type: type[Exception],
    match: str,
    body_is_read: bool,
) -> None:
    response = _FakeHTTPResponse(
        body,
        headers={"content-encoding": "identity", "content-length": declared},
    )

    _assert_stream_failure(
        tmp_path,
        monkeypatch,
        response,
        error_type=error_type,
        match=match,
        max_bytes=max_bytes,
    )

    assert bool(response.read_sizes) is body_is_read


def test_body_without_content_length_stops_at_limit_and_removes_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _FakeHTTPResponse(
        b"%PDF-1.7\n" + (b"x" * 64),
        headers={"content-encoding": "identity"},
    )

    _assert_stream_failure(
        tmp_path,
        monkeypatch,
        response,
        error_type=ValidacaoError,
        match="arquivo parcial foi removido",
        max_bytes=16,
    )

    assert response.read_sizes == [pje_docs._DOWNLOAD_CHUNK_BYTES]


@pytest.mark.parametrize("encoding", ["gzip", "br"])
def test_non_identity_content_encoding_is_rejected_before_body_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    encoding: str,
) -> None:
    body = b"%PDF-1.7\nbody"
    response = _FakeHTTPResponse(
        body,
        headers={"content-encoding": encoding, "content-length": str(len(body))},
    )

    _assert_stream_failure(
        tmp_path,
        monkeypatch,
        response,
        error_type=ServicoIndisponivelError,
        match="codificação de transporte não permitida",
    )

    assert response.read_sizes == []


@pytest.mark.parametrize(
    ("body", "error_type", "match"),
    [
        (
            b"<html><body>resposta inesperada</body></html>",
            CredenciaisAusentesError,
            "sessão expirou",
        ),
        (
            b'prefixo id="kc-form-login" sufixo',
            CredenciaisAusentesError,
            "sessão expirou",
        ),
        (b"MZ\x90\x00executavel", ServicoIndisponivelError, "formato executável"),
        (
            b"conteudo sem assinatura conhecida",
            ServicoIndisponivelError,
            "assinatura reconhecida de PDF ou ZIP",
        ),
    ],
)
def test_html_login_executable_and_unknown_payloads_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
    error_type: type[Exception],
    match: str,
) -> None:
    response = _FakeHTTPResponse(
        body,
        headers={"content-encoding": "identity", "content-length": str(len(body))},
    )

    _assert_stream_failure(
        tmp_path,
        monkeypatch,
        response,
        error_type=error_type,
        match=match,
    )

    assert response.read_sizes
