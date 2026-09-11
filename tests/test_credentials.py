import pytest

from mcp_pje_tjpe.credentials import normalize_cpf
from mcp_pje_tjpe.errors import ValidacaoError


def test_normalize_cpf() -> None:
    assert normalize_cpf("123.456.789-01") == "12345678901"


def test_rejects_malformed_cpf() -> None:
    with pytest.raises(ValidacaoError):
        normalize_cpf("123")
