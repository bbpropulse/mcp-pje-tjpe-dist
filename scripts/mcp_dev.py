#!/usr/bin/env python
"""Fala MCP com o servidor local, sem passar pelo Claude.

O servidor stdio é iniciado pelo cliente e vive enquanto a sessão dura, então
qualquer mudança de código só aparece depois de reconectar. Este script sobe um
processo novo a cada execução: é o jeito mais curto de ver o efeito de uma edição
no contrato real do MCP, e não só nos testes.

    uv run python scripts/mcp_dev.py tools
    uv run python scripts/mcp_dev.py schema consultar_metadados_processo
    uv run python scripts/mcp_dev.py call status_servidor
    uv run python scripts/mcp_dev.py call consultar_metadados_processo \
        numero=9999903-84.2099.8.17.9999 limite_movimentos=3

Argumentos são `chave=valor`; números e true/false/null são convertidos, e um
valor que comece com `{` ou `[` é lido como JSON.

Fluxos autenticados precisam de um processo só, porque a sessão do PJe morre com
ele. `chain` executa vários passos no mesmo servidor:

    uv run python scripts/mcp_dev.py chain \
        "abrir_login_certificado grau=1g" \
        "poll:autenticado=true:240 verificar_login_certificado grau=1g" \
        "listar_jurisdicoes_acervo grau=1g"

Passos especiais: `sleep:N` espera N segundos,
`foreach:@N.lista <ferramenta> arg=@item.campo` repete a chamada para cada item
(uma falha isolada não derruba o lote), e
`poll:<campo>=<valor>:<segundos> <ferramenta> [args]` repete a chamada a cada 5s
até o campo bater ou o prazo estourar.

Com `--keep`, o servidor não é encerrado ao fim dos passos: a janela do
navegador segue aberta e novos passos são lidos de stdin, um por linha. É o que
evita repetir certificado e PIN a cada chamada.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

RAIZ = Path(__file__).resolve().parent.parent
SERVIDOR = RAIZ / ".venv" / "bin" / "pje-tjpe"


def _coerce(texto: str) -> Any:
    if texto[:1] in "{[":
        return json.loads(texto)
    if texto in {"true", "false", "null"}:
        return {"true": True, "false": False, "null": None}[texto]
    try:
        return int(texto)
    except ValueError:
        pass
    try:
        return float(texto)
    except ValueError:
        return texto


class Servidor:
    """Um processo por execução: sempre o código que está no disco agora."""

    def __init__(self) -> None:
        comando = str(SERVIDOR) if SERVIDOR.exists() else shutil.which("pje-tjpe") or "pje-tjpe"
        self.proc = subprocess.Popen(
            [comando, "serve"],
            cwd=RAIZ,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._id = 0

    def _send(self, payload: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def _read(self) -> dict[str, Any] | None:
        assert self.proc.stdout is not None
        while True:
            linha = self.proc.stdout.readline()
            if not linha:
                return None
            linha = linha.strip()
            if linha.startswith("{"):
                return json.loads(linha)

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        self._send(
            {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        )
        resposta = self._read()
        if resposta is None:
            erro = (self.proc.stderr.read() if self.proc.stderr else "").strip()
            raise SystemExit(f"o servidor encerrou sem responder.\n{erro[:2000]}")
        return resposta

    def __enter__(self) -> Servidor:
        self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "mcp-dev", "version": "0"},
            },
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self

    def __exit__(self, *_: object) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def _tools(servidor: Servidor) -> list[dict[str, Any]]:
    return servidor.request("tools/list")["result"]["tools"]


def _resolver(valor: str, anteriores: list[Any]) -> Any:
    """`@2.jurisdicoes.0` lê o resultado do passo 2 — encadeia sem perder a sessão."""
    if not valor.startswith("@"):
        return _coerce(valor)
    indice, *caminho = valor[1:].split(".")
    atual: Any = anteriores[int(indice) - 1]
    for parte in caminho:
        atual = atual[int(parte)] if isinstance(atual, list) else atual[parte]
    return atual


def _chamar(
    servidor: Servidor, passo: str, anteriores: list[Any] | None = None
) -> tuple[bool, Any]:
    """Executa `<ferramenta> chave=valor ...` e devolve (ok, conteúdo).

    Usa shlex porque todo nome de jurisdição do TJPE tem espaço
    ("Recife - Varas"): um split simples partiria o valor em três argumentos.
    """
    nome, *pares = shlex.split(passo)
    argumentos = {
        chave: _resolver(valor, anteriores or [])
        for chave, _, valor in (par.partition("=") for par in pares)
    }
    resposta = servidor.request("tools/call", {"name": nome, "arguments": argumentos})
    if "error" in resposta:
        return False, resposta["error"]
    resultado = resposta["result"]
    conteudo = resultado.get("structuredContent")
    if conteudo is None:
        conteudo = "\n".join(bloco.get("text", "") for bloco in resultado.get("content", []))
    return not resultado.get("isError", False), conteudo


def _mostrar(rotulo: str, ok: bool, conteudo: Any) -> None:
    marca = "ok " if ok else "ERRO"
    print(f"\n=== [{marca}] {rotulo} ===")
    if isinstance(conteudo, str):
        print(conteudo[:4000])
    else:
        print(json.dumps(conteudo, ensure_ascii=False, indent=2)[:4000])


def _executar_chain(
    servidor: Servidor, passos: list[str], resultados: list[Any]
) -> int:
    import time

    for passo in passos:
        if passo.startswith("sleep:"):
            segundos = float(passo.split(":", 1)[1])
            print(f"\n=== aguardando {segundos:.0f}s ===", flush=True)
            time.sleep(segundos)
            resultados.append(None)
            continue

        if passo.startswith("foreach:"):
            codigo = _executar_foreach(servidor, passo, resultados)
            if codigo:
                return codigo
            continue

        if passo.startswith("poll:"):
            cabecalho, _, chamada = passo.partition(" ")
            _, condicao, prazo = cabecalho.split(":", 2)
            campo, _, esperado = condicao.partition("=")
            limite = time.monotonic() + float(prazo)
            tentativa = 0
            while True:
                tentativa += 1
                ok, conteudo = _chamar(servidor, chamada, resultados)
                atual = conteudo.get(campo) if isinstance(conteudo, dict) else None
                if ok and json.dumps(atual) == json.dumps(_coerce(esperado)):
                    _mostrar(f"{chamada} (tentativa {tentativa})", True, conteudo)
                    resultados.append(conteudo)
                    break
                if time.monotonic() >= limite:
                    rotulo = f"{chamada} — prazo esgotado após {tentativa} tentativas"
                    _mostrar(rotulo, False, conteudo)
                    return 1
                # Quando a chamada falha, o conteúdo não é o status: mostrar `None`
                # esconderia justamente o motivo pelo qual a espera nunca terminaria.
                if not ok:
                    motivo = (
                        conteudo
                        if isinstance(conteudo, str)
                        else json.dumps(conteudo, ensure_ascii=False)
                    )
                    print(f"  [ERRO] tentativa {tentativa}: {motivo[:220]}", flush=True)
                else:
                    print(
                        f"  aguardando {campo}={esperado} — agora {atual!r} "
                        f"(tentativa {tentativa})",
                        flush=True,
                    )
                time.sleep(5)
            continue

        ok, conteudo = _chamar(servidor, passo, resultados)
        _mostrar(passo, ok, conteudo)
        resultados.append(conteudo)
        if not ok:
            return 1
    return 0


def _executar_foreach(servidor: Servidor, passo: str, resultados: list[Any]) -> int:
    """`foreach:@4.documentos <ferramenta> ref=@item.referencia` — um por item.

    Uma falha isolada não derruba o lote: documento bloqueado por ciência, sem
    conteúdo ou acima do teto local é registrado e o restante segue.
    """
    cabecalho, _, chamada = passo.partition(" ")
    origem = _resolver(cabecalho.split(":", 1)[1], resultados)
    if not isinstance(origem, list):
        print(f"foreach: {cabecalho} não aponta para uma lista", file=sys.stderr)
        return 2

    itens = cast("list[Any]", origem)
    print(f"\n=== foreach sobre {len(itens)} itens ===", flush=True)
    falhas: list[tuple[int, Any]] = []
    coletados: list[Any] = []
    for posicao, item in enumerate(itens, start=1):
        expandido = chamada
        for token in sorted(set(re.findall(r"@item[\w.]*", chamada)), key=len, reverse=True):
            valor: Any = item
            for parte in token[len("@item") :].lstrip(".").split("."):
                if parte:
                    valor = valor[int(parte)] if isinstance(valor, list) else valor[parte]
            expandido = expandido.replace(token, shlex.quote(str(valor)))
        ok, conteudo = _chamar(servidor, expandido, resultados)
        rotulo = f"{posicao}/{len(itens)}"
        if ok:
            coletados.append(conteudo)
            resumo = conteudo.get("nome_arquivo") if isinstance(conteudo, dict) else None
            print(f"  [ok ] {rotulo} {resumo or ''}", flush=True)
        else:
            falhas.append((posicao, conteudo))
            motivo = (
                conteudo
                if isinstance(conteudo, str)
                else json.dumps(conteudo, ensure_ascii=False)
            )
            print(f"  [ERRO] {rotulo} {motivo[:180]}", flush=True)

    resultados.append(coletados)
    print(f"\n=== foreach: {len(coletados)} concluídos, {len(falhas)} com erro ===")
    return 0


def _manter_aberto(servidor: Servidor, resultados: list[Any]) -> int:
    """Segue lendo passos de stdin sem derrubar o servidor.

    O navegador pertence ao processo do servidor, então encerrá-lo fecha a janela
    e descarta a sessão autenticada. Num fluxo com certificado isso custa um PIN
    novo a cada chamada; aqui o login é feito uma vez e vale para o resto.
    """
    print(
        "\n=== sessão aberta — a janela continua de pé ===\n"
        "Digite um passo por linha (ex.: listar_acervo grau=1g jurisdicao=\"Olinda - Varas\").\n"
        "`sair` ou Ctrl-D encerra e fecha o navegador.",
        flush=True,
    )
    while True:
        try:
            linha = input("\nmcp> ").strip()
        except EOFError:
            print()
            return 0
        if not linha:
            continue
        if linha in {"sair", "quit", "exit"}:
            return 0
        ok, conteudo = _chamar(servidor, linha, resultados)
        _mostrar(linha, ok, conteudo)
        resultados.append(conteudo)


def main(argv: list[str]) -> int:
    if not argv or argv[0] in {"-h", "--help"}:
        print(__doc__)
        return 0
    acao, resto = argv[0], argv[1:]

    with Servidor() as servidor:
        if acao == "tools":
            ferramentas = _tools(servidor)
            print(f"{len(ferramentas)} ferramentas\n")
            for t in sorted(ferramentas, key=lambda item: item["name"]):
                print(f"  {t['name']:34s} {t.get('description', '')}")
            return 0

        if acao == "schema":
            if not resto:
                print("uso: schema <ferramenta>", file=sys.stderr)
                return 2
            for t in _tools(servidor):
                if t["name"] == resto[0]:
                    print(json.dumps(t, ensure_ascii=False, indent=2))
                    return 0
            print(f"ferramenta {resto[0]!r} não existe neste servidor", file=sys.stderr)
            return 1

        if acao == "chain":
            manter = "--keep" in resto
            passos = [item for item in resto if item != "--keep"]
            if not passos and not manter:
                print("uso: chain [--keep] \"<ferramenta> args\" [...]", file=sys.stderr)
                return 2
            resultados: list[Any] = []
            codigo = _executar_chain(servidor, passos, resultados)
            if manter:
                return _manter_aberto(servidor, resultados)
            return codigo

        if acao == "call":
            if not resto:
                print("uso: call <ferramenta> [chave=valor ...]", file=sys.stderr)
                return 2
            nome, pares = resto[0], resto[1:]
            argumentos = {
                chave: _coerce(valor)
                for chave, _, valor in (par.partition("=") for par in pares)
            }
            resposta = servidor.request("tools/call", {"name": nome, "arguments": argumentos})
            if "error" in resposta:
                print(json.dumps(resposta["error"], ensure_ascii=False, indent=2))
                return 1
            resultado = resposta["result"]
            # O conteúdo estruturado é o que o modelo consome; o texto é o espelho dele.
            if "structuredContent" in resultado:
                print(json.dumps(resultado["structuredContent"], ensure_ascii=False, indent=2))
            else:
                for bloco in resultado.get("content", []):
                    print(bloco.get("text", json.dumps(bloco, ensure_ascii=False)))
            if resultado.get("isError"):
                return 1
            return 0

    print(f"ação desconhecida: {acao}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
