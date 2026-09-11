"""Cliente da API pública DataJud (CNJ) — metadados processuais sem navegador.

O DataJud publica apenas metadados de nível público: classe, assuntos, órgão
julgador, datas e movimentações. Não traz partes, advogados, CPF/CNPJ nem
documentos, e não exige sessão, certificado ou MFA. É complementar à leitura
autenticada do PJe, nunca substituta dela.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import re
from datetime import UTC, datetime
from typing import cast

from mcp_pje_tjpe.config import Settings
from mcp_pje_tjpe.errors import (
    ServicoIndisponivelError,
    ValidacaoError,
)
from mcp_pje_tjpe.models import MetadadosProcesso, MovimentoDatajud
from mcp_pje_tjpe.tribunals import TribunalCodigo, validar_npu_tribunal

HOST = "api-publica.datajud.cnj.jus.br"
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_MOVIMENTOS = 500
# O índice é derivado do código do tribunal do catálogo, nunca de entrada do usuário.
_INDEX_PREFIX = "api_publica_"
_COMPACT_DATE = re.compile(r"^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})$")

AVISO = (
    "Metadados públicos da API DataJud do CNJ. Não contém partes, advogados, CPF/CNPJ "
    "nem documentos, e não substitui a leitura autenticada dos Autos. Nenhum acesso é "
    "registrado no PJe por esta consulta. Os dados refletem a última carga que o "
    "tribunal enviou ao CNJ, que pode estar atrás do PJe."
)


def _index_for(codigo: TribunalCodigo) -> str:
    return f"{_INDEX_PREFIX}{codigo.value}"


def _normalize_datetime(value: object) -> str | None:
    """Uniformiza as duas formas que o DataJud usa para data.

    `dataAjuizamento` chega compacto (`20170424000000`) e `dataHoraUltimaAtualizacao`
    em ISO 8601; devolver as duas no mesmo formato evita que o consumidor tenha de
    conhecer essa diferença.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    texto = value.strip()
    match = _COMPACT_DATE.fullmatch(texto)
    if match is None:
        return texto
    year, month, day, hour, minute, second = match.groups()
    try:
        moment = datetime(
            int(year), int(month), int(day), int(hour), int(minute), int(second), tzinfo=UTC
        )
    except ValueError:
        return texto
    return moment.isoformat().replace("+00:00", "Z")


def _named(value: object) -> str | None:
    if isinstance(value, dict):
        nome = cast(dict[str, object], value).get("nome")
        return nome.strip() if isinstance(nome, str) and nome.strip() else None
    return None


def _coded(value: object) -> int | None:
    if isinstance(value, dict):
        codigo = cast(dict[str, object], value).get("codigo")
        if isinstance(codigo, int):
            return codigo
        if isinstance(codigo, str) and codigo.isdigit():
            return int(codigo)
    return None


def _parse_movimentos(raw: object, limite: int) -> tuple[list[MovimentoDatajud], int, bool]:
    if not isinstance(raw, list):
        return [], 0, False
    entries = cast(list[object], raw)
    ordenados: list[tuple[str, MovimentoDatajud]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        item = cast(dict[str, object], entry)
        nome = item.get("nome")
        if not isinstance(nome, str) or not nome.strip():
            continue
        quando = _normalize_datetime(item.get("dataHora")) or ""
        ordenados.append(
            (
                quando,
                MovimentoDatajud(
                    data_hora=quando,
                    codigo=_coded(item) if isinstance(item.get("codigo"), int | str) else None,
                    nome=nome.strip(),
                    orgao_julgador=_named(item.get("orgaoJulgador")),
                ),
            )
        )
    # Mais recentes primeiro: é a ordem em que um advogado lê a movimentação.
    ordenados.sort(key=lambda par: par[0], reverse=True)
    total = len(ordenados)
    selecionados = [movimento for _, movimento in ordenados[:limite]]
    return selecionados, total, total > limite


class DatajudClient:
    """Consulta metadados públicos por NPU exato, sem navegador e sem sessão."""

    def __init__(self, config: Settings) -> None:
        self.config = config

    async def consultar_metadados(
        self,
        numero: str,
        tribunal: TribunalCodigo,
        *,
        limite_movimentos: int = 100,
    ) -> MetadadosProcesso:
        if not 1 <= limite_movimentos <= _MAX_MOVIMENTOS:
            raise ValidacaoError(
                f"limite_movimentos deve estar entre 1 e {_MAX_MOVIMENTOS}",
            )
        formatado = validar_npu_tribunal(tribunal, numero)
        digitos = re.sub(r"\D", "", formatado)

        # A consulta é montada aqui: o chamador informa um NPU, nunca uma query.
        corpo = json.dumps({"size": 1, "query": {"match": {"numeroProcesso": digitos}}})
        payload = await asyncio.to_thread(
            _post_search,
            _index_for(tribunal),
            corpo,
            api_key=self.config.datajud_api_key,
            timeout_seconds=max(self.config.timeout_ms / 1_000, 1.0),
        )
        return _to_model(payload, formatado, tribunal, limite_movimentos)


def _to_model(
    payload: dict[str, object],
    numero: str,
    tribunal: TribunalCodigo,
    limite_movimentos: int,
) -> MetadadosProcesso:
    hits_root = payload.get("hits")
    if not isinstance(hits_root, dict):
        raise ServicoIndisponivelError("a resposta do DataJud não trouxe a estrutura esperada")
    hits = cast(dict[str, object], hits_root).get("hits")
    if not isinstance(hits, list) or not hits:
        raise ServicoIndisponivelError(
            f"o DataJud não possui registro público do processo {numero} em "
            f"{tribunal.value.upper()}; a base do CNJ pode estar atrás do tribunal"
        )
    first = cast(list[object], hits)[0]
    if not isinstance(first, dict):
        raise ServicoIndisponivelError("a resposta do DataJud não trouxe a estrutura esperada")
    source_raw = cast(dict[str, object], first).get("_source")
    if not isinstance(source_raw, dict):
        raise ServicoIndisponivelError("a resposta do DataJud não trouxe a estrutura esperada")
    source = cast(dict[str, object], source_raw)

    # O DataJud publica somente nível 0 (público). Um valor diferente significaria que
    # a base mudou de política; o servidor recusa em vez de decidir por conta própria.
    nivel = source.get("nivelSigilo")
    nivel_sigilo = nivel if isinstance(nivel, int) else 0
    if nivel_sigilo != 0:
        raise ServicoIndisponivelError(
            f"o DataJud devolveu nível de sigilo {nivel_sigilo} para {numero}; "
            "esta ferramenta só entrega metadados de nível público"
        )

    devolvido = source.get("numeroProcesso")
    if isinstance(devolvido, str) and re.sub(r"\D", "", devolvido) != re.sub(r"\D", "", numero):
        raise ServicoIndisponivelError(
            "o DataJud devolveu um processo diferente do consultado"
        )

    assuntos_raw = cast(list[object], source.get("assuntos") or [])
    assuntos = [nome for nome in (_named(item) for item in assuntos_raw) if nome]
    movimentos, total, truncados = _parse_movimentos(source.get("movimentos"), limite_movimentos)
    tribunal_devolvido = source.get("tribunal")
    grau = source.get("grau")

    return MetadadosProcesso(
        numero=numero,
        tribunal=(
            tribunal_devolvido
            if isinstance(tribunal_devolvido, str) and tribunal_devolvido
            else tribunal.value.upper()
        ),
        grau=grau if isinstance(grau, str) and grau else None,
        sistema=_named(source.get("sistema")),
        formato=_named(source.get("formato")),
        classe=_named(source.get("classe")),
        codigo_classe=_coded(source.get("classe")),
        assuntos=assuntos,
        orgao_julgador=_named(source.get("orgaoJulgador")),
        data_ajuizamento=_normalize_datetime(source.get("dataAjuizamento")),
        ultima_atualizacao=_normalize_datetime(source.get("dataHoraUltimaAtualizacao")),
        nivel_sigilo=nivel_sigilo,
        movimentos=movimentos,
        total_movimentos=total,
        movimentos_truncados=truncados,
        aviso=AVISO,
    )


def _post_search(
    index: str,
    body: str,
    *,
    api_key: str,
    timeout_seconds: float,
) -> dict[str, object]:
    """POST único ao host fixado do CNJ, sem seguir redirecionamento e com teto de bytes."""
    if not api_key or not api_key.isascii() or any(c in api_key for c in "\r\n"):
        raise ValidacaoError(
            "a chave da API DataJud está ausente ou inválida; defina PJE_TJPE_DATAJUD_API_KEY"
        )
    if re.fullmatch(r"api_publica_[a-z0-9]{3,12}", index) is None:
        raise ValidacaoError("índice DataJud inválido")

    # A criação da conexão fica dentro do try: falha de resolução ou de socket também
    # precisa virar erro de domínio, e não escapar crua para o cliente MCP.
    connection: http.client.HTTPSConnection | None = None
    try:
        connection = http.client.HTTPSConnection(HOST, 443, timeout=timeout_seconds)
        connection.request(
            "POST",
            f"/{index}/_search",
            body=body.encode("utf-8"),
            headers={
                "Authorization": f"APIKey {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "User-Agent": "mcp-pje-tjpe",
            },
        )
        response = connection.getresponse()
        if response.status in {301, 302, 303, 307, 308}:
            raise ServicoIndisponivelError(
                "o DataJud tentou redirecionar a consulta; nenhum redirecionamento foi seguido"
            )
        if response.status in {401, 403}:
            raise ServicoIndisponivelError(
                "o DataJud recusou a chave pública; ela pode ter sido rotacionada pelo CNJ. "
                "Obtenha a vigente em datajud-wiki.cnj.jus.br/api-publica/acesso e defina "
                "PJE_TJPE_DATAJUD_API_KEY"
            )
        if response.status == 429:
            raise ServicoIndisponivelError(
                "o DataJud limitou a taxa de consultas; aguarde antes de repetir"
            )
        if response.status != 200:
            raise ServicoIndisponivelError(f"o DataJud respondeu HTTP {response.status}")
        raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except (TimeoutError, OSError, http.client.HTTPException) as exc:
        raise ServicoIndisponivelError("não foi possível falar com a API DataJud do CNJ") from exc
    finally:
        if connection is not None:
            connection.close()

    if len(raw) > _MAX_RESPONSE_BYTES:
        raise ServicoIndisponivelError("a resposta do DataJud excedeu o limite local")
    try:
        parsed: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ServicoIndisponivelError("o DataJud devolveu conteúdo que não é JSON") from exc
    if not isinstance(parsed, dict):
        raise ServicoIndisponivelError("o DataJud devolveu conteúdo que não é um objeto JSON")
    return cast(dict[str, object], parsed)
