import os
import re

import pytest

from mcp_pje_tjpe.browser import BrowserManager
from mcp_pje_tjpe.config import Settings
from mcp_pje_tjpe.errors import ServicoIndisponivelError
from mcp_pje_tjpe.models import Grau
from mcp_pje_tjpe.pje_public import (
    PjePublicClient,
    mask_cpf_cnpj,
    parse_result_count,
    validate_npu_tjpe,
)


def _synthetic_npu() -> str:
    sequence = "9999999"
    year = "2099"
    origin = "9999"
    base = sequence + year + "8" + "17" + origin + "00"
    check_digits = 98 - (int(base) % 97)
    return f"{sequence}-{check_digits:02d}.{year}.8.17.{origin}"


def test_validate_npu_tjpe_accepts_synthetic_mod97_number() -> None:
    numero = _synthetic_npu()

    assert validate_npu_tjpe(numero)
    assert validate_npu_tjpe(re.sub(r"\D", "", numero))


def test_validate_npu_tjpe_rejects_wrong_check_digit_and_tribunal() -> None:
    numero = _synthetic_npu()
    digits = re.sub(r"\D", "", numero)
    wrong_check = (
        f"{digits[:7]}-{(int(digits[7:9]) + 1) % 100:02d}.{digits[9:13]}.8.17.{digits[16:]}"
    )
    wrong_court = f"{digits[:7]}-{digits[7:9]}.{digits[9:13]}.8.18.{digits[16:]}"

    assert not validate_npu_tjpe(wrong_check)
    assert not validate_npu_tjpe(wrong_court)
    assert not validate_npu_tjpe(f"processo {numero}")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (" resultados encontrados", 0),
        ("1 resultados encontrados", 1),
        ("  25   resultados encontrados  ", 25),
    ],
)
def test_parse_result_count(text: str, expected: int) -> None:
    assert parse_result_count(text) == expected


def test_parse_result_count_rejects_unknown_markup_text() -> None:
    with pytest.raises(ServicoIndisponivelError, match="contador inesperado"):
        parse_result_count("resultado indisponível")


def test_mask_cpf_cnpj_masks_formatted_and_unformatted_labeled_values() -> None:
    text = (
        "PARTE A - CPF: 111.111.111-11; PARTE B - CNPJ 11111111111111; "
        "referência pública 12345678901"
    )

    masked = mask_cpf_cnpj(text)

    assert "CPF: ***.***.***-**" in masked
    assert "CNPJ **.***.***/****-**" in masked
    assert "111.111.111-11" not in masked
    assert "11111111111111" not in masked
    assert "referência pública 12345678901" in masked


def test_detail_url_accepts_only_current_tjpe_degree() -> None:
    onclick = (
        "openPopUp('Consulta pública',"
        "'/1g/ConsultaPublica/DetalheProcessoConsultaPublica/listView.seam?ca=token')"
    )

    assert PjePublicClient._detail_url(  # pyright: ignore[reportPrivateUsage]
        onclick, Grau.PRIMEIRO, "https://pje.cloud.tjpe.jus.br/1g"
    ) == (
        "https://pje.cloud.tjpe.jus.br/1g/ConsultaPublica/"
        "DetalheProcessoConsultaPublica/listView.seam?ca=token"
    )

    with pytest.raises(ServicoIndisponivelError):
        PjePublicClient._detail_url(  # pyright: ignore[reportPrivateUsage]
            onclick, Grau.SEGUNDO, "https://pje.cloud.tjpe.jus.br/2g"
        )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.live
@pytest.mark.anyio
async def test_consulta_publica_live_only_with_explicit_environment() -> None:
    if os.getenv("RUN_TJPE_LIVE_TESTS") != "1":
        pytest.skip("defina RUN_TJPE_LIVE_TESTS=1 para consultar o TJPE real")
    numero = os.getenv("TJPE_TEST_NPU")
    if not numero:
        pytest.skip("defina TJPE_TEST_NPU com um processo público autorizado para o teste")

    grau = Grau(os.getenv("TJPE_TEST_GRAU", Grau.PRIMEIRO.value))
    config = Settings(headless=True)
    browser = BrowserManager(config)
    try:
        result = await PjePublicClient(browser, config).consultar(numero, grau)
        assert result.numero == numero or re.sub(r"\D", "", result.numero) == re.sub(
            r"\D", "", numero
        )
        serialized = result.model_dump_json()
        assert mask_cpf_cnpj(serialized) == serialized
        assert "?ca=" not in result.url_fonte
    finally:
        await browser.close()
