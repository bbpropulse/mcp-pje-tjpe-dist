from __future__ import annotations

# pyright: reportUnknownMemberType=false
import re
import unicodedata
from collections.abc import Iterator
from importlib.resources import files
from typing import Literal, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode
from yaml.resolver import BaseResolver
from yaml.tokens import AliasToken, AnchorToken, TagToken

from mcp_pje_tjpe.tribunals import (
    MaturidadeAdaptador,
    TribunalCodigo,
    obter_perfil_tribunal,
)

_MAX_ADAPTER_BYTES = 64 * 1024
_BUNDLED_ADAPTERS = (
    "tjpe-pesquisa-geral-v1.yaml",
    "trt6-pesquisa-geral-v1.yaml",
    "trf5-pesquisa-geral-v1.yaml",
)
_SAFE_PHRASE = re.compile(r"^[0-9A-Za-zÀ-ÖØ-öø-ÿ _-]{2,80}$")


def fold_semantic(value: str) -> str:
    normalized = unicodedata.normalize("NFD", value)
    return " ".join(
        "".join(character for character in normalized if not unicodedata.combining(character))
        .casefold()
        .split()
    )


def semantic_words(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z]{3,30}", fold_semantic(value)))


class RestricoesDescoberta(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    native_submit: bool | None = None
    visible: bool = True
    enabled: bool = True
    unique: bool = True
    owns_form: bool | None = None
    input_types: tuple[
        Literal["text", "search", "tel", "email", "number", "date", "submit", "button"],
        ...,
    ] = ()
    editable: bool | None = None
    count: tuple[int, ...] = (1,)

    @field_validator("count")
    @classmethod
    def validate_count(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value or len(value) > 4 or len(set(value)) != len(value):
            raise ValueError("count deve conter de uma a quatro quantidades únicas")
        if any(item < 1 or item > 10 for item in value):
            raise ValueError("count possui quantidade fora do limite")
        return value


class EstrategiaDescoberta(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,79}$")
    kind: Literal["role_exact", "label_exact", "input_metadata"]
    approved: bool = False
    role: Literal["button", "textbox", "searchbox", "combobox", "link"] | None = None
    names: tuple[str, ...] = Field(default=(), max_length=8)
    labels: tuple[str, ...] = Field(default=(), max_length=8)
    terms: tuple[str, ...] = Field(default=(), max_length=8)
    constraints: RestricoesDescoberta

    @field_validator("names", "labels", "terms")
    @classmethod
    def validate_phrases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        folded: list[str] = []
        for item in value:
            compact = " ".join(item.split())
            if _SAFE_PHRASE.fullmatch(compact) is None:
                raise ValueError("a DSL aceita somente frases semânticas curtas")
            normalized = fold_semantic(compact)
            if not semantic_words(normalized):
                raise ValueError("a frase precisa conter ao menos uma palavra semântica")
            if normalized in folded:
                raise ValueError("a estratégia contém frase duplicada")
            folded.append(normalized)
        return value

    @model_validator(mode="after")
    def validate_shape(self) -> EstrategiaDescoberta:
        if self.kind == "role_exact":
            if self.role is None or not self.names or self.labels or self.terms:
                raise ValueError("role_exact exige role e names, sem labels ou terms")
        elif self.kind == "label_exact":
            if not self.labels or self.role is not None or self.names or self.terms:
                raise ValueError("label_exact exige labels e não aceita role, names ou terms")
        elif not self.terms or self.role is not None or self.names or self.labels:
            raise ValueError("input_metadata exige terms e não aceita role, names ou labels")
        return self


class EtapaAdaptador(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    strategies: tuple[EstrategiaDescoberta, ...] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> EtapaAdaptador:
        identifiers = [strategy.id for strategy in self.strategies]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("a etapa contém IDs de estratégia duplicados")
        return self


class AdaptadorNavegacao(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    adapter_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{5,100}$")
    tribunal: TribunalCodigo
    system: Literal["pje"]
    flow: str = Field(pattern=r"^[a-z0-9_]{3,80}$")
    environments: tuple[str, ...] = Field(min_length=1, max_length=8)
    approved_for_active: bool = False
    steps: dict[str, EtapaAdaptador] = Field(min_length=1, max_length=12)

    @field_validator("environments")
    @classmethod
    def validate_environments(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or any(
            re.fullmatch(r"[a-z0-9_]{2,40}", item) is None for item in value
        ):
            raise ValueError("environments contém código inválido ou duplicado")
        return value

    @field_validator("steps")
    @classmethod
    def validate_step_names(cls, value: dict[str, EtapaAdaptador]) -> dict[str, EtapaAdaptador]:
        if any(re.fullmatch(r"[a-z0-9_]{3,80}", name) is None for name in value):
            raise ValueError("steps contém nome fora da DSL")
        return value

    @model_validator(mode="after")
    def validate_active_policy(self) -> AdaptadorNavegacao:
        if self.approved_for_active and not any(
            strategy.approved for step in self.steps.values() for strategy in step.strategies
        ):
            raise ValueError("adaptador active precisa de ao menos uma estratégia aprovada")
        return self


class _StrictSafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _StrictSafeLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    loader.flatten_mapping(node)
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = cast(object, loader.construct_object(key_node, deep=deep))
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "chave YAML não é escalar",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"chave YAML duplicada: {key!r}",
                key_node.start_mark,
            )
        mapping[key] = cast(object, loader.construct_object(value_node, deep=deep))
    return mapping


_StrictSafeLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def parse_adapter_yaml(text: str, *, source: str = "adaptador") -> AdaptadorNavegacao:
    encoded = text.encode("utf-8")
    if not encoded or len(encoded) > _MAX_ADAPTER_BYTES:
        raise ValueError(f"{source} está vazio ou excede o limite local")
    tokens = cast(Iterator[object], yaml.scan(text))
    if any(isinstance(token, AliasToken | AnchorToken | TagToken) for token in tokens):
        raise ValueError(f"{source} usa alias, âncora ou tag YAML não permitida")
    raw = yaml.load(text, Loader=_StrictSafeLoader)
    if not isinstance(raw, dict):
        raise ValueError(f"{source} não possui objeto YAML na raiz")
    return AdaptadorNavegacao.model_validate(cast(dict[str, object], raw))


class AdapterRegistry:
    def __init__(self, adapters: tuple[AdaptadorNavegacao, ...] | None = None) -> None:
        selected = adapters if adapters is not None else self._load_bundled()
        keys: set[tuple[TribunalCodigo, str]] = set()
        identifiers: set[str] = set()
        for adapter in selected:
            profile = obter_perfil_tribunal(adapter.tribunal)
            known_environments = {instance.codigo for instance in profile.instancias}
            if not set(adapter.environments).issubset(known_environments):
                raise ValueError("adaptador declara ambiente fora do catálogo do tribunal")
            if (
                adapter.approved_for_active
                and profile.maturidade is not MaturidadeAdaptador.OPERACIONAL
            ):
                raise ValueError("tribunal em descoberta não pode ter adaptador active")
            key = (adapter.tribunal, adapter.flow)
            if key in keys or adapter.adapter_id in identifiers:
                raise ValueError("registro contém adaptador duplicado")
            keys.add(key)
            identifiers.add(adapter.adapter_id)
        self._adapters = {adapter.adapter_id: adapter for adapter in selected}
        self._by_flow = {(adapter.tribunal, adapter.flow): adapter for adapter in selected}

    @staticmethod
    def _load_bundled() -> tuple[AdaptadorNavegacao, ...]:
        root = files("mcp_pje_tjpe").joinpath("adapters")
        loaded: list[AdaptadorNavegacao] = []
        for filename in _BUNDLED_ADAPTERS:
            text = root.joinpath(filename).read_text(encoding="utf-8")
            loaded.append(parse_adapter_yaml(text, source=filename))
        return tuple(loaded)

    def list(self) -> tuple[AdaptadorNavegacao, ...]:
        return tuple(self._adapters.values())

    def get(self, tribunal: TribunalCodigo, flow: str) -> AdaptadorNavegacao:
        try:
            return self._by_flow[(tribunal, flow)]
        except KeyError:
            raise ValueError("não há adaptador empacotado para tribunal e fluxo") from None

    @property
    def vocabulary(self) -> frozenset[str]:
        words: set[str] = set()
        for adapter in self._adapters.values():
            for step in adapter.steps.values():
                for strategy in step.strategies:
                    for phrase in (*strategy.names, *strategy.labels, *strategy.terms):
                        words.update(semantic_words(phrase))
        return frozenset(words)
