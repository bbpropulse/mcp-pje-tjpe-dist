from __future__ import annotations

import pytest
from yaml.constructor import ConstructorError

from mcp_pje_tjpe.adaptive.adapters import (
    AdapterRegistry,
    parse_adapter_yaml,
    semantic_words,
)
from mcp_pje_tjpe.tribunals import TribunalCodigo


def _adapter_yaml(
    *,
    adapter_id: str = "tjpe-test-adapter-v1",
    tribunal: str = "tjpe",
    flow: str = "pesquisa_geral",
    approved_for_active: str = "true",
    strategy_approved: str = "true",
) -> str:
    return f"""
schema_version: 1
adapter_id: {adapter_id}
tribunal: {tribunal}
system: pje
flow: {flow}
environments:
  - tjpe_1g
approved_for_active: {approved_for_active}
steps:
  search_submit:
    strategies:
      - id: role-button-pesquisar
        kind: role_exact
        approved: {strategy_approved}
        role: button
        names:
          - Pesquisar
        constraints:
          native_submit: true
          visible: true
          enabled: true
          unique: true
          owns_form: true
          count:
            - 1
"""


def test_parse_adapter_yaml_accepts_only_the_closed_discovery_dsl() -> None:
    adapter = parse_adapter_yaml(_adapter_yaml(), source="teste.yaml")

    assert adapter.adapter_id == "tjpe-test-adapter-v1"
    assert adapter.tribunal is TribunalCodigo.TJPE
    assert adapter.approved_for_active is True
    strategy = adapter.steps["search_submit"].strategies[0]
    assert strategy.kind == "role_exact"
    assert strategy.names == ("Pesquisar",)
    assert strategy.constraints.count == (1,)


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_root: true\n",
        "steps:\n  bad-step!:\n    strategies: []\n",
        "system: outro\n",
    ],
)
def test_parse_adapter_yaml_rejects_unknown_or_out_of_schema_fields(mutation: str) -> None:
    if mutation.startswith("steps:") or mutation.startswith("system:"):
        key = mutation.split(":", 1)[0]
        source = _adapter_yaml().replace(
            next(line for line in _adapter_yaml().splitlines() if line.startswith(f"{key}:")),
            mutation.rstrip(),
            1,
        )
    else:
        source = _adapter_yaml() + mutation

    with pytest.raises(ValueError):
        parse_adapter_yaml(source)


def test_parse_adapter_yaml_rejects_duplicate_mapping_keys() -> None:
    source = _adapter_yaml().replace(
        "schema_version: 1",
        "schema_version: 1\nschema_version: 1",
        1,
    )

    with pytest.raises(ConstructorError, match="duplicada"):
        parse_adapter_yaml(source)


@pytest.mark.parametrize(
    "source",
    [
        "root: &shared {value: 1}\ncopy: *shared\n",
        "schema_version: !!int 1\n",
    ],
)
def test_parse_adapter_yaml_rejects_anchors_aliases_and_explicit_tags(source: str) -> None:
    with pytest.raises(ValueError, match="alias, âncora ou tag"):
        parse_adapter_yaml(source)


@pytest.mark.parametrize("source", ["", "- item\n", "x" * (64 * 1024 + 1)])
def test_parse_adapter_yaml_rejects_empty_non_mapping_and_oversized_input(source: str) -> None:
    with pytest.raises(ValueError):
        parse_adapter_yaml(source)


def test_parse_adapter_yaml_rejects_executable_or_selector_like_phrases() -> None:
    for unsafe in (
        "document.querySelector",
        "//button[@type='submit']",
        "https://evil.example/action",
        "Pesquisar();",
    ):
        source = _adapter_yaml().replace("          - Pesquisar", f"          - {unsafe}")
        with pytest.raises(ValueError):
            parse_adapter_yaml(source)


def test_parse_adapter_yaml_rejects_active_adapter_without_approved_strategy() -> None:
    with pytest.raises(ValueError, match="estratégia aprovada"):
        parse_adapter_yaml(_adapter_yaml(strategy_approved="false"))


def test_parse_adapter_yaml_rejects_duplicate_strategy_ids_and_counts() -> None:
    duplicate_strategy = """      - id: role-button-pesquisar
        kind: role_exact
        approved: true
        role: button
        names: [Consultar]
        constraints:
          count: [1]
"""
    source = _adapter_yaml().replace(
        "            - 1\n",
        "            - 1\n" + duplicate_strategy,
        1,
    )
    with pytest.raises(ValueError):
        parse_adapter_yaml(source)

    duplicate_count = _adapter_yaml().replace("            - 1", "            - 1\n            - 1")
    with pytest.raises(ValueError, match="quantidades únicas"):
        parse_adapter_yaml(duplicate_count)


def test_registry_loads_only_bundled_reviewed_adapters() -> None:
    registry = AdapterRegistry()
    adapters = registry.list()

    assert {adapter.tribunal for adapter in adapters} == {
        TribunalCodigo.TJPE,
        TribunalCodigo.TRT6,
        TribunalCodigo.TRF5,
    }
    assert sum(adapter.approved_for_active for adapter in adapters) == 1
    assert registry.get(TribunalCodigo.TJPE, "pesquisa_geral").approved_for_active is True
    assert registry.get(TribunalCodigo.TRT6, "pesquisa_geral").approved_for_active is False
    assert "pesquisar" in registry.vocabulary
    assert "numero" in registry.vocabulary


def test_registry_rejects_duplicate_identifier_or_tribunal_flow() -> None:
    first = parse_adapter_yaml(_adapter_yaml())
    duplicate_flow = parse_adapter_yaml(
        _adapter_yaml(adapter_id="tjpe-other-adapter-v1", approved_for_active="false")
    )

    with pytest.raises(ValueError, match="duplicado"):
        AdapterRegistry((first, first))
    with pytest.raises(ValueError, match="duplicado"):
        AdapterRegistry((first, duplicate_flow))


def test_registry_rejects_unknown_environment_and_active_discovery_tribunal() -> None:
    unknown_environment = parse_adapter_yaml(_adapter_yaml().replace("  - tjpe_1g", "  - tjpe_3g"))
    with pytest.raises(ValueError, match="fora do catálogo"):
        AdapterRegistry((unknown_environment,))

    trt6_active = parse_adapter_yaml(
        _adapter_yaml(
            adapter_id="trt6-test-adapter-v1",
            tribunal="trt6",
        ).replace("  - tjpe_1g", "  - trt6_1g")
    )
    with pytest.raises(ValueError, match="em descoberta"):
        AdapterRegistry((trt6_active,))


def test_registry_unknown_flow_fails_closed() -> None:
    with pytest.raises(ValueError, match="não há adaptador"):
        AdapterRegistry().get(TribunalCodigo.TJPE, "fluxo_inexistente")


def test_semantic_words_folds_accents_and_discards_values_outside_word_shape() -> None:
    assert semantic_words(" Número do PROCESSO 123.456; ação ") == (
        "numero",
        "processo",
        "acao",
    )


def test_adapter_phrase_must_contain_a_semantic_word() -> None:
    source = _adapter_yaml().replace("          - Pesquisar", '          - "123_45"')

    with pytest.raises(ValueError, match="palavra semântica"):
        parse_adapter_yaml(source)
