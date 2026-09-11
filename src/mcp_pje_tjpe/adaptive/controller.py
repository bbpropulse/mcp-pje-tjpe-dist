from __future__ import annotations

import asyncio
import hmac
import re
import secrets
from typing import Any, cast
from urllib.parse import unquote, urlparse

from playwright.async_api import Page

from mcp_pje_tjpe.adaptive.adapters import AdapterRegistry, semantic_words
from mcp_pje_tjpe.adaptive.events import ObservationStore, observation_signature_payload
from mcp_pje_tjpe.adaptive.models import (
    CodigoFalha,
    ControleEstrutural,
    ObservacaoNavegacao,
    ResultadoReplayAdaptador,
    StatusNavegacaoAdaptativa,
    ValidacaoAdaptadoresOffline,
)
from mcp_pje_tjpe.adaptive.resolver import active_semantic_phrases, evaluate_step
from mcp_pje_tjpe.config import ModoAdaptativo
from mcp_pje_tjpe.errors import ServicoIndisponivelError
from mcp_pje_tjpe.tribunals import TribunalCodigo, obter_instancia

_SAFE_TAGS = {"input", "button", "select", "textarea", "a", "div", "span"}
_SAFE_ROLES = {"button", "textbox", "searchbox", "combobox", "link"}
_SAFE_TYPES = {
    "text",
    "search",
    "tel",
    "email",
    "number",
    "date",
    "submit",
    "button",
    "select-one",
    "textarea",
}
_DYNAMIC_SEGMENT = re.compile(r"(?:\d|[A-Fa-f0-9]{16,}|[A-Za-z0-9_-]{32,})")
_SAFE_PATH_SEGMENTS = frozenset(
    {
        "1g",
        "2g",
        "consulta-terceiros",
        "consultaprocesso",
        "consultapublica",
        "detalhe",
        "listautosdigitais.seam",
        "listview.seam",
        "login.seam",
        "pje",
        "pjeconsulta",
        "pjekz",
        "primeirograu",
        "processo",
        "segundograu",
    }
)


class AdaptiveNavigation:
    def __init__(
        self,
        registry: AdapterRegistry,
        store: ObservationStore,
        mode: ModoAdaptativo,
    ) -> None:
        self.registry = registry
        self.store = store
        self.mode = mode
        self._last_capture_error: str | None = None
        self._capture_sequence = 0
        self._state_sequence = 0
        self._state_lock = asyncio.Lock()

    def semantic_phrases(
        self,
        tribunal: TribunalCodigo,
        instancia: str,
        flow: str,
        step: str,
        baseline: tuple[str, ...],
    ) -> tuple[str, ...]:
        adapter = self.registry.get(tribunal, flow)
        if instancia not in adapter.environments:
            raise ValueError("o adaptador não pertence à instância informada")
        return active_semantic_phrases(adapter, step, self.mode, baseline)

    async def record_discovery_failure(
        self,
        page: Page,
        *,
        tribunal: TribunalCodigo,
        instancia: str,
        flow: str,
        step: str,
        failure_code: CodigoFalha,
        side_effect_boundary_crossed: bool = False,
    ) -> ObservacaoNavegacao | None:
        sequence = await self._begin_capture()
        if side_effect_boundary_crossed:
            await self._finish_capture(sequence, "captura recusada após limite de efeito")
            return None
        try:
            observation = await self._build_observation(
                page,
                tribunal=tribunal,
                instancia=instancia,
                flow=flow,
                step=step,
                failure_code=failure_code,
            )
            if not await self.store.append(observation):
                await self._finish_capture(sequence, self.store.last_error)
                return None
        except Exception:
            await self._finish_capture(
                sequence,
                "não foi possível sanitizar a falha de navegação",
            )
            return None
        await self._finish_capture(sequence, None)
        return observation

    async def _begin_capture(self) -> int:
        async with self._state_lock:
            self._capture_sequence += 1
            return self._capture_sequence

    async def _finish_capture(self, sequence: int, error: str | None) -> None:
        async with self._state_lock:
            if sequence >= self._state_sequence:
                self._state_sequence = sequence
                self._last_capture_error = error

    async def status(self) -> StatusNavegacaoAdaptativa:
        observations = await self.store.count()
        adapters = self.registry.list()
        async with self._state_lock:
            capture_error = self._last_capture_error
        return StatusNavegacaoAdaptativa(
            modo=self.mode,
            observacoes=observations,
            limite_observacoes=self.store.max_events,
            adaptadores_carregados=len(adapters),
            adaptadores_active=sum(adapter.approved_for_active for adapter in adapters),
            ultimo_erro_local=capture_error or self.store.last_error,
            aviso=(
                "O aprendizado registra somente estrutura sanitizada. Nenhuma estratégia local "
                "é promovida automaticamente; active usa apenas YAML empacotado e revisado."
            ),
        )

    async def list_observations(self, *, limit: int = 20) -> list[ObservacaoNavegacao]:
        observations = await self.store.list(limit=limit)
        if self.store.last_error is not None:
            raise ServicoIndisponivelError(
                "o JSONL adaptativo local não pôde ser lido com segurança"
            )
        try:
            for observation in observations:
                self._validate_stored_observation(observation)
        except Exception:
            raise ServicoIndisponivelError(
                "o JSONL adaptativo local falhou na validação de integridade"
            ) from None
        return observations

    async def validate_offline(self, *, limit: int = 100) -> ValidacaoAdaptadoresOffline:
        observations = await self.list_observations(limit=min(limit, self.store.max_events))
        results: list[ResultadoReplayAdaptador] = []
        for observation in observations:
            adapter = self.registry.get(observation.tribunal, observation.fluxo)
            evaluations = evaluate_step(adapter, observation.etapa, observation.controles)
            results.append(
                ResultadoReplayAdaptador(
                    referencia_observacao=observation.referencia,
                    adapter_id=adapter.adapter_id,
                    etapa=observation.etapa,
                    estrategias=evaluations,
                    candidato_unico=(
                        sum(evaluation.quantidade_aceita for evaluation in evaluations) == 1
                    ),
                )
            )
        return ValidacaoAdaptadoresOffline(
            observacoes_avaliadas=len(results),
            resultados=results,
            aviso=(
                "Replay executado apenas sobre estrutura sanitizada local, sem navegador, "
                "requisição, preenchimento ou clique."
            ),
        )

    async def _build_observation(
        self,
        page: Page,
        *,
        tribunal: TribunalCodigo,
        instancia: str,
        flow: str,
        step: str,
        failure_code: CodigoFalha,
    ) -> ObservacaoNavegacao:
        if page.is_closed():
            raise ValueError("a página foi encerrada")
        instance = obter_instancia(tribunal, instancia)
        parsed = urlparse(page.url)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("a página possui porta inválida") from exc
        if (
            parsed.scheme != "https"
            or parsed.hostname not in set(instance.hosts_permitidos)
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
        ):
            raise ValueError("a página está fora do host esperado")
        decoded_path = unquote(parsed.path)
        instance_root = unquote(urlparse(instance.entrada_url).path).rstrip("/")
        if decoded_path != instance_root and not decoded_path.startswith(instance_root + "/"):
            raise ValueError("a página está fora da raiz da instância")
        raw_controls = cast(
            list[dict[str, object]],
            await page.locator(
                "input, button, select, textarea, a, [role='button'], [role='textbox'], "
                "[role='searchbox'], [role='combobox']"
            ).evaluate_all(
                r"""
                (elements) => elements.slice(0, 100).map((element) => {
                  const tag = element.tagName.toLowerCase();
                  const explicitRole = (element.getAttribute('role') || '').toLowerCase();
                  const type = (element.type || '').toLowerCase();
                  let role = explicitRole;
                  if (!role && (tag === 'button' || type === 'submit' || type === 'button')) {
                    role = 'button';
                  } else if (!role && tag === 'a') {
                    role = 'link';
                  } else if (!role && tag === 'select') {
                    role = 'combobox';
                  } else if (!role && tag === 'input') {
                    role = type === 'search' ? 'searchbox' : 'textbox';
                  }
                  const labels = [...(element.labels || [])]
                    .map((label) => label.textContent || '').join(' ');
                  const buttonText = tag === 'input' && ['submit', 'button'].includes(type)
                    ? (element.value || '')
                    : (['button', 'a', 'div', 'span'].includes(tag)
                      ? (element.innerText || element.textContent || '') : '');
                  const style = window.getComputedStyle(element);
                  const visible = element.getClientRects().length > 0 &&
                    style.visibility !== 'hidden' && style.display !== 'none';
                  return {
                    tag,
                    role,
                    type: type || (tag === 'select' ? 'select-one' : tag),
                    visible,
                    enabled: !Boolean(element.disabled),
                    editable: ['input', 'textarea', 'select'].includes(tag)
                      ? !Boolean(element.disabled) && !Boolean(element.readOnly) : null,
                    nativeSubmit: type === 'submit',
                    ownsForm: element.form instanceof HTMLFormElement,
                    semanticText: [
                      element.getAttribute('aria-label') || '',
                      element.getAttribute('placeholder') || '',
                      labels,
                      buttonText
                    ].join(' ').slice(0, 500),
                    identifier: [element.id || '', element.getAttribute('name') || '']
                      .join('\0').slice(0, 500)
                  };
                })
                """
            ),
        )
        controls = self.sanitize_controls(raw_controls)
        adapter = self.registry.get(tribunal, flow)
        if instancia not in adapter.environments:
            raise ValueError("o adaptador não pertence à instância informada")
        evaluations = evaluate_step(adapter, step, controls)
        origin = f"https://{parsed.hostname}"
        path_shape = self._path_shape(parsed.path)
        unsigned = ObservacaoNavegacao(
            referencia=secrets.token_urlsafe(24),
            tribunal=tribunal,
            instancia=instancia,
            fluxo=flow,
            etapa=step,
            modo=self.mode,
            origem=origin,
            formato_caminho=path_shape,
            assinatura_pagina="0" * 64,
            codigo_falha=failure_code,
            estrategias=evaluations,
            controles=controls,
        )
        return self.store.seal(unsigned)

    def sanitize_controls(
        self,
        raw_controls: list[dict[str, object]],
    ) -> tuple[ControleEstrutural, ...]:
        controls: list[ControleEstrutural] = []
        vocabulary = self.registry.vocabulary
        for raw in raw_controls[:100]:
            tag_value = raw.get("tag")
            if not isinstance(tag_value, str) or tag_value not in _SAFE_TAGS:
                continue
            role_value = raw.get("role")
            role = role_value if isinstance(role_value, str) and role_value in _SAFE_ROLES else None
            type_value = raw.get("type")
            control_type = (
                type_value
                if isinstance(type_value, str) and type_value in _SAFE_TYPES
                else "unknown"
            )
            semantic_text = raw.get("semanticText")
            words = semantic_words(semantic_text if isinstance(semantic_text, str) else "")
            unique_words = set(words)
            tokens = tuple(sorted(unique_words.intersection(vocabulary)))[:16]
            unknown_count = min(len(unique_words.difference(vocabulary)), 100)
            identifier = raw.get("identifier")
            signature = None
            if isinstance(identifier, str) and identifier.strip("\0"):
                signature = self.store.sign(identifier.encode("utf-8", errors="replace"))
            editable = raw.get("editable")
            controls.append(
                ControleEstrutural(
                    tag=cast(Any, tag_value),
                    role=cast(Any, role),
                    tipo=cast(Any, control_type),
                    visivel=raw.get("visible") is True,
                    habilitado=raw.get("enabled") is True,
                    editavel=editable if isinstance(editable, bool) else None,
                    submit_nativo=raw.get("nativeSubmit") is True,
                    possui_formulario=raw.get("ownsForm") is True,
                    tokens_semanticos=tokens,
                    tokens_desconhecidos=unknown_count,
                    assinatura_identificador=signature,
                )
            )
        return tuple(controls)

    def _validate_stored_observation(self, observation: ObservacaoNavegacao) -> None:
        adapter = self.registry.get(observation.tribunal, observation.fluxo)
        if observation.instancia not in adapter.environments:
            raise ValueError("observação pertence a outro ambiente")
        instance = obter_instancia(observation.tribunal, observation.instancia)
        allowed_origins = {f"https://{host}" for host in instance.hosts_permitidos}
        if observation.origem not in allowed_origins:
            raise ValueError("observação pertence a outra origem")
        for control in observation.controles:
            if tuple(sorted(set(control.tokens_semanticos))) != control.tokens_semanticos:
                raise ValueError("observação contém vocabulário estrutural inválido")
        expected = self.store.sign(observation_signature_payload(observation))
        if not hmac.compare_digest(expected, observation.assinatura_pagina):
            raise ValueError("assinatura estrutural da observação é inválida")

    @staticmethod
    def _path_shape(path: str) -> str:
        decoded = unquote(path)
        if any(character in decoded for character in "\r\n\\") or ".." in decoded.split("/"):
            raise ValueError("caminho não pode ser sanitizado")
        segments: list[str] = []
        for segment in decoded.split("/"):
            if not segment:
                continue
            if len(segment) > 80 or _DYNAMIC_SEGMENT.search(segment):
                segments.append(":dynamic")
            elif segment.casefold() in _SAFE_PATH_SEGMENTS:
                segments.append(segment.casefold())
            else:
                segments.append(":segment")
        return "/" + "/".join(segments)
