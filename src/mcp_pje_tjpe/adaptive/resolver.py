from __future__ import annotations

from mcp_pje_tjpe.adaptive.adapters import (
    AdaptadorNavegacao,
    EstrategiaDescoberta,
    fold_semantic,
    semantic_words,
)
from mcp_pje_tjpe.adaptive.models import AvaliacaoEstrategia, ControleEstrutural
from mcp_pje_tjpe.config import ModoAdaptativo


def _phrase_matches_exactly(tokens: set[str], phrases: tuple[str, ...]) -> bool:
    return any(set(semantic_words(phrase)) == tokens for phrase in phrases)


def _terms_match(tokens: set[str], phrases: tuple[str, ...]) -> bool:
    return any(set(semantic_words(phrase)).issubset(tokens) for phrase in phrases)


def _matches_strategy(
    control: ControleEstrutural,
    strategy: EstrategiaDescoberta,
) -> bool:
    if control.tokens_desconhecidos:
        return False
    constraints = strategy.constraints
    if constraints.visible and not control.visivel:
        return False
    if constraints.enabled and not control.habilitado:
        return False
    if constraints.native_submit is not None and (
        control.submit_nativo is not constraints.native_submit
    ):
        return False
    if constraints.owns_form is not None and (
        control.possui_formulario is not constraints.owns_form
    ):
        return False
    if constraints.input_types and control.tipo not in constraints.input_types:
        return False
    if constraints.editable is not None and control.editavel is not constraints.editable:
        return False

    tokens = set(control.tokens_semanticos)
    if strategy.kind == "role_exact":
        return control.role == strategy.role and _phrase_matches_exactly(tokens, strategy.names)
    if strategy.kind == "label_exact":
        return _phrase_matches_exactly(tokens, strategy.labels)
    return _terms_match(tokens, strategy.terms)


def evaluate_step(
    adapter: AdaptadorNavegacao,
    step: str,
    controls: tuple[ControleEstrutural, ...],
) -> tuple[AvaliacaoEstrategia, ...]:
    selected = adapter.steps.get(step)
    if selected is None:
        return ()
    evaluations: list[AvaliacaoEstrategia] = []
    for strategy in selected.strategies:
        matches = sum(_matches_strategy(control, strategy) for control in controls)
        allowed = matches in strategy.constraints.count
        if strategy.constraints.unique:
            allowed = allowed and matches == 1
        evaluations.append(
            AvaliacaoEstrategia(
                estrategia_id=strategy.id,
                correspondencias=matches,
                quantidade_aceita=allowed,
                aprovada_para_active=adapter.approved_for_active and strategy.approved,
            )
        )
    return tuple(evaluations)


def active_semantic_phrases(
    adapter: AdaptadorNavegacao,
    step: str,
    mode: ModoAdaptativo,
    baseline: tuple[str, ...],
) -> tuple[str, ...]:
    if mode is not ModoAdaptativo.ACTIVE or not adapter.approved_for_active:
        return baseline
    selected = adapter.steps.get(step)
    if selected is None:
        return baseline
    phrases = list(baseline)
    folded = {fold_semantic(item) for item in baseline}
    for strategy in selected.strategies:
        if not strategy.approved:
            continue
        candidates = (*strategy.names, *strategy.labels, *strategy.terms)
        for candidate in candidates:
            normalized = fold_semantic(candidate)
            if normalized not in folded:
                phrases.append(candidate)
                folded.add(normalized)
    return tuple(phrases)
