from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import mcp_pje_tjpe.build as build
from mcp_pje_tjpe.build import revisao_carregada


@pytest.fixture(autouse=True)
def _sem_cache() -> None:
    revisao_carregada.cache_clear()


def _resposta(codigo: int, saida: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=codigo, stdout=saida, stderr="")


def test_a_clean_tree_reports_the_bare_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    respostas = iter([_resposta(0, "abc1234\n"), _resposta(0, "")])
    monkeypatch.setattr(build.subprocess, "run", lambda *_a, **_k: next(respostas))

    assert revisao_carregada() == "abc1234"


def test_an_edited_tree_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """Servidor rodando código não commitado é a origem de conclusões erradas."""
    respostas = iter([_resposta(0, "abc1234\n"), _resposta(0, " M src/x.py\n")])
    monkeypatch.setattr(build.subprocess, "run", lambda *_a, **_k: next(respostas))

    assert revisao_carregada() == "abc1234+editado"


@pytest.mark.parametrize(
    "falha",
    [
        lambda *_a, **_k: _resposta(128, ""),
        lambda *_a, **_k: (_ for _ in ()).throw(FileNotFoundError("git")),
        lambda *_a, **_k: (_ for _ in ()).throw(subprocess.TimeoutExpired("git", 5)),
    ],
)
def test_without_git_the_answer_is_absent_not_wrong(
    falha: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(build.subprocess, "run", falha)

    assert revisao_carregada() is None


def test_the_real_repository_answers_something(tmp_path: Path) -> None:
    _ = tmp_path
    revisao = revisao_carregada()
    assert revisao is None or len(revisao.split("+")[0]) >= 7
