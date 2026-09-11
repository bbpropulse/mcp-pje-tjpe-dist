"""Identidade do código que está em execução.

O servidor stdio é iniciado pelo cliente e vive enquanto a sessão dura, então uma
edição só passa a valer depois de reconectar. Sem um identificador na resposta,
descobrir se uma correção entrou exige inferir pela presença de campos — o que já
levou a conclusões erradas nesta base.
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent.parent


@lru_cache(maxsize=1)
def revisao_carregada() -> str | None:
    """Commit curto do código em execução, com sufixo quando há edição não commitada."""
    try:
        commit = subprocess.run(
            ["git", "-C", str(_RAIZ), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if commit.returncode != 0:
            return None
        revisao = commit.stdout.strip()
        sujo = subprocess.run(
            ["git", "-C", str(_RAIZ), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if sujo.returncode == 0 and sujo.stdout.strip():
            return f"{revisao}+editado"
        return revisao
    except (OSError, subprocess.SubprocessError):
        return None
