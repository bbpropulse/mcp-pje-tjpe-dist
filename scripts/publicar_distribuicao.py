#!/usr/bin/env python
"""Gera o snapshot público deste MCP no repositório de distribuição.

O repositório de desenvolvimento é privado e o histórico dele não pode ir a público:
commits antigos carregam números de processos reais do acervo de um escritório. A
distribuição, por isso, nasce com histórico próprio — um commit por versão, com a
tag correspondente — a partir de ``git archive HEAD`` (só o que está rastreado; nada
de duplicatas do Finder, cache ou ambiente virtual).

    uv run python scripts/publicar_distribuicao.py            # sincroniza e commita
    uv run python scripts/publicar_distribuicao.py --push     # ... e envia com a tag
    uv run python scripts/publicar_distribuicao.py --sem-testes

Antes de copiar, o script exige árvore limpa em ``main``, roda ``ruff check`` e a
suíte (a menos que ``--sem-testes``), e varre o snapshot procurando o que já vazou
uma vez: NPU com dígito verificador válido fora da faixa sintética (ano 2098/2099),
CPF com dígitos válidos, e-mail fora dos domínios de exemplo e caminho pessoal.
Qualquer achado interrompe a publicação.

O destino padrão é ``../mcp-pje-tjpe-dist``. Se a pasta não existir, ela é
inicializada com ``git init`` e o remoto público; criar o repositório no GitHub é
passo único e manual (``gh repo create bbpropulse/mcp-pje-tjpe-dist --public``).
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DESTINO_PADRAO = RAIZ.parent / "mcp-pje-tjpe-dist"
REMOTO = "https://github.com/bbpropulse/mcp-pje-tjpe-dist.git"

# As bordas excluem letras e dígitos: onze dígitos dentro de um hash hexadecimal
# (uv.lock) não são um CPF.
_NPU = re.compile(
    r"(?<![0-9A-Za-z])(\d{7})-(\d{2})\.(\d{4})\.(\d)\.(\d{2})\.(\d{4})(?![0-9A-Za-z])"
)
_CPF = re.compile(r"(?<![0-9A-Za-z])(\d{3})\.?(\d{3})\.?(\d{3})-?(\d{2})(?![0-9A-Za-z])")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_CAMINHO_PESSOAL = re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+")
_ANOS_SINTETICOS = {"2098", "2099"}
_DOMINIOS_DE_EXEMPLO = ("exemplo.com", "example.com", "example.jus.br", "anthropic.com")
_EXTENSOES_TEXTO = {".py", ".md", ".toml", ".yaml", ".yml", ".txt", ".json", ".lock", ".cfg"}


def _sh(*comando: str, cwd: Path, capturar: bool = True) -> str:
    resultado = subprocess.run(
        comando,
        cwd=cwd,
        check=True,
        text=True,
        capture_output=capturar,
    )
    return (resultado.stdout or "").strip()


def _falhar(mensagem: str) -> int:
    print(f"erro: {mensagem}", file=sys.stderr)
    return 1


def _versao() -> str:
    with (RAIZ / "pyproject.toml").open("rb") as arquivo:
        return str(tomllib.load(arquivo)["project"]["version"])


def _npu_valido(seq: str, dv: str, ano: str, j: str, tr: str, origem: str) -> bool:
    return f"{98 - int(f'{seq}{ano}{j}{tr}{origem}00') % 97:02d}" == dv


def _cpf_valido(digitos: str) -> bool:
    if len(set(digitos)) == 1:
        return False
    for tamanho in (9, 10):
        pesos = range(tamanho + 1, 1, -1)
        soma = sum(int(d) * peso for d, peso in zip(digitos[:tamanho], pesos, strict=True))
        esperado = (soma * 10 % 11) % 10
        if esperado != int(digitos[tamanho]):
            return False
    return True


def varrer_vazamentos(pasta: Path) -> list[str]:
    """Aponta, por arquivo e linha, o que não pode ir a público."""
    achados: list[str] = []
    for arquivo in sorted(pasta.rglob("*")):
        if not arquivo.is_file() or arquivo.suffix not in _EXTENSOES_TEXTO:
            continue
        relativo = arquivo.relative_to(pasta)
        for numero, linha in enumerate(arquivo.read_text(encoding="utf-8").splitlines(), 1):
            for m in _NPU.finditer(linha):
                if m.group(3) not in _ANOS_SINTETICOS and _npu_valido(*m.groups()):
                    achados.append(f"{relativo}:{numero}: NPU com dígito válido: {m.group(0)}")
            for m in _CPF.finditer(linha):
                if _cpf_valido("".join(m.groups())):
                    achados.append(f"{relativo}:{numero}: CPF com dígitos válidos: {m.group(0)}")
            for m in _EMAIL.finditer(linha):
                dominio = m.group(1).lower()
                if not dominio.endswith(_DOMINIOS_DE_EXEMPLO) and not dominio.endswith(".jus.br"):
                    achados.append(f"{relativo}:{numero}: e-mail fora dos exemplos: {m.group(0)}")
            for m in _CAMINHO_PESSOAL.finditer(linha):
                achados.append(f"{relativo}:{numero}: caminho pessoal: {m.group(0)}")
    return achados


def _exportar_snapshot(destino: Path) -> None:
    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as temporario:
        caminho_tar = Path(temporario.name)
    try:
        subprocess.run(
            ["git", "archive", "--format=tar", "-o", str(caminho_tar), "HEAD"],
            cwd=RAIZ,
            check=True,
        )
        with tarfile.open(caminho_tar) as tar:
            tar.extractall(destino, filter="data")
    finally:
        caminho_tar.unlink(missing_ok=True)


def _preparar_destino(destino: Path) -> None:
    if not destino.exists():
        destino.mkdir(parents=True)
        _sh("git", "init", "-b", "main", cwd=destino)
        _sh("git", "remote", "add", "origin", REMOTO, cwd=destino)
        print(f"destino inicializado em {destino} com remoto {REMOTO}")
        return
    if not (destino / ".git").is_dir():
        raise SystemExit(_falhar(f"{destino} existe, mas não é um repositório git"))
    if _sh("git", "status", "--porcelain", cwd=destino):
        raise SystemExit(_falhar(f"{destino} tem alterações locais; resolva antes de publicar"))


def _substituir_conteudo(destino: Path, snapshot: Path) -> None:
    for item in destino.iterdir():
        if item.name == ".git":
            continue
        if item.is_dir() and not item.is_symlink():
            shutil.rmtree(item)
        else:
            item.unlink()
    for item in snapshot.iterdir():
        alvo = destino / item.name
        if item.is_dir():
            shutil.copytree(item, alvo, symlinks=False)
        else:
            shutil.copy2(item, alvo)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Publica o snapshot desta versão na distribuição.")
    parser.add_argument("--destino", type=Path, default=DESTINO_PADRAO)
    parser.add_argument("--push", action="store_true", help="envia main e a tag ao remoto")
    parser.add_argument("--sem-testes", action="store_true", help="pula ruff e pytest")
    args = parser.parse_args(argv)
    destino: Path = args.destino.resolve()

    if _sh("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=RAIZ) != "main":
        return _falhar("publique a partir de main")
    if _sh("git", "status", "--porcelain", cwd=RAIZ):
        return _falhar("a árvore de desenvolvimento tem alterações não commitadas")
    versao = _versao()
    tag = f"v{versao}"

    if not args.sem_testes:
        print("ruff check …")
        subprocess.run(["uv", "run", "ruff", "check", "."], cwd=RAIZ, check=True)
        print("pytest …")
        subprocess.run(
            ["uv", "run", "pytest", "-q", "-p", "no:cacheprovider"], cwd=RAIZ, check=True
        )

    with tempfile.TemporaryDirectory(prefix="mcp-pje-tjpe-snapshot-") as pasta:
        snapshot = Path(pasta)
        _exportar_snapshot(snapshot)
        achados = varrer_vazamentos(snapshot)
        if achados:
            print("\n".join(achados), file=sys.stderr)
            return _falhar(f"{len(achados)} achado(s) impedem a publicação")
        _preparar_destino(destino)
        _substituir_conteudo(destino, snapshot)

    _sh("git", "add", "-A", cwd=destino)
    if not _sh("git", "status", "--porcelain", cwd=destino):
        print(f"nada mudou em relação ao snapshot já presente em {destino}")
    else:
        _sh("git", "commit", "-q", "-m", f"{tag} — mcp-pje-tjpe {versao}", cwd=destino)
        print(f"commit criado em {destino}")
    tags = _sh("git", "tag", "--list", tag, cwd=destino)
    if not tags:
        _sh("git", "tag", "-a", tag, "-m", f"mcp-pje-tjpe {versao}", cwd=destino)
        print(f"tag {tag} criada")
    if args.push:
        subprocess.run(["git", "push", "-u", "origin", "main"], cwd=destino, check=True)
        subprocess.run(["git", "push", "origin", tag], cwd=destino, check=True)
        print("enviado")
    else:
        print(f"pronto; revise {destino} e rode novamente com --push para enviar")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
