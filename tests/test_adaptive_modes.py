from __future__ import annotations

from pathlib import Path

import pytest

from mcp_pje_tjpe.adaptive.adapters import AdapterRegistry
from mcp_pje_tjpe.adaptive.controller import AdaptiveNavigation
from mcp_pje_tjpe.adaptive.events import ObservationStore
from mcp_pje_tjpe.adaptive.models import ControleEstrutural
from mcp_pje_tjpe.adaptive.resolver import active_semantic_phrases, evaluate_step
from mcp_pje_tjpe.config import ModoAdaptativo, Settings
from mcp_pje_tjpe.tribunals import TribunalCodigo


def _submit_control(*, visible: bool = True) -> ControleEstrutural:
    return ControleEstrutural(
        tag="button",
        role="button",
        tipo="submit",
        visivel=visible,
        habilitado=True,
        submit_nativo=True,
        possui_formulario=True,
        tokens_semanticos=("pesquisar",),
    )


def _npu_control() -> ControleEstrutural:
    return ControleEstrutural(
        tag="input",
        role="textbox",
        tipo="text",
        visivel=True,
        habilitado=True,
        editavel=True,
        possui_formulario=True,
        tokens_semanticos=("numero", "processo"),
    )


def test_observe_and_shadow_never_add_adapter_fallback_phrases() -> None:
    adapter = AdapterRegistry().get(TribunalCodigo.TJPE, "pesquisa_geral")
    baseline = ("Processo",)

    assert (
        active_semantic_phrases(adapter, "npu_field", ModoAdaptativo.OBSERVE, baseline) == baseline
    )
    assert (
        active_semantic_phrases(adapter, "npu_field", ModoAdaptativo.SHADOW, baseline) == baseline
    )


def test_active_adds_only_reviewed_phrases_and_deduplicates_semantically() -> None:
    adapter = AdapterRegistry().get(TribunalCodigo.TJPE, "pesquisa_geral")

    phrases = active_semantic_phrases(
        adapter,
        "npu_field",
        ModoAdaptativo.ACTIVE,
        ("PROCESSO",),
    )

    assert phrases[0] == "PROCESSO"
    assert set(phrases[1:]) == {"Numeração única", "Número do processo"}
    assert "Processo" not in phrases[1:]


def test_active_does_not_enable_discovery_only_tribunal_adapter() -> None:
    adapter = AdapterRegistry().get(TribunalCodigo.TRT6, "pesquisa_geral")
    baseline = ("Pesquisar",)

    assert (
        active_semantic_phrases(adapter, "search_submit", ModoAdaptativo.ACTIVE, baseline)
        == baseline
    )


def test_navigation_controller_obeys_the_selected_mode(tmp_path: Path) -> None:
    registry = AdapterRegistry()
    baseline = ("Processo",)

    for mode in (ModoAdaptativo.OBSERVE, ModoAdaptativo.SHADOW):
        navigation = AdaptiveNavigation(registry, ObservationStore(tmp_path), mode)
        assert (
            navigation.semantic_phrases(
                TribunalCodigo.TJPE,
                "tjpe_1g",
                "pesquisa_geral",
                "npu_field",
                baseline,
            )
            == baseline
        )

    active = AdaptiveNavigation(
        registry,
        ObservationStore(tmp_path),
        ModoAdaptativo.ACTIVE,
    )
    phrases = active.semantic_phrases(
        TribunalCodigo.TJPE,
        "tjpe_1g",
        "pesquisa_geral",
        "npu_field",
        baseline,
    )
    assert phrases[0] == "Processo"
    assert set(phrases[1:]) == {"Numeração única", "Número do processo"}


def test_navigation_controller_rejects_adapter_from_another_instance(tmp_path: Path) -> None:
    navigation = AdaptiveNavigation(
        AdapterRegistry(),
        ObservationStore(tmp_path),
        ModoAdaptativo.ACTIVE,
    )

    with pytest.raises(ValueError, match="instância"):
        navigation.semantic_phrases(
            TribunalCodigo.TJPE,
            "trt6_1g",
            "pesquisa_geral",
            "search_submit",
            ("Pesquisar",),
        )


def test_settings_rejects_invalid_adaptive_environment_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PJE_TJPE_ADAPTIVE_MODE", "automatico")
    with pytest.raises(ValueError, match="observe, shadow, active"):
        Settings()

    monkeypatch.setenv("PJE_TJPE_ADAPTIVE_MODE", "shadow")
    monkeypatch.setenv("PJE_TJPE_ADAPTIVE_MAX_EVENTS", "0")
    with pytest.raises(ValueError, match="deve ser positivo"):
        Settings()

    monkeypatch.setenv("PJE_TJPE_ADAPTIVE_MAX_EVENTS", "10001")
    with pytest.raises(ValueError, match="no máximo 10000"):
        Settings()


def test_step_evaluation_requires_visibility_native_submit_form_and_uniqueness() -> None:
    adapter = AdapterRegistry().get(TribunalCodigo.TJPE, "pesquisa_geral")

    accepted = evaluate_step(adapter, "search_submit", (_submit_control(),))[0]
    ambiguous = evaluate_step(
        adapter,
        "search_submit",
        (_submit_control(), _submit_control()),
    )[0]
    hidden = evaluate_step(adapter, "search_submit", (_submit_control(visible=False),))[0]

    assert accepted.correspondencias == 1
    assert accepted.quantidade_aceita is True
    assert accepted.aprovada_para_active is True
    assert ambiguous.correspondencias == 2
    assert ambiguous.quantidade_aceita is False
    assert hidden.correspondencias == 0
    assert hidden.quantidade_aceita is False


def test_exact_strategy_rejects_extra_known_or_unknown_semantic_words() -> None:
    adapter = AdapterRegistry().get(TribunalCodigo.TJPE, "pesquisa_geral")
    known_extra = _submit_control().model_copy(
        update={"tokens_semanticos": ("consultar", "pesquisar")}
    )
    unknown_extra = _submit_control().model_copy(update={"tokens_desconhecidos": 1})

    assert evaluate_step(adapter, "search_submit", (known_extra,))[0].quantidade_aceita is False
    assert evaluate_step(adapter, "search_submit", (unknown_extra,))[0].quantidade_aceita is False


def test_segmented_npu_count_is_accepted_but_other_counts_fail_closed() -> None:
    adapter = AdapterRegistry().get(TribunalCodigo.TJPE, "pesquisa_geral")

    one = evaluate_step(adapter, "npu_field", (_npu_control(),))[0]
    six = evaluate_step(adapter, "npu_field", tuple(_npu_control() for _ in range(6)))[0]
    two = evaluate_step(adapter, "npu_field", (_npu_control(), _npu_control()))[0]

    assert one.quantidade_aceita is True
    assert six.quantidade_aceita is True
    assert two.quantidade_aceita is False


def test_unknown_step_never_produces_a_candidate() -> None:
    adapter = AdapterRegistry().get(TribunalCodigo.TJPE, "pesquisa_geral")

    assert evaluate_step(adapter, "unknown_step", (_submit_control(),)) == ()
