from __future__ import annotations

# pyright: reportPrivateUsage=false
import asyncio
import re
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import urlencode

import pytest
from playwright.async_api import Locator

from mcp_pje_tjpe.errors import (
    InterfacePjeAlteradaError,
    ServicoIndisponivelError,
    ValidacaoError,
)
from mcp_pje_tjpe.models import EstadoAcessoPesquisaGeral, Grau
from mcp_pje_tjpe.pje_search import (
    _AccessBinding,
    _allowed_search_get,
    _DialogState,
    _FormContract,
    _is_official_access_dialog,
    _normalized_npu_tokens,
    _page_is_partial,
    _Plan,
    _PlanStatus,
    _post_matches_contract,
    _sanitized_failure,
    _search_target,
    _SearchGate,
    _SearchResult,
    confirmation_phrase,
)


def _synthetic_npu(
    sequence: str = "9999999",
    *,
    year: str = "2099",
    origin: str = "9999",
) -> str:
    base = sequence + year + "8" + "17" + origin + "00"
    check_digits = 98 - (int(base) % 97)
    return f"{sequence}-{check_digits:02d}.{year}.8.17.{origin}"


@pytest.mark.parametrize("grau", [Grau.PRIMEIRO, Grau.SEGUNDO])
def test_search_target_accepts_only_the_authenticated_search_route(grau: Grau) -> None:
    base = f"https://pje.cloud.tjpe.jus.br/{grau.value}/Processo/ConsultaProcesso/listView.seam"

    assert _search_target(base, grau) == base
    assert _search_target(f"{base}?cid=123456789012", grau) == (f"{base}?cid=123456789012")
    explicit_https_port = base.replace("pje.cloud.tjpe.jus.br", "pje.cloud.tjpe.jus.br:443")
    assert _search_target(explicit_https_port, grau) == explicit_https_port


@pytest.mark.parametrize(
    "url",
    [
        "http://pje.cloud.tjpe.jus.br/1g/Processo/ConsultaProcesso/listView.seam",
        "https://pje.cloud.tjpe.jus.br/2g/Processo/ConsultaProcesso/listView.seam",
        "https://evil.example/1g/Processo/ConsultaProcesso/listView.seam",
        "https://usuario@pje.cloud.tjpe.jus.br/1g/Processo/ConsultaProcesso/listView.seam",
        "https://pje.cloud.tjpe.jus.br:444/1g/Processo/ConsultaProcesso/listView.seam",
        "https://pje.cloud.tjpe.jus.br/1g/Processo/ConsultaProcesso/listView.seam#resultado",
        "https://pje.cloud.tjpe.jus.br/1g/Processo/ConsultaProcesso/listView.seam?cid=",
        "https://pje.cloud.tjpe.jus.br/1g/Processo/ConsultaProcesso/listView.seam?cid=abc",
        "https://pje.cloud.tjpe.jus.br/1g/Processo/ConsultaProcesso/listView.seam?cid=1&cid=2",
        "https://pje.cloud.tjpe.jus.br/1g/Processo/ConsultaProcesso/listView.seam?x=1",
        "https://pje.cloud.tjpe.jus.br/1g/Processo/../ConsultaProcesso/listView.seam",
        "https://pje.cloud.tjpe.jus.br//1g/Processo/ConsultaProcesso/listView.seam",
    ],
)
def test_search_target_rejects_route_ambiguity(url: str) -> None:
    with pytest.raises(InterfacePjeAlteradaError, match=r"rota|query|parâmetros"):
        _search_target(url, Grau.PRIMEIRO)


@pytest.mark.parametrize(
    ("url", "resource_type"),
    [
        (
            "https://pje.cloud.tjpe.jus.br/1g/Processo/ConsultaProcesso/listView.seam",
            "document",
        ),
        ("https://pje.cloud.tjpe.jus.br/1g/javax.faces.resource/app.js", "script"),
        ("https://pje.cloud.tjpe.jus.br/1g/resources/app.css?v=1", "stylesheet"),
        ("https://pje.cloud.tjpe.jus.br/1g/static/logo.png", "image"),
        ("https://pje.cloud.tjpe.jus.br/1g/assets/fonte.woff2", "font"),
    ],
)
def test_allowed_search_get_accepts_only_search_document_and_assets(
    url: str,
    resource_type: str,
) -> None:
    assert _allowed_search_get(url, resource_type, Grau.PRIMEIRO) is True


@pytest.mark.parametrize(
    ("url", "resource_type"),
    [
        (
            "https://pje.cloud.tjpe.jus.br/1g/Processo/ConsultaProcesso/listView.seam",
            "xhr",
        ),
        ("https://pje.cloud.tjpe.jus.br/1g/Processo/Outra/listView.seam", "document"),
        ("https://pje.cloud.tjpe.jus.br/2g/static/app.js", "script"),
        ("https://evil.example/1g/static/app.js", "script"),
        ("https://pje.cloud.tjpe.jus.br/1g/assets/app.json", "script"),
        ("https://pje.cloud.tjpe.jus.br/1g/login/app.js", "script"),
    ],
)
def test_allowed_search_get_rejects_other_navigation_and_resource_types(
    url: str,
    resource_type: str,
) -> None:
    assert _allowed_search_get(url, resource_type, Grau.PRIMEIRO) is False


def test_post_matches_exact_native_and_jsf_contracts() -> None:
    numero = _synthetic_npu()
    required = {"numeroProcesso": numero, "javax.faces.ViewState": "view-state-a"}

    native = urlencode({**required, "pesquisar": "Pesquisar"})
    jsf = urlencode({**required, "javax.faces.source": "pesquisar"})
    both = urlencode(
        {
            **required,
            "pesquisar": "Pesquisar",
            "javax.faces.source": "pesquisar",
        }
    )

    assert _post_matches_contract(native, "pesquisar", required) is True
    assert (
        _post_matches_contract(
            native,
            "pesquisar",
            required,
            marker_value="Pesquisar",
        )
        is True
    )
    assert (
        _post_matches_contract(
            native,
            "pesquisar",
            required,
            marker_value="Excluir",
        )
        is False
    )
    assert _post_matches_contract(jsf, "pesquisar", required) is True
    assert _post_matches_contract(both, "pesquisar", required) is True


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "numeroProcesso=9999999&javax.faces.ViewState=view-state-a",
        "numeroProcesso=9999999&javax.faces.ViewState=view-state-a&pesquisar=1&pesquisar=2",
        "numeroProcesso=9999999&javax.faces.ViewState=view-state-a&javax.faces.source=outro",
        "numeroProcesso=9999999&javax.faces.ViewState=view-state-a&"
        "pesquisar=Pesquisar&javax.faces.source=outro",
        "numeroProcesso=9999999&javax.faces.ViewState=alterado&pesquisar=Pesquisar",
        "numeroProcesso=9999999&javax.faces.ViewState=view-state-a&pesquisar=Pesquisar&extra=1",
        "numeroProcesso=9999999&numeroProcesso=9999998&"
        "javax.faces.ViewState=view-state-a&pesquisar=Pesquisar",
    ],
)
def test_post_contract_rejects_missing_duplicate_changed_or_extra_fields(
    payload: str | None,
) -> None:
    required = {"numeroProcesso": "9999999", "javax.faces.ViewState": "view-state-a"}

    assert _post_matches_contract(payload, "pesquisar", required) is False


def test_post_contract_rejects_payload_over_local_limit() -> None:
    assert _post_matches_contract("x" * (1024 * 1024 + 1), "pesquisar", {}) is False


def test_normalized_npu_tokens_accepts_formatted_and_numeric_duplicates() -> None:
    numero = _synthetic_npu()
    digits = re.sub(r"\D", "", numero)

    assert _normalized_npu_tokens(f"Processo {numero}; referência {digits}; {numero}") == {numero}


def test_normalized_npu_tokens_detects_conflicts_and_fails_closed_on_invalid_token() -> None:
    numero = _synthetic_npu()
    outro = _synthetic_npu("9999998")
    invalido = f"{numero[:8]}00{numero[10:]}"

    assert _normalized_npu_tokens(f"{numero} e {outro}") == {numero, outro}
    assert _normalized_npu_tokens(f"{numero} e {invalido}") == set()
    assert _normalized_npu_tokens("sem NPU") == set()


@pytest.mark.parametrize(
    "text",
    [
        "Página 1 de 2",
        "Resultados 4 de 10",
        "Próxima página",
        "CARREGAR MAIS RESULTADOS",
    ],
)
def test_page_is_partial_detects_pagination_and_lazy_loading(text: str) -> None:
    assert _page_is_partial(text) is True


@pytest.mark.parametrize("text", ["Página 2 de 2", "Resultado 1 de 1", "sem paginação"])
def test_page_is_partial_accepts_complete_or_unpaginated_results(text: str) -> None:
    assert _page_is_partial(text) is False


def test_official_access_dialog_accepts_real_resolution_spelling_and_exact_npu() -> None:
    numero = _synthetic_npu()
    message = (
        "Conforme a Resolução CNJ n.º 121, o acesso aos autos do processo "
        f"{numero} será registrado para fins de auditoria. Deseja continuar?"
    )

    assert _is_official_access_dialog(message, numero) is True


def test_official_access_dialog_accepts_generic_notice_without_npu() -> None:
    numero = _synthetic_npu()
    message = (
        "Conforme a Resolução CNJ n.º 121, este pedido de acesso aos autos será "
        "registrado para fins de auditoria. Deseja continuar?"
    )

    assert _is_official_access_dialog(message, numero) is True


def test_official_access_dialog_rejects_a_different_or_conflicting_npu() -> None:
    numero = _synthetic_npu()
    outro = _synthetic_npu("9999998")
    prefix = "Conforme a Resolução CNJ n.º 121, este acesso será registrado"

    assert _is_official_access_dialog(f"{prefix}: {outro}", numero) is False
    assert _is_official_access_dialog(f"{prefix}: {numero} e {outro}", numero) is False


@pytest.mark.parametrize(
    "blocked_text",
    [
        "Tomar ciência da intimação pendente",
        "Protocolar petição",
        "Processo sigiloso",
        "Segredo de justiça: Sim",
    ],
)
def test_official_access_dialog_rejects_science_protocol_and_secrecy(
    blocked_text: str,
) -> None:
    numero = _synthetic_npu()
    message = (
        "Conforme a Resolução CNJ n.º 121, o acesso ao processo "
        f"{numero} será registrado. {blocked_text}"
    )

    assert _is_official_access_dialog(message, numero) is False


def test_confirmation_phrase_is_literal_and_bound_to_npu_and_degree() -> None:
    numero = _synthetic_npu()
    digits = re.sub(r"\D", "", numero)

    assert confirmation_phrase(digits, Grau.PRIMEIRO) == (
        f"CONFIRMO INTERESSE E POSSÍVEL REGISTRO DE ACESSO AO PROCESSO {numero} NO PJE-TJPE 1G"
    )
    assert confirmation_phrase(numero, Grau.SEGUNDO) == (
        f"CONFIRMO INTERESSE E POSSÍVEL REGISTRO DE ACESSO AO PROCESSO {numero} NO PJE-TJPE 2G"
    )
    with pytest.raises(ValidacaoError):
        confirmation_phrase("00000000000000000000", Grau.PRIMEIRO)


def test_sanitized_failure_hides_unexpected_exception_and_preserves_domain_errors() -> None:
    secret = "capability-secret-that-must-not-leak"
    safe_message = "não foi possível concluir a operação com segurança"

    @_sanitized_failure(safe_message)
    async def unexpected_failure() -> None:
        await asyncio.sleep(0)
        raise RuntimeError(secret)

    with pytest.raises(ServicoIndisponivelError) as captured:
        asyncio.run(cast(Coroutine[object, object, None], unexpected_failure()))
    assert str(captured.value) == safe_message
    assert secret not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__suppress_context__ is True

    expected = ValidacaoError("NPU inválido")

    @_sanitized_failure(safe_message)
    async def expected_failure() -> None:
        await asyncio.sleep(0)
        raise expected

    with pytest.raises(ValidacaoError) as domain_error:
        asyncio.run(cast(Coroutine[object, object, None], expected_failure()))
    assert domain_error.value is expected


def test_search_dataclass_reprs_hide_session_route_and_capability_material() -> None:
    numero = _synthetic_npu()
    now = datetime(2099, 1, 1, tzinfo=UTC)
    locator = cast(Locator, object())

    plan = _Plan(
        reference="plan-reference",
        generation="generation-secret",
        grau=Grau.PRIMEIRO,
        numero=numero,
        processo_id="process-id-secret",
        fingerprint="fingerprint-secret",
        created_at=now,
        expires_at=now + timedelta(minutes=10),
        status=_PlanStatus.PREPARADO,
    )
    access = _AccessBinding(
        reference="access-reference",
        plan_reference="plan-reference",
        generation="access-generation-secret",
        grau=Grau.PRIMEIRO,
        numero=numero,
        processo_id="access-process-id-secret",
        state=EstadoAcessoPesquisaGeral.ABERTO,
        accessed_at=now,
    )
    result = _SearchResult(
        numero=numero,
        processo_id="result-process-id-secret",
        autos_url="https://pje.cloud.tjpe.jus.br/1g/Autos?ca=route-secret",
        fingerprint="result-fingerprint-secret",
        anchor=locator,
    )
    contract = _FormContract(
        submit=locator,
        target="https://pje.cloud.tjpe.jus.br/1g/search?token=target-secret",
        marker="marker-secret",
        marker_value="marker-value-secret",
        fields={"javax.faces.ViewState": "view-state-secret"},
    )
    dialog = _DialogState(numero, messages=["dialog-secret"])
    gate = _SearchGate(
        Grau.PRIMEIRO,
        allowed_post_target="https://pje.cloud.tjpe.jus.br/1g/search?token=gate-secret",
        required_marker="gate-marker-secret",
        required_fields={"javax.faces.ViewState": "gate-view-state-secret"},
        opening_target="https://pje.cloud.tjpe.jus.br/1g/Autos?ca=opening-secret",
    )

    representations = [
        repr(plan),
        repr(access),
        repr(result),
        repr(contract),
        repr(dialog),
        repr(gate),
    ]
    secrets = [
        "generation-secret",
        "process-id-secret",
        "fingerprint-secret",
        "access-generation-secret",
        "access-process-id-secret",
        "result-process-id-secret",
        "route-secret",
        "result-fingerprint-secret",
        "target-secret",
        "marker-secret",
        "marker-value-secret",
        "view-state-secret",
        "dialog-secret",
        "gate-secret",
        "gate-marker-secret",
        "gate-view-state-secret",
        "opening-secret",
    ]
    assert all(secret not in rendered for secret in secrets for rendered in representations)
    assert numero in repr(plan)
    assert numero in repr(access)
    assert repr(result) == f"_SearchResult(numero='{numero}')"
    assert repr(contract) == "_FormContract()"
